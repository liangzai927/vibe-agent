"""跨项目的长期记忆。

**为什么需要它**：``AGENTS.md`` 是项目级的——换个项目就没了。
但有些东西是**跨项目通用**的：你偏好什么语言、用什么包管理器、
内网镜像地址是多少、哪些操作必须先问你。这些每换一个项目就要重讲一遍。

所以记忆存在 ``~/.vibe-agent/memory/``，**一个项目之外的位置**：

    ~/.vibe-agent/memory/
    ├── 偏好.md          "提交信息用中文祈使句"
    ├── 环境.md          "公司内网 npm 镜像：..."
    └── 代码风格.md       "Python 用 ruff，不用 black"

**为什么是 Markdown 文件而不是数据库**：你要能直接打开看、直接改、
直接 git diff。记忆这种东西一旦变成黑盒，人就不敢信它了。

**为什么要限长**：记忆每一轮都会进系统提示词。它涨到几万字的时候，
每一轮都在烧钱。所以有软上限，超了就把内容截断并列出文件名单，
让模型按需自己去读。

**两级作用域**：有些偏好是所有项目通用的（"提交信息用中文祈使句"），
有些只对某个项目成立（"这个库用 Django，不用 FastAPI"）。两者分开存：

    <数据根>/memory/                    全局——所有项目都读
    <数据根>/projects/<项目名-哈希>/memory/   项目级——只有这个项目读

放进提示词时**全局在前、项目级在后**——越具体的越靠后，模型更容易遵守。
两级都写 ``remember`` 时用 ``scope`` 参数区分。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from .paths import agent_home

# 每一轮都进提示词，所以必须有上限
MAX_PROMPT_CHARS = 6000
MAX_ENTRY_CHARS = 4000
MAX_ENTRIES = 40

# 文件名里不允许出现的字符（路径分隔符之类）
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def memory_dir(scope: str = "global", workspace: str | Path | None = None) -> Path:
    """某一级记忆放哪。

    * ``scope="global"``  —— 所有项目都读
    * ``scope="project"`` —— 只有 ``workspace`` 这个项目读

    给项目级却没给 workspace 时退回全局——宁可能读到点东西，
    也别静默写到一个谁都不看的地方去。
    """
    from .paths import global_memory_dir, project_memory_dir

    if scope == "project" and workspace is not None:
        return project_memory_dir(workspace)
    return global_memory_dir()


SCOPES = ("global", "project")


def _scope_of(path: Path) -> str:
    """反查一条记忆属于哪一级（给界面显示用）。"""
    from .paths import global_memory_dir

    try:
        if path.parent == global_memory_dir():
            return "global"
    except OSError:
        pass
    return "project"


def safe_name(name: str) -> str:
    """把用户给的名字变成一个安全的文件名。

    中文原样保留——记忆的标题本来就该是人能看懂的话。
    """
    cleaned = _BAD_NAME.sub("-", (name or "").strip())
    cleaned = cleaned.strip(".- ")
    if not cleaned:
        cleaned = time.strftime("记忆-%Y%m%d-%H%M%S")
    return cleaned[:60]


@dataclass
class Memory:
    name: str
    path: Path
    content: str
    scope: str = "global"

    @property
    def nonempty(self) -> bool:
        return bool(self.content.strip())


def _read_dir(directory: Path, scope: str, include_empty: bool) -> list[Memory]:
    if not directory.is_dir():
        return []
    items: list[Memory] = []
    for path in sorted(directory.glob("*.md")):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        entry = Memory(name=path.stem, path=path, content=content, scope=scope)
        if include_empty or entry.nonempty:
            items.append(entry)
    return items


def list_memories(
    *, include_empty: bool = False, workspace: str | Path | None = None
) -> list[Memory]:
    """列出所有记忆。**全局在前，项目级在后**，同级按文件名排序。"""
    items = _read_dir(memory_dir("global"), "global", include_empty)
    if workspace is not None:
        items += _read_dir(memory_dir("project", workspace), "project", include_empty)
    return items


def render_for_prompt(
    workspace: str | Path | None = None, *, budget: int = MAX_PROMPT_CHARS
) -> str:
    """拼出要注入系统提示词的文本。没有记忆时返回空串。

    超预算时**不静默丢内容**：截断并在末尾说明还有哪些文件、
    以及怎么读全文。悄悄吞掉记忆比不注入更糟——用户会以为记下来了。
    """
    items = list_memories(workspace=workspace)
    if not items:
        return ""

    lines: list[str] = []
    used = 0
    omitted: list[str] = []

    for entry in items[:MAX_ENTRIES]:
        body = entry.content.strip()
        if len(body) > MAX_ENTRY_CHARS:
            body = body[:MAX_ENTRY_CHARS].rstrip() + "\n…（这条太长，已截断）"
        # 标注来源：出问题时用户要能知道「这条是全局的还是这个项目的」
        tag = "全局" if entry.scope == "global" else "本项目"
        block = f"## {entry.name}（{tag}）\n{body}\n"
        if used + len(block) > budget:
            omitted.append(entry.name)
            continue
        lines.append(block)
        used += len(block)

    if len(items) > MAX_ENTRIES:
        omitted.extend(e.name for e in items[MAX_ENTRIES:])

    text = "\n".join(lines).rstrip()
    if omitted:
        names = "、".join(omitted[:10])
        text += (
            f"\n\n（还有 {len(omitted)} 条没放进来：{names}。"
            f"需要时用 read_file 去 memory 目录下读全文。）"
        )
    return text


def remember(
    name: str,
    content: str,
    *,
    mode: str = "replace",
    scope: str = "global",
    workspace: str | Path | None = None,
) -> Path:
    """写入一条记忆。

    ``mode="append"`` 时追加到已有内容后面（适合一点点积累），否则整体覆盖。
    ``scope`` 决定写到全局还是项目级。
    """
    if scope not in SCOPES:
        raise ValueError(f"scope 只能是 {SCOPES}，收到 {scope!r}")
    directory = memory_dir(scope, workspace)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(f"建不出记忆目录 {directory}：{exc}") from exc

    target = directory / f"{safe_name(name)}.md"
    body = (content or "").strip()
    if not body:
        raise ValueError("记忆内容不能为空")

    if mode == "append" and target.is_file():
        try:
            existing = target.read_text(encoding="utf-8").rstrip()
        except OSError:
            existing = ""
        if existing:
            body = f"{existing}\n\n{body}"

    target.write_text(body + "\n", encoding="utf-8")
    return target


def forget(name: str, *, scope: str = "global", workspace: str | Path | None = None) -> bool:
    """删掉一条记忆。不存在返回 False。"""
    target = memory_dir(scope, workspace) / f"{safe_name(name)}.md"
    if not target.is_file():
        return False
    try:
        target.unlink()
    except OSError:
        return False
    return True


def find(name: str, *, scope: str = "global", workspace: str | Path | None = None) -> Memory | None:
    target = memory_dir(scope, workspace) / f"{safe_name(name)}.md"
    if not target.is_file():
        return None
    try:
        return Memory(name=target.stem, path=target, content=target.read_text(encoding="utf-8"))
    except OSError:
        return None


def stats(workspace: str | Path | None = None) -> dict:
    """给设置界面用的概况。"""
    items = list_memories(workspace=workspace)
    return {
        "global_dir": str(memory_dir("global")),
        "project_dir": str(memory_dir("project", workspace)) if workspace else "",
        "count": len(items),
        "global_count": sum(1 for e in items if e.scope == "global"),
        "project_count": sum(1 for e in items if e.scope == "project"),
        "chars": sum(len(e.content) for e in items),
        "names": [f"{e.name}（{'全局' if e.scope == 'global' else '本项目'}）" for e in items][:20],
        "budget": MAX_PROMPT_CHARS,
    }
