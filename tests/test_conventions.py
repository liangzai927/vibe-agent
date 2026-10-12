"""团队约定的校验与生成（生产工作流·阶段 F）。

**为什么在工具层校验，而不是提示词里说「请遵守命名规范」**：
和测试门禁同一个道理——提示词是祈祷。工具**直接拒绝并给出合规示例**
才是真的约束。

**拒绝时必须给可用的例子**：只说「不合规」会让人（和模型）反复试错。
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402
from va import conventions  # noqa: E402
from va.tools.base import ToolError  # noqa: E402
from va.workflow import load  # noqa: E402


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


class TestNoRulesMeansNoValidation(unittest.TestCase):
    """**没配规则就不校验**——不能因为一个项目没写 workflow.yaml 就挡住它干活。"""

    def test_branch(self):
        self.assertEqual(conventions.check_branch_name("whatever", FakeWorkflow({})), "")

    def test_commit(self):
        self.assertEqual(conventions.check_commit_message("stuff", FakeWorkflow({})), "")

    def test_none_workflow(self):
        self.assertEqual(conventions.check_branch_name("x", None), "")
        self.assertEqual(conventions.check_commit_message("x", None), "")


class TestBranchNames(AgentTestCase):
    def workflow(self, pattern: str = "feature/{ticket}-{slug}"):
        config = self.root / ".vibe" / "workflow.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(f'branch:\n  pattern: "{pattern}"\n', encoding="utf-8")
        return load(self.root)

    def test_generates_from_ticket_and_title(self):
        name = conventions.build_branch_name(
            self.workflow(), ticket="ORD-312", title="Order Filter"
        )
        self.assertEqual(name, "feature/ORD-312-order-filter")

    def test_a_matching_name_passes(self):
        self.assertEqual(
            conventions.check_branch_name("feature/ORD-312-x", self.workflow()), ""
        )

    def test_a_bad_name_is_rejected_with_an_example(self):
        """**拒绝要给可用的例子**，否则只会反复试错。"""
        problem = conventions.check_branch_name("my-branch", self.workflow())
        self.assertIn("不符合团队约定", problem)
        self.assertIn("feature/ORD-312", problem, "没给出合规示例")

    def test_custom_pattern(self):
        name = conventions.build_branch_name(
            self.workflow("hotfix/{ticket}"), ticket="BUG-1", title="x"
        )
        self.assertTrue(name.startswith("hotfix/BUG-1"), name)


class TestCommitMessages(AgentTestCase):
    def workflow(self, body: str) -> object:
        config = self.root / ".vibe" / "workflow.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(body, encoding="utf-8")
        return load(self.root)

    def conventional(self):
        return self.workflow(
            'commit:\n  pattern: "^(feat|fix|chore)(\\\\([a-z0-9-]+\\\\))?: .{4,72}$"\n'
            '  template: "{type}({scope}): {subject} (#{ticket})"\n'
            "  require_ticket: true\n"
            '  forbid_words: ["WIP", "fix bug"]\n'
        )

    def test_builds_from_the_template(self):
        message = conventions.build_commit_message(
            self.conventional(), kind="feat", scope="order",
            subject="列表支持筛选", ticket="ORD-1",
        )
        self.assertEqual(message, "feat(order): 列表支持筛选 (#ORD-1)")

    def test_empty_scope_does_not_leave_empty_parens(self):
        """模板里是 `{type}({scope})`，没 scope 时会变成 `feat(): xxx`。"""
        message = conventions.build_commit_message(
            self.conventional(), kind="fix", subject="改了", ticket="X-1"
        )
        self.assertNotIn("()", message)
        self.assertNotIn("(#)", message)

    def test_a_good_message_passes(self):
        wf = self.conventional()
        self.assertEqual(
            conventions.check_commit_message("feat(order): 加了筛选 (#ORD-1)", wf), ""
        )

    def test_a_bad_message_is_rejected_with_an_example(self):
        problem = conventions.check_commit_message("update", self.conventional())
        self.assertIn("不符合团队约定", problem)
        self.assertIn("feat(", problem, "没给出合规模板")

    def test_forbidden_words(self):
        problem = conventions.check_commit_message(
            "chore: WIP 一下 (#X-1)", self.conventional()
        )
        self.assertIn("WIP", problem)

    def test_missing_ticket(self):
        problem = conventions.check_commit_message(
            "feat(order): 加了筛选", self.conventional()
        )
        self.assertIn("需求号", problem)

    def test_ticket_forms_are_accepted(self):
        wf = self.conventional()
        for text in ("feat(order): 加了筛选 (#ORD-1)", "fix(a): 修了 ORD-9 的问题"):
            self.assertEqual(conventions.check_commit_message(text, wf), "", text)

    def test_a_broken_pattern_in_config_does_not_crash(self):
        """配置里的正则坏了是阶段 A 该报的错，**不该在这里炸**。

        注意要显式关掉 require_ticket：load() 会把 SCHEMA 的默认值带进来
        （「不改的走内置默认值」），不关的话这条测试测的就不是它想说的事了。
        """
        wf = self.workflow(
            'commit:\n  pattern: "([unclosed"\n  require_ticket: false\n'
        )
        self.assertEqual(conventions.check_commit_message("anything", wf), "")


class TestWiredIntoTools(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        from va.loop import Agent
        from va.ui import UI

        subprocess.run(["git", "init"], cwd=str(self.root), capture_output=True)
        for key, value in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "config", key, value], cwd=str(self.root), capture_output=True)
        (self.root / "a.py").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(self.root), capture_output=True)
        subprocess.run(["git", "commit", "-m", "chore: init"], cwd=str(self.root), capture_output=True)

        config = self.root / ".vibe" / "workflow.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            'branch:\n  pattern: "feature/{ticket}-{slug}"\n  require_clean: false\n'
            'commit:\n  pattern: "^(feat|fix|chore)(\\\\([a-z0-9-]+\\\\))?: .{4,72}$"\n'
            '  template: "{type}({scope}): {subject} (#{ticket})"\n'
            "  require_ticket: true\n",
            encoding="utf-8",
        )
        self.agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)

    def test_branch_generated_from_ticket(self):
        output = self.agent.registry.get("git_create_branch").run(
            ticket="ORD-312", title="Order Filter"
        )
        self.assertIn("feature/ORD-312-order-filter", output)

    def test_bad_branch_name_is_refused(self):
        with self.assertRaises(ToolError) as ctx:
            self.agent.registry.get("git_create_branch").run(name="my-branch")
        self.assertIn("feature/ORD-312", str(ctx.exception))

    def test_commit_generated_from_fields(self):
        (self.root / "b.py").write_text("y\n", encoding="utf-8")
        output = self.agent.registry.get("git_commit").run(
            kind="feat", scope="order", subject="加了筛选", ticket="ORD-1"
        )
        self.assertIn("提交成功", output)

    def test_bad_message_is_refused(self):
        (self.root / "b.py").write_text("y\n", encoding="utf-8")
        with self.assertRaises(ToolError) as ctx:
            self.agent.registry.get("git_commit").run(message="update")
        self.assertIn("feat(", str(ctx.exception))

    def test_no_verify_is_never_used(self):
        """**不让绕过 pre-commit hook。** 宁可失败，也不静默跳过检查。"""
        source = (
            Path(__file__).resolve().parent.parent / "va" / "tools" / "git.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("--no-verify", source)


class TestWorkspaceMustBeClean(AgentTestCase):
    def test_dirty_workspace_blocks_branch_switch(self):
        """带着未提交的改动切分支很容易丢东西。"""
        from va.loop import Agent
        from va.ui import UI

        subprocess.run(["git", "init"], cwd=str(self.root), capture_output=True)
        for key, value in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "config", key, value], cwd=str(self.root), capture_output=True)
        (self.root / "a.py").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(self.root), capture_output=True)
        subprocess.run(["git", "commit", "-m", "chore: init"], cwd=str(self.root), capture_output=True)

        config = self.root / ".vibe" / "workflow.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            'branch:\n  pattern: "feature/{ticket}-{slug}"\n  require_clean: true\n',
            encoding="utf-8",
        )
        agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)

        (self.root / "a.py").write_text("改了\n", encoding="utf-8")
        with self.assertRaises(ToolError) as ctx:
            agent.registry.get("git_create_branch").run(ticket="X-1", title="t")
        self.assertIn("干净", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
