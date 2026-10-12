"""MCP 客户端（接外部工具的标准协议）。

第一个接入方是 MasterGo（设计稿）：它的 `get_selection_code` 让
「在设计工具里选中一个图层 → agent 直接拿到结构化代码」成为可能。

**写通用层而不是硬编码 MasterGo**：MCP 是标准协议，写一次客户端，
别的 MCP server 都能接。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402
from va.mcp import McpError, McpManager, load_servers, render_content  # noqa: E402

FAKE = str(Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py")


def manager(**overrides) -> McpManager:
    config = {"command": sys.executable, "args": [FAKE], "timeout": 10}
    config.update(overrides)
    return McpManager({"fake": config})


class TestHandshake(unittest.TestCase):
    def setUp(self) -> None:
        self.mgr = manager()
        self.addCleanup(self.mgr.close_all)

    def test_starts_and_lists_tools(self):
        server, error = self.mgr.get("fake")
        self.assertIsNotNone(server, error)
        names = {t.name for t in server.tools}
        self.assertEqual(names, {"echo", "get_selection_code", "slow", "boom"})

    def test_tool_schema_is_kept(self):
        server, _ = self.mgr.get("fake")
        echo = next(t for t in server.tools if t.name == "echo")
        self.assertIn("text", echo.schema.get("properties", {}))
        self.assertEqual(echo.schema.get("required"), ["text"])

    def test_unknown_server_says_what_is_configured(self):
        server, error = self.mgr.get("nope")
        self.assertIsNone(server)
        self.assertIn("fake", error, "要列出配了哪些")

    def test_lists_are_side_effect_free(self):
        """**列工具不该启动 server**——npx 第一次要下载包，可能几十秒。"""
        self.assertEqual(self.mgr.started(), [])
        self.assertEqual(self.mgr.all_tools(), [])
        self.assertEqual(self.mgr.started(), [], "all_tools 把 server 启动了")

    def test_bad_command_reports_clearly(self):
        mgr = McpManager({"x": {"command": "/definitely/not/here"}})
        self.addCleanup(mgr.close_all)
        server, error = mgr.get("x")
        self.assertIsNone(server)
        self.assertIn("起不来", error)


class TestCalling(unittest.TestCase):
    def setUp(self) -> None:
        self.mgr = manager()
        self.addCleanup(self.mgr.close_all)
        self.server, _ = self.mgr.get("fake")

    def test_text_result(self):
        out = render_content(self.server.call_tool("echo", {"text": "你好"}))
        self.assertIn("你好", out)

    def test_multi_block_content(self):
        """MCP 的返回可以是文本 + 图片 + 资源。图片给个占位说明，
        别让调用方以为「什么都没返回」。"""
        out = render_content(self.server.call_tool("get_selection_code", {}))
        self.assertIn("设计稿代码", out)
        self.assertIn("图片", out)

    def test_server_error_becomes_a_readable_message(self):
        with self.assertRaises(McpError) as ctx:
            self.server.call_tool("boom", {})
        self.assertIn("设计稿没打开", str(ctx.exception))

    def test_timeout(self):
        with self.assertRaises(McpError) as ctx:
            self.server.call_tool("slow", {}, timeout=1)
        self.assertIn("超时", str(ctx.exception))

    def test_stderr_is_captured(self):
        """连不上时日志是唯一的线索。"""
        self.assertIn("客户端已就绪", "\n".join(self.server.logs))

    def test_notifications_do_not_confuse_responses(self):
        """**响应和通知靠 id 配对，不靠顺序。**

        服务端可以随时插进来一条通知。按顺序配对会把通知当成响应——
        这条测试盯住那个 bug。
        """
        # fake server 在 initialized 之后主动发了一条通知
        self.assertTrue(any("通知" in line for line in self.server.logs))
        # 之后调用仍然正常
        self.assertIn("你好", render_content(self.server.call_tool("echo", {"text": "你好"})))


class TestLifecycle(unittest.TestCase):
    def test_close_stops_the_process(self):
        mgr = manager()
        server, _ = mgr.get("fake")
        self.assertTrue(server.running)
        mgr.close_all()
        self.assertFalse(server.running)

    def test_restart_after_close(self):
        mgr = manager()
        server, _ = mgr.get("fake")
        mgr.close_all()
        again, error = mgr.get("fake")
        self.assertIsNotNone(again, error)
        self.assertTrue(again.running)
        mgr.close_all()


class TestConfigLoading(AgentTestCase):
    def test_servers_come_from_the_workflow(self):
        """配置写在工作流定义里——它也是团队约定的一部分
        （「我们团队用 MasterGo」），而且天然享受「项目级覆盖用户级」。"""
        from va.workflow import load

        config = self.root / ".vibe" / "workflow.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            "mcp:\n  servers:\n    mastergo:\n      command: npx\n"
            "      args: ['-y', '@mastergo/vibe-mcp@latest']\n"
        )
        servers = load_servers(load(self.root))
        self.assertIn("mastergo", servers)
        self.assertEqual(servers["mastergo"]["command"], "npx")

    def test_entries_without_a_command_are_skipped(self):
        class Fake:
            def get(self, key, default=None):
                return {"broken": {"args": ["x"]}, "ok": {"command": "npx"}}

        self.assertEqual(list(load_servers(Fake())), ["ok"])

    def test_empty_config(self):
        class Fake:
            def get(self, key, default=None):
                return None

        self.assertEqual(load_servers(Fake()), {})


class TestToolsLayer(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        from va.loop import Agent
        from va.ui import UI

        self.agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)
        self.addCleanup(self.agent.mcp.close_all)

    def test_registered(self):
        for name in ("mcp_servers", "mcp_tools", "mcp_call"):
            self.assertIsNotNone(self.agent.registry.get(name), f"{name} 没注册")

    def test_listing_without_config_explains_how(self):
        out = self.agent.registry.get("mcp_servers").run()
        self.assertIn("没有配置", out)
        self.assertIn("workflow.yaml", out, "要告诉用户在哪配")

    def test_approval_defaults_to_on(self):
        """MCP 工具能改外部系统（MasterGo 会改设计画布），默认要审批。"""
        tool = self.agent.registry.get("mcp_call")
        self.assertTrue(tool.needs_approval({"tool": "echo"}))

    def test_approval_policy_read(self):
        """read 模式：只对看起来是写操作的要审批。"""
        tool = self.agent.registry.get("mcp_call")
        self.agent.workflow.data.setdefault("mcp", {})["approve"] = "read"
        self.assertFalse(tool.needs_approval({"tool": "get_selection_code"}))
        self.assertTrue(tool.needs_approval({"tool": "agent_update_node"}))
        self.assertTrue(tool.needs_approval({"tool": "agent_sync_design"}))

    def test_approval_policy_none(self):
        tool = self.agent.registry.get("mcp_call")
        self.agent.workflow.data.setdefault("mcp", {})["approve"] = "none"
        self.assertFalse(tool.needs_approval({"tool": "agent_update_node"}))

    def test_policy_reaches_the_tool_through_the_context(self):
        """**踩过的坑**：workflow 挂在了 Agent 上，而工具读的是 ctx.workflow——
        拿不到就永远回落到 all，配置形同虚设。"""
        self.assertIs(self.agent.agent_ctx.workflow, self.agent.workflow)


if __name__ == "__main__":
    unittest.main(verbosity=2)
