"""需要工作区与配置的工具的共同基础。

「渲染页面」和「解析设计稿」看起来是两件事，但它们共享同一套前置条件：
都不假设环境、都要解析工作区路径、都需要一个多模态模型配置。
把这些抽出来，是为了让「缺视觉模型时给什么提示」只有一份实现——
容器里用户看不到 traceback 的上下文，提示必须自解释且一致。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import Config
from ..workspace import Workspace, WorkspaceError
from .base import Tool, ToolError

VISION_HINT = (
    "没有配置视觉模型，读不了图。\n"
    "主模型通常不支持读图（deepseek-chat 就不支持），需要单独配一个多模态模型：\n"
    "  VA_VISION_MODEL=gpt-4o\n"
    "  VA_VISION_BASE_URL=https://api.openai.com/v1\n"
    "  VA_VISION_API_KEY=sk-...\n"
    "不想配模型的话：渲染与像素对比（render_page / pixel_diff）完全不需要模型，"
    "照样能驱动 UI 迭代，只是少了「该改什么」的自然语言提示。"
)


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
