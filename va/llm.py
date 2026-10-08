"""OpenAI 兼容的流式 chat 客户端。

对外只暴露 ``LLM.complete(messages, tools) -> AssistantTurn``，
内部负责：流式增量聚合、tool_calls 分片拼接、指数退避重试。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Sequence

import openai
from openai import OpenAI

from .config import Config
from .messages import AssistantTurn, ToolCall

TextSink = Callable[[str], None]
ReasoningSink = Callable[[str], None]

_MAX_ATTEMPTS = 4


def _retryable_types() -> tuple[type[BaseException], ...]:
    """收集当前 SDK 版本里可重试的异常类型。"""
    names = (
        "APIConnectionError",
        "APITimeoutError",
        "RateLimitError",
        "InternalServerError",
        "APIStatusError",
    )
    found: list[type[BaseException]] = []
    for name in names:
        exc = getattr(openai, name, None)
        if isinstance(exc, type) and issubclass(exc, BaseException):
            found.append(exc)
    return tuple(found) or (Exception,)


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._client = OpenAI(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            max_retries=0,  # 重试由我们自己控制，便于打印提示
        )
        self._retryable = _retryable_types()

    # ------------------------------------------------------------------ public

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        on_text: TextSink | None = None,
        on_reasoning: ReasoningSink | None = None,
    ) -> AssistantTurn:
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": list(messages),
            "temperature": self.cfg.temperature,
            "stream": True,
        }
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"
        if self.cfg.max_tokens:
            payload["max_tokens"] = self.cfg.max_tokens

        last_error: BaseException | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                return self._stream_once(payload, on_text, on_reasoning)
            except self._retryable as exc:  # type: ignore[misc]
                last_error = exc
                if attempt == _MAX_ATTEMPTS:
                    break
                delay = min(2**attempt, 15)
                print(f"\n  [warn] 请求失败({type(exc).__name__})，{delay}s 后重试 {attempt}/{_MAX_ATTEMPTS - 1}")
                time.sleep(delay)
            except openai.BadRequestError as exc:
                raise LLMError(self._explain_bad_request(exc, payload)) from exc

        raise LLMError(f"模型请求连续失败 {_MAX_ATTEMPTS} 次：{last_error}") from last_error

    # ----------------------------------------------------------------- private

    def _stream_once(
        self,
        payload: dict[str, Any],
        on_text: TextSink | None,
        on_reasoning: ReasoningSink | None,
    ) -> AssistantTurn:
        stream = self._client.chat.completions.create(**payload)

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        # index -> 分片累积器；tool_calls 的 id/name 只在首个分片出现
        slots: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None

        for chunk in stream:
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            choice = choices[0]
            if getattr(choice, "finish_reason", None):
                finish_reason = choice.finish_reason

            delta = getattr(choice, "delta", None)
            if delta is None:
                continue

            piece = getattr(delta, "content", None)
            if piece:
                content_parts.append(piece)
                if on_text:
                    on_text(piece)

            thought = getattr(delta, "reasoning_content", None)
            if thought:
                reasoning_parts.append(thought)
                if on_reasoning:
                    on_reasoning(thought)

            for call_delta in getattr(delta, "tool_calls", None) or ():
                key = getattr(call_delta, "index", 0) or 0
                slot = slots.setdefault(key, {"id": "", "name": "", "arguments": ""})
                if getattr(call_delta, "id", None):
                    slot["id"] = call_delta.id
                fn = getattr(call_delta, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        slot["arguments"] += fn.arguments

        tool_calls = [
            ToolCall(
                id=slot["id"] or f"call_{index}",
                name=slot["name"],
                arguments=slot["arguments"] or "{}",
                index=index,
            )
            for index, slot in sorted(slots.items())
            if slot["name"]
        ]

        return AssistantTurn(
            content="".join(content_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            reasoning="".join(reasoning_parts),
        )

    @staticmethod
    def _explain_bad_request(exc: openai.BadRequestError, payload: dict[str, Any]) -> str:
        detail = str(exc)
        hints: list[str] = []
        if "tool" in detail.lower() and "support" in detail.lower():
            hints.append("当前模型可能不支持 function calling —— 换一个支持工具调用的模型。")
        if "context" in detail.lower() or "too long" in detail.lower():
            hints.append("上下文超长 —— 缩短任务描述，或等待 Phase 2 的自动压缩能力。")
        if "model" in detail.lower() and "not found" in detail.lower():
            hints.append(f"模型名 '{payload.get('model')}' 可能不存在，检查 VA_MODEL。")
        suffix = ("\n  → " + "\n  → ".join(hints)) if hints else ""
        return f"模型返回 400：{detail}{suffix}"
