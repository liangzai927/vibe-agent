"""浏览器探测与启动。

核心原则：**不假设任何环境**。

有的机器装了 Chrome，有的只有 Edge，容器里通常是 Playwright 自带的 Chromium，
还有的什么都没有。这里按优先级逐个尝试，并且把「试过哪些、为什么失败」
如实报出来——而不是丢一句 `Executable doesn't exist` 让人自己猜。

探测优先级：
    1. VA_BROWSER_EXECUTABLE   显式指定可执行文件
    2. VA_BROWSER_CHANNEL      显式指定通道（chrome / msedge / chromium）
    3. 系统 Chrome
    4. 系统 Edge
    5. Playwright 自带 Chromium（需要 playwright install chromium）
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

INSTALL_HINT = (
    "安装浏览器二选一：\n"
    "  · 复用系统已装的 Chrome：export VA_BROWSER_CHANNEL=chrome\n"
    "  · 装 Playwright 自带 Chromium：playwright install chromium\n"
    "  容器里可以直接用：docker run -e VA_INSTALL_BROWSER=1 ..."
)

MAC_CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)

LINUX_BINARIES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
)


class BrowserUnavailable(RuntimeError):
    """找不到可用的浏览器，附带可执行的修复建议。"""

    def __init__(self, message: str, hint: str = INSTALL_HINT) -> None:
        super().__init__(message)
        self.hint = hint


@dataclass
class BrowserInfo:
    available: bool
    kind: str = ""  # channel / executable / bundled
    target: str = ""  # "chrome" 或可执行文件路径
    label: str = ""
    error: str = ""
    hint: str = ""
    tried: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.available:
            return f"{self.label}（{self.kind}）"
        return self.error or "未找到可用浏览器"


_cached: BrowserInfo | None = None


# --------------------------------------------------------------------- 依赖


def playwright_installed() -> bool:
    import importlib.util

    return importlib.util.find_spec("playwright") is not None


def sync_playwright():
    """拿到 playwright 的入口。没装就抛出带安装提示的异常。"""
    if not playwright_installed():
        raise BrowserUnavailable(
            "没有安装 playwright，无法渲染页面。",
            hint="安装：pip install -r requirements-browser.txt",
        )
    from playwright.sync_api import sync_playwright as _entry

    return _entry


# 兼容旧名字
_sync_playwright = sync_playwright


def _system_browser() -> tuple[str, str] | None:
    """找系统里现成的 Chrome / Edge，返回 (kind, target)。"""
    for command in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
        found = shutil.which(command)
        if found:
            return "executable", found
    for path in MAC_CHROME_PATHS:
        if Path(path).is_file():
            return "executable", path
    for name in LINUX_BINARIES:
        found = shutil.which(name)
        if found:
            return "executable", found
    return None


def _candidates() -> list[tuple[str, str]]:
    """按优先级给出候选列表。显式配置时不再自动回退到最后。"""
    explicit: list[tuple[str, str]] = []
    executable = os.environ.get("VA_BROWSER_EXECUTABLE", "").strip()
    channel = os.environ.get("VA_BROWSER_CHANNEL", "").strip()

    if executable:
        explicit.append(("executable", executable))
    if channel:
        explicit.append(("channel", channel))

    fallback: list[tuple[str, str]] = []
    system = _system_browser()
    if system:
        fallback.append(system)
    fallback.append(("channel", "chrome"))
    fallback.append(("channel", "msedge"))
    fallback.append(("bundled", ""))

    # 去重，保持顺序
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for item in explicit + fallback:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def label_for(kind: str, target: str) -> str:
    if kind == "bundled":
        return "Playwright 自带 Chromium"
    if kind == "channel":
        return f"系统 {target}"
    return Path(target).name


def _short_error(exc: BaseException, limit: int = 90) -> str:
    """Playwright 的异常类名就叫 Error，光报类名等于没报。取消息首行。"""
    text = " ".join(str(exc).split())
    if not text:
        return type(exc).__name__
    return text[:limit]


def _try_launch(entry, kind: str, target: str, timeout: int = 20):
    """真的启动一次。只有启动成功才算「可用」。"""
    with entry() as p:
        if kind == "executable":
            browser = p.chromium.launch(executable_path=target, timeout=timeout * 1000)
        elif kind == "channel":
            browser = p.chromium.launch(channel=target, timeout=timeout * 1000)
        else:
            browser = p.chromium.launch(timeout=timeout * 1000)
        try:
            version = browser.version
        finally:
            browser.close()
    return version


def detect(*, force: bool = False, probe: bool = True) -> BrowserInfo:
    """探测可用浏览器。结果会缓存（probe=True 时会真的启动一次，约 1s）。"""
    global _cached
    if _cached is not None and not force:
        return _cached

    info = BrowserInfo(available=False)

    if not playwright_installed():
        info.error = "未安装 playwright"
        info.hint = "安装：pip install -r requirements-browser.txt"
        _cached = info
        return info

    if not probe:
        kind, target = _candidates()[0]
        info.available = True
        info.kind, info.target = kind, target
        info.label = label_for(kind, target)
        _cached = info
        return info

    entry = sync_playwright()
    failures: list[str] = []

    for kind, target in _candidates():
        name = label_for(kind, target)
        try:
            version = _try_launch(entry, kind, target)
        except Exception as exc:  # noqa: BLE001 - 探测阶段要把各种失败都吃掉
            failures.append(f"{name}: {_short_error(exc)}")
            continue
        info.available = True
        info.kind, info.target = kind, target
        info.label = name
        info.error = ""
        info.tried = failures
        info.hint = ""
        _cached = info
        return info

    info.tried = failures
    info.error = "没有找到可启动的浏览器（试过：" + "、".join(failures) + "）"
    info.hint = INSTALL_HINT
    _cached = info
    return info


def reset_cache() -> None:
    global _cached
    _cached = None


def launch(p, *, timeout_ms: int = 30_000, info: BrowserInfo | None = None):
    """按探测结果启动浏览器。调用方负责 close。"""
    info = info or detect()
    if not info.available:
        raise BrowserUnavailable(info.error, info.hint or INSTALL_HINT)

    if info.kind == "executable":
        return p.chromium.launch(executable_path=info.target, timeout=timeout_ms)
    if info.kind == "channel":
        return p.chromium.launch(channel=info.target, timeout=timeout_ms)
    return p.chromium.launch(timeout=timeout_ms)
