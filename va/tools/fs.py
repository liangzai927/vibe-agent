"""文件系统工具：读、写、精确编辑、列目录。"""

from __future__ import annotations

from typing import Any

from ..util import truncate
from ..workspace import MAX_TEXT_FILE_BYTES, Workspace, WorkspaceError
from .base import Tool, ToolError


def _require_text(path, workspace: Workspace) -> str:
    """读文本文件，做体积与二进制检查。"""
    if path.stat().st_size > MAX_TEXT_FILE_BYTES:
        raise ToolError(
            f"文件过大（{path.stat().st_size / 1e6:.1f}MB > 2MB），请用 offset/limit 分段读取，"
            f"或改用 grep 定位关键片段。"
        )
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolError(f"读取失败：{exc}") from exc
    except OSError as exc:
        raise ToolError(f"读取失败：{exc}") from exc


class ReadFile(Tool):
    name = "read_file"
    description = (
        "读取工作区内某个文本文件的内容，返回带行号的结果。"
        "大文件请配合 offset/limit 分段读取。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区的路径，例如 src/main.py"},
            "offset": {"type": "integer", "description": "起始行号，从 1 开始（可选）"},
            "limit": {"type": "integer", "description": "最多读取多少行（可选，默认 2000）"},
        },
        "required": ["path"],
    }

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def run(self, path: str, offset: int = 1, limit: int = 2000, **_: Any) -> str:
        target = self.workspace.resolve(path, must_exist=True)
        if target.is_dir():
            raise ToolError(f"{path} 是目录，请用 list_dir")
        text = _require_text(target, self.workspace)

        lines = text.splitlines()
        total = len(lines)
        start = max(1, int(offset))
        end = min(total, start - 1 + max(1, int(limit)))
        if start > total:
            return f"{self.workspace.relative(target)} 共 {total} 行，起点 {start} 超出范围。"

        width = len(str(end))
        body = "\n".join(f"{i:>{width}}\t{lines[i - 1]}" for i in range(start, end + 1))
        header = f"{self.workspace.relative(target)}  第 {start}-{end} 行 / 共 {total} 行"
        if end < total:
            header += f"（还有 {total - end} 行未显示）"
        return f"{header}\n{body}"


class WriteFile(Tool):
    name = "write_file"
    description = (
        "把内容整体写入文件（覆盖已存在内容），父目录会自动创建。"
        "修改已有文件时优先用 edit_file，避免误删。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区的目标路径"},
            "content": {"type": "string", "description": "完整文件内容"},
        },
        "required": ["path", "content"],
    }
    requires_approval = True

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def run(self, path: str, content: str, **_: Any) -> str:
        target = self.workspace.resolve(path)
        if target.is_dir():
            raise ToolError(f"{path} 是目录，不能写入")
        existed = target.exists()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"写入失败：{exc}") from exc

        lines = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
        action = "覆盖" if existed else "新建"
        return f"已{action} {self.workspace.relative(target)}（{len(content)} 字符，{lines} 行）"


class EditFile(Tool):
    name = "edit_file"
    description = (
        "在文件中精确替换一段文本。old_string 必须与文件内容逐字符一致（含缩进），"
        "且默认要求在文件中唯一出现，否则报错。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区的文件路径"},
            "old_string": {"type": "string", "description": "要被替换的原文（需精确匹配）"},
            "new_string": {"type": "string", "description": "替换后的新文本"},
            "replace_all": {"type": "boolean", "description": "是否替换全部出现（默认 false）"},
        },
        "required": ["path", "old_string", "new_string"],
    }
    requires_approval = True

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def run(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        **_: Any,
    ) -> str:
        target = self.workspace.resolve(path, must_exist=True)
        if target.is_dir():
            raise ToolError(f"{path} 是目录")
        text = _require_text(target, self.workspace)

        if old_string == new_string:
            raise ToolError("old_string 与 new_string 相同，无需修改")
        count = text.count(old_string)
        if count == 0:
            raise ToolError(
                f"在 {path} 中找不到 old_string。请先 read_file 确认原文"
                f"（注意缩进与空白必须完全一致）。"
            )
        if count > 1 and not replace_all:
            raise ToolError(
                f"old_string 在 {path} 中出现了 {count} 次，不唯一。"
                f"请扩大上下文使其唯一，或设置 replace_all=true。"
            )

        updated = text.replace(old_string, new_string) if replace_all else text.replace(
            old_string, new_string, 1
        )
        try:
            target.write_text(updated, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"写入失败：{exc}") from exc

        replaced = count if replace_all else 1
        return f"已修改 {self.workspace.relative(target)}（替换 {replaced} 处）"


class ListDir(Tool):
    name = "list_dir"
    description = "列出目录内容。默认只列一层，设 recursive=true 可递归（自动跳过 node_modules 等）。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目录路径，默认工作区根目录"},
            "recursive": {"type": "boolean", "description": "是否递归列出（默认 false）"},
            "max_entries": {"type": "integer", "description": "最多返回多少项（默认 300）"},
        },
        "required": [],
    }

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def run(
        self,
        path: str = ".",
        recursive: bool = False,
        max_entries: int = 300,
        **_: Any,
    ) -> str:
        target = self.workspace.resolve(path, must_exist=True)
        if not target.is_dir():
            raise ToolError(f"{path} 不是目录，请用 read_file")

        limit = max(1, int(max_entries))
        rows: list[str] = []
        truncated_note = ""

        if recursive:
            for entry in self.workspace.iter_files(target):
                rel = entry.relative_to(target).as_posix()
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                rows.append(f"{rel}  ({size}B)")
                if len(rows) >= limit:
                    truncated_note = f"\n... 已达上限 {limit} 项，可能还有更多"
                    break
        else:
            try:
                entries = sorted(
                    target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
                )
            except OSError as exc:
                raise ToolError(f"无法读取目录：{exc}") from exc
            for entry in entries:
                if entry.is_dir():
                    rows.append(f"{entry.name}/")
                else:
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0
                    rows.append(f"{entry.name}  ({size}B)")
                if len(rows) >= limit:
                    truncated_note = f"\n... 已达上限 {limit} 项"
                    break

        if not rows:
            return f"{self.workspace.relative(target)} 是空目录"
        header = f"{self.workspace.relative(target)} 共 {len(rows)} 项" + ("（递归）" if recursive else "")
        return header + "\n" + "\n".join(rows) + truncated_note
