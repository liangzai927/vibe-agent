"""MCP 客户端。

MCP（Model Context Protocol）是「让 agent 用外部工具」的标准协议。
接一个 MCP server，就接上了它背后的一整套能力。

**第一个接入方是 MasterGo**（设计稿）：它的 `get_selection_code` 让
「你在设计工具里选中一个图层 → agent 直接拿到结构化代码」成为可能，
不用导出图片、不用传 URL。

────────────────────────────────────────────────────────────────

**为什么值得写成一个通用层**：MCP 是标准协议，写一次客户端，
Figma / 蓝湖 / 内部平台 / 任何别的 MCP server 都能接。
把 MasterGo 的特例硬编码进去，第二个接入方就要重写一遍。

**stdin/stdout 上的 JSON-RPC，一行一条消息**：

    客户端 → {"jsonrpc":"2.0","id":1,"method":"initialize",...}\\n
    服务端 → {"jsonrpc":"2.0","id":1,"result":{...}}\\n

**必须有读线程**：服务端不只会应答，还会主动发通知（日志、进度）。
不持续读的话管道会堵，而且应答和通知会错位——请求和响应靠 `id` 配对，
不靠顺序，所以只能异步收。

**MCP server 是子进程**：会话结束要清理，否则 npx 起的 node 进程会攒下来。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 协议版本。服务端会回它支持的版本，不一致时以服务端为准。
PROTOCOL_VERSION = "2024-11-05"

#: 单次请求默认超时。设计稿解析可能慢，给得比一般工具宽。
DEFAULT_TIMEOUT = 60.0

#: 握手超时。npx 第一次要下载包，可能要几十秒。
START_TIMEOUT = 120.0

#: 保留多少行服务端日志（stderr），排查连接问题用。
LOG_LINES = 200


class McpError(Exception):
    """MCP 层的错误。"""


@dataclass
class McpTool:
    server: str
    name: str
    description: str = ""
    schema: dict[str, Any] = field(default_factory=dict)

    @property
    def qualified(self) -> str:
        return f"{self.server}.{self.name}"

    def describe(self) -> str:
        first = (self.description or "").splitlines()[0] if self.description else ""
        return f"{self.qualified}: {first[:100]}"


class McpServer:
    """一个 MCP server 子进程。"""

    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        *,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.name = name
        self.command = command
        self.args = list(args or [])
        self.extra_env = dict(env or {})
        self.cwd = Path(cwd) if cwd else None
        self.timeout = timeout

        self.process: subprocess.Popen | None = None
        self.tools: list[McpTool] = []
        self.server_info: dict[str, Any] = {}
        self.logs: list[str] = []
        self.last_error = ""

        self._pending: dict[int, dict[str, Any]] = {}
        self._next_id = 0
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._reader: threading.Thread | None = None
        self._closed = False

    # ---- 生命周期

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, *, timeout: float = START_TIMEOUT) -> tuple[bool, str]:
        """启动并握手。返回 ``(成功吗, 说明)``。"""
        if self.running:
            return True, "已在运行"

        merged = {**os.environ, **self.extra_env}
        # npx 第一次要下载包，别让它弹交互提示
        merged.setdefault("npm_config_yes", "true")
        merged.setdefault("NO_COLOR", "1")

        try:
            self.process = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=str(self.cwd) if self.cwd else None,
                env=merged,
                start_new_session=True,   # 独立进程组，能整组收拾干净
            )
        except OSError as exc:
            self.last_error = f"启动不了 {self.command}：{exc}"
            return False, self.last_error

        self._closed = False
        self._reader = threading.Thread(
            target=self._read_loop, name=f"mcp-{self.name}", daemon=True
        )
        self._reader.start()
        threading.Thread(
            target=self._log_loop, name=f"mcp-log-{self.name}", daemon=True
        ).start()

        try:
            result = self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "vibe-agent", "version": "0.1.0"},
                },
                timeout=timeout,
            )
        except McpError as exc:
            self.last_error = str(exc)
            self.close()
            return False, self.last_error

        self.server_info = result or {}
        # 握手最后一步：告诉服务端「我准备好了」。这是**通知**，不要 id。
        self._notify("notifications/initialized", {})

        try:
            self.tools = self._load_tools()
        except McpError as exc:
            self.last_error = f"列不出工具：{exc}"
            return False, self.last_error

        return True, f"已连接，{len(self.tools)} 个工具"

    def close(self) -> None:
        self._closed = True
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # npx 起的是 node，会派生子进程——只杀父进程会留孤儿
                    import signal as _signal

                    try:
                        os.killpg(os.getpgid(process.pid), _signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        process.kill()
        except OSError:
            pass
        self.process = None

    # ---- 读写循环

    def _read_loop(self) -> None:
        stream = self.process.stdout if self.process else None
        if stream is None:
            return
        try:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    # 有些 server 会往 stdout 混日志。留着当线索，别崩。
                    self._log(f"（非 JSON 输出）{line[:200]}")
                    continue
                self._dispatch(message)
        except (OSError, ValueError):
            pass
        finally:
            # 进程没了：把所有等待中的请求都叫醒，别让调用方干等
            self._fail_pending("MCP server 已退出")

    def _log_loop(self) -> None:
        stream = self.process.stderr if self.process else None
        if stream is None:
            return
        try:
            for line in stream:
                self._log(line.rstrip())
        except (OSError, ValueError):
            pass

    def _log(self, line: str) -> None:
        self.logs.append(line)
        if len(self.logs) > LOG_LINES:
            del self.logs[: len(self.logs) - LOG_LINES]

    def _dispatch(self, message: dict[str, Any]) -> None:
        """按 id 把响应交给等待者；通知先记下来。

        **不能按顺序配对**：服务端可能在任何时候插进来一条通知，
        顺序配位会把通知当成响应。
        """
        if "id" in message and message["id"] is not None:
            with self._lock:
                waiter = self._pending.pop(message["id"], None)
            if waiter is None:
                return
            with waiter["lock"]:
                waiter["message"] = message
                waiter["done"] = True
                waiter["event"].set()
            return

        method = message.get("method")
        if method:
            self._log(f"（通知）{method}")

    # ---- 请求

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, payload: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            raise McpError(f"{self.name} 的进程已经不在了")
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise McpError(f"往 {self.name} 写数据失败：{exc}") from exc

    def _fail_pending(self, reason: str) -> None:
        with self._lock:
            waiters = list(self._pending.values())
            self._pending.clear()
        for waiter in waiters:
            with waiter["lock"]:
                waiter["error"] = reason
                waiter["done"] = True
                waiter["event"].set()

    def _request(
        self, method: str, params: dict[str, Any], *, timeout: float | None = None
    ) -> Any:
        waiter = {
            "event": threading.Event(),
            "done": False,
            "message": None,
            "error": "",
            "lock": threading.Lock(),
        }
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._pending[request_id] = waiter

        self._write(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )

        limit = timeout or self.timeout
        if not waiter["event"].wait(limit):
            with self._lock:
                self._pending.pop(request_id, None)
            raise McpError(f"{method} 超时（{limit:.0f}s）")

        if waiter["error"]:
            raise McpError(waiter["error"])

        message = waiter["message"] or {}
        if "error" in message:
            error = message["error"] or {}
            raise McpError(
                f"{method} 出错：{error.get('message') or error}"
            )
        return message.get("result")

    # ---- 工具

    def _load_tools(self) -> list[McpTool]:
        result = self._request("tools/list", {})
        items = (result or {}).get("tools") or []
        tools: list[McpTool] = []
        for item in items:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            tools.append(
                McpTool(
                    server=self.name,
                    name=str(item["name"]),
                    description=str(item.get("description") or ""),
                    schema=item.get("inputSchema") or {},
                )
            )
        return tools

    def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        """调一个工具。返回原始 result（里面是 content 数组）。"""
        if not self.running:
            ok, detail = self.start()
            if not ok:
                raise McpError(detail)

        result = self._request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            timeout=timeout,
        )
        return result or {}


def render_content(result: dict[str, Any]) -> str:
    """把 MCP 的 content 数组拼成文本。

    MCP 的返回可以是文本、图片、资源引用。图片这类先给个占位说明，
    别让调用方以为「什么都没返回」。
    """
    blocks = result.get("content") or []
    out: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            out.append(str(block))
            continue
        kind = block.get("type")
        if kind == "text":
            out.append(str(block.get("text") or ""))
        elif kind == "image":
            mime = block.get("mimeType") or "image"
            out.append(f"[图片内容：{mime}，已省略]")
        elif kind == "resource":
            resource = block.get("resource") or {}
            out.append(f"[资源：{resource.get('uri') or '未知'}]")
        else:
            out.append(f"[{kind} 内容]")
    if not out:
        return json.dumps(result, ensure_ascii=False)[:2000]
    return "\n".join(out)


class McpManager:
    """一个会话的全部 MCP server。"""

    def __init__(self, servers: dict[str, dict[str, Any]] | None = None) -> None:
        self._configs = dict(servers or {})
        self._servers: dict[str, McpServer] = {}
        self._lock = threading.Lock()
        self._error = ""

    @property
    def configured(self) -> bool:
        return bool(self._configs)

    def get(self, name: str, *, start: bool = True) -> tuple[McpServer | None, str]:
        """拿一个 server（必要时启动）。返回 ``(server, 说明)``。"""
        name = (name or "").strip()
        config = self._configs.get(name)
        if config is None:
            known = "、".join(self._configs) or "（一个都没配）"
            return None, f"没有名为 {name!r} 的 MCP server。已配的：{known}"

        with self._lock:
            server = self._servers.get(name)
            if server is None:
                server = McpServer(
                    name=name,
                    command=str(config.get("command") or ""),
                    args=list(config.get("args") or []),
                    env=config.get("env") or {},
                    timeout=float(config.get("timeout") or DEFAULT_TIMEOUT),
                )
                self._servers[name] = server

        if start and not server.running:
            ok, detail = server.start()
            if not ok:
                self._error = detail
                return None, f"{name} 起不来：{detail}"
        return server, ""

    def all_tools(self) -> list[McpTool]:
        """把所有已启动 server 的工具列出来。**不会主动启动**——
        列工具不该有副作用（npx 第一次要下载，可能几十秒）。"""
        tools: list[McpTool] = []
        for server in self._servers.values():
            tools.extend(server.tools)
        return tools

    def registered(self) -> list[str]:
        return sorted(self._configs)

    def started(self) -> list[str]:
        return sorted(name for name, s in self._servers.items() if s.running)

    def close_all(self) -> None:
        for server in self._servers.values():
            server.close()


def load_servers(workflow) -> dict[str, dict[str, Any]]:
    """从工作流定义里读 MCP server 配置。

    放在 workflow.yaml 里而不是单独的 mcp.json：**它也是团队约定**
    （「我们团队用 MasterGo」），而且能自然享受「项目级覆盖用户级」。
    """
    servers = workflow.get("mcp.servers") or {}
    if not isinstance(servers, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, config in servers.items():
        if isinstance(config, dict) and config.get("command"):
            out[str(name)] = config
    return out
