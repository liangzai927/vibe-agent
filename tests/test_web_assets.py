"""前端资产的测试。

这一组是补出来的，起因是一个真实的故障：我用字符串替换改了设置面板的
HTML，删掉了一个按钮，但 JS 里还在 `$("那个按钮").addEventListener(...)`。
于是顶层脚本抛 `Cannot read properties of null`，**后面的 `loadModels()`
和 `connect()` 全都没执行**。

后果是：模型下拉框空的、SSE 不连、发消息没反应、状态永远卡在「运行中」。
看起来像「LLM 连不上」，实际是前端一行都没跑起来。

单元测试抓不到这个——它需要**真的在浏览器里跑一遍**。
所以这里有两层：静态的 id 交叉检查（快、永远跑），
和真的开浏览器加载页面（慢，但能抓住运行期的问题）。
"""

from __future__ import annotations

import re
import shutil
import socket
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase, ScriptedLLM, turn  # noqa: E402
from va.tools import build_registry  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "va" / "web" / "static" / "index.html"


def read_index() -> str:
    return INDEX.read_text(encoding="utf-8")


def script_body(html: str) -> str:
    match = re.search(r"<script>(.*?)</script>", html, re.S)
    return match.group(1) if match else ""


class TestAssetIntegrity(unittest.TestCase):
    """静态检查：不依赖浏览器，永远能跑。"""

    def setUp(self) -> None:
        self.html = read_index()
        self.script = script_body(self.html)

    def test_has_exactly_one_script_block(self):
        self.assertEqual(self.html.count("<script>"), 1)
        self.assertEqual(self.html.count("</script>"), 1)
        self.assertTrue(self.script.strip(), "script 块是空的")

    def test_no_cdn_references(self):
        """必须是离线可用的——部署到没有外网的环境时不该白屏。"""
        for pattern in ("http://", "https://", "//cdn", "integrity="):
            for line in self.html.splitlines():
                stripped = line.strip()
                if stripped.startswith("//") or "http://www.w3.org" in stripped:
                    continue  # 注释和 SVG 命名空间
                if pattern in stripped and "<a " not in stripped:
                    self.fail(f"出现了外部资源引用：{stripped[:100]}")

    def test_script_syntax_is_valid(self):
        """用 node 检查语法（有 node 就检查，没有就跳过）。"""
        node = shutil.which("node")
        if not node:
            self.skipTest("没有 node，跳过语法检查")
        import subprocess
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(self.script)
            path = handle.name
        try:
            result = subprocess.run(
                [node, "--check", path], capture_output=True, text=True, timeout=30
            )
            self.assertEqual(result.returncode, 0, result.stderr[:500])
        finally:
            Path(path).unlink(missing_ok=True)

    def test_every_referenced_id_exists(self):
        """`$("x")` 里的 x 必须在 HTML 里有对应的 id。

        这条就是那个故障的直接守卫：JS 引用了一个已经不存在的按钮，
        顶层脚本一抛异常，后面所有初始化都不执行。
        """
        defined = set(re.findall(r'\bid="([^"]+)"', self.html))
        referenced = set(re.findall(r'\$\("([^"]+)"\)', self.script))

        missing = sorted(referenced - defined)
        self.assertEqual(
            missing,
            [],
            f"JS 引用了 HTML 里不存在的元素：{missing}\n"
            f"这会让顶层脚本抛 null 异常，后面的初始化全部不执行。",
        )

    def test_ids_are_unique(self):
        ids = re.findall(r'\bid="([^"]+)"', self.html)
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        self.assertEqual(duplicates, [], f"重复的 id：{duplicates}")

    def test_startup_calls_are_at_the_end(self):
        """初始化必须在脚本最后——放中间的话，前面抛异常就永远跑不到。"""
        tail = self.script[-400:]
        self.assertIn("connect()", tail)
        self.assertIn("loadModels()", tail)

    def test_event_types_the_backend_sends_are_handled(self):
        """后端发的每种事件，前端都要有分支——否则消息会静默丢掉。"""
        from va.web.bridge import WebUI  # noqa: F401

        # 后端会发的类型（人工维护，和 bridge/server 里的 publish 对应）
        backend_types = {
            "banner", "step", "text_delta", "thinking_delta", "message_end",
            "note", "tool_call", "tool_result", "diff", "plan", "block",
            "rule", "answer", "approval", "approval_done", "user",
            "task_end", "model", "settings",
        }
        handled = set(re.findall(r'case "([a-z_]+)":', self.script))
        unhandled = sorted(backend_types - handled)
        self.assertEqual(
            unhandled,
            [],
            f"这些事件前端没有处理分支：{unhandled}——用户会看到「什么都没发生」",
        )


class TestPageRunsInABrowser(AgentTestCase):
    """真的开浏览器加载页面。

    静态检查只能发现「id 不存在」这类问题；运行期的异常、请求没发出去、
    SSE 连不上，只有真跑一遍才知道。
    """

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("没有 playwright")
        if shutil.which("google-chrome") is None:
            # macOS 上 Chrome 装在 app bundle 里，which 找不到——用 channel 兜底
            pass

    def setUp(self) -> None:
        super().setUp()
        from va.config import Config
        from va.web.server import Handler, WebApp
        from http.server import ThreadingHTTPServer

        self.app = WebApp(
            Config(workspace=self.root, model="m", api_key="k", base_url="http://x"),
            self.workspace,
            llm=ScriptedLLM(turn("好了")),
            registry=build_registry(self.workspace),
        )
        Handler.app = self.app

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def _browser(self):
        from playwright.sync_api import sync_playwright

        return sync_playwright()

    def test_page_loads_without_js_errors(self):
        errors: list[str] = []
        with self._browser() as p:
            browser = p.chromium.launch(channel="chrome")
            page = browser.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            try:
                page.goto(self.url + "/", wait_until="domcontentloaded")
                time.sleep(1.5)
            finally:
                browser.close()

        self.assertEqual(
            errors,
            [],
            "页面加载时抛了未捕获异常——顶层脚本一挂，后面的初始化全不执行",
        )

    def test_startup_actually_fetches_models_and_opens_sse(self):
        """这两个请求是「前端活过来了」的证据。

        少了它们，界面看起来正常，但发消息没反应、下拉框是空的。
        """
        seen: list[str] = []
        with self._browser() as p:
            browser = p.chromium.launch(channel="chrome")
            page = browser.new_page()
            page.on(
                "request",
                lambda r: seen.append(r.url.split("?")[0].replace(self.url, "") or "/"),
            )
            try:
                page.goto(self.url + "/", wait_until="domcontentloaded")
                time.sleep(2.0)
                options = page.eval_on_selector("#model", "e => e.options.length")
            finally:
                browser.close()

        self.assertIn("/api/events", seen, "SSE 没连上——消息永远不会显示")
        self.assertIn("/api/models", seen, "没拉模型列表——下拉框会是空的")
        self.assertGreater(options, 0, "下拉框没有选项")

    def test_sending_a_message_shows_up_in_the_transcript(self):
        with self._browser() as p:
            browser = p.chromium.launch(channel="chrome")
            page = browser.new_page()
            try:
                page.goto(self.url + "/", wait_until="domcontentloaded")
                time.sleep(1.5)
                page.fill("#input", "打个招呼")
                page.click("#send")

                shown = False
                for _ in range(60):
                    if "打个招呼" in page.inner_text("#stream"):
                        shown = True
                        break
                    time.sleep(0.5)
            finally:
                browser.close()

        self.assertTrue(shown, "发出去的消息没有出现在对话区——SSE 事件到不了前端")


if __name__ == "__main__":
    unittest.main(verbosity=2)
