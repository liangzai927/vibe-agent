"""命令行入口：REPL 与一次性任务模式。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Config
from .llm import LLM
from .loop import Agent
from .prompts import build_system_prompt
from .tools import build_registry
from .ui import UI
from .util import bold, dim, red
from .workspace import Workspace, WorkspaceError

HELP = """可用命令：
  /help            显示这份帮助
  /tools           列出当前注册的工具
  /reset           清空对话历史（保留系统提示词）
  /compact         立即压缩上下文
  /init            扫描项目并生成 AGENTS.md 约定文件
  /cwd             显示工作区路径
  /exit            退出（Ctrl+D 同效）

其他任何输入都会作为任务交给 agent。
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="va",
        description="vibe-agent —— 一个从零构建的终端编码 agent",
    )
    parser.add_argument("task", nargs="*", help="一次性任务；省略则进入交互模式")
    parser.add_argument("-C", "--workspace", help="工作区目录（默认：当前目录）")
    parser.add_argument("-m", "--model", help="覆盖模型名")
    parser.add_argument("--base-url", help="覆盖 API base url")
    parser.add_argument("-y", "--yes", help="自动批准所有需要审批的操作（谨慎）", action="store_true")
    parser.add_argument("-q", "--quiet", help="不显示模型思考过程", action="store_true")
    parser.add_argument("--no-git", help="不注册 git 工具", action="store_true")
    parser.add_argument("--max-steps", type=int, help="单任务最大步数")
    parser.add_argument("--show-prompt", help="打印完整的系统提示词后退出（调试用）", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.load(args.workspace)

    if args.model:
        cfg.model = args.model
    if args.base_url:
        cfg.base_url = args.base_url
    if args.yes:
        cfg.auto_approve = True
    if args.quiet:
        cfg.show_thinking = False
    if args.max_steps:
        cfg.max_iterations = args.max_steps

    try:
        workspace = Workspace(cfg.workspace)
    except WorkspaceError as exc:
        print(red(f"工作区错误：{exc}"), file=sys.stderr)
        return 2

    if args.show_prompt:
        preview_registry = build_registry(workspace, include_git=not args.no_git)
        print(build_system_prompt(workspace, preview_registry.catalog()))
        return 0

    cfg.require_key()

    ui = UI(
        workspace,
        auto_approve=cfg.auto_approve,
        show_thinking=cfg.show_thinking,
    )
    registry = build_registry(workspace, include_git=not args.no_git)
    agent = Agent(cfg, ui, workspace=workspace, registry=registry, llm=LLM(cfg))

    # 一次性任务模式
    if args.task:
        task = " ".join(args.task).strip()
        answer = agent.run(task)
        ui.newline()
        if answer:
            print(answer)
        return 0

    # 交互模式
    ui.banner(cfg.model, cfg.base_url)
    _repl(agent, ui)
    return 0


# --------------------------------------------------------------------- REPL


def _repl(agent: Agent, ui: UI) -> None:
    while True:
        try:
            raw = ui.user_prompt()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not raw:
            continue

        if raw.startswith("/"):
            if _handle_command(raw, agent, ui):
                break
            continue

        try:
            answer = agent.run(raw)
        except KeyboardInterrupt:
            ui.newline()
            ui.warn("已中断")
            continue

        ui.newline()
        if answer:
            print(answer)
        print()


def _handle_command(raw: str, agent: Agent, ui: UI) -> bool:
    """返回 True 表示要退出 REPL。"""
    command, _, rest = raw.partition(" ")
    command = command.lower()

    if command in ("/exit", "/quit", "/q"):
        ui.info("再见 👋")
        return True

    if command == "/help":
        print(dim(HELP))
        return False

    if command == "/tools":
        for tool in agent.registry:
            flag = " [需审批]" if tool.requires_approval else ""
            print(f"  {bold(tool.name)}{flag}  — {tool.description.splitlines()[0]}")
        return False

    if command == "/reset":
        agent.reset()
        ui.success("对话历史已清空")
        return False

    if command == "/compact":
        before = len(agent.messages)
        agent._maybe_compact(force=True)
        ui.success(f"已压缩：{before} → {len(agent.messages)} 条消息")
        return False

    if command == "/cwd":
        ui.info(str(agent.workspace.root))
        return False

    if command == "/init":
        agent.run(
            "请扫描当前项目的结构（配置文件、依赖清单、目录布局、代码风格），"
            "然后生成一份 AGENTS.md 放在工作区根目录。内容要求："
            "1) 项目是什么、用什么技术栈；"
            "2) 常用命令（安装依赖、跑测试、构建、lint）；"
            "3) 代码风格与目录约定；"
            "4) 修改代码时的注意事项。"
            "只写你从文件里真实读到的信息，不要编造。保持简洁，控制在 80 行以内。"
        )
        return False

    ui.warn(f"未知命令：{command}。输入 /help 查看可用命令。")
    return False


def build_prompt_preview(cfg: Config, workspace: Workspace, include_git: bool = True) -> str:
    """调试用：打印实际会发送给模型的系统提示词。"""
    registry = build_registry(workspace, include_git=include_git)
    return build_system_prompt(workspace, registry.catalog())


if __name__ == "__main__":
    raise SystemExit(main())
