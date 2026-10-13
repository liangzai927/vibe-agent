"""自进化：检测「哪里不对」+ 验证「改对了没有」（生产工作流·阶段 I）。

**它不能知道什么是对的。** 工作流定义里写的是团队约定，约定对不对
只有你们知道。所以这里只做一件事：

    **发现「有东西在反复出问题」，并指出它在哪。**

「正确的是什么」来自你的反馈、失败信号、统计——agent 负责把你的判断
固化成配置，不是自己发明流程。
"""

from __future__ import annotations

import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402
from va.autonomy import AutonomyLog  # noqa: E402
from va.diagnose import change_effects, diagnose  # noqa: E402
from va.task import Task, TaskStore  # noqa: E402
from va.workflow import load  # noqa: E402
from va.workflow_edit import Change, apply_changes  # noqa: E402


def task_with(records: list[tuple[str, str]], *, days_ago: float = 0.0) -> Task:
    task = Task(id="T")
    at = time.time() - days_ago * 86400
    for state, intervention in records:
        task.record(state, trigger="human", intervention=intervention)
        task.transitions[-1].at = at
    return task


class TestHotspots(AgentTestCase):
    """**最可靠的一类信号**：统计出来的，不靠猜。"""

    def test_a_frequently_intervened_step_is_flagged(self):
        # **同状态内混合**才谈得上比例——热点是按状态统计的
        task = task_with([("integrate", "redirected")] * 8 + [("integrate", "none")] * 2)
        result = diagnose(self.root, tasks=[task])
        hotspots = [s for s in result.signals if s.kind == "hotspot"]
        self.assertEqual(len(hotspots), 1)
        self.assertIn("联调", hotspots[0].what)
        self.assertIn("8/10", hotspots[0].what)
        self.assertIn("80%", hotspots[0].what)

    def test_a_low_rate_is_not_flagged(self):
        task = task_with([("integrate", "none")] * 9 + [("integrate", "edited")])
        self.assertFalse(
            [s for s in diagnose(self.root, tasks=[task]).signals if s.kind == "hotspot"]
        )

    def test_too_few_samples_is_not_flagged(self):
        """**两次里错一次不代表什么。** 样本太少就不该下结论。"""
        task = task_with([("integrate", "edited")] * 2)
        self.assertFalse(
            [s for s in diagnose(self.root, tasks=[task]).signals if s.kind == "hotspot"]
        )

    def test_auto_transitions_are_ignored(self):
        task = Task(id="T")
        for _ in range(10):
            task.record("integrate", trigger="auto", intervention="redirected")
        self.assertFalse(
            [s for s in diagnose(self.root, tasks=[task]).signals if s.kind == "hotspot"]
        )


class TestIncidents(AgentTestCase):
    def test_a_serious_incident_is_flagged_with_context(self):
        AutonomyLog(self.root).record("gate_override", detail="样式测试跑不起来")
        result = diagnose(self.root)
        incidents = [s for s in result.signals if s.kind == "incident"]
        self.assertEqual(len(incidents), 1)
        self.assertIn("绕过测试门禁", incidents[0].what)
        # **一条永远被绕过的门禁比没有门禁更糟**——它训练出无脑 override 的习惯
        self.assertIn("配错", incidents[0].suggestion)

    def test_old_incidents_fall_out_of_the_window(self):
        log = AutonomyLog(self.root)
        log.record("gate_override")
        items = log.incidents()
        items[0].at = time.time() - 60 * 86400
        import json

        log.path.write_text(json.dumps({"incidents": [vars(i) for i in items]}), encoding="utf-8")
        self.assertFalse([s for s in diagnose(self.root).signals if s.kind == "incident"])


class TestConfigVsReality(AgentTestCase):
    """**配置和现实脱节是最常见的一类问题**，而且完全可判定。"""

    def write(self, text: str) -> None:
        config = self.root / ".vibe" / "workflow.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(text, encoding="utf-8")

    def problems(self, workflow) -> list:
        return [s for s in diagnose(self.root, workflow=workflow).signals
                if s.kind == "config_reality"]

    def test_missing_mock_dir(self):
        self.write("mock:\n  strategy: fixed\n  dir: src/mocks\n")
        signals = self.problems(load(self.root))
        self.assertTrue(any("mock.dir" in s.what for s in signals))

    def test_existing_mock_dir_is_fine(self):
        (self.root / "src" / "mocks").mkdir(parents=True)
        self.write("mock:\n  strategy: fixed\n  dir: src/mocks\n")
        self.assertFalse(self.problems(load(self.root)))

    def test_convention_strategy_does_not_check_the_dir(self):
        """strategy = existing_convention 时 dir 只是个提示，不该报缺。"""
        self.write("mock:\n  strategy: existing_convention\n  dir: src/mocks\n")
        self.assertFalse(self.problems(load(self.root)))

    def test_missing_base_branch(self):
        subprocess.run(["git", "init"], cwd=str(self.root), capture_output=True)
        for key, value in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "config", key, value], cwd=str(self.root), capture_output=True)
        (self.root / "a.py").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(self.root), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(self.root), capture_output=True)

        self.write("branch:\n  base: 不存在的分支\n")
        self.assertTrue(any("不存在" in s.what for s in self.problems(load(self.root))))

    def test_base_equal_to_submit_target(self):
        """从提测分支切、又推回提测分支——中间没有隔离。"""
        self.write("branch:\n  base: test\nsubmit:\n  target_branch: test\n")
        self.assertTrue(any("submit.target_branch" in s.what for s in self.problems(load(self.root))))

    def test_test_script_not_in_package_json(self):
        (self.root / "package.json").write_text(
            '{"scripts": {"test:unit": "vitest"}}', encoding="utf-8"
        )
        self.write("test:\n  logic: npm run test:e2e\n")
        signals = self.problems(load(self.root))
        self.assertTrue(any("test:e2e" in s.what for s in signals))

    def test_existing_script_is_fine(self):
        (self.root / "package.json").write_text(
            '{"scripts": {"test:unit": "vitest"}}', encoding="utf-8"
        )
        self.write("test:\n  logic: npm run test:unit\n")
        self.assertFalse(self.problems(load(self.root)))


class TestReport(unittest.TestCase):
    def test_a_clean_workspace_says_so(self):
        import tempfile

        result = diagnose(tempfile.mkdtemp())
        self.assertTrue(result.clean)
        self.assertIn("没发现明显问题", result.report())

    def test_the_report_says_it_is_not_a_prescription(self):
        """**故意不给「该怎么改」**——那要结合用户的意图。"""
        import tempfile

        task = task_with([("integrate", "redirected")] * 5)
        report = diagnose(tempfile.mkdtemp(), tasks=[task]).report()
        self.assertIn("哪里不对", report)
        self.assertIn("不是「该怎么改」", report)


class TestChangeEffects(AgentTestCase):
    """**闭环的关键一步。** 没有它，「自我进化」就只是「不断改配置」。"""

    def setUp(self) -> None:
        super().setUp()
        subprocess.run(["git", "init"], cwd=str(self.root), capture_output=True)
        # 配置改动的历史现在记在数据根——测试要能读到它
        (self.root / ".vibe").mkdir(exist_ok=True)

    def make(self, *, before: int, after: int, before_touched: int, after_touched: int):
        now = time.time()
        apply_changes(self.root, [Change("test.gate", "warn", reason="跑不起来")])

        task = Task(id="T")
        for index in range(before):
            task.record("integrate", trigger="human",
                        intervention="edited" if index < before_touched else "none")
            task.transitions[-1].at = now - 3 * 86400
        for index in range(after):
            task.record("integrate", trigger="human",
                        intervention="edited" if index < after_touched else "none")
            task.transitions[-1].at = now + 3 * 86400
        return task

    def test_improvement_is_recognised(self):
        task = self.make(before=6, after=6, before_touched=6, after_touched=0)
        effects = change_effects(self.root, [task])
        self.assertEqual(len(effects), 1)
        self.assertTrue(effects[0].comparable)
        self.assertIn("变好了", effects[0].describe())

    def test_regression_is_recognised(self):
        task = self.make(before=6, after=6, before_touched=0, after_touched=6)
        self.assertIn("变差了", change_effects(self.root, [task])[0].describe())

    def test_too_few_samples_says_it_cannot_tell(self):
        """**样本不够就直说看不出效果**，不硬给一个结论——
        拿两三次推进去判断一次配置改动好不好，那是自欺欺人。"""
        task = self.make(before=1, after=1, before_touched=1, after_touched=0)
        effects = change_effects(self.root, [task])
        self.assertFalse(effects[0].comparable)
        self.assertIn("看不出效果", effects[0].describe())

    def test_no_history_means_no_effects(self):
        self.assertEqual(change_effects(self.root, []), [])

    def test_a_regression_shows_up_in_the_diagnosis(self):
        task = self.make(before=6, after=6, before_touched=0, after_touched=6)
        result = diagnose(self.root, tasks=[task])
        self.assertTrue([s for s in result.signals if s.kind == "ineffective_change"])


class TestTool(AgentTestCase):
    def test_registered(self):
        from va.loop import Agent
        from va.ui import UI

        agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)
        self.assertIsNotNone(agent.registry.get("diagnose_workflow"))

    def test_unknown_task(self):
        from va.loop import Agent
        from va.tools.base import ToolError
        from va.ui import UI

        agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)
        with self.assertRaises(ToolError):
            agent.registry.get("diagnose_workflow").run(task_id="NOPE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
