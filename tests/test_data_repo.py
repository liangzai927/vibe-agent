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
    """第 1 步：必须是空白仓库。"""

    def test_empty_remote_passes(self):
        ok, why = data_repo.check_remote(str(self.origin))
        self.assertTrue(ok, why)

    def test_non_empty_remote_is_refused(self):
        self.seed_remote()
        ok, why = data_repo.check_remote(str(self.origin))
        self.assertFalse(ok)
        self.assertIn("空白仓库", why)

    def test_refusal_explains_how_to_get_an_empty_one(self):
        """光说「不是空的」不够——用户得知道怎么建一个空的。

        最常见的原因就是在 GitHub 上建仓库时勾了 README。
        """
        self.seed_remote()
        _, why = data_repo.check_remote(str(self.origin))
        self.assertIn("不要勾选", why)
        self.assertIn("README", why)

    def test_unreachable_remote_is_reported(self):
        ok, why = data_repo.check_remote("/definitely/not/a/repo")
        self.assertFalse(ok)
        self.assertIn("连不上", why)

    def test_empty_url(self):
        ok, why = data_repo.check_remote("")
        self.assertFalse(ok)
        self.assertIn("没填", why)


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
        self.assertEqual(keys, [data_repo.STEP_CHECK, data_repo.STEP_INIT, data_repo.STEP_SYNC])

    def test_successful_run_reports_every_step(self):
        result = data_repo.run(str(self.origin))
        self.assertTrue(result.ok)
        self.assertEqual(len(result.steps), 2, "成功路径应该报告「检查」和「初始化」两步")
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
        self.assertEqual(len(data["data_repo_steps"]), 3)
        for step in data["data_repo_steps"]:
            self.assertTrue(step["title"])
            self.assertTrue(step["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
