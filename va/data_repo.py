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

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import session_sync

# --------------------------------------------------------------- 步骤定义

STEP_CHECK = "check"
STEP_INIT = "init"
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


# --------------------------------------------------------------- 第一步


def check_remote(url: str) -> tuple[bool, str]:
    """远端是不是空白仓库。

    返回 ``(可用吗, 原因)``。连不上、没权限、已有分支都算「不可用」，
    但原因不同——调用方直接把原因给用户看，用户才知道该改哪儿。
    """
    empty, why = session_sync.remote_is_empty(url)
    if empty:
        return True, "远端是空白的"

    # 把「不能用的原因」翻译成「该怎么办」
    if "没填地址" in why:
        return False, why
    if "已经有分支" in why:
        return False, (
            f"{why}。请换一个**空白仓库**——记忆和会话会写进去，"
            f"混在已有内容里既乱又危险。"
            f"（在 GitHub 上新建仓库时不要勾选 README / .gitignore / license，"
            f"那三样会让仓库一开始就不是空的。）"
        )
    return False, f"连不上这个仓库：{why}"


# --------------------------------------------------------------- 第二步


def initialise(url: str, *, timeout: int = session_sync.DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """建结构 → 首次提交 → 推送。**调用前必须先过 check_remote。**"""
    result = session_sync.bootstrap(url, kind="data", timeout=timeout)
    return result.ok, result.describe()


# --------------------------------------------------------------- 第三步


def auto_sync_enabled(config) -> bool:
    """要不要在每轮回复后自动同步。两个条件都得满足。"""
    return bool(getattr(config, "auto_sync", False) and getattr(config, "data_remote", ""))


def sync_now(config) -> session_sync.SyncResult:
    """立刻同步一次。没配远端时返回一个说明清楚的结果，不抛异常。"""
    url = (getattr(config, "data_remote", "") or "").strip()
    if not url:
        return session_sync.SyncResult(
            False, detail="还没配记忆仓库。在设置里填一个空白仓库的地址。"
        )
    return session_sync.sync(url, kind="data")


# --------------------------------------------------------------- 完整流程


def run(url: str, *, timeout: int = session_sync.DEFAULT_TIMEOUT) -> WorkflowResult:
    """跑完整条初始化工作流（第一步 + 第二步）。

    第三步（自动同步）是个持续行为，由配置开关控制，成功后会由调用方打开。

    **不会在中途留下半成品**：第二步失败时，远端要么完全没动，
    要么已经推上去了——不存在「本地建好了但远端还是空的」却不告诉用户的状态。
    """
    url = (url or "").strip()
    if not url:
        return WorkflowResult(
            ok=False,
            steps=[_outcome(STEP_CHECK, False, "没填 git 地址")],
            detail="没填 git 地址",
        )

    result = WorkflowResult(ok=False, url=url)

    # ── 第一步：确认是空白仓库 ──────────────────────────────
    ok, reason = check_remote(url)
    result.steps.append(_outcome(STEP_CHECK, ok, reason))
    if not ok:
        result.detail = reason
        return result

    # ── 第二步：建结构并推送 ────────────────────────────────
    ok, detail = initialise(url, timeout=timeout)
    result.steps.append(_outcome(STEP_INIT, ok, detail))
    if not ok:
        result.detail = detail
        return result

    result.ok = True
    result.message = f"已初始化。记忆和会话会同步到 {url}"
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
