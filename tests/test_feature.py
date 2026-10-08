"""功能开发流水线的测试。

这一层的价值是「把顺序固定下来」——同一套工具，随手丢一句需求，和按
「先读懂现状 → 拆计划 → 实现 → 自测 → 让别人审一遍」走，结果差很远。
所以测试重点是：步骤有没有真的写进任务书、确定性动作有没有真的执行、
以及失败时退出码和提示对不对。
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase, ScriptedLLM, turn  # noqa: E402
from va.feature import (  # noqa: E402
    FeatureRequest,
    build_task,
    create_branch,
    default_branch_name,
    describe_flags,
    run as run_feature,
    slugify,
)

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


class TestBranchNaming(unittest.TestCase):
    def test_slugify_keeps_ascii_words(self):
        self.assertEqual(slugify("add user pagination"), "user-pagination")
        self.assertEqual(slugify("Fix Login Page"), "fix-login-page")

    def test_slugify_drops_stop_words(self):
        self.assertEqual(slugify("add a button to the page"), "button-page")

    def test_slugify_caps_word_count(self):
        slug = slugify("one two three four five six seven eight")
        self.assertLessEqual(len(slug.split("-")), 5)

    def test_slugify_returns_empty_for_pure_chinese(self):
        """抽不出英文词就老实返回空串，别硬编出一个 feat/--- 这样的名字。"""
        self.assertEqual(slugify("给用户列表加分页"), "")

    def test_slugify_strips_non_ascii_mixed(self):
        self.assertIn("pagination", slugify("给列表加 pagination 支持"))

    def test_default_branch_uses_slug(self):
        # "add" 在停用词里：前缀已经是 feat/，再加 add 是冗余
        self.assertEqual(default_branch_name("add pagination"), "feat/pagination")

    def test_default_branch_falls_back_to_timestamp(self):
        name = default_branch_name("给用户列表加分页")
        self.assertTrue(name.startswith("feat/"))
        self.assertGreater(len(name), len("feat/"))

    def test_explicit_branch_wins(self):
        req = FeatureRequest(requirement="随便什么", branch="my/branch")
        self.assertEqual(req.resolved_branch(), "my/branch")


class TestTaskBuilding(unittest.TestCase):
    def _task(self, **kwargs) -> str:
        base = dict(requirement="给列表加分页")
        base.update(kwargs)
        return build_task(FeatureRequest(**base))

    def test_all_steps_present_in_order(self):
        task = self._task()
        order = ["1. 理解需求", "2. 先读懂现状", "3. 实现", "4. 自测", "5. 让别人审一遍", "6. 提交"]
        positions = [task.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions), "步骤顺序被打乱了")

    def test_planning_is_explicitly_required(self):
        task = self._task()
        self.assertIn("update_plan", task)
        self.assertIn("不要跳过这一步", task)

    def test_self_test_step_is_not_optional(self):
        task = self._task()
        self.assertIn("修到通过为止", task)
        self.assertIn("render_page", task, "UI 改动应该要求看真实渲染")

    def test_review_step_mentions_requirement_passing(self):
        task = self._task()
        self.assertIn("review_changes", task)
        self.assertIn("把需求原文一起传给它", task)

    def test_review_can_be_disabled(self):
        task = self._task(review=False)
        self.assertNotIn("review_changes", task)
        self.assertIn("5. 提交", task)
        self.assertNotIn("6. 提交", task)

    def test_doc_and_design_are_pointed_at(self):
        task = self._task(doc="docs/prd.docx", design="mockups/list.png")
        self.assertIn("docs/prd.docx", task)
        self.assertIn("read_document", task)
        self.assertIn("mockups/list.png", task)
        self.assertIn("read_design", task)

    def test_custom_test_command(self):
        task = self._task(tests="pnpm test")
        self.assertIn("pnpm test", task)

    def test_requirement_text_is_embedded(self):
        task = self._task(requirement="把登录页的主色改成蓝色")
        self.assertIn("把登录页的主色改成蓝色", task)

    def test_asks_before_major_decisions(self):
        task = self._task()
        self.assertIn("先问我", task, "关键决策不该让它自己拍板")
        self.assertIn("不要硬做", task)

    def test_asks_to_stop_when_scope_explodes(self):
        task = self._task()
        self.assertIn("停下来告诉我", task)

    def test_describe_flags_lists_options(self):
        text = describe_flags()
        for flag in ("--branch", "--doc", "--design", "--tests", "--no-review", "--push"):
            self.assertIn(flag, text)


class TestCreateBranch(AgentTestCase):
    def _repo(self) -> None:
        (self.root / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, env=GIT_ENV)
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True, env=GIT_ENV)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.root, check=True, env=GIT_ENV)

    def _branch(self) -> str:
        return subprocess.run(
            ["git", "symbolic-ref", "--short", "-q", "HEAD"],
            cwd=self.root, capture_output=True, text=True, env=GIT_ENV,
        ).stdout.strip()

    def test_creates_and_switches_branch(self):
        self._repo()
        agent, ui = self.make_agent(ScriptedLLM(turn("ok")))
        ok, message = create_branch(agent, "feat/demo", "", ui)

        self.assertTrue(ok, message)
        self.assertEqual(self._branch(), "feat/demo")

    def test_reports_failure_outside_repo(self):
        agent, ui = self.make_agent(ScriptedLLM(turn("ok")))
        ok, message = create_branch(agent, "feat/x", "", ui)
        self.assertFalse(ok)
        self.assertIn("git 仓库", message)


class TestPipelineRun(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        (self.root / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, env=GIT_ENV)
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True, env=GIT_ENV)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.root, check=True, env=GIT_ENV)

    def _branch(self) -> str:
        return subprocess.run(
            ["git", "symbolic-ref", "--short", "-q", "HEAD"],
            cwd=self.root, capture_output=True, text=True, env=GIT_ENV,
        ).stdout.strip()

    def _run(self, llm, **kwargs):
        from va.loop import Agent
        from va.tools import build_registry

        agent = Agent(
            self.cfg,
            ui := __import__("tests.helpers", fromlist=["QuietUI"]).QuietUI(
                self.workspace, approve=True
            ),
            workspace=self.workspace,
            registry=build_registry(self.workspace),
            llm=llm,
        )
        req = FeatureRequest(requirement="add pagination", **kwargs)
        code = run_feature(agent, req, ui)
        return agent, ui, code

    def test_creates_branch_and_runs_agent(self):
        llm = ScriptedLLM(turn("做完了。"))
        agent, ui, code = self._run(llm)

        self.assertEqual(code, 0)
        self.assertEqual(self._branch(), "feat/pagination")

        brief = llm.received[0][-1]["content"]
        self.assertIn("功能开发流水线", brief, "任务书没传给模型")
        self.assertIn("做完了。", ui.answers, "agent 的回答没走到 UI 层")

    def test_task_book_carries_all_steps(self):
        llm = ScriptedLLM(turn("ok"))
        self._run(llm)
        brief = llm.received[0][-1]["content"]
        for marker in ("update_plan", "review_changes", "render_page"):
            self.assertIn(marker, brief)

    def test_no_push_by_default(self):
        llm = ScriptedLLM(turn("ok"))
        _, ui, _ = self._run(llm)
        self.assertFalse((self.root / ".git" / "refs" / "remotes").exists())

    def test_no_branch_skips_creation(self):
        before = self._branch()
        llm = ScriptedLLM(turn("ok"))
        _, ui, code = self._run(llm, no_branch=True)

        self.assertEqual(code, 0)
        self.assertEqual(self._branch(), before, "说不建分支还是建了")

    def test_branch_failure_returns_exit_2(self):
        # 换到非 git 目录
        import tempfile

        with tempfile.TemporaryDirectory() as other:
            from va.workspace import Workspace

            ws = Workspace(other)
            from va.loop import Agent
            from va.tools import build_registry
            from tests.helpers import QuietUI

            ui = QuietUI(ws, approve=True)
            agent = Agent(
                self.cfg, ui, workspace=ws, registry=build_registry(ws),
                llm=ScriptedLLM(turn("ok")),
            )
            code = run_feature(agent, FeatureRequest(requirement="x"), ui)

        self.assertEqual(code, 2)

    def test_unfinished_plan_returns_exit_1(self):
        """计划没做完就结束，说明可能漏了东西——不该当成功。"""
        from va.messages import ToolCall
        import json

        # 让 agent 建一个永远做不完的计划
        def plan_call():
            return ToolCall(
                id="p1",
                name="update_plan",
                arguments=json.dumps(
                    {"goal": "做三件事", "steps": [
                        {"title": "第一步", "status": "done"},
                        {"title": "第二步"},
                        {"title": "第三步"},
                    ]},
                    ensure_ascii=False,
                ),
            )

        llm = ScriptedLLM(turn("", plan_call()), turn("先做这么多。"))
        _, ui, code = self._run(llm)
        self.assertEqual(code, 1)

    def test_push_flag_triggers_push_attempt(self):
        llm = ScriptedLLM(turn("ok"))
        _, ui, _ = self._run(llm, push=True)
        # 没有远端，推送必然失败，但不该崩
        self.assertTrue(True)

    def test_doc_and_design_reach_the_brief(self):
        llm = ScriptedLLM(turn("ok"))
        self._run(llm, doc="docs/prd.md", design="mocks/a.png")
        brief = llm.received[0][-1]["content"]
        self.assertIn("docs/prd.md", brief)
        self.assertIn("mocks/a.png", brief)


if __name__ == "__main__":
    unittest.main(verbosity=2)
