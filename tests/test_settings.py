"""设置相关的测试：原生选择框、读取范围、远端会话同步、配置文件隔离。

这几件事都是「一旦错了很难查」的类型：

* 选择框在无图形界面时必须给出可读的原因，而不是抛异常；
* 读取范围决定了 agent 能不能读你桌面上的设计稿，三种策略语义不能串；
* 远端同步失败时**绝不能留下一个「配了远端但记录还在本地」的状态**；
* 配置文件的位置如果不隔离，任何一次「跑一下试试」都会写到用户真实的 .env 上。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase, ScriptedLLM, turn  # noqa: E402
from va import dialogs, session_sync  # noqa: E402
from va.config import Config  # noqa: E402
from va.paths import agent_home  # noqa: E402
from va.session import SessionStore  # noqa: E402
from va.tools import build_registry  # noqa: E402
from va.tools.base import ToolError  # noqa: E402
from va.workspace import (  # noqa: E402
    READ_SCOPE_ALL,
    READ_SCOPE_LISTED,
    READ_SCOPE_WORKSPACE,
    Workspace,
    WorkspaceError,
)


# --------------------------------------------------------------- 原生选择框


class TestFolderPicker(unittest.TestCase):
    def test_available_reports_a_method_or_a_reason(self):
        ok, how = dialogs.available()
        self.assertIsInstance(ok, bool)
        self.assertTrue(how, "总得说清楚用什么方式，或者为什么不行")

    def test_cancel_is_not_an_error(self):
        """用户点取消是正常操作，不该当成失败弹错误。"""
        with mock.patch.object(
            dialogs, "_run",
            return_value=mock.Mock(returncode=0, stdout=dialogs.CANCELLED, stderr=""),
        ):
            result = dialogs.pick_folder()
        self.assertFalse(result.ok)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.describe(), "已取消")

    def test_successful_pick(self):
        with mock.patch.object(
            dialogs, "_run",
            return_value=mock.Mock(returncode=0, stdout="/Users/me/Designs\n", stderr=""),
        ):
            result = dialogs.pick_folder()
        self.assertTrue(result.ok)
        self.assertEqual(result.path, "/Users/me/Designs")

    def test_missing_tool_is_reported_not_raised(self):
        with mock.patch.object(dialogs, "_run", side_effect=FileNotFoundError("no osascript")):
            result = dialogs.pick_folder()
        self.assertFalse(result.ok)
        self.assertIn("FileNotFoundError", result.error)

    def test_timeout_is_reported(self):
        with mock.patch.object(
            dialogs, "_run", side_effect=subprocess.TimeoutExpired("x", 1)
        ):
            result = dialogs.pick_folder()
        self.assertFalse(result.ok)
        self.assertIn("太久", result.error)

    def test_nonzero_exit_is_reported(self):
        with mock.patch.object(
            dialogs, "_run",
            return_value=mock.Mock(returncode=1, stdout="", stderr="某处出错"),
        ):
            result = dialogs.pick_folder()
        self.assertFalse(result.ok)
        self.assertIn("某处出错", result.error)

    def test_empty_output_is_a_failure(self):
        with mock.patch.object(
            dialogs, "_run", return_value=mock.Mock(returncode=0, stdout="  ", stderr="")
        ):
            self.assertFalse(dialogs.pick_folder().ok)


# --------------------------------------------------------------- 读取范围


class TestReadScope(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.outside = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-scope"
        self.outside.mkdir(exist_ok=True)
        self.ext = self.outside / "设计稿.png"
        self.ext.write_bytes(b"x")

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.outside, ignore_errors=True)
        super().tearDown()

    def test_all_is_the_default(self):
        """用户明确要求：默认允许读任何位置。"""
        self.assertEqual(Workspace(self.root).read_scope, READ_SCOPE_ALL)
        self.assertEqual(Config(workspace=".", model="m", api_key="k", base_url="u").read_scope, "all")

    def test_unknown_scope_falls_back_to_all(self):
        self.assertEqual(Workspace(self.root, read_scope="乱写的").read_scope, READ_SCOPE_ALL)

    def test_all_never_asks(self):
        ws = Workspace(self.root, read_scope=READ_SCOPE_ALL)
        self.assertFalse(ws.needs_external_approval(self.ext))
        self.assertTrue(ws.external_read_allowed())

    def test_listed_asks_outside_and_not_inside_extra_roots(self):
        ws = Workspace(self.root, read_scope=READ_SCOPE_LISTED, extra_read_roots=[self.outside])
        self.assertFalse(ws.needs_external_approval(self.ext), "放行目录里的不该问")
        self.assertTrue(ws.needs_external_approval("/etc/hosts"), "没放行的要问")

    def test_workspace_forbids_rather_than_asks(self):
        """禁止就是禁止，不该弹一个点了也没用的审批框。"""
        ws = Workspace(self.root, read_scope=READ_SCOPE_WORKSPACE)
        self.assertFalse(ws.external_read_allowed())
        self.assertFalse(
            ws.needs_external_approval(self.ext),
            "不允许越界时不该弹审批——点了也没用，纯浪费用户时间",
        )

    def test_workspace_scope_still_allows_the_agent_home(self):
        """设计稿规格存在 agent 自己的目录里，那个必须一直能读。"""
        ws = Workspace(self.root, read_scope=READ_SCOPE_WORKSPACE)
        self.assertTrue(ws.readable(ws.home / "designs" / "x.md"))

    # ------------------------------------------------------ 工具层

    def test_tool_asks_under_listed(self):
        listed = Workspace(self.root, read_scope=READ_SCOPE_LISTED)
        tool = build_registry(listed, cfg=self.cfg).get("inspect_image")
        self.assertTrue(tool.needs_approval({"path": str(self.ext)}))

    def test_tool_refuses_under_workspace_scope(self):
        """用 read_document 测策略——它不依赖 Pillow，任何环境都能跑到那一步。"""
        locked = Workspace(self.root, read_scope=READ_SCOPE_WORKSPACE)
        tool = build_registry(locked, cfg=self.cfg).get("read_document")
        with self.assertRaises((ToolError, WorkspaceError)) as ctx:
            tool.run(path=str(self.ext))
        message = str(ctx.exception)
        self.assertIn("只允许读工作区内", message)
        self.assertIn("设置", message, "要告诉用户去哪改")

    def test_tool_does_not_ask_under_all(self):
        tool = build_registry(self.workspace, cfg=self.cfg).get("inspect_image")
        self.assertFalse(tool.needs_approval({"path": str(self.ext)}))

    def test_subagent_inherits_the_scope(self):
        """子 agent 用父的 workspace，策略自然跟着走——这条防止以后改坏。"""
        from va.plan import PlanBoard
        from va.tools.context import AgentContext
        from va.tools.delegate import child_registry

        listed = Workspace(self.root, read_scope=READ_SCOPE_LISTED)
        ctx = AgentContext(workspace=listed, cfg=self.cfg, plan_board=PlanBoard(), max_depth=2)
        tool = child_registry(ctx, "read", PlanBoard()).get("inspect_image")
        self.assertTrue(tool.needs_approval({"path": str(self.ext)}))


# --------------------------------------------------------------- 远端同步


@unittest.skipUnless(
    __import__("shutil").which("git"), "需要 git"
)
class TestSessionSync(AgentTestCase):
    """用**真的本地 git 仓库**测，不 mock git。

    远端同步的坑基本都在 git 的边界行为上（浅克隆、空仓库、推送被拒），
    mock 掉 git 等于把这些坑全绕过去。
    """

    def setUp(self) -> None:
        super().setUp()
        self.origin = self.root / "origin.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(self.origin)],
            check=True, capture_output=True,
        )
        self.url = str(self.origin)

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args], cwd=str(cwd) if cwd else None,
            capture_output=True, text=True,
        )

    def test_remote_dir_is_stable_and_outside_the_project(self):
        first = session_sync.remote_dir(self.url)
        self.assertEqual(first, session_sync.remote_dir(self.url), "同一个地址要落在同一处")
        self.assertNotIn(str(self.root), str(first))
        self.assertIn(str(agent_home()), str(first))

    def test_different_urls_do_not_collide(self):
        """两个仓库都叫 sessions 时要分得开。"""
        a = session_sync.remote_dir("git@x.com:team/sessions.git")
        b = session_sync.remote_dir("git@x.com:me/sessions.git")
        self.assertNotEqual(a, b)

    def test_empty_url_is_rejected(self):
        self.assertFalse(session_sync.ensure_clone("").ok)
        self.assertFalse(session_sync.sync("").ok)

    def test_empty_remote_explains_which_remote(self):
        """「没配远端」这个提示一度让人以为在说项目代码的 git 远端。

        其实这里说的是**会话记录**的远端——一个单独的仓库。
        项目代码推没推、配没配 origin，和它没有任何关系。
        提示必须把这一点说清楚，否则用户会去配错地方。
        """
        message = session_sync.sync("").describe()
        self.assertIn("会话记录", message)
        self.assertIn("两回事", message, "要点明和项目代码远端不是一回事")
        self.assertIn("设置", message, "要告诉他去哪配")

    def test_bad_url_fails_without_leaving_junk(self):
        target = session_sync.remote_dir("/definitely/not/a/repo")
        result = session_sync.ensure_clone("/definitely/not/a/repo")
        self.assertFalse(result.ok)
        self.assertFalse(target.exists(), "失败的克隆不该留下目录")

    def test_stale_cache_dir_is_cleaned_after_a_failed_clone(self):
        """目录里已经有残留时，git 自己**不会**清理——这时得我们自己来。

        （全新克隆失败时 git 会自己删干净，所以那一条测不出我们的清理逻辑；
        这一条才行。）
        """
        target = session_sync.remote_dir("/definitely/not/a/repo")
        target.mkdir(parents=True, exist_ok=True)
        (target / "stale.txt").write_text("上一次失败留下的", encoding="utf-8")

        result = session_sync.ensure_clone("/definitely/not/a/repo")
        self.assertFalse(result.ok)
        self.assertFalse(
            target.exists(),
            "残留的缓存目录必须清掉，否则下次会被误判成「已经克隆好了」",
        )

    def test_clone_then_write_then_sync(self):
        self.assertTrue(session_sync.ensure_clone(self.url).ok)

        os.environ["VA_SESSION_REMOTE"] = self.url
        self.addCleanup(os.environ.pop, "VA_SESSION_REMOTE", None)

        store = SessionStore.create(self.root, "m")
        store.append_message({"role": "user", "content": "你好"})
        store.append_end("done", 1)
        self.assertIn(str(session_sync.remote_dir(self.url)), str(store.path))

        result = session_sync.sync(self.url)
        self.assertTrue(result.ok, result.describe())

        files = self.git("ls-tree", "-r", "--name-only", "HEAD", cwd=self.origin)
        self.assertIn(store.path.name, files.stdout)

    def test_sync_with_nothing_new_is_not_a_failure(self):
        session_sync.ensure_clone(self.url)
        result = session_sync.sync(self.url)
        self.assertTrue(result.ok)
        self.assertIn("没有新记录", result.message)

    def test_a_second_machine_can_pull_the_sessions_back(self):
        """这才是远端的意义：换台机器能看到之前的对话。"""
        os.environ["VA_SESSION_REMOTE"] = self.url
        self.addCleanup(os.environ.pop, "VA_SESSION_REMOTE", None)

        session_sync.ensure_clone(self.url)
        store = SessionStore.create(self.root, "m")
        store.append_message({"role": "user", "content": "第一台机器说的话"})
        store.append_end("done", 1)
        session_sync.sync(self.url)

        # 模拟另一台机器：把克隆目录挪走，重新 clone
        clone = session_sync.remote_dir(self.url)
        import shutil as _shutil

        _shutil.rmtree(clone)
        self.assertTrue(session_sync.ensure_clone(self.url).ok)

        sessions = SessionStore.sessions(self.root)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].id, store.meta.id)
        _, messages, _ = SessionStore.open(self.root, sessions[0].id).load()
        self.assertIn("第一台机器说的话", str(messages))

    def test_push_failure_is_reported_but_data_is_kept(self):
        """推不上去不算丢数据——提交已经落本地了，这点要说清楚。"""
        session_sync.ensure_clone(self.url)
        clone = session_sync.remote_dir(self.url)
        (clone / "note.jsonl").write_text("{}\n", encoding="utf-8")

        # 把 origin 挪走，制造推送失败
        moved = self.root / "origin-moved.git"
        self.origin.rename(moved)
        try:
            result = session_sync.sync(self.url)
        finally:
            moved.rename(self.origin)

        self.assertFalse(result.ok)
        self.assertIn("没丢", result.detail)
        # 提交确实落在本地了
        log = self.git("log", "--oneline", cwd=clone)
        self.assertTrue(log.stdout.strip())


# --------------------------------------------------------------- 配置文件


class TestSessionDirFailure(AgentTestCase):
    """会话目录建不出来时，agent 必须还能干活。

    记录存不下是「少了个功能」，不是「没法干活」。为了它把整个服务弄崩，
    等于用一个小问题换掉一个大功能——用户报的正是这个：
    `va web` 直接起不来，页面上什么都点不动。
    """

    def test_create_raises_a_dedicated_error(self):
        from va.session import SessionDirError

        blocked = self.root / "blocked"
        blocked.mkdir()
        blocked.chmod(0o500)
        try:
            with self.assertRaises(SessionDirError) as ctx:
                SessionStore.create(self.root, "m", session_dir=blocked / "sub" / "deep")
            message = str(ctx.exception)
            self.assertIn("VA_SESSION_DIR", message, "要告诉用户怎么改")
        finally:
            blocked.chmod(0o700)

    def test_helper_degrades_instead_of_raising(self):
        from va.cli import _create_session
        from va.ui import UI

        blocked = self.root / "blocked2"
        blocked.mkdir()
        blocked.chmod(0o500)
        os.environ["VA_SESSION_DIR"] = str(blocked / "sub" / "deep")
        self.addCleanup(os.environ.pop, "VA_SESSION_DIR", None)
        try:
            ui = UI(self.workspace)
            with mock.patch.object(ui, "warn") as warned:
                store = _create_session(self.root, self.cfg, ui)
            self.assertIsNone(store, "应该降级成「不记录」而不是抛异常")
            self.assertTrue(warned.called, "降级必须说出来，不能悄悄吞掉")
        finally:
            blocked.chmod(0o700)


class TestEnvFileOverride(AgentTestCase):
    """VA_ENV_FILE：配置文件的位置必须能被指到别处。

    没有这个的话，任何一次「起个服务跑一下试试」都会写到用户真实的 .env 上——
    开发时已经这样污染过一次了。
    """

    def setUp(self) -> None:
        super().setUp()
        self._saved = os.environ.pop("VA_ENV_FILE", None)

    def tearDown(self) -> None:
        if self._saved is not None:
            os.environ["VA_ENV_FILE"] = self._saved
        else:
            os.environ.pop("VA_ENV_FILE", None)
        super().tearDown()

    def test_defaults_to_the_package_dir(self):
        target = Config.env_file()
        self.assertEqual(target.name, ".env")
        self.assertTrue(str(target).endswith("vibe-agent/.env"))

    def test_override_is_honoured(self):
        os.environ["VA_ENV_FILE"] = str(self.root / "custom.env")
        self.assertEqual(Config.env_file(), self.root / "custom.env")

    def test_override_expands_user(self):
        os.environ["VA_ENV_FILE"] = "~/somewhere/my.env"
        self.assertFalse(str(Config.env_file()).startswith("~"))

    def test_load_reads_the_override(self):
        custom = self.root / "custom.env"
        custom.write_text("VA_MODEL=from-custom-file\n", encoding="utf-8")
        os.environ["VA_ENV_FILE"] = str(custom)
        self.assertEqual(Config.load(self.root).model, "from-custom-file")

    def test_settings_write_to_the_override(self):
        from va.web.server import WebApp

        custom = self.root / "custom.env"
        custom.write_text("VA_API_KEY=sk-x\n", encoding="utf-8")
        os.environ["VA_ENV_FILE"] = str(custom)

        app = WebApp(self.cfg, self.workspace, llm=ScriptedLLM(turn("x")))
        self.assertEqual(app.env_path, custom)
        app.update_settings({"auto_approve": True})
        self.assertIn("VA_AUTO_APPROVE", custom.read_text(encoding="utf-8"))


# --------------------------------------------------------------- 设置接口


class TestSettingsRemoteAndScope(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.env_path = self.root / ".env"
        self.env_path.write_text("VA_API_KEY=sk-x\n", encoding="utf-8")
        from va.web.server import WebApp

        self.app = WebApp(
            self.cfg, self.workspace, llm=ScriptedLLM(turn("x")), env_path=self.env_path
        )
        self._saved = os.environ.pop("VA_SESSION_REMOTE", None)

    def tearDown(self) -> None:
        if self._saved is not None:
            os.environ["VA_SESSION_REMOTE"] = self._saved
        else:
            os.environ.pop("VA_SESSION_REMOTE", None)
        super().tearDown()

    def read_env(self) -> str:
        return self.env_path.read_text(encoding="utf-8")

    def test_settings_expose_scope_and_picker(self):
        data = self.app.settings()
        self.assertIn(data["read_scope"], (READ_SCOPE_ALL, READ_SCOPE_LISTED, READ_SCOPE_WORKSPACE))
        self.assertIn("picker", data)
        self.assertIn("available", data["picker"])

    def test_setting_scope_is_saved_and_flagged_for_restart(self):
        result = self.app.update_settings({"read_scope": READ_SCOPE_LISTED})
        self.assertTrue(result["ok"])
        self.assertIn("VA_READ_SCOPE=listed", self.read_env())
        self.assertTrue(result["restart"], "读取范围改不了热生效，必须说清楚")

    def test_unknown_scope_is_rejected(self):
        result = self.app.update_settings({"read_scope": "whatever"})
        self.assertFalse(result["ok"])
        self.assertNotIn("VA_READ_SCOPE", self.read_env())

    def test_bad_remote_is_rejected_and_not_written(self):
        """配了一个连不上的远端却写进配置，会变成最难查的那种状态。"""
        result = self.app.update_settings({"session_remote": "/definitely/not/a/repo"})
        self.assertFalse(result["ok"])
        self.assertIn("连不上", result["error"])
        self.assertNotIn("VA_SESSION_REMOTE=/definitely", self.read_env())

    def test_remote_and_local_are_mutually_exclusive(self):
        self.app.update_settings({"session_dir": str(self.root / "sess")})
        self.assertIn("VA_SESSION_DIR", self.read_env())

        origin = self.root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        result = self.app.update_settings({"session_remote": str(origin)})
        self.assertTrue(result["ok"], result.get("error"))
        env = self.read_env()
        self.assertIn(f"VA_SESSION_REMOTE={origin}", env)
        self.assertNotIn(f"VA_SESSION_DIR={self.root / 'sess'}", env, "配远端时该把本地设置清掉")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestModelConfig(AgentTestCase):
    """设置里改模型配置（API Key / Base URL / 模型 / temperature）。

    **两条硬要求**：
      · 完整密钥不能回传到浏览器——界面只需要知道「设了没有」和打码的样子
      · **留空表示不改**——否则用户只想改 Base URL，却把 key 抹掉了
    """

    def setUp(self) -> None:
        super().setUp()
        from va.web.server import WebApp

        self.cfg.api_key = "sk-old-key-1234567890"
        self.cfg.base_url = "https://old.example/v1"
        self.cfg.model = "old-model"
        self.cfg.temperature = 0.0
        self.app = WebApp(self.cfg, self.workspace)

    def test_settings_never_send_the_full_key(self):
        data = self.app.settings()
        self.assertTrue(data["api_key_set"])
        self.assertNotIn("sk-old-key-1234567890", str(data), "完整密钥漏到前端了")
        self.assertEqual(data["api_key_masked"], "sk-old…7890")

    def test_short_keys_are_not_echoed(self):
        from va.web.server import _mask_secret

        self.assertEqual(_mask_secret("sk-123"), "已设置")
        self.assertEqual(_mask_secret(""), "")

    def test_updating_the_key_takes_effect(self):
        result = self.app.update_settings({"api_key": "sk-new-key-0987654321"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.cfg.api_key, "sk-new-key-0987654321")
        self.assertEqual(self.app.settings()["api_key_masked"], "sk-new…4321")

    def test_an_empty_key_does_not_erase_it(self):
        """**留空表示不改。** 否则只想改 Base URL 的人会把 key 抹掉——
        而且他多半不会立刻发现，下次调用模型才炸。"""
        self.app.update_settings({"base_url": "https://new.example/v1"})
        self.assertEqual(self.cfg.api_key, "sk-old-key-1234567890")
        self.assertEqual(self.cfg.base_url, "https://new.example/v1")

    def test_empty_base_url_also_does_not_erase(self):
        self.app.update_settings({"base_url": "https://new.example/v1"})
        self.app.update_settings({"base_url": ""})
        self.assertEqual(self.cfg.base_url, "https://new.example/v1")

    def test_the_key_is_written_to_env(self):
        """写进 .env 下次启动才还在。"""
        env = self.root / ".env"
        self.app.env_path = env
        self.app.update_settings({"api_key": "sk-persisted-0000000000"})
        self.assertIn("VA_API_KEY=sk-persisted-0000000000", env.read_text(encoding="utf-8"))

    def test_temperature_must_be_a_number(self):
        result = self.app.update_settings({"temperature": "热"})
        self.assertFalse(result["ok"])
        self.assertIn("数字", result["error"])

    def test_temperature_accepts_a_number(self):
        self.app.update_settings({"temperature": "0.7"})
        self.assertEqual(self.cfg.temperature, 0.7)

    def test_temperature_can_be_set_to_zero(self):
        """0 是合法值——**不能因为它是 falsy 就当没填**。"""
        self.cfg.temperature = 1.5
        self.app.update_settings({"temperature": "0"})
        self.assertEqual(self.cfg.temperature, 0.0)

    def test_changed_list_reports_what_moved(self):
        result = self.app.update_settings(
            {"api_key": "sk-x-1234567890", "base_url": "https://y/v1"}
        )
        self.assertIn("api_key", result["changed"])
        self.assertIn("base_url", result["changed"])

    def test_nothing_sent_changes_nothing(self):
        result = self.app.update_settings({})
        self.assertEqual(self.cfg.api_key, "sk-old-key-1234567890")
        self.assertNotIn("api_key", result.get("changed", []))
