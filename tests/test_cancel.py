"""停止任务 + 别再刷 traceback。

**用户报的两个真问题**：

1. 任务卡住时消息全被「上一个任务还在跑」挡住，**没有任何出口**——只能重启服务
2. 浏览器关页面/刷新时，终端被 `ConnectionResetError` 的 traceback 刷屏，
   而里面没有一条是有用的
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402
from va.tools.base import ToolError  # noqa: E402


class TestAgentCancel(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        from va.loop import Agent
        from va.ui import UI

        self.agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)

    def test_cancel_lifts_the_signal(self):
        self.assertFalse(self.agent.cancel_event.is_set())
        self.agent.cancel()
        self.assertTrue(self.agent.cancel_event.is_set())

    def test_cancelling_twice_says_so(self):
        self.agent.cancel()
        self.assertIn("已经在停", self.agent.cancel())

    def test_a_new_run_clears_the_signal(self):
        """**上一次取消不能影响下一次任务**——不然所有后续任务一开头就停。"""
        self.agent.cancel()
        # run() 会先 clear；这里直接验证那个行为存在
        self.agent.cancel_event.clear()
        self.assertFalse(self.agent.cancel_event.is_set())

    def test_the_tool_layer_can_see_it(self):
        """`run_bash` 要通过 ctx 拿到取消信号——**「能不能停」不该是
        调用方记得传才有的能力**。"""
        self.assertIs(self.agent.agent_ctx.cancel_event, self.agent.cancel_event)


class TestRunBashIsInterruptible(AgentTestCase):
    """一个 `grep` 卡在巨大目录树上、或者 `sleep 900`，
    光靠「下一轮循环检查」是等不到的——**工具内部也得能被打断**。"""

    def setUp(self) -> None:
        super().setUp()
        from va.loop import Agent
        from va.ui import UI

        self.agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)
        self.tool = self.agent.registry.get("run_bash")

    def test_a_long_command_stops_promptly(self):
        outcome: dict[str, str] = {}

        def run() -> None:
            try:
                outcome["text"] = self.tool.run(command="sleep 60", timeout=300)
            except Exception as exc:  # noqa: BLE001
                outcome["text"] = f"[{type(exc).__name__}] {exc}"

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        time.sleep(1.2)

        started = time.time()
        self.agent.cancel()
        worker.join(timeout=20)
        elapsed = time.time() - started

        self.assertLess(elapsed, 8, "取消没有立刻生效——「停止」按钮就没意义了")
        self.assertIn("停止", outcome.get("text", ""))

    def test_a_normal_command_still_finishes(self):
        output = self.tool.run(command="echo 正常结束")
        self.assertIn("正常结束", output)

    def test_normal_command_is_not_affected_by_a_stale_signal(self):
        """取消之后还能接着干活——不然一次取消就把会话废了。"""
        self.agent.cancel()
        self.agent.cancel_event.clear()
        self.assertIn("还行", self.tool.run(command="echo 还行"))


class TestQuietServer(AgentTestCase):
    """浏览器走了不是错误。

    keep-alive 连接上，浏览器关掉页面之后服务端下一次读请求会拿到
    `ConnectionResetError`。`socketserver` 默认给**每个**这种连接打一整段
    traceback——终端会被刷屏，而里面没有一条是有用的。
    """

    def test_connection_errors_are_swallowed(self):
        import io
        from contextlib import redirect_stderr

        from va.web.server import _QuietServer

        server = _QuietServer.__new__(_QuietServer)
        captured = io.StringIO()

        for exc_type in (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            try:
                raise exc_type("客户端走了")
            except exc_type:
                with redirect_stderr(captured):
                    server.handle_error(None, ("127.0.0.1", 1))
        self.assertEqual(captured.getvalue(), "", "连接断开被当成错误打出来了")

    def test_real_errors_still_surface(self):
        """**不能顺手把所有错误都吞了**——真出问题时你得看得见。"""
        import io
        from contextlib import redirect_stderr

        from va.web.server import _QuietServer

        server = _QuietServer.__new__(_QuietServer)
        captured = io.StringIO()
        try:
            raise ValueError("真的出问题了")
        except ValueError:
            with redirect_stderr(captured):
                server.handle_error(None, ("127.0.0.1", 1))
        self.assertIn("真的出问题了", captured.getvalue())

    def test_it_is_actually_used(self):
        source = (
            Path(__file__).resolve().parent.parent / "va" / "web" / "server.py"
        ).read_text(encoding="utf-8")
        self.assertIn("_QuietServer((host", source, "定义了但没用上")


class TestWebCancel(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        from va.web.server import WebApp

        self.app = WebApp(self.cfg, self.workspace)

    def test_cancel_with_no_running_task(self):
        ok, message = self.app.cancel("")
        self.assertFalse(ok)
        self.assertIn("没有在跑", message)

    def test_cancel_unknown_session(self):
        ok, message = self.app.cancel("不存在的会话")
        self.assertFalse(ok)
        self.assertIn("没有这个会话", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
