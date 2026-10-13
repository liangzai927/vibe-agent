"""独立代码审查。

**为什么不能自己审自己**：刚写完一段代码的人，看的是「我打算怎么写」，
不是「实际写成了什么」。同一个上下文里，模型会顺着自己刚才的思路往下看，
跳过那些「我本来就是这么想的」的地方——而那些恰恰是 bug 藏身处。

所以这里起一个**干净上下文的子 agent**：它看不到作者的解释，
只拿得到 diff 和需求原文，被迫从零判断这段改动对不对。

**为什么把 diff 留给它自己取**：改动很大时，把 diff 塞进父上下文会挤掉
真正有用的东西。这里只给它一份概览（哪些文件、增删多少行）和 base ref，
让它自己用 git_diff 去拿细节。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .base import Tool, ToolError
from .context import AgentContext
from .delegate import spawn_subagent

REVIEWER_SYSTEM = """你是一个**代码审查者**。

你看不到作者的思路，只看得到改动本身——这是刻意的。这样你更能发现问题，
而不是被作者的解释带着走。

按重要性排序，审这些：

1. **正确性**：这段代码真的解决了需求吗？边界条件呢？空值、越界、并发、
   错误路径、异常吞掉。有测试覆盖吗，测试是不是只测了顺利路径？
2. **需求符合度**：有要求没做到的吗？有做到了但需求没提、可能多余的改动吗？
   有没有曲解需求的地方？
3. **回归风险**：改动有没有影响现有行为却没有相应验证？
4. **可维护性**：命名、重复、过度设计、把简单问题搞复杂。

**不要**审这些（除非真的很严重）：
- 个人风格偏好（空格、引号、行宽）
- 与本次改动无关的既有问题
- 「建议加个日志」「建议加注释」这类泛泛而谈
- 单纯的「可以更好」

每条问题必须给出：`文件:行号`、具体现象、为什么是问题、怎么改。
判断不了的地方明确说「无法判断」，不要猜。

输出格式：

## 结论
可以合并 / 有问题需修 / 有严重问题（三选一，加一句话理由）

## 必须修
（没有就写「无」）

## 建议改

## 看不出来的部分
（例如缺少需求原文、缺少测试、改动不完整等）"""


def _git(workspace_root: Path, *args: str, timeout: int = 60) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        return 1, "系统里找不到 git"
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    output = (result.stdout or "").rstrip("\n") or (result.stderr or "").rstrip("\n")
    return result.returncode, output


class ReviewChanges(Tool):
    name = "review_changes"
    description = (
        "让一个独立上下文的子 agent 审查你刚做的改动，专门挑问题。\n"
        "它看不到你的对话，只看 diff 和（可选的）需求原文——所以不会护着自己的思路。"
        "写完一个功能、准备提交之前用它过一遍。\n"
        "把需求原文一并给它，它才能判断「做对了没有」，而不只是「代码写得对不对」。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "requirement": {
                "type": "string",
                "description": "需求原文或摘要。给了它才能判断需求符合度，强烈建议带上",
            },
            "base": {
                "type": "string",
                "description": "跟哪个 commit/分支比，默认 HEAD（即尚未提交的改动）",
            },
            "focus": {
                "type": "string",
                "description": "额外关注点，例如「特别注意并发安全」（可选）",
            },
            "max_steps": {
                "type": "integer",
                "description": "最多让审查者走多少步，默认 18",
            },
        },
        "required": [],
    }
    requires_approval = False  # 只读，不改任何东西

    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    def run(
        self,
        requirement: str = "",
        base: str = "HEAD",
        focus: str = "",
        max_steps: int = 18,
        **_: Any,
    ) -> str:
        if not self.ctx.can_delegate():
            raise ToolError(
                f"委派深度已达上限（{self.ctx.max_depth} 层），没法再起审查者。"
            )

        root = self.ctx.workspace.root
        ref = (base or "HEAD").strip() or "HEAD"

        if not (root / ".git").exists():
            code, _ = _git(root, "rev-parse", "--is-inside-work-tree")
            if code != 0:
                raise ToolError(
                    f"{root} 不是 git 仓库，没法取差异。"
                    f"可以先 git_init，或者把要审的内容直接贴给 delegate。"
                )

        changed, untracked, stat_line = self._collect(root, ref)
        if not changed and not untracked:
            return f"没有可审查的改动——工作区相对 {ref} 没有任何变化。"

        overview = self._overview(changed, untracked, stat_line)
        task = self._task(ref, overview, requirement, focus)

        report, label = spawn_subagent(
            self.ctx,
            task=task,
            mode="read",
            system_prompt=REVIEWER_SYSTEM,
            max_steps=max_steps,
            label=f"审查 {len(changed) + len(untracked)} 个文件",
        )

        head = [f"审查范围：相对 {ref}", overview]
        if report.truncated:
            head.append("⚠ 审查者达到步数上限就停了，下面的结论可能不完整")
        head.append("")
        head.append(report.text.strip() or "（审查者没有给出结论）")
        return "\n".join(head)

    # ------------------------------------------------------------- 内部

    def _collect(self, root: Path, ref: str) -> tuple[list[str], list[str], str]:
        """拿到改动清单。未跟踪的文件也要算进来——新写的代码同样需要审。"""
        code, numstat = _git(root, "diff", "--numstat", ref)
        if code != 0 and "unknown revision" in numstat:
            raise ToolError(
                f"git 不认识这个 ref：{ref}。用 `git_log` 看一下有哪些提交。"
            )

        changed: list[str] = []
        added = removed = 0
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            plus, minus, name = parts[0], parts[1], parts[2]
            changed.append(name)
            added += int(plus) if plus.isdigit() else 0
            removed += int(minus) if minus.isdigit() else 0

        _, others = _git(root, "ls-files", "--others", "--exclude-standard")
        untracked = [line for line in others.splitlines() if line.strip()]

        stat_line = f"{len(changed)} 个已改动文件，+{added} −{removed} 行"
        if untracked:
            stat_line += f"；另有 {len(untracked)} 个未跟踪的新文件"
        return changed, untracked, stat_line

    @staticmethod
    def _overview(changed: list[str], untracked: list[str], stat_line: str) -> str:
        def brief(names: list[str], limit: int = 12) -> str:
            shown = "、".join(names[:limit])
            if len(names) > limit:
                shown += f" …（还有 {len(names) - limit} 个）"
            return shown

        lines = [f"  {stat_line}"]
        if changed:
            lines.append(f"  已改动：{brief(changed)}")
        if untracked:
            lines.append(f"  新文件：{brief(untracked)}")
        return "\n".join(lines)

    @staticmethod
    def _task(ref: str, overview: str, requirement: str, focus: str) -> str:
        parts = [
            f"请审查当前工作区相对 `{ref}` 的改动。",
            "",
            "先跑 `git diff` 拿到完整差异（新文件用 read_file 直接读），"
            "然后**读改动周围的代码**再下判断——只看 diff 容易误判，"
            "尤其是要判断「这样改对不对」的时候。",
            "",
            "【改动概览】",
            overview,
        ]

        text = (requirement or "").strip()
        if text:
            parts += ["", "【需求原文】", text]
        else:
            parts.append("")
            parts.append(
                "【需求原文】未提供——所以请只从代码本身判断正确性与风险，"
                "不要臆测需求应该是什么样；需求符合度这一项写「无法判断」。"
            )

        extra = (focus or "").strip()
        if extra:
            parts += ["", "【额外关注】", extra]

        return "\n".join(parts)


REVIEW_TOOLS = (ReviewChanges,)
