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
from typing import Any, Iterator

from ..util import red


class ToolError(Exception):
    """工具执行失败——会被包装成文本结果回传给模型，而不是抛穿循环。"""


class Tool(ABC):
    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}
    requires_approval: bool = False

    @abstractmethod
    def run(self, **kwargs: Any) -> str: ...

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

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
    try:
        return tool.run(**args), True
    except ToolError as exc:
        return f"错误：{exc}", False
    except TypeError as exc:
        return (
            f"错误：工具 {tool.name} 的参数不匹配（{exc}）。"
            f"请检查参数名，可用参数见工具定义。",
            False,
        )
    except Exception as exc:  # noqa: BLE001 - 兜底，绝不让工具炸穿循环
        return f"错误：{tool.name} 执行异常 {type(exc).__name__}: {exc}", False
