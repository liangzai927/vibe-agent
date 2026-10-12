"""MCP 工具层：让 agent 用上外部 MCP 服务。

三个工具：

    mcp_servers   有哪些 MCP server，起来没有，有多少工具
    mcp_tools     看某个 server 提供了哪些工具（含参数说明）
    mcp_call      调一个工具

**为什么不做成「每个 MCP 工具注册成一个原生工具」**

那需要**注册时就启动所有 server**。而 npx 第一次要下载包（几十秒），
每个会话启动都等一遍不可接受；server 没起来时（MasterGo 客户端没开）
整个 registry 还会构建失败。

代价是模型要先 `mcp_tools` 看一眼再 `mcp_call`——多一次往返，
但只在第一次用某个 server 时。换来的是「没配 MCP 的会话零开销」。

**审批**：MCP 工具能改外部系统（MasterGo 的 `agent_update_node` 会改设计画布），
所以默认要审批。可以在 workflow.yaml 里按 server 放宽。
"""

from __future__ import annotations

from typing import Any

from ..mcp import McpError, render_content
from ..util import truncate
from .base import ToolError
from .context import AgentContext, WorkspaceTool

#: 名字里带这些词的当成写操作。「read 模式」审批时用它区分。
_WRITE_HINTS = (
    "update", "create", "delete", "remove", "set", "sync", "write",
    "modify", "insert", "add", "replace", "apply", "generate", "design",
)


class McpTool(WorkspaceTool):
    def __init__(self, ctx: AgentContext) -> None:
        super().__init__(ctx.workspace, ctx.cfg)
        self.ctx = ctx

    @property
    def manager(self):
        manager = self.ctx.mcp
        if manager is None:
            raise ToolError("这个会话没有 MCP 管理器")
        return manager

    def _server(self, name: str):
        server, error = self.manager.get(name)
        if server is None:
            raise ToolError(error)
        if error:
            # 起不来时把 stderr 带上——那才是排查线索
            logs = "\n".join(server.logs[-15:])
            raise ToolError(f"{error}\n{server.name} 的输出：\n{logs}" if logs else error)
        return server


class McpServers(McpTool):
    name = "mcp_servers"
    description = (
        "列出配置了哪些 MCP 服务器、起来没有、各有多少工具。\n"
        "MCP 是外部工具协议——比如 MasterGo 的设计稿能力就是通过它接进来的。\n"
        "**不会主动启动**：启动可能要下载包（几十秒），想知道工具就用 mcp_tools。"
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **_: Any) -> str:
        names = self.manager.registered()
        if not names:
            return (
                "没有配置任何 MCP 服务器。\n"
                "配置写在 .vibe/workflow.yaml 的 mcp.servers 下，例如：\n"
                "  mcp:\n"
                "    servers:\n"
                "      mastergo:\n"
                "        command: npx\n"
                '        args: ["-y", "@mastergo/vibe-mcp@latest", "--url=http://localhost:30678"]'
            )

        started = set(self.manager.started())
        lines = [f"配置了 {len(names)} 个 MCP 服务器：", ""]
        for name in names:
            if name in started:
                server = self.manager.get(name)[0]
                lines.append(f"  ● {name}  [已连接]  {len(server.tools)} 个工具")
            else:
                lines.append(f"  ○ {name}  [未启动]")
        lines.append("")
        lines.append("用 mcp_tools(server=名字) 看它有哪些工具。")
        return "\n".join(lines)


class McpTools(McpTool):
    name = "mcp_tools"
    description = (
        "看某个 MCP 服务器提供了哪些工具（含参数说明）。\n"
        "第一次用某个 server 之前先调它——**server 没起来会自动启动**，"
        "所以第一次可能等几十秒（npx 要下载包）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "server": {"type": "string", "description": "服务器名字，例如 mastergo"},
        },
        "required": ["server"],
    }

    def run(self, server: str, **_: Any) -> str:
        target = self._server(server)
        if not target.tools:
            return f"{server} 没有提供任何工具。"

        lines = [f"{server} 提供 {len(target.tools)} 个工具：", ""]
        for tool in target.tools:
            lines.append(f"  {tool.name}")
            if tool.description:
                for line in tool.description.strip().splitlines()[:4]:
                    lines.append(f"      {line.strip()[:110]}")
            props = (tool.schema or {}).get("properties") or {}
            required = set((tool.schema or {}).get("required") or [])
            if props:
                params = []
                for key, spec in props.items():
                    mark = "*" if key in required else ""
                    kind = (spec or {}).get("type", "?")
                    params.append(f"{key}{mark}:{kind}")
                lines.append(f"      参数：{', '.join(params)}")
            lines.append("")
        lines.append("用 mcp_call(server=..., tool=..., arguments={...}) 调用。")
        return truncate("\n".join(lines), limit=6000, label=f"{server} 的工具")


class McpCall(McpTool):
    name = "mcp_call"
    description = (
        "调用一个 MCP 工具。\n"
        "server 和 tool 的名字用 mcp_tools 查。arguments 按那个工具的参数说明给。\n"
        "**有的 MCP 工具会改外部系统**（比如修改设计画布），这类会先问你。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "server": {"type": "string", "description": "服务器名字"},
            "tool": {"type": "string", "description": "工具名字"},
            "arguments": {
                "type": "object",
                "description": "工具参数。按 mcp_tools 里那个工具的说明填",
            },
            "timeout": {
                "type": "integer",
                "description": "超时秒数。设计稿解析可能慢，默认 60",
            },
        },
        "required": ["server", "tool"],
    }

    def needs_approval(self, args: dict[str, Any]) -> bool:
        """按配置决定要不要审批。

        MCP 工具能改外部系统——MasterGo 的 `agent_update_node` 会直接改
        设计画布，那种操作不该悄悄发生。
        """
        workflow = getattr(self.ctx, "workflow", None)
        mode = str(workflow.get("mcp.approve", "all")) if workflow is not None else "all"

        if mode == "none":
            return False
        if mode == "read":
            tool = str(args.get("tool") or "").lower()
            return any(hint in tool for hint in _WRITE_HINTS)
        return True

    def run(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any] | None = None,
        timeout: int = 0,
        **_: Any,
    ) -> str:
        target = self._server(server)
        kwargs = {"timeout": float(timeout)} if timeout else {}

        try:
            result = target.call_tool(tool, arguments or {}, **kwargs)
        except McpError as exc:
            logs = "\n".join(target.logs[-8:])
            hint = f"\n{server} 最近的日志：\n{logs}" if logs else ""
            raise ToolError(f"调用 {server}.{tool} 失败：{exc}{hint}") from exc

        body = render_content(result)
        head = f"{server}.{tool}"
        if result.get("isError"):
            raise ToolError(f"{head} 报告了错误：\n{truncate(body, limit=4000)}")
        return f"{head} 返回：\n{truncate(body, limit=8000, label=head)}"


MCP_TOOLS = (McpServers, McpTools, McpCall)
