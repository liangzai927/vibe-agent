"""需要工作区与配置的工具的共同基础。

「渲染页面」和「解析设计稿」看起来是两件事，但它们共享同一套前置条件：
都不假设环境、都要解析工作区路径、都需要一个多模态模型配置。
把这些抽出来，是为了让「缺视觉模型时给什么提示」只有一份实现——
容器里用户看不到 traceback 的上下文，提示必须自解释且一致。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..config import Config
from ..workspace import Workspace, WorkspaceError
from .base import Tool, ToolError

VISION_HINT = (
    "没有配置视觉模型，读不了图。\n"
    "主模型通常不支持读图（deepseek-chat 就不支持），要单独指一个多模态模型。\n"
    "\n"
    "**先看你现在的服务商有没有能读图的模型**——同源的话不用另开账号、不用另一个 key：\n"
    "  /models 会标注哪些模型「可读图」，然后只写一行就够了（地址和 key 自动沿用主配置）：\n"
    "    VA_VISION_MODEL=<那个模型名>\n"
    "\n"
    "跨服务商才需要写全：\n"
    "  VA_VISION_MODEL=gpt-4o\n"
    "  VA_VISION_BASE_URL=https://api.openai.com/v1\n"
    "  VA_VISION_API_KEY=sk-...\n"
    "\n"
    "完全不想配模型也行，这几条路不需要模型：\n"
    "  inspect_image  尺寸、主色 hex、内容区边界、字符布局图——写 CSS 够用了\n"
    "  render_page    渲染真实页面\n"
    "  pixel_diff     和设计稿逐像素对比，差异百分比是客观判据\n"
    "少的是「该改什么」的自然语言提示，以及图上的文字和语义。"
)


@dataclass
class AgentContext:
    """工具层需要共享的运行时状态。

    有些工具不能只靠 workspace 干活——比如委派子 agent 时要拿到父 agent 的
    撤销栈，否则子 agent 改的文件 `/undo` 撤不掉。把它们显式打包传下去，
    比让工具各自去反查全局状态可靠得多。

    ui / undo / plan_board 用 Any 标注是刻意的：它们来自上层，写死类型会让
    工具层反向依赖 ui、history、plan，得不偿失。
    """

    workspace: Workspace
    cfg: Config | None = None
    plan_board: Any = None  # PlanBoard
    undo: Any = None  # UndoStack
    ui: Any = None  # UI
    llm: Any = None  # LLM —— 子 agent 要复用它，别每次新建连接
    # 后台长驻进程（dev server 之类）。**属于会话，不属于某次工具调用**——
    # 启动之后要在后续多次调用里继续读它、等它、停它。
    processes: Any = None  # ProcessManager
    # MCP 客户端（外部工具协议）。会话级：server 是子进程，要统一收拾。
    mcp: Any = None  # McpManager
    # 生效的工作流定义。工具的审批策略要读它（比如 mcp.approve）
    workflow: Any = None  # Workflow
    depth: int = 0  # 0 表示这是主 agent
    max_depth: int = 1

    def can_delegate(self) -> bool:
        return self.depth < self.max_depth


class WorkspaceTool(Tool):
    """工具层里「要解析路径、要读配置」的那一类。"""

    def __init__(self, workspace: Workspace, cfg: Config | None = None) -> None:
        self.workspace = workspace
        self._cfg = cfg

    # ------------------------------------------------------------------ 配置

    def config(self) -> Config:
        if self._cfg is None:
            self._cfg = Config.load(self.workspace.root)
        return self._cfg

    def main_model_reads_images(self) -> bool:
        """主模型自己就是多模态的吗。

        只看缓存的模型清单（首次配置时拉下来的）。清单过期或没有清单时
        返回 False——宁可让用户看到明确的提示，也不要瞎猜。
        """
        cfg = self.config()
        if not cfg:
            return False
        try:
            from ..models import load_catalog

            catalog = load_catalog(Path(__file__).resolve().parents[2])
        except Exception:  # noqa: BLE001 - 读不到清单就别下结论
            return False
        if catalog is None:
            return False
        info = catalog.find(cfg.model)
        return bool(info and info.supports_image)

    @staticmethod
    def vision_label(cfg: Config) -> str:
        """视觉模型的展示文案。沿用主模型时要点明，否则用户会以为自己配过。"""
        if cfg.vision_model == cfg.model:
            return f"{cfg.vision_model}（沿用主模型——它自己支持读图）"
        return cfg.vision_model

    def require_vision(self) -> Config:
        """拿到视觉配置。

        没单独配 VA_VISION_MODEL 时，**先看主模型自己能不能读图**——
        能读就直接沿用它。否则用户会撞见一个很别扭的局面：
        他用的模型明明支持图片输入，工具却告诉他「没有视觉模型」，
        于是转头去写脚本绕过工具。
        """
        cfg = self.config()
        if cfg.has_vision:
            return cfg
        if self.main_model_reads_images():
            return replace(cfg, vision_model=cfg.model)
        raise ToolError(VISION_HINT)

    # ------------------------------------------------------------------ 路径

    def resolve(self, path: str, *, what: str = "文件") -> Path:
        """解析工作区内路径，把 WorkspaceError 转成带上下文的 ToolError。"""
        try:
            return self.workspace.resolve(path, must_exist=True)
        except WorkspaceError as exc:
            raise ToolError(f"找不到{what} {path!r}：{exc}") from exc

    # ---------------------------------------------------- 读工作区外的文件

    def is_external(self, path: str | None) -> bool:
        """这个路径在工作区外、且没被 VA_READ_DIRS 预先放行吗。"""
        if not path:
            return False
        try:
            return self.workspace.needs_external_approval(path)
        except Exception:  # noqa: BLE001 - 判不出来就当外部，宁可多问一次
            return True

    def resolve_readable(self, path: str, *, what: str = "文件") -> Path:
        """解析一个只用于读取的路径，允许落在工作区外。

        **写操作永远不要用这个。** 它存在的唯一理由是：用户手上那张设计稿
        在 ~/Downloads，而不是在项目里。调用方有义务配套实现
        ``needs_approval()``，否则越界读取就没有任何拦截了。
        """
        # scope=workspace 时越界是禁止的——这里就要拦住，
        # 不能指望「审批会被拒」，因为那种设置下根本不会弹审批。
        if not self.workspace.external_read_allowed() and not self.workspace.readable(path):
            raise ToolError(
                f"当前设置只允许读工作区内的文件：{path!r}\n"
                f"（想读工作区外，在设置里把「允许读取工作区外的文件」"
                f"改成「全部」或列出具体目录）"
            )

        try:
            return self.workspace.resolve_readable(path, must_exist=True)
        except WorkspaceError as exc:
            raise ToolError(f"找不到{what} {path!r}：{exc}") from exc

    def external_preview(self, args: dict, arg_name: str) -> str | None:
        """越界读取时在审批框里显示的话。工作区内的返回 None（不用多嘴）。

        顺带点出 VA_READ_DIRS——不然用户会以为每次都得点同意，
        而「每次都问」和「不能用」在体感上差不多。
        """
        path = args.get(arg_name)
        if not self.is_external(path):
            return None
        try:
            parent = Path(str(path)).expanduser().parent
        except (OSError, RuntimeError):
            parent = None
        lines = [
            f"⚠ 要读工作区外的文件：{path}",
            "",
            "读图片/文档允许越界（设计稿常常不在项目里），但每次都会先问。",
        ]
        if parent:
            lines.append(f"把这个目录加进 .env 就不用再问：VA_READ_DIRS={parent}")
        lines.append("写操作和静默的文本读取仍然锁死在工作区内。")
        return "\n".join(lines)

    def output_path(self, path: str | None, default: str) -> Path:
        """产物落盘路径：给了就用给的，没给就用默认值（相对工作区）。"""
        return self.workspace.resolve(path) if path else self.workspace.resolve(default)

    def rel(self, path: Path) -> str:
        """给人看的路径。

        工作区内的用相对路径（``src/a.py``），agent 自己目录下的显示成
        ``~/.vibe-agent/...``——把一长串绝对路径糊到界面上没有任何信息量。
        """
        path = Path(path)
        if self.workspace.contains(path):
            return self.workspace.relative(path)
        from ..paths import describe

        return describe(path)
