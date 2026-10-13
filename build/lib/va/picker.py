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

import io
import os
import select as _select  # 本模块有个函数也叫 select，必须用别名
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
    """把终端切到原始模式，逐个读取按键并规范化。

    **一切都走文件描述符（``os.read`` + ``select``），不碰 ``sys.stdin``。**

    这一点是踩过坑的：``sys.stdin`` 是带缓冲的 ``TextIOWrapper``。
    对它调 ``read(1)`` 会把 ``\x1b[B`` 三个字节**一起吞进 Python 层缓冲区**，
    之后 ``select`` 去查 OS 层的 fd 自然什么都查不到——于是每次按方向键
    都被判成超时。真终端里的表现就是「按了没反应，然后冒出一串 ^[[B」。
    注入按键的测试全绿而真终端全废，正是因为测试绕过了真实 IO。
    """

    def __init__(self, stream=None) -> None:
        self.stream = stream or sys.stdin
        self.fd: int | None = None
        self._saved = None

    def __enter__(self) -> "RawKeys":
        try:
            fd = self.stream.fileno()
        except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
            fd = None
        # mock 对象之类会返回非整数，那就退回按流读取（测试场景）
        self.fd = fd if isinstance(fd, int) else None

        if self.fd is None:
            return self

        try:
            import termios
            import tty

            self._saved = termios.tcgetattr(self.fd)
            # 用 TCSANOW 而不是默认的 TCSAFLUSH：后者会丢掉还没读的输入，
            # 手快连按两下方向键时第二下就没了。
            tty.setraw(self.fd, termios.TCSANOW)
        except Exception:  # noqa: BLE001 - 不是终端（管道、重定向）就算了
            self._saved = None
        return self

    def __exit__(self, *exc) -> None:
        # 一定要还原。留在原始模式下的终端，用户敲什么都看不见——
        # 这比「选不了模型」严重得多。
        if self._saved is None or self.fd is None:
            return
        try:
            import termios

            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)
        except Exception:  # noqa: BLE001 - 还原失败也不该盖过原来的异常
            pass
        finally:
            self._saved = None

    # ------------------------------------------------------------- 读取

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

    def _read_char(self) -> str | None:
        if self.fd is not None:
            try:
                data = os.read(self.fd, 1)
            except (OSError, ValueError):
                return None
            return data.decode("utf-8", "replace") if data else None

        if os.name == "nt":
            import msvcrt

            return msvcrt.getch().decode("utf-8", "ignore")

        try:
            text = self.stream.read(1)
        except (OSError, ValueError, io.UnsupportedOperation):
            return None
        return text or None

    def _ready(self, timeout: float) -> bool:
        """fd 上有没有后续字节。没有探测手段时就当有。"""
        if self.fd is None:
            return True
        try:
            ready, _, _ = _select.select([self.fd], [], [], timeout)
        except (OSError, ValueError):
            return True
        return bool(ready)

    def _read_escape(self) -> str:
        """ESC 既可能是「取消」也可能是方向键前缀——只能靠「后面还有没有字节」区分。

        不判这个超时，单独的 ESC 会一直等第二个字节，表现成「按了没反应」。
        """
        if not self._ready(0.05):
            return CANCEL

        second = self._read_char()
        if second not in ("[", "\xe0"):
            return CANCEL
        third = self._read_char()
        return {"A": UP, "B": DOWN, "H": HOME, "F": END}.get(third or "", OTHER)


def stdin_supports_arrow_keys() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


# --------------------------------------------------------------------- 选择


def _frame_lines(labels: Sequence[str], index: int, total: int) -> list[str]:
    """渲染一帧。

    **行数恒定**（可见项 + 上下两条滚动提示），这一点很关键：
    重画靠的是「光标上移 N 行」，N 一旦和上一帧对不上就会越画越歪，
    用户看到的就是列表错位。所以提示行即使没内容也占一行，用空串占位。
    """
    visible = min(MAX_VISIBLE, total)
    start = _window_start(index, total)
    above = start
    below = total - (start + visible)

    lines = [dim(f"     …… 上面还有 {above} 个") if above else ""]
    for offset in range(visible):
        position = start + offset
        text = labels[position]
        if position == index:
            lines.append("  " + cyan(" ❯ ") + bold(text))
        else:
            lines.append("     " + text)
    lines.append(dim(f"     …… 下面还有 {below} 个") if below else "")
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
    out("\x1b[?25l")  # 藏起光标，免得它跟着重画乱跳

    emitted = 0
    try:
        while True:
            lines = _frame_lines(options, index, len(options))

            if emitted:
                # F = 上移 N 行并回到行首；A 只上移，容易残留缩进
                out(f"\x1b[{emitted}F")
            for line in lines:
                out("\x1b[2K" + line + "\r\n")
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
                out("\r\n")
                do_flush()
                return index
            elif key == CANCEL:
                out("\r\n")
                out(dim("  已取消\r\n\r\n"))
                do_flush()
                return None
            # 其他键忽略，等着
    finally:
        out("\x1b[?25h")  # 无论怎么退出都要把光标还回去
        do_flush()
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
