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


class TestManagement(SwitchTestCase):
    """新建 / 改名 / 删除 会话，以及工作区增删。"""

    def test_new_session_lands_in_the_current_workspace(self):
        """**默认建在当前会话的工作区里**——你正在某个项目里，
        点「新会话」自然是这个项目的新会话。"""
        app = self.app_with(self.make("p1", "A", "a"))
        result = app.create_session()
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(Path(app.workspace.root).name, "p1")
        self.assertNotEqual(app.active_id, "")
        self.assertEqual(app.history(), [], "新会话不该有历史")

    def test_new_session_can_target_another_workspace(self):
        other = self.root / "p2"
        other.mkdir()
        app = self.app_with(self.make("p1", "A", "a"))
        self.assertTrue(app.create_session(str(other))["ok"])
        self.assertEqual(Path(app.workspace.root).name, "p2")

    def test_new_session_rejects_a_missing_workspace(self):
        app = self.app_with(self.make("p1", "A", "a"))
        result = app.create_session(str(self.root / "nope"))
        self.assertFalse(result["ok"])
        self.assertIn("不存在", result["error"])

    def test_rename(self):
        app = self.app_with(self.make("p1", "旧名", "a"))
        self.assertTrue(app.rename_session(app.active_id, "新名")["ok"])
        self.assertEqual(app.session_info()["title"], "新名")

    def test_rename_rejects_empty(self):
        app = self.app_with(self.make("p1", "A", "a"))
        self.assertFalse(app.rename_session(app.active_id, "   ")["ok"])

    def test_rename_truncates(self):
        app = self.app_with(self.make("p1", "A", "a"))
        app.rename_session(app.active_id, "长" * 200)
        self.assertLessEqual(len(app.session_info()["title"]), 80)

    def test_rename_unknown_session(self):
        app = self.app_with(self.make("p1", "A", "a"))
        self.assertFalse(app.rename_session("nope", "x")["ok"])

    def test_delete_removes_the_file(self):
        a = self.make("p1", "A", "a")
        path = a.path
        app = self.app_with(a)
        self.assertTrue(app.delete_session(a.meta.id)["ok"])
        self.assertFalse(path.exists(), "文件还在")

    def test_delete_switches_away_from_the_removed_session(self):
        """删的正好是当前会话时不能留下一个指向空气的 active。"""
        a = self.make("p1", "A", "a")
        b = self.make("p2", "B", "b")
        app = self.app_with(a)
        app.open_session(b.meta.id)

        self.assertTrue(app.delete_session(b.meta.id)["ok"])
        self.assertNotEqual(app.active_id, b.meta.id)
        self.assertIn(app.active_id, app.runtimes)

    def test_delete_last_session_leaves_nothing_active(self):
        a = self.make("p1", "A", "a")
        app = self.app_with(a)
        self.assertTrue(app.delete_session(a.meta.id)["ok"])
        self.assertEqual(app.active_id, "")

    def test_cannot_delete_a_running_session(self):
        """那个线程还持有它，删了会写到半个文件上。"""
        a = self.make("p1", "A", "a")
        app = self.app_with(a)
        stop = threading.Event()
        app.runtimes[a.meta.id].worker = threading.Thread(target=stop.wait, daemon=True)
        app.runtimes[a.meta.id].worker.start()
        try:
            result = app.delete_session(a.meta.id)
            self.assertFalse(result["ok"])
            self.assertIn("正在跑", result["error"])
            self.assertTrue(a.path.exists(), "文件不该被删")
        finally:
            stop.set()
            app.runtimes[a.meta.id].worker.join(timeout=2)

    def test_delete_unknown_session(self):
        app = self.app_with(self.make("p1", "A", "a"))
        self.assertFalse(app.delete_session("nope")["ok"])


class TestWorkspaceManagement(SwitchTestCase):
    def test_add(self):
        app = self.app_with(self.make("p1", "A", "a"))
        other = self.root / "another"
        other.mkdir()
        result = app.add_workspace(str(other))
        self.assertTrue(result["ok"], result.get("error"))

        from va.projects import Registry

        self.assertIsNotNone(Registry.load().find_path(other))

    def test_add_is_idempotent(self):
        app = self.app_with(self.make("p1", "A", "a"))
        other = self.root / "another"
        other.mkdir()
        app.add_workspace(str(other))
        app.add_workspace(str(other))

        from va.projects import Registry

        self.assertEqual(len(Registry.load().workspaces), 1)

    def test_add_rejects_a_non_directory(self):
        app = self.app_with(self.make("p1", "A", "a"))
        result = app.add_workspace(str(self.root / "nope"))
        self.assertFalse(result["ok"])
        self.assertIn("不是有效目录", result["error"])

    def test_add_rejects_empty(self):
        app = self.app_with(self.make("p1", "A", "a"))
        self.assertFalse(app.add_workspace("")["ok"])

    def test_remove_keeps_the_data(self):
        """从注册表移除 ≠ 删除数据。"""
        a = self.make("p1", "A", "a")
        app = self.app_with(a)
        slug = app.add_workspace(str(self.root / "p1"))["workspace"]["slug"]

        self.assertTrue(app.remove_workspace(slug)["ok"])
        self.assertTrue(a.path.exists(), "会话文件被删了")

    def test_remove_unknown(self):
        app = self.app_with(self.make("p1", "A", "a"))
        self.assertFalse(app.remove_workspace("nope")["ok"])


class TestUngroupedSessions(SwitchTestCase):
    """「不选项目直接开一个」。

    未分组会话也有一个**真实目录**（scratch），所以沙箱照常生效——
    不为它开任何特例。
    """

    def test_ungrouped_lands_in_scratch(self):
        from va.paths import scratch_dir

        app = self.app_with(self.make("p1", "A", "a"))
        result = app.create_session("ungrouped")
        self.assertTrue(result["ok"], result.get("error"))
        # 两边都 resolve：macOS 上 /var 是 /private/var 的软链
        self.assertEqual(Path(app.workspace.root).resolve(), Path(scratch_dir()).resolve())

    def test_scratch_is_created_on_demand(self):
        """第一次用的时候 scratch 可能还不存在。"""
        import shutil

        from va.paths import scratch_dir

        shutil.rmtree(scratch_dir(), ignore_errors=True)
        app = self.app_with(self.make("p1", "A", "a"))
        self.assertTrue(app.create_session("ungrouped")["ok"])
        self.assertTrue(scratch_dir().is_dir())

    def test_ungrouped_session_is_grouped_as_ungrouped(self):
        from va.projects import GROUP_UNGROUPED, group_sessions, index_sessions

        app = self.app_with(self.make("p1", "A", "a"))
        app.create_session("ungrouped")

        grouped = group_sessions(index_sessions())
        ids = [e.id for e in grouped[GROUP_UNGROUPED]]
        self.assertIn(app.active_id, ids)

    def test_creating_in_one_workspace_does_not_affect_another(self):
        """在项目乙建一条，项目甲的数量不该变。"""
        from va.projects import GROUP_WORKSPACE, group_sessions, index_sessions

        self.make("p1", "甲一", "a")
        other = self.root / "p2"
        other.mkdir()
        app = self.app_with(self.make("p1", "甲二", "b"))
        before = len(group_sessions(index_sessions())[GROUP_WORKSPACE])

        app.create_session(str(other))

        after = len(group_sessions(index_sessions())[GROUP_WORKSPACE])
        self.assertEqual(after, before + 1)


class TestDefaultWorkspace(SwitchTestCase):
    def test_default_can_be_set_and_read(self):
        from va.projects import Registry

        app = self.app_with(self.make("p1", "A", "a"))
        target = self.root / "p1"
        result = app.update_settings({"default_workspace": str(target)})
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(Path(Registry.load().effective_default()), target)

    def test_setting_default_rejects_a_non_directory(self):
        app = self.app_with(self.make("p1", "A", "a"))
        result = app.update_settings({"default_workspace": str(self.root / "nope")})
        self.assertFalse(result["ok"])
        self.assertIn("不是有效目录", result["error"])

    def test_clearing_falls_back_to_the_data_root(self):
        from va.paths import default_workspace_dir
        from va.projects import Registry

        app = self.app_with(self.make("p1", "A", "a"))
        app.update_settings({"default_workspace": str(self.root / "p1")})
        app.update_settings({"default_workspace": ""})
        self.assertEqual(
            Path(Registry.load().effective_default()), default_workspace_dir()
        )
