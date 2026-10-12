"""让 agent 提议修改工作流定义。

**不是直接写文件，是提议。**

    提议 → 算出 diff → 你确认 → 落盘 + 记一笔「谁在什么时候因为什么改的」

**为什么要走这一步而不是直接 write_file**：

1. **改动要能 review**。配置文件是团队约定，改错了整个流程跑不动。
2. **要能归因**。半年后看到 `gate: warn`，你得知道「这是当初因为样式
   测试跑不起来才降的」——否则你会不敢动它，或者误以为它该是 strict。
3. **要能拒绝**。模型对「你们的约定该是什么」的判断不能自动生效。

**这个工具是阶段 I（自进化）的落地端**：I 负责从反馈和统计推导出该改什么，
这里负责把改动**安全地**落下去。
"""

from __future__ import annotations

from typing import Any

from ..util import truncate
from ..workflow import SCHEMA
from ..workflow_edit import Change, apply_changes, diff_text
from .base import ToolError
from .context import AgentContext, WorkspaceTool


def _known_paths() -> set[str]:
    return {f"{section}.{key}" for section, fields in SCHEMA.items() for key in fields}


def _render(value: Any) -> str:
    from ..workflow_edit import _render_value

    return _render_value(value)


class ProposeWorkflowChange(WorkspaceTool):
    name = "propose_workflow_change"
    description = (
        "提议修改工作流定义（分支规则、commit 规范、mock 约定、测试配置、"
        "提测配置、自主权等）。\n"
        "**不是直接写文件，是提议**——会算出 diff 给你确认，确认后才生效，"
        "并记下「谁在什么时候因为什么改的」。\n"
        "\n"
        "用在你发现「这条约定和实际不符」的时候，例如：\n"
        "  · 用户说「mock 别放 src/mocks，我们用 src/__mocks__」\n"
        "  · 观察到门禁反复被绕过（那说明这条门禁配错了）\n"
        "  · 观察到某一步反复被干预（那说明那一步的定义有问题）\n"
        "\n"
        "**不要**用它改和工作流无关的东西——那用 edit_file。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "changes": {
                "type": "array",
                "description": "要改的配置项",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "配置路径，段.键 的形式，例如 mock.dir",
                        },
                        "value": {
                            "description": "新值（字符串/数字/布尔/数组）",
                        },
                        "reason": {
                            "type": "string",
                            "description": "为什么改。**会记进历史**，写清楚依据",
                        },
                    },
                    "required": ["path", "value"],
                },
            },
            "summary": {
                "type": "string",
                "description": "一句话说明这批改动是为什么，会显示在确认框里",
            },
        },
        "required": ["changes"],
    }
    # 改的是团队约定，必须你点头
    requires_approval = True

    def __init__(self, ctx: AgentContext) -> None:
        # 注册时传的是 ctx（不是 workspace）——别的收 ctx 的工具都显式声明了，
        # 漏了的话 ctx 会被当成 workspace，然后 self.workspace.root 就炸了。
        super().__init__(ctx.workspace, ctx.cfg)
        self.ctx = ctx

    def _changes(self, args: dict[str, Any]) -> list[Change]:
        raw = args.get("changes") or []
        if not isinstance(raw, list) or not raw:
            raise ToolError("changes 不能是空的")

        known = _known_paths()
        out: list[Change] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ToolError("changes 里每一项都要是对象")
            path = str(item.get("path") or "").strip()
            if not path:
                raise ToolError("每一项都要有 path")
            if path not in known:
                near = sorted(p for p in known if path.split(".")[-1] in p)
                hint = f"你是不是想改：{'、'.join(near[:3])}" if near else ""
                raise ToolError(
                    f"没有 {path!r} 这个配置项。\n"
                    f"可用的：{'、'.join(sorted(known))}\n{hint}"
                )
            out.append(
                Change(path=path, value=item.get("value"), reason=str(item.get("reason") or ""))
            )
        return out

    def preview(self, args: dict[str, Any]) -> str | None:
        """审批框里显示的就是这个 **diff**，不是原始参数。

        批准配置文件改动之前，你要看的是「哪一行变成什么」，
        而不是 `{"changes": [...]}` 这坨 JSON。
        """
        try:
            changes = self._changes(args)
        except ToolError as exc:
            return f"参数有问题：{exc}"

        before = self._current_text()
        try:
            after, _ = self._preview_text(before, changes)
        except Exception as exc:  # noqa: BLE001
            return f"算不出 diff：{exc}"

        lines = []
        if args.get("summary"):
            lines.append(f"为什么：{args['summary']}")
            lines.append("")
        lines.append(f".vibe/workflow.yaml")
        lines.append(diff_text(before, after))
        return "\n".join(lines)

    def _current_text(self) -> str:
        path = self.workspace.root / ".vibe" / "workflow.yaml"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def _preview_text(self, before: str, changes: list[Change]) -> tuple[str, list[str]]:
        from ..workflow_edit import set_values

        return set_values(before, changes)

    def run(self, changes: list[dict[str, Any]] | None = None, summary: str = "", **_: Any) -> str:
        items = self._changes({"changes": changes, "summary": summary})

        # 值和类型对不对——**落盘之前先拦**，别改出一个坏配置
        problems = self._validate(items)
        if problems:
            raise ToolError("改动有问题，没有落盘：\n" + "\n".join(f"  ✗ {p}" for p in problems))

        config, notes = apply_changes(
            self.workspace.root, items, source=summary or "agent 提议"
        )
        lines = [f"已修改 {config}"]
        lines.extend(f"  · {note}" for note in notes)
        lines.append("")
        lines.append("改动记在 .vibe/workflow-history.json（谁、什么时候、为什么）")
        lines.append("**下次会话生效**——当前会话用的还是改之前那份。")
        return truncate("\n".join(lines), limit=4000)

    def _validate(self, changes: list[Change]) -> list[str]:
        """按 SCHEMA 校验值和类型。

        复用阶段 A 的校验规则——两处各写一套迟早对不上。
        """
        from ..workflow import _cross_checks, _type_ok

        problems: list[str] = []
        for change in changes:
            section, _, key = change.path.partition(".")
            spec = SCHEMA.get(section, {}).get(key)
            if spec is None:
                continue
            if not _type_ok(change.value, spec.kind):
                problems.append(
                    f"{change.path}：应该是 {spec.kind.__name__}，"
                    f"收到的是 {type(change.value).__name__}"
                )
                continue
            if spec.check is not None:
                message = spec.check(change.value)
                if message:
                    problems.append(f"{change.path}：{message}")

        # 跨字段的检查（比如「门禁开着却没配测试」）也要过一遍
        candidate: dict[str, Any] = {}
        for change in changes:
            section, _, key = change.path.partition(".")
            candidate.setdefault(section, {})[key] = change.value
        problems.extend(
            f"{p.path}：{p.message}"
            for p in _cross_checks(candidate, "本次改动")
            if p.level == "error"
        )
        return problems


WORKFLOW_TOOLS = (ProposeWorkflowChange,)
