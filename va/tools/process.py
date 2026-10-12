"""后台进程工具。

给 agent 一套「把项目跑起来」的能力，补上 `run_bash` 够不到的场景：
`run_bash` 是「跑完才返回」，而 dev server 永远不结束。

五个工具对应五个动作：启动、读日志、等就绪、停止、查看。

**为什么不允许在 `run_bash` 里加个 `background=true`**：
后台进程需要**跨工具调用存活**——启动之后要在后续多次调用里读它、等它、停它。
那套状态必须有个明确的归属（会话），而不是藏在某次调用的返回值里。
"""

from __future__ import annotations

from typing import Any

from ..process import DEFAULT_READY_TIMEOUT, ProcessManager
from ..util import truncate
from .base import ToolError
from .context import AgentContext, WorkspaceTool


class ProcessTool(WorkspaceTool):
    """后台进程工具的公共部分。"""

    def __init__(self, ctx: AgentContext) -> None:
        super().__init__(ctx.workspace, ctx.cfg)
        self.ctx = ctx

    @property
    def manager(self) -> ProcessManager:
        manager = self.ctx.processes
        if manager is None:
            raise ToolError(
                "这个会话没有后台进程管理器（可能是子 agent 或旧版本的调用路径）"
            )
        return manager

    def _resolve_cwd(self, cwd: str) -> "Any":
        """相对工作区解析执行目录。和 run_bash 一个口径。"""
        target = self.workspace.resolve(cwd or ".", must_exist=True)
        if not target.is_dir():
            raise ToolError(f"cwd 不是目录：{cwd}")
        return target


class StartProcess(ProcessTool):
    name = "start_process"
    description = (
        "在后台启动一个长驻进程（dev server、mock 服务、后端等），立刻返回。\n"
        "**和 run_bash 的区别**：run_bash 会一直等到命令结束，起 dev server 会挂住；\n"
        "这个立刻返回一个 handle，之后用 read_output / wait_for_port 跟进。\n"
        "启动后记得用 wait_for_port 等它真的就绪，别直接假设已经好了。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要启动的命令，例如 npm run dev"},
            "label": {
                "type": "string",
                "description": "给它起个好认的名字，例如 web / api。省略则用命令的第一个词",
            },
            "cwd": {
                "type": "string",
                "description": "相对工作区的执行目录（默认工作区根目录）",
            },
            "expect_port": {
                "type": "integer",
                "description": (
                    "期望它监听的端口。给了的话会**顺便等它就绪**再返回，"
                    "省掉单独一次 wait_for_port。启动失败会带上真实日志"
                ),
            },
            "timeout": {
                "type": "integer",
                "description": f"配合 expect_port：就绪探测最长等多少秒（默认 {DEFAULT_READY_TIMEOUT}）",
            },
        },
        "required": ["command"],
    }

    def run(
        self,
        command: str,
        label: str = "",
        cwd: str = ".",
        expect_port: int = 0,
        timeout: int = DEFAULT_READY_TIMEOUT,
        **_: Any,
    ) -> str:
        workdir = self._resolve_cwd(cwd)
        managed, error = self.manager.start(label, command, cwd=workdir)
        if managed is None:
            raise ToolError(error)

        lines = [f"已在后台启动 {managed.handle}（{managed.label}）", f"  $ {command}"]

        if expect_port:
            ready, message = self.manager.wait_for_port(
                int(expect_port), handle=managed.handle, timeout=timeout
            )
            if not ready:
                # 起不来就顺手停掉——留着它只会占端口和名额
                self.manager.stop(managed.handle)
                raise ToolError(
                    f"{managed.label} 没能在端口 {expect_port} 就绪。\n{message}"
                )
            lines.append(f"  ✓ {message}")
        else:
            lines.append(
                "  接下来用 wait_for_port 等它就绪（或者 read_output 看日志）"
            )

        return "\n".join(lines)


class ReadOutput(ProcessTool):
    name = "read_output"
    description = (
        "读后台进程**新产生**的输出（增量，不是全量）。\n"
        "首次不给 cursor 会返回最近若干行；之后再调用时把返回的游标传回来，"
        "就只拿到新内容。排查「为什么起不来」主要靠它。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "handle": {"type": "string", "description": "start_process 返回的句柄，例如 p1"},
            "cursor": {
                "type": "integer",
                "description": "上次返回的游标。不给则读最近的若干行",
            },
            "tail": {
                "type": "integer",
                "description": "不给 cursor 时读最近多少行（默认 50）",
            },
        },
        "required": ["handle"],
    }

    def run(self, handle: str, cursor: int = -1, tail: int = 50, **_: Any) -> str:
        managed = self.manager.get(handle)
        if managed is None:
            return self._unknown(handle)

        if cursor < 0:
            lines = managed.tail(max(1, min(int(tail), 500)))
            _, new_cursor = managed.since(0)
        else:
            lines, new_cursor = managed.since(int(cursor))

        state = "运行中" if managed.running else f"已退出（码 {managed.exit_code}）"
        header = f"{handle}（{managed.label}）{state}"
        if not lines:
            return f"{header}\n（没有新输出）\n游标：{new_cursor}"

        body = truncate("\n".join(lines), limit=6000, label=f"{handle} 的输出")
        return f"{header}\n{body}\n游标：{new_cursor}"

    def _unknown(self, handle: str) -> str:
        known = "、".join(p.handle for p in self.manager.all()) or "（没有）"
        return f"没有 {handle} 这个进程。现有：{known}"


class WaitForPort(ProcessTool):
    name = "wait_for_port"
    description = (
        "等某个端口可以连接——**判断服务是否真的起来了**。\n"
        "不要用 sleep 猜：慢机器上不够、快机器上白等，而且服务崩了也会继续往下走。\n"
        "这个会在进程提前退出时立刻返回，并带上真实日志。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "port": {"type": "integer", "description": "端口号"},
            "handle": {
                "type": "string",
                "description": "关联的进程句柄（可选）。给了就能在它崩溃时立刻知道",
            },
            "host": {"type": "string", "description": "主机，默认 127.0.0.1"},
            "timeout": {
                "type": "integer",
                "description": f"最长等多少秒（默认 {DEFAULT_READY_TIMEOUT}）",
            },
        },
        "required": ["port"],
    }

    def run(
        self,
        port: int,
        handle: str = "",
        host: str = "127.0.0.1",
        timeout: int = DEFAULT_READY_TIMEOUT,
        **_: Any,
    ) -> str:
        ok, message = self.manager.wait_for_port(
            int(port), handle=handle, host=host, timeout=timeout
        )
        if not ok:
            raise ToolError(message)
        return message


class StopProcess(ProcessTool):
    name = "stop_process"
    description = (
        "停止一个后台进程（连同它派生的子进程——只杀父进程会留下孤儿占着端口）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "handle": {"type": "string", "description": "进程句柄，例如 p1"},
            "force": {
                "type": "boolean",
                "description": "直接强杀，不给优雅退出的时间（默认 false）",
            },
        },
        "required": ["handle"],
    }

    def run(self, handle: str, force: bool = False, **_: Any) -> str:
        ok, message = self.manager.stop(handle, force=bool(force))
        if not ok:
            known = "、".join(p.handle for p in self.manager.all()) or "（没有）"
            raise ToolError(f"{message}。现有：{known}")
        return message


class ListProcesses(ProcessTool):
    name = "list_processes"
    description = "列出这个会话启动过的后台进程及其状态。"

    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **_: Any) -> str:
        items = self.manager.all()
        if not items:
            return "还没有启动过后台进程。"

        blocks = []
        for managed in sorted(items, key=lambda p: p.handle):
            blocks.append(managed.summary())
            recent = managed.tail(3)
            if recent:
                blocks.append("  最近输出：" + " / ".join(line[:70] for line in recent))
        return "\n".join(blocks)


PROCESS_TOOLS = (StartProcess, ReadOutput, WaitForPort, StopProcess, ListProcesses)
