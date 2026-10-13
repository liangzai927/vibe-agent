"""与具体 SDK 解耦的消息数据结构。

LLM 层只产出 ``AssistantTurn``，主循环只消费它；
需要写回历史时再用 ``to_message()`` 转成 OpenAI 的 dict 格式。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str = "{}"
    index: int = 0

    def parsed_arguments(self) -> dict[str, Any]:
        """解析参数；空字符串按空对象处理。"""
        raw = (self.arguments or "").strip()
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"工具参数不是合法 JSON: {exc}。原始内容: {raw[:400]}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"工具参数必须是 JSON 对象，收到 {type(value).__name__}")
        return value


@dataclass(slots=True)
class AssistantTurn:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    reasoning: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def to_message(self) -> dict[str, Any]:
        """转成写回对话历史用的 assistant 消息。"""
        message: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments or "{}"},
                }
                for call in self.tool_calls
            ]
        return message


def tool_message(call_id: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def user_message(content: str) -> dict[str, Any]:
    return {"role": "user", "content": content}


def system_message(content: str) -> dict[str, Any]:
    return {"role": "system", "content": content}
