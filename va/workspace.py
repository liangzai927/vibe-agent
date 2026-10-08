"""工作区沙箱：所有文件读写的路径都必须落在根目录内。

这一层是安全边界。工具层永远通过 ``Workspace.resolve()`` 拿路径，
不允许自己拼 ``Path``，这样越界访问会在唯一的位置被拦住。
"""

from __future__ import annotations

from pathlib import Path

from .util import dim

# 这些目录不参与遍历/搜索，避免把 agent 拖进依赖地狱
IGNORED_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".next",
        ".nuxt",
        ".turbo",
        ".cache",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "target",
        ".idea",
        ".vscode",
        "coverage",
        ".DS_Store",
    }
)

BINARY_SUFFIXES = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tiff",
        ".pdf", ".zip", ".gz", ".tar", ".7z", ".rar", ".bz2", ".xz",
        ".woff", ".woff2", ".ttf", ".otf", ".eot",
        ".so", ".dylib", ".dll", ".exe", ".bin", ".o", ".a", ".class", ".pyc",
        ".mp3", ".mp4", ".mov", ".avi", ".wav", ".flac", ".webm",
        ".sqlite", ".db", ".lock",
    }
)

MAX_TEXT_FILE_BYTES = 2_000_000  # 单文件超过 2MB 就拒绝读入


class WorkspaceError(Exception):
    """路径越界或非法访问。"""


class Workspace:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise WorkspaceError(f"工作区不存在或不是目录：{self.root}")

    # ------------------------------------------------------------------ 路径

    def contains(self, path: Path) -> bool:
        return path == self.root or self.root in path.parents

    def resolve(self, path: str | Path, *, must_exist: bool = False) -> Path:
        """把相对/绝对路径统一解析为工作区内的绝对路径。"""
        raw = Path(str(path)).expanduser()
        candidate = raw if raw.is_absolute() else self.root / raw
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise WorkspaceError(f"无法解析路径 {path!r}: {exc}") from exc

        if not self.contains(resolved):
            raise WorkspaceError(
                f"路径越界：{path!r} 解析为 {resolved}，不在工作区 {self.root} 内。\n"
                f"（安全策略：agent 只能在工作区内读写）"
            )
        if must_exist and not resolved.exists():
            raise WorkspaceError(f"路径不存在：{self.relative(resolved)}")
        return resolved

    def relative(self, path: Path) -> str:
        try:
            rel = path.relative_to(self.root)
        except ValueError:
            return str(path)
        text = rel.as_posix()
        return text if text else "."

    # ------------------------------------------------------------------ 遍历

    def iter_files(
        self,
        start: Path | None = None,
        *,
        follow_symlinks: bool = False,
    ):
        """深度优先遍历工作区内的文件，跳过忽略目录。"""
        base = start or self.root
        stack = [base]
        while stack:
            current = stack.pop()
            try:
                entries = sorted(current.iterdir(), key=lambda p: p.name)
            except (PermissionError, OSError):
                continue
            for entry in entries:
                name = entry.name
                try:
                    if entry.is_symlink() and not follow_symlinks:
                        continue
                    if entry.is_dir():
                        if name in IGNORED_DIRS or name.startswith(".git"):
                            continue
                        stack.append(entry)
                    elif entry.is_file():
                        if name in IGNORED_DIRS:
                            continue
                        yield entry
                except OSError:
                    continue

    # ------------------------------------------------------------------ 展示

    def describe(self) -> str:
        return f"{self.root}  {dim('(沙箱根目录)')}"
