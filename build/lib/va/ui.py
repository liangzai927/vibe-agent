"""终端交互层：流式输出、工具调用展示、审批询问。

刻意不依赖 rich —— 少一个依赖，装起来更省事。
"""

from __future__ import annotations

import sys
from typing import Any

from .util import blue, bold, cyan, dim, green, preview, red, yellow
from .workspace import Workspace


class UI:
    def __init__(self, workspace: Workspace, *, auto_approve: bool = False, show_thinking: bool = True) -> None:
        self.workspace = workspace
        self.auto_approve = auto_approve
        self.show_thinking = show_thinking
        self._streaming = False
        self._in_thinking = False
        self._always_allow: set[str] = set()

    # ------------------------------------------------------------------ 基础

    def banner(self, model: str, base_url: str, session_id: str | None = None) -> None:
        print()
        print(bold("  vibe-agent") + dim(f"  v0.1  ·  {model}"))
        print(dim(f"  工作区  {self.workspace.root}"))
        print(dim(f"  接口    {base_url}"))
        if session_id:
            print(dim(f"  会话    {session_id}"))
        print(dim("  输入任务开始；/help 查看命令；/exit 退出"))
        print()

    def info(self, text: str) -> None:
        self._close_stream()
        print(dim(f"  {text}"))

    def warn(self, text: str) -> None:
        self._close_stream()
        print(yellow(f"  ⚠ {text}"))

    def error(self, text: str) -> None:
        self._close_stream()
        print(red(f"  ✗ {text}"))

    def success(self, text: str) -> None:
        self._close_stream()
        print(green(f"  ✓ {text}"))

    # ------------------------------------------------------------------ 流式

    def assistant_header(self, step: int) -> None:
        """一轮回复开始。

        刻意**不显示步号**：一个正常任务跑几十轮是常态，把轮次标出来只会
        让人以为「怎么又要一步」，而它其实只是模型在陆续调工具。
        留一个空行做视觉分隔就够了。
        """
        self._close_stream()
        print()

    def stream_text(self, piece: str) -> None:
        self._close_stream_thinking()
        if not self._streaming:
            print("  ", end="", flush=True)
            self._streaming = True
        # 保持缩进，让多行输出好看
        print(piece.replace("\n", "\n  "), end="", flush=True)

    def stream_thinking(self, piece: str) -> None:
        if not self.show_thinking:
            return
        if not self._in_thinking:
            self._close_stream()
            print(dim("  ┌ 思考 "), end="", flush=True)
            self._in_thinking = True
        print(dim(piece.replace("\n", " ")), end="", flush=True)

    def _close_stream(self) -> None:
        if self._streaming:
            print()
            self._streaming = False

    def _close_stream_thinking(self) -> None:
        if self._in_thinking:
            print()
            self._in_thinking = False

    def newline(self) -> None:
        self._close_stream()
        self._close_stream_thinking()

    # ------------------------------------------------------------------ 工具

    def tool_call(self, index: int, total: int, name: str, summary: str) -> None:
        self.newline()
        prefix = f"  ├─ [{index}/{total}] " if total > 1 else "  ├─ "
        print(cyan(prefix + name) + dim(f"({summary})"))

    def tool_result(self, text: str, ok: bool) -> None:
        flat_lines = text.strip().splitlines() or ["(空)"]
        marker = green("  │  ✓ ") if ok else red("  │  ✗ ")
        first = preview(flat_lines[0], 100)
        print(marker + dim(first))
        for line in flat_lines[1:4]:
            print(dim("  │    " + preview(line, 100)))
        if len(flat_lines) > 4:
            print(dim(f"  │    … 还有 {len(flat_lines) - 4} 行"))

    def show_plan(self, plan: Any) -> None:
        """完整打印任务计划。

        通用的工具结果展示会把多行内容截断到 4 行，但计划是模型和用户都要
        反复回看的东西，值得占屏。
        """
        self.newline()
        lines = plan.render().splitlines()
        if not lines:
            return
        print("  " + bold(lines[0]))
        for line in lines[1:]:
            if line.strip().startswith("▸"):
                print("  " + cyan(line))
            elif line.strip().startswith("☑"):
                print("  " + green(line))
            elif line.strip().startswith("✗"):
                print("  " + red(line))
            else:
                print("  " + line)

    def block(self, title: str, rows: list[tuple[str, str]]) -> None:
        """打印一个带标题的信息块。

        给上层流程（比如 feature 流水线）用的。所有输出都走 UI 层是刻意的——
        哪一层自己 print，将来换界面时就会漏掉哪一层。
        """
        self.newline()
        print(bold(f"  {title}"))
        print(dim("  " + "─" * 58))
        width = max((len(key) for key, _ in rows), default=0)
        for key, value in rows:
            print(f"  {key:<{width}}  {value}")
        print()

    def horizontal_rule(self, label: str = "") -> None:
        self.newline()
        if label:
            print(bold(f"  ── {label} ──"))
        else:
            print(dim("  " + "─" * 58))
        print()

    def answer(self, text: str) -> None:
        """agent 的最终回答，原样打印，不加任何修饰。"""
        self.newline()
        print(text)

    # ------------------------------------------------------------------ 审批

    def confirm(
        self,
        tool_name: str,
        args: dict[str, Any],
        detail: str,
        preview: str | None = None,
    ) -> bool:
        self.newline()
        if self.auto_approve or tool_name in self._always_allow:
            print(yellow(f"  ⚡ 自动批准 {tool_name}") + dim(f"  {detail}"))
            if preview:
                # 自动批准模式下只显示摘要行，避免刷屏
                print(dim("  │  " + preview.splitlines()[0]))
            return True
        if not sys.stdin.isatty():
            print(red(f"  ✗ 非交互环境，默认拒绝 {tool_name}"))
            return False

        print(yellow(f"  ⚠ 需要确认：{tool_name}"))
        if preview:
            self.show_diff(preview)
        else:
            for key, value in args.items():
                text = value if isinstance(value, str) else str(value)
                if len(text) > 300:
                    text = text[:300] + "…"
                print(dim(f"      {key} = {text}"))
        try:
            answer = input(yellow("  执行吗？[y]是 / [a]本次会话总是 / [n]否 > ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if answer in ("a", "always"):
            self._always_allow.add(tool_name)
            return True
        return answer in ("y", "yes", "")

    def show_diff(self, diff_text: str) -> None:
        """带左侧竖线打印一段（可能已着色的）diff。"""
        for line in diff_text.splitlines():
            print("  │ " + line)

    # ------------------------------------------------------------------ 其他

    def user_prompt(self) -> str:
        try:
            return input(bold("\n› ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return "/exit"
