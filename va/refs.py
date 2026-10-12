"""`@路径` 文件引用。

用户写「看一下 @src/auth.ts 里的刷新逻辑」，比写完整路径快，
而且**省掉 agent 自己找文件的那两轮**（先 glob、再 read_file）。
又快又省 token。

────────────────────────────────────────────────────────────────

**内联还是只给路径**

小文件直接内联进消息——agent 立刻就有内容，一处往返都不用。
大文件只给路径加一句提示，让它需要时自己 `read_file`。

这条分界很重要：内联一个 2000 行的文件，等于把整个上下文预算吃掉一块，
而用户可能只是想问「这个文件是干嘛的」。

**只认工作区里的路径**

`@../外面` 会被拒绝并说清原因。理由：这个展开发生在**提交消息之前**，
是 agent 替用户读文件，走的是「静默读取」那条线——而那条线一直是
锁在工作区里的（`read_file` / `grep` / `glob` 没有审批环节）。
要读外面得用 `@clip` 那类明确指向的外部内容。

**别把邮箱当地址**

`someone@example.com` 里的 `@` 不是引用。所以要求 `@` 前面是
行首或空白，且后面不能有空格。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: `@` 必须出现在行首或空白之后（避免吃掉邮箱里的 @），
#: 后面跟一串非空白、非 @ 的字符。
REF_TOKEN = re.compile(r"(?:(?<=\s)|^)@([^\s@]+)")

#: 小于这个大小就内联。定成字节而不是行数——长行文件（压缩过的 js）按行数
#: 判断会严重低估。
INLINE_LIMIT_BYTES = 24_000

#: 内联时最多给多少行，防止一行超长的文件把上下文撑爆
INLINE_LIMIT_LINES = 600

#: 自动补全遍历时跳过的目录。这些里面翻出来的路径只会淹没结果。
INLINE_SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", ".nuxt", "target", "vendor", ".idea", ".vscode", "coverage",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", "egg-info",
})

#: 按扩展名给代码块标语言，纯为了好看；认不出就不标
_LANGUAGES = {
    ".py": "python", ".js": "javascript", ".jsx": "jsx", ".ts": "typescript",
    ".tsx": "tsx", ".json": "json", ".md": "markdown", ".html": "html",
    ".css": "css", ".scss": "scss", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".go": "go", ".rs": "rust", ".java": "java", ".rb": "ruby",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".sql": "sql",
    ".vue": "vue", ".svelte": "svelte", ".xml": "xml", ".ini": "ini",
}


@dataclass
class Reference:
    raw: str          # 用户原样写的（含 @）
    given: str        # @ 后面那串
    path: str = ""    # 相对工作区的路径
    resolved: Path | None = None
    kind: str = "missing"   # file / dir / missing / outside / too_big / unreadable
    note: str = ""
    inlined: bool = False

    @property
    def ok(self) -> bool:
        return self.kind in ("file", "dir", "too_big")


def parse(text: str) -> list[Reference]:
    """找出所有 `@路径`。不去重——用户写两次就是两次。"""
    out: list[Reference] = []
    for match in REF_TOKEN.finditer(text or ""):
        raw = match.group(0)
        given = match.group(1)
        # `@clip` 那一类是剪贴板 token，由 cli 的展开逻辑处理，这里跳过
        if given.lower() in ("clip", "paste", "image", "img", "图片", "剪贴板"):
            continue
        # 邮箱的残留：@ 前面紧跟着字母数字（正则已经挡了大部分，
        # 但 `x@y.com` 这种在行首时挡不住）
        if match.start() > 0 and text[match.start() - 1].isalnum():
            continue
        out.append(Reference(raw=raw, given=given))
    return out


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _looks_binary(path: Path) -> bool:
    """用前 4KB 里有没有 NUL 判断。二进制内联进对话没有意义。"""
    try:
        with path.open("rb") as handle:
            chunk = handle.read(4096)
    except OSError:
        return True
    return b"\x00" in chunk


def resolve_one(ref: Reference, workspace_root: Path) -> Reference:
    """把一个引用解析成可用的东西，并把失败原因写进 note。"""
    root = Path(workspace_root).resolve()
    given = ref.given.rstrip(".,;:!?，。；：！？")   # 别把句末标点吃进路径

    try:
        candidate = (root / given).resolve() if not Path(given).is_absolute() else Path(given).resolve()
    except OSError as exc:
        ref.kind = "missing"
        ref.note = f"路径解析失败：{exc}"
        return ref

    # 越界拦在工作区这一层——和 read_file 一个口径
    if candidate != root and root not in candidate.parents:
        ref.kind = "outside"
        ref.note = (
            f"{given} 在工作区外。@ 引用只能读工作区里的文件；"
            f"外部图片用 @clip 贴进来"
        )
        return ref

    if not candidate.exists():
        ref.kind = "missing"
        ref.note = f"找不到 {given}"
        return ref

    ref.resolved = candidate
    ref.path = _relative(candidate, root)

    if candidate.is_dir():
        ref.kind = "dir"
        ref.note = f"{ref.path} 是目录，没有内联（需要时用 list_dir 看）"
        return ref

    try:
        size = candidate.stat().st_size
    except OSError as exc:
        ref.kind = "unreadable"
        ref.note = f"读不了 {given}：{exc}"
        return ref

    if size > INLINE_LIMIT_BYTES:
        ref.kind = "too_big"
        ref.note = f"{ref.path} 有 {size // 1024} KB，没有内联——需要时用 read_file 读"
        return ref

    if _looks_binary(candidate):
        ref.kind = "unreadable"
        ref.note = f"{ref.path} 看起来是二进制文件，没有内联"
        return ref

    ref.kind = "file"
    return ref


def _read_text(path: Path, limit_lines: int) -> tuple[str, bool]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", False
    lines = text.splitlines()
    if len(lines) > limit_lines:
        return "\n".join(lines[:limit_lines]), True
    return text, False


def _fence_language(path: Path) -> str:
    return _LANGUAGES.get(path.suffix.lower(), "")


def render(refs: list[Reference], workspace_root: Path) -> str:
    """把引用渲染成要插进消息里的文本。成功的在前，失败的一起列在后面。"""
    blocks: list[str] = []
    problems: list[str] = []

    for ref in refs:
        if ref.kind in ("missing", "outside", "unreadable"):
            problems.append(f"- {ref.raw}：{ref.note}")
            continue

        if ref.kind == "dir":
            blocks.append(f"[引用的目录：{ref.path}]（是个目录，需要时用 list_dir）")
            continue

        if ref.kind == "too_big":
            blocks.append(f"[引用的文件：{ref.note}]")
            continue

        text, truncated = _read_text(ref.resolved, INLINE_LIMIT_LINES)
        language = _fence_language(ref.resolved)
        head = f"[引用的文件：{ref.path}]"
        if truncated:
            head += f"（只给前 {INLINE_LIMIT_LINES} 行，其余需要时用 read_file）"
        blocks.append(f"{head}\n\n```{language}\n{text.rstrip()}\n```")
        ref.inlined = True

    if problems:
        blocks.append("[没能读取的引用]\n" + "\n".join(problems))
    return "\n\n".join(blocks)


def expand(text: str, workspace_root: str | Path) -> tuple[str, list[Reference]]:
    """把消息里的 `@路径` 展开。

    返回 ``(新文本, 引用列表)``。**没有引用时原样返回**——别做任何改动，
    免得不小心动了用户写的普通文本。
    """
    refs = parse(text)
    if not refs:
        return text, []

    resolved = [resolve_one(ref, Path(workspace_root)) for ref in refs]
    body = render(resolved, Path(workspace_root))
    if not body:
        return text, resolved

    return f"{text}\n\n---\n\n{body}", resolved
