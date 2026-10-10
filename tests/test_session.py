"""会话持久化的测试。

除了「存得下、读得回」，重点验证两件容易漏的事：
* 压缩前后的历史必须能正确重放（compact 是整体替换，不是追加）；
* 会话文件是追加写的，最后一行可能因为崩溃而残缺，读取时必须能容忍。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase, ScriptedLLM, tc, turn  # noqa: E402
from va.cli import _staleness_note  # noqa: E402
from va.paths import agent_home, project_slug, sessions_dir  # noqa: E402
from va.session import SessionMeta, SessionStore, git_head  # noqa: E402


class TestSessionStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---------------------------------------------------------------- 基础

    def test_create_writes_header(self):
        store = SessionStore.create(self.root, "test-model")

        self.assertTrue(store.path.is_file())
        records = [json.loads(line) for line in store.path.read_text().splitlines()]
        self.assertEqual(records[0]["type"], "session")
        self.assertEqual(records[0]["model"], "test-model")
        self.assertEqual(records[0]["workspace"], str(self.root))

    def test_default_dir_is_outside_workspace(self):
        """默认在项目外——放项目里会一直弄脏 git status。"""
        target = SessionStore.default_dir(self.root)
        self.assertNotIn(str(self.root), str(target))
        self.assertEqual(target, sessions_dir(self.root))

    def test_append_and_reload(self):
        store = SessionStore.create(self.root, "m")
        store.append_message({"role": "user", "content": "你好"})
        store.append_message({"role": "assistant", "content": "你也好"})

        meta, messages, _ = SessionStore.open(self.root, store.meta.id).load()

        self.assertEqual([m["content"] for m in messages], ["你好", "你也好"])
        self.assertEqual(meta.id, store.meta.id)

    def test_system_messages_are_not_persisted(self):
        store = SessionStore.create(self.root, "m")
        store.append_message({"role": "system", "content": "内部提示词"})
        store.append_message({"role": "user", "content": "hi"})

        _, messages, _ = store.load()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "user")

    def test_tool_calls_round_trip_intact(self):
        store = SessionStore.create(self.root, "m")
        store.append_message(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
            }
        )
        store.append_message({"role": "tool", "tool_call_id": "c1", "content": "结果"})

        _, messages, _ = store.load()
        self.assertEqual(messages[0]["tool_calls"][0]["id"], "c1")
        self.assertEqual(messages[1]["tool_call_id"], "c1")

    def test_writes_are_append_only(self):
        store = SessionStore.create(self.root, "m")
        for i in range(5):
            store.append_message({"role": "user", "content": str(i)})

        lines = store.path.read_text().strip().splitlines()

        self.assertEqual(len(lines), 6)  # 1 行头 + 5 行消息
        self.assertEqual(json.loads(lines[1])["message"]["content"], "0")

    # ---------------------------------------------------------------- 压缩

    def test_compact_replaces_history_instead_of_appending(self):
        store = SessionStore.create(self.root, "m")
        for i in range(6):
            store.append_message({"role": "user", "content": f"旧消息{i}"})
        store.append_compact([{"role": "user", "content": "[摘要] 之前做了些事"}])
        store.append_message({"role": "user", "content": "压缩后的新消息"})

        _, messages, _ = store.load()
        self.assertEqual(len(messages), 2)
        self.assertIn("摘要", messages[0]["content"])
        self.assertEqual(messages[1]["content"], "压缩后的新消息")
        self.assertNotIn("旧消息0", json.dumps(messages, ensure_ascii=False))

    def test_latest_compact_wins(self):
        store = SessionStore.create(self.root, "m")
        store.append_compact([{"role": "user", "content": "第一次压缩"}])
        store.append_message({"role": "user", "content": "中间消息"})
        store.append_compact([{"role": "user", "content": "第二次压缩"}])

        _, messages, _ = store.load()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"], "第二次压缩")

    # ---------------------------------------------------------------- 列表

    def test_sessions_listed_newest_first(self):
        old = SessionStore.create(self.root, "m")
        old.append_message({"role": "user", "content": "老会话"})
        time.sleep(0.02)
        new = SessionStore.create(self.root, "m")
        new.append_message({"role": "user", "content": "新会话"})

        metas = SessionStore.sessions(self.root)

        self.assertEqual(len(metas), 2)
        self.assertEqual(metas[0].id, new.meta.id)

    def test_latest_returns_most_recent(self):
        SessionStore.create(self.root, "m")
        time.sleep(0.02)
        newest = SessionStore.create(self.root, "m")

        store = SessionStore.latest(self.root)

        self.assertIsNotNone(store)
        self.assertEqual(store.meta.id, newest.meta.id)

    def test_no_sessions_returns_empty(self):
        self.assertEqual(SessionStore.sessions(self.root), [])
        self.assertIsNone(SessionStore.latest(self.root))

    def test_open_missing_session_raises(self):
        with self.assertRaises(FileNotFoundError):
            SessionStore.open(self.root, "does-not-exist")

    def test_title_and_end_are_recorded(self):
        store = SessionStore.create(self.root, "m")
        store.set_title("修复登录 bug")
        store.append_end("finished", 7)

        meta, _, _ = store.load()
        self.assertEqual(meta.title, "修复登录 bug")
        self.assertEqual(meta.steps, 7)
        self.assertEqual(meta.end_reason, "finished")

    # ---------------------------------------------------------------- 容错

    def test_truncated_last_line_is_skipped(self):
        """崩溃时可能只写了半行 JSON —— 读取必须能容忍，而不是整个会话报废。"""
        store = SessionStore.create(self.root, "m")
        store.append_message({"role": "user", "content": "完整的一行"})
        with store.path.open("a", encoding="utf-8") as handle:
            handle.write('{"type": "message", "message": {"role": "user", "conte')

        meta, messages, _ = store.load()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"], "完整的一行")

    def test_file_permissions_are_restrictive(self):
        if os.name == "nt":
            self.skipTest("Windows 权限模型不同")
        store = SessionStore.create(self.root, "m")
        mode = store.path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_git_head_none_outside_repo(self):
        self.assertIsNone(git_head(self.root))

    def test_git_head_recorded_inside_repo(self):
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, env=env)
        (self.root / "a.txt").write_text("x")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.root, check=True, env=env)

        store = SessionStore.create(self.root, "m")

        self.assertIsNotNone(store.meta.git_head)
        self.assertEqual(store.meta.git_head, git_head(self.root))


class TestSessionLocation(unittest.TestCase):
    """会话存哪。

    用户的诉求：记录别放在项目里（git status 一直被弄脏），
    而且要能配置到 git 仓库或同步盘。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.project = self.root / "myapi"
        self.project.mkdir()
        self._saved = os.environ.pop("VA_SESSION_DIR", None)

    def tearDown(self) -> None:
        if self._saved is not None:
            os.environ["VA_SESSION_DIR"] = self._saved
        else:
            os.environ.pop("VA_SESSION_DIR", None)

    def _legacy(self, count: int = 1) -> Path:
        legacy = self.project / ".va" / "sessions"
        legacy.mkdir(parents=True, exist_ok=True)
        for index in range(count):
            (legacy / f"2026010{index}-000000-aaaa.jsonl").write_text(
                '{"type":"session","id":"x"}\n', encoding="utf-8"
            )
        return legacy

    # ---------------------------------------------------------- 默认位置

    def test_default_is_outside_the_project(self):
        target = SessionStore.default_dir(self.project)
        self.assertNotIn(
            str(self.project), str(target), "会话不该落在项目里——会弄脏 git status"
        )
        self.assertTrue(str(target).startswith(str(agent_home())))

    def test_two_projects_with_the_same_name_do_not_collide(self):
        """~/work/api 和 ~/personal/api 是两个项目，不能混在一起。"""
        a, b = self.root / "work" / "api", self.root / "personal" / "api"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        self.assertNotEqual(
            SessionStore.default_dir(a), SessionStore.default_dir(b)
        )

    def test_slug_is_readable(self):
        self.assertTrue(project_slug(self.project).startswith("myapi-"))

    def test_slug_keeps_non_ascii_names(self):
        chinese = self.root / "我的项目"
        chinese.mkdir()
        self.assertTrue(project_slug(chinese).startswith("我的项目-"))

    def test_slug_handles_weird_names(self):
        weird = self.root / "a b/c:d"
        weird.mkdir(parents=True)
        slug = project_slug(weird)
        self.assertNotIn("/", slug)
        self.assertNotIn(" ", slug)

    def test_slug_handles_filesystem_root(self):
        self.assertTrue(project_slug("/"))

    # ---------------------------------------------------------- 配置覆盖

    def test_env_override_wins(self):
        os.environ["VA_SESSION_DIR"] = str(self.root / "elsewhere")
        self.assertEqual(
            SessionStore.default_dir(self.project), self.root / "elsewhere"
        )

    def test_env_override_expands_user(self):
        os.environ["VA_SESSION_DIR"] = "~/Dropbox/va"
        self.assertFalse(str(SessionStore.default_dir(self.project)).startswith("~"))

    def test_env_override_wins_over_legacy(self):
        """显式配置比「旧位置有历史」优先级更高。"""
        self._legacy()
        os.environ["VA_SESSION_DIR"] = str(self.root / "elsewhere")
        self.assertEqual(
            SessionStore.default_dir(self.project), self.root / "elsewhere"
        )

    def test_sessions_dir_honours_override(self):
        os.environ["VA_SESSION_DIR"] = str(self.root / "shared")
        self.assertEqual(sessions_dir(self.project), self.root / "shared")

    # ---------------------------------------------------------- 旧位置兼容

    def test_legacy_with_history_is_kept(self):
        """旧位置有会话就继续用它——装作没看见等于把用户的历史弄丢。"""
        legacy = self._legacy()
        self.assertEqual(SessionStore.default_dir(self.project), legacy)

    def test_empty_legacy_dir_is_ignored(self):
        (self.project / ".va" / "sessions").mkdir(parents=True)
        target = SessionStore.default_dir(self.project)
        self.assertNotIn(str(self.project), str(target))

    def test_legacy_is_used_even_with_many_sessions(self):
        legacy = self._legacy(5)
        self.assertEqual(SessionStore.default_dir(self.project), legacy)

    # ---------------------------------------------------------- 迁移提示

    def test_hint_is_empty_without_legacy(self):
        self.assertEqual(SessionStore.migration_hint(self.project), "")

    def test_hint_is_empty_when_legacy_is_empty(self):
        (self.project / ".va" / "sessions").mkdir(parents=True)
        self.assertEqual(SessionStore.migration_hint(self.project), "")

    def test_hint_mentions_count_and_a_runnable_command(self):
        self._legacy(3)
        hint = SessionStore.migration_hint(self.project)
        self.assertIn("3 个", hint)
        self.assertIn("mkdir -p", hint)
        self.assertIn("mv ", hint)
        self.assertIn(str(sessions_dir(self.project)), hint)

    def test_hint_paths_are_resolved(self):
        """给出的命令要能直接复制粘贴——/tmp 和 /private/tmp 混着写很怪。"""
        self._legacy()
        hint = SessionStore.migration_hint(self.project)
        for line in hint.splitlines():
            command = line.strip()
            if not command.startswith("mkdir"):
                continue
            for token in command.replace("&&", " ").split():
                if not token.startswith("/"):
                    continue
                # 去掉 _1 和末尾斜杠再比
                bare = token.rstrip("/").split("/*")[0]
                self.assertEqual(bare, str(Path(bare).resolve()), f"{token} 没 resolve")


class TestAgentSessionWritesOutsideTheProject(AgentTestCase):
    """端到端：新项目里跑一个任务，项目目录不该多出任何东西。"""

    def test_session_lands_outside(self):
        store = SessionStore.create(self.root, "m")
        self.assertNotIn(str(self.root), str(store.path))
        self.assertTrue(store.path.is_file())

    def test_project_directory_stays_empty(self):
        SessionStore.create(self.root, "m")
        leftovers = [
            p for p in self.root.rglob("*")
            if p.is_file() and ".va" in p.parts
        ]
        self.assertEqual(leftovers, [], f"项目里多了：{leftovers}")


class TestAgentSession(AgentTestCase):
    def test_run_persists_full_conversation(self):
        store = SessionStore.create(self.root, "fake")
        llm = ScriptedLLM(
            turn("", tc("write_file", path="a.txt", content="hello\n")),
            turn("写完了。"),
        )
        agent, _ = self.make_agent(llm, session=store)
        agent.run("写个文件")

        _, messages, _ = store.load()
        roles = [m["role"] for m in messages]

        self.assertEqual(roles[0], "user")
        self.assertIn("assistant", roles)
        self.assertIn("tool", roles)
        self.assertEqual(messages[-1]["content"], "写完了。")

    def test_resume_restores_history(self):
        store = SessionStore.create(self.root, "fake")
        first = ScriptedLLM(turn("第一次回答"))
        agent, _ = self.make_agent(first, session=store)
        agent.run("第一次任务")

        reopened = SessionStore.open(self.root, store.meta.id)
        meta, history, _ = reopened.load()
        self.assertTrue(history)

        second = ScriptedLLM(turn("第二次回答"))
        agent2, _ = self.make_agent(second, session=reopened, history=history)
        answer = agent2.run("继续")

        self.assertEqual(answer, "第二次回答")
        sent = json.dumps(second.received[0], ensure_ascii=False)
        self.assertIn("第一次任务", sent)
        self.assertIn("第一次回答", sent)
        # 系统提示词是最新的，不是从文件里读回来的
        self.assertEqual(second.received[0][0]["role"], "system")

    def test_resumed_conversation_keeps_appending(self):
        store = SessionStore.create(self.root, "fake")
        agent, _ = self.make_agent(ScriptedLLM(turn("一")), session=store)
        agent.run("第一轮")

        reopened = SessionStore.open(self.root, store.meta.id)
        _, history, _ = reopened.load()
        agent2, _ = self.make_agent(ScriptedLLM(turn("二")), session=reopened, history=history)
        agent2.run("第二轮")

        _, messages, _ = reopened.load()
        contents = [m.get("content") for m in messages if m["role"] == "user"]
        self.assertIn("第一轮", contents)
        self.assertIn("第二轮", contents)

    def test_compaction_is_persisted(self):
        self.cfg.context_budget = 100
        store = SessionStore.create(self.root, "fake")
        filler = "x" * 400
        llm = ScriptedLLM(
            *[turn("", tc("run_bash", command=f"echo {filler}")) for _ in range(12)],
            turn("完成。"),
        )
        agent, _ = self.make_agent(llm, session=store)
        agent.run("刷屏")

        self.assertGreater(agent.compactions, 0)
        _, messages, _ = store.load()
        # 压缩后落盘的应该是「摘要 + 保留段」，而不是全部原始消息
        self.assertLess(len(messages), len(agent.messages))
        self.assertIn("压缩摘要", messages[0]["content"])
        self.assertEqual(messages[0]["role"], "user")

    def test_no_session_means_no_files(self):
        llm = ScriptedLLM(turn("hi"))
        agent, _ = self.make_agent(llm)
        agent.run("你好")

        self.assertIsNone(agent.session_id)
        self.assertFalse((self.root / ".va").exists())

    def test_end_record_written_after_run(self):
        store = SessionStore.create(self.root, "fake")
        agent, _ = self.make_agent(ScriptedLLM(turn("done")), session=store)
        agent.run("随便说说")

        meta, _, _ = store.load()
        self.assertEqual(meta.end_reason, "finished")
        self.assertEqual(meta.steps, 1)

    def test_title_taken_from_first_task(self):
        store = SessionStore.create(self.root, "fake")
        agent, _ = self.make_agent(ScriptedLLM(turn("ok")), session=store)
        agent.run("修复登录页的样式问题")

        meta, _, _ = store.load()
        self.assertIn("修复登录页", meta.title)


class TestResumeSafety(unittest.TestCase):
    """恢复会话时最容易出事的地方：历史是过期的。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _meta(self, head=None) -> SessionMeta:
        return SessionMeta(
            id="x",
            workspace=str(self.root),
            model="m",
            created_at=time.time(),
            git_head=head,
        )

    def test_note_always_tells_model_to_recheck(self):
        note = _staleness_note(self._meta(), self.root)
        self.assertIn("恢复的会话", note)
        self.assertIn("read_file", note)

    def test_note_mentions_head_change(self):
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, env=env)
        (self.root / "a.txt").write_text("x")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.root, check=True, env=env)

        note = _staleness_note(self._meta(head="deadbee"), self.root)

        self.assertIn("deadbee", note)
        self.assertIn("已被改动", note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
