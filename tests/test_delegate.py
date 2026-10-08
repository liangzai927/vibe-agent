"""子 agent 委派的测试。

这块的价值是「过程很长、结论很短」和「换个干净上下文看问题」，
所以测试重点在几个容易破的保证上：

* 子 agent **不能**再往下派（否则调用链和成本都会失控）
* 子 agent 翻的文件内容**不能**回流到主上下文（否则委派就白做了）
* 子 agent 改的文件，主 agent 的 `/undo` 必须撤得掉
* 只读模式真的只给只读工具
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase, QuietUI, ScriptedLLM, tc, turn  # noqa: E402
from va.config import Config  # noqa: E402
from va.messages import AssistantTurn, ToolCall  # noqa: E402
from va.plan import PlanBoard  # noqa: E402
from va.tools import AgentContext, build_registry, filter_registry  # noqa: E402
from va.tools.base import ToolError  # noqa: E402
from va.tools.delegate import (  # noqa: E402
    READ_ONLY_TOOLS,
    WRITE_EXTRA_TOOLS,
    Delegate,
    DelegateReport,
    SubagentUI,
    child_registry,
)

# 取自 SUBAGENT_SYSTEM 的独特句子。不能用「子 agent」这种词——
# 父 agent 的工具目录里就有它（delegate 的描述里写着），会误判。
CHILD_MARK = "被主 agent 派来做一件自成一体的活"

_counter = {"n": 0}


def call(name: str, **args) -> ToolCall:
    _counter["n"] += 1
    return ToolCall(
        id=f"x{_counter['n']}", name=name, arguments=json.dumps(args, ensure_ascii=False)
    )


class TwoLevelLLM:
    """父与子各走一份剧本，按系统提示词里的标记区分。"""

    def __init__(self, parent_turns, child_turns):
        self.parent = list(parent_turns)
        self.child = list(child_turns)
        self.calls: list[tuple[str, int, set[str]]] = []

    def complete(self, messages, tools=None, on_text=None, on_reasoning=None):
        system = str(messages[0].get("content", "")) if messages else ""
        is_child = CHILD_MARK in system
        names = {t["function"]["name"] for t in (tools or [])}
        self.calls.append(("child" if is_child else "parent", len(messages), names))

        script = self.child if is_child else self.parent
        if not script:
            return AssistantTurn(content="(剧本用尽)")
        return script.pop(0)


class TestToolContract(AgentTestCase):
    def _ctx(self, **kw) -> AgentContext:
        base = dict(
            workspace=self.workspace,
            cfg=self.cfg,
            plan_board=PlanBoard(),
            max_depth=1,
        )
        base.update(kw)
        return AgentContext(**base)

    def test_no_approval_by_itself(self):
        """要不要批准取决于它实际调用的工具，所以自己不设审批。"""
        tool = Delegate(self._ctx())
        self.assertFalse(tool.requires_approval)
        self.assertEqual(tool.affected_paths({"task": "x"}), [])

    def test_registered_only_with_context(self):
        with_ctx = build_registry(self.workspace, cfg=self.cfg, ctx=self._ctx())
        without = build_registry(self.workspace, cfg=self.cfg)
        self.assertIn("delegate", with_ctx)
        self.assertNotIn("delegate", without)

    def test_not_registered_when_depth_exhausted(self):
        ctx = self._ctx(depth=1, max_depth=1)
        self.assertFalse(ctx.can_delegate())
        registry = build_registry(self.workspace, cfg=self.cfg, ctx=ctx)
        self.assertNotIn("delegate", registry)

    def test_refuses_when_depth_exhausted(self):
        """即使硬调用，也要明确拒绝而不是默默再开一层。"""
        tool = Delegate(self._ctx(depth=1, max_depth=1))
        with self.assertRaises(ToolError) as ctx:
            tool.run(task="再派一个")
        self.assertIn("上限", str(ctx.exception))

    def test_empty_task_rejected(self):
        with self.assertRaises(ToolError) as ctx:
            Delegate(self._ctx()).run(task="   ")
        self.assertIn("不能为空", str(ctx.exception))

    def test_bad_mode_rejected(self):
        with self.assertRaises(ToolError) as ctx:
            Delegate(self._ctx()).run(task="x", mode="随便")
        self.assertIn("read", str(ctx.exception))

    def test_missing_ui_is_reported(self):
        with self.assertRaises(ToolError) as ctx:
            Delegate(self._ctx(ui=None)).run(task="x")
        self.assertIn("UI", str(ctx.exception))


class TestToolFiltering(AgentTestCase):
    def _full(self):
        return build_registry(self.workspace, cfg=self.cfg, ctx=AgentContext(
            workspace=self.workspace, cfg=self.cfg, plan_board=PlanBoard(), max_depth=1
        ))

    def test_read_mode_gets_only_read_only_tools(self):
        ctx = AgentContext(workspace=self.workspace, cfg=self.cfg)
        subset = child_registry(ctx, "read", PlanBoard())
        names = set(subset.names())
        self.assertEqual(names, set(READ_ONLY_TOOLS))
        for forbidden in ("write_file", "edit_file", "run_bash", "git_commit", "git_push", "delegate"):
            self.assertNotIn(forbidden, names, f"只读模式不该给 {forbidden}")

    def test_write_mode_adds_write_tools_but_not_push_or_delegate(self):
        ctx = AgentContext(workspace=self.workspace, cfg=self.cfg)
        subset = child_registry(ctx, "write", PlanBoard())
        names = set(subset.names())
        self.assertIn("write_file", names)
        self.assertIn("run_bash", names)
        self.assertIn("git_commit", names)
        self.assertNotIn("git_push", names, "子 agent 不该能推远端")
        self.assertNotIn("git_init", names)
        self.assertNotIn("delegate", names, "不允许递归委派")
        self.assertNotIn("review_changes", names, "审查者也不该被委派者调用")
        self.assertEqual(names, set(READ_ONLY_TOOLS) | set(WRITE_EXTRA_TOOLS))

    def test_filter_registry_shares_instances(self):
        full = self._full()
        subset = filter_registry(full, only={"read_file", "grep"})
        self.assertIs(subset.get("read_file"), full.get("read_file"))

    def test_filter_by_exclude(self):
        full = self._full()
        subset = filter_registry(full, exclude={"delegate", "git_push"})
        self.assertNotIn("delegate", subset)
        self.assertNotIn("git_push", subset)
        self.assertIn("read_file", subset)


class TestSubagentUI(AgentTestCase):
    def test_forwards_confirm_to_parent(self):
        """子 agent 要改文件时，你在父 UI 里照样会被问到。"""
        parent = QuietUI(self.workspace, approve=False)
        child_ui = SubagentUI(self.workspace, parent, "子任务")

        allowed = child_ui.confirm("write_file", {"path": "a"}, "d", None)

        self.assertFalse(allowed)
        self.assertIn("write_file", parent.denied)

    def test_inherits_auto_approve(self):
        parent = QuietUI(self.workspace, approve=True)
        child_ui = SubagentUI(self.workspace, parent, "子任务")
        self.assertTrue(child_ui.auto_approve)
        self.assertTrue(child_ui.confirm("write_file", {}, "", None))

    def test_writes_are_labelled(self):
        class Recorder(QuietUI):
            def __init__(self, ws):
                super().__init__(ws, approve=True)
                self.lines = []

            def info(self, text):
                self.lines.append(text)

        parent = Recorder(self.workspace)
        child_ui = SubagentUI(self.workspace, parent, "调研")
        child_ui.assistant_header(2)
        child_ui.tool_result("结果是什么", ok=True)

        joined = "\n".join(parent.lines)
        self.assertIn("调研", joined, "输出里应带子 agent 标签")
        self.assertIn("第 2 步", joined)


class TestDelegationEndToEnd(AgentTestCase):
    def _run(self, *, mode="read", parent_turns=None, child_turns=None, **kwargs):
        (self.root / "app.py").write_text("def buggy():\n    return 1 / 0\n")
        llm = TwoLevelLLM(
            parent_turns
            or [
                turn("", call("delegate", task="找出 app.py 的问题", mode=mode, **kwargs)),
                turn("主 agent 收工。"),
            ],
            child_turns
            or [
                turn("", call("read_file", path="app.py")),
                turn("结论：app.py:2 除零错误。"),
            ],
        )
        agent, _ = self.make_agent(llm)
        answer = agent.run("查一下 app.py")
        return agent, llm, answer

    def test_child_conclusion_comes_back(self):
        agent, _, answer = self._run()
        self.assertEqual(answer, "主 agent 收工。")
        tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
        self.assertIn("除零错误", tool_msgs[0]["content"])

    def test_child_has_no_delegate_tool(self):
        _, llm, _ = self._run()
        child_calls = [c for c in llm.calls if c[0] == "child"]
        self.assertTrue(child_calls, "子 agent 根本没被调用")
        for _, _, names in child_calls:
            self.assertNotIn("delegate", names, "子 agent 拿到了 delegate，会递归")

    def test_parent_has_delegate_tool(self):
        _, llm, _ = self._run()
        parent_calls = [c for c in llm.calls if c[0] == "parent"]
        self.assertTrue(any("delegate" in names for _, _, names in parent_calls))

    def test_file_contents_do_not_leak_into_parent_context(self):
        """委派的意义就在于「过程留在子上下文」。"""
        agent, _, _ = self._run()
        flat = json.dumps(agent.messages, ensure_ascii=False)
        self.assertNotIn("def buggy", flat, "子 agent 读到的文件内容回流到了主上下文")

    def test_parent_history_stays_short(self):
        agent, _, _ = self._run()
        # 主 agent：系统 + 用户 + assistant(调 delegate) + tool + assistant = 5
        self.assertLessEqual(len(agent.messages), 6)

    def test_read_mode_blocks_writes(self):
        """只读模式下子 agent 想改文件也没工具可用。"""
        (self.root / "app.py").write_text("x = 1\n")
        llm = TwoLevelLLM(
            [turn("", call("delegate", task="改掉它", mode="read")), turn("收工。")],
            [turn("结论：我改不了，没有写文件的工具。")],
        )
        agent, _ = self.make_agent(llm)
        agent.run("改文件")
        self.assertEqual((self.root / "app.py").read_text(), "x = 1\n")

    def test_write_mode_child_change_is_undoable_by_parent(self):
        """子 agent 和父共用撤销栈，否则它改的东西 /undo 撤不掉。"""
        (self.root / "app.py").write_text("value = 1\n")
        llm = TwoLevelLLM(
            [turn("", call("delegate", task="把 1 改成 2", mode="write")), turn("收工。")],
            [
                turn("", call("edit_file", path="app.py", old_string="value = 1", new_string="value = 2")),
                turn("改完了。"),
            ],
        )
        agent, _ = self.make_agent(llm)
        agent.run("改一下")
        self.assertEqual((self.root / "app.py").read_text(), "value = 2\n")

        message = agent.undo_last()

        self.assertEqual((self.root / "app.py").read_text(), "value = 1\n", "子 agent 的改动撤不掉")
        self.assertIn("已还原", message)

    def test_child_reuses_parent_llm_client(self):
        """重建一个客户端既浪费连接，也让测试没法注入假模型。"""
        agent, llm, _ = self._run()
        self.assertIs(agent.agent_ctx.llm, llm)

    def test_max_steps_is_honoured(self):
        (self.root / "a.py").write_text("x = 1\n")
        # 子 agent 每轮都调工具，跑到被强制停下
        child = [turn("", call("read_file", path="a.py")) for _ in range(30)]
        agent, _, _ = self._run(child_turns=child, max_steps=3)
        tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
        self.assertIn("步数上限", tool_msgs[0]["content"], "子 agent 没有在步数上限处停下")
        self.assertIn("可能不完整", tool_msgs[0]["content"])

    def test_context_is_passed_through(self):
        seen = {}

        class Spy(TwoLevelLLM):
            def complete(self, messages, tools=None, on_text=None, on_reasoning=None):
                if CHILD_MARK in str(messages[0].get("content", "")):
                    seen["first_user"] = str(messages[1].get("content", ""))
                return super().complete(messages, tools, on_text, on_reasoning)

        (self.root / "app.py").write_text("x = 1\n")
        llm = Spy(
            [turn("", call("delegate", task="查一下", mode="read", context="这是登录模块")),
             turn("收工。")],
            [turn("结论：没问题。")],
        )
        agent, _ = self.make_agent(llm)
        agent.run("查")

        self.assertIn("这是登录模块", seen.get("first_user", ""))
        self.assertIn("查一下", seen.get("first_user", ""))

    def test_report_mentions_mode_and_tools(self):
        agent, _, _ = self._run()
        content = [m["content"] for m in agent.messages if m.get("role") == "tool"][0]
        self.assertIn("read 模式", content)
        self.assertIn("read_file", content)


class TestFalsySharedState(AgentTestCase):
    """`x or default` 遇上定义了 __len__ 的对象会静默换掉它。

    这个坑只有在传入的实例**恰好为空**时才暴露，平时很难发现。
    子 agent 共享父 agent 的撤销栈正是靠这条路径。
    """

    def test_empty_undo_stack_is_truthy_enough_to_keep(self):
        from va.history import UndoStack

        shared = UndoStack(self.root)
        self.assertFalse(shared, "前提变了：空 UndoStack 居然是真值，这个测试要重写")

        agent, _ = self.make_agent(ScriptedLLM(turn("ok")), undo=shared)

        self.assertIs(agent.undo, shared, "空的撤销栈被换成了新对象")

    def test_registry_is_kept_even_though_empty_registry_is_falsy(self):
        from va.tools import Registry

        empty = Registry()
        self.assertFalse(empty, "前提变了：空 Registry 居然是真值")

        # 直接走构造参数，确认不会被换成新注册表
        from va.loop import Agent

        ui = QuietUI(self.workspace, approve=True)
        agent = Agent(self.cfg, ui, workspace=self.workspace, registry=empty, llm=ScriptedLLM(turn("ok")))
        self.assertIs(agent.registry, empty)

    def test_child_gets_the_same_undo_stack_object(self):
        """端到端确认一次：不只看构造，还要看委派时传下去的是不是同一个。"""
        (self.root / "app.py").write_text("v = 1\n")
        llm = TwoLevelLLM(
            [turn("", call("delegate", task="改成 2", mode="write")), turn("收工。")],
            [
                turn("", call("edit_file", path="app.py", old_string="v = 1", new_string="v = 2")),
                turn("改完了。"),
            ],
        )
        agent, _ = self.make_agent(llm)
        self.assertEqual(len(agent.undo), 0, "起点应该是空栈——这正是容易踩雷的状态")

        agent.run("改一下")

        self.assertGreater(len(agent.undo), 0, "子 agent 的检查点没进父 agent 的栈")
        agent.undo_last()
        self.assertEqual((self.root / "app.py").read_text(), "v = 1\n")


class TestDelegateReport(unittest.TestCase):
    def test_summary_lists_tools(self):
        report = DelegateReport(ok=True, text="结论", steps=3, tools_used=["grep", "read_file"])
        text = report.summary("调研", "read")
        self.assertIn("调研", text)
        self.assertIn("3 步", text)
        self.assertIn("grep、read_file", text)
        self.assertIn("结论", text)

    def test_truncated_is_flagged(self):
        report = DelegateReport(ok=True, text="半截", steps=24, truncated=True)
        self.assertIn("可能不完整", report.summary("x", "read"))

    def test_empty_text_is_reported(self):
        report = DelegateReport(ok=False, text="   ")
        self.assertIn("没有给出结论", report.summary("x", "read"))

    def test_no_tools_used(self):
        report = DelegateReport(ok=True, text="直接回答")
        self.assertIn("没有调用工具", report.summary("x", "read"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
