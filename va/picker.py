"""终端里的交互式列表选择：上下键移动、回车确定。

**为什么要自己写**：换模型是个高频动作，而每次都要手打模型名既费事又容易打错。
编号列表（输入 1/2/3）好一点，但仍要「看一眼编号→再输一个数字」两步。

**平台差异**：方向键在 POSIX 上靠 ``termios`` 把终端切到原始模式再读转义序列，
Windows 上用 ``msvcrt.getch()``。两边都得保证**退出时把终端还原**——
原始模式没还原的话，用户的终端会变得不能回显、Ctrl-C 也失灵。

**为什么按键来源可注入**：注入之后选择逻辑就成了纯函数，
「按下下键再回车会选中第二项」这种事能在测试里断言，而不用真去敲键盘。
"""

from __future__ import annotations

import os
import sys
from typing import Callable, Iterable, Sequence

from .util import bold, cyan, dim, green, yellow

# 按键的规范化名字
UP = "up"
DOWN = "down"
ENTER = "enter"
CANCEL = "cancel"
HOME = "home"
END = "end"
OTHER = "other"

MAX_VISIBLE = 14  # 模型列表可能很长，一屏放不下就开窗口滚动


# --------------------------------------------------------------------- 按键


class RawKeys:
    """把终端切到原始模式，逐个读取按键并规范化。"""

    def __init__(self, stream=None) -> None:
        self.stream = stream or sys.stdin
        self._saved = None

    def __enter__(self) -> "RawKeys":
        try:
            import termios
            import tty
        except ImportError:
            return self
        try:
            self._saved = termios.tcgetattr(self.stream.fileno())
            tty.setraw(self.stream.fileno())
        except (termios.error, OSError, ValueError):
            self._saved = None  # 不是真终端，读的时候会走别的分支
        return self

    def __exit__(self, *exc) -> None:
        # 一定要还原。留在原始模式下的终端，用户敲什么都看不见。
        if self._saved is None:
            return
        try:
            import termios

            termios.tcsetattr(self.stream.fileno(), termios.TCSADRAIN, self._saved)
        except Exception:  # noqa: BLE001 - 还原失败也不该把异常盖过原来的
            pass

    def read(self) -> str:
        char = self._read_char()
        if char is None:
            return CANCEL
        if char in ("\r", "\n"):
            return ENTER
        if char in ("\x03", "\x04"):  # Ctrl-C / Ctrl-D
            return CANCEL
        if char == "\x1b":
            return self._read_escape()
        return char

    # ------------------------------------------------------------- 内部

    def _read_char(self) -> str | None:
        if os.name == "nt":
            import msvcrt

            return msvcrt.getch().decode("utf-8", "ignore")
        try:
            data = self.stream.read(1)
        except (OSError, ValueError):
            return None
        return data or None

    def _read_escape(self) -> str:
        """ESC 可能是「取消」，也可能是方向键的前缀。"""
        if os.name == "nt":
            import msvcrt

            if not msvcrt.kbhit():
                return CANCEL
            second = msvcrt.getch().decode("utf-8", "ignore")
            if second not in ("[", "\xe0"):
                return CANCEL
            third = msvcrt.getch().decode("utf-8", "ignore")
            return {"H": UP, "P": DOWN, "G": HOME, "O": END}.get(third, OTHER)

        # 读 [ A/B/C/D。用 select 判超时——单独的 ESC 后面不会跟着东西。
        import select

        ready, _, _ = select.select([self.stream], [], [], 0.05)
        if not ready:
            return CANCEL
        second = self.stream.read(1)
        if second != "[":
            return CANCEL
        third = self.stream.read(1)
        return {"A": UP, "B": DOWN, "H": HOME, "F": END}.get(third, OTHER)


def stdin_supports_arrow_keys() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


# --------------------------------------------------------------------- 选择


def _format(labels: Sequence[str], index: int, window_start: int) -> list[str]:
    """把当前要显示的那一段渲染成一行行文本。"""
    lines: list[str] = []
    for offset in range(min(MAX_VISIBLE, len(labels) - window_start)):
        position = window_start + offset
        selected = position == index
        marker = cyan(" ❯ ") if selected else dim("   ")
        text = labels[position]
        if selected:
            text = bold(cyan(text))
        lines.append(f"  {marker}{text}")
    if window_start > 0:
        lines.insert(0, dim(f"     …… 上面还有 {window_start} 个"))
    remaining = len(labels) - (window_start + MAX_VISIBLE)
    if remaining > 0:
        lines.append(dim(f"     …… 下面还有 {remaining} 个"))
    return lines


def _window_start(index: int, total: int) -> int:
    """把选中项保持在窗口里。"""
    if total <= MAX_VISIBLE:
        return 0
    half = MAX_VISIBLE // 2
    start = max(0, index - half)
    return min(start, total - MAX_VISIBLE)


def select(
    title: str,
    options: Sequence[str],
    *,
    default: int = 0,
    hint: str = "↑↓ 移动 · Enter 确定 · Esc 取消",
    keys: Callable[[], str] | None = None,
    write: Callable[[str], None] | None = None,
    flush: Callable[[], None] | None = None,
    interactive: bool | None = None,
) -> int | None:
    """让用户从列表里选一个。返回索引；取消返回 None。

    ``keys`` / ``write`` / ``flush`` 可以注入——测试里就能完整跑一遍
    上下移动和确认，不需要真的接管终端。
    """
    if not options:
        return None

    out = write or (lambda text: sys.stdout.write(text))
    do_flush = flush or sys.stdout.flush
    index = max(0, min(default, len(options) - 1))

    can_interact = stdin_supports_arrow_keys() if interactive is None else interactive
    # 注入了 keys 就按可交互处理（测试场景），否则看终端支不支持
    if keys is None and not can_interact:
        return _numbered_select(title, options, default, out, do_flush)

    reader = keys
    context = None
    if reader is None:
        context = RawKeys()
        context.__enter__()
        reader = context.read

    out(f"\n  {bold(title)}\n")
    out(dim(f"  {hint}\n\n"))

    emitted = 0
    try:
        while True:
            start = _window_start(index, len(options))
            lines = _format(options, index, start)

            if emitted:
                # 光标上移回去，逐行覆盖。比清屏闪烁小得多。
                out(f"\x1b[{emitted}A")
            for line in lines:
                out("\x1b[2K" + line + "\n")
            do_flush()
            emitted = len(lines)

            key = reader()
            if key == UP:
                index = (index - 1) % len(options)
            elif key == DOWN:
                index = (index + 1) % len(options)
            elif key == HOME:
                index = 0
            elif key == END:
                index = len(options) - 1
            elif key == ENTER:
                out("\n")
                do_flush()
                return index
            elif key == CANCEL:
                out("\n")
                out(dim("  已取消\n\n"))
                do_flush()
                return None
            # 其他键忽略，等着
    finally:
        if context is not None:
            context.__exit__(None, None, None)


def _numbered_select(
    title: str,
    options: Sequence[str],
    default: int,
    out: Callable[[str], None],
    do_flush: Callable[[], None],
) -> int | None:
    """退化方案：不能用方向键时（管道、老终端、Windows 的某些壳）用编号。"""
    out(f"\n  {bold(title)}\n\n")
    for position, label in enumerate(options, 1):
        marker = cyan(" ❯ ") if position - 1 == default else "   "
        out(f"  {marker}{dim(str(position) + '.')} {label}\n")
    out("\n")
    do_flush()
    try:
        raw = input(yellow(f"  输入序号（回车用 {default + 1}，q 取消）> ")).strip()
    except (EOFError, KeyboardInterrupt):
        out("\n")
        return None
    if raw.lower() in ("q", "quit", "exit"):
        return None
    if not raw:
        return default
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return int(raw) - 1
    out(dim("  没看懂，已取消\n"))
    return None


def select_model(
    models: Iterable,  # ModelInfo 列表
    *,
    current: str = "",
    vision: str = "",
    **kwargs,
) -> str | None:
    """模型专用的一层包装：把元信息拼成展示文案。"""
    items = list(models)

    # 当前模型可能不在清单里——服务商的 /models 不列别名（deepseek-chat 就是
    # 这种情况），但那个名字确实能用。不补进去的话，用户一旦切走就再也
    # 切不回来了。这件事放在这里而不是调用方，否则每个入口都要抄一遍。
    ids = [getattr(m, "id", str(m)) for m in items]
    if current and current not in ids:
        from .models import ModelInfo

        items.insert(
            0, ModelInfo(id=current, name="服务商未列出这个别名，但当前正在用")
        )

    labels: list[str] = []
    ids = []
    default = 0
    width = max((len(getattr(m, "id", str(m))) for m in items), default=0)

    for position, model in enumerate(items):
        model_id = getattr(model, "id", str(model))
        ids.append(model_id)

        tags = []
        if model_id == current:
            tags.append(green("当前"))
        if vision and model_id == vision:
            tags.append(green("视觉模型"))
        elif getattr(model, "supports_image", False):
            tags.append(cyan("可读图"))

        suffix = ("   " + " · ".join(tags)) if tags else ""
        labels.append(f"{model_id:<{width}}{suffix}")

        if model_id == current:
            default = position

    chosen = select("选择模型", labels, default=default, **kwargs)
    if chosen is None:
        return None
    return ids[chosen]
