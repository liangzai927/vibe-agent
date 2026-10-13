"""文件快照与撤销栈。

思路：在工具真正动盘之前，把目标文件的原内容抓一份到内存里。
一次 agent 步（一个 assistant 轮次）对应一个 Checkpoint，`/undo` 回退整步。

有些工具无法安全撤销（比如 run_bash 跑了任意命令、git checkout 换了工作树），
它们会把 Checkpoint 标记为 opaque，撤销时如实告知用户而不是假装成功。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .diffview import make_diff
from .util import dim

# 超过这个体积就不做快照，直接把检查点标成不可撤销
MAX_SNAPSHOT_BYTES = 5_000_000
# 撤销栈最多保留多少步
DEFAULT_LIMIT = 50


@dataclass
class FileSnapshot:
    path: Path
    existed: bool
    content: str | None  # existed=False 时为 None

    @property
    def code(self) -> str:
        """状态码：A 新增 / M 修改 / D 删除。"""
        if not self.existed:
            return "A"
        if self.content is None:
            return "D"
        return "M"


@dataclass
class Checkpoint:
    index: int
    label: str
    snapshots: dict[Path, FileSnapshot] = field(default_factory=dict)
    opaque: bool = False
    opaque_reason: str = ""

    def is_empty(self) -> bool:
        return not self.snapshots and not self.opaque


class UndoStack:
    def __init__(self, workspace: Path | None = None, limit: int = DEFAULT_LIMIT) -> None:
        self.workspace = workspace
        self.limit = limit
        self._stack: list[Checkpoint] = []
        self._counter = 0

    # ---------------------------------------------------------------- 记录

    def begin(self, label: str) -> Checkpoint:
        self._counter += 1
        return Checkpoint(index=self._counter, label=label)

    def capture(self, checkpoint: Checkpoint, path: Path) -> None:
        """记录 path 改动前的内容。同一个检查点内对同一文件只记第一次。"""
        if path in checkpoint.snapshots:
            return
        try:
            if not path.exists():
                checkpoint.snapshots[path] = FileSnapshot(path, False, None)
                return
            if not path.is_file():
                return
            if path.stat().st_size > MAX_SNAPSHOT_BYTES:
                self.mark_opaque(checkpoint, f"{self.rel(path)} 超过 5MB，无法快照")
                return
            checkpoint.snapshots[path] = FileSnapshot(path, True, path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            self.mark_opaque(checkpoint, f"{self.rel(path)} 不是可读文本，无法快照")

    def mark_opaque(self, checkpoint: Checkpoint, reason: str) -> None:
        checkpoint.opaque = True
        if not checkpoint.opaque_reason:
            checkpoint.opaque_reason = reason

    def commit(self, checkpoint: Checkpoint) -> Checkpoint | None:
        """收尾。空检查点（这一轮没改任何文件）不压栈。"""
        if checkpoint.is_empty():
            return None
        self._stack.append(checkpoint)
        while len(self._stack) > self.limit:
            self._stack.pop(0)
        return checkpoint

    # ---------------------------------------------------------------- 撤销

    def undo(self, count: int = 1) -> tuple[list[Checkpoint], list[str]]:
        """回退最近 count 次**文件改动**。

        注意：没有文件快照的步骤（例如只跑了条命令验证）会被跳过，而不是
        消耗掉一次撤销机会——用户说「撤销」指的是把文件改回来。这类步骤
        如果副作用不可追踪，会通过第二个返回值如实报告。
        """
        if not self._stack:
            return [], []

        count = max(1, count)
        taken: list[Checkpoint] = []
        blocked: list[str] = []

        while self._stack and len(taken) < count:
            checkpoint = self._stack.pop()
            if not checkpoint.snapshots:
                if checkpoint.opaque:
                    blocked.append(
                        f"第 {checkpoint.index} 步（{checkpoint.label}）没有文件改动，"
                        f"未做还原：{checkpoint.opaque_reason}"
                    )
                continue
            taken.append(checkpoint)

        # 顺序很关键：从最新往最老依次还原，最后写入的才是最早的快照。
        # 反过来（老 -> 新）会让最终状态停在中途某一步。
        for checkpoint in taken:
            if checkpoint.opaque:
                blocked.append(
                    f"第 {checkpoint.index} 步（{checkpoint.label}）还执行过无法追踪的操作："
                    f"{checkpoint.opaque_reason}"
                )
            for path, snap in checkpoint.snapshots.items():
                try:
                    if snap.existed:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(snap.content or "", encoding="utf-8")
                    elif path.exists():
                        path.unlink()
                except OSError as exc:
                    blocked.append(f"{self.rel(path)}：还原失败 {exc}")
        return taken, blocked

    # ---------------------------------------------------------------- 查询

    def __len__(self) -> int:
        return len(self._stack)

    def list(self) -> list[Checkpoint]:
        return list(self._stack)

    def rel(self, path: Path) -> str:
        if self.workspace:
            try:
                return path.relative_to(self.workspace).as_posix()
            except ValueError:
                pass
        return str(path)

    def describe(self, checkpoint: Checkpoint) -> str:
        if not checkpoint.snapshots:
            return f"第 {checkpoint.index} 步  {checkpoint.label}  {dim('(无文件改动)')}"
        files = "、".join(f"{s.code} {self.rel(p)}" for p, s in checkpoint.snapshots.items())
        flag = "  ⚠ 不可完全撤销" if checkpoint.opaque else ""
        return f"第 {checkpoint.index} 步  {checkpoint.label}  {files}{flag}"

    def cumulative_diff(self) -> list[tuple[str, str]]:
        """会话开始至今每个文件的总改动，对比最早的快照与当前磁盘内容。"""
        first: dict[Path, FileSnapshot] = {}
        for checkpoint in self._stack:
            for path, snap in checkpoint.snapshots.items():
                first.setdefault(path, snap)

        out: list[tuple[str, str]] = []
        for path, snap in first.items():
            old = snap.content if snap.existed else ""
            try:
                new = path.read_text(encoding="utf-8") if path.exists() else ""
            except (OSError, UnicodeDecodeError):
                continue
            diff_text = make_diff(old, new, self.rel(path))
            if diff_text:
                out.append((self.rel(path), diff_text))
        return out
