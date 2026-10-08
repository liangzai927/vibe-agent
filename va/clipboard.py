"""从剪贴板取图片。

**为什么需要它**：终端传的是按键，不是图片数据。你复制了一张设计稿，
在终端里按 Ctrl+V 什么都不会发生（或者只粘出一堆乱码）。但「剪贴板里有一张图」
这件事是可以问操作系统的——问到了就落成文件，再把路径给你。

各平台的做法：

    macOS    自带的 osascript。**不要用 System Events**——那会触发自动化权限
             弹窗；直接问剪贴板不需要任何权限。
    Windows  PowerShell 的 System.Windows.Forms.Clipboard
    Linux    Wayland 用 wl-paste，X11 用 xclip

拿不到图片时给的是「怎么装工具 / 为什么拿不到」，而不是一个 traceback。
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# 剪贴板里出现在这些类型名中，就认为有图片
_IMAGE_TYPE_HINTS = ("PNGf", "TIFF", "JPEG", "GIF", "BMP", "AVIF", "jp2", "8BPS")

DEFAULT_TIMEOUT = 20

# macOS 上用 heredoc 把脚本喂给 osascript，路径通过 argv 传——
# 这样路径里带空格或引号也不会把脚本拼坏。
_MACOS_SCRIPT = """on run argv
    set outFile to POSIX file (item 1 of argv)
    set fileRef to open for access outFile with write permission
    set eof fileRef to 0
    write (the clipboard as \u00abclass PNGf\u00bb) to fileRef
    close access fileRef
    return "ok"
end run
"""

_WINDOWS_SCRIPT = """Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$img = [System.Windows.Forms.Clipboard]::GetImage()
if ($null -eq $img) { [Console]::Error.WriteLine("clipboard has no image"); exit 2 }
$img.Save($args[0], [System.Drawing.Imaging.ImageFormat]::Png)
"""


@dataclass
class ClipboardImage:
    ok: bool
    path: Path | None = None
    error: str = ""
    hint: str = ""
    size: tuple[int, int] | None = None
    bytes_written: int = 0

    def describe(self) -> str:
        if not self.ok:
            text = f"没能从剪贴板取到图片：{self.error}"
            if self.hint:
                text += f"\n{self.hint}"
            return text
        dims = f"{self.size[0]}x{self.size[1]}" if self.size else "尺寸未知"
        return (
            f"已从剪贴板保存图片 → {self.path}\n"
            f"  {dims}，{self.bytes_written / 1024:.1f} KB"
        )


def _run(command: list[str], *, stdin: str | None = None, timeout: int = DEFAULT_TIMEOUT):
    return subprocess.run(
        command,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


# --------------------------------------------------------------------- 探测


def has_image() -> bool | None:
    """剪贴板里有没有图片。None 表示判断不了（工具缺失）。"""
    system = platform.system()

    if system == "Darwin":
        try:
            result = _run(["osascript", "-e", "clipboard info"])
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return any(hint in result.stdout for hint in _IMAGE_TYPE_HINTS)

    if system == "Windows":
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "if ([System.Windows.Forms.Clipboard]::ContainsImage()) { 'yes' } else { 'no' }"
        )
        try:
            result = _run(["powershell", "-NoProfile", "-Command", script])
        except (OSError, subprocess.SubprocessError):
            return None
        return "yes" in result.stdout

    # Linux：先看有没有工具，再看类型列表
    if shutil.which("wl-paste"):
        try:
            result = _run(["wl-paste", "--list-types"])
        except (OSError, subprocess.SubprocessError):
            return None
        return "image/" in result.stdout
    if shutil.which("xclip"):
        try:
            result = _run(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"])
        except (OSError, subprocess.SubprocessError):
            return None
        return "image/" in result.stdout
    return None


def support() -> tuple[bool, str]:
    """当前平台支不支持，以及用的是什么方式。"""
    system = platform.system()
    if system == "Darwin":
        if shutil.which("osascript"):
            return True, "macOS 自带的 osascript"
        return False, "找不到 osascript"
    if system == "Windows":
        if shutil.which("powershell") or shutil.which("pwsh"):
            return True, "PowerShell"
        return False, "找不到 powershell"
    if shutil.which("wl-paste"):
        return True, "wl-paste（Wayland）"
    if shutil.which("xclip"):
        return True, "xclip（X11）"
    return False, "需要 wl-paste（Wayland）或 xclip（X11）"


# --------------------------------------------------------------------- 取图


def _clean_osascript_error(stderr: str) -> str:
    """osascript 的报错前面挂着一串行号列号，对用户毫无意义，去掉。"""
    text = (stderr or "").strip()
    if not text:
        return "剪贴板里没有图片数据"
    last = text.splitlines()[-1]
    cleaned = re.sub(r"^\d+:\d+:\s*", "", last)
    cleaned = cleaned.replace("execution error: ", "")
    return cleaned or "剪贴板里没有图片数据"


def _macos_grab(target: Path) -> ClipboardImage:
    script = _MACOS_SCRIPT.replace("\u00ab", "\u00ab").replace("\u00bb", "\u00bb")
    try:
        result = _run(["osascript", "-", str(target)], stdin=script)
    except FileNotFoundError:
        return ClipboardImage(False, error="找不到 osascript", hint="这不是 macOS？")
    except subprocess.TimeoutExpired:
        return ClipboardImage(False, error="读取剪贴板超时")

    if result.returncode != 0 or "ok" not in result.stdout:
        return ClipboardImage(
            False,
            error=_clean_osascript_error(result.stderr),
            hint="先复制一张图片（截图、或在浏览器里右键复制图像），再试一次。",
        )
    return ClipboardImage(True, path=target)


def _windows_grab(target: Path) -> ClipboardImage:
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        return ClipboardImage(False, error="找不到 PowerShell")
    try:
        result = _run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_SCRIPT, str(target)]
        )
    except subprocess.TimeoutExpired:
        return ClipboardImage(False, error="读取剪贴板超时")

    if result.returncode != 0:
        return ClipboardImage(
            False,
            error=(result.stderr or "").strip()[:200] or "取不到图片",
            hint="先复制一张图片再试。远程会话里剪贴板可能不互通。",
        )
    return ClipboardImage(True, path=target)


def _linux_grab(target: Path) -> ClipboardImage:
    if shutil.which("wl-paste"):
        command = ["wl-paste", "--type", "image/png"]
        tool = "wl-paste"
    elif shutil.which("xclip"):
        command = ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]
        tool = "xclip"
    else:
        return ClipboardImage(
            False,
            error="没有可用的剪贴板工具",
            hint="装一个：Wayland 用 wl-clipboard，X11 用 xclip。",
        )

    try:
        result = subprocess.run(command, capture_output=True, timeout=DEFAULT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return ClipboardImage(False, error="读取剪贴板超时")

    if result.returncode != 0 or not result.stdout:
        return ClipboardImage(
            False,
            error=f"{tool} 没取到 PNG 数据",
            hint="先复制一张图片再试。也可能剪贴板里只有别的格式。",
        )
    target.write_bytes(result.stdout)
    return ClipboardImage(True, path=target)


def read_image(target: str | Path) -> ClipboardImage:
    """把剪贴板里的图片写到 target。失败时返回带建议的原因。"""
    path = Path(target)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return ClipboardImage(False, error=f"创建目录失败：{exc}")

    if path.exists():
        path.unlink()

    system = platform.system()
    if system == "Darwin":
        result = _macos_grab(path)
    elif system == "Windows":
        result = _windows_grab(path)
    else:
        result = _linux_grab(path)

    if not result.ok:
        return result

    # 落盘成功不代表内容对——osascript 失败时也可能留下个空文件
    if not path.is_file() or path.stat().st_size == 0:
        path.unlink(missing_ok=True)
        return ClipboardImage(
            False,
            error="剪贴板里没有图片数据",
            hint="先复制一张图片（截图、或浏览器里右键复制图像），再试一次。",
        )

    # 落盘成功 != 内容是一张图。剪贴板里可能是被塞进去的任意字节
    # （比如某些程序把文本数据标成了 PNG 类型）。有 Pillow 就真解码一次，
    # 别让一个坏文件流到下游，到那时报错的地方离病因已经很远了。
    size: tuple[int, int] | None = None
    try:
        from PIL import Image
    except ImportError:
        pass  # 没有 Pillow 就只做大小检查，上面已经做过了
    else:
        try:
            with Image.open(path) as image:
                size = image.size
                image.load()  # 强制完整解码——被截断的文件在这里才会暴露
        except Exception as exc:  # noqa: BLE001
            path.unlink(missing_ok=True)
            return ClipboardImage(
                False,
                error=f"剪贴板里的数据不是有效图片（{type(exc).__name__}）",
                hint="有些程序会把文本塞进剪贴板的图片类型里。换成真正复制一张图试试。",
            )

    return ClipboardImage(
        True, path=path, size=size, bytes_written=path.stat().st_size
    )


def default_filename(prefix: str = "clip") -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}.png"


def inbox_dir(workspace_root: str | Path) -> Path:
    """剪贴板图片的默认落脚点。

    放在工作区的 .va/ 下——它本来就被建议加进 .gitignore，
    而且是「临时素材」，不该混进业务代码目录。
    """
    return Path(workspace_root) / ".va" / "inbox"
