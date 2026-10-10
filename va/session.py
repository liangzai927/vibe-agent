"""会话持久化。

存储格式是 JSONL —— 每轮往文件尾追加一行，而不是把整个历史重写一遍。
好处：写入是 O(1)，历史到几万 token 也不会卡；中途崩溃最多丢最后一行。

记录类型：
    session  —— 会话头（工作区、模型、git HEAD、创建时间）
    message  —— 一条对话消息（user / assistant / tool）
    compact  —— 上下文压缩后的完整消息快照（整体替换，不是追加）
    end      —— 会话正常结束（步数、结束原因）

恢复时按顺序重放：message 追加、compact 整体替换。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from .paths import sessions_dir

SESSION_DIR_NAME = Path(".va") / "sessions"
FILE_MODE = 0o600


class SessionDirError(Exception):
    """会话目录建不出来或写不进去。

    单独一个类型，因为调用方需要区分「记录存不下」和「别的错」——
    前者不该让整个 agent 挂掉，降级成不记录也要能干活。
    """


@dataclass
class SessionMeta:
    id: str
    workspace: str
    model: str
    created_at: float
    updated_at: float = 0.0
    git_head: str | None = None
    steps: int = 0
    end_reason: str = ""
    title: str = ""
    message_count: int = 0

    @property
    def created_text(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.created_at))

    @property
    def age_text(self) -> str:
        delta = max(0.0, time.time() - (self.updated_at or self.created_at))
        if delta < 60:
            return "刚刚"
        if delta < 3600:
            return f"{int(delta // 60)} 分钟前"
        if delta < 86400:
            return f"{int(delta // 3600)} 小时前"
        return f"{int(delta // 86400)} 天前"

    def one_line(self) -> str:
        title = self.title or "(未命名会话)"
        return f"{self.id}  {title}  ·  {self.steps} 步  ·  {self.age_text}"


def git_head(workspace: Path) -> str | None:
    """当前 HEAD 短哈希；不是 git 仓库或无提交时返回 None。"""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


class SessionStore:
    def __init__(self, path: Path, meta: SessionMeta) -> None:
        self.path = path
        self.meta = meta
        self._handle = None

    # ------------------------------------------------------------ 创建/打开

    @staticmethod
    def legacy_dir(workspace: Path) -> Path:
        """旧位置：项目内的 .va/sessions。"""
        return Path(workspace) / SESSION_DIR_NAME

    @staticmethod
    def default_dir(workspace: Path) -> Path:
        """会话存哪。

        默认在项目之外（``~/.vibe-agent/sessions/<项目名-哈希>/``），
        这样 ``git status`` 不会一直被会话文件弄脏。
        ``VA_SESSION_DIR`` 可以把它们指到 git 仓库或同步盘里。

        **旧位置里已经有会话的话就继续用它**——装作没看见等于把用户的
        历史弄丢，而迁移这种事该由用户决定什么时候做。
        """
        # 远端优先：配了 git 地址就用本地克隆那份
        remote = os.environ.get("VA_SESSION_REMOTE", "").strip()
        if remote:
            from .session_sync import remote_dir

            return remote_dir(remote)

        override = os.environ.get("VA_SESSION_DIR", "").strip()
        if override:
            return Path(override).expanduser()

        legacy = SessionStore.legacy_dir(workspace)
        try:
            if legacy.is_dir() and any(legacy.glob("*.jsonl")):
                return legacy
        except OSError:
            pass

        return sessions_dir(workspace)

    @staticmethod
    def migration_hint(workspace: Path) -> str:
        """还在用旧位置时给一句提示。空字符串表示不需要提示。"""
        legacy = SessionStore.legacy_dir(workspace)
        try:
            count = len(list(legacy.glob("*.jsonl"))) if legacy.is_dir() else 0
        except OSError:
            return ""
        if not count:
            return ""
        # 两边都 resolve，给出的命令才好直接复制粘贴——
        # macOS 上 /tmp 和 /private/tmp 混着写看着很怪。
        try:
            target = sessions_dir(workspace).resolve()
            source = legacy.resolve()
        except (OSError, RuntimeError):
            target, source = sessions_dir(workspace), legacy
        return (
            f"会话还在项目内的 {SESSION_DIR_NAME.as_posix()}/（{count} 个）。"
            f"新会话默认写到项目外，想一起搬过去：\n"
            f"    mkdir -p {target} && mv {source}/*.jsonl {target}/ && rmdir {source}"
        )

    @classmethod
    def create(
        cls,
        workspace: Path,
        model: str,
        session_dir: Path | None = None,
    ) -> "SessionStore":
        directory = Path(session_dir) if session_dir else cls.default_dir(workspace)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SessionDirError(
                f"建不出会话目录 {directory}：{exc}\n"
                f"（可以用 VA_SESSION_DIR 换个位置，或者直接设成空字符串停用记录）"
            ) from exc
        session_id = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()
        meta = SessionMeta(
            id=session_id,
            workspace=str(workspace),
            model=model,
            created_at=time.time(),
            updated_at=time.time(),
            git_head=git_head(Path(workspace)),
        )
        store = cls(directory / f"{session_id}.jsonl", meta)
        store._append_raw({"type": "session", **store._meta_dict()})
        try:
            os.chmod(store.path, FILE_MODE)
        except OSError:
            pass
        return store

    @classmethod
    def open(cls, workspace: Path, session_id: str, session_dir: Path | None = None) -> "SessionStore":
        directory = Path(session_dir) if session_dir else cls.default_dir(workspace)
        path = directory / f"{session_id}.jsonl"
        if not path.is_file():
            raise FileNotFoundError(f"找不到会话 {session_id}（{path}）")
        meta, _, _ = cls.read(path)
        return cls(path, meta)

    @classmethod
    def sessions(cls, workspace: Path, session_dir: Path | None = None) -> list[SessionMeta]:
        directory = Path(session_dir) if session_dir else cls.default_dir(workspace)
        if not directory.is_dir():
            return []
        metas: list[SessionMeta] = []
        for path in sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                meta, _, _ = cls.read(path)
            except (OSError, ValueError):
                continue
            metas.append(meta)
        return metas

    @classmethod
    def latest(cls, workspace: Path, session_dir: Path | None = None) -> "SessionStore | None":
        directory = Path(session_dir) if session_dir else cls.default_dir(workspace)
        if not directory.is_dir():
            return None
        candidates = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            return None
        return cls.open(workspace, candidates[0].stem, session_dir)

    # ------------------------------------------------------------ 写入

    def _meta_dict(self) -> dict[str, Any]:
        return {
            "id": self.meta.id,
            "workspace": self.meta.workspace,
            "model": self.meta.model,
            "created_at": self.meta.created_at,
            "git_head": self.meta.git_head,
        }

    def _append_raw(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        self.meta.updated_at = time.time()

    def append_message(self, message: dict[str, Any]) -> None:
        if message.get("role") == "system":
            return  # 系统提示词每次重建，不落盘
        self._append_raw({"type": "message", "message": message, "ts": time.time()})
        self.meta.message_count += 1

    def append_compact(self, messages: list[dict[str, Any]]) -> None:
        """压缩后整体替换：把当前完整消息列表存一份快照。"""
        payload = [m for m in messages if m.get("role") != "system"]
        self._append_raw({"type": "compact", "messages": payload, "ts": time.time()})
        self.meta.message_count = len(payload)

    def append_plan(self, plan: dict[str, Any] | None) -> None:
        """记录任务计划。

        计划必须落盘：它通常建在很早的步骤里，恢复会话时如果丢了，
        模型会重新规划一遍，前面走过的路白走。
        """
        self._append_raw({"type": "plan", "plan": plan, "ts": time.time()})

    def append_end(self, reason: str, steps: int) -> None:
        self.meta.end_reason = reason
        self.meta.steps = steps
        self._append_raw({"type": "end", "reason": reason, "steps": steps, "ts": time.time()})

    def set_title(self, title: str) -> None:
        self.meta.title = title.strip()[:80]
        self._append_raw({"type": "title", "title": self.meta.title})

    # ------------------------------------------------------------ 读取

    @staticmethod
    def read(path: Path) -> tuple[SessionMeta, list[dict[str, Any]], dict[str, Any] | None]:
        """解析会话文件，返回 (元数据, 去掉 system 的消息列表, 计划或 None)。"""
        meta: SessionMeta | None = None
        messages: list[dict[str, Any]] = []
        plan: dict[str, Any] | None = None
        saw_plan_record = False

        for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue  # 崩溃时可能留下半行，跳过即可

            kind = record.get("type")
            if kind == "session":
                meta = SessionMeta(
                    id=record.get("id", path.stem),
                    workspace=record.get("workspace", ""),
                    model=record.get("model", ""),
                    created_at=record.get("created_at", 0.0),
                    updated_at=record.get("created_at", 0.0),
                    git_head=record.get("git_head"),
                )
            elif kind == "message" and isinstance(record.get("message"), dict):
                messages.append(record["message"])
            elif kind == "compact" and isinstance(record.get("messages"), list):
                messages = list(record["messages"])
            elif kind == "plan":
                # 允许显式清空计划（plan 为 null），所以要用「见过记录」而不是
                # 「plan 非空」来判断最终状态
                saw_plan_record = True
                raw_plan = record.get("plan")
                plan = raw_plan if isinstance(raw_plan, dict) else None
            elif kind == "title":
                if meta:
                    meta.title = str(record.get("title", ""))
            elif kind == "end":
                if meta:
                    meta.end_reason = str(record.get("reason", ""))
                    meta.steps = int(record.get("steps", 0))

        if meta is None:
            meta = SessionMeta(
                id=path.stem,
                workspace="",
                model="",
                created_at=path.stat().st_mtime,
                updated_at=path.stat().st_mtime,
            )
        meta.message_count = len(messages)
        meta.updated_at = path.stat().st_mtime
        return meta, messages, (plan if saw_plan_record else None)

    def load(self) -> tuple[SessionMeta, list[dict[str, Any]], dict[str, Any] | None]:
        return self.read(self.path)

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None
