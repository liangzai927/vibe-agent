"""工具集合与注册表装配。"""

from __future__ import annotations

from ..config import Config
from ..plan import PlanBoard
from ..workspace import Workspace
from .base import Registry, Tool, ToolError, safe_run
from .design import DESIGN_TOOLS
from .document import DOCUMENT_TOOLS
from .fs import EditFile, ListDir, ReadFile, WriteFile
from .git import GIT_TOOLS
from .plan import UpdatePlan
from .render import RENDER_TOOLS
from .search import Glob, Grep
from .shell import RunBash

__all__ = [
    "Registry",
    "Tool",
    "ToolError",
    "safe_run",
    "build_registry",
    "ReadFile",
    "WriteFile",
    "EditFile",
    "ListDir",
    "Glob",
    "Grep",
    "RunBash",
    "UpdatePlan",
    "GIT_TOOLS",
    "RENDER_TOOLS",
    "DOCUMENT_TOOLS",
    "DESIGN_TOOLS",
]

CORE_TOOLS = (ReadFile, WriteFile, EditFile, ListDir, Glob, Grep, RunBash)


def build_registry(
    workspace: Workspace,
    *,
    include_git: bool = True,
    include_render: bool = True,
    cfg: Config | None = None,
    plan_board: PlanBoard | None = None,
) -> Registry:
    registry = Registry()

    # 计划工具排在最前面：它是模型开工前第一个该想到的东西
    board = plan_board if plan_board is not None else PlanBoard()
    registry.register(UpdatePlan(board))

    for tool_cls in CORE_TOOLS:
        registry.register(tool_cls(workspace))
    if include_git:
        for tool_cls in GIT_TOOLS:
            registry.register(tool_cls(workspace))
    if include_render:
        for tool_cls in RENDER_TOOLS:
            registry.register(tool_cls(workspace, cfg))
        for tool_cls in DESIGN_TOOLS:
            registry.register(tool_cls(workspace, cfg))
    for tool_cls in DOCUMENT_TOOLS:
        registry.register(tool_cls(workspace, cfg))
    return registry
