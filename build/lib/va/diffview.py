"""统一 diff 的生成、着色与统计。

用途：在真正动盘之前，把「将要发生什么改动」以人类可读的方式呈现出来。
审批时看的应该是 diff，而不是工具的原始参数。
"""

from __future__ import annotations

import difflib

from .util import bold, cyan, dim, green, red

CONTEXT_LINES = 3
MAX_DIFF_LINES = 500


def make_diff(old: str, new: str, path: str, context: int = CONTEXT_LINES) -> str:
    """生成 unified diff。old/new 是完整文件内容，返回空串表示无差异。

    用 splitlines()（丢弃行尾符）配合 lineterm=""，这样即使原文末尾没有换行符，
    输出也不会被拼歪——这是直接对 splitlines(keepends=True) 做 diff 时的经典坑。
    """
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    if old_lines == new_lines:
        return ""
    return "\n".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=context,
            lineterm="",
        )
    )


def diff_stats(diff_text: str) -> tuple[int, int]:
    """返回 (新增行数, 删除行数)。"""
    added = removed = 0
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def colorize(diff_text: str) -> str:
    """给 diff 上色：新增绿、删除红、位置信息青、上下文灰。"""
    out: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith(("+++", "---")):
            out.append(bold(line))
        elif line.startswith("@@"):
            out.append(cyan(line))
        elif line.startswith("+"):
            out.append(green(line))
        elif line.startswith("-"):
            out.append(red(line))
        else:
            out.append(dim(line))
    return "\n".join(out)


def truncate_diff(diff_text: str, max_lines: int = MAX_DIFF_LINES) -> str:
    """diff 过长时保留头尾，中间折叠。"""
    lines = diff_text.splitlines()
    if len(lines) <= max_lines:
        return diff_text
    half = max_lines // 2
    head, tail = lines[:half], lines[-half:]
    omitted = len(lines) - len(head) - len(tail)
    return "\n".join(head + [f"... [中间省略 {omitted} 行差异] ..."] + tail)


def describe_change(path: str, old: str, new: str, *, max_lines: int = MAX_DIFF_LINES) -> str | None:
    """生成带表头的彩色 diff 块，供审批界面直接打印。返回 None 表示无差异。"""
    diff_text = make_diff(old, new, path)
    if not diff_text:
        return None
    added, removed = diff_stats(diff_text)
    return f"{path}  (+{added} -{removed})\n{colorize(truncate_diff(diff_text, max_lines))}"


def plain_diff(path: str, old: str, new: str) -> str | None:
    """不带颜色、不带表头的纯 diff。"""
    return make_diff(old, new, path) or None
