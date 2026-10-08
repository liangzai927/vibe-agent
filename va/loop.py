"""agent 主循环。

一次 ``run(task)`` 的生命周期：
    追加用户消息 → 调用模型 → 若有 tool_calls 就执行 → 结果回灌 → 再调用……
    直到模型不再调用工具，或触达步数上限。

其中还负责：工具审批、参数解析容错、上下文超限时的自动压缩。
"""

from __future__ import annotations

import json
from typing import Any

from .config import Config
from .llm import LLM, LLMError
from .messages import AssistantTurn, ToolCall, system_message, tool_message, user_message
from .prompts import build_system_prompt
from .tools import Registry, build_registry, safe_run
from .ui import UI
from .util import preview
from .workspace import Workspace


class Agent:
    def __init__(
        self,
        cfg: Config,
        ui: UI,
        *,
        workspace: Workspace | None = None,
        registry: Registry | None = None,
        llm: LLM | None = None,
    ) -> None:
        self.cfg = cfg
        self.ui = ui
        self.workspace = workspace or Workspace(cfg.workspace)
        self.registry = registry or build_registry(self.workspace)
        self.llm = llm or LLM(cfg)
        self.messages: list[dict[str, Any]] = [self._system_message()]
        self.compactions = 0

    # ------------------------------------------------------------------ 公开

    @property
    def system_prompt(self) -> str:
        return self.messages[0]["content"]  # type: ignore[return-value]

    def reset(self) -> None:
        self.messages = [self._system_message()]
        self.compactions = 0

    def run(self, task: str) -> str:
        """执行一个任务，返回模型的最终文本回答。"""
        self._maybe_compact(force=False)
        self.messages.append(user_message(task))

        final_text = ""
        for step in range(1, self.cfg.max_iterations + 1):
            self.ui.assistant_header(step)
            try:
                turn = self.llm.complete(
                    self.messages,
                    tools=self.registry.schemas(),
                    on_text=self.ui.stream_text,
                    on_reasoning=self.ui.stream_thinking,
                )
            except LLMError as exc:
                self.ui.error(str(exc))
                return f"[模型调用失败] {exc}"
            except KeyboardInterrupt:
                self.ui.newline()
                self.ui.warn("已中断本次生成")
                return "[用户中断]"

            self.ui.newline()
            self.messages.append(turn.to_message())

            if not turn.has_tool_calls:
                final_text = turn.content
                break

            self._execute_tool_calls(turn.tool_calls)
            self._maybe_compact(force=False)
        else:
            self.ui.warn(f"已达步数上限 {self.cfg.max_iterations}，强制停止。")
            final_text = final_text or "[已达步数上限]"

        return final_text

    # ------------------------------------------------------------------ 内部

    def _system_message(self) -> dict[str, Any]:
        prompt = build_system_prompt(self.workspace, self.registry.catalog())
        return system_message(prompt)

    def _execute_tool_calls(self, calls: list[ToolCall]) -> None:
        total = len(calls)
        for index, call in enumerate(calls, 1):
            tool = self.registry.get(call.name)

            if tool is None:
                message = (
                    f"错误：不存在名为 {call.name!r} 的工具。"
                    f"可用工具：{', '.join(self.registry.names())}"
                )
                self.ui.tool_call(index, total, call.name, "未知工具")
                self.ui.tool_result(message, ok=False)
                self.messages.append(tool_message(call.id, message))
                continue

            try:
                args = call.parsed_arguments()
            except ValueError as exc:
                message = f"错误：{exc}"
                self.ui.tool_call(index, total, call.name, "参数解析失败")
                self.ui.tool_result(message, ok=False)
                self.messages.append(tool_message(call.id, message))
                continue

            summary = tool.summarize(args)
            self.ui.tool_call(index, total, call.name, summary)

            if tool.requires_approval and not self._approved(call, args):
                message = "用户拒绝执行该操作。请换一种方式，或询问用户希望怎么做。"
                self.ui.tool_result("用户拒绝", ok=False)
                self.messages.append(tool_message(call.id, message))
                continue

            result, ok = safe_run(tool, args)
            self.ui.tool_result(result, ok=ok)
            self.messages.append(tool_message(call.id, result))

    def _approved(self, call: ToolCall, args: dict[str, Any]) -> bool:
        detail = preview(_describe(args), 120)
        return self.ui.confirm(call.name, args, detail)

    # ------------------------------------------------------------- 上下文压缩

    def _estimate_tokens(self) -> int:
        return len(json.dumps(self.messages, ensure_ascii=False)) // 3

    def _maybe_compact(self, *, force: bool) -> None:
        budget = self.cfg.context_budget
        if not force and self._estimate_tokens() < budget:
            return
        if len(self.messages) < 8:
            return

        keep_from = self._safe_cut_index(keep_recent=12)
        if keep_from <= 1:
            return

        dropped = self.messages[1:keep_from]
        summary = self._summarize(dropped)
        note = (
            "（以下是更早对话的压缩摘要，供你延续上下文；"
            "需要细节时请重新 read_file / grep 确认，不要凭记忆下结论）\n\n" + summary
        )
        self.messages = [self.messages[0], user_message(note), *self.messages[keep_from:]]
        self.compactions += 1
        self.ui.info(f"上下文已压缩（第 {self.compactions} 次），丢弃 {len(dropped)} 条历史消息")

    def _safe_cut_index(self, keep_recent: int) -> int:
        """找一个不会拆散 assistant(tool_calls)/tool 配对的切点。

        返回的下标处，消息必须是 ``user`` 角色——这样保留段永远以完整轮次开始。
        """
        index = max(1, len(self.messages) - keep_recent)
        while index < len(self.messages) and self.messages[index].get("role") != "user":
            index += 1
        return index

    def _summarize(self, dropped: list[dict[str, Any]]) -> str:
        transcript = _render_transcript(dropped)
        prompt = (
            "下面是一段编码 agent 的历史对话。请压缩成结构化摘要，"
            "保留所有对未来工作有影响的事实，不要保留寒暄。\n\n"
            "必须包含这些小节：\n"
            "## 任务目标\n"
            "## 已完成的工作（含具体文件路径）\n"
            "## 关键决策与原因\n"
            "## 遇到的错误与解决办法\n"
            "## 待办与已知问题\n\n"
            "要求：只写对话里真实出现过的信息，不要推测；"
            "文件路径、函数名、命令原样保留。\n\n"
            "=== 历史对话 ===\n" + transcript
        )
        try:
            turn: AssistantTurn = self.llm.complete([user_message(prompt)])
            text = turn.content.strip()
            return text or "（摘要生成失败，历史已丢弃）"
        except (LLMError, KeyboardInterrupt):
            return "（摘要生成失败，历史已丢弃；请重新查看文件确认现状）"


def _render_transcript(messages: list[dict[str, Any]], limit: int = 40_000) -> str:
    lines: list[str] = []
    for message in messages:
        role = message.get("role", "?")
        content = message.get("content") or ""
        if isinstance(content, list):
            content = json.dumps(content, ensure_ascii=False)
        head = f"[{role}]"
        if message.get("tool_calls"):
            names = ", ".join(c["function"]["name"] for c in message["tool_calls"])
            head += f" 调用工具: {names}"
        lines.append(f"{head} {content}")
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit] + "\n... (历史过长，已截断)"


def _describe(args: dict[str, Any]) -> str:
    for key in ("message", "command", "path", "name", "branch", "pattern"):
        if key in args:
            return str(args[key])
    return json.dumps(args, ensure_ascii=False)
