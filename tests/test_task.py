"""任务状态与推进（生产工作流·阶段 E）。

**为什么需要它**：之前所有能力都是「干活的手」，但没有东西记住
「这个任务走到哪了」。`update_plan` 只是个便签，不驱动任何东西、也不跨会话。

**推进记录里为什么要记「有没有被干预」**：用户要的是「挣来的自主权」——
连续一周无干预率 ≥95% 才开自动推进。**必须从现在开始记**，
不记的话一周后算不出这个数。
"""

from __future__ import annotations

import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402
from va.task import BLOCKED, STATES, Task, TaskStore, open_task, tasks_dir  # noqa: E402
from va.tools.base import ToolError  # noqa: E402


class TestTaskObject(unittest.TestCase):
    def test_next_state_follows_the_order(self):
        task = Task(id="A")
        self.assertEqual(task.state, STATES[0])
        self.assertEqual(task.next_state(), STATES[1])

    def test_last_state_has_no_next(self):
        task = Task(id="A", state="done")
        self.assertEqual(task.next_state(), "")

    def test_progress_marks_done_current_and_remaining(self):
        task = Task(id="A", state="plan")
        text = task.progress()
        self.assertIn("✓读需求", text)
        self.assertIn("▶计划", text)
        self.assertIn("·实现", text)

    def test_transition_records_from_and_to(self):
        """**踩过的坑**：先改 state 再 record，历史会变成
        「建分支 → 建分支」这种废话——record 从 self.state 取「从哪来」。"""
        task = Task(id="A")
        task.record("branch")
        task.state = "branch"
        task.record("plan")
        self.assertEqual(task.transitions[0].frm, "intake")
        self.assertEqual(task.transitions[1].frm, "branch")

    def test_transition_measures_time_in_state(self):
        task = Task(id="A")
        task.updated_at = time.time() - 5
        transition = task.record("branch")
        self.assertGreaterEqual(transition.seconds, 4)

    def test_unknown_intervention_falls_back_to_none(self):
        task = Task(id="A")
        self.assertEqual(task.record("branch", intervention="nonsense").intervention, "none")


class TestStats(unittest.TestCase):
    """「挣来的自主权」靠这些数说话。"""

    def build(self, *interventions: str) -> Task:
        task = Task(id="A")
        for index, kind in enumerate(interventions):
            task.record(STATES[min(index + 1, len(STATES) - 1)], intervention=kind)
        return task

    def test_all_untouched_is_a_hundred_percent(self):
        stats = self.build("none", "none", "none").stats()
        self.assertEqual(stats["transitions"], 3)
        self.assertEqual(stats["rate"], 1.0)

    def test_an_intervention_lowers_the_rate(self):
        stats = self.build("none", "none", "none", "edited").stats()
        self.assertAlmostEqual(stats["rate"], 0.75)

    def test_counts_by_kind(self):
        stats = self.build("edited", "edited", "rerun").stats()
        self.assertEqual(stats["interventions"]["edited"], 2)
        self.assertEqual(stats["interventions"]["rerun"], 1)

    def test_auto_transitions_do_not_dilute_the_ratio(self):
        """**自动推进本来就没人干预**，算进去会把比例稀释成
        「看起来很好」——那个数字没有意义。"""
        task = Task(id="A")
        task.record("branch", trigger="human", intervention="edited")
        for state in STATES[2:6]:
            task.record(state, trigger="auto")
        stats = task.stats()
        self.assertEqual(stats["transitions"], 1, "自动推进被算进分母了")
        self.assertEqual(stats["rate"], 0.0)

    def test_window_excludes_older_transitions(self):
        task = self.build("edited")
        task.transitions[0].at = time.time() - 8 * 86400
        task.record("plan")
        stats = task.stats(since=time.time() - 7 * 86400)
        self.assertEqual(stats["transitions"], 1)
        self.assertEqual(stats["rate"], 1.0)


class TestStorage(AgentTestCase):
    def test_lives_in_the_data_root_not_the_project(self):
        """**任务状态是 agent 的运行状态，不是团队约定。**

        我一开始把它放在项目的 `.vibe/tasks/` 下，理由是「任务状态是项目的
        事」——那个理由把两件事混为一谈了。放项目里的实际后果：每跑一个任务
        就多一个 json，**无限累积**，用户的仓库里冒出陌生目录。

        放数据根一样能跨会话、跨重启，但不污染仓库。用户报过这个。
        """
        task, created = open_task(self.root, "ORD-1", "标题")
        self.assertTrue(created)
        TaskStore(self.root).save(task)

        self.assertFalse(
            (self.root / ".vibe" / "tasks").exists(),
            "任务又被写进项目里了",
        )
        self.assertTrue((tasks_dir(self.root) / "ORD-1.json").is_file())

    def test_round_trip_keeps_everything(self):
        store = TaskStore(self.root)
        task = Task(id="ORD-1", title="标题", branch="feature/x")
        task.record("branch", intervention="edited", note="我改的")
        store.save(task)

        loaded = store.load("ORD-1")
        self.assertEqual(loaded.title, "标题")
        self.assertEqual(loaded.branch, "feature/x")
        self.assertEqual(loaded.transitions[0].intervention, "edited")
        self.assertEqual(loaded.transitions[0].note, "我改的")

    def test_open_existing_does_not_reset(self):
        store = TaskStore(self.root)
        store.save(Task(id="ORD-1", title="原题", state="implement"))
        task, created = open_task(self.root, "ORD-1", "新题")
        self.assertFalse(created)
        self.assertEqual(task.title, "原题")
        self.assertEqual(task.state, "implement")

    def test_bad_json_is_skipped_not_fatal(self):
        directory = tasks_dir(self.root)
        directory.mkdir(parents=True)
        (directory / "broken.json").write_text("{ 不是 json", encoding="utf-8")
        store = TaskStore(self.root)
        store.save(Task(id="OK-1"))
        self.assertEqual([t.id for t in store.all()], ["OK-1"])

    def test_active_excludes_finished(self):
        store = TaskStore(self.root)
        store.save(Task(id="A", state="implement"))
        store.save(Task(id="B", state="done"))
        self.assertEqual([t.id for t in store.active()], ["A"])


class TestTools(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        from va.loop import Agent
        from va.ui import UI

        subprocess.run(["git", "init"], cwd=str(self.root), capture_output=True)
        for key, value in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "config", key, value], cwd=str(self.root), capture_output=True)
        (self.root / "a.py").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(self.root), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(self.root), capture_output=True)

        config = self.root / ".vibe" / "workflow.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("test:\n  gate: strict\n  logic: 'echo 3 passed'\n", encoding="utf-8")

        self.agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)
        self.status = self.agent.registry.get("task_status")
        self.advance = self.agent.registry.get("task_advance")
        self.block = self.agent.registry.get("task_block")

    def test_all_registered(self):
        for name in ("task_status", "task_advance", "task_block"):
            self.assertIsNotNone(self.agent.registry.get(name), f"{name} 没注册")

    def test_creates_then_advances(self):
        out = self.advance.run(task_id="ORD-1", title="标题", to="branch")
        self.assertIn("已新建", out)
        self.assertIn("读需求 → 建分支", out)

    def test_default_target_is_the_next_state(self):
        self.advance.run(task_id="ORD-1", to="branch")
        self.assertIn("建分支 → 计划", self.advance.run(task_id="ORD-1"))

    def test_unknown_state_is_rejected(self):
        with self.assertRaises(ToolError) as ctx:
            self.advance.run(task_id="ORD-1", to="nonsense")
        self.assertIn("不认识的状态", str(ctx.exception))

    def test_reaching_the_last_state_is_reported(self):
        store = TaskStore(self.root)
        store.save(Task(id="A", state="done"))
        with self.assertRaises(ToolError) as ctx:
            self.advance.run(task_id="A")
        self.assertIn("最后一步", str(ctx.exception))

    def test_gate_blocks_advancing_to_commit(self):
        """**和 git_commit 用的是同一个台账。**"""
        for state in ("branch", "plan", "implement", "integrate", "test"):
            self.advance.run(task_id="ORD-1", to=state)
        with self.assertRaises(ToolError) as ctx:
            self.advance.run(task_id="ORD-1", to="commit")
        self.assertIn("门禁", str(ctx.exception))

    def test_gate_allows_after_tests_pass(self):
        for state in ("branch", "plan", "implement", "integrate", "test"):
            self.advance.run(task_id="ORD-1", to=state)
        self.agent.registry.get("run_tests").run(kind="logic")
        self.assertIn("测试 → 提交", self.advance.run(task_id="ORD-1", to="commit"))

    def test_status_lists_when_no_id_given(self):
        self.assertIn("还没有任何任务", self.status.run())
        self.advance.run(task_id="ORD-1", title="标题", to="branch")
        self.assertIn("ORD-1", self.status.run())

    def test_status_timeline(self):
        self.advance.run(task_id="ORD-1", to="branch", intervention="edited", note="手改了")
        text = self.status.run(task_id="ORD-1", timeline=True)
        self.assertIn("读需求 → 建分支", text)
        self.assertIn("手改了产出", text)

    def test_status_unknown_task(self):
        with self.assertRaises(ToolError):
            self.status.run(task_id="NOPE")

    def test_blocking_requires_a_checkable_condition(self):
        """「等后端」会变成永远等下去。"""
        self.advance.run(task_id="ORD-1", to="branch")
        with self.assertRaises(ToolError) as ctx:
            self.block.run(task_id="ORD-1", what="订单接口")
        self.assertIn("什么条件下可以继续", str(ctx.exception))

    def test_block_and_resume(self):
        self.advance.run(task_id="ORD-1", to="branch")
        self.advance.run(task_id="ORD-1", to="plan")
        out = self.block.run(
            task_id="ORD-1", what="订单接口", check="GET /api/orders 返回 200"
        )
        self.assertIn("已挂起", out)

        task = TaskStore(self.root).load("ORD-1")
        self.assertEqual(task.state, BLOCKED)
        self.assertEqual(task.resume_to, "plan")
        self.assertEqual(task.blockers[0].check, "GET /api/orders 返回 200")

        self.advance.run(task_id="ORD-1", to="plan")
        task = TaskStore(self.root).load("ORD-1")
        self.assertEqual(task.state, "plan")
        self.assertEqual(task.blockers, [], "恢复后阻塞项该清掉")

    def test_intervention_type_is_validated(self):
        self.advance.run(task_id="ORD-1", to="branch", intervention="nonsense")
        task = TaskStore(self.root).load("ORD-1")
        self.assertEqual(task.transitions[0].intervention, "none")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestWebBoard(AgentTestCase):
    """任务看板的数据（界面里「怎么监控」）。

    **「哪一步最常被干预」是最有价值的一列**——它就是流程定义最该改的地方。
    """

    def setUp(self) -> None:
        super().setUp()
        from va.loop import Agent
        from va.task import Task
        from va.ui import UI
        from va.web.server import WebApp

        self.agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)
        self.app = WebApp(self.cfg, self.workspace)

        store = TaskStore(self.root)
        task = Task(id="ORD-9", title="测试任务", state="implement")
        task.record("branch")
        task.record("plan", intervention="edited")
        store.save(task)

    def test_reports_the_task(self):
        rows = self.app.task_report()["tasks"]
        self.assertEqual(rows[0]["id"], "ORD-9")
        self.assertEqual(rows[0]["state_label"], "实现")
        self.assertIn("▶实现", rows[0]["progress"])

    def test_carries_the_intervention_stats(self):
        """界面要显示无干预率——那是「能不能开自动」的依据。"""
        stats = self.app.task_report()["tasks"][0]["stats"]
        self.assertEqual(stats["transitions"], 2)
        self.assertEqual(stats["untouched"], 1)
        self.assertAlmostEqual(stats["rate"], 0.5)

    def test_aggregates_the_bottleneck(self):
        """跨任务汇总哪个状态最常被干预。"""
        bottleneck = self.app.task_report()["bottleneck"]
        self.assertEqual(bottleneck["plan"]["total"], 1)
        self.assertEqual(bottleneck["plan"]["touched"], 1)
        self.assertEqual(bottleneck["branch"]["touched"], 0)

    def test_labels_are_included(self):
        labels = self.app.task_report()["labels"]
        self.assertEqual(labels["intake"], "读需求")

    def test_empty_workspace_is_not_an_error(self):
        from va.web.server import WebApp

        empty = WebApp(self.cfg, self.workspace)
        (tasks_dir(self.root) / "ORD-9.json").unlink()
        self.assertEqual(empty.task_report()["tasks"], [])


class TestWorkflowIsEnforced(AgentTestCase):
    """**「按工作流跑」不能只是句口号。**

    建了一堆机制，但如果没有东西保证 agent 按顺序用它们，
    那和「提示词里写请遵守流程」没区别——还是祈祷。

    三件事让它真的跑起来：
      1. 顺序强制（跳步被拒）
      2. **每一步该做什么，推进到那一步时才告诉它**
      3. 系统提示词里点明「接到需求先开任务」
    """

    def setUp(self) -> None:
        super().setUp()
        from va.loop import Agent
        from va.ui import UI

        self.agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)
        self.advance = self.agent.registry.get("task_advance")

    def test_skipping_steps_is_refused(self):
        """不强制的话，agent 可以从「读需求」直接跳到「提交」，
        中间什么都没做，而前面所有的检查全部落空。"""
        with self.assertRaises(ToolError) as ctx:
            self.advance.run(task_id="T-1", to="commit")
        message = str(ctx.exception)
        self.assertIn("不能从", message)
        self.assertIn("建分支", message, "要说出下一个应该是哪个")

    def test_force_requires_a_reason(self):
        with self.assertRaises(ToolError) as ctx:
            self.advance.run(task_id="T-1", to="commit", force=True)
        self.assertIn("理由", str(ctx.exception))

    def test_force_with_a_reason_works_and_is_recorded(self):
        self.advance.run(
            task_id="T-1", to="commit", force=True, note="历史任务补录"
        )
        task = TaskStore(self.root).load("T-1")
        self.assertEqual(task.state, "commit")
        self.assertIn("补录", task.transitions[0].note)

    def test_normal_advance_is_allowed(self):
        self.assertIn("读需求 → 建分支", self.advance.run(task_id="T-1", to="branch"))

    def test_the_step_instruction_is_returned(self):
        """**一次性把十条指令塞进系统提示词，模型容易漏。**

        推进到那一步时再给，它就在正确的时机看到正确的那一条。
        """
        output = self.advance.run(task_id="T-1", to="branch")
        self.assertIn("这一步要做", output)
        self.assertIn("ticket", output, "没给出建分支那一步的具体要求")

    def test_the_instruction_says_what_comes_next(self):
        output = self.advance.run(task_id="T-1", to="branch")
        self.assertIn("计划", output)

    def test_instructions_are_configurable(self):
        """**这是「流程进化」的关键**：诊断发现某一步老出问题，
        改的就是这里——改的是数据，不是模型的行为。"""
        self.agent.workflow.data.setdefault("steps", {})["branch"] = "必须先跟组长确认"
        output = self.advance.run(task_id="T-1", to="branch")
        self.assertIn("必须先跟组长确认", output)
        self.assertNotIn("ticket + title", output, "内置指令该被覆盖掉")

    def test_every_state_has_an_instruction(self):
        from va.task import STEPS, STATES

        for state in STATES:
            self.assertTrue(STEPS.get(state), f"{state} 没有指令——推进到那步 agent 不知道该干什么")


class TestInstructionsReachThePrompt(AgentTestCase):
    """系统提示词里点明「接到需求先开任务」。

    **它只负责入口。** 后面每一步做什么由 task_advance 下发——
    提示词里那一段是入口，不是全部流程。
    """

    def test_no_workflow_config_means_no_section(self):
        from va.loop import Agent
        from va.ui import UI

        agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)
        self.assertNotIn("# 工作流", agent.system_prompt)

    def test_configured_project_gets_the_section(self):
        from va.loop import Agent
        from va.ui import UI

        config = self.root / ".vibe" / "workflow.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("branch:\n  base: develop\n", encoding="utf-8")

        agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)
        agent.set_workflow_mode("task")     # 工作流那一段只在任务模式下才有
        prompt = agent.system_prompt
        self.assertIn("# 工作流", prompt)
        self.assertIn("不要直接动手", prompt, "没点明「先开任务」")
        self.assertIn("task_advance", prompt)
        self.assertIn("develop", prompt, "该把关键约定摆出来")

    def test_it_says_when_submit_is_not_configured(self):
        """没配提测目标就明说——否则 agent 会以为该提测。"""
        from va.loop import Agent
        from va.ui import UI

        config = self.root / ".vibe" / "workflow.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("branch:\n  base: main\n", encoding="utf-8")

        agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)
        agent.set_workflow_mode("task")
        self.assertIn("没配提测目标", agent.system_prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestWorkflowMode(AgentTestCase):
    """对话 / 任务 模式。

    **项目配了 workflow.yaml 不代表每个问题都要建分支。**
    「这个函数干什么」和「实现这个需求」是两回事，得让用户选。
    """

    def setUp(self) -> None:
        super().setUp()
        config = self.root / ".vibe" / "workflow.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("branch:\n  base: develop\n", encoding="utf-8")

        from va.loop import Agent
        from va.ui import UI

        self.agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)

    def test_defaults_to_chat(self):
        """默认对话——**问一句「这个函数干什么」不该建分支**。"""
        self.assertEqual(self.agent.workflow_mode, "chat")
        self.assertNotIn("# 工作流", self.agent.system_prompt)

    def test_task_mode_adds_the_workflow_section(self):
        self.agent.set_workflow_mode("task")
        self.assertIn("# 工作流", self.agent.system_prompt)
        self.assertIn("不要直接动手", self.agent.system_prompt)

    def test_switching_back_removes_it(self):
        self.agent.set_workflow_mode("task")
        self.agent.set_workflow_mode("chat")
        self.assertNotIn("# 工作流", self.agent.system_prompt)

    def test_the_system_message_is_replaced_not_appended(self):
        """**要重建而不是追加**——追加的话切几次就有几份，越堆越多。"""
        for mode in ("task", "chat", "task", "chat", "task"):
            self.agent.set_workflow_mode(mode)
        systems = [m for m in self.agent.messages if m["role"] == "system"]
        self.assertEqual(len(systems), 1)

    def test_unknown_mode_falls_back_to_chat(self):
        self.agent.set_workflow_mode("nonsense")
        self.assertEqual(self.agent.workflow_mode, "chat")

    def test_setting_the_same_mode_is_a_noop(self):
        before = self.agent.system_prompt
        self.agent.set_workflow_mode("chat")
        self.assertEqual(self.agent.system_prompt, before)

    def test_no_workflow_config_means_task_mode_is_still_plain(self):
        """没配工作流的项目，任务模式下也不该凭空多出一段。"""
        import tempfile

        from va.loop import Agent
        from va.ui import UI
        from va.workspace import Workspace

        empty = Workspace(tempfile.mkdtemp())
        agent = Agent(self.cfg, UI(empty), workspace=empty)
        agent.set_workflow_mode("task")
        self.assertNotIn("# 工作流", agent.system_prompt)


class TestNothingPollutesTheProject(AgentTestCase):
    """**用户报过这个**：每跑一个任务项目里就多出 `.va`/`.vibe` 的东西。

    我一开始把任务状态放在项目的 `.vibe/tasks/` 下，理由是「任务状态是
    项目的事，不是某次对话的事」——**那个理由把两件事混为一谈了**：

      · **团队约定**（`workflow.yaml`）→ 项目里，要进 git 共享
      · **agent 的运行状态**（任务、事故、变更日志）→ 数据根
        ——一样能跨会话、跨重启，但不污染用户的仓库

    放项目里的实际后果：每跑一个任务就多一个 json，**无限累积**。
    """

    def test_a_task_does_not_touch_the_project(self):
        from va.loop import Agent
        from va.ui import UI

        before = {p.name for p in self.root.iterdir()}
        agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)
        agent.registry.get("task_advance").run(task_id="T-1", title="测试", to="branch")

        after = {p.name for p in self.root.iterdir()}
        self.assertEqual(after - before, set(), "跑任务往项目里写了东西")

    def test_an_incident_does_not_touch_the_project(self):
        from va.autonomy import AutonomyLog

        before = {p.name for p in self.root.iterdir()}
        AutonomyLog(self.root).record("gate_override", detail="演示")
        after = {p.name for p in self.root.iterdir()}
        self.assertEqual(after - before, set(), "事故日志写进项目了")

    def test_a_config_change_keeps_the_convention_in_the_project(self):
        """**约定要留在项目里**（要进 git 共享），只有运行状态搬走。"""
        from va.workflow_edit import Change, apply_changes

        apply_changes(self.root, [Change("branch.base", "develop", reason="我们从不从 main 切")])
        self.assertTrue(
            (self.root / ".vibe" / "workflow.yaml").is_file(),
            "约定文件被搬走了——它该留在项目里",
        )
        self.assertFalse((self.root / ".vibe" / "workflow-history.json").exists())
        self.assertTrue((self.root / ".vibe" / "workflow.yaml").read_text().strip())

    def test_old_leftovers_are_migrated_without_losing_data(self):
        """**只搬不删。** 用户已经产生的任务记录不能因为改了设计就丢掉。"""
        import json

        from va.paths import migrate_runtime_state

        legacy = self.root / ".vibe" / "tasks"
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "T-old.json").write_text(
            json.dumps({"id": "T-old", "state": "implement", "transitions": []}),
            encoding="utf-8",
        )
        (self.root / ".vibe" / "workflow.yaml").write_text("branch:\n  base: main\n")

        moved, note = migrate_runtime_state(self.root)
        self.assertEqual(moved, 1)
        self.assertIn("搬到了", note)

        self.assertFalse(legacy.exists(), "旧位置还留着")
        task = TaskStore(self.root).load("T-old")
        self.assertIsNotNone(task, "数据丢了")
        self.assertEqual(task.state, "implement")
        self.assertTrue(
            (self.root / ".vibe" / "workflow.yaml").is_file(),
            "约定文件被误搬了",
        )

    def test_migration_is_idempotent(self):
        from va.paths import migrate_runtime_state

        self.assertEqual(migrate_runtime_state(self.root)[0], 0)

    def test_migration_never_overwrites(self):
        """目标已存在就跳过——**宁可留着让你自己看，也不悄悄覆盖**。"""
        import json

        from va.paths import migrate_runtime_state

        legacy = self.root / ".vibe" / "tasks"
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "T-1.json").write_text('{"id":"T-1","state":"branch"}', encoding="utf-8")

        store = TaskStore(self.root)
        store.save(Task(id="T-1", state="done"))

        migrate_runtime_state(self.root)
        self.assertEqual(store.load("T-1").state, "done", "把已有的覆盖了")


class TestTaskStateIsRemindedEveryTurn(AgentTestCase):
    """**agent 不走流程的根因：只在开头被交代一次，之后再也没有提醒。**

    用户报过：执行任务时不推进步骤，永远是「读需求」，功能写完了
    也不跑测试、不提交。

    查下来是 `_system_message()` 只在初始化和切模式时调用——
    **从不在每轮刷新**。模型开头答应了一声「按工作流走」，
    聊上十几轮就飘了。

    现在 `run()` 每轮重拼一次，把「现在在哪、这一步做什么、
    做完推进到哪」重新喂给它。
    """

    def setUp(self) -> None:
        super().setUp()
        config = self.root / ".vibe" / "workflow.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("branch:\n  base: main\n", encoding="utf-8")

        from va.loop import Agent
        from va.ui import UI

        self.agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)
        self.agent.set_workflow_mode("task")

    def prompt(self) -> str:
        self.agent._refresh_system_message()
        return self.agent.system_prompt

    def test_it_says_to_open_a_task_when_there_is_none(self):
        text = self.prompt()
        self.assertIn("# 当前任务", text)
        self.assertIn("还没有任务对象", text)
        self.assertIn("task_advance", text)

    def test_it_names_the_current_step(self):
        self.agent.registry.get("task_advance").run(
            task_id="ORD-1", title="做个筛选", to="branch"
        )
        text = self.prompt()
        self.assertIn("ORD-1", text)
        self.assertIn("现在在：建分支", text)

    def test_it_says_what_to_do_at_this_step(self):
        self.agent.registry.get("task_advance").run(task_id="ORD-1", to="branch")
        text = self.prompt()
        self.assertIn("这一步要做", text)
        self.assertIn("ticket", text, "没给出建分支那一步的具体要求")

    def test_it_says_what_to_advance_to(self):
        """**不推进的话流程就停在那儿了**——所以要明说下一步推到哪。"""
        self.agent.registry.get("task_advance").run(task_id="ORD-1", to="branch")
        text = self.prompt()
        self.assertIn("做完之后调 task_advance 推进", text)
        self.assertIn("计划", text)

    def test_the_reminder_follows_the_state(self):
        """推进之后，提示词要跟着变——不然它还是照着旧的那步做。"""
        advance = self.agent.registry.get("task_advance")
        advance.run(task_id="ORD-1", to="branch")
        self.assertIn("建分支", self.prompt())

        advance.run(task_id="ORD-1", to="plan")
        text = self.prompt()
        self.assertIn("现在在：计划", text)
        self.assertNotIn("现在在：建分支", text)

    def test_chat_mode_has_no_task_reminder(self):
        """对话模式本来就不走工作流——不该塞这些。"""
        self.agent.set_workflow_mode("chat")
        self.assertNotIn("# 当前任务", self.prompt())

    def test_blocked_task_shows_what_it_waits_for(self):
        advance = self.agent.registry.get("task_advance")
        advance.run(task_id="ORD-1", to="branch")
        self.agent.registry.get("task_block").run(
            task_id="ORD-1", what="订单接口", check="GET /api/orders 返回 200"
        )
        text = self.prompt()
        self.assertIn("在等：订单接口", text)

    def test_a_broken_task_store_does_not_break_the_prompt(self):
        """拼不出任务状态就沿用旧的，**别让对话挂了**。"""
        with mock.patch("va.task.TaskStore.active", side_effect=OSError("boom")):
            self.assertEqual(self.agent._task_state_note(), "")
