"""需要工作区与配置的工具的共同基础。

「渲染页面」和「解析设计稿」看起来是两件事，但它们共享同一套前置条件：
都不假设环境、都要解析工作区路径、都需要一个多模态模型配置。
把这些抽出来，是为了让「缺视觉模型时给什么提示」只有一份实现——
容器里用户看不到 traceback 的上下文，提示必须自解释且一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Config
from ..workspace import Workspace, WorkspaceError
from .base import Tool, ToolError

VISION_HINT = (
    "没有配置视觉模型，读不了图。\n"
    "主模型通常不支持读图（deepseek-chat 就不支持），要单独指一个多模态模型。\n"
    "\n"
    "**先看你现在的服务商有没有能读图的模型**——同源的话不用另开账号、不用另一个 key：\n"
    "  /models 会标注哪些模型「可读图」，然后只写一行就够了（地址和 key 自动沿用主配置）：\n"
    "    VA_VISION_MODEL=<那个模型名>\n"
    "\n"
    "跨服务商才需要写全：\n"
    "  VA_VISION_MODEL=gpt-4o\n"
    "  VA_VISION_BASE_URL=https://api.openai.com/v1\n"
    "  VA_VISION_API_KEY=sk-...\n"
    "\n"
    "完全不想配模型也行，这几条路不需要模型：\n"
    "  inspect_image  尺寸、主色 hex、内容区边界、字符布局图——写 CSS 够用了\n"
    "  render_page    渲染真实页面\n"
    "  pixel_diff     和设计稿逐像素对比，差异百分比是客观判据\n"
    "少的是「该改什么」的自然语言提示，以及图上的文字和语义。"
)


@dataclass
class AgentContext:
    """工具层需要共享的运行时状态。

    有些工具不能只靠 workspace 干活——比如委派子 agent 时要拿到父 agent 的
    撤销栈，否则子 agent 改的文件 `/undo` 撤不掉。把它们显式打包传下去，
    比让工具各自去反查全局状态可靠得多。

    ui / undo / plan_board 用 Any 标注是刻意的：它们来自上层，写死类型会让
    工具层反向依赖 ui、history、plan，得不偿失。
    """

    workspace: Workspace
    cfg: Config | None = None
    plan_board: Any = None  # PlanBoard
    undo: Any = None  # UndoStack
    ui: Any = None  # UI
    llm: Any = None  # LLM —— 子 agent 要复用它，别每次新建连接
    depth: int = 0  # 0 表示这是主 agent
    max_depth: int = 1

    def can_delegate(self) -> bool:
        return self.depth < self.max_depth


class WorkspaceTool(Tool):
    """工具层里「要解析路径、要读配置」的那一类。"""

    def __init__(self, workspace: Workspace, cfg: Config | None = None) -> None:
        self.workspace = workspace
        self._cfg = cfg

    # ------------------------------------------------------------------ 配置

    def config(self) -> Config:
        if self._cfg is None:
            self._cfg = Config.load(self.workspace.root)
        return self._cfg

    def require_vision(self) -> Config:
        cfg = self.config()
        if not cfg.has_vision:
            raise ToolError(VISION_HINT)
        return cfg

    # ------------------------------------------------------------------ 路径

    def resolve(self, path: str, *, what: str = "文件") -> Path:
        """解析工作区内路径，把 WorkspaceError 转成带上下文的 ToolError。"""
        try:
            return self.workspace.resolve(path, must_exist=True)
        except WorkspaceError as exc:
            raise ToolError(f"找不到{what} {path!r}：{exc}") from exc

    def output_path(self, path: str | None, default: str) -> Path:
        """产物落盘路径：给了就用给的，没给就用默认值（相对工作区）。"""
        return self.workspace.resolve(path) if path else self.workspace.resolve(default)

    def rel(self, path: Path) -> str:
        return self.workspace.relative(path)
