"""会话记录写入前的脱敏。

**用户报过这个**：API Key 泄漏到了记忆仓库的 git 历史里。

泄漏路径很普通：agent 执行了一条命令（`cat .env`、`grep -r API_KEY`、
打印环境变量……），**工具结果原样写进了会话文件**，然后自动同步推上去。

会话记录会被推到 git 远端，所以一旦密钥进了那里，它就不只是
「本机的一个文件」——它已经在别人的服务器上，而且进了 git 历史，
**删文件是删不掉的**。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402
from va.secrets import looks_secret, redact, redact_message  # noqa: E402


class TestRedact(unittest.TestCase):
    def test_env_style_assignment(self):
        out, hits = redact("VA_API_KEY=sk-abcdefghijklmnopqrstuvwxyz")
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", out)
        self.assertIn("[已隐藏", out)
        self.assertEqual(hits, ["环境变量"])

    def test_the_variable_name_survives(self):
        """**「哪个 key 泄漏了」本身是有用信息**，值才是敏感的。"""
        out, _ = redact("OPENAI_API_KEY=sk-abcdefghijklmnopqrst")
        self.assertIn("OPENAI_API_KEY", out)
        self.assertNotIn("sk-abcdefghijklmnopqrst", out)

    def test_quoted_values(self):
        for line in (
            'OPENAI_API_KEY="sk-proj-abcdefghijklmnop"',
            "OPENAI_API_KEY: 'sk-proj-abcdefghijklmnop'",
            "AI_TOKEN: ghp_abcdefghijklmnopqrstuvwx",
        ):
            out, hits = redact(line)
            self.assertTrue(hits, f"没拦住：{line}")

    def test_bearer_tokens(self):
        out, hits = redact("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6")
        self.assertIn("Bearer", out)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6", out)
        self.assertEqual(hits, ["Bearer"])

    def test_bare_sk_tokens(self):
        """没写变量名、光一个 `sk-` 令牌也要拦。"""
        out, hits = redact("常见的 key 是 sk-1234567890123456789012 这样的")
        self.assertNotIn("sk-1234567890123456789012", out)
        self.assertTrue(hits)

    def test_private_key_blocks(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----"
        out, hits = redact(pem)
        self.assertNotIn("MIIEow", out)
        self.assertEqual(hits, ["私钥"])

    def test_normal_text_is_untouched(self):
        """**别误伤。** 打码太多会把会话记录变得没法读。"""
        for line in (
            "这个函数的参数 key 是字符串",
            "在 workflow.yaml 里设置 VA_API_KEY= 然后重启",
            "普通的说明文字，没有任何密钥",
        ):
            out, hits = redact(line)
            self.assertEqual(out, line, f"误伤了：{line}")
            self.assertEqual(hits, [])

    def test_empty_and_none(self):
        self.assertEqual(redact(""), ("", []))
        self.assertEqual(redact(None), (None, []))

    def test_looks_secret_does_not_modify(self):
        self.assertTrue(looks_secret("VA_API_KEY=sk-abcdefghijklmnop"))
        self.assertFalse(looks_secret("普通文本"))


class TestRedactMessage(unittest.TestCase):
    def test_does_not_mutate_the_original(self):
        original = {"role": "tool", "content": "VA_API_KEY=sk-abcdefghijklmnop"}
        snapshot = json.dumps(original)
        redact_message(original)
        self.assertEqual(json.dumps(original), snapshot, "改了原对象")

    def test_handles_nested_structures(self):
        message = {
            "role": "assistant",
            "tool_calls": [
                {"function": {"arguments": '{"env": "API_SECRET=abcdefghijklmnop"}'}}
            ],
        }
        cleaned, hits = redact_message(message)
        self.assertTrue(hits)
        self.assertNotIn("abcdefghijklmnop", json.dumps(cleaned))

    def test_non_string_values_survive(self):
        cleaned, _ = redact_message({"role": "x", "n": 3, "flag": True, "none": None})
        self.assertEqual(cleaned, {"role": "x", "n": 3, "flag": True, "none": None})


class TestSessionWritesAreScrubbed(AgentTestCase):
    """**拦在写入的唯一出口上。**"""

    def create(self):
        from va.session import SessionStore

        return SessionStore.create(self.root, "test-model")

    def session_file(self) -> Path:
        import os

        files = list(Path(os.environ["VA_HOME"]).rglob("*.jsonl"))
        self.assertTrue(files, "没找到会话文件")
        return files[0]

    def test_a_key_in_a_tool_result_never_lands_on_disk(self):
        store = self.create()
        store.append_message(
            {
                "role": "tool",
                "content": "VA_API_KEY=sk-realkey1234567890abcdef\n正常输出",
            }
        )
        text = self.session_file().read_text(encoding="utf-8")
        self.assertNotIn("sk-realkey1234567890abcdef", text, "密钥落盘了")
        self.assertIn("[已隐藏", text)
        self.assertIn("正常输出", text, "把正常内容也删了")

    def test_the_redaction_is_reported(self):
        """**用户不该在毫不知情的情况下被保护**——出现它说明差点泄漏，
        他该知道并考虑轮换。"""
        store = self.create()
        store.append_message({"role": "tool", "content": "TOKEN=abcdefghijklmnopqrst"})
        self.assertTrue(store.redactions)

    def test_compact_snapshots_are_scrubbed_too(self):
        """压缩会把整个消息列表重写一遍——**那条路也要走脱敏**，
        不然脱敏等于没做。"""
        store = self.create()
        store.append_compact(
            [{"role": "user", "content": "VA_API_KEY=sk-compact1234567890abcd"}]
        )
        text = self.session_file().read_text(encoding="utf-8")
        self.assertNotIn("sk-compact1234567890abcd", text, "压缩快照这条路漏了")

    def test_clean_sessions_are_untouched(self):
        store = self.create()
        store.append_message({"role": "user", "content": "帮我改个按钮颜色"})
        self.assertEqual(store.redactions, [])
        self.assertIn("帮我改个按钮颜色", self.session_file().read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
