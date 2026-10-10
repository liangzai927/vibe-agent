"""项目仓库远端配置的测试。

用户因为「两个远端」困惑过一次（项目代码的 vs 会话记录的），
所以这里既测功能，也测**文案有没有把两者区分开**。

用**真的本地 git 仓库**测，不 mock git——远端相关的坑基本都在
git 自己的边界行为上（首次推送、没有 upstream、远端不存在）。
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase, ScriptedLLM, turn  # noqa: E402
from va import gitstate  # noqa: E402
from va.gitstate import looks_like_credentials, mask_url  # noqa: E402
from va.gitutil import explain as explain_error  # noqa: E402


class TestMaskUrl(unittest.TestCase):
    """远端 URL 里的凭据必须打码。

    这个值会显示在界面上、也会被写进日志和会话文件——
    明文 token 泄露一次就够麻烦了。
    """

    def test_masks_password_in_https(self):
        masked = mask_url("https://me:ghp_SECRET123@github.com/x/y.git")
        self.assertNotIn("ghp_SECRET123", masked)
        self.assertIn("***", masked)
        self.assertIn("github.com/x/y.git", masked, "主机和路径要保留，否则认不出是哪个远端")

    def test_keeps_username(self):
        self.assertTrue(mask_url("https://me:secret@github.com/x").startswith("https://me:"))

    def test_scp_style_is_untouched(self):
        """git@github.com:... 里的 git 是用户名，不是密钥——打码反而让人认不出。"""
        url = "git@github.com:owner/repo.git"
        self.assertEqual(mask_url(url), url)

    def test_plain_https_is_untouched(self):
        url = "https://github.com/owner/repo.git"
        self.assertEqual(mask_url(url), url)

    def test_token_only_userinfo(self):
        masked = mask_url("https://ghp_SECRET@github.com/x/y")
        self.assertNotIn("ghp_SECRET", masked)

    def test_credentials_detection(self):
        self.assertTrue(looks_like_credentials("https://me:secret@github.com/x"))
        self.assertFalse(looks_like_credentials("https://github.com/x"))
        self.assertFalse(looks_like_credentials("git@github.com:owner/repo.git"))
        self.assertFalse(looks_like_credentials("/local/path/repo.git"))


class TestExplain(unittest.TestCase):
    """把 git 的 stderr 翻成人话。

    起因是一个真实的坏消息：用户推送失败，界面上显示
    「推送失败：and the repository exists.」——那是 git 报错的**最后一行套话**，
    真正的病因（`Connection closed by ... port 22`）在第一行，被丢掉了。
    """

    # git 报错的固定尾巴，任何情况下都不该成为结论
    FILLER = (
        "Please make sure you have the correct access rights\n"
        "and the repository exists."
    )

    def test_never_returns_the_boilerplate_tail(self):
        stderr = (
            "Connection closed by 203.0.113.1 port 22\n"
            "fatal: Could not read from remote repository.\n\n" + self.FILLER
        )
        message = explain_error(stderr)
        self.assertNotIn("repository exists", message)
        self.assertNotIn("correct access rights", message)

    def test_identifies_a_blocked_ssh_port(self):
        stderr = (
            "Connection closed by 203.0.113.1 port 22\n"
            "fatal: Could not read from remote repository.\n\n" + self.FILLER
        )
        message = explain_error(stderr)
        self.assertIn("连不上", message)
        self.assertIn("HTTPS", message, "要给出可操作的替代方案")

    def test_identifies_missing_ssh_key(self):
        stderr = (
            "git@github.com: Permission denied (publickey).\n"
            "fatal: Could not read from remote repository."
        )
        self.assertIn("SSH key", explain_error(stderr))

    def test_identifies_timeout(self):
        self.assertIn("超时", explain_error("ssh: connect to host github.com port 22: Connection timed out"))

    def test_identifies_dns_failure(self):
        self.assertIn("解析", explain_error("ssh: Could not resolve hostname github.com"))

    def test_identifies_missing_repo(self):
        self.assertIn("不存在", explain_error("remote: Repository not found."))

    def test_identifies_rejected_push(self):
        stderr = (
            "! [rejected]        main -> main (non-fast-forward)\n"
            "error: failed to push some refs to 'git@github.com:x/y.git'"
        )
        self.assertIn("拉取", explain_error(stderr))

    def test_falls_back_to_the_first_meaningful_line(self):
        """没有已知模式时，取第一条有信息量的，而不是最后一条。"""
        stderr = "something weird happened\nfatal: Could not read from remote repository.\n\n" + self.FILLER
        self.assertEqual(explain_error(stderr), "something weird happened")

    def test_empty_input(self):
        self.assertTrue(explain_error(""))
        self.assertTrue(explain_error("   \n  "))

    def test_patterns_look_at_all_lines_not_just_the_last(self):
        """病因几乎总在第一行——只看最后一行必然错过。"""
        for first_line, expected in (
            ("Connection closed by x port 22", "连不上"),
            ("ssh: connect to host github.com port 22: Connection timed out", "超时"),
            ("git@github.com: Permission denied (publickey).", "SSH key"),
        ):
            stderr = first_line + "\nfatal: Could not read from remote repository.\n" + self.FILLER
            self.assertIn(expected, explain_error(stderr), f"漏了：{first_line}")


class GitRepoTestCase(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.project = self.root / "proj"
        self.project.mkdir()
        self.origin = self.root / "origin.git"
        self.git("init", "-q", "--bare", str(self.origin))

    def git(self, *args, cwd=None, check=False):
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.project),
            capture_output=True, text=True, check=check,
        )

    def init_project(self, *, commit: bool = True, remote: str | None = None) -> None:
        self.git("init", "-q")
        self.git("config", "user.name", "t")
        self.git("config", "user.email", "t@t")
        if commit:
            (self.project / "a.txt").write_text("x\n")
            self.git("add", "-A")
            self.git("commit", "-q", "-m", "初始提交")
        if remote:
            self.git("remote", "add", "origin", remote)


class TestState(GitRepoTestCase):
    def test_non_repo_is_reported_not_raised(self):
        info = gitstate.state(self.project)
        self.assertFalse(info.is_repo)
        self.assertEqual(info.error, "")

    def test_reads_branch_and_cleanliness(self):
        self.init_project()
        info = gitstate.state(self.project)
        self.assertTrue(info.is_repo)
        self.assertIn(info.branch, ("main", "master"))
        self.assertEqual(info.dirty, 0)
        self.assertEqual(info.untracked, 0)
        self.assertTrue(info.last_commit)

    def test_counts_changes(self):
        self.init_project()
        (self.project / "a.txt").write_text("改了\n")
        (self.project / "new.txt").write_text("新的\n")
        info = gitstate.state(self.project)
        self.assertEqual(info.dirty, 1)
        self.assertEqual(info.untracked, 1)

    def test_lists_remotes(self):
        self.init_project(remote=str(self.origin))
        info = gitstate.state(self.project)
        self.assertEqual([name for name, _ in info.remotes], ["origin"])
        self.assertIn("origin.git", info.origin_url)

    def test_remote_url_is_masked_in_state(self):
        """状态会被发到浏览器，里面的凭据必须先打码。"""
        self.init_project(remote="https://me:ghp_SECRET@github.com/x/y.git")
        info = gitstate.state(self.project)
        self.assertNotIn("ghp_SECRET", str(info.remotes))
        self.assertNotIn("ghp_SECRET", info.origin_url)

    def test_ahead_and_behind(self):
        self.init_project(remote=str(self.origin))
        gitstate.push(self.project)
        self.assertEqual(gitstate.state(self.project).ahead, 0)

        (self.project / "b.txt").write_text("b\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "第二个")
        self.assertEqual(gitstate.state(self.project).ahead, 1)


class TestSetRemote(GitRepoTestCase):
    def test_adds_a_new_remote(self):
        self.init_project()
        ok, message = gitstate.set_remote(self.project, "origin", str(self.origin))
        self.assertTrue(ok, message)
        self.assertIn("已添加", message)
        self.assertIn("origin", self.git("remote").stdout)

    def test_updates_an_existing_remote(self):
        self.init_project(remote=str(self.origin))
        other = self.root / "other.git"
        subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True)
        ok, message = gitstate.set_remote(self.project, "origin", str(other))
        self.assertTrue(ok, message)
        self.assertIn("已更新", message)
        self.assertEqual(self.git("remote").stdout.split().count("origin"), 1, "不该重复添加")

    def test_rejects_empty_url(self):
        self.init_project()
        ok, message = gitstate.set_remote(self.project, "origin", "")
        self.assertFalse(ok)
        self.assertIn("没填地址", message)

    def test_rejects_spaces(self):
        self.init_project()
        ok, _ = gitstate.set_remote(self.project, "origin", "/tmp/a b.git")
        self.assertFalse(ok)

    def test_warns_when_credentials_are_embedded(self):
        """凭据会明文躺在 .git/config 里，得提醒一句。"""
        self.init_project()
        ok, message = gitstate.set_remote(
            self.project, "origin", "https://me:ghp_SECRET@github.com/x/y.git"
        )
        self.assertTrue(ok)
        self.assertIn("⚠", message)
        self.assertNotIn("ghp_SECRET", message, "提示里的地址也要打码")

    def test_defaults_to_origin(self):
        self.init_project()
        ok, _ = gitstate.set_remote(self.project, "", str(self.origin))
        self.assertTrue(ok)
        self.assertIn("origin", self.git("remote").stdout)


class TestRemoveRemote(GitRepoTestCase):
    def test_removes(self):
        self.init_project(remote=str(self.origin))
        ok, _ = gitstate.remove_remote(self.project, "origin")
        self.assertTrue(ok)
        self.assertNotIn("origin", self.git("remote").stdout)

    def test_missing_name_is_rejected(self):
        self.init_project()
        self.assertFalse(gitstate.remove_remote(self.project, "")[0])

    def test_unknown_remote_is_reported(self):
        self.init_project()
        ok, message = gitstate.remove_remote(self.project, "nope")
        self.assertFalse(ok)
        self.assertTrue(message)


class TestPush(GitRepoTestCase):
    def test_push_without_remote_explains_what_to_do(self):
        self.init_project()
        ok, message = gitstate.push(self.project)
        self.assertFalse(ok)
        self.assertIn("不存在", message)
        self.assertIn("设置", message, "要告诉用户去哪配")

    def test_first_push_sets_upstream(self):
        self.init_project(remote=str(self.origin))
        ok, message = gitstate.push(self.project)
        self.assertTrue(ok, message)
        self.assertIn("已推送", message)
        self.assertTrue(gitstate.state(self.project).has_upstream)

        # 远端真的收到了
        files = subprocess.run(
            ["git", "log", "--oneline"], cwd=str(self.origin),
            capture_output=True, text=True,
        )
        self.assertIn("初始提交", files.stdout)

    def test_push_when_nothing_to_push(self):
        self.init_project(remote=str(self.origin))
        gitstate.push(self.project)
        ok, _ = gitstate.push(self.project)
        self.assertTrue(ok, "没有新提交不算失败")

    def test_push_reports_ahead_count(self):
        self.init_project(remote=str(self.origin))
        gitstate.push(self.project)
        (self.project / "b.txt").write_text("b\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "第二个")
        self.assertEqual(gitstate.state(self.project).ahead, 1)
        self.assertTrue(gitstate.push(self.project)[0])

    def test_push_on_non_repo_is_reported(self):
        ok, message = gitstate.push(self.project)
        self.assertFalse(ok)
        self.assertIn("git 仓库", message)


class TestInitRepo(GitRepoTestCase):
    def test_initialises(self):
        ok, message = gitstate.init_repo(self.project)
        self.assertTrue(ok, message)
        self.assertTrue((self.project / ".git").exists())

    def test_is_idempotent(self):
        gitstate.init_repo(self.project)
        ok, message = gitstate.init_repo(self.project)
        self.assertTrue(ok)
        self.assertIn("已经是", message)


class TestGitApi(AgentTestCase):
    """走 HTTP 的那一层。"""

    def setUp(self) -> None:
        super().setUp()
        from http.server import ThreadingHTTPServer

        from va.web.server import Handler, WebApp
        import json as _json
        import socket
        import threading
        import urllib.request

        self._json = _json
        self._urllib = urllib.request

        self.app = WebApp(
            self.cfg, self.workspace, llm=ScriptedLLM(turn("x")),
            registry=__import__("va.tools", fromlist=["build_registry"]).build_registry(self.workspace),
        )
        Handler.app = self.app
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def get(self, path):
        with self._urllib.urlopen(self.url + path, timeout=10) as r:
            return self._json.loads(r.read())

    def post(self, payload):
        import urllib.request as u
        request = u.Request(
            self.url + "/api/git",
            data=self._json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with u.urlopen(request, timeout=30) as r:
            return self._json.loads(r.read())

    def test_state_endpoint(self):
        data = self.get("/api/git")
        self.assertIn("is_repo", data)
        self.assertIn("remotes", data)
        self.assertIn("branch", data)

    def test_set_remote_through_the_api(self):
        origin = self.root / "o.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        # 工作区得先是仓库
        subprocess.run(["git", "init", "-q"], cwd=str(self.root), check=True)

        data = self.post({"command": "set-remote", "name": "origin", "url": str(origin)})
        self.assertTrue(data["ok"], data.get("message"))
        self.assertIn(str(origin), data["message"])

    def test_unknown_command_is_rejected(self):
        data = self.post({"command": "nonsense"})
        self.assertFalse(data["ok"])
        self.assertIn("未知操作", data["error"])


class TestMessagingDistinguishesTheTwoRemotes(unittest.TestCase):
    """文案必须把两个远端区分开——用户已经困惑过一次。"""

    def test_session_sync_message_says_which_remote(self):
        from va.session_sync import sync

        message = sync("").describe()
        self.assertIn("会话记录", message)
        self.assertIn("两回事", message)

    def test_git_modules_are_separate(self):
        """两个模块管的是不同的仓库，不能互相依赖。"""
        import va.session_sync as sessions

        self.assertFalse(hasattr(gitstate, "remote_dir"), "项目仓库不该有会话仓库的概念")
        self.assertTrue(hasattr(sessions, "remote_dir"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
