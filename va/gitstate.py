"""项目仓库的远端配置与推送。

和 ``session_sync`` 是**两件完全不同的事**，这里再强调一次：

    gitstate       项目代码的仓库（<项目>/.git/config 里的 origin）
    session_sync   会话记录的仓库（VA_SESSION_REMOTE，另一份克隆）

用户已经因为这两个都叫「远端」而困惑过一次，所以凡是给用户看的文案
都要把「哪个仓库」写出来。

**远端 URL 里可能嵌着 token**（``https://user:ghp_xxx@github.com/...``），
这类值会原样显示在界面上、也可能被写进日志。所有对外暴露的 URL
一律先过 ``mask_url()``。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

TIMEOUT = 60
PUSH_TIMEOUT = 300


def mask_url(url: str) -> str:
    """把 URL 里的凭据打码。

    ``https://me:ghp_secret@github.com/x/y.git`` → ``https://me:***@github.com/x/y.git``

    scp 风格的 ``git@github.com:owner/repo.git`` 里那个 ``git`` 是用户名不是密钥，
    原样保留——打码打过头反而让人认不出是哪个远端。
    """
    if "://" not in url:
        return url
    scheme, _, rest = url.partition("://")
    if "@" not in rest:
        return url
    creds, _, host = rest.rpartition("@")
    if ":" in creds:
        user, _, _ = creds.partition(":")
        return f"{scheme}://{user}:***@{host}"
    return f"{scheme}://***@{host}"


def looks_like_credentials(url: str) -> bool:
    """URL 里是不是嵌了账号密码。界面要据此提醒用户别这么写。"""
    if "://" not in url:
        return False
    _, _, rest = url.partition("://")
    creds, _, _ = rest.rpartition("@")
    return "@" in rest and ":" in creds


def _run(root: Path, *args: str, timeout: int = TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _explain(stderr: str) -> str:
    text = (stderr or "").strip()
    if not text:
        return "git 没有给出原因"
    last = text.splitlines()[-1]
    lowered = last.lower()
    if "could not resolve host" in lowered or "unable to access" in lowered:
        return "连不上远端（检查网络和地址）"
    if "permission denied" in lowered or "authentication failed" in lowered:
        return "没有权限（检查 SSH key 或 token）"
    if "not found" in lowered or "does not appear to be a git repository" in lowered:
        return "远端仓库不存在或地址不对"
    if "rejected" in lowered or "non-fast-forward" in lowered:
        return "远端有你本地没有的提交，先拉取再推"
    return last[:200]


@dataclass
class GitState:
    is_repo: bool = False
    branch: str = ""
    remotes: list[tuple[str, str]] = field(default_factory=list)  # (名字, 打码后的 URL)
    origin_url: str = ""  # 打码后的
    dirty: int = 0
    untracked: int = 0
    ahead: int = 0
    behind: int = 0
    has_upstream: bool = False
    last_commit: str = ""
    error: str = ""


def state(root: str | Path) -> GitState:
    """读一份项目仓库的现状。不是仓库就返回 ``is_repo=False``。"""
    root = Path(root)
    try:
        probe = _run(root, "rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.SubprocessError) as exc:
        return GitState(error=f"{type(exc).__name__}: {exc}")

    if probe.returncode != 0:
        return GitState(is_repo=False)

    info = GitState(is_repo=True)

    try:
        branch = _run(root, "symbolic-ref", "--short", "-q", "HEAD")
        info.branch = branch.stdout.strip() if branch.returncode == 0 else ""
        if not info.branch:
            detached = _run(root, "rev-parse", "--short", "HEAD")
            info.branch = f"（游离 HEAD {detached.stdout.strip()}）" if detached.returncode == 0 else "（无提交）"

        remotes = _run(root, "remote")
        for name in remotes.stdout.split():
            url = _run(root, "remote", "get-url", name)
            masked = mask_url(url.stdout.strip())
            info.remotes.append((name, masked))
            if name == "origin":
                info.origin_url = masked

        porcelain = _run(root, "status", "--porcelain=v1", "--untracked-files=all")
        for line in porcelain.stdout.splitlines():
            if line.startswith("??"):
                info.untracked += 1
            elif line.strip():
                info.dirty += 1

        upstream = _run(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        if upstream.returncode == 0:
            info.has_upstream = True
            counts = _run(root, "rev-list", "--left-right", "--count", "@{u}...HEAD")
            if counts.returncode == 0:
                parts = counts.stdout.split()
                if len(parts) == 2:
                    info.behind, info.ahead = int(parts[0]), int(parts[1])

        last = _run(root, "log", "-1", "--pretty=%h %s")
        if last.returncode == 0:
            info.last_commit = last.stdout.strip()[:120]
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        info.error = f"{type(exc).__name__}: {exc}"

    return info


def set_remote(root: str | Path, name: str, url: str) -> tuple[bool, str]:
    """新增或修改一个远端。已存在就改地址，不重复添加。"""
    root = Path(root)
    name = (name or "origin").strip() or "origin"
    url = (url or "").strip()
    if not url:
        return False, "没填地址"

    # 只做很轻的格式检查。git 自己也认很多形式（本地路径、file://、scp 语法），
    # 校验太严会把合法输入挡掉。
    if any(ch.isspace() for ch in url):
        return False, "地址里不能有空格"

    try:
        existing = _run(root, "remote")
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"跑不了 git：{exc}"

    if name in existing.stdout.split():
        result = _run(root, "remote", "set-url", name, url)
        action = "已更新"
    else:
        result = _run(root, "remote", "add", name, url)
        action = "已添加"

    if result.returncode != 0:
        return False, f"{action.replace('已', '')}失败：{_explain(result.stderr)}"

    note = ""
    if looks_like_credentials(url):
        note = "。⚠ 地址里嵌了账号密码，它会明文存在 .git/config 里——建议改用 SSH key 或凭据助手"
    return True, f"{action}远端 {name} → {mask_url(url)}{note}"


def remove_remote(root: str | Path, name: str) -> tuple[bool, str]:
    root = Path(root)
    name = (name or "").strip()
    if not name:
        return False, "没指定远端名"
    try:
        result = _run(root, "remote", "remove", name)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"跑不了 git：{exc}"
    if result.returncode != 0:
        return False, f"删除失败：{_explain(result.stderr)}"
    return True, f"已删除远端 {name}"


def push(root: str | Path, remote: str = "origin", branch: str = "") -> tuple[bool, str]:
    """推送当前分支。首次推送自动设置 upstream。"""
    root = Path(root)
    remote = (remote or "origin").strip() or "origin"

    try:
        current = state(root)
    except Exception as exc:  # noqa: BLE001
        return False, f"读不到仓库状态：{exc}"

    if not current.is_repo:
        return False, "这不是一个 git 仓库。先 git init，或者用 agent 初始化。"

    target = (branch or current.branch).strip()
    if not target or target.startswith("（"):
        return False, "当前没有可推送的分支（仓库里可能还没有提交）"

    if remote not in [name for name, _ in current.remotes]:
        names = "、".join(name for name, _ in current.remotes) or "（一个都没有）"
        return False, f"远端 {remote!r} 不存在。现有远端：{names}。先在设置里配一个。"

    args = ["push"]
    if not current.has_upstream:
        args.append("--set-upstream")
    args += [remote, target]

    try:
        result = _run(root, *args, timeout=PUSH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "推送超时（网络太慢或仓库太大）"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"跑不了 git：{exc}"

    if result.returncode != 0:
        return False, f"推送失败：{_explain(result.stderr)}"

    tail = (result.stdout + result.stderr).strip().splitlines()
    detail = tail[-1] if tail else ""
    return True, f"已推送 {target} → {remote}" + (f"\n{detail}" if detail else "")


def init_repo(root: str | Path) -> tuple[bool, str]:
    """初始化仓库。文件夹里已经有 .git 就什么也不做。"""
    root = Path(root)
    try:
        if _run(root, "rev-parse", "--git-dir").returncode == 0:
            return True, "已经是 git 仓库了"
        result = _run(root, "init")
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"跑不了 git：{exc}"
    if result.returncode != 0:
        return False, f"初始化失败：{_explain(result.stderr)}"
    return True, "已初始化 git 仓库"
