"""工具集合与注册表装配。"""

from __future__ import annotations

from ..config import Config
from ..workspace import Workspace
from .base import Registry, Tool, ToolError, safe_run
from .fs import EditFile, ListDir, ReadFile, WriteFile
from .git import GIT_TOOLS
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
    "GIT_TOOLS",
    "RENDER_TOOLS",
]

CORE_TOOLS = (ReadFile, WriteFile, EditFile, ListDir, Glob, Grep, RunBash)


def build_registry(
    workspace: Workspace,
    *,
    include_git: bool = True,
    include_render: bool = True,
    cfg: Config | None = None,
) -> Registry:
    registry = Registry()
    for tool_cls in CORE_TOOLS:
        registry.register(tool_cls(workspace))
    if include_git:
        for tool_cls in GIT_TOOLS:
            registry.register(tool_cls(workspace))
    if include_render:
        for tool_cls in RENDER_TOOLS:
            registry.register(tool_cls(workspace, cfg))
    return registry
