"""针对 LLM 流式层的集成测试。

起一个假的 OpenAI 兼容服务，真实走一遍 HTTP + SSE，专门验证最难的部分：
tool_calls 在流式响应里是被切碎成多个分片下发的，必须正确拼回一个完整调用。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from va.config import Config  # noqa: E402
from va.llm import LLM  # noqa: E402
from va.loop import Agent  # noqa: E402
from va.tools import build_registry  # noqa: E402
from va.ui import UI  # noqa: E402
from va.workspace import Workspace  # noqa: E402


def chunk(delta: dict, finish: str | None = None) -> dict:
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "mock",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def text_chunks(*pieces: str) -> list[dict]:
    chunks = [chunk({"role": "assistant", "content": p}) for p in pieces]
    chunks.append(chunk({}, "stop"))
    return chunks


def tool_call_chunks(calls: list[tuple[str, str, list[str]]]) -> list[dict]:
    """calls: [(id, name, [参数分片...]), ...]，模拟真实服务逐片下发。"""
    chunks: list[dict] = []
    for index, (call_id, name, arg_pieces) in enumerate(calls):
        chunks.append(
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": ""},
                        }
                    ]
                }
            )
        )
        for piece in arg_pieces:
            chunks.append(
                chunk({"tool_calls": [{"index": index, "function": {"arguments": piece}}]})
            )
    chunks.append(chunk({}, "tool_calls"))
    return chunks


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        body = json.loads(raw or b"{}")

        server: MockServer = self.server  # type: ignore[assignment]
        server.requests.append(body)
        index = server.served
        server.served += 1
        script = server.script[index] if index < len(server.script) else [chunk({}, "stop")]

        payload = b"".join(
            f"data: {json.dumps(c, ensure_ascii=False)}\n\n".encode() for c in script
        ) + b"data: [DONE]\n\n"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()

    def log_message(self, *args) -> None:
        pass  # 静音


class MockServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, script):
        super().__init__(("127.0.0.1", 0), MockHandler)
        self.script = script
        self.requests: list[dict] = []
        self.served = 0

    @property
    def base_url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}/v1"


class StreamTestCase(unittest.TestCase):
    script: list = []

    def setUp(self) -> None:
        self.server = MockServer(type(self).script)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        self.cfg = Config(
            workspace=self.root,
            model="mock-model",
            api_key="test-key",
            base_url=self.server.base_url,
        )
        self.llm = LLM(self.cfg)

    def _stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class TestStreaming(StreamTestCase):
    script = [text_chunks("你好", "，", "世界")]

    def test_content_is_concatenated(self):
        pieces: list[str] = []
        turn = self.llm.complete([{"role": "user", "content": "hi"}], on_text=pieces.append)

        self.assertEqual(turn.content, "你好，世界")
        self.assertEqual("".join(pieces), "你好，世界")
        self.assertFalse(turn.has_tool_calls)
        self.assertEqual(turn.finish_reason, "stop")

    def test_request_payload_shape(self):
        self.llm.complete(
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "x", "description": "d", "parameters": {}}}],
        )
        sent = self.server.requests[-1]
        self.assertEqual(sent["model"], "mock-model")
        self.assertTrue(sent["stream"])
        self.assertEqual(sent["tool_choice"], "auto")
        self.assertEqual(len(sent["tools"]), 1)


class SilentUI(UI):
    """完全静音的 UI，只记录工具结果。"""

    def __init__(self, workspace):
        super().__init__(workspace, auto_approve=True, show_thinking=False)
        self.results: list[str] = []

    def tool_result(self, text, ok):
        self.results.append(text)

    def tool_call(self, *a, **k):
        pass

    def assistant_header(self, *a, **k):
        pass

    def stream_text(self, *a, **k):
        pass

    def stream_thinking(self, *a, **k):
        pass

    def newline(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def success(self, *a, **k):
        pass

    def warn(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class TestToolCallStreaming(StreamTestCase):
    script = [
        tool_call_chunks(
            [
                (
                    "call_abc",
                    "read_file",
                    ['{"pa', 'th": ', '"a.txt"}'],
                )
            ]
        ),
        text_chunks("读完了。"),
    ]

    def test_fragmented_arguments_are_reassembled(self):
        turn = self.llm.complete([{"role": "user", "content": "读文件"}])
        self.assertEqual(len(turn.tool_calls), 1)

        call = turn.tool_calls[0]
        self.assertEqual(call.id, "call_abc")
        self.assertEqual(call.name, "read_file")
        self.assertEqual(call.parsed_arguments(), {"path": "a.txt"})

    def test_full_loop_over_http(self):
        (self.root / "a.txt").write_text("文件内容\n")

        workspace = Workspace(self.root)
        ui = SilentUI(workspace)
        agent = Agent(
            self.cfg,
            ui,
            workspace=workspace,
            registry=build_registry(workspace),
            llm=self.llm,
        )
        answer = agent.run("读取 a.txt")

        self.assertEqual(answer, "读完了。")
        self.assertTrue(ui.results)
        self.assertIn("文件内容", ui.results[0])
        # 工具结果必须真的回灌给了模型（第二轮请求里应含 tool 消息）
        second = self.server.requests[-1]["messages"]
        self.assertTrue(any(m.get("role") == "tool" for m in second))


class TestParallelToolCalls(StreamTestCase):
    script = [
        tool_call_chunks(
            [
                ("call_1", "list_dir", ['{"path"', ': "."}']),
                ("call_2", "glob", ['{"pattern"', ': "*.py"}']),
            ]
        ),
        text_chunks("看完了。"),
    ]

    def test_two_calls_kept_separate(self):
        turn = self.llm.complete([{"role": "user", "content": "看看"}])
        self.assertEqual(len(turn.tool_calls), 2)
        self.assertEqual(turn.tool_calls[0].name, "list_dir")
        self.assertEqual(turn.tool_calls[0].parsed_arguments(), {"path": "."})
        self.assertEqual(turn.tool_calls[1].name, "glob")
        self.assertEqual(turn.tool_calls[1].parsed_arguments(), {"pattern": "*.py"})


class TestReasoningStream(StreamTestCase):
    script = [
        [
            chunk({"role": "assistant", "reasoning_content": "让我想想"}),
            chunk({"reasoning_content": "……"}),
            chunk({"content": "答案是 42"}),
            chunk({}, "stop"),
        ]
    ]

    def test_reasoning_content_captured(self):
        thoughts: list[str] = []
        turn = self.llm.complete([{"role": "user", "content": "?"}], on_reasoning=thoughts.append)
        self.assertEqual(turn.reasoning, "让我想想……")
        self.assertEqual(turn.content, "答案是 42")
        self.assertEqual("".join(thoughts), "让我想想……")


if __name__ == "__main__":
    unittest.main(verbosity=2)
