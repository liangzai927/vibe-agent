"""工作区沙箱：所有文件读写的路径都必须落在根目录内。

这一层是安全边界。工具层永远通过 ``Workspace.resolve()`` 拿路径，
不允许自己拼 ``Path``，这样越界访问会在唯一的位置被拦住。

**唯一的例外是 ``resolve_readable()``**：用户明确指着的**图片/文档**
可以来自工作区外（设计稿常常躺在 ~/Desktop 或 ~/Downloads）。
这条通道有三个约束：

* 只有主动申请的工具能用（``read_design`` / ``inspect_image`` /
  ``read_document``），**写操作和静默的文本读取一律不许**
* 目标目录不在 ``VA_READ_DIRS`` 里时，每次都要用户点同意
* 越界的路径会原样展示给用户，不藏

为什么文本读取不放开：``read_file`` / ``grep`` 是「自己找路」的工具，
没有审批环节。放开它们等于让 prompt injection 能直接把
``~/.ssh/id_rsa`` 读出来发走。而读图片是「用户递过来的东西」，
语义和风险都不同。
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
    def __init__(self, root: str | Path, *, extra_read_roots: "list[str] | tuple[str, ...]" = ()) -> None:
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise WorkspaceError(f"工作区不存在或不是目录：{self.root}")
        # 预先放行的额外读取目录（VA_READ_DIRS）。放进来之后读它们不再询问。
        self.extra_read_roots: list[Path] = []
        for entry in extra_read_roots:
            try:
                candidate = Path(str(entry)).expanduser().resolve()
            except (OSError, RuntimeError):
                continue
            if candidate.is_dir():
                self.extra_read_roots.append(candidate)

    # ------------------------------------------------------------------ 路径

    def contains(self, path: Path) -> bool:
        return path == self.root or self.root in path.parents

    def _absolutise(self, path: str | Path) -> Path | None:
        """相对路径按工作区解析。

        不能因为「它是相对路径」就放行——``../outside.txt`` 也是相对路径，
        但它恰恰能走出工作区。
        """
        try:
            candidate = Path(str(path)).expanduser()
            return (candidate if candidate.is_absolute() else self.root / candidate).resolve()
        except (OSError, RuntimeError):
            return None

    def readable(self, path: str | Path) -> bool:
        """这个路径能不能读——工作区内，或者落在预先放行的目录里。

        注意这和 ``resolve()`` 不是一回事：``resolve()`` 只管工作区，
        写操作和文本读取走它。这个方法只服务于 ``resolve_readable()``。
        """
        resolved = self._absolutise(path)
        if resolved is None:
            return False
        if self.contains(resolved):
            return True
        return any(
            resolved == extra or extra in resolved.parents
            for extra in self.extra_read_roots
        )

    def needs_external_approval(self, path: str | Path) -> bool:
        """读这个路径要不要先问用户。工作区内的和 VA_READ_DIRS 里的不用问。"""
        return not self.readable(path)

    def resolve_readable(self, path: str | Path, *, must_exist: bool = False) -> Path:
        """解析一个**只用于读取**的路径，允许落在工作区外。

        写操作不要用这个。它存在的唯一理由是：用户手上那张设计稿
        在 ~/Downloads，而不是在项目里。
        """
        raw = Path(str(path)).expanduser()
        candidate = raw if raw.is_absolute() else self.root / raw
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise WorkspaceError(f"无法解析路径 {path!r}: {exc}") from exc

        # 只查「存不存在」，不查「是不是文件」——和 resolve() 保持一致。
        # 查得太严会把调用方更好的提示（「xx 是目录，不是文档」）挡掉，
        # 用户拿到一句含糊的「文件不存在」。
        if must_exist and not resolved.exists():
            raise WorkspaceError(f"路径不存在：{resolved}")
        return resolved

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
