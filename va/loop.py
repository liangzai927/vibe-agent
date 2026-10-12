"""agent 主循环。

一次 ``run(task)`` 的生命周期：
    追加用户消息 → 调用模型 → 若有 tool_calls 就执行 → 结果回灌 → 再调用……
    直到模型不再调用工具，或触达步数上限。

这一层还负责四件横切的事：
* **审批**  —— 改动前把 diff 摆给用户看，而不是展示工具原始参数；
* **撤销**  —— 每一步开始前抓文件快照，``/undo`` 能整步回退；
* **持久化** —— 每条消息追加写进会话文件，退出后可恢复；
* **压缩**  —— 上下文超限时summary旧历史，且保证不破坏消息配对。
"""

from __future__ import annotations

import json
from typing import Any

from .config import Config
from .history import Checkpoint, UndoStack
from .llm import LLM, LLMError
from .messages import AssistantTurn, ToolCall, system_message, tool_message, user_message
from .plan import Plan, PlanBoard
from .prompts import build_system_prompt
from .session import SessionStore
from .tools import Registry, build_registry, safe_run
from .tools.context import AgentContext
from .tools.delegate import Delegate
from .tools.review import ReviewChanges
from .tools.plan import UpdatePlan
from .ui import UI
from .util import preview, red
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
        session: SessionStore | None = None,
        undo: UndoStack | None = None,
        history: list[dict[str, Any]] | None = None,
        extra_instructions: str = "",
        plan_board: PlanBoard | None = None,
        plan: Plan | None = None,
        agent_depth: int = 0,
    ) -> None:
        self.cfg = cfg
        self.ui = ui
        self.workspace = workspace or Workspace(cfg.workspace)

        self.plan_board = plan_board or PlanBoard()
        # 最近几次工具调用，用来识别「原地打转」
        self._recent_calls: list[tuple[str, str]] = []
        if plan is not None:
            self.plan_board.plan = plan

        self.llm = llm or LLM(cfg)
        self.session = session
        self.extra_instructions = extra_instructions
        self.compactions = 0

        # 注意用 `is None` 而不是 `undo or ...`：
        # UndoStack 定义了 __len__，**空的撤销栈是 falsy 的**，
        # 用 or 会把它丢掉换成新对象——父 agent 传下来的空栈会被无声替换，
        # 子 agent 的改动于是撤不掉。
        self.undo = undo if undo is not None else UndoStack(self.workspace.root)

        from .process import ProcessManager

        # 每个 Agent 一套后台进程：会话结束（或进程退出）时统一清理，
        # 不留孤儿占着端口
        self.processes = ProcessManager(self.workspace.root)

        self.agent_ctx = AgentContext(
            workspace=self.workspace,
            cfg=cfg,
            plan_board=self.plan_board,
            undo=self.undo,
            ui=ui,
            llm=self.llm,
            depth=agent_depth,
            max_depth=cfg.max_delegate_depth,
            processes=self.processes,
        )

        if registry is None:
            self.registry = build_registry(self.workspace, cfg=cfg, ctx=self.agent_ctx)
        else:
            self.registry = registry
            # 无条件重绑：build_registry 在没收到 ctx 时会自己造看板、
            # 也不会注册委派工具。重绑保证工具看到的状态和 agent 持有的完全一致。
            self.registry.register(UpdatePlan(self.plan_board))
            if self.agent_ctx.can_delegate():
                self.registry.register(Delegate(self.agent_ctx))
                self.registry.register(ReviewChanges(self.agent_ctx))
            else:
                self.registry.unregister("delegate")
                self.registry.unregister("review_changes")

        self.messages: list[dict[str, Any]] = [self._system_message()]
        if history:
            self.messages.extend(history)

    # ------------------------------------------------------------------ 公开

    @property
    def system_prompt(self) -> str:
        return self.messages[0]["content"]  # type: ignore[return-value]

    @property
    def session_id(self) -> str | None:
        return self.session.meta.id if self.session else None

    @property
    def plan(self) -> Plan | None:
        return self.plan_board.plan

    def set_model(self, name: str) -> None:
        """切换模型。

        要重建 LLM 客户端，并且**同步给 agent_ctx**——否则委派出去的子 agent
        还在用旧模型，而这一点从主对话里完全看不出来。
        """
        self.cfg.model = name
        self.llm = LLM(self.cfg)
        self.agent_ctx.llm = self.llm

    def reset(self) -> None:
        self.messages = [self._system_message()]
        self.compactions = 0
        self.undo = UndoStack(self.workspace.root)
        self.plan_board.clear()

    def run(self, task: str, *, display: str | None = None) -> str:
        """执行一个任务，返回模型的最终文本回答。

        ``display`` 是给用户看的原文。`@路径` 引用会把文件内容并进
        ``task``，但界面上该显示用户原话——展开后那一大坨不是他写的。
        """
        self._maybe_compact(force=False)
        self._append(user_message(task))
        # 会话文件里存**展开后**的版本：那是 agent 实际看到的东西，
        # 恢复会话时不能少。界面上另用 display 显示原文。
        if self.session is not None and not self.session.meta.title:
            self.session.set_title(preview(display or task, 60))

        final_text = ""
        reason = "max_steps"
        steps_used = 0

        for step in range(1, self.cfg.max_iterations + 1):
            steps_used = step
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
                self._finish_session("llm_error", steps_used)
                return f"[模型调用失败] {exc}"
            except KeyboardInterrupt:
                self.ui.newline()
                self.ui.warn("已中断本次生成")
                self._finish_session("interrupted", steps_used)
                return "[用户中断]"

            self.ui.newline()
            self._append(turn.to_message())

            if not turn.has_tool_calls:
                final_text = turn.content
                reason = "finished"
                break

            self._execute_tool_calls(turn.tool_calls, step)

            # 真正该防的是**打转**，不是干活多。
            # 同一个工具用同样的参数连着调三次，说明它卡住了——
            # 再给 200 步也只会重复这三次。这比按步数一刀切准确得多：
            # 一个大任务跑 80 步是正常的，原地转三圈是不正常的。
            stuck = self._detect_loop(turn.tool_calls)
            if stuck:
                self.ui.warn(stuck)
                final_text = final_text or stuck
                reason = "stuck"
                break

            self._maybe_compact(force=False)
        else:
            # 走到这儿说明连上限都撞到了。不装作任务完成，也不丢下干过的活——
            # 把话说明白，让用户决定是接着做还是拆小。
            limit = self.cfg.max_iterations
            self.ui.warn(
                f"已经执行到步数上限（{limit} 步）还没结束，先停在这里。\n"
                f"  两种可能：任务太大，或者卡在某个循环里。\n"
                f"  接着做：加大上限（--max-steps 500）后 /continue；"
                f"或者把任务拆成几步分别交代。"
            )
            final_text = (
                (final_text + "\n\n") if final_text else ""
            ) + (
                f"[已达步数上限（{limit} 步），任务可能不完整。"
                f"已完成的部分都在会话里，可以加大上限后用 --continue 接着做。]"
            )

        self._finish_session(reason, steps_used)
        return final_text

    # ---------------------------------------------------------------- 撤销

    def undo_last(self, count: int = 1) -> str:
        """回退最近 count 次文件改动，并把这个事实写回对话历史。"""
        taken, blocked = self.undo.undo(count)

        lines: list[str] = []
        restored: list[str] = []
        for checkpoint in taken:
            files = [
                f"{snap.code} {self.undo.rel(path)}"
                for path, snap in checkpoint.snapshots.items()
            ]
            restored.extend(files)
            if files:
                lines.append(
                    f"已还原第 {checkpoint.index} 步（{checkpoint.label}）：{'、'.join(files)}"
                )

        if not taken and not blocked:
            return "没有可撤销的步骤。"
        if not taken:
            lines.append("没有可撤销的文件改动。")
        for reason in blocked:
            lines.append(f"{red('未还原')}：{reason}")

        if taken:
            # 追加说明而不是删除历史——会话文件是追加写的，而且模型需要知道磁盘已回退
            note = (
                "[用户操作] 用户撤销了最近 %d 次文件改动。已还原：%s。\n"
                "当前磁盘内容已经回退，请重新 read_file 确认现状后再继续，"
                "不要依赖上方对话里关于这些文件的旧结论。"
                % (len(taken), "、".join(restored) if restored else "（无）")
            )
            self._append(user_message(note))
        return "\n".join(lines)

    # ------------------------------------------------------------------ 内部

    def _system_message(self) -> dict[str, Any]:
        # 恢复会话时可能已经有一份计划，直接写进系统提示词，
        # 这样模型第一轮就知道自己站在哪，不用先问一遍
        extra = self.extra_instructions
        reminder = self.plan_board.reminder()
        if reminder:
            extra = (extra + "\n\n" + reminder).strip()

        prompt = build_system_prompt(self.workspace, self.registry.catalog(), extra)
        return system_message(prompt)

    def _append(self, message: dict[str, Any]) -> None:
        """所有消息的唯一入口：同时进内存和会话文件。"""
        self.messages.append(message)
        if self.session is not None:
            try:
                self.session.append_message(message)
            except OSError as exc:
                self.ui.warn(f"会话写入失败（不影响继续工作）：{exc}")

    def _finish_session(self, reason: str, steps: int) -> None:
        if self.session is None:
            return
        try:
            self.session.append_end(reason, steps)
        except OSError:
            pass

    def _detect_loop(self, calls: list[ToolCall]) -> str | None:
        """同一个工具用同样的参数连着调三次，就是在原地打转。

        只比对**连续**的调用：中间插了别的动作说明它在推进，不算卡住。
        参数用原始 JSON 字符串比——解析失败时也能比。
        """
        signatures = [(call.name, call.arguments) for call in calls]
        self._recent_calls.extend(signatures)
        del self._recent_calls[:-9]  # 只留最近几条，别无限长

        repeat = self.cfg.loop_repeat_limit
        if repeat <= 0 or len(self._recent_calls) < repeat:
            return None

        tail = self._recent_calls[-repeat:]
        if any(sig != tail[0] for sig in tail):
            return None

        name = tail[0][0]
        return (
            f"检测到原地打转：连续 {repeat} 次用相同参数调用 {name}，先停在这里。\n"
            f"  多半是工具返回的结果让它走进了死胡同——换一种做法，"
            f"或者把当前情况告诉我。"
        )

    def _execute_tool_calls(self, calls: list[ToolCall], step: int) -> None:
        total = len(calls)
        names = "、".join(dict.fromkeys(call.name for call in calls))
        checkpoint = self.undo.begin(names or f"{total} 个工具调用")
        mutating_ran = False
        plan_updates_before = self.plan_board.updates

        try:
            for index, call in enumerate(calls, 1):
                tool = self.registry.get(call.name)

                if tool is None:
                    message = (
                        f"错误：不存在名为 {call.name!r} 的工具。"
                        f"可用工具：{', '.join(self.registry.names())}"
                    )
                    self.ui.tool_call(index, total, call.name, "未知工具")
                    self.ui.tool_result(message, ok=False)
                    self._append(tool_message(call.id, message))
                    continue

                try:
                    args = call.parsed_arguments()
                except ValueError as exc:
                    message = f"错误：{exc}"
                    self.ui.tool_call(index, total, call.name, "参数解析失败")
                    self.ui.tool_result(message, ok=False)
                    self._append(tool_message(call.id, message))
                    continue

                summary = tool.summarize(args)
                self.ui.tool_call(index, total, call.name, summary)

                # 动盘之前先抓快照
                targets = tool.affected_paths(args)
                for path in targets:
                    self.undo.capture(checkpoint, path)
                if tool.opaque:
                    self.undo.mark_opaque(checkpoint, f"{call.name} 的副作用无法追踪")

                if tool.needs_approval(args) and not self._approved(call, args, tool):
                    message = "用户拒绝执行该操作。请换一种方式，或询问用户希望怎么做。"
                    self.ui.tool_result("用户拒绝", ok=False)
                    self._append(tool_message(call.id, message))
                    continue

                result, ok = safe_run(tool, args)
                self.ui.tool_result(result, ok=ok)
                if ok:
                    # 有些工具需要特殊展示（比如把计划完整打出来）。
                    # 展示失败绝不能影响主流程。
                    try:
                        tool.after_run(self.ui, result)
                    except Exception:  # noqa: BLE001
                        pass
                self._append(tool_message(call.id, result))

                if ok and (targets or tool.opaque):
                    mutating_ran = True
        finally:
            if mutating_ran:
                self.undo.commit(checkpoint)
            # 计划变过就落盘：它建得很早，只留在内存里的话恢复会话就丢了
            if self.plan_board.updates != plan_updates_before:
                self._persist_plan()

    def _persist_plan(self) -> None:
        if self.session is None:
            return
        plan = self.plan_board.plan
        try:
            self.session.append_plan(plan.to_dict() if plan else None)
        except OSError as exc:
            self.ui.warn(f"计划写入会话失败（不影响继续工作）：{exc}")

    def _approved(self, call: ToolCall, args: dict[str, Any], tool: Any) -> bool:
        preview_text = self._safe_preview(tool, args)
        detail = preview(preview_text.splitlines()[0], 100) if preview_text else preview(_describe(args), 120)
        return self.ui.confirm(call.name, args, detail, preview_text)

    @staticmethod
    def _safe_preview(tool: Any, args: dict[str, Any]) -> str | None:
        """预览失败绝不能阻断审批流程。"""
        try:
            return tool.preview(args)
        except Exception:  # noqa: BLE001
            return None

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

        # 计划一定要在压缩后重新注入。
        # 它通常是在很早的步骤里建的，按「保留最近 N 条」的规则必然被丢掉，
        # 结果就是模型丢了任务清单、开始重复劳动。
        reminder = self.plan_board.reminder()
        if reminder:
            note += "\n\n" + reminder

        self.messages = [self.messages[0], user_message(note), *self.messages[keep_from:]]
        self.compactions += 1
        self.ui.info(f"上下文已压缩（第 {self.compactions} 次），丢弃 {len(dropped)} 条历史消息")

        if self.session is not None:
            try:
                self.session.append_compact(self.messages)
            except OSError as exc:
                self.ui.warn(f"压缩结果写入会话失败：{exc}")

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
            return turn.content.strip() or "（摘要生成失败，历史已丢弃）"
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
