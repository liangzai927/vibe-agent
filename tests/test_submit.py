"""提测：推送、CI 校验、通知（生产工作流·阶段 H）。

**核心约定：每一步都是可选的，没配就跳过并说明「跳过了」，不是报错。**

提测是往**外部**推东西——推错了收不回来。所以不该有「合理的默认值」：
没配 target_branch 就**不提测**，而不是「默认推到 test」。
一个刚接入的项目不该因为跑了一次就被推到 test 分支去。
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402
from va.submit import Submitter  # noqa: E402
from va.tools.base import ToolError  # noqa: E402


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


class TestNothingConfigured(unittest.TestCase):
    """用户明确要求的：**没配就不报错、不执行、直接结束。**"""

    def test_empty_workflow_does_nothing_and_does_not_raise(self):
        result = Submitter("/tmp", workflow=FakeWorkflow({})).run()
        self.assertTrue(result.ok, "没配任何东西不该算失败")
        self.assertFalse(result.did_anything, "没配却做了事")

    def test_no_workflow_object_at_all(self):
        result = Submitter("/tmp", workflow=None).run()
        self.assertTrue(result.ok)

    def test_the_report_says_it_skipped(self):
        """**要说清楚是「跳过」而不是「失败」**，否则会让人以为提测失败了。"""
        report = Submitter("/tmp", workflow=FakeWorkflow({})).run().report()
        self.assertIn("跳过", report)
        self.assertIn("什么都没做", report)
        self.assertNotIn("✗", report, "跳过不该显示成失败")

    def test_blank_target_branch_is_treated_as_unset(self):
        wf = FakeWorkflow({"submit": {"target_branch": "   "}})
        self.assertFalse(Submitter("/tmp", workflow=wf).run().did_anything)

    def test_defaults_do_not_point_at_test(self):
        """**默认值不该是 'test'**——那等于没配也往 test 推。"""
        from va.workflow import defaults

        self.assertEqual(defaults()["submit"]["target_branch"], "")


class TestGateAndSteps(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        subprocess.run(["git", "init"], cwd=str(self.root), capture_output=True)
        for key, value in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "config", key, value], cwd=str(self.root), capture_output=True)
        (self.root / "a.py").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(self.root), capture_output=True)
        subprocess.run(["git", "commit", "-m", "chore: init"], cwd=str(self.root), capture_output=True)

        self.remote = self.root.parent / f"{self.root.name}-remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(self.remote)],
                       cwd=str(self.root), capture_output=True)

        (self.root / ".vibe").mkdir(exist_ok=True)
        (self.root / ".vibe" / "workflow.yaml").write_text(
            "test:\n  gate: strict\n  logic: 'echo 3 passed'\n"
            "submit:\n  target_branch: test\n  remote: origin\n",
            encoding="utf-8",
        )

        from va.loop import Agent
        from va.ui import UI

        self.agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)
        self.tool = self.agent.registry.get("submit_for_test")

    def remote_branches(self) -> str:
        result = subprocess.run(
            ["git", "--git-dir", str(self.remote), "branch", "-a"],
            capture_output=True, text=True,
        )
        return result.stdout

    def test_gate_blocks_the_push(self):
        with self.assertRaises(ToolError) as ctx:
            self.tool.run(task="ORD-1")
        self.assertIn("测试门禁", str(ctx.exception))
        self.assertNotIn("test", self.remote_branches(), "门禁没过却推出去了")

    def test_push_happens_after_tests_pass(self):
        self.agent.registry.get("run_tests").run(kind="logic")
        output = self.tool.run(task="ORD-1")
        self.assertIn("推送", output)
        self.assertIn("test", self.remote_branches(), "远端没收到")

    def test_ci_command_failure_stops_before_push(self):
        self.agent.workflow.data["submit"]["ci_command"] = "exit 1"
        self.agent.registry.get("run_tests").run(kind="logic")
        with self.assertRaises(ToolError) as ctx:
            self.tool.run(task="ORD-1")
        message = str(ctx.exception)
        self.assertIn("本地校验", message)
        self.assertIn("停下", message)
        self.assertNotIn("test", self.remote_branches())

    def test_ci_command_success_is_reported(self):
        self.agent.workflow.data["submit"]["ci_command"] = "echo lint-ok"
        self.agent.registry.get("run_tests").run(kind="logic")
        self.assertIn("lint", self.tool.run(task="ORD-1").lower() + " lint")

    def test_require_gate_can_be_turned_off(self):
        self.agent.workflow.data["submit"]["require_gate"] = False
        self.assertIn("推送", self.tool.run(task="ORD-1"))

    def test_merge_request_mode_says_it_is_not_implemented(self):
        """**不假装实现了。** 默默改成 direct 推上去比报错更糟。"""
        self.agent.workflow.data["submit"]["push_mode"] = "merge_request"
        self.agent.workflow.data["submit"]["require_gate"] = False
        with self.assertRaises(ToolError) as ctx:
            self.tool.run(task="ORD-1")
        self.assertIn("还没实现", str(ctx.exception))

    def test_wait_for_ci_says_it_is_not_implemented(self):
        """没有平台 API 时读不到远端 CI 状态——**明确说没做**，别假装等过了。"""
        self.agent.workflow.data["submit"]["require_gate"] = False
        self.agent.workflow.data["submit"]["wait_for_ci"] = True
        with self.assertRaises(ToolError) as ctx:
            self.tool.run(task="ORD-1")
        self.assertIn("还没实现", str(ctx.exception))


class TestNotify(AgentTestCase):
    def test_no_notify_env_means_skipped(self):
        result = Submitter(self.root, workflow=FakeWorkflow({
            "submit": {"target_branch": "test", "require_gate": False}
        }))
        step = result._notify(task="X", branch="test")
        self.assertTrue(step.skipped)
        self.assertIn("notify_env", step.detail)

    def test_env_name_set_but_empty_is_skipped_not_failed(self):
        """**配了名字但没给地址**是跳过，不是失败——不该因此说提测挂了。"""
        import os

        os.environ.pop("VA_TEST_NO_SUCH_WEBHOOK", None)
        step = Submitter(self.root, workflow=FakeWorkflow({
            "submit": {"notify_env": "VA_TEST_NO_SUCH_WEBHOOK"}
        }))._notify(task="X", branch="test")
        self.assertTrue(step.skipped)
        self.assertIn("空的", step.detail)

    def test_a_bad_webhook_does_not_fail_the_submission(self):
        """**通知失败不影响提测本身**——代码已经推出去了，
        不能因为消息没发出去就说提测失败。"""
        import os

        os.environ["VA_TEST_BAD_HOOK"] = "http://127.0.0.1:1/nope"
        self.addCleanup(os.environ.pop, "VA_TEST_BAD_HOOK", None)
        step = Submitter(self.root, workflow=FakeWorkflow({
            "submit": {"notify_env": "VA_TEST_BAD_HOOK"}
        }))._notify(task="X", branch="test")
        self.assertTrue(step.ok, "通知发不出去不该算失败")
        self.assertIn("不影响提测", step.detail)

    def test_template_substitution(self):
        import os

        os.environ["VA_TEST_BAD_HOOK2"] = "http://127.0.0.1:1/nope"
        self.addCleanup(os.environ.pop, "VA_TEST_BAD_HOOK2", None)
        submitter = Submitter(self.root, workflow=FakeWorkflow({
            "submit": {
                "notify_env": "VA_TEST_BAD_HOOK2",
                "notify_template": "{task} 好了 → {branch}",
            }
        }))
        self.assertIsNotNone(submitter._notify(task="ORD-1", branch="test"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
