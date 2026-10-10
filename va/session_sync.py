"""把会话记录同步到 git 远端。

场景：你希望在多台机器之间共享对话记录，或者只是想给它们做版本管理。
做法是把远端仓库克隆到本地缓存目录，会话直接写进那份克隆，同步时
commit + push。

**几个刻意的选择**：

* **不自动推送。** 推送会把内容发到远端，那是外部可见的动作，该由人来按。
  这里只提供 ``sync()``，什么时候调由上层决定。
* **克隆失败不吞掉。** 网络不通、没权限、仓库不存在——用户必须知道，
  否则会出现「我明明配了远端，记录却还在本地」这种最难查的状态。
* **推不上去也不算丢数据。** 会话文件先在本地写好，推送失败只是没同步，
  下次再推就行。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .gitutil import explain as _explain
from .paths import agent_home

DEFAULT_TIMEOUT = 180
SYNC_MESSAGE = "chore: 同步会话记录"


@dataclass
class SyncResult:
    ok: bool
    message: str = ""
    detail: str = ""

    def describe(self) -> str:
        return self.message or ("成功" if self.ok else self.detail or "失败")


def _slug_from_url(url: str) -> str:
    tail = url.rstrip("/").split("/")[-1] or "sessions"
    tail = tail[:-4] if tail.endswith(".git") else tail
    safe = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", tail).strip("-.") or "sessions"
    return safe


def remote_dir(url: str, *, kind: str = "sessions") -> Path:
    """远端仓库的本地克隆放在哪。

    路径里带哈希是因为不同远端可能重名（``team/sessions`` vs ``me/sessions``），
    光靠仓库名会撞。

    ``kind`` 区分用途——会话记录和长期记忆可以放在同一个仓库，也可以分开放，
    所以缓存目录要分得开。（sessions 是默认值，保持路径不变，
    免得用户已有的克隆失效。）
    """
    digest = hashlib.sha1(url.strip().encode("utf-8")).hexdigest()[:10]
    slug = _slug_from_url(url)
    if kind == "sessions":
        return agent_home() / "remotes" / f"{slug}-{digest}"
    return agent_home() / "remotes" / f"{kind}-{slug}-{digest}"


def _git(args: list[str], cwd: Path | None = None, timeout: int = DEFAULT_TIMEOUT):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


# --------------------------------------------------------------- 初始化空仓库

INIT_README = """# {name}

这个仓库由 [vibe-agent](https://github.com/liangzai927/vibe-agent) 自动创建，
用来在多台机器之间同步**记忆**和**会话记录**。

## 结构

```
memory/                             全局偏好——所有项目都读
projects/<项目名-哈希>/memory/        这个项目自己的偏好
projects/<项目名-哈希>/sessions/      这个项目的对话记录
```

目录名里的哈希来自项目的绝对路径，所以两个同名的项目不会撞。

## 注意

这里是**对话记录**。别把项目代码推到这个仓库里——两者互不相干。
"""


def remote_is_empty(url: str, *, timeout: int = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """远端是不是一个**空白**仓库。

    返回 ``(是否空白, 不是空白的原因)``。连不上或没权限时也算「不是空白」，
    但原因不同——调用方直接把原因给用户看。
    """
    url = (url or "").strip()
    if not url:
        return False, "没填地址"

    result = _git(["ls-remote", "--heads", url], timeout=timeout)
    if result.returncode != 0:
        return False, _explain(result.stderr)
    if result.stdout.strip():
        return False, "这个仓库里已经有分支了"
    return True, ""


def _git_commit(target: Path, message: str) -> "subprocess.CompletedProcess":
    """提交。带上身份兜底——容器和 CI 里常常没有全局 gitconfig。"""
    return _git(
        [
            "-c", "user.name=vibe-agent",
            "-c", "user.email=vibe-agent@localhost",
            "commit", "-m", message,
        ],
        cwd=target,
    )


def bootstrap(url: str, *, kind: str = "data", timeout: int = DEFAULT_TIMEOUT) -> SyncResult:
    """把一个**空白**仓库初始化成数据仓库。

    流程（和用户在 GitHub 上看到的指引等价，只是内容换成了数据仓库该有的）：

        1. 确认远端是空白的——不是就中断，绝不动已有内容的仓库
        2. 克隆下来（顺带验证地址和权限）
        3. 建 README + memory/ + projects/
        4. 首次提交并推送，设置 upstream

    **第 1 步是硬性的。** 往一个已经有东西的仓库里塞提交是有破坏性的，
    宁可让用户换一个空白仓库。
    """
    url = (url or "").strip()
    if not url:
        return SyncResult(False, detail="没填 git 地址")

    empty, why = remote_is_empty(url, timeout=timeout)
    if not empty:
        return SyncResult(
            False,
            detail=(
                f"这个仓库不能用作数据仓库：{why}。\n"
                f"请换一个**空白仓库**——记忆和会话会写进去，"
                f"混在已有内容里既乱又危险。"
            ),
        )

    target = remote_dir(url, kind=kind)

    # 之前可能留下过一个失败的克隆，清掉重来
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return SyncResult(False, detail=f"建不出目录 {target.parent}：{exc}")

    clone = _git(["clone", url, str(target)], timeout=timeout)
    if clone.returncode != 0:
        shutil.rmtree(target, ignore_errors=True)
        return SyncResult(False, detail=f"克隆失败：{_explain(clone.stderr)}")

    # 建结构。README 用仓库名当标题，和用户自己敲的那套一致。
    name = _slug_from_url(url)
    (target / "README.md").write_text(INIT_README.format(name=name), encoding="utf-8")
    (target / "memory").mkdir(exist_ok=True)
    (target / "projects").mkdir(exist_ok=True)

    add = _git(["add", "-A"], cwd=target)
    if add.returncode != 0:
        return SyncResult(False, detail=f"暂存失败：{_explain(add.stderr)}")

    commit = _git_commit(target, "chore: 初始化记忆仓库")
    if commit.returncode != 0:
        # 空仓库克隆下来不会有 commit，正常不会走到这里
        return SyncResult(False, detail=f"首次提交失败：{_explain(commit.stderr)}")

    push = _git(["push", "-u", "origin", _default_branch(target)], cwd=target, timeout=timeout)
    if push.returncode != 0:
        return SyncResult(
            False,
            detail=(
                f"初始化好了，但推送失败：{_explain(push.stderr)}\n"
                f"配置已经写下，网络恢复后点「同步」即可。"
            ),
        )

    return SyncResult(True, message=f"已初始化 {name}，记忆和会话会同步到这里")


def _default_branch(target: Path) -> str:
    """当前分支名。新克隆一般是 main 或 master，取实际的那个。"""
    result = _git(["symbolic-ref", "--short", "-q", "HEAD"], cwd=target)
    branch = result.stdout.strip() if result.returncode == 0 else ""
    return branch or "main"


def ensure_clone(
    url: str, *, timeout: int = DEFAULT_TIMEOUT, pull: bool = True, kind: str = "sessions"
) -> SyncResult:
    """确保本地有一份可用的克隆。已经有的就 pull 一下。

    不抛异常——上层要把失败原因原样告诉用户。
    """
    url = (url or "").strip()
    if not url:
        return SyncResult(False, detail="没填 git 地址（会话记录用哪个仓库）")

    target = remote_dir(url, kind=kind)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return SyncResult(False, detail=f"建不了缓存目录：{exc}")

    if not (target / ".git").is_dir():
        try:
            result = _git(["clone", "--depth", "50", url, str(target)], timeout=timeout)
        except subprocess.TimeoutExpired:
            return SyncResult(False, detail="克隆超时（网络太慢或地址不通）")
        except OSError as exc:
            return SyncResult(False, detail=f"跑不了 git：{exc}")
        if result.returncode != 0:
            # 克隆失败会在 target 留下半个目录，清掉免得下次误判成「已克隆」
            import shutil

            shutil.rmtree(target, ignore_errors=True)
            return SyncResult(False, detail=f"克隆失败：{_explain(result.stderr)}")
        return SyncResult(True, message=f"已克隆到 {target}")

    if not pull:
        return SyncResult(True, message="已有本地克隆")

    try:
        result = _git(["pull", "--ff-only"], cwd=target, timeout=timeout)
    except subprocess.TimeoutExpired:
        return SyncResult(False, detail="拉取超时")
    if result.returncode != 0:
        # 拉不动不算致命：本地记录还能照常写，只是没同步
        return SyncResult(
            False, detail=f"拉取失败（本地记录仍可正常写入）：{_explain(result.stderr)}"
        )
    return SyncResult(True, message="已是最新")


def sync(
    url: str, *, message: str = SYNC_MESSAGE, timeout: int = DEFAULT_TIMEOUT, kind: str = "sessions"
) -> SyncResult:
    """把本地会话 commit 并推送。

    **推送是外部可见的动作**，所以这个函数只在明确调用时才执行。
    """
    url = (url or "").strip()
    if not url:
        return SyncResult(
            False,
            detail=(
                "还没配「会话记录」的远端。注意这和项目代码的 git 远端是两回事——"
                "会话记录建议单独开一个仓库。在设置里把「会话保存位置」选成 "
                "Git 仓库并填上地址即可。"
            ),
        )

    target = remote_dir(url, kind=kind)
    if not (target / ".git").is_dir():
        ready = ensure_clone(url, timeout=timeout, kind=kind)
        if not ready.ok:
            return ready

    try:
        if _git(["add", "-A"], cwd=target, timeout=timeout).returncode != 0:
            return SyncResult(False, detail="git add 失败")

        status = _git(["status", "--porcelain"], cwd=target, timeout=timeout)
        if not (status.stdout or "").strip():
            return SyncResult(True, message="没有新记录要同步")

        commit = _git(["commit", "-m", message], cwd=target, timeout=timeout)
        if commit.returncode != 0:
            return SyncResult(False, detail=f"提交失败：{_explain(commit.stderr)}")

        push = _git(["push"], cwd=target, timeout=timeout)
        if push.returncode != 0:
            # 提交已经落本地了，所以不算丢数据
            return SyncResult(
                False,
                detail=f"提交成功但推送失败（记录没丢，下次再推）：{_explain(push.stderr)}",
            )
    except subprocess.TimeoutExpired:
        return SyncResult(False, detail="同步超时")
    except OSError as exc:
        return SyncResult(False, detail=f"跑不了 git：{exc}")

    return SyncResult(True, message="已同步到远端")
