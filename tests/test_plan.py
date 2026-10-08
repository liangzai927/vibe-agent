"""规划层的测试。

这块的价值全在「大任务里不迷路」上，所以测试重点不是数据结构本身，
而是几个容易断掉的连接点：

* 计划能不能压过上下文压缩（它建得很早，按「保留最近 N 条」必然被丢掉）
* 能不能落到会话文件、恢复会话时还回得来
* 外部传入注册表时，工具绑定的看板和 agent 持有的看板是不是同一个
* 模型给出畸形输入时，是打断工作还是容错继续
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase, ScriptedLLM, tc, turn  # noqa: E402
from va.plan import (  # noqa: E402
    BLOCKED,
    DONE,
    IN_PROGRESS,
    MAX_STEPS,
    PENDING,
    SKIPPED,
    Plan,
    PlanBoard,
    PlanStep,
    normalize_steps,
)
from va.session import SessionStore  # noqa: E402
from va.tools import build_registry  # noqa: E402
from va.tools.base import ToolError  # noqa: E402
from va.tools.plan import UpdatePlan  # noqa: E402


class TestPlanModel(unittest.TestCase):
    def _plan(self) -> Plan:
        return Plan(
            goal="加个分页",
            steps=[
                PlanStep("读现有实现", DONE),
                PlanStep("实现切片", IN_PROGRESS, note="正在改 list.ts"),
                PlanStep("补测试", PENDING),
                PlanStep("提交", PENDING),
            ],
        )

    def test_counts(self):
        plan = self._plan()
        self.assertEqual(plan.total, 4)
        self.assertEqual(plan.done_count, 1)
        self.assertEqual(len(plan.remaining), 3)

    def test_current_is_the_in_progress_one(self):
        self.assertEqual(self._plan().current.title, "实现切片")

    def test_not_finished_while_steps_remain(self):
        self.assertFalse(self._plan().is_finished)

    def test_finished_when_all_done_or_skipped(self):
        plan = Plan(goal="g", steps=[PlanStep("a", DONE), PlanStep("b", SKIPPED)])
        self.assertTrue(plan.is_finished)

    def test_empty_plan_is_not_finished(self):
        """空计划不该被当成「已完成」，否则模型会以为没事干了。"""
        self.assertFalse(Plan(goal="g", steps=[]).is_finished)

    def test_render_shows_marks_and_notes(self):
        text = self._plan().render()
        self.assertIn("加个分页", text)
        self.assertIn("☑", text)
        self.assertIn("▸", text)
        self.assertIn("☐", text)
        self.assertIn("正在改 list.ts", text)

    def test_render_marks_blocked_reason(self):
        plan = Plan(goal="g", steps=[PlanStep("部署", BLOCKED, note="没有权限")])
        self.assertIn("卡住", plan.render())
        self.assertIn("没有权限", plan.render())

    def test_compact_render_is_one_line(self):
        text = self._plan().render(compact=True)
        self.assertEqual(len(text.splitlines()), 1)
        self.assertIn("1/4", text)

    def test_round_trip_dict(self):
        plan = self._plan()
        restored = Plan.from_dict(plan.to_dict())
        self.assertEqual(restored.goal, plan.goal)
        self.assertEqual(len(restored.steps), len(plan.steps))
        self.assertEqual(restored.steps[1].note, "正在改 list.ts")
        self.assertEqual(restored.steps[1].status, IN_PROGRESS)

    def test_from_dict_survives_garbage(self):
        restored = Plan.from_dict({"goal": None, "steps": [None, 42, {"title": "ok"}]})
        self.assertEqual(len(restored.steps), 1)
        self.assertEqual(restored.steps[0].title, "ok")


class TestNormalizeSteps(unittest.TestCase):
    def test_accepts_plain_strings(self):
        steps, warnings = normalize_steps(["a", "b"])
        self.assertEqual([s.title for s in steps], ["a", "b"])
        self.assertEqual([s.status for s in steps], [PENDING, PENDING])
        self.assertEqual(warnings, [])

    def test_rejects_non_list(self):
        steps, warnings = normalize_steps("不是数组")
        self.assertEqual(steps, [])
        self.assertTrue(warnings)

    def test_skips_entries_without_title(self):
        steps, warnings = normalize_steps([{"title": ""}, {"status": "done"}, {"title": "ok"}])
        self.assertEqual([s.title for s in steps], ["ok"])
        self.assertEqual(len(warnings), 2)

    def test_unknown_status_falls_back_with_warning(self):
        steps, warnings = normalize_steps([{"title": "a", "status": "瞎写的"}])
        self.assertEqual(steps[0].status, PENDING)
        self.assertTrue(any("不认识" in w for w in warnings))

    def test_status_is_case_insensitive(self):
        steps, _ = normalize_steps([{"title": "a", "status": "DONE"}])
        self.assertEqual(steps[0].status, DONE)

    def test_deduplicates_titles(self):
        steps, warnings = normalize_steps([{"title": "跑测试"}, {"title": "跑测试"}])
        self.assertEqual(len(steps), 1)
        self.assertTrue(any("重复" in w for w in warnings))

    def test_caps_step_count(self):
        steps, warnings = normalize_steps([{"title": f"步骤{i}"} for i in range(MAX_STEPS + 10)])
        self.assertEqual(len(steps), MAX_STEPS)
        self.assertTrue(any("太多" in w for w in warnings))

    def test_warns_on_multiple_in_progress(self):
        steps, warnings = normalize_steps(
            [
                {"title": "a", "status": IN_PROGRESS},
                {"title": "b", "status": IN_PROGRESS},
            ]
        )
        self.assertEqual(len(steps), 2)
        self.assertTrue(any("一次只推进一件事" in w for w in warnings))

    def test_single_in_progress_is_fine(self):
        _, warnings = normalize_steps(
            [{"title": "a", "status": IN_PROGRESS}, {"title": "b"}]
        )
        self.assertEqual(warnings, [])


class TestPlanBoard(unittest.TestCase):
    def setUp(self):
        self.board = PlanBoard()

    def test_first_update_creates_plan(self):
        plan, warnings = self.board.update("目标", [{"title": "a"}])
        self.assertEqual(plan.goal, "目标")
        self.assertEqual(plan.revision, 1)
        self.assertEqual(warnings, [])

    def test_revision_increments(self):
        self.board.update("目标", [{"title": "a"}])
        plan, _ = self.board.update("目标", [{"title": "a"}, {"title": "b"}])
        self.assertEqual(plan.revision, 2)

    def test_goal_can_be_omitted_after_first_update(self):
        self.board.update("原始目标", [{"title": "a"}])
        plan, _ = self.board.update("", [{"title": "a", "status": DONE}])
        self.assertEqual(plan.goal, "原始目标", "省略 goal 时应沿用原目标")

    def test_goal_can_be_corrected(self):
        self.board.update("写错了", [{"title": "a"}])
        plan, _ = self.board.update("改对了", [{"title": "a"}])
        self.assertEqual(plan.goal, "改对了")

    def test_first_update_requires_a_goal(self):
        _, warnings = self.board.update("", [{"title": "a"}])
        self.assertTrue(any("goal" in w for w in warnings))

    def test_full_replacement_not_merge(self):
        """整体替换是刻意的：增量合并会让模型越改越乱。"""
        self.board.update("g", [{"title": "a"}, {"title": "b"}])
        plan, _ = self.board.update("g", [{"title": "b"}])
        self.assertEqual([s.title for s in plan.steps], ["b"])

    def test_warns_when_steps_go_empty(self):
        self.board.update("g", [{"title": "a"}])
        _, warnings = self.board.update("g", [])
        self.assertTrue(any("清空" in w for w in warnings))

    def test_clear(self):
        self.board.update("g", [{"title": "a"}])
        self.board.clear()
        self.assertIsNone(self.board.plan)
        self.assertIn("没有计划", self.board.render())

    def test_reminder_is_empty_without_plan(self):
        self.assertEqual(self.board.reminder(), "")

    def test_reminder_contains_goal_and_progress(self):
        self.board.update("加个分页", [{"title": "a", "status": DONE}, {"title": "b"}])
        reminder = self.board.reminder()
        self.assertIn("加个分页", reminder)
        self.assertIn("1/2", reminder)

    def test_reminder_flags_missing_in_progress(self):
        self.board.update("g", [{"title": "a"}, {"title": "b"}])
        self.assertIn("没有标记为 in_progress", self.board.reminder())

    def test_reminder_when_finished(self):
        self.board.update("g", [{"title": "a", "status": DONE}])
        self.assertIn("已完成", self.board.reminder())


class TestUpdatePlanTool(unittest.TestCase):
    def setUp(self):
        self.board = PlanBoard()
        self.tool = UpdatePlan(self.board)

    def test_runs_and_returns_rendered_plan(self):
        out = self.tool.run(goal="加个分页", steps=[{"title": "读代码", "status": "done"}])
        self.assertIn("加个分页", out)
        self.assertIn("☑", out)
        self.assertIsNotNone(self.board.plan)

    def test_no_approval_needed(self):
        self.assertFalse(self.tool.requires_approval)
        self.assertEqual(self.tool.affected_paths({}), [])

    def test_none_steps_raises_tool_error(self):
        with self.assertRaises(ToolError):
            self.tool.run(steps=None)

    def test_malformed_steps_do_not_raise(self):
        """计划是辅助手段，格式不对不该打断正经工作。"""
        for bad in ("不是数组", [{"title": ""}], [None], [{"title": "a", "status": "?"}]):
            out = self.tool.run(steps=bad)
            self.assertIsInstance(out, str)
            self.assertTrue(out)

    def test_warnings_surface_in_result(self):
        out = self.tool.run(goal="g", steps=[{"title": "a", "status": "瞎写"}])
        self.assertIn("需要修正", out)

    def test_confirms_when_plan_finished(self):
        out = self.tool.run(goal="g", steps=[{"title": "a", "status": "done"}])
        self.assertIn("全部完成", out)

    def test_after_run_renders_the_plan(self):
        class FakeUI:
            def __init__(self):
                self.shown = None

            def show_plan(self, plan):
                self.shown = plan

        ui = FakeUI()
        self.tool.run(goal="g", steps=[{"title": "a"}])
        self.tool.after_run(ui, "irrelevant")
        self.assertIsNotNone(ui.shown)
        self.assertEqual(ui.shown.goal, "g")


class TestPlanWiring(AgentTestCase):
    """接线测试：这类断线用肉眼很难发现。"""

    def test_registry_and_agent_share_one_board(self):
        """build_registry 在没收到 board 时会自己造一个。

        如果 agent 换用自己的 board 却不重绑工具，计划就会写进看不见的地方。
        """
        external_registry = build_registry(self.workspace)  # 没传 board
        llm = ScriptedLLM(turn("ok"))
        agent, _ = self.make_agent(llm)
        agent.registry = external_registry
        agent.registry.register(UpdatePlan(agent.plan_board))

        agent.registry.get("update_plan").run(steps=[{"title": "a"}])
        self.assertIsNotNone(agent.plan, "计划没有写进 agent 自己的看板")

    def test_agent_created_registry_has_plan_tool(self):
        llm = ScriptedLLM(turn("ok"))
        agent, _ = self.make_agent(llm)
        self.assertIsNotNone(agent.registry.get("update_plan"))
        self.assertIsNone(agent.plan)

    def test_plan_tool_is_listed_first(self):
        """模型最容易忘的就是它，放第一个增加被看到的概率。"""
        llm = ScriptedLLM(turn("ok"))
        agent, _ = self.make_agent(llm)
        self.assertEqual(agent.registry.names()[0], "update_plan")

    def test_plan_updates_through_a_real_run(self):
        llm = ScriptedLLM(
            turn("", tc("update_plan", goal="修 bug", steps=[{"title": "复现"}, {"title": "修复"}])),
            turn("", tc("update_plan", goal="修 bug", steps=[
                {"title": "复现", "status": "done"},
                {"title": "修复", "status": "in_progress"},
            ])),
            turn("做完了。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("修个 bug")

        self.assertIsNotNone(agent.plan)
        self.assertEqual(agent.plan.done_count, 1)
        self.assertEqual(agent.plan.current.title, "修复")


class TestPlanSurvivesCompaction(AgentTestCase):
    def test_plan_is_reinjected_after_compaction(self):
        """计划建得很早，按「保留最近 N 条」必然被丢掉。

        丢掉之后模型会忘了自己干到哪，开始重复劳动。
        """
        self.cfg.context_budget = 100
        filler = "x" * 400
        llm = ScriptedLLM(
            turn("", tc("update_plan", goal="刷屏任务", steps=[
                {"title": "第一步", "status": "done"},
                {"title": "第二步", "status": "in_progress"},
                {"title": "第三步"},
            ])),
            *[turn("", tc("run_bash", command=f"echo {filler}")) for _ in range(12)],
            turn("完成。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("跑一堆命令")

        self.assertGreater(agent.compactions, 0, "测试前提没成立：根本没触发压缩")

        joined = "\n".join(
            str(m.get("content", "")) for m in agent.messages
        )
        self.assertIn("刷屏任务", joined, "压缩后计划丢了")
        self.assertIn("第二步", joined)

    def test_plan_goes_into_system_prompt_on_resume(self):
        llm = ScriptedLLM(turn("ok"))
        board = PlanBoard()
        board.update("恢复的任务", [{"title": "继续写"}])
        agent, _ = self.make_agent(llm, plan=board.plan)

        self.assertIn("恢复的任务", agent.system_prompt)


class TestPlanPersistence(AgentTestCase):
    def test_plan_is_written_to_session(self):
        store = SessionStore.create(self.root, "fake")
        llm = ScriptedLLM(
            turn("", tc("update_plan", goal="持久化测试", steps=[{"title": "a", "status": "done"}])),
            turn("好了。"),
        )
        agent, _ = self.make_agent(llm, session=store)
        agent.run("做点事")

        _, _, saved = SessionStore.open(self.root, store.meta.id).load()
        self.assertIsNotNone(saved, "计划没落盘")
        self.assertEqual(saved["goal"], "持久化测试")

    def test_plan_survives_a_resume(self):
        store = SessionStore.create(self.root, "fake")
        llm = ScriptedLLM(
            turn("", tc("update_plan", goal="跨进程的任务", steps=[
                {"title": "第一步", "status": "done"},
                {"title": "第二步", "status": "in_progress"},
            ])),
            turn("先做这么多。"),
        )
        agent, _ = self.make_agent(llm, session=store)
        agent.run("开工")

        # 用另一个 store 实例模拟新进程
        reopened = SessionStore.open(self.root, store.meta.id)
        _, history, saved = reopened.load()

        resumed = ScriptedLLM(turn("继续。"))
        # 走真实路径：CLI 也是把恢复出来的计划当构造参数传进去的，
        # 因为系统提示词是在 __init__ 里生成的，事后再设就晚了
        agent2, _ = self.make_agent(
            resumed,
            session=reopened,
            history=history,
            plan=Plan.from_dict(saved) if saved else None,
        )

        self.assertIsNotNone(agent2.plan)
        self.assertEqual(agent2.plan.goal, "跨进程的任务")
        self.assertEqual(agent2.plan.done_count, 1)
        self.assertEqual(agent2.plan.current.title, "第二步")
        # 恢复后系统提示里应带上计划，第一轮就知道自己站在哪
        self.assertIn("跨进程的任务", agent2.system_prompt)

    def test_plan_set_after_construction_does_not_retrofit_prompt(self):
        """记录一个刻意的顺序约束，避免以后有人把计划改成事后注入还指望它生效。"""
        llm = ScriptedLLM(turn("ok"))
        agent, _ = self.make_agent(llm)
        board = PlanBoard()
        board.update("事后加的计划", [{"title": "a"}])
        agent.plan_board = board
        self.assertNotIn("事后加的计划", agent.system_prompt)

    def test_cleared_plan_is_recorded_as_none(self):
        store = SessionStore.create(self.root, "fake")
        store.append_plan({"goal": "先有", "steps": [{"title": "a", "status": "pending"}]})
        store.append_plan(None)

        _, _, saved = SessionStore.open(self.root, store.meta.id).load()
        self.assertIsNone(saved, "显式清空计划后不该又冒出来")

    def test_no_session_means_no_plan_writes(self):
        llm = ScriptedLLM(
            turn("", tc("update_plan", goal="g", steps=[{"title": "a"}])),
            turn("ok"),
        )
        agent, _ = self.make_agent(llm)  # 不传 session
        agent.run("做事")
        self.assertIsNotNone(agent.plan)
        self.assertFalse((self.root / ".va").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
