"""agent 自己的文件放在哪。

**为什么不在工作区里**：用户的项目应该只有用户的代码。设计稿规格、渲染截图、
上传的图片都是 agent 的中间产物——堆在项目里就是垃圾，`git status` 会一直脏着，
`.gitignore` 还得用户自己想起来加。

所以默认放到 ``~/.vibe-agent/``（可以用 ``VA_HOME`` 改）::

    ~/.vibe-agent/
    ├── inbox/         剪贴板、拖拽进来的图片
    ├── designs/       设计稿解析出的规格（.md）
    └── screenshots/   渲染截图和差异热力图

**但日程表上有个例外**：会话记录（``.va/sessions``）留在项目里。
它是纯文本、体积极小，而且「这个项目的对话历史」放在项目旁边更符合直觉。

这个目录会被自动加入可读范围——设计稿规格存在这儿，
上下文被压缩之后 agent 得能 ``read_file`` 把它取回来。
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

DEFAULT_HOME = "~/.vibe-agent"

INBOX_DIR = "inbox"
DESIGNS_DIR = "designs"
SCREENSHOTS_DIR = "screenshots"

# 会话的旧位置（项目内）。为了不丢历史还认它，但新会话不再写这儿。
LEGACY_SESSION_DIR = ".va/sessions"


def agent_home() -> Path:
    """agent 自己的家目录。``VA_HOME`` 可以改位置。"""
    raw = (os.environ.get("VA_HOME") or "").strip() or DEFAULT_HOME
    return Path(raw).expanduser()


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
