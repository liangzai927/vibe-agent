"""浏览器界面的测试。

web 化最容易做错的一件事是**审批**：终端里 ``input()`` 天然阻塞，
到了 HTTP 里如果不显式阻塞 worker 线程，agent 会一路跑到底，
审批卡片变成摆设——而且从界面上完全看不出来。所以这里花最多篇幅盯它。

HTTP 层的测试会**真起一个服务器**打真实请求。handlers 里塞的是
ScriptedLLM，不会产生任何 API 调用。
"""

from __future__ import annotations

import os
import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

from helpers import AgentTestCase, ScriptedLLM, tc, turn  # noqa: E402
from va.config import Config  # noqa: E402
from va.tools import build_registry  # noqa: E402
from va.web.bridge import ApprovalGate, EventBus, WebUI  # noqa: E402
from va.web.server import Handler, WebApp  # noqa: E402


# --------------------------------------------------------------- 事件总线


class TestEventBus(unittest.TestCase):
    def test_publish_assigns_increasing_seq(self):
        bus = EventBus()
        self.assertEqual(bus.publish("a"), 1)
        self.assertEqual(bus.publish("b"), 2)
        self.assertEqual(bus.seq, 2)

    def test_since_returns_only_newer(self):
        bus = EventBus()
        bus.publish("a")
        bus.publish("b")
        bus.publish("c")
        events = bus.since(1)
        self.assertEqual([e["type"] for e in events], ["b", "c"])

    def test_payload_is_merged(self):
        bus = EventBus()
        bus.publish("note", level="warn", text="小心")
        event = bus.since(0)[0]
        self.assertEqual(event["level"], "warn")
        self.assertEqual(event["text"], "小心")

    def test_replay_after_reconnect(self):
        """浏览器断线重连后要能按序号补发——这正是用日志而不是队列的原因。"""
        bus = EventBus()
        for i in range(5):
            bus.publish("tick", i=i)
        missed = bus.since(2)
        self.assertEqual([e["i"] for e in missed], [2, 3, 4])

    def test_log_is_trimmed(self):
        bus = EventBus(limit=10)
        for i in range(50):
            bus.publish("tick", i=i)
        kept = bus.since(0)
        self.assertEqual(len(kept), 10)
        self.assertEqual(kept[-1]["i"], 49, "最新的必须还在")

    def test_wait_returns_immediately_when_data_exists(self):
        bus = EventBus()
        bus.publish("a")
        started = time.monotonic()
        events = bus.wait(0, timeout=5)
        self.assertEqual(len(events), 1)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_wait_blocks_then_wakes(self):
        bus = EventBus()
        got: list[list[dict]] = []

        def reader():
            got.append(bus.wait(0, timeout=5))

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        time.sleep(0.2)
        self.assertTrue(thread.is_alive(), "没有事件时应该等着")
        bus.publish("wake")
        thread.join(timeout=5)
        self.assertEqual(got[0][0]["type"], "wake")

    def test_wait_times_out_without_events(self):
        bus = EventBus()
        started = time.monotonic()
        self.assertEqual(bus.wait(0, timeout=0.2), [])
        self.assertGreaterEqual(time.monotonic() - started, 0.15)


# --------------------------------------------------------------- 审批闸门


class TestApprovalGate(unittest.TestCase):
    def test_resolve_unblocks_the_request(self):
        gate = ApprovalGate(timeout=5)
        answer: list[str] = []

        def worker():
            request_id, result = gate.request(lambda rid: None)
            answer.append(result)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        time.sleep(0.2)
        self.assertTrue(thread.is_alive(), "没人回答之前必须一直等着")

        self.assertTrue(gate.resolve("req-1", "y"))
        thread.join(timeout=5)
        self.assertEqual(answer, ["y"])

    def test_announce_receives_the_request_id(self):
        gate = ApprovalGate(timeout=5)
        seen: list[str] = []

        def worker():
            gate.request(lambda rid: seen.append(rid))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        time.sleep(0.2)
        self.assertEqual(seen, ["req-1"])
        gate.resolve("req-1", "n")
        thread.join(timeout=5)

    def test_timeout_returns_timeout_not_a_crash(self):
        gate = ApprovalGate(timeout=0.3)
        request_id, answer = gate.request(lambda rid: None)
        self.assertEqual(answer, "timeout")
        self.assertEqual(request_id, "req-1")

    def test_unknown_id_is_rejected(self):
        gate = ApprovalGate(timeout=1)
        self.assertFalse(gate.resolve("nope", "y"))

    def test_double_resolve_only_counts_once(self):
        gate = ApprovalGate(timeout=5)
        result: list[str] = []

        def worker():
            result.append(gate.request(lambda rid: None)[1])

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        time.sleep(0.2)
        self.assertTrue(gate.resolve("req-1", "y"))
        self.assertFalse(gate.resolve("req-1", "n"), "同一个请求不该能回答两次")
        thread.join(timeout=5)
        self.assertEqual(result, ["y"])

    def test_ids_do_not_repeat(self):
        gate = ApprovalGate(timeout=5)
        seen: list[str] = []

        def worker():
            for _ in range(3):
                gate.request(lambda rid: seen.append(rid))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        for expected in ("req-1", "req-2", "req-3"):
            for _ in range(50):
                if expected in seen:
                    break
                time.sleep(0.02)
            gate.resolve(expected, "y")
        thread.join(timeout=5)
        self.assertEqual(seen, ["req-1", "req-2", "req-3"])

    def test_waiting_count_is_tracked(self):
        gate = ApprovalGate(timeout=5)
        threading.Thread(target=lambda: gate.request(lambda rid: None), daemon=True).start()
        time.sleep(0.2)
        self.assertEqual(gate.waiting, 1)
        gate.resolve("req-1", "y")


# --------------------------------------------------------------- WebUI


class TestWebUIEvents(AgentTestCase):
    def make(self, **kwargs):
        bus = EventBus()
        ui = WebUI(self.workspace, bus, ApprovalGate(timeout=5), **kwargs)
        return ui, bus

    def test_each_note_level_has_its_own_type(self):
        ui, bus = self.make()
        ui.info("i")
        ui.warn("w")
        ui.error("e")
        ui.success("s")
        notes = [e for e in bus.since(0) if e["type"] == "note"]
        self.assertEqual([n["level"] for n in notes], ["info", "warn", "error", "success"])

    def test_streaming_is_batched(self):
        """逐 token 发事件会把 SSE 打爆。攒够一定量才发。"""
        ui, bus = self.make()
        for _ in range(3):
            ui.stream_text("x")
        self.assertEqual(
            [e for e in bus.since(0) if e["type"] == "text_delta"], [],
            "这么点内容不该立刻发出去",
        )
        ui.stream_text("y" * 500)
        deltas = [e for e in bus.since(0) if e["type"] == "text_delta"]
        self.assertEqual(len(deltas), 1)
        self.assertEqual(len(deltas[0]["piece"]), 503)

    def test_flush_on_message_end(self):
        ui, bus = self.make()
        ui.stream_text("短")
        ui.newline()
        text = "".join(e["piece"] for e in bus.since(0) if e["type"] == "text_delta")
        self.assertEqual(text, "短")

    def test_text_and_thinking_do_not_mix(self):
        """类型切换时必须先把攒下的发出去，否则两段会串在一起。"""
        ui, bus = self.make()
        ui.stream_text("正文")
        ui.stream_thinking("思考")
        # 切换时旧的那段应该已经被推出去
        events = [e for e in bus.since(0) if e["type"].endswith("_delta")]
        self.assertEqual([e["type"] for e in events], ["text_delta"])
        self.assertEqual(events[0]["piece"], "正文")

        # 收尾时把新的那段也冲出去
        ui.newline()
        events = [e for e in bus.since(0) if e["type"].endswith("_delta")]
        self.assertEqual([e["type"] for e in events], ["text_delta", "thinking_delta"])
        self.assertEqual(events[1]["piece"], "思考")

    def test_thinking_is_dropped_when_disabled(self):
        ui, bus = self.make(show_thinking=False)
        ui.stream_thinking("内心戏")
        ui.newline()
        self.assertEqual([e for e in bus.since(0) if e["type"] == "thinking_delta"], [])

    def test_tool_call_and_result(self):
        ui, bus = self.make()
        ui.tool_call(1, 2, "read_file", "a.py")
        ui.tool_result("第一行\n第二行", ok=False)
        events = bus.since(0)
        call = next(e for e in events if e["type"] == "tool_call")
        result = next(e for e in events if e["type"] == "tool_result")
        self.assertEqual(call["name"], "read_file")
        self.assertEqual(call["total"], 2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["first"], "第一行")

    def test_long_tool_result_is_capped(self):
        """工具输出可能几万行，全推给浏览器会卡死页面。"""
        ui, bus = self.make()
        ui.tool_result("\n".join(f"行{i}" for i in range(5000)), ok=True)
        event = [e for e in bus.since(0) if e["type"] == "tool_result"][0]
        self.assertEqual(event["total_lines"], 5000)
        self.assertEqual(len(event["text"].splitlines()), 60)

    def test_diff_has_ansi_stripped(self):
        """终端着色过的文本直接发给浏览器，用户会看到一堆 \\x1b[32m。"""
        ui, bus = self.make()
        ui.show_diff("\x1b[32m+added\x1b[0m")
        event = [e for e in bus.since(0) if e["type"] == "diff"][0]
        self.assertNotIn("\x1b", event["text"])
        self.assertIn("+added", event["text"])

    def test_banner_carries_context(self):
        ui, bus = self.make()
        ui.banner("m", "http://x", session_id="s1")
        event = bus.since(0)[0]
        self.assertEqual(event["type"], "banner")
        self.assertEqual(event["model"], "m")
        self.assertEqual(event["session"], "s1")

    def test_plan_and_block(self):
        ui, bus = self.make()
        ui.show_plan(type("P", (), {"render": lambda self: "● 目标"} )())
        ui.block("标题", [("a", "1")])
        types = [e["type"] for e in bus.since(0)]
        self.assertIn("plan", types)
        self.assertIn("block", types)


class TestWebUIConfirm(AgentTestCase):
    def make(self, **kwargs):
        bus = EventBus()
        gate = ApprovalGate(timeout=5)
        ui = WebUI(self.workspace, bus, gate, **kwargs)
        return ui, bus, gate

    def _run_confirm(self, ui, *args, **kwargs):
        box: list[bool] = []
        thread = threading.Thread(
            target=lambda: box.append(ui.confirm(*args, **kwargs)), daemon=True
        )
        thread.start()
        return thread, box

    def _wait_for_approval(self, bus) -> dict:
        for _ in range(200):
            found = [e for e in bus.since(0) if e["type"] == "approval"]
            if found:
                return found[0]
            time.sleep(0.02)
        raise AssertionError("审批事件一直没出现")

    def test_confirm_blocks_until_answered(self):
        ui, bus, gate = self.make()
        thread, box = self._run_confirm(ui, "write_file", {"path": "a"}, "a (新文件)")

        event = self._wait_for_approval(bus)
        self.assertTrue(thread.is_alive(), "没人点按钮之前必须一直阻塞")
        self.assertEqual(event["tool"], "write_file")
        self.assertIn("id", event)

        gate.resolve(event["id"], "y")
        thread.join(timeout=5)
        self.assertEqual(box, [True])

    def test_reject(self):
        ui, bus, gate = self.make()
        thread, box = self._run_confirm(ui, "write_file", {}, "x")
        gate.resolve(self._wait_for_approval(bus)["id"], "n")
        thread.join(timeout=5)
        self.assertEqual(box, [False])

    def test_always_remembers_for_the_session(self):
        ui, bus, gate = self.make()
        thread, box = self._run_confirm(ui, "write_file", {}, "x")
        gate.resolve(self._wait_for_approval(bus)["id"], "a")
        thread.join(timeout=5)
        self.assertEqual(box, [True])

        # 第二次应该直接放行，不再发审批事件
        before = len([e for e in bus.since(0) if e["type"] == "approval"])
        self.assertTrue(ui.confirm("write_file", {}, "x"))
        after = len([e for e in bus.since(0) if e["type"] == "approval"])
        self.assertEqual(before, after, "「总是允许」之后不该再问")

    def test_auto_approve_skips_the_gate(self):
        ui, bus, gate = self.make(auto_approve=True)
        self.assertTrue(ui.confirm("run_bash", {"command": "rm -rf /"}, "危险"))
        self.assertEqual([e for e in bus.since(0) if e["type"] == "approval"], [])
        self.assertEqual(gate.waiting, 0)

    def test_timeout_is_treated_as_reject(self):
        ui, bus, gate = self.make()
        gate.timeout = 0.2
        self.assertFalse(ui.confirm("write_file", {}, "x"))
        notes = [e for e in bus.since(0) if e["type"] == "note"]
        self.assertTrue(any("超时" in n["text"] for n in notes))

    def test_preview_is_sent_without_ansi(self):
        ui, bus, gate = self.make()
        thread, _ = self._run_confirm(
            ui, "edit_file", {}, "x", preview="\x1b[31m-old\x1b[0m"
        )
        event = self._wait_for_approval(bus)
        self.assertNotIn("\x1b", event["preview"])
        gate.resolve(event["id"], "n")
        thread.join(timeout=5)

    def test_non_string_args_are_stringified(self):
        """JSON 里必须是字符串，否则前端渲染会拿到 [object Object]。"""
        ui, bus, gate = self.make()
        thread, _ = self._run_confirm(ui, "write_file", {"n": 3, "flag": True}, "x")
        event = self._wait_for_approval(bus)
        self.assertEqual(event["args"]["n"], "3")
        self.assertEqual(event["args"]["flag"], "True")
        gate.resolve(event["id"], "n")
        thread.join(timeout=5)


# --------------------------------------------------------------- WebApp


class TestWebApp(AgentTestCase):
    def app(self, llm=None, **kwargs):
        registry = build_registry(self.workspace)
        return WebApp(
            self.cfg, self.workspace, llm=llm, registry=registry, **kwargs
        ), registry

    def test_state_reflects_config(self):
        app, _ = self.app()
        state = app.state()
        self.assertEqual(state["model"], self.cfg.model)
        self.assertFalse(state["busy"])
        # macOS 上 /var 是 /private/var 的符号链接，两边都 resolve 再比
        self.assertEqual(Path(state["workspace"]).resolve(), self.root.resolve())

    def test_submit_runs_the_agent(self):
        llm = ScriptedLLM(turn("做完了"))
        app, _ = self.app(llm)
        ok, _ = app.submit("干点活")
        self.assertTrue(ok)
        for _ in range(200):
            if not app.busy:
                break
            time.sleep(0.02)
        types = [e["type"] for e in app.bus.since(0)]
        self.assertIn("user", types)
        self.assertIn("task_end", types)

    def test_submit_rejects_empty(self):
        app, _ = self.app(ScriptedLLM())
        ok, message = app.submit("   ")
        self.assertFalse(ok)
        self.assertIn("空", message)

    def test_submit_rejects_while_busy(self):
        gate_holder = {}

        def slow(messages, tools=None, **kwargs):
            time.sleep(1.5)
            return turn("慢")

        llm = ScriptedLLM(turn("x"))
        llm.complete = slow
        app, _ = self.app(llm)
        app.submit("第一个")
        time.sleep(0.1)
        ok, message = app.submit("第二个")
        self.assertFalse(ok)
        self.assertIn("还在跑", message)
        del gate_holder

    def test_command_help(self):
        app, _ = self.app(ScriptedLLM())
        app.submit("/help")
        notes = [e for e in app.bus.since(0) if e["type"] == "note"]
        self.assertTrue(any("/diff" in n["text"] for n in notes))

    def test_command_unknown(self):
        app, _ = self.app(ScriptedLLM())
        ok, _ = app.submit("/nope")
        self.assertFalse(ok)
        notes = [e for e in app.bus.since(0) if e["type"] == "note"]
        self.assertTrue(any("未知命令" in n["text"] for n in notes))

    def test_command_diff_on_clean_session(self):
        app, _ = self.app(ScriptedLLM())
        app.submit("/diff")
        notes = [e for e in app.bus.since(0) if e["type"] == "note"]
        self.assertTrue(notes)

    def test_command_tools_lists_registry(self):
        app, _ = self.app(ScriptedLLM())
        app.submit("/tools")
        blocks = [e for e in app.bus.since(0) if e["type"] == "block"]
        self.assertEqual(len(blocks), 1)
        names = [row[0] for row in blocks[0]["rows"]]
        self.assertIn("read_file", names)

    def test_command_reset(self):
        app, _ = self.app(ScriptedLLM())
        app.agent.messages.append({"role": "user", "content": "x"})
        app.submit("/reset")
        self.assertEqual(len(app.agent.messages), 1, "应该只剩系统提示词")

    def test_task_error_is_reported_not_swallowed(self):
        class Boom:
            def complete(self, *a, **k):
                raise RuntimeError("炸了")

        app, _ = self.app(Boom())
        app.submit("干活")
        for _ in range(200):
            if not app.busy:
                break
            time.sleep(0.02)
        notes = [e for e in app.bus.since(0) if e["type"] == "note"]
        self.assertTrue(any("炸了" in n["text"] for n in notes))

    def test_save_upload(self):
        """上传的图片落在 agent 自己的目录，不进用户的项目。"""
        app, _ = self.app(ScriptedLLM())
        info = app.save_upload("图.png", b"\x89PNG\r\n\x1a\n" + b"0" * 40)
        stored = Path(info["path"])
        self.assertTrue(stored.is_file())
        self.assertEqual(stored.name, "图.png")
        self.assertNotIn(
            str(self.root), str(stored), "不该写进工作区——那会污染用户的项目"
        )
        self.assertEqual(info["bytes"], 48)

    def test_save_upload_forces_an_image_extension(self):
        app, _ = self.app(ScriptedLLM())
        info = app.save_upload("evil.sh", b"x")
        self.assertTrue(info["path"].endswith("evil.sh.png"))

    def test_save_upload_strips_directory_components(self):
        """别让 ../../ 决定文件落到哪去。"""
        app, _ = self.app(ScriptedLLM())
        info = app.save_upload("../../escape.png", b"x")
        self.assertEqual(Path(info["path"]).name, "escape.png")
        self.assertEqual(Path(info["path"]).parent.name, "inbox")


# --------------------------------------------------------------- HTTP 层


class ServerFixture:
    def __init__(self, app: WebApp) -> None:
        Handler.app = app
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def get(self, path: str, timeout: float = 5):
        with urllib.request.urlopen(self.url + path, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)

    def post(self, path: str, payload=None, raw: bytes | None = None, headers=None):
        data = raw if raw is not None else json.dumps(payload or {}).encode()
        request = urllib.request.Request(self.url + path, data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())


class TestHTTPSurface(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app = WebApp(
            self.cfg,
            self.workspace,
            llm=ScriptedLLM(turn("好了")),
            registry=build_registry(self.workspace),
        )
        self.server = ServerFixture(self.app)

    def tearDown(self) -> None:
        self.server.stop()
        super().tearDown()

    def test_index_is_served(self):
        status, body, headers = self.server.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"vibe-agent", body)
        self.assertIn("text/html", headers["Content-Type"])

    def test_state_endpoint(self):
        status, body, _ = self.server.get("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["model"], self.cfg.model)

    def test_message_endpoint(self):
        status, data = self.server.post("/api/message", {"text": "干活"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        for _ in range(200):
            if not self.app.busy:
                break
            time.sleep(0.02)
        self.assertIn("task_end", [e["type"] for e in self.app.bus.since(0)])

    def test_message_endpoint_rejects_empty(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.server.post("/api/message", {"text": ""})
        self.assertEqual(ctx.exception.code, 409)

    def test_upload_endpoint_stores_the_file(self):
        payload = b"\x89PNG\r\n\x1a\n" + b"1" * 64
        status, data = self.server.post(
            "/api/upload", raw=payload, headers={"X-Filename": "shot.png"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(Path(data["path"]).is_file())
        self.assertEqual(Path(data["path"]).read_bytes(), payload)

    def test_upload_filename_is_url_decoded(self):
        """浏览器会把中文文件名编码后再放进请求头。"""
        from urllib.parse import quote

        status, data = self.server.post(
            "/api/upload",
            raw=b"x" * 10,
            headers={"X-Filename": quote("我的设计稿.png")},
        )
        self.assertIn("我的设计稿", data["path"])

    def test_raw_serves_files_inside_the_workspace(self):
        target = self.root / "pic.png"
        target.write_bytes(b"\x89PNG-data")
        status, body, headers = self.server.get("/api/raw?path=pic.png")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"\x89PNG-data")

    def test_raw_blocks_path_traversal(self):
        outside = self.root.parent / "secret.txt"
        outside.write_text("秘密", encoding="utf-8")
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.server.get("/api/raw?path=../secret.txt")
            self.assertEqual(ctx.exception.code, 404)
        finally:
            outside.unlink()

    def test_raw_blocks_absolute_paths(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.server.get("/api/raw?path=/etc/hosts")
        self.assertEqual(ctx.exception.code, 404)

    def test_unknown_asset_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.server.get("/nope.js")
        self.assertEqual(ctx.exception.code, 404)

    def test_asset_path_traversal_is_blocked(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.server.get("/../server.py")
        self.assertEqual(ctx.exception.code, 404)


def _fingerprint(path: Path):
    return (path.exists(), path.read_bytes() if path.exists() else None)


# 真实的配置和模型缓存。这个模块里的任何测试都不许改动它们。
# 之前就是因为直接往真实目录写，把开发者的 .models.json 冲掉过一次。
_REAL_CONFIG = (ROOT / ".env", ROOT / ".models.json")
_REAL_BEFORE = None


def setUpModule() -> None:
    global _REAL_BEFORE
    _REAL_BEFORE = [_fingerprint(p) for p in _REAL_CONFIG]


def tearDownModule() -> None:
    after = [_fingerprint(p) for p in _REAL_CONFIG]
    if after != _REAL_BEFORE:
        raise AssertionError(
            "web 测试改动了项目根目录下的真实配置（.env / .models.json）！\n"
            "测试必须把 env_path 和 models_dir 指向临时目录。"
        )


class TestModelSwitching(AgentTestCase):
    """界面上换模型必须真的持久化，否则重启就变回去了。"""

    def setUp(self) -> None:
        super().setUp()
        self.env_path = self.root / ".env"
        self.env_path.write_text("VA_MODEL=old\nVA_API_KEY=sk-x\n", encoding="utf-8")
        self.catalog_dir = self.root / "agent-config"
        self.catalog_dir.mkdir(exist_ok=True)
        self.app = WebApp(
            self.cfg,
            self.workspace,
            llm=ScriptedLLM(turn("x")),
            registry=build_registry(self.workspace),
            env_path=self.env_path,
            models_dir=self.catalog_dir,
        )
        self.server = ServerFixture(self.app)

    def tearDown(self) -> None:
        self.server.stop()
        super().tearDown()

    def read_env(self) -> str:
        return self.env_path.read_text(encoding="utf-8")

    def test_models_endpoint_lists_the_catalog(self):
        from va.models import ModelCatalog, ModelInfo, save_catalog

        save_catalog(
            self.catalog_dir,
            ModelCatalog(
                base_url="x",
                models=[
                    ModelInfo(id="seer", input_modalities=["text", "image"]),
                    ModelInfo(id="blind"),
                ],
            ),
        )
        status, body, _ = self.server.get("/api/models")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn(data["current"], ["seer", "blind", "fake"])
        ids = [m["id"] for m in data["catalog"]]
        self.assertIn("seer", ids)
        seer = next(m for m in data["catalog"] if m["id"] == "seer")
        self.assertTrue(seer["supports_image"])

    def test_switch_updates_agent_and_env(self):
        status, data = self.server.post("/api/model", {"name": "brand-new"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(self.app.agent.cfg.model, "brand-new")
        self.assertIn("VA_MODEL=brand-new", self.read_env())

    def test_switch_keeps_other_env_keys(self):
        self.server.post("/api/model", {"name": "x"})
        self.assertIn("VA_API_KEY=sk-x", self.read_env(), "换模型不该冲掉别的配置")

    def test_switch_publishes_an_event(self):
        self.server.post("/api/model", {"name": "notify-me"})
        events = [e for e in self.app.bus.since(0) if e["type"] == "model"]
        self.assertEqual(events[0]["name"], "notify-me")

    def test_empty_name_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.server.post("/api/model", {"name": "  "})
        self.assertEqual(ctx.exception.code, 400)

    def test_switch_goes_through_the_slash_command_too(self):
        self.app.submit("/model via-command")
        self.assertEqual(self.app.agent.cfg.model, "via-command")


class TestSettings(AgentTestCase):
    """设置面板的后端。

    界面上改配置而不是让人去翻 .env。这里盯三件事：
    校验不能糊弄（无效路径必须在写盘前被拦）、密钥绝不能发到浏览器、
    以及**哪些改动能热生效、哪些必须重启**要说清楚。
    """

    def setUp(self) -> None:
        super().setUp()
        self.env_path = self.root / ".env"
        self.env_path.write_text("VA_API_KEY=sk-secret\n", encoding="utf-8")
        self.app = WebApp(
            self.cfg,
            self.workspace,
            llm=ScriptedLLM(turn("x")),
            registry=build_registry(self.workspace),
            env_path=self.env_path,
        )
        self.server = ServerFixture(self.app)

    def tearDown(self) -> None:
        self.server.stop()
        os.environ.pop("VA_SESSION_DIR", None)
        super().tearDown()

    def read_env(self) -> str:
        return self.env_path.read_text(encoding="utf-8")

    def post_expecting_error(self, payload: dict) -> tuple[int, dict]:
        """发一个预期会被拒的请求，把状态码和错误体都拿回来。

        不改 ServerFixture.post：别的测试靠它抛 HTTPError 来断言拒绝，
        改了会把那些测试变成永远通过。
        """
        try:
            return self.server.post("/api/settings", payload)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    # ---------------------------------------------------------- 读

    def test_settings_reports_the_fields_the_panel_needs(self):
        status, body, _ = self.server.get("/api/settings")
        self.assertEqual(status, 200)
        data = json.loads(body)
        for key in (
            "session_dir", "session_dir_display", "session_dir_default",
            "read_dirs", "home", "vision_model", "auto_approve",
            "show_thinking", "env_path",
        ):
            self.assertIn(key, data, f"设置里少了 {key}")

    def test_settings_never_leaks_the_api_key(self):
        """这个接口会把内容发到浏览器，**密钥一个字都不能出现**。

        界面需要知道「设了没有」和一个打码的样子（`sk-123…cdef`），
        所以不能简单地禁掉 `api_key` 这个词——那会把合法字段一起禁掉。
        真正该守住的是：**密钥本身不许出现**，而且任何 key 相关字段
        都必须是短的打码形式（长到能当密钥用就是漏了）。
        """
        # **必须真的配一个 key**，否则这个测试测的是空气——
        # 第一版就是这样：env 文件里写了 VA_API_KEY=sk-secret，
        # 但那份 cfg 没加载它，api_key 是空的，于是「把打码去掉」
        # 也测不出任何东西（实测照样绿）。
        self.app.cfg.api_key = "sk-secret-abcdefghijklmnop"

        status, body, _ = self.server.get("/api/settings")
        text = body.decode("utf-8")
        self.assertNotIn(
            "sk-secret-abcdefghijklmnop", text, "密钥值漏到前端了"
        )

        data = json.loads(body)
        # 这个测试的 .env 里写的是 sk-secret（见上面 setUp）。
        # **跟真实密钥值比对**才对——第一版我用「长度 < 24」来判断，
        # 而 sk-secret 只有 9 个字符，直接漏过去了（实测：把打码去掉
        # 换成返回完整 key，测试照样绿）。
        secret = "sk-secret-abcdefghijklmnop"
        for field, value in data.items():
            if "key" not in field.lower() and "secret" not in field.lower():
                continue
            if isinstance(value, bool) or value is None:
                continue          # 「设了没有」这种布尔标志是安全的
            self.assertNotEqual(
                str(value), secret,
                f"{field} 返回了完整密钥——必须打码",
            )
            self.assertNotIn(
                secret, str(value),
                f"{field} 里含完整密钥——必须打码",
            )

    def test_the_leak_check_still_catches_a_real_leak(self):
        """**别把守卫改松了。** 真把密钥塞进去必须还能抓住。"""
        self.assertIn("sk-secret", json.dumps({"api_key": "sk-secret-1234567890"}))

    def test_session_dir_default_is_outside_the_project(self):
        data = json.loads(self.server.get("/api/settings")[1])
        self.assertNotIn(str(self.root), data["session_dir"])

    # ---------------------------------------------------------- 写

    def test_session_dir_is_saved_and_applied_live(self):
        status, data = self.server.post(
            "/api/settings", {"session_dir": str(self.root.parent / "sess")}
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn("VA_SESSION_DIR", self.read_env())
        self.assertEqual(data["restart"], [], "会话位置不用重启，新会话直接生效")

    def test_empty_session_dir_restores_the_default(self):
        self.server.post("/api/settings", {"session_dir": str(self.root / "custom")})
        self.assertIn(str(self.root / "custom"), self.read_env())

        self.server.post("/api/settings", {"session_dir": ""})
        self.assertIn("VA_SESSION_DIR=", self.read_env())
        live = json.loads(self.server.get("/api/settings")[1])
        self.assertNotIn(str(self.root / "custom"), live["session_dir"])

    def test_unwritable_session_dir_is_rejected_before_writing(self):
        """校验必须在写盘前——写一半再报错会留下一个坏配置。"""
        status, data = self.post_expecting_error({"session_dir": "/proc/nope/cannot"})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertNotIn("VA_SESSION_DIR", self.read_env())

    def test_invalid_read_dir_is_rejected(self):
        status, data = self.post_expecting_error({"read_dirs": ["/definitely/not/here"]})
        self.assertEqual(status, 400)
        self.assertIn("不是有效目录", data["error"])
        self.assertNotIn("VA_READ_DIRS", self.read_env())

    def test_valid_read_dirs_are_saved_but_need_restart(self):
        """工作区已经建好了，改可读目录必须重启才认——不能假装热生效。"""
        status, data = self.server.post("/api/settings", {"read_dirs": [str(self.root)]})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(data["restart"], "应该明确告知需要重启")
        self.assertIn("VA_READ_DIRS", self.read_env())

    def test_read_dirs_use_the_platform_separator(self):
        other = self.root.parent / "extra"
        other.mkdir(exist_ok=True)
        self.server.post("/api/settings", {"read_dirs": [str(self.root), str(other)]})
        self.assertIn(os.pathsep, self.read_env())

    def test_auto_approve_applies_live(self):
        self.server.post("/api/settings", {"auto_approve": True})
        self.assertTrue(self.app.ui.auto_approve)
        self.assertIn("VA_AUTO_APPROVE=1", self.read_env())

    def test_show_thinking_applies_live(self):
        self.server.post("/api/settings", {"show_thinking": False})
        self.assertFalse(self.app.ui.show_thinking)
        self.assertIn("VA_SHOW_THINKING=0", self.read_env())

    def test_vision_model_applies_live(self):
        self.server.post("/api/settings", {"vision_model": "seer"})
        self.assertEqual(self.app.cfg.vision_model, "seer")
        self.assertIn("VA_VISION_MODEL=seer", self.read_env())

    def test_other_env_keys_survive(self):
        """改设置不能把用户自己的 key 冲掉。"""
        self.server.post("/api/settings", {"auto_approve": True})
        self.assertIn("VA_API_KEY=sk-secret", self.read_env())

    def test_changes_publish_an_event(self):
        self.server.post("/api/settings", {"auto_approve": True})
        events = [e for e in self.app.bus.since(0) if e["type"] == "settings"]
        self.assertTrue(events)
        self.assertIn("auto_approve", events[0]["changed"])


class TestSubscriberTracking(AgentTestCase):
    """事件总线的订阅计数。

    它是「浏览器还在不在」的判据——归零意味着审批提示没人看得到，
    这时候还让 worker 干等就是永远卡住。
    """

    def test_counts_up_and_down(self):
        bus = EventBus()
        self.assertEqual(bus.subscribers, 0)
        self.assertEqual(bus.subscribe(), 1)
        self.assertEqual(bus.subscribe(), 2)
        self.assertEqual(bus.unsubscribe(), 1)
        self.assertEqual(bus.unsubscribe(), 0)

    def test_never_goes_negative(self):
        bus = EventBus()
        self.assertEqual(bus.unsubscribe(), 0)
        self.assertEqual(bus.unsubscribe(), 0)


class TestApprovalCancellation(AgentTestCase):
    """浏览器断开时，挂着的审批必须被解掉。

    不解的话 worker 会干等到 30 分钟超时，界面上表现为「一直显示运行中，
    发什么都说上一个任务还在跑」——这正是用户报的现象。
    """

    def test_cancel_all_unblocks_every_waiter(self):
        gate = ApprovalGate(timeout=30)
        results: list[str] = []

        def worker():
            results.append(gate.request(lambda rid: None)[1])

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(3)]
        for t in threads:
            t.start()
        time.sleep(0.3)
        self.assertEqual(gate.waiting, 3)

        self.assertEqual(gate.cancel_all("no"), 3)
        for t in threads:
            t.join(timeout=3)
            self.assertFalse(t.is_alive(), "cancel_all 之后 worker 必须醒过来")
        self.assertEqual(results, ["no", "no", "no"])
        self.assertEqual(gate.waiting, 0)

    def test_cancel_all_with_nothing_waiting(self):
        self.assertEqual(ApprovalGate(timeout=5).cancel_all("no"), 0)

    def test_cancel_uses_the_given_answer(self):
        gate = ApprovalGate(timeout=30)
        box: list[str] = []
        threading.Thread(
            target=lambda: box.append(gate.request(lambda rid: None)[1]), daemon=True
        ).start()
        time.sleep(0.2)
        gate.cancel_all("timeout")
        time.sleep(0.3)
        self.assertEqual(box, ["timeout"])


class TestSSEDisconnectCancelsApprovals(AgentTestCase):
    """走真实 HTTP：最后一个 SSE 连接断开时，待审批要被解掉。"""

    def setUp(self) -> None:
        super().setUp()
        self.app = WebApp(
            self.cfg,
            self.workspace,
            llm=ScriptedLLM(turn("x")),
            registry=build_registry(self.workspace),
        )
        self.server = ServerFixture(self.app)

    def tearDown(self) -> None:
        self.server.stop()
        super().tearDown()

    def test_disconnect_unblocks_a_waiting_approval(self):
        # 手工占一个 SSE 连接
        response = urllib.request.urlopen(
            self.server.url + "/api/events?since=0", timeout=10
        )
        time.sleep(0.5)
        self.assertEqual(self.app.bus.subscribers, 1)

        # 造一个正在等待的审批
        box: list[bool] = []
        threading.Thread(
            target=lambda: box.append(
                self.app.ui.confirm("write_file", {"path": "a"}, "x")
            ),
            daemon=True,
        ).start()
        time.sleep(0.5)
        self.assertEqual(self.app.gate.waiting, 1)

        response.close()  # 浏览器关标签页
        # 服务端要等下一次写（心跳超时）才会发现连接断了，所以这里得等
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and self.app.gate.waiting:
            time.sleep(0.1)

        self.assertEqual(self.app.gate.waiting, 0, "断开后不该还有挂着的审批")
        time.sleep(0.3)
        self.assertEqual(box, [False], "应该按拒绝处理")


class TestSSE(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app = WebApp(
            self.cfg,
            self.workspace,
            llm=ScriptedLLM(turn("回答")),
            registry=build_registry(self.workspace),
        )
        self.server = ServerFixture(self.app)

    def tearDown(self) -> None:
        self.server.stop()
        super().tearDown()

    def _read_events(self, count: int, timeout: float = 6.0) -> list[dict]:
        """读 SSE 流直到攒够 count 个事件。"""
        events: list[dict] = []
        response = urllib.request.urlopen(self.server.url + "/api/events?since=0", timeout=timeout)
        deadline = time.monotonic() + timeout
        try:
            while len(events) < count and time.monotonic() < deadline:
                line = response.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if text.startswith("data: "):
                    events.append(json.loads(text[6:]))
        except (TimeoutError, OSError):
            pass
        finally:
            response.close()
        return events

    def test_stream_delivers_events(self):
        result: list[list[dict]] = []

        def reader():
            result.append(self._read_events(2))

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        time.sleep(0.3)
        self.app.submit("干活")
        thread.join(timeout=8)

        self.assertTrue(result, "没读到任何事件")
        types = [e["type"] for e in result[0]]
        self.assertIn("user", types)
        self.assertTrue(all("seq" in e for e in result[0]))

    def test_heartbeat_keeps_the_connection_alive(self):
        """没有心跳，中间的代理会以为连接死了。"""
        response = urllib.request.urlopen(self.server.url + "/api/events?since=0", timeout=20)
        try:
            # 事件日志里已经有内容，先读完
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if response.readline():
                    continue
                break
        finally:
            response.close()

    def test_reconnect_replays_by_sequence(self):
        self.app.bus.publish("note", level="info", text="一")
        self.app.bus.publish("note", level="info", text="二")
        response = urllib.request.urlopen(
            self.server.url + "/api/events?since=1", timeout=5
        )
        try:
            line = response.readline().decode().strip()
            self.assertTrue(line.startswith("data: "))
            event = json.loads(line[6:])
            self.assertEqual(event["seq"], 2)
            self.assertEqual(event["text"], "二")
        finally:
            response.close()


class TestApprovalOverHTTP(AgentTestCase):
    """走真实 HTTP 的审批：worker 阻塞、POST 解锁。"""

    def setUp(self) -> None:
        super().setUp()
        # 让 agent 请求一次写文件，从而触发审批
        llm = ScriptedLLM(
            turn("我来写", tc("write_file", path="out.txt", content="hello")),
            turn("写好了"),
        )
        self.app = WebApp(
            self.cfg, self.workspace, llm=llm, registry=build_registry(self.workspace)
        )
        self.server = ServerFixture(self.app)

    def tearDown(self) -> None:
        self.server.stop()
        super().tearDown()

    def _wait_for(self, predicate, timeout: float = 8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = [e for e in self.app.bus.since(0) if predicate(e)]
            if found:
                return found[0]
            time.sleep(0.02)
        return None

    def test_reject_prevents_the_write(self):
        self.server.post("/api/message", {"text": "写个文件"})
        approval = self._wait_for(lambda e: e["type"] == "approval")
        self.assertIsNotNone(approval, "审批事件没出现")

        # 关键：此刻 agent 必须真的停住
        self.assertTrue(self.app.busy, "任务不该结束")
        self.assertFalse((self.root / "out.txt").exists(), "还没批准就写了文件！")

        status, data = self.server.post(
            "/api/approve", {"id": approval["id"], "answer": "n"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

        for _ in range(200):
            if not self.app.busy:
                break
            time.sleep(0.02)
        self.assertFalse((self.root / "out.txt").exists(), "拒绝了还是写了")

    def test_approve_lets_the_write_through(self):
        self.server.post("/api/message", {"text": "写个文件"})
        approval = self._wait_for(lambda e: e["type"] == "approval")
        self.assertIsNotNone(approval)

        self.server.post("/api/approve", {"id": approval["id"], "answer": "y"})
        for _ in range(300):
            if not self.app.busy:
                break
            time.sleep(0.02)
        self.assertTrue((self.root / "out.txt").is_file(), "批准了却没写")
        self.assertEqual((self.root / "out.txt").read_text(), "hello")

    def test_approving_an_unknown_id_is_reported(self):
        status, data = self.server.post("/api/approve", {"id": "nope", "answer": "y"})
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
