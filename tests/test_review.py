"""独立审查的测试。

审查者的价值在于「换个干净上下文、看不到作者的解释」，所以测试重点是：

* 它真的拿不到写权限（否则「独立审查」会变成「边审边改」）
* 未跟踪的新文件也要算进审查范围（新写的代码同样需要审）
* 没有需求原文时，它被明确告知「不要臆测需求」
* 没有改动、不是 git 仓库、ref 打错，这三种情况的提示都要自解释
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase, QuietUI  # noqa: E402
from va.messages import AssistantTurn, ToolCall  # noqa: E402
from va.tools import AgentContext, build_registry  # noqa: E402
from va.tools.base import ToolError  # noqa: E402
from va.tools.review import REVIEWER_SYSTEM, ReviewChanges  # noqa: E402

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}

_counter = {"n": 0}


def call(name: str, **args) -> ToolCall:
    _counter["n"] += 1
    return ToolCall(
        id=f"r{_counter['n']}", name=name, arguments=json.dumps(args, ensure_ascii=False)
    )


def turn(content: str = "", *calls: ToolCall) -> AssistantTurn:
    return AssistantTurn(content=content, tool_calls=list(calls), finish_reason="stop")


class ReviewTestCase(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.git("init", "-q")
        (self.root / "app.py").write_text("def add(a, b):\n    return a - b\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True, env=GIT_ENV
        )
        return result.stdout

    def ctx(self, **kw) -> AgentContext:
        base = dict(workspace=self.workspace, cfg=self.cfg, max_depth=1)
        base.update(kw)
        return AgentContext(**base)

    def tool(self, **kw) -> ReviewChanges:
        return ReviewChanges(self.ctx(**kw))


class TestGuards(ReviewTestCase):
    def test_no_changes_is_reported_clearly(self):
        out = self.tool().run()
        self.assertIn("没有可审查的改动", out)
        self.assertIn("HEAD", out)

    def test_not_a_git_repo_is_explained(self):
        import tempfile

        with tempfile.TemporaryDirectory() as other:
            from va.workspace import Workspace

            ws = Workspace(other)
            tool = ReviewChanges(
                AgentContext(workspace=ws, cfg=self.cfg, max_depth=1)
            )
            with self.assertRaises(ToolError) as ctx:
                tool.run()
            message = str(ctx.exception)
            self.assertIn("不是 git 仓库", message)
            self.assertIn("delegate", message, "应该给出替代方案")

    def test_unknown_ref_is_explained(self):
        (self.root / "app.py").write_text("x = 1\n")
        with self.assertRaises(ToolError) as ctx:
            self.tool().run(base="不存在的分支")
        message = str(ctx.exception)
        self.assertIn("不认识", message)
        self.assertIn("git_log", message, "应该告诉它怎么查有哪些提交")

    def test_depth_exhausted_is_refused(self):
        tool = self.tool(depth=1, max_depth=1)
        with self.assertRaises(ToolError) as ctx:
            tool.run()
        self.assertIn("上限", str(ctx.exception))

    def test_no_approval_and_no_snapshots(self):
        tool = self.tool()
        self.assertFalse(tool.requires_approval, "审查是只读的，不该要审批")
        self.assertEqual(tool.affected_paths({}), [])

    def test_registered_only_with_context(self):
        with_ctx = build_registry(self.workspace, cfg=self.cfg, ctx=self.ctx())
        without = build_registry(self.workspace, cfg=self.cfg)
        self.assertIn("review_changes", with_ctx)
        self.assertNotIn("review_changes", without)


class TestCollect(ReviewTestCase):
    def test_counts_changed_lines(self):
        (self.root / "app.py").write_text("def add(a, b):\n    return a + b\n")
        changed, untracked, stat = self.tool()._collect(self.root, "HEAD")
        self.assertEqual(changed, ["app.py"])
        self.assertEqual(untracked, [])
        self.assertIn("1 个已改动文件", stat)
        self.assertIn("+1", stat)
        self.assertIn("−1", stat)

    def test_untracked_files_are_included(self):
        """新写的代码同样需要审，不能因为没 add 就漏掉。"""
        (self.root / "new_module.py").write_text("VALUE = 1\n")
        changed, untracked, stat = self.tool()._collect(self.root, "HEAD")
        self.assertEqual(changed, [])
        self.assertIn("new_module.py", untracked)
        self.assertIn("未跟踪", stat)

    def test_overview_truncates_long_lists(self):
        names = [f"file{i}.py" for i in range(20)]
        text = ReviewChanges._overview(names, [], "20 个文件")
        self.assertIn("还有 8 个", text)
        self.assertIn("file0.py", text)
        self.assertNotIn("file19.py", text)

    def test_overview_lists_both_categories(self):
        text = ReviewChanges._overview(["a.py"], ["b.py"], "1 个已改动")
        self.assertIn("已改动：a.py", text)
        self.assertIn("新文件：b.py", text)


class TestTaskBuilding(ReviewTestCase):
    def test_task_asks_for_context_not_just_diff(self):
        task = ReviewChanges._task("HEAD", "  概览", "需求原文", "")
        self.assertIn("读改动周围的代码", task)
        self.assertIn("git diff", task)

    def test_requirement_is_embedded(self):
        task = ReviewChanges._task("HEAD", "概览", "把 subtract 改成加法", "")
        self.assertIn("把 subtract 改成加法", task)
        self.assertIn("需求原文", task)

    def test_missing_requirement_forbids_guessing(self):
        """没给需求就别让它猜需求——否则会编出一堆「应该这样」的建议。"""
        task = ReviewChanges._task("HEAD", "概览", "", "")
        self.assertIn("未提供", task)
        self.assertIn("不要臆测", task)
        self.assertIn("无法判断", task)

    def test_focus_is_appended(self):
        task = ReviewChanges._task("HEAD", "概览", "需求", "特别注意并发安全")
        self.assertIn("额外关注", task)
        self.assertIn("特别注意并发安全", task)

    def test_ref_is_mentioned(self):
        task = ReviewChanges._task("main~3", "概览", "", "")
        self.assertIn("main~3", task)

    def test_reviewer_prompt_demands_actionable_findings(self):
        prompt = REVIEWER_SYSTEM
        self.assertIn("文件:行号", prompt)
        self.assertIn("为什么是问题", prompt)
        self.assertIn("不要猜", prompt)
        self.assertIn("泛泛而谈", prompt, "应该明确排除「建议加日志」这类噪音")


class ReviewingLLM:
    """父 agent 调 review_changes；审查者只做只读动作。"""

    REVIEWER_MARK = "你是一个**代码审查者**"
    CHILD_MARK = "被主 agent 派来做一件自成一体的活"

    def __init__(self, requirement: str = "把减法改成加法"):
        self.requirement = requirement
        self.calls: list[tuple[str, int, set[str]]] = []
        self.reviewer_task = ""
        self.reviewer_tools: set[str] = set()

    def complete(self, messages, tools=None, on_text=None, on_reasoning=None):
        system = str(messages[0].get("content", "")) if messages else ""
        names = {t["function"]["name"] for t in (tools or [])}
        is_reviewer = self.REVIEWER_MARK in system
        is_child = self.CHILD_MARK in system

        self.calls.append(
            ("reviewer" if is_reviewer else ("child" if is_child else "parent"), len(messages), names)
        )

        if is_reviewer:
            self.reviewer_tools = names
            self.reviewer_task = str(messages[1].get("content", "")) if len(messages) > 1 else ""
            if not any(m.get("role") == "tool" for m in messages):
                return turn("", call("git_diff"))
            return AssistantTurn(content="## 结论\n有问题需修\n\n## 必须修\n1. 无")

        has_tool = any(m.get("role") == "tool" for m in messages)
        if not has_tool:
            return turn("", call("review_changes", requirement=self.requirement))
        return AssistantTurn(content="审查完成。")


class TestReviewEndToEnd(ReviewTestCase):
    def setUp(self) -> None:
        super().setUp()
        (self.root / "app.py").write_text("def add(a, b):\n    return a + b\n")
        (self.root / "helper.py").write_text("VALUE = 1\n")  # 未跟踪

    def _run(self, requirement: str = "把减法改成加法", **kwargs):
        from va.loop import Agent
        from va.tools import build_registry

        llm = ReviewingLLM(requirement)
        agent = Agent(
            self.cfg,
            QuietUI(self.workspace, approve=True),
            workspace=self.workspace,
            registry=build_registry(self.workspace),
            llm=llm,
        )
        return agent, llm

    def test_reviewer_gets_only_read_only_tools(self):
        agent, llm = self._run()
        agent.run("改完审一下")

        self.assertTrue(llm.reviewer_tools, "审查者根本没被调用")
        for forbidden in ("write_file", "edit_file", "run_bash", "git_commit", "delegate", "review_changes"):
            self.assertNotIn(forbidden, llm.reviewer_tools, f"审查者拿到了 {forbidden}")

    def test_reviewer_sees_requirement_and_overview(self):
        agent, llm = self._run(requirement="把减法改成加法")
        agent.run("改完审一下")

        self.assertIn("把减法改成加法", llm.reviewer_task)
        self.assertIn("app.py", llm.reviewer_task)
        self.assertIn("helper.py", llm.reviewer_task, "未跟踪的新文件没进审查范围")

    def test_verdict_comes_back_to_parent(self):
        agent, _ = self._run()
        answer = agent.run("改完审一下")
        self.assertEqual(answer, "审查完成。")
        tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
        self.assertIn("有问题需修", tool_msgs[0]["content"])
        self.assertIn("审查范围", tool_msgs[0]["content"])

    def test_parent_context_stays_small(self):
        """审查者翻的文件不该回流到主上下文。"""
        agent, _ = self._run()
        agent.run("改完审一下")
        flat = json.dumps(agent.messages, ensure_ascii=False)
        self.assertNotIn("def add(a, b)", flat)


if __name__ == "__main__":
    unittest.main(verbosity=2)
