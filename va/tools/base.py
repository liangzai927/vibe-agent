"""工具基类与注册表。

设计要点：
* 每个工具自带 JSON Schema，直接喂给模型；
* ``run()`` 只返回字符串（模型能读的文本），异常在这里被转成可读报错，
  不让一个工具炸掉整个循环；
* ``requires_approval`` 标记危险操作，由主循环决定是否询问用户。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterator

from ..util import red


class ToolError(Exception):
    """工具执行失败——会被包装成文本结果回传给模型，而不是抛穿循环。"""


class Tool(ABC):
    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}
    requires_approval: bool = False
    # True 表示可能有无法追踪的文件副作用（如执行任意 shell 命令、切换工作树），
    # 撤销栈无法可靠回退这类操作，会如实标记而不是假装成功。
    opaque: bool = False

    @abstractmethod
    def run(self, **kwargs: Any) -> str: ...

    def needs_approval(self, args: dict[str, Any]) -> bool:
        """这一次调用要不要先问用户。

        默认看类属性。工具可以按参数决定——比如「读工作区里的图」不用问，
        「读 ~/Downloads 里的图」要问。判定放在工具里而不是主循环里，
        因为只有工具自己知道哪个参数是路径。
        """
        return self.requires_approval

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def preview(self, args: dict[str, Any]) -> str | None:
        """执行前给用户看的人类可读预览（通常是一份 diff）。

        返回 None 表示没有更好的呈现方式，UI 会退回到展示原始参数。
        这里必须容错：预览失败不应该阻断审批流程。
        """
        return None

    def affected_paths(self, args: dict[str, Any]) -> list[Path]:
        """这次调用可能改动哪些文件——主循环会在执行前对它们抓快照。"""
        return []

    def after_run(self, ui: Any, result: str) -> None:
        """执行成功后给 UI 一次特殊展示的机会。默认什么都不做。

        用途：有些工具的结果值得完整打印出来（比如更新后的任务计划），
        而通用的工具结果展示为了不刷屏会把长内容截断。
        """
        return None

    def summarize(self, args: dict[str, Any]) -> str:
        """一行人类可读的调用摘要，用于终端展示。"""
        parts = []
        for key, value in args.items():
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            if len(text) > 60:
                text = text[:57] + "…"
            parts.append(f"{key}={text!r}")
        return ", ".join(parts)


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if not tool.name:
            raise ValueError(f"{type(tool).__name__} 缺少 name")
        self._tools[tool.name] = tool
        return tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)

    def catalog(self) -> str:
        """工具清单，注入系统提示词。"""
        return "\n".join(f"- {t.name}: {t.description.splitlines()[0]}" for t in self._tools.values())

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)


def safe_run(tool: Tool, args: dict[str, Any]) -> tuple[str, bool]:
    """执行工具，永不抛出。返回 (结果文本, 是否成功)。"""
    from ..workspace import WorkspaceError

    try:
        return tool.run(**args), True
    except ToolError as exc:
        return f"错误：{exc}", False
    except WorkspaceError as exc:
        # 路径越界 / 文件不存在 —— 这是最高频的一类失败，给干净的提示
        return f"错误：{exc}", False
    except TypeError as exc:
        return (
            f"错误：工具 {tool.name} 的参数不匹配（{exc}）。"
            f"请检查参数名，可用参数见工具定义。",
            False,
        )
    except Exception as exc:  # noqa: BLE001 - 兜底，绝不让工具炸穿循环
        return f"错误：{tool.name} 执行异常 {type(exc).__name__}: {exc}", False
