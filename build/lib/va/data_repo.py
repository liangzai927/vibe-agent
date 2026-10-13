"""记忆仓库的初始化工作流。

**这个文件是整条流程的唯一入口。** CLI、Web 界面、测试都从这里调，
不要在别处再拼一遍 git 命令——流程只有一份，改的时候才不会漏。

────────────────────────────────────────────────────────────────

三步（和用户在 GitHub 上看到的指引等价，只是内容换成了数据仓库该有的）：

    1. 确认远端是**空白仓库**
       不是空白就中断。往一个已经有内容的仓库里塞提交是有破坏性的，
       也可能把别人的东西覆盖掉——宁可让用户换一个。

    2. 初始化
       建 README + memory/ + projects/ → 首次提交 → 推送并设置 upstream。

    3. 之后每次回复结束自动同步
       由 ``auto_sync_enabled()`` 和 ``sync_now()`` 支持。

────────────────────────────────────────────────────────────────

**为什么要抽出来**：这三步横跨 git clone / 建目录结构 / 提交 / 推送，
散在 Web 层里会变成一堆没法单独测、也没法在别处复用的 subprocess 调用。
收在这里之后：

* 每一步都有名字和结果，界面能逐步显示「现在在做什么」
* 失败点明确（是「不是空白仓库」还是「推送失败」），提示能对症
* 换 CLI 或者其他界面时直接调用，不用重写

底层 git 动作仍然复用一个地方（``session_sync``），这里只负责「什么顺序、
每步什么含义、失败了怎么解释」。
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import session_sync

#: 数据仓库的标识文件。**判断一个非空仓库能不能接管，靠的就是它。**
#: 不能只看「有没有 memory/ 目录」——空目录 git 根本不跟踪，
#: 而别人的仓库里也可能恰好有个叫 memory 的目录。
MARKER_FILE = ".vibe-agent.json"
MARKER_KIND = "vibe-agent-data"
MARKER_VERSION = 1

# --------------------------------------------------------------- 步骤定义

STEP_CHECK = "check"
STEP_INIT = "init"
STEP_MIGRATE = "migrate"
STEP_SYNC = "sync"

#: 三步的固定说明。界面直接拿去显示，保证「界面上说的」和「实际做的」一致——
#: 两边各写一份文案，早晚会改歪一个。
STEPS: tuple[tuple[str, str, str], ...] = (
    (
        STEP_CHECK,
        "确认远端是空白仓库",
        "不是空白就中断，绝不动已有内容的仓库",
    ),
    (
        STEP_INIT,
        "建好目录结构并首次推送",
        "README + memory/ + projects/，提交后推送并设置 upstream",
    ),
    (
        STEP_MIGRATE,
        "把已有的记忆和会话搬过去",
        "只复制不删除；新位置已有的文件不覆盖",
    ),
    (
        STEP_SYNC,
        "之后每次回复结束自动同步",
        "推送在后台进行，失败不影响对话",
    ),
)


def describe_steps() -> list[dict[str, str]]:
    """给界面用的步骤清单。"""
    return [
        {"key": key, "title": title, "detail": detail} for key, title, detail in STEPS
    ]


# --------------------------------------------------------------- 结果类型


@dataclass
class StepOutcome:
    key: str
    title: str
    ok: bool
    detail: str = ""

    def describe(self) -> str:
        mark = "✓" if self.ok else "✗"
        return f"{mark} {self.title}" + (f"：{self.detail}" if self.detail else "")


@dataclass
class WorkflowResult:
    ok: bool
    steps: list[StepOutcome] = field(default_factory=list)
    url: str = ""
    message: str = ""
    detail: str = ""

    @property
    def failed_step(self) -> StepOutcome | None:
        for step in self.steps:
            if not step.ok:
                return step
        return None

    def describe(self) -> str:
        if self.ok:
            return self.message or "完成"
        failed = self.failed_step
        if failed is not None:
            return failed.detail or f"{failed.title}失败"
        return self.detail or "失败"

    def transcript(self) -> str:
        """整条流程的文字记录，适合贴给用户看。"""
        return "\n".join(step.describe() for step in self.steps)


def _outcome(key: str, ok: bool, detail: str = "") -> StepOutcome:
    title = next((t for k, t, _ in STEPS if k == key), key)
    return StepOutcome(key=key, title=title, ok=ok, detail=detail)


# --------------------------------------------------------------- 仓库标识


def write_marker(root: Path) -> Path:
    """在仓库里留下我们的标识。"""
    target = root / MARKER_FILE
    target.write_text(
        json.dumps(
            {
                "kind": MARKER_KIND,
                "version": MARKER_VERSION,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


def read_marker(root: Path) -> dict | None:
    """读标识。不是我们的仓库（或文件坏了）返回 None。"""
    target = root / MARKER_FILE
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("kind") != MARKER_KIND:
        return None
    return data


def describe_foreign_repo(root: Path) -> str:
    """一个非空、又不是我们的仓库——尽量说清楚里面是什么，好让用户判断。"""
    try:
        names = sorted(
            p.name for p in root.iterdir()
            if p.name not in (".git", ".gitignore") and not p.name.startswith(".")
        )
    except OSError:
        return "仓库里有内容"

    if not names:
        return "仓库里有提交，但工作区几乎是空的"

    shown = "、".join(names[:6])
    more = f" 等 {len(names)} 项" if len(names) > 6 else ""
    return f"仓库里已经有这些内容：{shown}{more}"


# --------------------------------------------------------------- 迁移


@dataclass
class MigrationResult:
    copied: int = 0
    skipped: int = 0
    from_root: str = ""
    to_root: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def describe(self) -> str:
        if self.error:
            return self.error
        if not self.copied and not self.skipped:
            return "没有需要搬的内容"
        parts = [f"搬过来 {self.copied} 个文件"]
        if self.skipped:
            parts.append(f"{self.skipped} 个已存在，跳过（不覆盖）")
        return "，".join(parts)


#: 数据根下需要跟着走的顶层目录
MOVABLE = ("memory", "projects")


def migrate(from_root: Path, to_root: Path) -> MigrationResult:
    """把旧的记忆和会话复制到新位置。

    **只复制，不删除**：切回旧位置或者中途失败时，原来的东西还在，
    不会因为一次误操作就丢了。想清理旧的自己删即可。

    **已存在的文件不覆盖**：新位置可能已经有内容（比如刚克隆下来的仓库
    里就有别人同步上去的记录），覆盖会把人家的东西冲掉。
    """
    result = MigrationResult(from_root=str(from_root), to_root=str(to_root))

    try:
        from_root = Path(from_root).resolve()
        to_root = Path(to_root).resolve()
    except OSError as exc:
        result.error = f"路径解析失败：{exc}"
        return result

    if from_root == to_root:
        return result

    if not from_root.is_dir():
        return result

    for name in MOVABLE:
        source = from_root / name
        if not source.is_dir():
            continue
        for item in source.rglob("*"):
            if not item.is_file():
                continue
            # 克隆目录里的 .git 不能碰
            if ".git" in item.parts:
                continue
            relative = item.relative_to(from_root)
            destination = to_root / relative
            if destination.exists():
                result.skipped += 1
                continue
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, destination)
                result.copied += 1
            except OSError as exc:
                result.error = f"复制 {relative} 失败：{exc}"
                return result

    return result


# --------------------------------------------------------------- 第一步


#: 检查远端之后该做什么
ACTION_INIT = "init"      # 空白仓库 → 初始化
ACTION_ADOPT = "adopt"    # 已经是本 agent 的仓库 → 接管并拉取


def inspect_remote(url: str) -> tuple[bool, str, str]:
    """判断远端是什么情况。

    返回 ``(可用吗, 说明, 该做什么)``，``该做什么`` 是 ``ACTION_INIT``
    或 ``ACTION_ADOPT``。

    三种情况：

    * **空白** → 初始化
    * **非空，但带本 agent 的标识** → 接管：克隆下来，把里面的会话拉过来
    * **非空，也没有标识** → 拒绝。可能是别人的仓库，也可能是用户自己
      放的别的东西——不管是哪种，都不该往里塞我们的记录
    """
    url = (url or "").strip()
    if not url:
        return False, "没填 git 地址", ""

    empty, why = session_sync.remote_is_empty(url)
    if empty:
        return True, "远端是空白的，将初始化成记忆仓库", ACTION_INIT

    if "没填地址" in why:
        return False, why, ""
    if "已经有分支" not in why:
        # 连不上 / 没权限 / 地址不对
        return False, f"连不上这个仓库：{why}", ""

    # 非空——克隆下来看看是不是我们的
    ok, detail, marker, inside = _peek_remote(url)
    if not ok:
        return False, detail, ""

    if marker is not None:
        created = marker.get("created") or "（没记时间）"
        return True, f"已经是本 agent 的记忆仓库（建于 {created}），将接管并拉取记录", ACTION_ADOPT

    return False, (
        f"这个仓库不是本 agent 的记忆仓库，不能用。{inside}\n"
        f"请换一个**空白仓库**——新建时不要勾选 README / .gitignore / license。"
    ), ""


def _peek_remote(url: str) -> tuple[bool, str, dict | None, str]:
    """把远端克隆下来看一眼。

    返回 ``(成功吗, 说明, 标识, 仓库里有什么)``。

    最后一个要在**删除克隆之前**取——不然描述一个已经不存在的目录，
    只能得到一句「仓库里有内容」，对用户毫无帮助。
    """
    target = session_sync.remote_dir(url, kind="data")
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)

    ready = session_sync.ensure_clone(url, kind="data")
    if not ready.ok:
        return False, f"读不了这个仓库：{ready.detail}", None, ""

    marker = read_marker(target)
    if marker is not None:
        return True, "", marker, ""

    inside = describe_foreign_repo(target)
    # 不是我们的，把克隆清掉，别留着占地方
    shutil.rmtree(target, ignore_errors=True)
    return True, "", None, inside


# --------------------------------------------------------------- 第二步


def initialise(url: str, *, timeout: int = session_sync.DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """建结构 → 写标识 → 首次提交 → 推送。**调用前必须先过 inspect_remote。**"""
    result = session_sync.bootstrap(url, kind="data", timeout=timeout)
    if not result.ok:
        return False, result.describe()

    # 标识文件要进首次提交，所以补一次提交再推
    target = session_sync.remote_dir(url, kind="data")
    try:
        write_marker(target)
        session_sync.commit_and_push(target, "chore: 标记为 vibe-agent 记忆仓库")
    except Exception as exc:  # noqa: BLE001
        return False, f"标识写入或推送失败：{exc}"

    return True, result.describe()


def adopt(url: str) -> tuple[bool, str]:
    """接管一个已经是我们的仓库：确保克隆在、并拉取最新。"""
    ready = session_sync.ensure_clone(url, kind="data")
    if not ready.ok:
        return False, ready.detail
    return True, "已拉取仓库里的记录"


# --------------------------------------------------------------- 第三步


def auto_sync_enabled(config) -> bool:
    """要不要在每轮回复后自动同步。

    **配了仓库就默认同步**——用户特意填了个 git 地址，意思就是要同步。
    想关掉得显式设 ``VA_AUTO_SYNC=0``。
    """
    remote = (getattr(config, "data_remote", "") or "").strip()
    if not remote:
        return False
    return bool(getattr(config, "auto_sync", True))


def sync_now(config) -> session_sync.SyncResult:
    """立刻同步一次。没配远端时返回一个说明清楚的结果，不抛异常。"""
    url = (getattr(config, "data_remote", "") or "").strip()
    if not url:
        return session_sync.SyncResult(
            False, detail="还没配记忆仓库。在设置里填一个空白仓库的地址。"
        )
    return session_sync.sync(url, kind="data")


# --------------------------------------------------------------- 完整流程


def run(
    url: str,
    *,
    local_root: Path | None = None,
    timeout: int = session_sync.DEFAULT_TIMEOUT,
) -> WorkflowResult:
    """跑完整条工作流：判断 → 初始化或接管 → 把本地已有内容搬过去。

    ``local_root`` 是**切换前的数据根**。给了就把里面的记忆和会话
    复制到新位置——换存储位置不该把之前积累的东西落下。
    """
    url = (url or "").strip()
    if not url:
        return WorkflowResult(
            ok=False,
            steps=[_outcome(STEP_CHECK, False, "没填 git 地址")],
            detail="没填 git 地址",
        )

    result = WorkflowResult(ok=False, url=url)

    # ── 第一步：远端能不能用，该初始化还是接管 ──────────────
    ok, reason, action = inspect_remote(url)
    result.steps.append(_outcome(STEP_CHECK, ok, reason))
    if not ok:
        result.detail = reason
        return result

    # ── 第二步：初始化 或 接管 ─────────────────────────────
    if action == ACTION_ADOPT:
        ok, detail = adopt(url)
    else:
        ok, detail = initialise(url, timeout=timeout)

    result.steps.append(_outcome(STEP_INIT, ok, detail))
    if not ok:
        result.detail = detail
        return result

    # ── 把切换前的东西搬过来 ───────────────────────────────
    if local_root is not None:
        new_root = session_sync.remote_dir(url, kind="data")
        moved = migrate(Path(local_root), new_root)
        if not moved.ok:
            result.steps.append(_outcome(STEP_MIGRATE, False, moved.error))
            result.detail = moved.error
            return result
        result.steps.append(_outcome(STEP_MIGRATE, True, moved.describe()))

    result.ok = True
    verb = "接管" if action == ACTION_ADOPT else "初始化"
    result.message = f"已{verb}。记忆和会话会同步到 {url}"
    return result


def clean_local_clone(url: str, *, kind: str = "data") -> bool:
    """删掉本地克隆。换仓库或者本地状态坏了时用。

    只删 ``~/.vibe-agent/remotes/`` 下我们自己建的那份，不碰用户的任何东西。
    """
    target = session_sync.remote_dir(url, kind=kind)
    if not target.exists():
        return False
    shutil.rmtree(target, ignore_errors=True)
    return True
