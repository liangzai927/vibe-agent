"""挣来的自主权（生产工作流·阶段 E4+E5）。

用户要的：「刚开始需要我人工审核，连续一周任务完成度达到 95% 满意度
（几乎不需要我干预方向、纠正问题）就可以自动推进。」

**这是「挣来的」而不是「配置出来的」**：不是设个开关就完事，
而是要先证明它值得信任。所以要有度量。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402
from va import autonomy  # noqa: E402
from va.autonomy import AutonomyLog, Readiness, evaluate  # noqa: E402
from va.task import Task, TaskStore  # noqa: E402


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


def workflow(**autonomy_config) -> FakeWorkflow:
    return FakeWorkflow({"autonomy": {"min_samples": 5, **autonomy_config}})


def task_with(*interventions: str, trigger: str = "human") -> Task:
    task = Task(id="T")
    for kind in interventions:
        task.record("branch", trigger=trigger, intervention=kind)
    return task


class TestReadiness(AgentTestCase):
    def test_no_records_cannot_be_judged(self):
        readiness = evaluate([], self.root, workflow())
        self.assertFalse(readiness.ready)
        self.assertIn("还没法判断", readiness.describe())

    def test_all_untouched_is_ready(self):
        readiness = evaluate([task_with(*["none"] * 10)], self.root, workflow())
        self.assertTrue(readiness.ready, readiness.reasons)
        self.assertIn("可以开自动推进", readiness.describe())

    def test_an_intervention_lowers_the_rate(self):
        readiness = evaluate(
            [task_with(*["none"] * 8, "edited", "rerun")], self.root, workflow()
        )
        self.assertFalse(readiness.ready)
        self.assertIn("无干预率没到门槛", readiness.reasons)

    def test_too_few_samples_is_not_ready(self):
        """**2 次全对不代表什么。** 样本太少就不该下结论。"""
        readiness = evaluate([task_with(*["none"] * 2)], self.root, workflow())
        self.assertFalse(readiness.ready)
        self.assertIn("样本太少", " ".join(readiness.reasons))

    def test_auto_transitions_are_excluded(self):
        """**自动推进本来就没人干预**，算进分母会把比例稀释成
        「看起来很好」——那个数字没有意义。"""
        task = Task(id="T")
        task.record("branch", trigger="human", intervention="edited")
        for _ in range(20):
            task.record("plan", trigger="auto")
        readiness = evaluate([task], self.root, workflow())
        self.assertEqual(readiness.transitions, 1)
        self.assertEqual(readiness.rate, 0.0)

    def test_threshold_is_configurable(self):
        tasks = [task_with(*["none"] * 8, "edited")]
        self.assertFalse(evaluate(tasks, self.root, workflow(threshold=0.95)).ready)
        self.assertTrue(evaluate(tasks, self.root, workflow(threshold=0.8)).ready)

    def test_window_excludes_old_records(self):
        task = task_with(*["edited"] * 10)
        for transition in task.transitions:
            transition.at = time.time() - 30 * 86400
        task.record("plan")   # 只有这条在窗口内
        readiness = evaluate([task], self.root, workflow(window_days=7))
        self.assertEqual(readiness.transitions, 1)


class TestSevereIncidents(AgentTestCase):
    """**光有比例不够。**

    极端例子：40 次推进里 38 次无干预，但其中一次是提交了没测过的代码
    并推到了 test 分支。比例 95%，实际不可接受。
    """

    def test_an_incident_blocks_even_at_a_perfect_rate(self):
        tasks = [task_with(*["none"] * 20)]
        AutonomyLog(self.root).record("gate_override", detail="纯文档改动")
        readiness = evaluate(tasks, self.root, workflow())
        self.assertEqual(readiness.rate, 1.0)
        self.assertFalse(readiness.ready, "比例满分却因为事故放行了")
        self.assertIn("严重事故", " ".join(readiness.reasons))

    def test_old_incidents_fall_out_of_the_window(self):
        tasks = [task_with(*["none"] * 20)]
        log = AutonomyLog(self.root)
        incident = log.record("gate_override")
        items = log.incidents()
        items[0].at = time.time() - 30 * 86400
        import json

        log.path.write_text(
            json.dumps({"incidents": [autonomy.asdict(i) for i in items]}), encoding="utf-8"
        )
        self.assertTrue(evaluate(tasks, self.root, workflow()).ready)

    def test_incidents_persist_in_the_project(self):
        AutonomyLog(self.root).record("wrong_branch", detail="推到了 main")
        self.assertTrue((self.root / ".vibe" / "autonomy.json").is_file())
        self.assertEqual(AutonomyLog(self.root).incidents()[0].kind, "wrong_branch")

    def test_bad_json_is_not_fatal(self):
        path = self.root / ".vibe" / "autonomy.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ 不是 json", encoding="utf-8")
        self.assertEqual(AutonomyLog(self.root).incidents(), [])

    def test_format_problems_are_not_severe(self):
        """格式问题算「干预」不算「事故」——它说明约定没写清楚，不是危险动作。"""
        self.assertIn("commit_format", autonomy.NOT_SEVERE)
        self.assertNotIn("commit_format", autonomy.SEVERE_KINDS)


class TestAdvanceApproval(AgentTestCase):
    """E4：自动推进开关 + 三处硬停。"""

    def setUp(self) -> None:
        super().setUp()
        from va.loop import Agent
        from va.ui import UI

        self.agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)
        self.tool = self.agent.registry.get("task_advance")

    def test_manual_mode_asks_every_time(self):
        for state in ("branch", "implement", "commit", "submit"):
            self.assertTrue(self.tool.needs_approval({"to": state}), state)

    def test_auto_mode_only_asks_for_irreversible_steps(self):
        """**推错了收不回来，所以哪怕开了自动也要你点头。**"""
        self.agent.workflow.data.setdefault("autonomy", {})["mode"] = "auto"
        self.assertFalse(self.tool.needs_approval({"to": "implement"}))
        self.assertFalse(self.tool.needs_approval({"to": "test"}))
        self.assertTrue(self.tool.needs_approval({"to": "commit"}))
        self.assertTrue(self.tool.needs_approval({"to": "submit"}))

    def test_confirm_before_is_configurable(self):
        self.agent.workflow.data.setdefault("autonomy", {})["mode"] = "auto"
        self.agent.workflow.data["autonomy"]["confirm_before"] = ["submit"]
        self.assertFalse(self.tool.needs_approval({"to": "commit"}))
        self.assertTrue(self.tool.needs_approval({"to": "submit"}))

    def test_auto_mode_records_transitions_as_auto(self):
        """自动推进不该计入无干预率的分母。"""
        self.agent.workflow.data.setdefault("autonomy", {})["mode"] = "auto"
        # **按顺序推**——工作流现在强制顺序，跳步会被拒
        self.tool.run(task_id="T-1", to="branch")
        self.tool.run(task_id="T-1", to="plan")
        task = TaskStore(self.root).load("T-1")
        self.assertEqual([t.trigger for t in task.transitions], ["auto", "auto"])

    def test_manual_mode_records_as_human(self):
        self.tool.run(task_id="T-1", to="branch")
        task = TaskStore(self.root).load("T-1")
        self.assertEqual(task.transitions[0].trigger, "human")

    def test_default_mode_is_manual(self):
        from va.workflow import defaults

        self.assertEqual(defaults()["autonomy"]["mode"], "manual")

    def test_commit_and_submit_are_confirmed_by_default(self):
        from va.workflow import defaults

        self.assertEqual(defaults()["autonomy"]["confirm_before"], ["commit", "submit"])


class TestGateOverrideRecordsAnIncident(AgentTestCase):
    def test_override_leaves_an_incident(self):
        """绕过门禁提交这件事**发生过一次就不能接受**，不是「经常发生才不行」。"""
        import subprocess

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
        config.write_text("test:\n  gate: strict\n  logic: 'echo 3 passed'\n", encoding="utf-8")

        agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)
        (self.root / "b.py").write_text("y\n", encoding="utf-8")
        agent.registry.get("git_commit").run(
            message="docs: 只改了文档", override_gate="纯文档改动"
        )

        incidents = AutonomyLog(self.root).incidents()
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].kind, "gate_override")


if __name__ == "__main__":
    unittest.main(verbosity=2)
