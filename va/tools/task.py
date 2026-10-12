"""任务状态工具。

让 agent（和你）能看见「这个任务走到哪了」。

**推进靠人点，先不自动**——用户要的是「挣来的自主权」：
先人工审核，连续一周无干预率 ≥95% 才开自动。所以现在
`task_advance` 默认 `trigger="human"`，而且**每次推进都记下有没有被干预**——
不记的话，一周后那个比例算不出来。
"""

from __future__ import annotations

import time
from typing import Any

from ..task import (
    BLOCKED,
    INTERVENTIONS,
    STATE_LABELS,
    STATES,
    Blocker,
    TaskStore,
    open_task,
)
from .base import ToolError
from .context import AgentContext, WorkspaceTool


class _TaskTool(WorkspaceTool):
    def __init__(self, ctx: AgentContext) -> None:
        super().__init__(ctx.workspace, ctx.cfg)
        self.ctx = ctx

    @property
    def store(self) -> TaskStore:
        return TaskStore(self.workspace.root)

    def _current(self, task_id: str):
        if task_id:
            task = self.store.load(task_id)
            if task is None:
                known = "、".join(t.id for t in self.store.all()) or "（还没有任务）"
                raise ToolError(f"没有 {task_id} 这个任务。现有的：{known}")
            return task
        active = self.store.active()
        if not active:
            raise ToolError(
                "现在没有进行中的任务。用 task_advance(task_id=...) 开一个，"
                "或者 task_status() 看全部。"
            )
        return active[0]


class TaskStatus(_TaskTool):
    name = "task_status"
    description = (
        "看任务走到哪了：当前状态、进度、挂起在等什么、推进历史、\n"
        "以及**无干预率**（这个数是「能不能开自动推进」的依据）。\n"
        "不给 task_id 就列全部进行中的任务。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务号，例如 ORD-312。留空列全部"},
            "timeline": {"type": "boolean", "description": "是否展开推进历史（默认 false）"},
        },
        "required": [],
    }

    def run(self, task_id: str = "", timeline: bool = False, **_: Any) -> str:
        if task_id:
            task = self.store.load(task_id)
            if task is None:
                raise ToolError(f"没有 {task_id} 这个任务")
            text = task.describe()
            if timeline:
                text += "\n\n推进历史：\n" + task.timeline()
            return text

        tasks = self.store.all()
        if not tasks:
            return (
                "还没有任何任务。\n"
                "用 task_advance(task_id=\"ORD-312\", title=\"...\") 开一个。\n"
                "任务状态存在项目里的 .vibe/tasks/ ——换个会话、过一周回来都还在。"
            )

        lines = [f"共 {len(tasks)} 个任务：", ""]
        for task in tasks:
            mark = "✓" if task.finished else ("⏸" if task.blocked else "▶")
            lines.append(f"  {mark} {task.id}  {task.title}")
            lines.append(f"      {task.label} ｜ {task.progress()}")
        lines.append("")
        lines.append("看详情：task_status(task_id=\"...\", timeline=true)")
        return "\n".join(lines)


class TaskAdvance(_TaskTool):
    name = "task_advance"
    description = (
        "把任务推进到下一个状态。任务不存在就新建。\n"
        "**提交那一步会被测试门禁拦住**——测试没跑绿推进不过去。\n"
        "如果你的推进是在修正 agent 的产出（改了代码、改了方案），"
        "用 intervention 说明，**这个记录用于将来判断能不能开自动推进**。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务号，例如 ORD-312"},
            "title": {"type": "string", "description": "任务标题（新建时用）"},
            "to": {
                "type": "string",
                "enum": list(STATES),
                "description": "推进到哪个状态。省略则推进到下一个",
            },
            "intervention": {
                "type": "string",
                "enum": list(INTERVENTIONS),
                "description": (
                    "这次推进伴随的干预：none 无（默认）/ rejected 否决过 / "
                    "edited 手改过产出 / interrupted 打断过 / rerun 要求重跑 / "
                    "redirected 改了方向"
                ),
            },
            "note": {"type": "string", "description": "说明，会记进推进历史"},
            "trigger": {
                "type": "string",
                "enum": ["human", "auto"],
                "description": "谁推的。默认 human（现在还不该自动推）",
            },
        },
        "required": ["task_id"],
    }

    def run(
        self,
        task_id: str,
        title: str = "",
        to: str = "",
        intervention: str = "none",
        note: str = "",
        trigger: str = "human",
        **_: Any,
    ) -> str:
        task_id = (task_id or "").strip()
        if not task_id:
            raise ToolError("task_id 不能为空")

        task, created = open_task(self.workspace.root, task_id, title)
        if created and not task.title:
            task.title = title

        target = (to or "").strip() or task.next_state()
        if not target:
            raise ToolError(f"{task.id} 已经在最后一步了（{task.label}）")
        if target not in STATES:
            raise ToolError(
                f"不认识的状态 {target!r}。可用：{' / '.join(STATES)}"
            )

        if task.blocked and target != BLOCKED:
            # 从挂起恢复：清掉阻塞项
            task.blockers.clear()
            if task.resume_to and target == task.resume_to:
                pass

        # 门禁参与推进：提交那一步不能绕过去
        gate_note = self._check_gate(target, intervention)

        previous = task.state
        # **先记再改状态**。record() 从 self.state 取「从哪来」，
        # 先改了的话历史会变成「建分支 → 建分支」这种废话。
        task.record(
            target,
            trigger=trigger if trigger in ("human", "auto") else "human",
            intervention=intervention,
            note=note,
        )
        task.state = target
        if target != BLOCKED:
            task.resume_to = ""
        path = self.store.save(task)

        lines = [
            f"{'已新建 ' if created else ''}{task.id}："
            f"{STATE_LABELS.get(previous, previous)} → {STATE_LABELS.get(target, target)}"
        ]
        if gate_note:
            lines.append(f"  {gate_note}")
        lines.append(f"  进度：{task.progress()}")
        nxt = task.next_state()
        if nxt:
            lines.append(f"  下一步：{STATE_LABELS.get(nxt, nxt)}")
        lines.append(f"  记在 {path}")
        return "\n".join(lines)

    def _check_gate(self, target: str, intervention: str) -> str:
        """推进到「提交」时要过测试门禁。

        **和 git_commit 用的是同一个台账**——两处都拦，
        因为推进入「提交」状态和真的提交是两件事，但都不该在没测过时发生。
        """
        if target != "commit":
            return ""
        ledger = getattr(self.ctx, "ledger", None)
        if ledger is None:
            return ""
        workflow = getattr(self.ctx, "workflow", None)
        from ..gate import fingerprint

        verdict = ledger.evaluate(
            workflow, current=fingerprint(self.workspace.root)
        )
        if verdict.ok:
            return "测试门禁：通过"
        if intervention != "none":
            return f"⚠ 测试门禁没过，但已标注干预（{INTERVENTIONS[intervention]}）：" + \
                   "；".join(verdict.problems)[:200]
        raise ToolError(
            f"{verdict.describe()}\n\n"
            "**测试没绿不能推进到提交。** 先把测试跑通，"
            "或者确认这次改动不需要测试后用 intervention 标注。"
        )


class TaskBlock(_TaskTool):
    name = "task_block"
    description = (
        "把任务挂起（等后端、等设计、等人工决定）。\n"
        "**必须写清楚「什么条件下可以继续」，而且最好是机器能判定的**\n"
        "——「等后端」会变成永远等下去，「GET /api/orders 返回 200 且含 total」才行。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务号"},
            "what": {"type": "string", "description": "在等什么，例如「订单列表接口」"},
            "check": {
                "type": "string",
                "description": "可判定的恢复条件，例如「GET /api/orders 返回 200 且含 total」",
            },
            "kind": {
                "type": "string",
                "enum": ["backend", "design", "human", "other"],
                "description": "等谁。默认 backend",
            },
        },
        "required": ["task_id", "what"],
    }

    def run(
        self,
        task_id: str,
        what: str,
        check: str = "",
        kind: str = "backend",
        **_: Any,
    ) -> str:
        task = self._current(task_id)
        if not check.strip():
            raise ToolError(
                "要写清楚「什么条件下可以继续」。\n"
                "「等后端」不行——那是永远等下去；"
                "「GET /api/orders 返回 200 且含 total」才行。"
            )

        task.resume_to = task.state
        task.blockers.append(
            Blocker(what=what.strip(), check=check.strip(), kind=kind)
        )
        task.record(
            BLOCKED,
            trigger="human",
            intervention="none",
            note=f"等 {what}",
        )
        task.state = BLOCKED
        task.updated_at = time.time()
        self.store.save(task)

        return (
            f"{task.id} 已挂起\n"
            f"  在等：{what}（{kind}）\n"
            f"  恢复条件：{check}\n"
            f"  挂起前是：{STATE_LABELS.get(task.resume_to, task.resume_to)}\n"
            f"  进度：{task.progress()}"
        )


TASK_TOOLS = (TaskStatus, TaskAdvance, TaskBlock)
