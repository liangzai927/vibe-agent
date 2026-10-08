"""检索工具：按文件名找（glob）、按内容找（grep）。"""

from __future__ import annotations

import re
from fnmatch import fnmatch
from typing import Any

from ..workspace import BINARY_SUFFIXES, MAX_TEXT_FILE_BYTES, Workspace
from .base import Tool, ToolError


class Glob(Tool):
    name = "glob"
    description = (
        "按文件名模式查找文件，例如 '**/*.py'、'src/**/*.tsx'。"
        "用于快速了解项目结构，比 list_dir 递归更精准。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "glob 模式，支持 ** 递归"},
            "path": {"type": "string", "description": "搜索起点目录，默认工作区根目录"},
            "max_results": {"type": "integer", "description": "最多返回条数（默认 200）"},
        },
        "required": ["pattern"],
    }

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def run(self, pattern: str, path: str = ".", max_results: int = 200, **_: Any) -> str:
        base = self.workspace.resolve(path, must_exist=True)
        if not base.is_dir():
            raise ToolError(f"{path} 不是目录")

        limit = max(1, int(max_results))
        pattern = pattern.strip()
        anchored = not pattern.startswith("**/")

        matches: list[str] = []
        for entry in self.workspace.iter_files(base):
            rel = entry.relative_to(base).as_posix()
            # '**/*.py' 要能匹配根目录下的 a.py，所以两条都试
            if fnmatch(rel, pattern) or (anchored and fnmatch(rel, f"**/{pattern}")):
                matches.append(rel)

        if not matches:
            return f"没有匹配 {pattern!r} 的文件（起点：{self.workspace.relative(base)}）"

        matches.sort()
        shown = matches[:limit]
        note = f"\n... 共 {len(matches)} 个匹配，仅显示前 {limit} 个" if len(matches) > limit else ""
        return f"匹配 {pattern!r} 的文件（{len(matches)} 个）：\n" + "\n".join(shown) + note


class Grep(Tool):
    name = "grep"
    description = (
        "用正则表达式搜索文件内容，返回 文件:行号: 内容。"
        "定位函数定义、变量引用、报错信息时首选。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式，例如 'def main\\('"},
            "path": {"type": "string", "description": "搜索起点（文件或目录），默认工作区根目录"},
            "include": {"type": "string", "description": "只搜匹配的文件名，如 '*.py'（可选）"},
            "ignore_case": {"type": "boolean", "description": "忽略大小写（默认 false）"},
            "max_results": {"type": "integer", "description": "最多返回多少条匹配（默认 100）"},
        },
        "required": ["pattern"],
    }

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def run(
        self,
        pattern: str,
        path: str = ".",
        include: str | None = None,
        ignore_case: bool = False,
        max_results: int = 100,
        **_: Any,
    ) -> str:
        start = self.workspace.resolve(path, must_exist=True)
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            raise ToolError(f"正则表达式非法：{exc}") from exc

        limit = max(1, int(max_results))
        candidates = [start] if start.is_file() else list(self.workspace.iter_files(start))

        hits: list[str] = []
        scanned = 0
        for file_path in candidates:
            if include and not fnmatch(file_path.name, include):
                continue
            if file_path.suffix.lower() in BINARY_SUFFIXES:
                continue
            try:
                if file_path.stat().st_size > MAX_TEXT_FILE_BYTES:
                    continue
                text = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            scanned += 1
            rel = self.workspace.relative(file_path)
            for lineno, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{rel}:{lineno}: {line.strip()[:240]}")
                    if len(hits) >= limit:
                        break
            if len(hits) >= limit:
                break

        if not hits:
            return f"没有找到匹配 {pattern!r} 的内容（扫描 {scanned} 个文件）"
        note = f"\n... 已达上限 {limit} 条，请缩小搜索范围" if len(hits) >= limit else ""
        return f"匹配 {pattern!r}（扫描 {scanned} 个文件）：\n" + "\n".join(hits) + note
