"""计划相关工具。"""

from __future__ import annotations

from typing import Any

from ..plan import MAX_STEPS, PlanBoard, VALID_STATUSES
from .base import Tool, ToolError


class UpdatePlan(Tool):
    name = "update_plan"
    description = (
        "创建或更新任务计划。多步任务（大致超过 3 个动作）开工前先拆成步骤，"
        "之后每完成一步就更新状态。\n"
        "这个工具是**整体替换**：每次都要传完整的步骤列表，不是增量修改。\n"
        "一次只让一个步骤处于 in_progress；卡住了就把状态改成 blocked 并写清原因。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "这次任务要达成什么。第一次调用必须给；之后可以省略沿用，也可以修正",
            },
            "steps": {
                "type": "array",
                "description": f"完整步骤列表，顺序就是执行顺序（最多 {MAX_STEPS} 条）",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "一步做什么，具体到可执行"},
                        "status": {
                            "type": "string",
                            "enum": list(VALID_STATUSES),
                            "description": "pending 待办 / in_progress 进行中 / done 完成 / blocked 卡住 / skipped 跳过",
                        },
                        "note": {
                            "type": "string",
                            "description": "补充说明。blocked 时写清卡在哪，这比状态本身更有用",
                        },
                    },
                    "required": ["title"],
                },
            },
        },
        "required": ["steps"],
    }
    requires_approval = False  # 只是改内存里的计划，不碰文件

    def __init__(self, board: PlanBoard) -> None:
        self.board = board

    def run(self, steps: Any, goal: str = "", **_: Any) -> str:
        if steps is None:
            raise ToolError("steps 不能为空。第一次调用请给出拆好的步骤列表。")

        plan, warnings = self.board.update(goal, steps)

        parts = [plan.render()]
        if warnings:
            parts.append("")
            parts.append("需要修正的地方：")
            parts.extend(f"  · {w}" for w in warnings)
        if plan.is_finished:
            parts.append("")
            parts.append("计划已全部完成——如果任务目标也达成了，就可以收尾了。")
        return "\n".join(parts)

    def after_run(self, ui: Any, result: str) -> None:
        """计划值得完整打印一次；通用展示会把多行内容截断成 4 行。"""
        if self.board.plan is not None:
            ui.show_plan(self.board.plan)


PLAN_TOOLS = (UpdatePlan,)
