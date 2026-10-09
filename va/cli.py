"""命令行入口：REPL、一次性任务、会话恢复。"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from . import __version__
from .config import Config
from .diffview import colorize, make_diff
from .feature import FeatureRequest, describe_flags
from .feature import run as run_feature
from .llm import LLM
from .loop import Agent
from .plan import Plan
from .prompts import build_system_prompt
from .session import SessionStore, git_head
from .tools import build_registry
from .ui import UI
from .util import bold, cyan, dim, green, red, yellow
from .workspace import Workspace, WorkspaceError

HELP = """可用命令：
  /help            显示这份帮助
  /tools           列出当前注册的工具
  /diff            查看本次会话至今所有文件改动（对比会话开始前）
  /feature <需求>  跑一遍功能开发流水线（建分支→规划→编码→自测→自审→提交）
  /paste [名字]     把剪贴板里的图片存成文件，并给出路径
  /plan            查看当前任务计划
  /model           模型选择器（↑↓ 移动，回车确定）
  /models          列出所有模型（纯文本，不进选择器）
  /undo [N]        撤销最近 N 步的文件改动（默认 1 步）
  /checkpoints     列出可撤销的步骤
  /sessions        列出本项目的历史会话
  /reset           清空对话历史（保留系统提示词）
  /compact         立即压缩上下文
  /init            扫描项目并生成 AGENTS.md 约定文件
  /cwd             显示工作区路径
  /exit            退出（Ctrl+D 同效）

其他任何输入都会作为任务交给 agent。

想把图片交给它：先在别处复制图片，然后在消息里写 @clip —— 它会自动替换成
图片路径。终端本身传不了图片数据，所以用这个标记来「粘贴」。

子命令：
  python3 run.py web                                        浏览器界面（可视化，支持拖拽传图）
  python3 run.py setup                                      首次配置（也可以直接启动，没配会自己引导）
  python3 run.py doctor [--offline] [--quick] [--json]     环境自检
  python3 run.py feature "需求描述" [--doc X --design Y]    功能开发流水线
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
    parser.add_argument("--no-render", help="不注册渲染/截图工具", action="store_true")
    parser.add_argument("--max-steps", type=int, help="单任务最大步数")
    parser.add_argument("--show-prompt", help="打印完整的系统提示词后退出（调试用）", action="store_true")
    parser.add_argument("-V", "--version", action="version", version=f"vibe-agent {__version__}")

    session = parser.add_argument_group("会话")
    session.add_argument("-c", "--continue", dest="continue_", action="store_true", help="继续本项目最近一次会话")
    session.add_argument("--resume", metavar="ID", help="恢复指定会话（可用 /sessions 查看 ID）")
    session.add_argument("--no-session", action="store_true", help="不保存本次会话")
    return parser


# 主解析器里「带值」的选项。扫描 doctor 子命令时要跳过它们的值，
# 否则 `-C /path doctor` 里的 /path 会被误当成第一个位置参数。
_VALUE_OPTIONS = frozenset(
    {"-C", "--workspace", "-m", "--model", "--base-url", "--resume", "--max-steps"}
)


def split_subcommand(raw: list[str], name: str) -> tuple[list[str], list[str]] | None:
    """在参数里找 `name` 子命令。

    返回 (子命令之前需要转交的全局参数, 子命令之后的参数)；没有则返回 None。

    这里不能只看 ``raw[0]``：容器里 entrypoint 会补上 ``-C /workspace``，
    于是子命令跑到了第二个位置。
    """
    index = 0
    passthrough: list[str] = []
    while index < len(raw):
        token = raw[index]

        if token == "--":
            index += 1
            break
        if token in _VALUE_OPTIONS:
            if token in ("-C", "--workspace") and index + 1 < len(raw):
                passthrough += [token, raw[index + 1]]
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue

        # 第一个位置参数：是目标子命令就走它，否则当成普通任务
        if token == name:
            return passthrough, raw[index + 1 :]
        return None

    return None


def split_doctor_args(raw: list[str]) -> tuple[list[str], list[str]] | None:
    """保留旧名字，内部走通用实现。"""
    return split_subcommand(raw, "doctor")


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)

    # 子命令不是交给 agent 的任务 —— 提前拦截
    for name, handler in (
        ("doctor", _doctor),
        ("feature", _feature),
        ("setup", _setup),
        ("web", _web),
    ):
        split = split_subcommand(raw, name)
        if split is None:
            continue
        head, tail = split
        return handler(head + tail)

    args = build_parser().parse_args(raw)
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
        workspace = Workspace(cfg.workspace, extra_read_roots=cfg.read_dirs)
    except WorkspaceError as exc:
        print(red(f"工作区错误：{exc}"), file=sys.stderr)
        return 2

    if args.show_prompt:
        preview_registry = build_registry(
            workspace, include_git=not args.no_git, include_render=not args.no_render, cfg=cfg
        )
        print(build_system_prompt(workspace, preview_registry.catalog()))
        return 0

    # 第一次用：还没配 key 又是在交互式终端里，直接把向导跑起来。
    # 报一句「缺少 API key」然后退出，对第一次上手的人毫无帮助。
    if not cfg.api_key and not args.task and sys.stdin.isatty():
        from .setup import run as run_setup

        result = run_setup(workspace.root / ".env", directory=Path(__file__).resolve().parent.parent)
        if not result.ok:
            print(red(f"配置未完成：{result.error}"), file=sys.stderr)
            return 2
        cfg = Config.load(args.workspace)

    cfg.require_key()

    ui = UI(workspace, auto_approve=cfg.auto_approve, show_thinking=cfg.show_thinking)

    session, history, resume_note, saved_plan = _open_session(args, workspace, cfg, ui)

    registry = build_registry(
        workspace, include_git=not args.no_git, include_render=not args.no_render, cfg=cfg
    )
    agent = Agent(
        cfg,
        ui,
        workspace=workspace,
        registry=registry,
        llm=LLM(cfg),
        session=session,
        history=history,
        extra_instructions=resume_note,
        plan=Plan.from_dict(saved_plan) if saved_plan else None,
    )

    if resume_note:
        ui.warn(resume_note)
    if session is not None and history:
        ui.info(f"已恢复会话 {session.meta.id}（{len(history)} 条消息）")
    if agent.plan is not None:
        ui.info(f"已恢复任务计划：{agent.plan.goal}（{agent.plan.done_count}/{agent.plan.total}）")

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
) -> tuple[SessionStore | None, list[dict], str, dict | None]:
    """决定本次是新建会话还是恢复。

    返回 (会话存储, 历史消息, 可能过期的提示, 已保存的计划)。
    """
    if args.no_session:
        return None, [], "", None

    root = workspace.root

    if args.resume:
        try:
            store = SessionStore.open(root, args.resume)
        except FileNotFoundError as exc:
            print(red(f"{exc}"), file=sys.stderr)
            raise SystemExit(2)
        meta, history, plan = store.load()
        return store, history, _staleness_note(meta, root), plan

    if args.continue_:
        store = SessionStore.latest(root)
        if store is None:
            ui.warn("没有找到历史会话，已新建一个。")
            _warn_sessions_not_ignored(workspace, ui)
            return SessionStore.create(root, cfg.model), [], "", None
        meta, history, plan = store.load()
        return store, history, _staleness_note(meta, root), plan

    _warn_sessions_not_ignored(workspace, ui)
    return SessionStore.create(root, cfg.model), [], "", None


def _warn_sessions_not_ignored(workspace: Workspace, ui: UI) -> None:
    """确保 <workspace>/.va/ 被 git 忽略。

    图片、设计稿规格、截图现在都放在 agent 自己的目录下，不再进项目。
    但会话记录还留在 ``.va/sessions``——它很小、纯文本，而且「这个项目的
    对话历史」放在项目旁边更顺手。

    只是提醒不管用：用户会忘。既然我们知道自己建了这个目录，就顺手把
    .gitignore 加好，并明确告诉用户改了什么。
    """
    import subprocess

    root = workspace.root
    if not (root / ".git").exists():
        return
    try:
        if subprocess.run(
            ["git", "check-ignore", "-q", ".va"],
            cwd=str(root), capture_output=True, timeout=10,
        ).returncode == 0:
            return  # 已经忽略了
    except (OSError, subprocess.SubprocessError):
        return

    ignore_file = root / ".gitignore"
    try:
        existing = ignore_file.read_text(encoding="utf-8") if ignore_file.is_file() else ""
        if ".va/" in existing or ".va\n" in existing:
            return
        addition = ("\n" if existing and not existing.endswith("\n") else "") + (
            "\n# vibe-agent 的会话记录\n.va/\n"
        )
        ignore_file.write_text(existing + addition, encoding="utf-8")
    except OSError as exc:
        ui.warn(f"没能把 .va/ 加进 .gitignore（{exc}）。手动加一下：echo '.va/' >> .gitignore")
        return

    ui.info("已把 .va/ 加进 .gitignore（里面是会话记录）")


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

        task = _expand_clip_tokens(raw, agent, ui)
        if task is None:
            continue

        try:
            answer = agent.run(task)
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

    if command in ("/paste", "/clip", "/图片"):
        _paste_clipboard(agent, ui, rest)
        return False

    if command == "/models":
        _show_models(agent, ui)
        return False

    if command == "/model":
        _switch_model(agent, ui, rest.strip())
        return False

    if command == "/plan":
        plan = agent.plan
        if plan is None:
            ui.info("当前没有计划。多步任务里 agent 会自己用 update_plan 建。")
        else:
            ui.newline()
            print(plan.render())
        return False

    if command == "/diff":
        _show_session_diff(agent, ui)
        return False

    if command == "/feature":
        if not rest.strip():
            ui.warn("用法：/feature <需求描述>")
            ui.info("更完整的用法用命令行：va feature --help")
            return False
        req = FeatureRequest(requirement=rest.strip())
        run_feature(agent, req, ui)
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


# --------------------------------------------------------------------- doctor


def _doctor(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="va doctor",
        description="检查运行环境：缺什么、怎么装，一次说清楚",
    )
    parser.add_argument("-C", "--workspace", help="工作区目录（默认：当前目录）")
    parser.add_argument("--offline", action="store_true", help="跳过模型连通性检查")
    parser.add_argument("--quick", action="store_true", help="跳过浏览器实际启动探测（快很多）")
    parser.add_argument("--json", dest="as_json", action="store_true", help="输出 JSON，便于 CI 消费")
    args = parser.parse_args(argv)

    cfg = Config.load(args.workspace)

    root = cfg.workspace
    if not root.exists():
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    try:
        workspace = Workspace(root, extra_read_roots=cfg.read_dirs)
    except WorkspaceError as exc:
        print(red(f"工作区问题：{exc}"), file=sys.stderr)
        return 1

    from .doctor import run as run_doctor

    return run_doctor(
        cfg,
        workspace,
        offline=args.offline,
        quick=args.quick,
        as_json=args.as_json,
    )


# --------------------------------------------------------------------- web


def _web(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="va web",
        description="在浏览器里用 agent：拖拽传图、彩色 diff、点按钮审批",
    )
    parser.add_argument("-C", "--workspace", help="工作区目录")
    parser.add_argument("--port", type=int, default=8420, help="监听端口（被占用会自动换）")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="监听地址。默认只允许本机——这东西能执行任意命令，别暴露到局域网",
    )
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument(
        "--window", action="store_true", help="用原生窗口打开（需要 pip install pywebview）"
    )
    parser.add_argument("-y", "--yes", action="store_true", help="自动批准所有操作")
    parser.add_argument("--model", help="本次使用的模型")
    parser.add_argument("--base-url", help="本次使用的服务地址")
    parser.add_argument("--resume", metavar="会话ID", help="恢复指定会话")
    parser.add_argument("--continue", dest="continue_", action="store_true", help="接着最近的会话")
    parser.add_argument("--no-session", action="store_true", help="不记录会话")
    parser.add_argument("--no-git", action="store_true", help="不注册 git 工具")
    parser.add_argument("--no-render", action="store_true", help="不注册浏览器渲染工具")
    parser.add_argument("--no-tools", action="store_true", help="不注册任何工具（只聊天）")
    parser.add_argument("-q", "--quiet", action="store_true", help="不显示思考过程")
    parser.add_argument("--max-steps", type=int, help="单任务最大步数")
    args = parser.parse_args(argv)

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
        workspace = Workspace(cfg.workspace, extra_read_roots=cfg.read_dirs)
    except WorkspaceError as exc:
        print(red(f"工作区错误：{exc}"), file=sys.stderr)
        return 2

    if not cfg.api_key:
        print(yellow("还没有配置模型服务。先跑一遍向导："), file=sys.stderr)
        print(dim("  python3 run.py setup"), file=sys.stderr)
        return 2

    from .web import WebApp, serve

    app = WebApp(cfg, workspace, auto_approve=cfg.auto_approve)

    # 会话：和终端模式走同一套恢复逻辑，别搞两套
    session, history, resume_note, saved_plan = _open_session(args, workspace, cfg, app.ui)
    if history:
        app.agent.messages = history
    if saved_plan:
        from .plan import Plan

        app.agent.plan_board.plan = Plan.from_dict(saved_plan)
    app.agent.session = session

    app.ui.banner(cfg.model, cfg.base_url, session_id=app.agent.session_id)
    if resume_note:
        app.ui.warn(resume_note)
    if history:
        app.ui.info(f"已恢复会话（{len(history)} 条消息）")

    print()
    print(bold("  vibe-agent") + dim("  浏览器界面"))
    print(dim(f"  工作区  {workspace.root}"))
    print(dim(f"  模型    {cfg.model}"))
    if cfg.auto_approve:
        print(yellow("  ⚡ 自动批准所有操作"))
    print(dim("  按 Ctrl-C 停止"))
    print()

    return serve(
        app,
        port=args.port,
        host=args.host,
        open_browser=not args.no_browser,
        window=args.window,
    )


# --------------------------------------------------------------------- setup


def _setup(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="va setup",
        description="配置模型服务：填 key 和地址，把可用模型拉下来存着",
    )
    parser.add_argument("-C", "--workspace", help="工作区目录（用于确定 .env 放哪）")
    parser.add_argument("--api-key", help="直接给 key（默认交互录入）")
    parser.add_argument("--base-url", help="服务地址")
    parser.add_argument("--model", help="默认模型（不给就取拉到的第一个）")
    parser.add_argument("--vision-model", help="多模态模型（可选）")
    parser.add_argument("--no-verify", action="store_true", help="跳过连通性验证")
    parser.add_argument("--force", action="store_true", help="已有配置时也重跑")
    args = parser.parse_args(argv)

    from .setup import run as run_setup

    directory = Path(__file__).resolve().parent.parent
    env_path = directory / ".env"

    if env_path.is_file() and not args.force:
        print(dim(f"  {env_path} 已存在。要重新配置加 --force；"))
        print(dim("  只想换模型的话，进入 agent 后用 /model 就行。"))
        return 0

    result = run_setup(
        env_path,
        directory=directory,
        api_key=args.api_key or "",
        base_url=args.base_url or "",
        model=args.model or "",
        vision_model=args.vision_model or "",
        verify=not args.no_verify,
    )
    if not result.ok:
        print(red(f"配置失败：{result.error}"), file=sys.stderr)
        return 2
    if not result.verified:
        print(dim("  配置已写入，但连通性没验证通过。改好后跑一次 doctor 确认。"))
    return 0


# --------------------------------------------------------------------- 剪贴板


# @clip / @paste / @剪贴板 都会被替换成图片路径。
# 为什么要用标记而不是拦截 Ctrl+V：终端传的是按键不是图片数据，
# 大多数终端在粘贴图片时干脆什么都不发——没有可拦截的东西。
CLIP_TOKEN = re.compile(r"@(?:clip|paste|image|img|图片|剪贴板)(?![\w.\-])", re.IGNORECASE)


def _grab_clipboard(agent: Agent) -> "object":
    from .clipboard import default_filename, inbox_dir, read_image

    target = inbox_dir(agent.workspace.root) / default_filename()
    return read_image(target)


def _relative_to_workspace(agent: Agent, path) -> str:
    try:
        return agent.workspace.relative(path)
    except Exception:  # noqa: BLE001
        return str(path)


def _paste_clipboard(agent: Agent, ui: UI, name: str = "") -> None:
    """把剪贴板里的图片存进工作区，并打印它的路径。"""
    from .clipboard import default_filename, inbox_dir, read_image, support

    ok, how = support()
    if not ok:
        ui.error(f"当前平台不支持读剪贴板图片：{how}")
        return

    filename = name.strip() or default_filename()
    if not filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        filename += ".png"

    target = inbox_dir(agent.workspace.root) / filename
    result = read_image(target)

    if not result.ok:
        ui.error(result.describe())
        return

    rel = _relative_to_workspace(agent, result.path)
    dims = f"（{result.size[0]}x{result.size[1]}）" if result.size else ""
    ui.success(f"已存为 {rel}{dims}")
    ui.info("用法：在消息里直接写 @clip，它会自动替换成这个路径。例如：")
    ui.info(f"  › 用 inspect_image 看一下 {rel}，然后照着它实现")


def _expand_clip_tokens(text: str, agent: Agent, ui: UI) -> str | None:
    """把消息里的 @clip 换成剪贴板图片路径。取不到就返回 None（放弃这条消息）。"""
    if not CLIP_TOKEN.search(text):
        return text

    result = _grab_clipboard(agent)
    if not result.ok:
        ui.error(result.describe())
        ui.info("（这条消息没有发出去。也可以用 /paste 单独存图。）")
        return None

    rel = _relative_to_workspace(agent, result.path)
    ui.success(f"已把剪贴板图片存为 {rel}")
    return CLIP_TOKEN.sub(rel, text)


# --------------------------------------------------------------------- 模型切换


def _show_models(agent: Agent, ui: UI) -> None:
    from .models import load_catalog

    catalog = load_catalog(Path(__file__).resolve().parent.parent)
    if catalog is None or not catalog.models:
        ui.info("还没有模型清单。")
        ui.info("进入 agent 前跑一次 `python3 run.py setup` 就能拉下来。")
        return

    current = agent.cfg.model
    vision = agent.cfg.vision_model
    print(dim(f"  服务    {catalog.base_url}"))
    print(dim(f"  拉取于  {catalog.age_text}（共 {len(catalog.models)} 个）"))
    print()
    for model in catalog.models:
        tags = []
        if model.id == current:
            tags.append(cyan("当前主模型"))
        if vision and model.id == vision:
            tags.append(green("视觉模型"))
        elif model.supports_image:
            tags.append("可读图")
        suffix = "  ← " + " / ".join(tags) if tags else ""
        print(f"    {model.id}{suffix}")
        if model.summary and model.summary != model.id:
            print(dim(f"        {model.summary}"))
    print()
    if not vision:
        candidates = [m.id for m in catalog.vision_candidates]
        if candidates:
            print(dim(f"  提示：{candidates[0]} 能读图。配成视觉模型后就能参考设计稿了："))
            print(dim(f"        在 .env 里加 VA_VISION_MODEL={candidates[0]}"))
            print()
    print(dim("  /model 打开选择器（上下键选，回车确定）；/model <名字> 直接切"))


def _switch_model(agent: Agent, ui: UI, name: str) -> None:
    from .config import write_env_values
    from .models import load_catalog

    catalog = load_catalog(Path(__file__).resolve().parent.parent)

    if not name:
        # 不给名字就进交互式选择器：上下键移动、回车确定。
        # 手打模型名既费事又容易打错，而换模型是个高频动作。
        if not catalog or not catalog.models:
            ui.info(f"当前模型：{agent.cfg.model}")
            ui.warn("还没有模型清单，跑一次 `python3 run.py setup` 就能拉下来。")
            return
        if len(catalog.models) == 1:
            ui.info(f"只有一个可用模型：{catalog.ids[0]}")
            return

        from .picker import select_model

        try:
            picked = select_model(
                catalog.models,
                current=agent.cfg.model,
                vision=agent.cfg.vision_model,
            )
        except (KeyboardInterrupt, EOFError):
            ui.newline()
            ui.info("已取消")
            return

        if picked is None:
            ui.info("已取消")
            return
        if picked == agent.cfg.model:
            ui.info(f"还是 {picked}，没变")
            return
        name = picked

    if name == agent.cfg.model:
        ui.info(f"已经是 {name} 了")
        return

    if catalog and catalog.models and name not in catalog.ids:
        # 不在清单里不直接拒绝——清单可能是旧的，或者用户就是想试个新的
        ui.warn(f"{name} 不在已缓存的清单里（可能清单过期了）。仍然切过去。")

    agent.set_model(name)
    ui.success(f"已切换到 {name}")

    try:
        write_env_values(Path(__file__).resolve().parent.parent / ".env", {"VA_MODEL": name})
        ui.info("已写进 .env，下次启动还是这个模型")
    except OSError as exc:
        ui.warn(f"写 .env 失败（本次会话仍生效）：{exc}")


# --------------------------------------------------------------------- feature


def _feature(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="va feature",
        description="一条命令跑完：建分支 → 规划 → 编码 → 自测 → 自审 → 提交",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="其他参数：\n" + describe_flags(),
    )
    parser.add_argument("requirement", nargs="*", help="需求描述")
    parser.add_argument("-C", "--workspace", help="工作区目录（默认：当前目录）")
    parser.add_argument("-m", "--model", help="覆盖模型名")
    parser.add_argument("--base-url", help="覆盖 API base url")
    parser.add_argument("--branch", help="指定分支名")
    parser.add_argument("--base", help="从哪个提交/分支切出")
    parser.add_argument("--no-branch", action="store_true", help="不建分支，就地改")
    parser.add_argument("--doc", help="需求文档路径")
    parser.add_argument("--design", help="设计稿图片路径")
    parser.add_argument("--tests", help="自测要跑的命令")
    parser.add_argument("--no-review", action="store_true", help="跳过独立审查")
    parser.add_argument("--push", action="store_true", help="结束后推送（默认不推）")
    parser.add_argument("-y", "--yes", action="store_true", help="自动批准所有操作")
    parser.add_argument("-q", "--quiet", action="store_true", help="不显示思考过程")
    parser.add_argument("--max-steps", type=int, help="最大步数")
    parser.add_argument("--no-session", action="store_true", help="不保存会话")
    args = parser.parse_args(argv)

    requirement = " ".join(args.requirement).strip()
    if not requirement and not args.doc and not args.design:
        parser.error("需要给需求描述，或者用 --doc / --design 指定输入")

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

    cfg.require_key()

    try:
        workspace = Workspace(cfg.workspace, extra_read_roots=cfg.read_dirs)
    except WorkspaceError as exc:
        print(red(f"工作区错误：{exc}"), file=sys.stderr)
        return 2

    ui = UI(workspace, auto_approve=cfg.auto_approve, show_thinking=cfg.show_thinking)

    # feature 跑得久，默认存会话，中途断了能接上
    session_args = argparse.Namespace(
        no_session=args.no_session, resume=None, continue_=False
    )
    session, history, resume_note, saved_plan = _open_session(
        session_args, workspace, cfg, ui
    )

    registry = build_registry(workspace, cfg=cfg)
    agent = Agent(
        cfg,
        ui,
        workspace=workspace,
        registry=registry,
        llm=LLM(cfg),
        session=session,
        history=history,
        extra_instructions=resume_note,
        plan=Plan.from_dict(saved_plan) if saved_plan else None,
    )

    request = FeatureRequest(
        requirement=requirement or "（需求见文档 / 设计稿）",
        branch=args.branch or "",
        base=args.base or "",
        doc=args.doc or "",
        design=args.design or "",
        push=args.push,
        review=not args.no_review,
        tests=args.tests or "",
        no_branch=args.no_branch,
    )
    return run_feature(agent, request, ui)


if __name__ == "__main__":
    raise SystemExit(main())
