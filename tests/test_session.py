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

    def test_default_dir_is_inside_workspace(self):
        self.assertEqual(SessionStore.default_dir(self.root), self.root / ".va" / "sessions")

    def test_append_and_reload(self):
        store = SessionStore.create(self.root, "m")
        store.append_message({"role": "user", "content": "你好"})
        store.append_message({"role": "assistant", "content": "你也好"})

        meta, messages = SessionStore.open(self.root, store.meta.id).load()

        self.assertEqual([m["content"] for m in messages], ["你好", "你也好"])
        self.assertEqual(meta.id, store.meta.id)

    def test_system_messages_are_not_persisted(self):
        store = SessionStore.create(self.root, "m")
        store.append_message({"role": "system", "content": "内部提示词"})
        store.append_message({"role": "user", "content": "hi"})

        _, messages = store.load()

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

        _, messages = store.load()

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

        _, messages = store.load()

        self.assertEqual(len(messages), 2)
        self.assertIn("摘要", messages[0]["content"])
        self.assertEqual(messages[1]["content"], "压缩后的新消息")
        self.assertNotIn("旧消息0", json.dumps(messages, ensure_ascii=False))

    def test_latest_compact_wins(self):
        store = SessionStore.create(self.root, "m")
        store.append_compact([{"role": "user", "content": "第一次压缩"}])
        store.append_message({"role": "user", "content": "中间消息"})
        store.append_compact([{"role": "user", "content": "第二次压缩"}])

        _, messages = store.load()

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

        meta, _ = store.load()

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

        meta, messages = store.load()

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


class TestAgentSession(AgentTestCase):
    def test_run_persists_full_conversation(self):
        store = SessionStore.create(self.root, "fake")
        llm = ScriptedLLM(
            turn("", tc("write_file", path="a.txt", content="hello\n")),
            turn("写完了。"),
        )
        agent, _ = self.make_agent(llm, session=store)
        agent.run("写个文件")

        _, messages = store.load()
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
        meta, history = reopened.load()
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
        _, history = reopened.load()
        agent2, _ = self.make_agent(ScriptedLLM(turn("二")), session=reopened, history=history)
        agent2.run("第二轮")

        _, messages = reopened.load()
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
        _, messages = store.load()

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

        meta, _ = store.load()
        self.assertEqual(meta.end_reason, "finished")
        self.assertEqual(meta.steps, 1)

    def test_title_taken_from_first_task(self):
        store = SessionStore.create(self.root, "fake")
        agent, _ = self.make_agent(ScriptedLLM(turn("ok")), session=store)
        agent.run("修复登录页的样式问题")

        meta, _ = store.load()
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
