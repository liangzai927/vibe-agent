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

_WINDOWS_SCRIPT = """Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = $args[0]
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


def _run(command: list[str], *, stdin: str | None = None, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        input=stdin,
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
            result = _run(
                [shell, "-NoProfile", "-STA", "-Command", _WINDOWS_SCRIPT, prompt],
                timeout=timeout,
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
