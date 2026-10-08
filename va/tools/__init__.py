"""工具集合与注册表装配。"""

from __future__ import annotations

from ..config import Config
from ..plan import PlanBoard
from ..workspace import Workspace
from .base import Registry, Tool, ToolError, safe_run
from .context import AgentContext
from .delegate import DELEGATE_TOOLS, Delegate
from .design import DESIGN_TOOLS
from .document import DOCUMENT_TOOLS
from .fs import EditFile, ListDir, ReadFile, WriteFile
from .git import GIT_TOOLS
from .plan import UpdatePlan
from .render import RENDER_TOOLS
from .review import REVIEW_TOOLS, ReviewChanges
from .search import Glob, Grep
from .shell import RunBash

__all__ = [
    "Registry",
    "Tool",
    "ToolError",
    "safe_run",
    "build_registry",
    "filter_registry",
    "AgentContext",
    "ReadFile",
    "WriteFile",
    "EditFile",
    "ListDir",
    "Glob",
    "Grep",
    "RunBash",
    "UpdatePlan",
    "Delegate",
    "GIT_TOOLS",
    "RENDER_TOOLS",
    "DOCUMENT_TOOLS",
    "DESIGN_TOOLS",
    "DELEGATE_TOOLS",
    "REVIEW_TOOLS",
    "ReviewChanges",
]

CORE_TOOLS = (ReadFile, WriteFile, EditFile, ListDir, Glob, Grep, RunBash)


def build_registry(
    workspace: Workspace,
    *,
    include_git: bool = True,
    include_render: bool = True,
    cfg: Config | None = None,
    plan_board: PlanBoard | None = None,
    ctx: AgentContext | None = None,
) -> Registry:
    registry = Registry()

    # 计划工具排在最前面：它是模型开工前第一个该想到的东西
    board = plan_board or (ctx.plan_board if ctx else None) or PlanBoard()
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

    # 委派需要 UI 和撤销栈，所以只有拿到 ctx 才注册。
    # 深度用完就不再提供——子 agent 不能再往下派。
    if ctx is not None and ctx.can_delegate():
        for tool_cls in DELEGATE_TOOLS:
            registry.register(tool_cls(ctx))
        registry.register(ReviewChanges(ctx))

    return registry


def filter_registry(
    registry: Registry,
    *,
    only: set[str] | None = None,
    exclude: set[str] | None = None,
) -> Registry:
    """按名字筛出一个新注册表。

    工具实例是共享的（不是复制），所以筛选只决定「能看见哪些」，
    不会让同一个工具出现两份状态。
    """
    out = Registry()
    for tool in registry:
        if only is not None and tool.name not in only:
            continue
        if exclude is not None and tool.name in exclude:
            continue
        out.register(tool)
    return out
