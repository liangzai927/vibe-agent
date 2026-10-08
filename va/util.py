"""通用小工具：输出截断、文本度量、ANSI 颜色。"""

from __future__ import annotations

import os
import sys

# --------------------------------------------------------------------- 截断

DEFAULT_LIMIT = 30_000


def truncate(text: str, limit: int = DEFAULT_LIMIT, label: str = "输出") -> str:
    """头尾保留式截断——比单纯砍尾巴更有用，因为报错通常在末尾。"""
    if len(text) <= limit:
        return text
    head_size = int(limit * 0.7)
    tail_size = limit - head_size
    omitted = len(text) - limit
    return (
        text[:head_size]
        + f"\n\n... [{label}过长，中间省略 {omitted} 字符] ...\n\n"
        + text[-tail_size:]
    )


# --------------------------------------------------------------------- 文本

def preview(text: str, width: int = 72) -> str:
    """压成单行短预览，用于日志。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def line_count(text: str) -> int:
    return text.count("\n") + 1 if text else 0


# --------------------------------------------------------------------- 颜色

def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("VA_FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


_COLOR = _supports_color()


def c(text: str, code: str) -> str:
    if not _COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def dim(text: str) -> str:
    return c(text, "2")


def bold(text: str) -> str:
    return c(text, "1")


def red(text: str) -> str:
    return c(text, "31")


def green(text: str) -> str:
    return c(text, "32")


def yellow(text: str) -> str:
    return c(text, "33")


def blue(text: str) -> str:
    return c(text, "34")


def cyan(text: str) -> str:
    return c(text, "36")
