"""诊断工具：让 agent 看见「哪里不对」。

**这是「自进化」的输入端。**

它能做的只有一件事：**发现「有东西在反复出问题」，并指出它在哪。**
「正确的是什么」只有你知道——agent 负责把你的判断固化成配置，
不是自己发明流程。

所以这个工具的产出**故意不带「该怎么改」**：那要结合你的意图。
它做完诊断之后，应该来问你，而不是自作主张。
"""

from __future__ import annotations

from typing import Any

from ..diagnose import diagnose
from ..task import TaskStore
from .base import ToolError
from .context import AgentContext, WorkspaceTool


class DiagnoseWorkflow(WorkspaceTool):
    name = "diagnose_workflow"
    description = (
        "看看工作流本身有没有问题——**这是「流程进化」的输入端**。\n"
        "会检查：\n"
        "  · 哪个步骤最常被干预（那一步的定义可能不对）\n"
        "  · 最近有没有严重事故（绕过门禁、推错分支）\n"
        "  · 配置和现实是否脱节（mock 目录不存在、base 分支不存在、\n"
        "    测试脚本不在 package.json 里）\n"
        "\n"
        "**它只报告「哪里不对」，不说「该怎么改」**——改法要结合用户的意图。\n"
        "拿到报告后应该**去问用户**，而不是自己改配置。\n"
        "\n"
        "用在：用户说「流程哪里不对」、你要做周期复盘、"
        "或者察觉到某一步反复出问题的时候。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "只看某个任务（可选）。不给就看全部",
            },
        },
        "required": [],
    }

    def __init__(self, ctx: AgentContext) -> None:
        super().__init__(ctx.workspace, ctx.cfg)
        self.ctx = ctx

    def run(self, task_id: str = "", **_: Any) -> str:
        store = TaskStore(self.workspace.root)
        if task_id:
            task = store.load(task_id)
            if task is None:
                raise ToolError(f"没有 {task_id} 这个任务")
            tasks = [task]
        else:
            tasks = store.all()

        result = diagnose(
            self.workspace.root,
            workflow=getattr(self.ctx, "workflow", None),
            tasks=tasks,
        )
        return result.report()


DIAGNOSE_TOOLS = (DiagnoseWorkflow,)
