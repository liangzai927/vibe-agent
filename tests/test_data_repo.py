"""记忆仓库初始化工作流的测试。

整条流程是三步（``va/data_repo.py``）：

    1. 确认远端是空白仓库——不是就中断
    2. 建结构 → 首次提交 → 推送
    3. 之后每次回复结束自动同步

第 1 步是硬性的：往一个有内容的仓库里塞提交是破坏性的，必须拦住。
第 3 步必须**在配置真的落盘之后**才可能发生——这里踩过一次坑：
初始化分支提前 return，跳过了写 .env，结果仓库建好了、配置没保存，
重启后自动同步永远不触发，而且界面看起来一切正常。
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402
from va import data_repo, session_sync  # noqa: E402


class WorkflowTestCase(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.origin = self.root / "ai-memory.git"
        self.git("init", "-q", "--bare", str(self.origin))

    def git(self, *args, cwd=None, check=False):
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, check=check,
        )

    def remote_tree(self) -> str:
        return self.git(
            "-c", "core.quotepath=false",
            "ls-tree", "-r", "--name-only", "HEAD", cwd=self.origin,
        ).stdout

    def remote_log(self) -> str:
        return self.git("log", "--oneline", cwd=self.origin).stdout

    def seed_remote(self) -> None:
        """给远端塞一个提交，让它不再是空白。"""
        work = self.root / "seed"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(work)], check=True)
        (work / "existing.txt").write_text("已有内容\n", encoding="utf-8")
        for args in (["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t",
                                     "commit", "-q", "-m", "先有的提交"]):
            subprocess.run(["git", *args], cwd=str(work), check=True,
                           capture_output=True)
        subprocess.run(["git", "push", "-q", "origin", "HEAD"], cwd=str(work),
                       check=True, capture_output=True)


class TestStepCheck(WorkflowTestCase):
    """第 1 步：远端能不能用，该初始化还是接管。

    三种情况：
      * 空白                  → 初始化
      * 非空但是我们的仓库     → 接管（把里面的会话拉过来）
      * 非空且不是我们的       → 拒绝
    """

    def test_empty_remote_means_initialise(self):
        ok, why, action = data_repo.inspect_remote(str(self.origin))
        self.assertTrue(ok, why)
        self.assertEqual(action, data_repo.ACTION_INIT)

    def test_foreign_repo_is_refused(self):
        self.seed_remote()
        ok, why, _ = data_repo.inspect_remote(str(self.origin))
        self.assertFalse(ok)
        self.assertIn("不是本 agent 的记忆仓库", why)

    def test_refusal_says_what_is_inside(self):
        """只说「不行」没用——要告诉用户里面是什么，他才能判断。"""
        self.seed_remote()
        _, why, _ = data_repo.inspect_remote(str(self.origin))
        self.assertIn("existing.txt", why, "要把仓库里已有的东西列出来")

    def test_refusal_explains_how_to_get_an_empty_one(self):
        """最常见的原因就是在 GitHub 上建仓库时勾了 README。"""
        self.seed_remote()
        _, why, _ = data_repo.inspect_remote(str(self.origin))
        self.assertIn("不要勾选", why)
        self.assertIn("README", why)

    def test_our_own_repo_is_adopted(self):
        """第二次配置同一个仓库时，应该接管而不是报错。"""
        data_repo.run(str(self.origin))

        ok, why, action = data_repo.inspect_remote(str(self.origin))
        self.assertTrue(ok, why)
        self.assertEqual(action, data_repo.ACTION_ADOPT)
        self.assertIn("记忆仓库", why)

    def test_unreachable_remote_is_reported(self):
        ok, why, _ = data_repo.inspect_remote("/definitely/not/a/repo")
        self.assertFalse(ok)
        self.assertIn("连不上", why)

    def test_empty_url(self):
        ok, why, _ = data_repo.inspect_remote("")
        self.assertFalse(ok)
        self.assertIn("没填", why)


class TestMarker(WorkflowTestCase):
    """仓库标识：判断一个非空仓库能不能接管，靠的就是它。"""

    def test_marker_is_written_on_init(self):
        data_repo.run(str(self.origin))
        clone = session_sync.remote_dir(str(self.origin), kind="data")
        self.assertIsNotNone(data_repo.read_marker(clone))

    def test_marker_reaches_the_remote(self):
        data_repo.run(str(self.origin))
        self.assertIn(data_repo.MARKER_FILE, self.remote_tree())

    def test_marker_records_the_kind(self):
        data_repo.run(str(self.origin))
        clone = session_sync.remote_dir(str(self.origin), kind="data")
        marker = data_repo.read_marker(clone)
        self.assertEqual(marker["kind"], data_repo.MARKER_KIND)
        self.assertIn("created", marker)

    def test_missing_marker_reads_as_none(self):
        self.assertIsNone(data_repo.read_marker(self.root))

    def test_corrupt_marker_reads_as_none(self):
        """文件坏了要当「不是我们的」处理，而不是崩掉。"""
        (self.root / data_repo.MARKER_FILE).write_text("{ 不是 json", encoding="utf-8")
        self.assertIsNone(data_repo.read_marker(self.root))

    def test_someone_elses_marker_reads_as_none(self):
        (self.root / data_repo.MARKER_FILE).write_text(
            '{"kind": "something-else"}', encoding="utf-8"
        )
        self.assertIsNone(data_repo.read_marker(self.root))


class TestStepInit(WorkflowTestCase):
    """第 2 步：建结构并推送。"""

    def test_creates_the_documented_structure(self):
        result = data_repo.run(str(self.origin))
        self.assertTrue(result.ok, result.describe())

        tree = self.remote_tree()
        self.assertIn("README.md", tree)

        # memory/ 和 projects/ 是空目录，git 不跟踪空目录——
        # 所以这里查本地克隆里的目录本身
        clone = session_sync.remote_dir(str(self.origin), kind="data")
        self.assertTrue((clone / "memory").is_dir(), "缺 memory/")
        self.assertTrue((clone / "projects").is_dir(), "缺 projects/")

    def test_sets_upstream_so_later_pushes_need_no_arguments(self):
        data_repo.run(str(self.origin))
        clone = session_sync.remote_dir(str(self.origin), kind="data")
        upstream = self.git("rev-parse", "--abbrev-ref", "@{u}", cwd=clone)
        self.assertEqual(upstream.returncode, 0, "没设 upstream，以后同步还得指定分支")

    def test_first_commit_is_on_a_usable_branch(self):
        data_repo.run(str(self.origin))
        branch = self.git("symbolic-ref", "--short", "HEAD", cwd=self.origin).stdout.strip()
        self.assertIn(branch, ("main", "master"))

    def test_refuses_to_touch_a_non_empty_remote(self):
        """最关键的一条：**绝不动已有内容的仓库。**"""
        self.seed_remote()
        before = self.remote_tree()

        result = data_repo.run(str(self.origin))

        self.assertFalse(result.ok)
        self.assertEqual(self.remote_tree(), before, "已有内容被改动了")
        self.assertEqual(len(self.remote_log().splitlines()), 1, "多出了提交")

    def test_failed_check_stops_before_cloning(self):
        """第 1 步没过就不该留下本地克隆——免得下次误判成「已经配好了」。"""
        self.seed_remote()
        clone = session_sync.remote_dir(str(self.origin), kind="data")
        data_repo.run(str(self.origin))
        self.assertFalse(clone.exists(), "第 1 步失败却留下了本地克隆")

    def test_empty_url_produces_a_clean_failure(self):
        result = data_repo.run("")
        self.assertFalse(result.ok)
        self.assertIn("没填", result.describe())


class TestStepsReporting(WorkflowTestCase):
    """每一步都要有名字和结果，界面才能逐步显示。"""

    def test_describe_steps_covers_the_whole_flow(self):
        keys = [s["key"] for s in data_repo.describe_steps()]
        self.assertEqual(
            keys,
            [data_repo.STEP_CHECK, data_repo.STEP_INIT, data_repo.STEP_MIGRATE, data_repo.STEP_SYNC],
        )

    def test_successful_run_reports_every_step(self):
        result = data_repo.run(str(self.origin))
        self.assertTrue(result.ok)
        self.assertEqual(len(result.steps), 2, "没给 local_root 时是「检查」+「初始化」两步")
        self.assertTrue(all(s.ok for s in result.steps))
        self.assertIsNone(result.failed_step)

    def test_failure_points_at_the_step_that_failed(self):
        self.seed_remote()
        result = data_repo.run(str(self.origin))
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.failed_step)
        self.assertEqual(result.failed_step.key, data_repo.STEP_CHECK)

    def test_transcript_reads_like_a_checklist(self):
        result = data_repo.run(str(self.origin))
        text = result.transcript()
        self.assertIn("✓", text)
        self.assertIn("确认远端是空白仓库", text)
        self.assertIn("建好目录结构并首次推送", text)


class TestStepThree(WorkflowTestCase):
    """第 3 步：自动同步的开关和动作。"""

    def test_disabled_without_a_remote(self):
        class Cfg:
            auto_sync = True
            data_remote = ""

        self.assertFalse(data_repo.auto_sync_enabled(Cfg()))

    def test_disabled_when_the_switch_is_off(self):
        class Cfg:
            auto_sync = False
            data_remote = "git@example.com:x.git"

        self.assertFalse(data_repo.auto_sync_enabled(Cfg()))

    def test_enabled_when_both_are_set(self):
        class Cfg:
            auto_sync = True
            data_remote = "git@example.com:x.git"

        self.assertTrue(data_repo.auto_sync_enabled(Cfg()))

    def test_sync_now_without_a_remote_explains_itself(self):
        class Cfg:
            auto_sync = True
            data_remote = ""

        result = data_repo.sync_now(Cfg())
        self.assertFalse(result.ok)
        self.assertIn("设置", result.describe(), "要告诉用户去哪配")

    def test_sync_now_pushes_sessions_and_memory(self):
        data_repo.run(str(self.origin))
        os.environ["VA_DATA_REMOTE"] = str(self.origin)
        self.addCleanup(os.environ.pop, "VA_DATA_REMOTE", None)

        from va.memory import remember
        from va.session import SessionStore

        remember("偏好", "提交信息用中文", scope="global")
        store = SessionStore.create(self.root, "m")
        store.append_message({"role": "user", "content": "你好"})
        store.append_end("done", 1)

        class Cfg:
            auto_sync = True
            data_remote = str(self.origin)

        self.assertTrue(data_repo.sync_now(Cfg()).ok)

        tree = self.remote_tree()
        self.assertIn("memory/", tree)
        self.assertIn("projects/", tree)
        self.assertIn("sessions/", tree)

    def test_clean_local_clone(self):
        data_repo.run(str(self.origin))
        clone = session_sync.remote_dir(str(self.origin), kind="data")
        self.assertTrue(clone.exists())
        self.assertTrue(data_repo.clean_local_clone(str(self.origin)))
        self.assertFalse(clone.exists())

    def test_clean_is_safe_when_nothing_is_there(self):
        self.assertFalse(data_repo.clean_local_clone("/tmp/never-cloned-xyz"))


class TestSetupThroughTheApi(AgentTestCase):
    """走 HTTP 那一层。

    这里专门盯一个踩过的坑：**初始化成功后配置必须落盘**。
    以前的写法在成功分支里直接 return，跳过了后面统一的写 .env 步骤——
    仓库建好了、配置没保存，重启后自动同步永远不触发，
    而界面上看起来一切正常。
    """

    def setUp(self) -> None:
        super().setUp()
        import json
        import socket
        import threading
        import urllib.request

        from http.server import ThreadingHTTPServer

        from va.tools import build_registry
        from va.web.server import Handler, WebApp
        from helpers import ScriptedLLM, turn

        self._json = json
        self._urllib = urllib.request
        self._turn = turn

        self.env_path = self.root / ".env"
        self.env_path.write_text("VA_API_KEY=sk-x\n", encoding="utf-8")

        self.app = WebApp(
            self.cfg,
            self.workspace,
            llm=ScriptedLLM(turn("x")),
            registry=build_registry(self.workspace),
            env_path=self.env_path,
        )
        Handler.app = self.app
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

        self.origin = self.root / "ai-memory.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def post(self, payload):
        import urllib.error
        import urllib.request as u

        request = u.Request(
            self.url + "/api/settings",
            data=self._json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with u.urlopen(request, timeout=60) as response:
                return self._json.loads(response.read())
        except urllib.error.HTTPError as exc:
            # 失败时服务端会用 4xx——响应体里仍然是我们关心的 JSON，
            # 不该因为它不是 2xx 就把内容丢掉
            return self._json.loads(exc.read())

    def test_setup_writes_the_config_to_disk(self):
        data = self.post({"setup_data_repo": True, "url": str(self.origin)})
        self.assertTrue(data["ok"], data.get("error"))

        saved = self.env_path.read_text(encoding="utf-8")
        self.assertIn(f"VA_DATA_REMOTE={self.origin}", saved)
        self.assertIn("VA_AUTO_SYNC=1", saved)

    def test_setup_turns_on_auto_sync(self):
        data = self.post({"setup_data_repo": True, "url": str(self.origin)})
        self.assertTrue(data["settings"]["auto_sync"], "初始化完应该自动打开自动同步")

    def test_setup_clears_conflicting_session_settings(self):
        """配了统一数据仓库，就不该再留着单独的会话目录，否则两边打架。"""
        data = self.post({"setup_data_repo": True, "url": str(self.origin)})
        self.assertTrue(data["ok"])
        saved = self.env_path.read_text(encoding="utf-8")
        self.assertIn("VA_SESSION_REMOTE=\n", saved)
        self.assertIn("VA_SESSION_DIR=\n", saved)

    def test_failed_setup_does_not_write_the_config(self):
        """第 1 步没过就绝不能写配置——否则会出现「配了远端但数据在本地」。"""
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.root / "seed")], check=True
        )
        seed = self.root / "seed"
        (seed / "x.txt").write_text("x", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(seed), check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed"],
            cwd=str(seed), check=True,
        )
        subprocess.run(["git", "push", "-q", "origin", "HEAD"], cwd=str(seed), check=True)

        data = self.post({"setup_data_repo": True, "url": str(self.origin)})
        self.assertFalse(data["ok"])
        self.assertNotIn("VA_DATA_REMOTE", self.env_path.read_text(encoding="utf-8"))

    def test_failure_response_lists_every_step(self):
        """失败时要说清卡在哪一步。"""
        data = self.post({"setup_data_repo": True, "url": "/definitely/not/a/repo"})
        self.assertFalse(data["ok"])
        self.assertEqual(len(data["steps"]), 1)
        self.assertFalse(data["steps"][0]["ok"])

    def test_settings_expose_the_step_list(self):
        """界面直接拿这份清单渲染，避免两边文案改歪。"""
        data = self.app.settings()
        self.assertIn("data_repo_steps", data)
        self.assertEqual(len(data["data_repo_steps"]), len(data_repo.describe_steps()))
        for step in data["data_repo_steps"]:
            self.assertTrue(step["title"])
            self.assertTrue(step["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestMigration(WorkflowTestCase):
    """切换存储位置时，已有的记忆和会话要跟着走。

    这条踩过坑：配置改了、路径算对了，但**正在用的那条会话**还指着老路径，
    消息继续写到老地方，推送过去什么都没有——看起来就是「同步成功但记录没进去」。
    """

    def _seed_old_root(self) -> Path:
        old = self.root / "old"
        (old / "memory").mkdir(parents=True)
        (old / "memory" / "偏好.md").write_text("提交信息用中文", encoding="utf-8")
        sessions = old / "projects" / "proj-a" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "20260101-000000-aaaa.jsonl").write_text("{}\n", encoding="utf-8")
        return old

    def test_copies_memory_and_sessions(self):
        old = self._seed_old_root()
        new = self.root / "new"
        new.mkdir()

        result = data_repo.migrate(old, new)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.copied, 2)
        self.assertTrue((new / "memory" / "偏好.md").is_file())
        self.assertTrue(
            (new / "projects" / "proj-a" / "sessions" / "20260101-000000-aaaa.jsonl").is_file()
        )

    def test_does_not_delete_the_original(self):
        """只复制不删除——切回去或者中途失败时东西还在。"""
        old = self._seed_old_root()
        new = self.root / "new"
        new.mkdir()
        data_repo.migrate(old, new)
        self.assertTrue((old / "memory" / "偏好.md").is_file(), "原位置的被删了")

    def test_does_not_overwrite_existing_files(self):
        """新位置可能已经有内容（比如刚克隆下来的别人的记录）。"""
        old = self._seed_old_root()
        new = self.root / "new"
        (new / "memory").mkdir(parents=True)
        (new / "memory" / "偏好.md").write_text("远端已有的版本", encoding="utf-8")

        result = data_repo.migrate(old, new)

        self.assertEqual(result.skipped, 1)
        self.assertEqual(
            (new / "memory" / "偏好.md").read_text(encoding="utf-8"),
            "远端已有的版本",
            "覆盖了远端已有的文件",
        )

    def test_never_touches_git_directories(self):
        old = self._seed_old_root()
        (old / "projects" / ".git").mkdir(parents=True)
        (old / "projects" / ".git" / "config").write_text("x", encoding="utf-8")
        new = self.root / "new"
        new.mkdir()

        data_repo.migrate(old, new)

        self.assertFalse((new / "projects" / ".git").exists(), "把 .git 也搬过去了")

    def test_same_path_is_a_noop(self):
        old = self._seed_old_root()
        result = data_repo.migrate(old, old)
        self.assertTrue(result.ok)
        self.assertEqual(result.copied, 0)

    def test_missing_source_is_not_an_error(self):
        new = self.root / "new"
        new.mkdir()
        result = data_repo.migrate(self.root / "nope", new)
        self.assertTrue(result.ok, "源目录不存在不该算失败")


class TestWorkflowWithMigration(WorkflowTestCase):
    """完整工作流里带上迁移。"""

    def test_run_reports_a_migration_step(self):
        old = self.root / "old"
        (old / "memory").mkdir(parents=True)
        (old / "memory" / "x.md").write_text("x", encoding="utf-8")

        result = data_repo.run(str(self.origin), local_root=old)

        self.assertTrue(result.ok, result.describe())
        keys = [s.key for s in result.steps]
        self.assertIn(data_repo.STEP_MIGRATE, keys)

        clone = session_sync.remote_dir(str(self.origin), kind="data")
        self.assertTrue((clone / "memory" / "x.md").is_file(), "旧内容没搬过来")

    def test_second_run_adopts_instead_of_failing(self):
        """第二次配置同一个仓库要能接管，而不是报「不是空白仓库」。"""
        first = data_repo.run(str(self.origin))
        self.assertTrue(first.ok)

        second = data_repo.run(str(self.origin))
        self.assertTrue(second.ok, second.describe())
        self.assertIn("接管", second.message)


class TestHistoryAndSessions(AgentTestCase):
    """浏览器界面要能看到之前的对话。"""

    def setUp(self) -> None:
        super().setUp()
        from va.web.server import WebApp

        self.app = WebApp(self.cfg, self.workspace)

    def test_history_is_empty_for_a_fresh_session(self):
        self.assertEqual(self.app.history(), [])

    def test_history_flattens_messages_for_display(self):
        from va.session import SessionStore

        store = SessionStore.create(self.root, "m")
        store.append_message({"role": "user", "content": "帮我看下登录"})
        store.append_message(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "1", "type": "function",
                     "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}
                ],
            }
        )
        store.append_message({"role": "tool", "content": "内容", "name": "read_file"})
        store.append_message({"role": "assistant", "content": "有三处问题"})

        _, history, _ = store.load()
        self.app.agent.messages = history

        kinds = [m["kind"] for m in self.app.history()]
        self.assertEqual(kinds, ["user", "tool", "result", "assistant"])

    def test_system_messages_are_not_shown(self):
        self.app.agent.messages = [
            {"role": "system", "content": "提示词"},
            {"role": "user", "content": "你好"},
        ]
        self.assertEqual([m["kind"] for m in self.app.history()], ["user"])

    def test_session_info_reports_the_file(self):
        from va.session import SessionStore

        store = SessionStore.create(self.root, "m")
        store.set_title("测试会话")
        self.app.agent.session = store

        info = self.app.session_info()
        self.assertTrue(info["saved"])
        self.assertEqual(info["title"], "测试会话")
        self.assertTrue(info["path"].endswith(".jsonl"), info["path"])

    def test_session_info_without_a_store(self):
        self.app.agent.session = None
        self.assertFalse(self.app.session_info()["saved"])


class TestSessionRebinding(AgentTestCase):
    """改了存储位置之后，**正在用的**那条会话必须跟着走。

    这是「同步成功但记录没进 git」的根因：会话存储在服务启动时建好，
    路径只算一次。之后改配置，环境变量变了，但那个 store 还指着老路径。
    """

    def setUp(self) -> None:
        super().setUp()
        from va.tools import build_registry
        from va.web.server import WebApp

        self.app = WebApp(
            self.cfg, self.workspace, registry=build_registry(self.workspace)
        )

    def test_rebinds_after_the_location_changes(self):
        from va.session import SessionStore

        store = SessionStore.create(self.root, "m", session_dir=self.root / "before")
        store.append_message({"role": "user", "content": "旧位置写的"})
        self.app.agent.session = store

        os.environ["VA_SESSION_DIR"] = str(self.root / "after")
        self.addCleanup(os.environ.pop, "VA_SESSION_DIR", None)

        note = self.app._rebind_session()

        self.assertIn("挪到", note)
        self.assertEqual(store.path.parent, self.root / "after")
        self.assertTrue(store.path.is_file(), "文件没跟着复制过去")
        self.assertIn("旧位置写的", store.path.read_text(encoding="utf-8"))

    def test_no_op_when_already_in_the_right_place(self):
        from va.session import SessionStore

        target = self.root / "same"
        store = SessionStore.create(self.root, "m", session_dir=target)
        self.app.agent.session = store
        os.environ["VA_SESSION_DIR"] = str(target)
        self.addCleanup(os.environ.pop, "VA_SESSION_DIR", None)

        self.assertEqual(self.app._rebind_session(), "")

    def test_writes_go_to_the_new_place_afterwards(self):
        from va.session import SessionStore

        store = SessionStore.create(self.root, "m", session_dir=self.root / "before")
        self.app.agent.session = store
        os.environ["VA_SESSION_DIR"] = str(self.root / "after")
        self.addCleanup(os.environ.pop, "VA_SESSION_DIR", None)

        self.app._rebind_session()
        store.append_message({"role": "assistant", "content": "新位置写的"})

        self.assertIn("新位置写的", store.path.read_text(encoding="utf-8"))


class TestLegacySessionsAreAdopted(AgentTestCase):
    """项目里老位置（``.va/sessions``）的会话要能进仓库。

    这条是用户实际踩到的：他配好了远端，但会话一直在 ``<项目>/.va/sessions``
    里——**老位置的优先级压过了数据仓库**，于是推送过去永远是空的。

    两件事都要做对：
      1. 配了仓库时，仓库优先于老位置
      2. 老位置里已有的记录要搬过去，不能变成孤儿
    """

    def setUp(self) -> None:
        super().setUp()
        self.project = self.root / "proj"
        self.legacy = self.project / ".va" / "sessions"
        self.legacy.mkdir(parents=True)
        for index in (1, 2, 3):
            (self.legacy / f"2026010{index}-000000-aaaa.jsonl").write_text(
                '{"type":"message"}\n', encoding="utf-8"
            )
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def use_repo(self) -> None:
        os.environ["VA_DATA_REMOTE"] = str(self.repo)
        os.environ.pop("VA_SESSION_REMOTE", None)
        os.environ.pop("VA_SESSION_DIR", None)
        self.addCleanup(os.environ.pop, "VA_DATA_REMOTE", None)

    def test_repo_wins_over_the_legacy_location(self):
        """**这是根因**：老位置不该盖过用户显式配的仓库。"""
        from va.session import SessionStore

        os.environ.pop("VA_DATA_REMOTE", None)
        os.environ.pop("VA_SESSION_REMOTE", None)
        os.environ.pop("VA_SESSION_DIR", None)
        without = SessionStore.default_dir(self.project)
        self.assertEqual(without, self.legacy, "没配仓库时该继续用老位置")

        self.use_repo()
        with_repo = SessionStore.default_dir(self.project)
        self.assertNotEqual(with_repo, self.legacy, "配了仓库还在用老位置")
        self.assertIn("repo", str(with_repo))

    def test_legacy_records_are_copied_into_the_repo(self):
        from va.session import migrate_legacy_sessions

        self.use_repo()
        moved, note = migrate_legacy_sessions(self.project)

        self.assertEqual(moved, 3)
        self.assertIn("3 条", note)
        # 数据根是**克隆目录**，不是那个 URL 本身
        from va.session import SessionStore

        target = SessionStore.default_dir(self.project)
        sessions = list(target.glob("*.jsonl"))
        self.assertEqual(len(sessions), 3)

    def test_original_files_are_kept(self):
        """只复制不删除——用户可能还想留着项目里那份。"""
        from va.session import migrate_legacy_sessions

        self.use_repo()
        migrate_legacy_sessions(self.project)
        self.assertEqual(len(list(self.legacy.glob("*.jsonl"))), 3)

    def test_does_not_overwrite(self):
        from va.session import SessionStore, migrate_legacy_sessions

        self.use_repo()
        target = SessionStore.default_dir(self.project)
        target.mkdir(parents=True, exist_ok=True)
        existing = target / "20260101-000000-aaaa.jsonl"
        existing.write_text("远端已有的版本", encoding="utf-8")

        moved, _ = migrate_legacy_sessions(self.project)

        self.assertEqual(moved, 2, "已存在的那条不该被算作搬过去")
        self.assertEqual(existing.read_text(encoding="utf-8"), "远端已有的版本")

    def test_noop_when_there_is_nothing_to_move(self):
        from va.session import migrate_legacy_sessions

        self.use_repo()
        import shutil as _shutil

        _shutil.rmtree(self.legacy)
        moved, note = migrate_legacy_sessions(self.project)
        self.assertEqual((moved, note), (0, ""))

    def test_noop_when_not_configured(self):
        """没配仓库时不该乱搬——那会让老位置的记录突然消失。"""
        from va.session import migrate_legacy_sessions

        os.environ.pop("VA_DATA_REMOTE", None)
        os.environ.pop("VA_SESSION_REMOTE", None)
        os.environ.pop("VA_SESSION_DIR", None)
        moved, _ = migrate_legacy_sessions(self.project)
        self.assertEqual(moved, 0)

    def test_end_to_end_legacy_records_reach_the_remote(self):
        """完整链路：老位置 → 仓库 → 远端。用户要的就是这个。"""
        origin = self.root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)

        os.environ["VA_DATA_REMOTE"] = str(origin)
        os.environ.pop("VA_SESSION_REMOTE", None)
        os.environ.pop("VA_SESSION_DIR", None)
        self.addCleanup(os.environ.pop, "VA_DATA_REMOTE", None)

        self.assertTrue(session_sync.ensure_clone(str(origin), kind="data").ok)
        from va.session import migrate_legacy_sessions

        migrate_legacy_sessions(self.project)
        self.assertTrue(session_sync.sync(str(origin), kind="data").ok)

        listing = subprocess.run(
            ["git", "-c", "core.quotepath=false", "ls-tree", "-r", "--name-only", "HEAD"],
            cwd=str(origin), capture_output=True, text=True,
        ).stdout
        self.assertEqual(listing.count(".jsonl"), 3, f"远端没收到会话：{listing}")


class TestStorageReport(AgentTestCase):
    """启动自检。

    「配了远端但记录还写在本地」排查起来最费劲——配置对、路径算出来也对，
    只有真去 git 里看才发现什么都没有。所以启动时就把状态摆出来。
    """

    def setUp(self) -> None:
        super().setUp()
        from va.web.server import WebApp

        self.app = WebApp(self.cfg, self.workspace)

    def labels(self) -> dict:
        return {row["label"]: row for row in self.app.storage_report()}

    def test_reports_no_remote_by_default(self):
        rows = self.labels()
        self.assertIn("会话写在", rows)
        self.assertIn("未配置", rows["记忆仓库"]["value"])

    def test_warns_when_sessions_are_not_in_the_configured_repo(self):
        """**这就是用户踩的坑**：配了仓库，会话却还在项目里。"""
        import subprocess

        repo = self.root / "r.git"
        subprocess.run(["git", "init", "-q", "--bare", str(repo)], check=True)
        self.cfg.data_remote = str(repo)
        self.cfg.auto_sync = True

        legacy = self.root / ".va" / "sessions"
        legacy.mkdir(parents=True)
        (legacy / "x.jsonl").write_text("{}", encoding="utf-8")

        rows = self.labels()
        self.assertEqual(rows["旧位置的记录"]["state"], "warn")
        self.assertIn("自动搬进仓库", rows["旧位置的记录"]["hint"])

    def test_says_auto_push_is_on(self):
        import subprocess

        repo = self.root / "r.git"
        subprocess.run(["git", "init", "-q", "--bare", str(repo)], check=True)
        self.cfg.data_remote = str(repo)
        self.cfg.auto_sync = True
        os.environ["VA_DATA_REMOTE"] = str(repo)
        self.addCleanup(os.environ.pop, "VA_DATA_REMOTE", None)

        rows = self.labels()
        self.assertIn("自动推送", rows["记忆仓库"]["hint"])

    def test_says_auto_push_is_off(self):
        import subprocess

        repo = self.root / "r.git"
        subprocess.run(["git", "init", "-q", "--bare", str(repo)], check=True)
        self.cfg.data_remote = str(repo)
        self.cfg.auto_sync = False

        self.assertIn("已关闭", self.labels()["记忆仓库"]["hint"])


class TestAutoSyncDefaults(AgentTestCase):
    """配了仓库就默认自动同步。"""

    def test_on_when_a_repo_is_configured(self):
        from va.data_repo import auto_sync_enabled

        class Cfg:
            data_remote = "git@x:y.git"
            auto_sync = True

        self.assertTrue(auto_sync_enabled(Cfg()))

    def test_off_without_a_repo(self):
        from va.data_repo import auto_sync_enabled

        class Cfg:
            data_remote = ""
            auto_sync = True

        self.assertFalse(auto_sync_enabled(Cfg()))

    def test_explicitly_disabled(self):
        from va.data_repo import auto_sync_enabled

        class Cfg:
            data_remote = "git@x:y.git"
            auto_sync = False

        self.assertFalse(auto_sync_enabled(Cfg()))
