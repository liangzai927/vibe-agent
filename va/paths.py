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

import os
from pathlib import Path

DEFAULT_HOME = "~/.vibe-agent"

INBOX_DIR = "inbox"
DESIGNS_DIR = "designs"
SCREENSHOTS_DIR = "screenshots"

# 会话仍然跟着项目走
SESSION_DIR = ".va/sessions"


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
