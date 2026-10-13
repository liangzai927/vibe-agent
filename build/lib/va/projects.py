"""工作区注册表 + 跨工作区的会话索引。

**为什么需要注册表**：以前工作区是启动参数（``va web -C <目录>``），
进程自己知道在哪儿就够了。改成界面里选工作区之后，得记住「有哪些工作区」——
而工作区可以散落在磁盘任何地方，**扫不出来**，只能记。

**为什么需要会话索引**：``SessionStore.sessions()`` 只列**一个**目录。
侧栏要按工作区分组显示所有会话，得跨目录扫。

────────────────────────────────────────────────────────────────

存储：

    <数据根>/projects.json          注册表
    <数据根>/projects/<slug>/sessions/*.jsonl   会话（slug 是路径的纯函数）

**slug 用的是 ``paths.project_slug()``，一个纯函数**——不能换成自增 ID 之类，
因为数据目录已经按它分好了，改了会找不到老数据。

**三种分组**（和界面上那三块对应）：

    workspace    用户添加的项目文件夹
    default      默认工作区（``<数据根>/workspace``）
    ungrouped    未分组（``<数据根>/scratch``）
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .paths import (
    data_root,
    default_workspace_dir,
    project_dir,
    project_slug,
    projects_index_path,
    scratch_dir,
)

#: 三种分组
GROUP_WORKSPACE = "workspace"
GROUP_DEFAULT = "default"
GROUP_UNGROUPED = "ungrouped"

INDEX_VERSION = 1

#: 读预览时最多往回看多少字节。会话文件可能很大，只取尾巴。
PREVIEW_TAIL_BYTES = 8192


# --------------------------------------------------------------- 注册表


@dataclass
class WorkspaceEntry:
    path: str
    slug: str = ""
    added_at: float = 0.0
    last_opened: float = 0.0
    pinned: bool = False

    def __post_init__(self) -> None:
        if not self.slug:
            self.slug = project_slug(self.path)
        if not self.added_at:
            self.added_at = time.time()

    @property
    def name(self) -> str:
        return Path(self.path).name or self.path

    @property
    def exists(self) -> bool:
        """目录还在不在。被删掉的工作区要能看出来，而不是打开时报错。"""
        try:
            return Path(self.path).is_dir()
        except OSError:
            return False


@dataclass
class Registry:
    workspaces: list[WorkspaceEntry] = field(default_factory=list)
    default_workspace: str = ""

    # ---- 读写

    @classmethod
    def load(cls, path: Path | None = None) -> "Registry":
        target = path or projects_index_path()
        if not target.is_file():
            return cls()
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 坏了就当空的——注册表丢了顶多是「要重新添加工作区」，
            # 而会话数据不在这个文件里，不会跟着坏
            return cls()

        entries: list[WorkspaceEntry] = []
        for item in raw.get("workspaces") or []:
            if not isinstance(item, dict) or not item.get("path"):
                continue
            entries.append(
                WorkspaceEntry(
                    path=str(item["path"]),
                    slug=str(item.get("slug") or ""),
                    added_at=float(item.get("added_at") or 0),
                    last_opened=float(item.get("last_opened") or 0),
                    pinned=bool(item.get("pinned")),
                )
            )
        return cls(workspaces=entries, default_workspace=str(raw.get("default_workspace") or ""))

    def save(self, path: Path | None = None) -> None:
        target = path or projects_index_path()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(
                    {
                        "version": INDEX_VERSION,
                        "default_workspace": self.default_workspace,
                        "workspaces": [asdict(entry) for entry in self.workspaces],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            # 写不进去不该让整个界面挂掉——注册表是缓存性质的东西，
            # 内存里那份仍然可用
            pass

    # ---- 查询

    def all(self) -> list[WorkspaceEntry]:
        """固定的排前面，其余按最近打开排。"""
        return sorted(
            self.workspaces,
            key=lambda e: (not e.pinned, -e.last_opened, e.name),
        )

    def find(self, slug: str) -> WorkspaceEntry | None:
        for entry in self.workspaces:
            if entry.slug == slug:
                return entry
        return None

    def find_path(self, path: str | Path) -> WorkspaceEntry | None:
        try:
            slug = project_slug(path)
        except OSError:
            return None
        return self.find(slug)

    # ---- 修改

    def add(self, path: str | Path, *, name: str = "") -> WorkspaceEntry:
        """加一个工作区。**幂等**——同一个路径重复加只会更新。

        路径不存在时照样记下来（用户可能还没建，或者盘还没挂）。
        界面上会标成「找不到」，而不是加不进去。
        """
        raw = str(Path(path).expanduser())
        slug = project_slug(raw)
        existing = self.find(slug)
        if existing is not None:
            existing.last_opened = time.time()
            if name:
                existing.path = raw
            self.save()
            return existing

        entry = WorkspaceEntry(path=raw)
        entry.last_opened = time.time()
        self.workspaces.append(entry)
        self.save()
        return entry

    def remove(self, slug: str) -> bool:
        """从注册表移除。**不删数据**——会话和记忆都还在磁盘上。"""
        before = len(self.workspaces)
        self.workspaces = [e for e in self.workspaces if e.slug != slug]
        if len(self.workspaces) == before:
            return False
        self.save()
        return True

    def touch(self, slug: str) -> None:
        entry = self.find(slug)
        if entry is None:
            return
        entry.last_opened = time.time()
        self.save()

    def set_default(self, path: str | Path) -> None:
        """设默认工作区。**空串表示「没设」**，不是「当前目录」。

        ``str(Path(""))`` 是 ``"."``，直接存下去会把默认工作区变成
        「启动时所在目录」——那是随启动位置变的东西，正是要避免的。
        """
        raw = str(path or "").strip()
        self.default_workspace = str(Path(raw).expanduser()) if raw else ""
        self.save()

    def effective_default(self) -> str:
        """默认工作区实际指哪。

        没配过就用 ``<数据根>/workspace``——一个真实存在的空目录，
        而不是「当前目录」这种随启动位置变的东西。
        """
        if self.default_workspace:
            return self.default_workspace
        return str(default_workspace_dir())


# --------------------------------------------------------------- 会话索引


@dataclass
class SessionEntry:
    id: str
    workspace: str
    slug: str
    title: str = ""
    preview: str = ""
    updated_at: float = 0.0
    message_count: int = 0
    model: str = ""
    path: str = ""

    @property
    def group(self) -> str:
        """三种分组之一。靠**路径**判断，不靠目录名。"""
        try:
            resolved = Path(self.workspace).expanduser().resolve()
        except OSError:
            return GROUP_WORKSPACE
        if resolved == scratch_dir().resolve():
            return GROUP_UNGROUPED
        if resolved == default_workspace_dir().resolve():
            return GROUP_DEFAULT
        return GROUP_WORKSPACE


def _read_tail_preview(path: Path) -> str:
    """从文件尾部找最后一条用户消息。

    只读尾巴是有意的：会话文件可能几 MB，而侧栏只需要一行字。
    按行从后往前找，遇到空行或坏行就跳过。
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > PREVIEW_TAIL_BYTES:
                handle.seek(size - PREVIEW_TAIL_BYTES)
                handle.readline()  # 丢掉可能被截断的半行
            chunk = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""

    for line in reversed(chunk.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("type") != "message":
            continue
        message = record.get("message") or {}
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip().replace("\n", " ")[:120]
    return ""


#: 读文件头时最多看多少字节。meta 和 title 都在前面。
HEAD_BYTES = 8192


def read_head(path: Path) -> tuple[dict | None, str]:
    """读文件头，返回 ``(meta, title)``。

    为什么要读头而不是只读第一行：**标题是单独一条记录**，
    ``set_title`` 写的是 ``{"type":"title",...}``，不会回头改首行。
    它通常紧跟在第一条消息后面，所以扫文件头就能拿到。

    首行的 ``type`` 是 ``"session"`` 不是 ``"meta"``——这是落盘格式，别猜。
    """
    meta: dict | None = None
    title = ""
    try:
        with path.open("rb") as handle:
            chunk = handle.read(HEAD_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None, ""

    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            # 头部被字节数截断时最后一行可能不完整，忽略即可
            continue
        kind = record.get("type")
        if meta is None and kind in ("session", "meta"):
            meta = record
        elif kind == "title":
            # 后面的覆盖前面的——用户改过名就以最后一次为准
            title = str(record.get("title") or "")
    return meta, title


def index_sessions(*, limit: int = 500) -> list[SessionEntry]:
    """跨工作区扫出所有会话，按最近更新排。

    **不走 ``SessionStore.sessions()``**——那个只列一个目录。
    这里直接扫 ``<数据根>/projects/*/sessions/*.jsonl``：
    工作区数据本来就按 slug 分在那儿，一个 glob 就够。

    ``updated_at`` 用文件 mtime——会话每次追加都会更新它，
    比 meta 里的 ``updated_at``（只在特定时机才写）更贴近「上次活跃」。
    """
    # **必须是 data_root() 而不是 agent_home()**：配了记忆仓库时，
    # 会话数据在仓库的克隆里（``<克隆>/projects/...``），
    # 而不是本地的 ``~/.vibe-agent/projects``。扫错了地方就会
    # 「明明有会话，侧栏是空的」——而且新建的也不出现。
    root = data_root() / "projects"
    if not root.is_dir():
        return []

    entries: list[SessionEntry] = []
    try:
        candidates = list(root.glob("*/sessions/*.jsonl"))
    except OSError:
        return []

    candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)

    for path in candidates[:limit]:
        meta, title = read_head(path)
        if meta is None:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        entries.append(
            SessionEntry(
                id=str(meta.get("id") or path.stem),
                workspace=str(meta.get("workspace") or ""),
                slug=path.parent.parent.name,
                title=title or str(meta.get("title") or ""),
                preview=_read_tail_preview(path),
                updated_at=mtime,
                message_count=int(meta.get("message_count") or 0),
                model=str(meta.get("model") or ""),
                path=str(path),
            )
        )
    return entries


def group_sessions(entries: list[SessionEntry]) -> dict[str, list[SessionEntry]]:
    """按分组摊开。空组也要有键，界面才好渲染固定的三块。"""
    out: dict[str, list[SessionEntry]] = {
        GROUP_DEFAULT: [],
        GROUP_UNGROUPED: [],
        GROUP_WORKSPACE: [],
    }
    for entry in entries:
        out.setdefault(entry.group, []).append(entry)
    return out


def relative_time(timestamp: float, *, now: float | None = None) -> str:
    """「3天」「2分钟」这种。侧栏那行时间用。"""
    if not timestamp:
        return ""
    now = now if now is not None else time.time()
    seconds = max(0, int(now - timestamp))
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    if seconds < 86400:
        return f"{seconds // 3600} 小时"
    if seconds < 86400 * 30:
        return f"{seconds // 86400} 天"
    return time.strftime("%Y-%m-%d", time.localtime(timestamp))


def session_dir_for(workspace: str | Path) -> Path:
    """某个工作区的会话目录。建索引和建会话共用同一个算法。"""
    return project_dir(workspace) / "sessions"
