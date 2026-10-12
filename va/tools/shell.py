"""Shell 工具：在工作区内执行命令。

安全策略：
* 命令在 ``cwd`` 下执行，且 cwd 必须落在工作区内；
* 硬性拦截少数不可逆的破坏性命令（rm -rf / 之类），其余交给用户审批；
* 有超时，超时后杀掉整个进程组，避免残留子进程。
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from typing import Any

from ..util import truncate
from ..workspace import Workspace
from .base import Tool, ToolError

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 900

# 绝对禁止的命令模式（即使 auto_approve 也拦）
FORBIDDEN = [
    (re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rR][a-zA-Z]*\s+/\s*($|[;&|])"), "删除根目录"),
    (re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rR][a-zA-Z]*\s+(~|\$HOME)\s*($|[;&|])"), "删除家目录"),
    (re.compile(r"\bmkfs(\.\w+)?\b"), "格式化磁盘"),
    (re.compile(r"\bdd\b.*\bof=/dev/[sh]d"), "直接写裸设备"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork 炸弹"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"), "关停系统"),
    (re.compile(r"\bchmod\s+(-[a-zA-Z]+\s+)*777\s+/\s*($|[;&|])"), "修改根目录权限"),
]


class RunBash(Tool):
    def __init__(self, workspace, ctx=None) -> None:
        self.workspace = workspace
        # **要 ctx 才能拿到取消信号。** 不接的话「停止」按钮对它无效——
        # 一个卡住的 grep 只能等超时（默认 120 秒，最长 900 秒）。
        self.ctx = ctx

    name = "run_bash"
    description = (
        "在工作区内执行 shell 命令并返回 stdout+stderr。"
        "用于运行测试、构建、git、安装依赖等。避免使用交互式命令（如不带参数的 vim）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 shell 命令"},
            "cwd": {"type": "string", "description": "相对工作区的执行目录（默认根目录）"},
            "timeout": {
                "type": "integer",
                "description": f"超时秒数，默认 {DEFAULT_TIMEOUT}，最大 {MAX_TIMEOUT}",
            },
        },
        "required": ["command"],
    }
    requires_approval = True
    # 任意命令的副作用无法追踪（可能改任意文件）——撤销栈必须如实标记
    opaque = True


    def run(
        self,
        command: str,
        cwd: str = ".",
        timeout: int = DEFAULT_TIMEOUT,
        **_: Any,
    ) -> str:
        # **取消检查从 ctx 拿。** 用参数的话每个调用方都要传，
        # 而「能不能停」不该是调用方记得才有的能力。
        cancel_check = None
        ctx = getattr(self, "ctx", None)
        if ctx is not None and getattr(ctx, "cancel_event", None) is not None:
            cancel_check = ctx.cancel_event.is_set

        command = command.strip()
        if not command:
            raise ToolError("命令为空")

        for pattern, reason in FORBIDDEN:
            if pattern.search(command):
                raise ToolError(f"命令被安全策略拒绝（{reason}）：{command}")

        workdir = self.workspace.resolve(cwd, must_exist=True)
        if not workdir.is_dir():
            raise ToolError(f"cwd 不是目录：{cwd}")

        seconds = max(1, min(int(timeout), MAX_TIMEOUT))

        try:
            process = subprocess.Popen(
                command,
                shell=True,
                cwd=str(workdir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,  # 便于整组 kill
                env={**os.environ, "GIT_PAGER": "cat", "PAGER": "cat", "TERM": "dumb"},
            )
            # 登记：取消任务时要能杀掉它
            with _active_lock:
                _active[id(process)] = process
        except OSError as exc:
            raise ToolError(f"无法启动命令：{exc}") from exc

        try:
            # **轮询而不是一次等到超时。** 一次 communicate(timeout=900) 的话，
            # 用户点「停止」要等 15 分钟才生效——那等于不能停。
            # 切成小段，每段之间看一眼取消信号。
            deadline = time.monotonic() + seconds
            output = ""
            timed_out = False
            cancelled = False
            while True:
                slice_seconds = min(0.4, max(0.05, deadline - time.monotonic()))
                try:
                    output, _ = process.communicate(timeout=slice_seconds)
                    break
                except subprocess.TimeoutExpired:
                    if cancel_check is not None and cancel_check():
                        _kill_group(process, force=True)
                        try:
                            output, _ = process.communicate(timeout=5)
                        except subprocess.TimeoutExpired:
                            output = ""
                        cancelled = True
                        break
                    if time.monotonic() >= deadline:
                        _kill_group(process)
                        try:
                            output, _ = process.communicate(timeout=5)
                        except subprocess.TimeoutExpired:
                            output = ""
                        timed_out = True
                        break
        finally:
            with _active_lock:
                _active.pop(id(process), None)

        # 外部杀掉的（cancel() 里的 kill_active_shell）不会走 cancelled 分支——
        # communicate 会正常返回，只是退出码是负的。**那种情况也要说清楚**，
        # 不然用户看到的是一句莫名其妙的「exit code: -9」。
        if not cancelled and cancel_check is not None and cancel_check():
            cancelled = True

        if cancelled:
            raise ToolError("命令被用户停止了。\n（跑的东西已经杀掉）")

        output = output or ""
        exit_code = process.returncode

        parts: list[str] = []
        if output.strip():
            parts.append(truncate(output.rstrip(), label="命令输出"))
        else:
            parts.append("(无输出)")

        if timed_out:
            parts.append(f"\n[超时] 命令超过 {seconds}s 被终止。可增大 timeout，或拆分命令。")
        parts.append(f"\n[exit code: {exit_code}]")
        return "\n".join(parts)


#: 正在跑的 shell 进程。**取消任务时要能杀掉它**——
#: 一个 grep 卡在巨大目录树上，光靠「下一轮循环检查」是等不到的。
_active: dict[int, subprocess.Popen] = {}
_active_lock = threading.Lock()


def kill_active_shell() -> bool:
    """杀掉正在跑的 shell 命令。返回有没有杀到东西。"""
    with _active_lock:
        processes = list(_active.values())
        _active.clear()
    for process in processes:
        _kill_group(process, force=True)
    return bool(processes)


def _kill_group(process: subprocess.Popen, *, force: bool = False) -> None:
    try:
        os.killpg(
            os.getpgid(process.pid), signal.SIGKILL if force else signal.SIGTERM
        )
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.terminate()
        except OSError:
            return
    if force:
        return
    # 给它 3 秒优雅退出，否则 SIGKILL
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except OSError:
                pass
