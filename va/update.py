"""自己更新自己。

**难点不在「拉新代码」，在「认出当初是怎么装的」。**
pipx / pip / git clone / 压缩包，四种装法的更新方式完全不同，
弄错了轻则没更新，重则把环境搞坏。

好在 pip 会把安装来源写进 `direct_url.json`（VCS 安装还会带 `vcs_info`），
所以能问出来，不用让用户记。

────────────────────────────────────────────────────────────────

**两条原则：**

1. **不能自更新的情况要明说怎么做**，而不是硬猜一个命令跑下去。
   压缩包装的没有来源信息，猜错了可能把整个环境弄坏。

2. **先看再动。** 默认只检查并打印「要做什么」，加 `-y` 才真的执行。
   更新是个会改变本机环境的动作，不该悄悄发生。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: git 命令的超时。网络慢的时候 fetch 可能要一会儿。
GIT_TIMEOUT = 120
#: 安装/更新命令的超时。要下载依赖，给得宽一些。
INSTALL_TIMEOUT = 900

PACKAGE = "vibe-agent"


@dataclass
class Install:
    """这份程序是怎么装上的。"""

    kind: str = "unknown"       # git | pipx | pip | local | unknown
    root: Path | None = None    # 源码根（git 时）
    source: str = ""            # 安装来源（URL 或路径）
    reason: str = ""            # 怎么判断出来的

    @property
    def can_self_update(self) -> bool:
        return self.kind in ("git", "pipx", "pip")

    @property
    def label(self) -> str:
        return {
            "git": "从源码仓库跑（git clone）",
            "pipx": "pipx 装的",
            "pip": "pip 装的",
            "local": "从本地目录装的",
            "unknown": "认不出来",
        }.get(self.kind, self.kind)


def _git(*args: str, cwd: Path, timeout: int = GIT_TIMEOUT) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**_env(), "GIT_TERMINAL_PROMPT": "0"},   # 别弹账号密码
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _env() -> dict[str, str]:
    import os

    return dict(os.environ)


def _direct_url() -> dict:
    """pip 记下来的安装来源。VCS 安装会带 `vcs_info`。"""
    try:
        from importlib.metadata import distribution

        raw = distribution(PACKAGE).read_text("direct_url.json")
    except Exception:  # noqa: BLE001 - 没装成包、或者元数据缺失
        return {}
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def detect() -> Install:
    """这份程序是怎么装上的。

    顺序有讲究：**先看是不是源码仓库**——那是开发时的形态，也最好更新。
    再看 pip 记的来源。都不匹配就说认不出来，并给出可行的做法。
    """
    from .config import Config

    # ① 旁边就有 .git 和 run.py → 从源码跑
    root = Path(__file__).resolve().parent.parent
    if (root / ".git").exists() and (root / "run.py").is_file():
        return Install(kind="git", root=root, source=str(root), reason="旁边有 .git 和 run.py")

    info = _direct_url()
    url = str(info.get("url") or "")
    vcs = info.get("vcs_info") or {}

    # ② pip 从 VCS 装的（pipx 也是 pip，用 sys.prefix 区分）
    if vcs:
        kind = "pipx" if "pipx" in sys.prefix else "pip"
        return Install(
            kind=kind,
            source=url,
            reason=f"pip 记录的直接来源是 {vcs.get('vcs', 'vcs')} 仓库",
        )

    # ③ 从本地目录装的
    if url.startswith("file://"):
        return Install(
            kind="local",
            root=Path(url[7:]),
            source=url,
            reason="pip 记录的是本地目录",
        )

    # ④ pip 从 PyPI / wheel 装的（没有 direct_url）
    if not url:
        prefix = "pipx" if "pipx" in sys.prefix else "pip"
        return Install(kind=prefix, source="", reason="没有来源记录，按包管理器处理")

    return Install(kind="unknown", source=url, reason=f"来源是 {url}")


# --------------------------------------------------------------- 版本


def current_revision(install: Install) -> str:
    """当前跑的是哪个版本。git 用 commit 号，装的用包版本。"""
    if install.kind == "git" and install.root:
        code, out = _git("rev-parse", "--short", "HEAD", cwd=install.root)
        if code == 0:
            return out.strip()
    try:
        from importlib.metadata import version

        return version(PACKAGE)
    except Exception:  # noqa: BLE001
        return "未知"


def check(install: Install) -> tuple[bool, str]:
    """有没有新版本。返回 ``(有新版吗, 说明)``。

    **只查不动**——`git fetch` 只更新远端引用，不改工作区。
    """
    if install.kind != "git" or install.root is None:
        # 装出来的包：没法在本地比版本，得问来源
        return False, (
            "装出来的包没法在本地判断有没有新版本，"
            "直接跑更新就行（会重新拉一次来源）。"
        )

    root = install.root
    code, out = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=root)
    if code != 0:
        return False, f"拿不到当前分支：{out.strip()[:200]}"
    branch = out.strip()

    code, out = _git("fetch", "--quiet", "origin", branch, cwd=root)
    if code != 0:
        # **别把 git 的原始输出丢给用户。** 那里面全是样板文字，
        # 真正的原因（网络不通 / 没权限 / 分支不存在）埋在里面。
        from .gitutil import explain

        reason = explain(out) or out.strip().splitlines()[-1][:200]
        return False, f"拉不到远端更新：{reason}"

    code, local = _git("rev-parse", "HEAD", cwd=root)
    code2, remote = _git("rev-parse", "FETCH_HEAD", cwd=root)
    if code != 0 or code2 != 0:
        return False, "比不出本地和远端"

    local, remote = local.strip(), remote.strip()
    if local == remote:
        return False, "已经是最新的"
    behind = _count(root, local, remote)
    return True, f"有新版本（落后 {behind} 个提交）"


def _count(root: Path, local: str, remote: str) -> int:
    code, out = _git("rev-list", "--count", f"{local}..{remote}", cwd=root)
    if code != 0:
        return 0
    try:
        return int(out.strip())
    except ValueError:
        return 0


# --------------------------------------------------------------- 执行


def plan(install: Install) -> tuple[list[str], str]:
    """要跑哪些命令。返回 ``(命令列表, 说明)``。

    **不能自更新的情况返回空列表 + 一句人话**，让调用方决定怎么显示——
    硬猜一个命令跑下去，可能把用户的环境弄坏。
    """
    if install.kind == "git" and install.root:
        return (
            ["git pull --ff-only"],
            f"拉取 {install.root} 的最新代码",
        )

    if install.kind in ("pipx", "pip"):
        if not install.source:
            return [], (
                "认不出安装来源，没法自动更新。\\n"
                "  按你当初装的方式来：\\n"
                '    pipx install --force "git+https://github.com/liangzai927/vibe-agent.git"\\n'
                "  或者用压缩包那条路重装一次。"
            )
        installer = "pipx install --force" if install.kind == "pipx" else "pip install --upgrade"
        return (
            [f"{installer} {install.source}"],
            f"从 {install.source} 重新安装",
        )

    if install.kind == "local":
        return [], (
            f"这份是从本地目录装的（{install.source}）。\\n"
            "  去那个目录拉最新代码，然后重跑一次安装。"
        )

    return [], (
        "认不出这是怎么装的。\\n"
        "  用压缩包装的：重新下载新版本的包，解压后跑 ./scripts/install.sh\\n"
        "  pipx 装的：pipx install --force git+https://github.com/liangzai927/vibe-agent.git"
    )


def run(install: Install, *, executable: str | None = None) -> tuple[bool, str]:
    """真的执行更新。返回 ``(成功吗, 输出)``。

    **调用方负责确认**——这个函数不问你。
    """
    commands, note = plan(install)
    if not commands:
        return False, note

    outputs: list[str] = []
    for command in commands:
        argv = shlex_split(command)
        if argv and argv[0] in ("pipx", "pip") and executable:
            # 用**同一个解释器**，否则可能装到别的 Python 里去
            argv = [executable, "-m", "pip", *argv[1:]] if argv[0] == "pip" else argv

        try:
            result = subprocess.run(
                argv,
                cwd=str(install.root) if install.root else None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=INSTALL_TIMEOUT,
                env={**_env(), "GIT_TERMINAL_PROMPT": "0"},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"{command} 跑不起来：{exc}"

        combined = ((result.stdout or "") + (result.stderr or "")).strip()
        outputs.append(f"$ {command}\\n{combined[-2000:]}")
        if result.returncode != 0:
            return False, "\\n\\n".join(outputs)

    return True, "\\n\\n".join(outputs)


def shlex_split(command: str) -> list[str]:
    import shlex

    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def can_find(name: str) -> bool:
    return shutil.which(name) is not None
