"""agent 的数据放哪。

**为什么不在工作区里**：用户的项目应该只有用户的代码。会话记录、记忆、
设计稿规格、渲染截图都是 agent 的东西——堆在项目里就是垃圾，
``git status`` 会一直脏着。

所以全部放在一个**数据根**下（``VA_HOME`` 可以改）::

    <数据根>/
    ├── projects.json   工作区注册表
    ├── workspace/      默认工作区（不选项目时用它）
    ├── scratch/        未分组会话的工作区
    ├── projects/       各项目的数据，按 <项目名-哈希> 分目录
    ├── memory/         全局记忆
    ├── inbox/          剪贴板、拖拽进来的图片
    ├── designs/        设计稿解析出的规格（.md）
    └── screenshots/    渲染截图和差异热力图

**Windows 上刻意避开系统盘**：数据（尤其是会话历史）会一直长，
放 C: 盘迟早把系统盘撑满。能选的话优先放别的固定盘。

这个目录会被自动加入可读范围——设计稿规格存在这儿，
上下文被压缩之后 agent 得能 ``read_file`` 把它取回来。
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

#: 数据根的名字。放在平台默认位置下面。
DATA_DIR_NAME = "vibe-agent"

#: 老位置。没有 VA_HOME 时优先用它，免得老用户的数据「消失」。
LEGACY_DEFAULT_HOME = "~/.vibe-agent"

#: 默认工作区。不选项目时新会话就用它。
DEFAULT_WORKSPACE_DIR = "workspace"
#: 未分组会话的工作区。和默认工作区分开，免得「随手问的问题」混进正式工作目录。
SCRATCH_DIR = "scratch"
#: 工作区注册表。
PROJECTS_INDEX = "projects.json"

INBOX_DIR = "inbox"
DESIGNS_DIR = "designs"
SCREENSHOTS_DIR = "screenshots"

# 会话的旧位置（项目内）。为了不丢历史还认它，但新会话不再写这儿。
LEGACY_SESSION_DIR = ".va/sessions"


def _is_windows() -> bool:
    """单独抽出来是为了**可测**。

    测试里直接 patch ``os.name`` 会影响全局——``pathlib.Path`` 也是按
    ``os.name`` 决定返回 PosixPath 还是 WindowsPath 的，一 patch 整个进程的
    Path 行为都变了。所以这里留一个窄接口。
    """
    return os.name == "nt"


def _windows_non_system_drives() -> list[tuple[str, int]]:
    """Windows：列出**非系统盘的固定盘**及剩余空间，按剩余从大到小。

    排掉的不只是系统盘，还有网络盘、光驱、U 盘——那些要么慢、
    要么随时会拔掉，不该拿来存长期数据。
    """
    try:
        import ctypes
        import shutil as _shutil
    except ImportError:
        return []

    DRIVE_FIXED = 3
    system = (os.environ.get("SystemDrive") or "C:").rstrip(":").upper()

    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        mask = kernel32.GetLogicalDrives()
    except (AttributeError, OSError):
        return []

    found: list[tuple[str, int]] = []
    for index in range(26):
        if not (mask >> index) & 1:
            continue
        letter = chr(ord("A") + index)
        if letter == system:
            continue
        root = f"{letter}:\\"
        try:
            if kernel32.GetDriveTypeW(root) != DRIVE_FIXED:
                continue
            free = _shutil.disk_usage(root).free
        except OSError:
            continue
        found.append((letter, free))

    return sorted(found, key=lambda item: -item[1])


def default_data_root() -> tuple[Path, str]:
    """数据该放哪 + 一句说明（给自检和提示用）。

    优先级：

    1. ``VA_HOME``
    2. **老位置 ``~/.vibe-agent`` 已经存在** → 继续用它
       （不能因为换了默认值就让老用户的数据「不见了」）
    3. Windows：非系统盘里剩余空间最大的那块，``<盘>:\vibe-agent``
    4. 退回用户目录 ``~/.vibe-agent``

    **第 3 步找不到时不静默降级**：调用方要把原因告诉用户，
    否则他会以为装在 D 盘，实际还在 C 盘。
    """
    override = (os.environ.get("VA_HOME") or "").strip()
    if override:
        return Path(override).expanduser(), "VA_HOME 指定"

    legacy = Path(LEGACY_DEFAULT_HOME).expanduser()
    if legacy.is_dir():
        return legacy, "沿用已有的 ~/.vibe-agent"

    if _is_windows():
        drives = _windows_non_system_drives()
        if drives:
            letter, free = drives[0]
            gb = free / (1024 ** 3)
            return (
                Path(f"{letter}:\\{DATA_DIR_NAME}"),
                f"非系统盘 {letter}:（剩余 {gb:.0f} GB）",
            )
        return legacy, (
            "没找到非系统盘，只能用系统盘。想换位置就设 "
            f"VA_HOME=D:\\{DATA_DIR_NAME}"
        )

    return legacy, "默认位置"


def agent_home() -> Path:
    """agent 的数据根。``VA_HOME`` 可以改位置。"""
    return default_data_root()[0]


def data_root_note() -> str:
    """数据根为什么在那儿——自检和启动提示用。"""
    return default_data_root()[1]


def default_workspace_dir() -> Path:
    """默认工作区。

    **不选项目时的落脚点。** 刻意放在数据根下面：数据根已经做过平台判定
    （Windows 上避开系统盘），所以这里自动跟着走，不用再判一次。
    """
    return agent_home() / DEFAULT_WORKSPACE_DIR


def scratch_dir() -> Path:
    """未分组会话的工作区。

    和默认工作区分开是有意的：「随手问一句」产生的东西不该混进
    你真正的工作目录里。这个地方可以随时整个删掉。
    """
    return agent_home() / SCRATCH_DIR


def projects_index_path() -> Path:
    """工作区注册表。"""
    return agent_home() / PROJECTS_INDEX


def inbox_dir() -> Path:
    """剪贴板 / 拖拽进来的图片。临时素材，更不该落在项目里。"""
    return agent_home() / INBOX_DIR


def designs_dir() -> Path:
    return agent_home() / DESIGNS_DIR


def screenshots_dir() -> Path:
    return agent_home() / SCREENSHOTS_DIR


def diffs_dir() -> Path:
    return screenshots_dir() / "diffs"


def data_root() -> Path:
    """所有 agent 数据的根。

    * 配了 ``VA_DATA_REMOTE``（一个 git 仓库）→ 那份本地克隆
    * 否则 → ``~/.vibe-agent``

    **仓库里的结构是固定的**，本地和远端一致：

        <data-root>/
        ├── memory/                     所有项目都生效的偏好
        └── projects/
            └── <项目名-哈希>/
                ├── memory/             只有这个项目生效的偏好
                └── sessions/           这个项目的对话记录

    **先按项目分，项目下再分**：打开仓库一眼就能看出哪个项目有哪些东西，
    而不是一堆项目的会话和偏好混在一起。
    """
    remote = (os.environ.get("VA_DATA_REMOTE") or "").strip()
    if remote:
        from .session_sync import remote_dir

        return remote_dir(remote, kind="data")
    return agent_home()


def project_slug(workspace: str | Path) -> str:
    """给项目起一个又好认又不会撞的短名。

    「目录名 + 路径哈希前缀」：目录名让人一眼认出是哪个项目，
    哈希保证同名项目不打架——``~/work/api`` 和 ``~/personal/api``
    是两个完全不同的项目，只按目录名存会搅在一起。
    """
    root = Path(workspace).expanduser().resolve()
    name = root.name or "root"
    # 去掉路径分隔符之类的危险字符，保留中文（项目名常是中文）
    safe = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", name).strip("-.") or "project"
    digest = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{digest}"


def project_dir(workspace: str | Path) -> Path:
    """这个项目的数据目录：``<data-root>/projects/<项目名-哈希>/``。"""
    return data_root() / "projects" / project_slug(workspace)


def project_memory_dir(workspace: str | Path) -> Path:
    """只有这个项目生效的偏好。"""
    override = (os.environ.get("VA_MEMORY_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    return project_dir(workspace) / "memory"


def global_memory_dir() -> Path:
    """所有项目都读的偏好。放在数据根下，不属于任何项目。"""
    override = (os.environ.get("VA_GLOBAL_MEMORY_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    remote = (os.environ.get("VA_MEMORY_REMOTE") or "").strip()
    if remote:
        from .session_sync import remote_dir

        return remote_dir(remote, kind="memory")
    return data_root() / "memory"


LEGACY_HOME_SESSIONS = "sessions"


def sessions_dir(workspace: str | Path) -> Path:
    
    """这个项目的会话存哪。

    **默认在项目之外**，会话是你和 agent 的对话记录，不是项目的一部分——
    放在项目里会让 ``git status`` 一直脏着。

    三个层次，优先级从高到低：

    1. ``VA_SESSION_DIR``        显式指定一个目录
    2. ``VA_SESSION_REMOTE``     单独给会话一个仓库
    3. ``VA_DATA_REMOTE``        所有数据共用一个仓库（推荐），按项目分目录

    没配远端时用本地 ``~/.vibe-agent/sessions/<项目名-哈希>/``——
    维持原样，免得已经存在的记录突然找不到了。
    """
    override = (os.environ.get("VA_SESSION_DIR") or "").strip()
    if override:
        return Path(override).expanduser()

    remote = (os.environ.get("VA_SESSION_REMOTE") or "").strip()
    if remote:
        from .session_sync import remote_dir

        return remote_dir(remote)

    target = project_dir(workspace) / "sessions"

    # 老版本把会话放在 <home>/sessions/<slug>/。如果那儿已经有东西、
    # 新位置还是空的，就**继续用老的**——把用户的记录弄丢比目录不整齐糟得多。
    legacy = agent_home() / LEGACY_HOME_SESSIONS / project_slug(workspace)
    if not target.exists() and legacy.is_dir() and any(legacy.glob("*.jsonl")):
        return legacy
    return target


def _relative_to_home(path: Path) -> str | None:
    """把 home 下的绝对路径转成 ``~/.vibe-agent/...`` 那种可读形式。"""
    try:
        return str(Path("~") / path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def describe(path: Path) -> str:
    """给人看的路径。home 下的显示成 ~/ 开头，别把一长串绝对路径糊到界面上。"""
    home = agent_home()
    try:
        if path == home or home in path.parents:
            return _relative_to_home(path) or str(path)
    except (OSError, ValueError):
        pass
    return str(path)
