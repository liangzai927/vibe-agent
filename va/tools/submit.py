"""提测工具。

**每一步都是可选的**：没配就跳过并说明「跳过了」，不是报错，也不执行。
一个刚接入的项目不该因为跑了一次就被推到 test 分支去。
"""

from __future__ import annotations

from typing import Any

from ..submit import Submitter
from .base import ToolError
from .context import AgentContext, WorkspaceTool


class SubmitForTest(WorkspaceTool):
    name = "submit_for_test"
    description = (
        "提测：按 workflow.yaml 的 submit 段执行推送、CI 校验、通知。\n"
        "**全部没配就什么都不做，直接返回**（不报错）——每步的结果都会列出来，\n"
        "包括「跳过了，为什么」。\n"
        "推送前会过测试门禁（可用 submit.require_gate 关掉）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "任务号/标题，用在通知里"},
            "commit": {"type": "string", "description": "commit 号，用在通知里（可选）"},
        },
        "required": [],
    }
    requires_approval = True   # 往外部推东西，推错了收不回来

    def __init__(self, ctx: AgentContext) -> None:
        super().__init__(ctx.workspace, ctx.cfg)
        self.ctx = ctx

    def run(self, task: str = "", commit: str = "", **_: Any) -> str:
        submitter = Submitter(
            self.workspace.root,
            workflow=getattr(self.ctx, "workflow", None),
            gate=getattr(self.ctx, "ledger", None),
        )
        result = submitter.run(task=task, commit=commit)
        text = result.report()
        if not result.ok:
            # 失败了要抛错——让调用方知道没成，而不是「成功返回一段写着失败的文本」
            raise ToolError(text)
        return text


SUBMIT_TOOLS = (SubmitForTest,)
