"""会话切换（多会话第二步）。

**为什么要保留多个运行时而不是切换时重建**：撤销栈只在内存里
（``UndoStack._stack``），重建就等于把 ``/undo`` 历史扔了——
而撤销是改代码时最需要的东西之一。

**为什么同一时刻只跑一个任务**：两个 agent 同时改一个项目，
出问题的方式比单线程多得多，而且很难查。
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402
from va.session import SessionStore  # noqa: E402
from va.web.bridge import ApprovalGate, EventBus, WebUI  # noqa: E402
from va.web.server import WebApp  # noqa: E402
from va.workspace import Workspace  # noqa: E402


class SwitchTestCase(AgentTestCase):
    def make(self, folder: str, title: str, message: str) -> SessionStore:
        workspace = self.root / folder
        workspace.mkdir(parents=True, exist_ok=True)
        store = SessionStore.create(str(workspace), "m")
        store.set_title(title)
        store.append_message({"role": "user", "content": message})
        store.append_end("done", 1)
        return store

    def app_with(self, store: SessionStore) -> WebApp:
        _, history, _ = store.load()
        return WebApp(
            self.cfg,
            Workspace(store.meta.workspace),
            session=store,
            history=history,
        )


class TestOpenSession(SwitchTestCase):
    def test_switching_loads_the_other_session(self):
        a = self.make("p1", "会话A", "A 说的")
        b = self.make("p2", "会话B", "B 说的")
        app = self.app_with(a)

        texts = lambda: [m.get("text") for m in app.history() if m["kind"] == "user"]
        self.assertEqual(texts(), ["A 说的"])

        result = app.open_session(b.meta.id)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(texts(), ["B 说的"])
        self.assertEqual(Path(app.workspace.root).name, "p2")

    def test_switching_back_reuses_the_runtime(self):
        """**这条是关键**：切回来必须是同一个运行时，
        否则撤销栈就没了。"""
        a = self.make("p1", "A", "a")
        b = self.make("p2", "B", "b")
        app = self.app_with(a)

        self.assertFalse(app.open_session(b.meta.id)["reused"], "第一次要新建")
        self.assertTrue(app.open_session(a.meta.id)["reused"], "切回来该复用")
        self.assertEqual(len(app.runtimes), 2)

    def test_undo_stack_survives_a_round_trip(self):
        """真正要保住的东西。"""
        a = self.make("p1", "A", "a")
        b = self.make("p2", "B", "b")
        app = self.app_with(a)

        marker = object()
        app.agent.undo._stack.append(marker)  # type: ignore[attr-defined]

        app.open_session(b.meta.id)
        app.open_session(a.meta.id)

        self.assertIn(marker, app.agent.undo._stack, "切回来撤销栈丢了")  # type: ignore[attr-defined]

    def test_unknown_session_is_reported(self):
        app = self.app_with(self.make("p1", "A", "a"))
        result = app.open_session("nope")
        self.assertFalse(result["ok"])
        self.assertIn("找不到", result["error"])

    def test_empty_id_is_rejected(self):
        app = self.app_with(self.make("p1", "A", "a"))
        self.assertFalse(app.open_session("")["ok"])

    def test_workspace_of_the_target_is_used(self):
        """切过去之后，写操作该落在**目标**工作区里。"""
        a = self.make("p1", "A", "a")
        b = self.make("p2", "B", "b")
        app = self.app_with(a)
        app.open_session(b.meta.id)
        self.assertIn("p2", str(app.workspace.root))


class TestEventTagging(SwitchTestCase):
    def test_events_carry_the_session_id(self):
        """多个会话共用一条事件流，前端靠 session 分流。"""
        a = self.make("p1", "A", "a")
        app = self.app_with(a)

        app.ui.info("测试")
        event = app.bus.since(app.bus.seq - 1)[0]
        self.assertEqual(event.get("session"), app.active_id)

    def test_first_session_is_tagged_too(self):
        """首个会话复用的是构造时的兜底 UI，它一开始没有标识——
        忘了补就会出现「不知道哪来的消息」。"""
        a = self.make("p1", "A", "a")
        app = self.app_with(a)
        self.assertTrue(app.active_id)
        self.assertEqual(app.ui.session_id, app.active_id)

    def test_each_runtime_tags_its_own_events(self):
        a = self.make("p1", "A", "a")
        b = self.make("p2", "B", "b")
        app = self.app_with(a)
        app.open_session(b.meta.id)

        app.bus.publish("marker")
        app.ui.info("来自B")
        events = [e for e in app.bus.since(app.bus.seq - 3) if e["type"] == "note"]
        self.assertTrue(events)
        self.assertEqual(events[-1]["session"], b.meta.id)


class TestSingleRunner(SwitchTestCase):
    def test_any_busy_finds_a_running_session(self):
        a = self.make("p1", "A", "a")
        b = self.make("p2", "B", "b")
        app = self.app_with(a)
        app.open_session(b.meta.id)

        # 手工造一个「B 在跑」的状态
        stop = threading.Event()
        app.runtimes[b.meta.id].worker = threading.Thread(
            target=stop.wait, daemon=True
        )
        app.runtimes[b.meta.id].worker.start()
        try:
            self.assertIsNotNone(app.any_busy())
            self.assertEqual(app.any_busy().id, b.meta.id)

            # 此时切到 A 再发消息，应该被挡回来并说清是谁在跑
            app.open_session(a.meta.id)
            ok, message = app.submit("试试")
            self.assertFalse(ok)
            self.assertIn(b.meta.id[:12], message, "要说清是哪个会话在跑")
        finally:
            stop.set()
            app.runtimes[b.meta.id].worker.join(timeout=2)

    def test_submit_on_the_running_session_says_so(self):
        a = self.make("p1", "A", "a")
        app = self.app_with(a)
        stop = threading.Event()
        app.runtimes[a.meta.id].worker = threading.Thread(target=stop.wait, daemon=True)
        app.runtimes[a.meta.id].worker.start()
        try:
            ok, message = app.submit("再来一条")
            self.assertFalse(ok)
            self.assertIn("还在跑", message)
        finally:
            stop.set()
            app.runtimes[a.meta.id].worker.join(timeout=2)

    def test_idle_when_nothing_runs(self):
        app = self.app_with(self.make("p1", "A", "a"))
        self.assertIsNone(app.any_busy())
        self.assertFalse(app.busy)


class TestEviction(SwitchTestCase):
    def test_old_runtimes_are_evicted(self):
        """内存里最多留几个——每个 agent 持完整消息历史。"""
        app = self.app_with(self.make("p0", "s0", "m0"))
        app._runtime_limit = 3

        ids = []
        for index in range(1, 6):
            store = self.make(f"p{index}", f"s{index}", f"m{index}")
            app.open_session(store.meta.id)
            ids.append(store.meta.id)

        self.assertLessEqual(len(app.runtimes), 3, "没有按上限清理")
        self.assertIn(app.active_id, app.runtimes, "把当前会话挤掉了")

    def test_running_sessions_are_not_evicted(self):
        """正在跑的会话不能被挤掉——那会把它下面的工作弄没。"""
        app = self.app_with(self.make("p0", "s0", "m0"))
        app._runtime_limit = 1

        stop = threading.Event()
        first = app.active_id
        app.runtimes[first].worker = threading.Thread(target=stop.wait, daemon=True)
        app.runtimes[first].worker.start()
        try:
            other = self.make("p9", "s9", "m9")
            app.open_session(other.meta.id)
            self.assertIn(first, app.runtimes, "把正在跑的会话挤掉了")
        finally:
            stop.set()
            app.runtimes[first].worker.join(timeout=2)


class TestIndexConsistency(SwitchTestCase):
    def test_sidebar_marks_the_active_session(self):
        a = self.make("p1", "A", "a")
        b = self.make("p2", "B", "b")
        app = self.app_with(a)
        app.open_session(b.meta.id)

        blocks = app.sessions_index()["blocks"]
        active = [s for block in blocks for s in block["sessions"] if s["active"]]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], b.meta.id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
