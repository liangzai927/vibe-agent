"""工作区注册表 + 会话索引 + 平台数据根。

**为什么需要注册表**：工作区可以散落在磁盘任何地方，扫不出来，只能记。
**为什么需要会话索引**：``SessionStore.sessions()`` 只列一个目录，
侧栏要跨工作区分组，得自己扫。
**为什么数据根要判平台**：Windows 上数据放系统盘会越涨越满。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402
from va import paths, projects  # noqa: E402
from va.projects import (  # noqa: E402
    GROUP_DEFAULT,
    GROUP_UNGROUPED,
    GROUP_WORKSPACE,
    Registry,
    WorkspaceEntry,
    group_sessions,
    index_sessions,
    relative_time,
)


class TestPlatformDataRoot(unittest.TestCase):
    """数据根的平台判定。"""

    def setUp(self) -> None:
        self._saved = os.environ.pop("VA_HOME", None)

    def tearDown(self) -> None:
        if self._saved is not None:
            os.environ["VA_HOME"] = self._saved
        else:
            os.environ.pop("VA_HOME", None)

    def test_explicit_home_wins(self):
        os.environ["VA_HOME"] = "/tmp/custom-vibe"
        root, why = paths.default_data_root()
        self.assertEqual(root, Path("/tmp/custom-vibe"))
        self.assertIn("VA_HOME", why)

    def test_legacy_location_is_kept_when_it_exists(self):
        """换了默认值不能让老用户的数据「不见了」。"""
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / ".vibe-agent"
            legacy.mkdir()
            with mock.patch.object(paths, "LEGACY_DEFAULT_HOME", str(legacy)):
                root, why = paths.default_data_root()
            self.assertEqual(root, legacy)
            self.assertIn("沿用", why)

    def test_macos_linux_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope"
            with mock.patch.object(paths, "LEGACY_DEFAULT_HOME", str(missing)):
                with mock.patch.object(paths, "_is_windows", return_value=False):
                    root, _ = paths.default_data_root()
            self.assertEqual(root, missing)

    def test_windows_avoids_the_system_drive(self):
        """Windows 上要落到非系统盘——数据会一直长，放 C: 迟早撑满。"""
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope"
            os.environ["SystemDrive"] = "C:"
            try:
                with mock.patch.object(paths, "LEGACY_DEFAULT_HOME", str(missing)):
                    with mock.patch.object(paths, "_is_windows", return_value=True):
                        with mock.patch.object(
                            paths, "_windows_non_system_drives", return_value=[("D", 5 * 1024**3)]
                        ):
                            root, why = paths.default_data_root()
            finally:
                os.environ.pop("SystemDrive", None)

            self.assertEqual(str(root).replace("\\", "/").lower(), "d:/vibe-agent")
            self.assertIn("非系统盘", why)
            self.assertIn("D", why)

    def test_windows_fallback_is_not_silent(self):
        """只有一块盘时只能放系统盘——但要说出来，别让人以为装在 D 盘。"""
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope"
            with mock.patch.object(paths, "LEGACY_DEFAULT_HOME", str(missing)):
                with mock.patch.object(paths, "_is_windows", return_value=True):
                    with mock.patch.object(paths, "_windows_non_system_drives", return_value=[]):
                        root, why = paths.default_data_root()
            self.assertEqual(root, missing)
            self.assertIn("没找到非系统盘", why)
            self.assertIn("VA_HOME", why, "要告诉用户怎么改")

    def test_drive_picker_sorts_by_free_space(self):
        """盘有好几块时挑剩余空间最大的——装到快满的备份盘上很快又要搬。"""
        entry = WorkspaceEntry(path="/tmp/x")
        self.assertTrue(entry.slug)
        self.assertEqual(entry.slug, paths.project_slug("/tmp/x"))


class TestRegistry(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.registry = Registry()

    def test_add_is_idempotent(self):
        first = self.registry.add(self.root)
        second = self.registry.add(self.root)
        self.assertEqual(first.slug, second.slug)
        self.assertEqual(len(self.registry.workspaces), 1, "同一个路径加两次不该变成两条")

    def test_add_accepts_a_path_that_does_not_exist(self):
        """路径可能还没建、或者盘还没挂——不该加不进去，界面上标「找不到」即可。"""
        entry = self.registry.add("/definitely/not/here")
        self.assertEqual(entry.path, "/definitely/not/here")
        self.assertFalse(entry.exists)

    def test_remove_does_not_touch_data(self):
        """从注册表移除 ≠ 删除数据。会话和记忆都得留着。"""
        from va.session import SessionStore

        store = SessionStore.create(self.root, "m")
        store.append_message({"role": "user", "content": "你好"})
        self.registry.add(self.root)

        self.assertTrue(self.registry.remove(self.registry.all()[0].slug))
        self.assertTrue(store.path.is_file(), "数据被删了")
        self.assertEqual(len(self.registry.workspaces), 0)

    def test_remove_unknown_slug(self):
        self.assertFalse(self.registry.remove("nope"))

    def test_pinned_comes_first(self):
        a = self.registry.add("/tmp/aaa")
        self.registry.add("/tmp/bbb")
        a.pinned = True
        self.assertEqual(self.registry.all()[0].slug, a.slug)

    def test_save_and_load_roundtrip(self):
        self.registry.add(self.root)
        self.registry.set_default("/tmp/default-ws")
        self.registry.save()

        again = Registry.load()
        self.assertEqual(len(again.workspaces), 1)
        self.assertEqual(again.workspaces[0].path, str(self.root))
        self.assertEqual(again.default_workspace, "/tmp/default-ws")

    def test_corrupt_file_degrades_to_empty(self):
        """注册表坏了顶多「要重新添加工作区」——会话数据不在这个文件里。"""
        paths.projects_index_path().parent.mkdir(parents=True, exist_ok=True)
        paths.projects_index_path().write_text("{ 不是 json", encoding="utf-8")
        self.assertEqual(Registry.load().workspaces, [])

    def test_effective_default_uses_the_data_root(self):
        """没配过就用 <数据根>/workspace——一个真实的空目录，
        而不是「当前目录」这种随启动位置变的东西。"""
        self.assertEqual(
            Path(self.registry.effective_default()), paths.default_workspace_dir()
        )

    def test_find_by_path(self):
        entry = self.registry.add(self.root)
        self.assertIsNotNone(self.registry.find_path(self.root))
        self.assertEqual(self.registry.find_path(self.root).slug, entry.slug)


class TestSessionIndex(AgentTestCase):
    def make_session(self, workspace: Path, title: str, message: str):
        from va.session import SessionStore

        workspace.mkdir(parents=True, exist_ok=True)
        store = SessionStore.create(str(workspace), "m")
        if title:
            store.set_title(title)
        store.append_message({"role": "user", "content": message})
        store.append_message({"role": "assistant", "content": "好的"})
        store.append_end("done", 1)
        return store

    def test_lists_across_workspaces(self):
        self.make_session(self.root / "p1", "会话一", "第一条")
        self.make_session(self.root / "p2", "会话二", "第二条")

        entries = index_sessions()
        self.assertEqual(len(entries), 2)
        self.assertEqual({e.title for e in entries}, {"会话一", "会话二"})

    def test_title_comes_from_the_title_record(self):
        """标题是**单独一条记录**，不改首行——不读文件头就会漏掉。"""
        self.make_session(self.root / "p", "登录页排查", "看看登录")
        self.assertEqual(index_sessions()[0].title, "登录页排查")

    def test_preview_is_the_last_user_message(self):
        from va.session import SessionStore

        workspace = self.root / "p"
        workspace.mkdir()
        store = SessionStore.create(str(workspace), "m")
        store.append_message({"role": "user", "content": "老问题"})
        store.append_message({"role": "assistant", "content": "回答"})
        store.append_message({"role": "user", "content": "新问题"})
        store.append_end("done", 1)

        self.assertEqual(index_sessions()[0].preview, "新问题")

    def test_groups_by_workspace(self):
        self.make_session(self.root / "proj", "", "项目会话")
        scratch = paths.scratch_dir()
        self.make_session(scratch, "", "随手问的")

        grouped = group_sessions(index_sessions())
        self.assertEqual(len(grouped[GROUP_WORKSPACE]), 1)
        self.assertEqual(len(grouped[GROUP_UNGROUPED]), 1)

    def test_empty_groups_are_still_keys(self):
        """界面要渲染固定的三块，所以空组也得有键。"""
        grouped = group_sessions([])
        for key in (GROUP_WORKSPACE, GROUP_DEFAULT, GROUP_UNGROUPED):
            self.assertIn(key, grouped)

    def test_relative_time(self):
        now = time.time()
        self.assertEqual(relative_time(now), "刚刚")
        self.assertIn("分钟", relative_time(now - 120))
        self.assertIn("小时", relative_time(now - 7200))
        self.assertIn("天", relative_time(now - 86400 * 3))
        self.assertEqual(relative_time(0), "")

    def test_missing_projects_dir_is_empty_not_an_error(self):
        self.assertEqual(index_sessions(), [])


class TestSessionIndexApi(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        from va.web.server import WebApp

        self.app = WebApp(self.cfg, self.workspace)

    def test_workspaces_endpoint(self):
        data = self.app.workspaces()
        self.assertIn("workspaces", data)
        self.assertIn("default", data)
        self.assertEqual(data["current"], str(self.workspace.root))

    def test_session_counts_are_computed_not_stored(self):
        """会话数是现算的——存了就会和实际对不上。"""
        registry = Registry.load()
        entry = registry.add(self.root)

        data = self.app.workspaces()
        row = next(r for r in data["workspaces"] if r["slug"] == entry.slug)
        self.assertEqual(row["sessions"], 0)

        from va.session import SessionStore

        store = SessionStore.create(self.root, "m")
        store.append_message({"role": "user", "content": "x"})
        store.append_end("done", 1)

        data = self.app.workspaces()
        row = next(r for r in data["workspaces"] if r["slug"] == entry.slug)
        self.assertEqual(row["sessions"], 1)

    def test_sessions_index_groups(self):
        from va.session import SessionStore

        store = SessionStore.create(self.root, "m")
        store.append_message({"role": "user", "content": "你好"})
        store.append_end("done", 1)

        data = self.app.sessions_index()
        self.assertEqual(data["total"], 1)
        self.assertTrue(data["blocks"])
        self.assertTrue(any(b["sessions"] for b in data["blocks"]))

    def test_sessions_index_survives_a_corrupt_file(self):
        """坏掉的会话文件不该让整个侧栏空掉。"""
        paths.project_dir(self.root).mkdir(parents=True, exist_ok=True)
        sessions = paths.project_dir(self.root) / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / "broken.jsonl").write_text("不是 json\n", encoding="utf-8")

        self.assertEqual(self.app.sessions_index()["total"], 0)

    def test_session_entry_reports_active(self):
        from va.session import SessionStore

        store = SessionStore.create(self.root, "m")
        store.append_message({"role": "user", "content": "你好"})
        store.append_end("done", 1)
        self.app.agent.session = store

        blocks = self.app.sessions_index()["blocks"]
        active = [s for b in blocks for s in b["sessions"] if s["active"]]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], store.meta.id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
