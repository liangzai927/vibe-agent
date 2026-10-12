"""测试门禁：不绿不许提交（生产工作流·阶段 D）。

**为什么这是「机制」不是「工具」**：提示词里写「请确保测试通过」是祈祷——
模型可能看错、可能乐观、可能忘了跑。要让它成立，必须让**提交这个动作本身**
在测试没过时失败。

两条判定缺一不可：
  1. 该跑的测试都跑了、都过了
  2. **测试发生在最后一次代码改动之后** ← 挡的是「拿旧结果提交没测过的代码」
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402
from va.gate import KIND_LABELS, TestLedger, fingerprint  # noqa: E402
from va.tools.base import ToolError  # noqa: E402
from va.tools.tests import parse_counts  # noqa: E402


class FakeWorkflow:
    def __init__(self, data: dict) -> None:
        self.data = data

    def get(self, path: str, fallback=None):
        node = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return fallback
            node = node[part]
        return node


class TestCountParsing(unittest.TestCase):
    def test_pytest_style(self):
        self.assertEqual(parse_counts("42 passed in 1.2s", 0), (42, 0))

    def test_pytest_failures_first(self):
        self.assertEqual(parse_counts("3 failed, 42 passed", 1), (42, 3))

    def test_jest_style(self):
        self.assertEqual(parse_counts("Tests:       1 failed, 5 passed, 6 total", 1), (5, 1))

    def test_vitest_style(self):
        self.assertEqual(parse_counts("8 passing", 0), (8, 0))
        self.assertEqual(parse_counts("2 failing", 1), (0, 2))

    def test_unparseable_falls_back_to_the_exit_code(self):
        """**猜错不如不猜**：宁可记成「一次运行」，也不要因为解析出个奇怪的
        数字让门禁做出错误判断。"""
        self.assertEqual(parse_counts("all good", 0), (1, 0))
        self.assertEqual(parse_counts("boom", 1), (0, 1))

    def test_the_bare_passed_pattern_is_covered(self):
        """**漏过一次**：只写了 `passing`，结果最常见的 `42 passed` 认不出来——
        门禁不受影响，但人看到的数字是错的。"""
        self.assertEqual(parse_counts("42 passed", 0), (42, 0))


class TestRequiredKinds(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = TestLedger()

    def test_nothing_configured_means_nothing_required(self):
        """**没配测试的项目不受影响**——不能因为它没配就挡住提交。"""
        self.assertEqual(self.ledger.required_kinds(FakeWorkflow({})), [])

    def test_logic_requires_a_command(self):
        wf = FakeWorkflow({"test": {"logic": "npm test"}})
        self.assertEqual(self.ledger.required_kinds(wf), ["logic"])

    def test_e2e_requires_both_start_and_url(self):
        only_url = FakeWorkflow({"test": {"e2e_base_url": "http://x"}})
        self.assertEqual(self.ledger.required_kinds(only_url), [])
        both = FakeWorkflow({"test": {"start": "npm run dev", "e2e_base_url": "http://x"}})
        self.assertEqual(self.ledger.required_kinds(both), ["e2e"])

    def test_visual_requires_designs(self):
        wf = FakeWorkflow({"test": {"visual_designs": "docs/mockups"}})
        self.assertEqual(self.ledger.required_kinds(wf), ["visual"])


class TestEvaluate(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = TestLedger()
        self.wf = FakeWorkflow({"test": {"gate": "strict", "logic": "npm test"}})

    def test_gate_off_never_blocks(self):
        wf = FakeWorkflow({"test": {"gate": "off", "logic": "npm test"}})
        self.assertTrue(self.ledger.evaluate(wf).ok)

    def test_no_tests_configured_never_blocks(self):
        self.assertTrue(self.ledger.evaluate(FakeWorkflow({"test": {"gate": "strict"}})).ok)

    def test_missing_run_blocks(self):
        verdict = self.ledger.evaluate(self.wf)
        self.assertFalse(verdict.ok)
        self.assertIn("从来没跑过", "\n".join(verdict.problems))

    def test_failure_blocks(self):
        self.ledger.record("logic", passed=3, failed=2)
        verdict = self.ledger.evaluate(self.wf)
        self.assertFalse(verdict.ok)
        self.assertIn("2 个失败", "\n".join(verdict.problems))

    def test_passing_allows(self):
        self.ledger.record("logic", passed=42, failed=0)
        self.assertTrue(self.ledger.evaluate(self.wf).ok)

    def test_stale_result_blocks(self):
        """**这条挡的是最危险的情况**：改完代码 → 拿旧结果过门禁 →
        提交了从没测过的代码。"""
        self.ledger.record("logic", passed=42, failed=0, fingerprint="aaa")
        verdict = self.ledger.evaluate(self.wf, current="bbb")
        self.assertFalse(verdict.ok)
        self.assertIn("又改过", "\n".join(verdict.problems))

    def test_same_fingerprint_allows(self):
        self.ledger.record("logic", passed=42, failed=0, fingerprint="aaa")
        self.assertTrue(self.ledger.evaluate(self.wf, current="aaa").ok)

    def test_zero_passed_is_not_a_pass(self):
        self.ledger.record("logic", passed=0, failed=0)
        self.assertFalse(self.ledger.evaluate(self.wf).ok, "一个用例都没跑不算通过")


class TestFingerprint(AgentTestCase):
    def test_changes_when_a_file_changes(self):
        """内容敏感：同样大小但内容改了也要算变化。"""
        self.git("init")
        target = self.root / "a.py"
        target.write_text("x = 1\n", encoding="utf-8")
        first = fingerprint(self.root)

        target.write_text("x = 2\n", encoding="utf-8")
        self.assertNotEqual(first, fingerprint(self.root), "内容变了指纹没变")

    def test_changes_when_a_file_is_added(self):
        self.git("init")
        (self.root / "a.py").write_text("x\n", encoding="utf-8")
        first = fingerprint(self.root)
        (self.root / "b.py").write_text("y\n", encoding="utf-8")
        self.assertNotEqual(first, fingerprint(self.root))

    def test_stable_when_nothing_changes(self):
        self.git("init")
        (self.root / "a.py").write_text("x\n", encoding="utf-8")
        self.assertEqual(fingerprint(self.root), fingerprint(self.root))

    def test_non_git_workspace_returns_empty(self):
        """非 git 仓库退化成「必须跑过一次测试」——不能因为它不是仓库就崩。"""
        self.assertEqual(fingerprint(self.root), "")

    def git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=str(self.root), capture_output=True, check=False
        )


class TestCommitIsBlocked(AgentTestCase):
    """端到端：门禁确实拦在 git_commit 里面。"""

    def setUp(self) -> None:
        super().setUp()
        from va.loop import Agent
        from va.ui import UI

        subprocess.run(["git", "init"], cwd=str(self.root), capture_output=True)
        for key, value in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(
                ["git", "config", key, value], cwd=str(self.root), capture_output=True
            )
        (self.root / "a.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(self.root), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"], cwd=str(self.root), capture_output=True
        )

        config = self.root / ".vibe" / "workflow.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            "test:\n  gate: strict\n  logic: 'echo 3 passed'\n", encoding="utf-8"
        )

        self.agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)
        self.commit = self.agent.registry.get("git_commit")
        self.run_tests = self.agent.registry.get("run_tests")

    def change_a_file(self, text: str = "x = 2\n") -> None:
        (self.root / "a.py").write_text(text, encoding="utf-8")

    def test_commit_without_tests_is_refused(self):
        self.change_a_file()
        with self.assertRaises(ToolError) as ctx:
            self.commit.run(message="feat: 改点东西")
        message = str(ctx.exception)
        self.assertIn("门禁", message)
        self.assertIn("从来没跑过", message)

    def test_commit_after_tests_pass(self):
        self.change_a_file()
        self.run_tests.run(kind="logic")
        self.assertIn("提交成功", self.commit.run(message="feat: 改点东西"))

    def test_commit_after_changing_code_again_is_refused(self):
        self.run_tests.run(kind="logic")
        self.change_a_file("x = 3\n")   # 测试之后又改了
        with self.assertRaises(ToolError) as ctx:
            self.commit.run(message="feat: 又改")
        self.assertIn("又改过", str(ctx.exception))

    def test_override_allows_but_leaves_a_trace(self):
        """豁免要留痕：写进会话记录，**也写进 commit message**——
        事后查 git log 的人也该看得见。"""
        self.change_a_file()
        result = self.commit.run(
            message="docs: 只改了文档", override_gate="纯文档改动，不需要测试"
        )
        self.assertIn("提交成功", result)

        log = subprocess.run(
            ["git", "log", "-1", "--pretty=%s"], cwd=str(self.root),
            capture_output=True, text=True,
        ).stdout
        self.assertIn("绕过测试门禁", log)
        self.assertIn("纯文档改动", log)

    def test_no_test_config_means_no_blocking(self):
        """没配测试的项目照常提交。"""
        config = self.root / ".vibe" / "workflow.yaml"
        config.write_text("test:\n  gate: strict\n", encoding="utf-8")

        from va.loop import Agent
        from va.ui import UI

        agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)
        self.change_a_file()
        result = agent.registry.get("git_commit").run(message="chore: 没配测试")
        self.assertIn("提交成功", result)


class TestRunTestsTool(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        from va.loop import Agent
        from va.ui import UI

        self.agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)
        self.tool = self.agent.registry.get("run_tests")

    def test_no_command_configured_explains_where_to_put_it(self):
        with self.assertRaises(ToolError) as ctx:
            self.tool.run(kind="logic")
        self.assertIn("workflow.yaml", str(ctx.exception))

    def test_runs_and_records(self):
        self.agent.workflow.data.setdefault("test", {})["logic"] = "echo 7 passed"
        output = self.tool.run(kind="logic")
        self.assertIn("7 通过", output)
        run = self.agent.ledger.latest("logic")
        self.assertIsNotNone(run)
        self.assertTrue(run.ok)

    def test_failure_is_recorded_and_hinted(self):
        self.agent.workflow.data.setdefault("test", {})["logic"] = "exit 1"
        output = self.tool.run(kind="logic")
        self.assertIn("1 失败", output)
        self.assertFalse(self.agent.ledger.latest("logic").ok)
        self.assertIn("override_gate", output, "要告诉调用方怎么办")

    def test_unknown_kind(self):
        with self.assertRaises(ToolError) as ctx:
            self.tool.run(kind="nonsense")
        self.assertIn("不认识", str(ctx.exception))

    def test_timeout_is_recorded_as_a_failure(self):
        self.agent.workflow.data.setdefault("test", {})["logic"] = "sleep 10"
        with self.assertRaises(ToolError):
            self.tool.run(kind="logic", timeout=1)
        self.assertFalse(self.agent.ledger.latest("logic").ok)

    def test_labels_cover_every_kind(self):
        for kind in ("logic", "e2e", "visual"):
            self.assertIn(kind, KIND_LABELS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
