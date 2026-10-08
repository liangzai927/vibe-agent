"""子 agent 委派。

**为什么需要它**：有两种活会拖垮主对话——

1. 「在几十个文件里找出这个报错是谁抛的」——过程要翻十几个文件，结论只有一行；
2. 「独立审查这份改动」——同一个人既写又审，天然会带着自己刚才的思路看。

前者的问题是**上下文被过程占满**，后者的问题是**缺少独立视角**。
两者都能用「换个干净上下文去做，只把结论带回来」解决。

这里刻意**不允许递归委派**：子 agent 不能再开子 agent。省下的那点并行度
不值得换来失控的成本和难以追踪的调用链。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from ..util import preview
from ..workspace import Workspace
from .base import Tool, ToolError
from .context import AgentContext

# 只读模式下允许的工具。这些工具不会改动工作区内容。
READ_ONLY_TOOLS = (
    "read_file",
    "list_dir",
    "glob",
    "grep",
    "read_document",
    "read_design",
    "pixel_diff",
    "render_page",
    "git_status",
    "git_diff",
    "git_log",
)

# 写模式下额外允许的（仍然拿不到 git_push、git_init 和 delegate）
WRITE_EXTRA_TOOLS = (
    "write_file",
    "edit_file",
    "run_bash",
    "git_create_branch",
    "git_checkout",
    "git_commit",
    "update_plan",
)

SUBAGENT_SYSTEM = """你是一个**子 agent**，被主 agent 派来做一件自成一体的活。

规则：
- 你看不到主 agent 的对话历史，所以只依据任务描述和你在工作区里查到的东西行动。
- 专注完成任务描述里那件事，不要顺手做别的改动。
- 完成后用一小段话给出结论。**结论要自包含**——主 agent 只拿到这段文字，
  它看不到你翻过的文件。引用代码时给出 `文件:行号`。
- 不确定的地方直接说不确定，不要为了显得完整而编造。
- 需要动手改代码时，只改任务要求的部分。"""


@dataclass
class DelegateReport:
    ok: bool
    text: str
    steps: int = 0
    tools_used: list[str] = field(default_factory=list)
    truncated: bool = False

    def summary(self, label: str, mode: str) -> str:
        tools = "、".join(self.tools_used) if self.tools_used else "（没有调用工具）"
        lines = [
            f"子 agent『{label}』已完成（{mode} 模式，{self.steps} 步）",
            f"  用到的工具：{tools}",
        ]
        if self.truncated:
            lines.append(f"  ⚠ 它达到步数上限就停了，结论可能不完整")
        lines.append("")
        lines.append(self.text.strip() or "（它没有给出结论）")
        return "\n".join(lines)


# --------------------------------------------------------------------- 装配


def child_registry(ctx: AgentContext, mode: str, board: Any):
    """给子 agent 装配工具集。只读模式真的只给只读工具。"""
    from . import build_registry, filter_registry

    registry = build_registry(ctx.workspace, cfg=ctx.cfg, plan_board=board)
    allowed = READ_ONLY_TOOLS + (WRITE_EXTRA_TOOLS if mode == "write" else ())
    return filter_registry(registry, only=set(allowed))


def spawn_subagent(
    ctx: AgentContext,
    *,
    task: str,
    mode: str = "read",
    context: str = "",
    max_steps: int = 24,
    system_prompt: str = SUBAGENT_SYSTEM,
    label: str | None = None,
) -> tuple[DelegateReport, str]:
    """起一个干净上下文的子 agent 跑完任务，返回 (报告, 标签)。

    委派工具和审查工具都走这里——它们只差一个提示词和一段任务描述。
    """
    from ..loop import Agent
    from ..plan import PlanBoard

    if ctx.ui is None:
        raise ToolError("内部错误：子 agent 没有 UI 可用。")
    if ctx.cfg is None:
        raise ToolError("内部错误：子 agent 缺少配置。")

    tag = label or preview(task, 24)
    board = PlanBoard()
    registry = child_registry(ctx, mode, board)
    child_ui = SubagentUI(ctx.workspace, ctx.ui, tag)

    # 继承父 agent 的配置，但按这次委派的要求收紧步数
    child_cfg = dataclasses.replace(
        ctx.cfg, max_iterations=max(1, min(int(max_steps), 60))
    )

    child = Agent(
        child_cfg,
        child_ui,
        workspace=ctx.workspace,
        registry=registry,
        # 复用父 agent 的客户端实例：重建一个既浪费连接，
        # 也让测试没法注入假模型
        llm=ctx.llm,
        session=None,  # 子 agent 的对话不落主会话文件
        undo=ctx.undo,  # 共用撤销栈，否则它改的文件 /undo 撤不掉
        extra_instructions=system_prompt,
        plan_board=board,
        agent_depth=ctx.depth + 1,
    )

    brief = task
    if context.strip():
        brief = f"【背景】\n{context.strip()}\n\n【任务】\n{task}"

    answer = child.run(brief)

    tools_used = sorted(
        {
            call["function"]["name"]
            for message in child.messages
            if message.get("role") == "assistant" and message.get("tool_calls")
            for call in message["tool_calls"]
        }
    )
    steps = sum(
        1 for m in child.messages if m.get("role") == "assistant" and m.get("tool_calls")
    )
    truncated = "步数上限" in answer

    return (
        DelegateReport(
            ok=bool(answer.strip()),
            text=answer,
            steps=steps,
            tools_used=tools_used,
            truncated=truncated,
        ),
        tag,
    )


# --------------------------------------------------------------------- 工具


class Delegate(Tool):
    name = "delegate"
    description = (
        "把一块自成一体的子任务交给独立上下文的子 agent，只拿回它的结论。\n"
        "适合：\n"
        "  · 探索性调研——「这个报错是从哪抛出来的」，过程很长但结论很短；\n"
        "  · 独立审查——换一个干净上下文看你的改动（也可以用更专门的 review_changes）；\n"
        "  · 任何「翻很多文件、只要一个答案」的活。\n"
        "子 agent **看不到你的对话**，所以任务描述必须自包含：写清楚背景、"
        "要它在哪些文件/目录里找、期望的结论形式。\n"
        "子 agent 不能再往下委派。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "要它做什么。必须自包含：背景 + 范围 + 期望的结论形式",
            },
            "mode": {
                "type": "string",
                "enum": ["read", "write"],
                "description": (
                    "read（默认）= 只读工具，不会改动任何文件；"
                    "write = 允许改代码和提交，但不会推送"
                ),
            },
            "context": {
                "type": "string",
                "description": "额外背景，例如相关的需求原文或你已经查到的线索（可选）",
            },
            "max_steps": {
                "type": "integer",
                "description": "最多让它走多少步，默认 24。调研类可以放宽，审查类十几步通常够",
            },
        },
        "required": ["task"],
    }
    requires_approval = False  # 具体要不要批准，取决于它实际调用的工具

    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    def run(
        self,
        task: str,
        mode: str = "read",
        context: str = "",
        max_steps: int = 24,
        **_: Any,
    ) -> str:
        text = (task or "").strip()
        if not text:
            raise ToolError("task 不能为空。要委派什么，写清楚。")

        mode = (mode or "read").strip().lower()
        if mode not in ("read", "write"):
            raise ToolError(f"mode 只能是 read 或 write，收到 {mode!r}")
        if not self.ctx.can_delegate():
            raise ToolError(
                f"委派深度已达上限（{self.ctx.max_depth} 层）。请自己完成这个子任务，"
                f"不要再往下派。"
            )

        report, label = spawn_subagent(
            self.ctx, task=text, mode=mode, context=context, max_steps=max_steps
        )
        return report.summary(label, mode)


class SubagentUI:
    """把子 agent 的输出缩进转发给父 UI。

    让它复用父 UI 的审批通道：你在子 agent 要改文件时照样会被问到，
    只是提示前面会带上子 agent 的标签，免得你分不清是谁在请求。
    """

    def __init__(self, workspace: Workspace, parent: Any, label: str) -> None:
        self.workspace = workspace
        self.parent = parent
        self.label = label
        self.auto_approve = getattr(parent, "auto_approve", False)
        self.show_thinking = False

    # ------------------------------------------------------------- 展示类

    def assistant_header(self, step: int) -> None:
        self.parent.newline()
        self.parent.info(f"┌ 子 agent『{self.label}』第 {step} 步")

    def tool_call(self, index: int, total: int, name: str, summary: str) -> None:
        self.parent.newline()
        self.parent.info(f"│ [{self.label}] {name}({summary})")

    def tool_result(self, text: str, ok: bool) -> None:
        first = (text.strip().splitlines() or ["(空)"])[0]
        mark = "✓" if ok else "✗"
        self.parent.info(f"│ [{self.label}] {mark} {preview(first, 88)}")

    def stream_text(self, piece: str) -> None:
        # 子 agent 的流式输出太吵，而且你只关心它最后那句话
        return None

    def stream_thinking(self, piece: str) -> None:
        return None

    def newline(self) -> None:
        return None

    def show_plan(self, plan: Any) -> None:
        self.parent.info(
            f"│ [{self.label}] 计划：{plan.goal}（{plan.done_count}/{plan.total}）"
        )

    # ------------------------------------------------------------- 交互类

    def confirm(
        self, tool_name: str, args: dict, detail: str, preview_text: str | None = None
    ) -> bool:
        return self.parent.confirm(tool_name, args, detail, preview_text)

    def info(self, text: str) -> None:
        self.parent.info(f"  {text}")

    def warn(self, text: str) -> None:
        self.parent.warn(f"[{self.label}] {text}")

    def error(self, text: str) -> None:
        self.parent.error(f"[{self.label}] {text}")

    def success(self, text: str) -> None:
        self.parent.success(f"[{self.label}] {text}")

    def user_prompt(self) -> str:
        return ""


DELEGATE_TOOLS = (Delegate,)
