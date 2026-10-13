"""调用系统原生的文件/文件夹选择框。

**为什么必须由服务端来弹**：浏览器出于安全考虑拿不到真实路径——
``<input webkitdirectory>`` 只给 ``webkitRelativePath``（相对路径），
拿不到用户可以复制粘贴的绝对路径。但我们的服务**就跑在用户本机上**，
所以让它去弹系统对话框，再把路径回给浏览器。

**为什么保留手输**：对话框不是总能弹出来——无图形界面、远程会话、
权限被拒、或者用户就是喜欢敲路径。所以这个函数返回失败原因而不是抛异常，
界面上「浏览…」按钮旁边永远留着一个可以直接输入的框。
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

DEFAULT_TIMEOUT = 300  # 用户可能要想一会儿，但也不能永远挂着

CANCELLED = "__VA_CANCELLED__"

_MACOS_SCRIPT = """on run argv
    set thePrompt to item 1 of argv
    try
        set chosen to choose folder with prompt thePrompt
        return POSIX path of chosen
    on error number -128
        return "%s"
    end try
end run
""" % CANCELLED

#: 提示语通过**环境变量**传，不走命令行参数。
#:
#: 踩过的坑：原来写的是
#:     [shell, "-NoProfile", "-STA", "-Command", _WINDOWS_SCRIPT, prompt]
#: 而 **`-Command` 会把后面所有东西都当成脚本体**——于是 prompt（比如
#: 「选择文件夹」）被 PowerShell 当成一条**命令**去执行，直接在 Windows 上报：
#:     CommandNotFoundException
#:     + FullyQualifiedErrorId : CommandNotFoundException
#: 同时 `$args[0]` 也是空的。
#: 环境变量没有引号、没有参数切分，这类问题一次性绕开。
_WINDOWS_SCRIPT = """Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = $env:VA_PICK_PROMPT
$dialog.ShowNewFolderButton = $true
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Out.Write($dialog.SelectedPath)
} else {
    [Console]::Out.Write("%s")
}
""" % CANCELLED


@dataclass
class PickResult:
    ok: bool
    path: str = ""
    cancelled: bool = False
    error: str = ""

    def describe(self) -> str:
        if self.ok:
            return self.path
        if self.cancelled:
            return "已取消"
        return self.error or "没能打开选择框"


def available() -> tuple[bool, str]:
    """当前平台能不能弹框，以及用的是什么。"""
    import platform

    system = platform.system()

    if system == "Windows":
        if shutil.which("powershell") or shutil.which("pwsh"):
            return True, "PowerShell 的文件夹选择框"
        return False, "找不到 PowerShell"
    if system == "Darwin":
        if shutil.which("osascript"):
            return True, "macOS 原生的 choose folder"
        return False, "找不到 osascript"
    if shutil.which("zenity"):
        return True, "zenity"
    if shutil.which("kdialog"):
        return True, "kdialog"
    return False, "需要 zenity（GNOME）或 kdialog（KDE）"


def _run(
    command: list[str],
    *,
    stdin: str | None = None,
    timeout: int,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        input=stdin,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _interpret(result: subprocess.CompletedProcess) -> PickResult:
    raw = (result.stdout or "").strip()
    if raw == CANCELLED:
        return PickResult(False, cancelled=True)
    if result.returncode != 0 or not raw:
        detail = (result.stderr or "").strip().splitlines()
        return PickResult(
            False,
            error=detail[-1] if detail else "选择框没能打开（可能会话没有图形界面）",
        )
    return PickResult(True, path=raw)


def pick_folder(prompt: str = "选择文件夹", *, timeout: int = DEFAULT_TIMEOUT) -> PickResult:
    """弹出系统原生的文件夹选择框。用户取消时 ``cancelled=True``。"""
    import platform

    system = platform.system()

    try:
        if system == "Darwin":
            result = _run(
                ["osascript", "-", prompt], stdin=_MACOS_SCRIPT, timeout=timeout
            )
        elif system == "Windows":
            shell = shutil.which("pwsh") or shutil.which("powershell")
            if not shell:
                return PickResult(False, error="找不到 PowerShell")

            import os
            import platform as _platform

            argv = [shell, "-NoProfile", "-Command", _WINDOWS_SCRIPT]
            # **`-STA` 只有 Windows PowerShell 5.1 需要**（WinForms 要求单线程
            # 单元）。PowerShell 7（pwsh）默认就是 STA，而且不认识这个开关——
            # 传了会让它也报错。
            if "pwsh" not in os.path.basename(shell).lower():
                argv.insert(2, "-STA")

            result = _run(
                argv,
                timeout=timeout,
                # 提示语走环境变量，不占命令行位置
                env={**os.environ, "VA_PICK_PROMPT": prompt},
            )
        else:
            if shutil.which("zenity"):
                result = _run(
                    ["zenity", "--file-selection", "--directory", f"--title={prompt}"],
                    timeout=timeout,
                )
            elif shutil.which("kdialog"):
                result = _run(
                    ["kdialog", "--getexistingdirectory", ".", "--title", prompt],
                    timeout=timeout,
                )
            else:
                return PickResult(
                    False, error="没有可用的选择框工具（zenity / kdialog）"
                )
    except subprocess.TimeoutExpired:
        return PickResult(False, error="选择框开着太久，已放弃")
    except (OSError, subprocess.SubprocessError) as exc:
        return PickResult(False, error=f"{type(exc).__name__}: {exc}")

    return _interpret(result)
