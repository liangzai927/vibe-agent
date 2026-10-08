"""命令行入口：REPL、一次性任务、会话恢复。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Config
from .diffview import colorize, make_diff
from .llm import LLM
from .loop import Agent
from .prompts import build_system_prompt
from .session import SessionStore, git_head
from .tools import build_registry
from .ui import UI
from .util import bold, dim, red, yellow
from .workspace import Workspace, WorkspaceError

HELP = """可用命令：
  /help            显示这份帮助
  /tools           列出当前注册的工具
  /diff            查看本次会话至今所有文件改动（对比会话开始前）
  /undo [N]        撤销最近 N 步的文件改动（默认 1 步）
  /checkpoints     列出可撤销的步骤
  /sessions        列出本项目的历史会话
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

    session = parser.add_argument_group("会话")
    session.add_argument("-c", "--continue", dest="continue_", action="store_true", help="继续本项目最近一次会话")
    session.add_argument("--resume", metavar="ID", help="恢复指定会话（可用 /sessions 查看 ID）")
    session.add_argument("--no-session", action="store_true", help="不保存本次会话")
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

    ui = UI(workspace, auto_approve=cfg.auto_approve, show_thinking=cfg.show_thinking)

    session, history, resume_note = _open_session(args, workspace, cfg, ui)

    registry = build_registry(workspace, include_git=not args.no_git)
    agent = Agent(
        cfg,
        ui,
        workspace=workspace,
        registry=registry,
        llm=LLM(cfg),
        session=session,
        history=history,
        extra_instructions=resume_note,
    )

    if resume_note:
        ui.warn(resume_note)
    if session is not None and history:
        ui.info(f"已恢复会话 {session.meta.id}（{len(history)} 条消息）")

    # 一次性任务模式
    if args.task:
        task = " ".join(args.task).strip()
        answer = agent.run(task)
        ui.newline()
        if answer:
            print(answer)
        return 0

    # 交互模式
    ui.banner(cfg.model, cfg.base_url, session_id=agent.session_id)
    _repl(agent, ui)
    return 0


# --------------------------------------------------------------------- 会话


def _open_session(
    args: argparse.Namespace,
    workspace: Workspace,
    cfg: Config,
    ui: UI,
) -> tuple[SessionStore | None, list[dict], str]:
    """决定本次是新建会话还是恢复，并给出「历史可能已过时」的提示。"""
    if args.no_session:
        return None, [], ""

    root = workspace.root

    if args.resume:
        try:
            store = SessionStore.open(root, args.resume)
        except FileNotFoundError as exc:
            print(red(f"{exc}"), file=sys.stderr)
            raise SystemExit(2)
        meta, history = store.load()
        return store, history, _staleness_note(meta, root)

    if args.continue_:
        store = SessionStore.latest(root)
        if store is None:
            ui.warn("没有找到历史会话，已新建一个。")
            _warn_sessions_not_ignored(workspace, ui)
            return SessionStore.create(root, cfg.model), [], ""
        meta, history = store.load()
        return store, history, _staleness_note(meta, root)

    _warn_sessions_not_ignored(workspace, ui)
    return SessionStore.create(root, cfg.model), [], ""


def _warn_sessions_not_ignored(workspace: Workspace, ui: UI) -> None:
    """会话写在 <workspace>/.va/ 下。如果这是 git 仓库又没忽略它，提醒一次。"""
    import subprocess

    root = workspace.root
    if not (root / ".git").exists():
        return
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", ".va"],
            cwd=str(root),
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if result.returncode != 0:
        ui.warn("会话文件保存在 .va/ 下，但它没有被 .gitignore 忽略。建议执行：echo '.va/' >> .gitignore")


def _staleness_note(meta, root: Path) -> str:
    """恢复会话时最重要的一件事：提醒模型历史结论可能已经作废。"""
    base = (
        "这是一个『恢复的会话』，不是新任务。上方历史里的文件内容和命令输出"
        "可能已经过时。动手之前请重新 read_file / grep 确认当前磁盘的实际状态，"
        "不要直接沿用历史里的旧结论。"
    )
    current = git_head(root)
    if meta.git_head and current and meta.git_head != current:
        return (
            base
            + f"\n另外：工作区在你离开后已被改动（git HEAD {meta.git_head} → {current}），"
            "请特别留意这一点。"
        )
    if meta.git_head and current is None:
        return base + "\n另外：原本记录的 git 提交已不存在，工作区可能被大幅改动。"
    return base


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
    ui.newline()

    if command in ("/exit", "/quit", "/q"):
        ui.info("再见 👋")
        return True

    if command == "/help":
        print(dim(HELP))
        return False

    if command == "/tools":
        for tool in agent.registry:
            flags = []
            if tool.requires_approval:
                flags.append("需审批")
            if tool.opaque:
                flags.append("副作用不可追踪")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            print(f"  {bold(tool.name)}{suffix}  — {tool.description.splitlines()[0]}")
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

    if command == "/sessions":
        _show_sessions(agent, ui)
        return False

    if command == "/checkpoints":
        _show_checkpoints(agent, ui)
        return False

    if command == "/diff":
        _show_session_diff(agent, ui)
        return False

    if command == "/undo":
        count = 1
        if rest.strip().isdigit():
            count = max(1, int(rest.strip()))
        print(agent.undo_last(count))
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


def _show_sessions(agent: Agent, ui: UI) -> None:
    metas = SessionStore.sessions(agent.workspace.root)
    if not metas:
        ui.info("本项目还没有历史会话。")
        return
    current = agent.session_id
    print(dim(f"  {'ID':<24} {'标题':<28} {'步数':>4}  时间"))
    for meta in metas:
        marker = "←" if meta.id == current else " "
        title = (meta.title or "(未命名)")[:26]
        print(f" {marker} {meta.id:<24} {title:<28} {meta.steps:>4}  {meta.age_text}")
    print(dim("  用 va --resume <ID> 恢复某个会话"))


def _show_checkpoints(agent: Agent, ui: UI) -> None:
    checkpoints = agent.undo.list()
    if not checkpoints:
        ui.info("还没有可撤销的步骤。")
        return
    for checkpoint in checkpoints:
        print("  " + agent.undo.describe(checkpoint))
    print(dim("  /undo [N] 回退最近 N 步"))


def _show_session_diff(agent: Agent, ui: UI) -> None:
    changes = agent.undo.cumulative_diff()
    if not changes:
        ui.info("本次会话还没有改动过文件。")
        return
    for path, diff_text in changes:
        print()
        print(bold(f"  {path}"))
        print("\n".join("  " + line for line in colorize(diff_text).splitlines()))
    print()
    ui.info(f"共 {len(changes)} 个文件有改动")


def build_prompt_preview(cfg: Config, workspace: Workspace, include_git: bool = True) -> str:
    """调试用：打印实际会发送给模型的系统提示词。"""
    registry = build_registry(workspace, include_git=include_git)
    return build_system_prompt(workspace, registry.catalog())


if __name__ == "__main__":
    raise SystemExit(main())
