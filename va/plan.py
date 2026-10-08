"""任务计划：让 agent 在多步任务里不迷路。

为什么需要它：

* 不分解就跑，模型会来回打转、重复劳动、忘记已经做过什么；
* 上下文一压缩，中间那句「A 已经改完了」就没了，它会从头再来；
* 用户看不到进度，只知道它一直在转，不知道卡在哪一步。

计划是一份**显式的、会被渲染出来的状态**，而不是模型脑子里的隐性推理。
每次更新都会原样回灌给模型，压缩后还会重新注入——这样它随时知道自己站在哪。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# 状态用普通字符串而不是 Enum：要直接进 JSON、进工具参数、进会话文件，
# 字符串让这三处都不用做转换。
PENDING = "pending"
IN_PROGRESS = "in_progress"
DONE = "done"
BLOCKED = "blocked"
SKIPPED = "skipped"

VALID_STATUSES = (PENDING, IN_PROGRESS, DONE, BLOCKED, SKIPPED)

STATUS_MARK = {
    PENDING: "☐",
    IN_PROGRESS: "▸",
    DONE: "☑",
    BLOCKED: "✗",
    SKIPPED: "–",
}

MAX_STEPS = 30


@dataclass
class PlanStep:
    title: str
    status: str = PENDING
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"title": self.title, "status": self.status}
        if self.note:
            data["note"] = self.note
        return data


@dataclass
class Plan:
    goal: str
    steps: list[PlanStep] = field(default_factory=list)
    revision: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # ------------------------------------------------------------------ 统计

    @property
    def total(self) -> int:
        return len(self.steps)

    @property
    def done_count(self) -> int:
        return sum(1 for step in self.steps if step.status == DONE)

    @property
    def remaining(self) -> list[PlanStep]:
        return [s for s in self.steps if s.status in (PENDING, IN_PROGRESS, BLOCKED)]

    @property
    def current(self) -> PlanStep | None:
        for step in self.steps:
            if step.status == IN_PROGRESS:
                return step
        return None

    @property
    def is_finished(self) -> bool:
        return bool(self.steps) and all(
            step.status in (DONE, SKIPPED) for step in self.steps
        )

    # ------------------------------------------------------------------ 渲染

    def render(self, *, compact: bool = False) -> str:
        """渲染成人（和模型）都能一眼看懂的清单。"""
        if compact:
            return f"[计划] {self.goal} —— {self.done_count}/{self.total} 已完成"

        lines = [f"目标  {self.goal}", f"进度  {self.done_count}/{self.total}"]
        if not self.steps:
            lines.append("（还没有步骤——先把它拆开）")
            return "\n".join(lines)

        lines.append("")
        width = len(str(self.total))
        for index, step in enumerate(self.steps, 1):
            mark = STATUS_MARK.get(step.status, "?")
            line = f"  {mark} {index:>{width}}. {step.title}"
            if step.status == BLOCKED and step.note:
                line += f"\n      ↳ 卡住：{step.note}"
            elif step.status == IN_PROGRESS and step.note:
                line += f"\n      ↳ {step.note}"
            lines.append(line)

        if self.is_finished:
            lines.append("")
            lines.append("所有步骤都已完成。")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 序列化

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "steps": [step.to_dict() for step in self.steps],
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Plan":
        return cls(
            goal=str(data.get("goal", "")),
            steps=[
                PlanStep(
                    title=str(item.get("title", "")),
                    status=str(item.get("status", PENDING)),
                    note=str(item.get("note", "") or ""),
                )
                for item in data.get("steps", [])
                if isinstance(item, dict)
            ],
            revision=int(data.get("revision", 1)),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
        )


def normalize_steps(raw: Any) -> tuple[list[PlanStep], list[str]]:
    """把模型给的步骤列表规范化，并挑出值得提醒它的问题。

    返回 (步骤列表, 提醒)。**永远不因为格式问题直接报错**——计划本身是辅助
    手段，让它失败反而会打断正经工作。能修的修，修不了的记下来告诉模型。
    """
    warnings: list[str] = []

    if not isinstance(raw, list):
        return [], ["steps 必须是数组，每个元素形如 {\"title\": \"...\", \"status\": \"pending\"}"]

    steps: list[PlanStep] = []
    seen_titles: set[str] = set()

    for index, item in enumerate(raw, 1):
        if isinstance(item, str):
            title, status, note = item, PENDING, ""
        elif isinstance(item, dict):
            title = str(item.get("title", "") or "").strip()
            status = str(item.get("status", PENDING) or PENDING).strip().lower()
            note = str(item.get("note", "") or "").strip()
        else:
            warnings.append(f"第 {index} 项格式不对，已跳过")
            continue

        if not title:
            warnings.append(f"第 {index} 项没有 title，已跳过")
            continue

        if status not in VALID_STATUSES:
            warnings.append(
                f"第 {index} 项状态 {status!r} 不认识，已按 {PENDING} 处理"
                f"（合法值：{'、'.join(VALID_STATUSES)}）"
            )
            status = PENDING

        # 标题归一化后去重：重复的步骤多半是模型自己绕晕了
        key = title.lower()
        if key in seen_titles:
            warnings.append(f"第 {index} 项「{title}」与前面重复，已跳过")
            continue
        seen_titles.add(key)

        steps.append(PlanStep(title=title, status=status, note=note))

    if len(steps) > MAX_STEPS:
        warnings.append(f"步骤太多（{len(steps)} 条），只保留前 {MAX_STEPS} 条。请合并同类项。")
        steps = steps[:MAX_STEPS]

    running = [s for s in steps if s.status == IN_PROGRESS]
    if len(running) > 1:
        warnings.append(
            "同时有 "
            + str(len(running))
            + " 个步骤处于 in_progress。一次只推进一件事，其余的改回 pending。"
        )

    return steps, warnings


class PlanBoard:
    """计划的可变容器。

    工具层和主循环共享同一个实例：工具负责写入，主循环负责在压缩后重新注入。
    """

    def __init__(self) -> None:
        self.plan: Plan | None = None
        self.updates = 0

    # ------------------------------------------------------------------ 更新

    def update(self, goal: str, raw_steps: Any) -> tuple[Plan, list[str]]:
        goal_text = (goal or "").strip()
        steps, warnings = normalize_steps(raw_steps)

        if self.plan is None:
            if not goal_text:
                warnings.append("第一次给计划时必须写清楚 goal（这次任务要达成什么）。")
                goal_text = "(未填写目标)"
            plan = Plan(goal=goal_text, steps=steps)
        else:
            # 目标允许后续修正；不填就沿用原来的
            plan = Plan(
                goal=goal_text or self.plan.goal,
                steps=steps,
                revision=self.plan.revision + 1,
                created_at=self.plan.created_at,
            )
            if not steps:
                warnings.append(
                    "这次没给任何步骤，计划被清空了。如果你只是想更新状态，"
                    "请把完整步骤列表带上（这个工具是整体替换，不是增量修改）。"
                )

        self.plan = plan
        self.updates += 1
        return plan, warnings

    def clear(self) -> None:
        self.plan = None
        self.updates = 0

    # ------------------------------------------------------------------ 查询

    def render(self, *, compact: bool = False) -> str:
        if self.plan is None:
            return "（当前没有计划）"
        return self.plan.render(compact=compact)

    def reminder(self) -> str:
        """注入对话用的提醒文本。压缩之后尤其需要——否则模型会忘了进度。"""
        if self.plan is None:
            return ""
        plan = self.plan
        if plan.is_finished:
            return f"[计划状态] 所有步骤已完成。目标：{plan.goal}"

        current = plan.current
        lines = [
            f"[计划状态] 目标：{plan.goal}（{plan.done_count}/{plan.total} 已完成）",
            plan.render(),
        ]
        if current is None and plan.remaining:
            lines.append("")
            lines.append("注意：当前没有标记为 in_progress 的步骤——要么挑一个继续，要么更新计划。")
        return "\n".join(lines)
