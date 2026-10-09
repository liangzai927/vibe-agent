"""交互式选择器的测试。

这类代码平时只能靠人手敲一遍来验证，而人手只会敲「正常路径」——
上下键连按、到顶回绕、列表比屏幕长、中途按 Esc，这些都不会试。
把按键来源做成可注入之后，它们就都是普通断言了。

另外盯一件事：**终端状态必须还原**。原始模式没还原的话，用户的终端会
变得不能回显、Ctrl-C 也失灵——比功能不好用严重得多。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from va import picker  # noqa: E402
from va.models import ModelInfo  # noqa: E402
from va.picker import CANCEL, DOWN, END, ENTER, HOME, UP, select, select_model  # noqa: E402

ANSI = __import__("re").compile(r"\x1b\[[0-9;]*[A-Za-z]")


def keys_for(*sequence: str):
    """把一串按键做成可注入的读取器。"""
    remaining = list(sequence)

    def read() -> str:
        if not remaining:
            raise AssertionError("按键用完了，选择器还在读——说明它没在该退出的时候退出")
        return remaining.pop(0)

    return read


def render(**kwargs):
    frames: list[str] = []
    return frames, kwargs


def run_select(options, *sequence, **kwargs):
    """跑一次选择，返回 (结果, 全部输出)。"""
    frames: list[str] = []
    result = select(
        "标题",
        options,
        keys=keys_for(*sequence),
        write=frames.append,
        flush=lambda: None,
        **kwargs,
    )
    return result, ANSI.sub("", "".join(frames))


# --------------------------------------------------------------- 基本行为


class TestSelect(unittest.TestCase):
    def test_enter_picks_the_first_by_default(self):
        result, _ = run_select(["a", "b", "c"], ENTER)
        self.assertEqual(result, 0)

    def test_down_then_enter(self):
        result, _ = run_select(["a", "b", "c"], DOWN, ENTER)
        self.assertEqual(result, 1)

    def test_multiple_downs(self):
        result, _ = run_select(["a", "b", "c"], DOWN, DOWN, ENTER)
        self.assertEqual(result, 2)

    def test_up_wraps_to_the_end(self):
        """到顶再按上应该绕到最后一个，而不是卡住。"""
        result, _ = run_select(["a", "b", "c"], UP, ENTER)
        self.assertEqual(result, 2)

    def test_down_wraps_to_the_start(self):
        result, _ = run_select(["a", "b"], DOWN, DOWN, DOWN, ENTER)
        self.assertEqual(result, 1)

    def test_home_and_end(self):
        result, _ = run_select(["a", "b", "c", "d"], END, ENTER)
        self.assertEqual(result, 3)
        result, _ = run_select(["a", "b", "c", "d"], END, HOME, ENTER)
        self.assertEqual(result, 0)

    def test_cancel_returns_none(self):
        result, _ = run_select(["a", "b"], CANCEL)
        self.assertIsNone(result)

    def test_cancel_after_moving(self):
        result, _ = run_select(["a", "b", "c"], DOWN, DOWN, CANCEL)
        self.assertIsNone(result)

    def test_default_index(self):
        result, _ = run_select(["a", "b", "c"], ENTER, default=2)
        self.assertEqual(result, 2)

    def test_default_beyond_the_end_is_clamped(self):
        result, _ = run_select(["a", "b"], ENTER, default=99)
        self.assertEqual(result, 1)

    def test_empty_list_returns_none(self):
        self.assertIsNone(select("标题", [], keys=keys_for(), write=lambda *_: None))

    def test_unknown_keys_are_ignored(self):
        result, _ = run_select(["a", "b"], "x", "z", DOWN, ENTER)
        self.assertEqual(result, 1)

    def test_raises_if_it_keeps_reading_past_the_script(self):
        """选择器必须在收到 Enter/Esc 时退出，不能无限读。"""
        with self.assertRaises(AssertionError):
            run_select(["a"], DOWN)


# --------------------------------------------------------------- 渲染


class TestRendering(unittest.TestCase):
    def test_shows_all_options(self):
        _, text = run_select(["alpha", "beta", "gamma"], ENTER)
        for label in ("alpha", "beta", "gamma"):
            self.assertIn(label, text)

    def test_marks_the_selected_one(self):
        _, text = run_select(["alpha", "beta"], DOWN, ENTER)
        self.assertIn("❯", text)

    def test_header_shows_title_and_hint(self):
        frames: list[str] = []
        select(
            "选择模型", ["a"], keys=keys_for(ENTER),
            write=frames.append, flush=lambda: None,
        )
        text = ANSI.sub("", "".join(frames))
        self.assertIn("选择模型", text)
        self.assertIn("Esc", text, "要告诉用户怎么取消")

    def test_redraws_with_cursor_movement(self):
        """每帧全屏重画会闪，应该是光标上移覆盖。"""
        frames: list[str] = []
        select(
            "t", ["a", "b"], keys=keys_for(DOWN, ENTER),
            write=frames.append, flush=lambda: None,
        )
        raw = "".join(frames)
        self.assertIn("\x1b[", raw, "应该有光标控制序列")
        self.assertIn("\x1b[2K", raw, "应该逐行清除，而不是清整个屏幕")

    def test_cancel_message_is_shown(self):
        frames: list[str] = []
        select(
            "t", ["a"], keys=keys_for(CANCEL),
            write=frames.append, flush=lambda: None,
        )
        self.assertIn("取消", ANSI.sub("", "".join(frames)))


class TestLongLists(unittest.TestCase):
    def _long(self, count: int):
        return [f"model-{i:02d}" for i in range(count)]

    def test_window_keeps_the_selection_visible(self):
        options = self._long(40)
        _, text = run_select(options, END, ENTER)
        self.assertIn("model-39", text, "选到最后一个时它必须可见")
        self.assertIn("上面还有", text, "应该提示上面还有多少")

    def test_window_does_not_render_everything(self):
        options = self._long(40)
        _, text = run_select(options, ENTER)
        shown = sum(1 for line in text.splitlines() if "model-" in line)
        self.assertLessEqual(shown, picker.MAX_VISIBLE, f"一屏渲染了 {shown} 项")

    def test_short_list_has_no_scroll_hints(self):
        _, text = run_select(["a", "b", "c"], ENTER)
        self.assertNotIn("上面还有", text)
        self.assertNotIn("下面还有", text)

    def test_window_start_clamps_at_the_end(self):
        self.assertEqual(picker._window_start(39, 40), 40 - picker.MAX_VISIBLE)
        self.assertEqual(picker._window_start(0, 40), 0)
        self.assertEqual(picker._window_start(5, 3), 0)


# --------------------------------------------------------------- 退化方案


class TestNumberedFallback(unittest.TestCase):
    """不能用方向键时（管道、老终端）也不能没法选。"""

    def run_numbered(self, options, answer, **kwargs):
        frames: list[str] = []
        with mock.patch("builtins.input", return_value=answer):
            result = select(
                "标题", options,
                write=frames.append, flush=lambda: None,
                interactive=False,
                **kwargs,
            )
        return result, ANSI.sub("", "".join(frames))

    def test_picks_by_number(self):
        result, _ = self.run_numbered(["a", "b", "c"], "2")
        self.assertEqual(result, 1)

    def test_empty_answer_uses_default(self):
        result, _ = self.run_numbered(["a", "b"], "", default=1)
        self.assertEqual(result, 1)

    def test_q_cancels(self):
        self.assertIsNone(self.run_numbered(["a"], "q")[0])
        self.assertIsNone(self.run_numbered(["a"], "Q")[0])

    def test_out_of_range_is_cancelled(self):
        self.assertIsNone(self.run_numbered(["a", "b"], "9")[0])

    def test_garbage_is_cancelled(self):
        self.assertIsNone(self.run_numbered(["a"], "随便打的")[0])

    def test_eof_is_cancelled(self):
        with mock.patch("builtins.input", side_effect=EOFError):
            result = select(
                "t", ["a"], write=lambda *_: None, flush=lambda: None, interactive=False
            )
        self.assertIsNone(result)

    def test_shows_numbers(self):
        _, text = self.run_numbered(["alpha", "beta"], "1")
        self.assertIn("1.", text)
        self.assertIn("2.", text)


# --------------------------------------------------------------- 模型包装


class TestSelectModel(unittest.TestCase):
    MODELS = [
        ModelInfo(id="deepseek-chat"),
        ModelInfo(
            id="deepseek-flash",
            name="DeepSeek-V4.1-Flash",
            input_modalities=["text", "image"],
            context_window=1048576,
        ),
        ModelInfo(id="deepseek-v4-pro", name="DeepSeek-V4-Pro", context_window=1048576),
    ]

    def choose(self, *sequence, **kwargs):
        """注意别把这个方法叫 run —— TestCase.run 是 unittest 用来执行测试的，
        覆盖它会让整个 runner 崩掉，而且报错信息完全指不到点上。"""
        frames: list[str] = []
        result = select_model(
            self.MODELS,
            keys=keys_for(*sequence),
            write=frames.append,
            flush=lambda: None,
            **kwargs,
        )
        return result, ANSI.sub("", "".join(frames))

    def test_returns_the_model_id(self):
        chosen, _ = self.choose(DOWN, ENTER, current="deepseek-chat")
        self.assertEqual(chosen, "deepseek-flash")

    def test_defaults_to_the_current_model(self):
        chosen, _ = self.choose(ENTER, current="deepseek-v4-pro")
        self.assertEqual(chosen, "deepseek-v4-pro")

    def test_marks_the_current_model(self):
        _, text = self.choose(ENTER, current="deepseek-chat")
        self.assertIn("当前", text)

    def test_marks_image_capable_models(self):
        _, text = self.choose(ENTER, current="deepseek-chat")
        self.assertIn("可读图", text)

    def test_marks_the_vision_model(self):
        _, text = self.choose(ENTER, current="deepseek-chat", vision="deepseek-flash")
        self.assertIn("视觉模型", text)

    def test_cancel_returns_none(self):
        chosen, _ = self.choose(CANCEL, current="deepseek-chat")
        self.assertIsNone(chosen)

    def test_current_not_in_list_is_added_back(self):
        """服务商的 /models 不列别名（deepseek-chat 就是这种情况），
        但那个名字确实能用。补不进去的话用户切走就再也切不回来了。"""
        models = [m for m in self.MODELS if m.id != "deepseek-chat"]
        frames: list[str] = []
        result = select_model(
            models, current="deepseek-chat",
            keys=keys_for(ENTER), write=frames.append, flush=lambda: None,
        )
        self.assertEqual(result, "deepseek-chat", "回车应该就能选回当前模型")
        self.assertIn("deepseek-chat", ANSI.sub("", "".join(frames)))

    def test_missing_current_is_not_invented(self):
        """没有 current 时不该凭空造一个条目出来。"""
        models = [m for m in self.MODELS if m.id != "deepseek-chat"]
        frames: list[str] = []
        select_model(
            models, keys=keys_for(ENTER), write=frames.append, flush=lambda: None
        )
        self.assertNotIn("deepseek-chat", ANSI.sub("", "".join(frames)))

    def test_accepts_plain_strings(self):
        frames: list[str] = []
        chosen = select_model(
            ["a", "b"], keys=keys_for(DOWN, ENTER),
            write=frames.append, flush=lambda: None,
        )
        self.assertEqual(chosen, "b")


# --------------------------------------------------------------- 终端状态


class TestRawKeys(unittest.TestCase):
    def test_exit_without_enter_is_safe(self):
        """没成功切进原始模式时（不是真终端），退出也不能炸。"""
        keys = picker.RawKeys(stream=mock.Mock())
        keys.__exit__(None, None, None)  # 没 __enter__ 过

    def test_enter_and_exit_on_a_non_tty(self):
        stream = mock.Mock()
        stream.fileno.side_effect = ValueError("not a tty")
        with picker.RawKeys(stream=stream) as keys:
            self.assertIsNotNone(keys)
        # 没有真正切进原始模式，就不该尝试还原

    def test_read_returns_cancel_on_eof(self):
        stream = mock.Mock()
        stream.read.return_value = ""
        keys = picker.RawKeys(stream=stream)
        self.assertEqual(keys.read(), CANCEL)

    def test_control_c_is_cancel(self):
        stream = mock.Mock()
        stream.read.return_value = "\x03"
        self.assertEqual(picker.RawKeys(stream=stream).read(), CANCEL)

    def test_escape_alone_is_cancel_not_a_hang(self):
        """单独的 ESC 后面不会有字符。读的时候必须判超时，否则会卡死。"""
        if os.name == "nt":
            self.skipTest("Windows 分支走 msvcrt")
        stream = mock.Mock()
        stream.read.return_value = "\x1b"
        with mock.patch("select.select", return_value=([], [], [])):
            self.assertEqual(picker.RawKeys(stream=stream).read(), CANCEL)

    def test_arrow_sequence_is_decoded(self):
        if os.name == "nt":
            self.skipTest("Windows 分支走 msvcrt")
        stream = mock.Mock()
        stream.read.side_effect = ["\x1b", "[", "A"]
        with mock.patch("select.select", return_value=([stream], [], [])):
            self.assertEqual(picker.RawKeys(stream=stream).read(), UP)

    def test_unknown_escape_is_not_mistaken_for_cancel(self):
        if os.name == "nt":
            self.skipTest("Windows 分支走 msvcrt")
        stream = mock.Mock()
        stream.read.side_effect = ["\x1b", "[", "Z"]
        with mock.patch("select.select", return_value=([stream], [], [])):
            self.assertEqual(picker.RawKeys(stream=stream).read(), picker.OTHER)

    def test_plain_characters_pass_through(self):
        stream = mock.Mock()
        stream.read.return_value = "q"
        self.assertEqual(picker.RawKeys(stream=stream).read(), "q")

    def test_enter_accepts_both_line_endings(self):
        for char in ("\r", "\n"):
            stream = mock.Mock()
            stream.read.return_value = char
            self.assertEqual(picker.RawKeys(stream=stream).read(), ENTER)


class TestRawKeysOverARealFd(unittest.TestCase):
    """用**真的文件描述符**测按键读取。

    这一组是补出来的，因为原来那些用 mock 流的测试**全都漏掉了真正的 bug**：
    `sys.stdin` 是带缓冲的 TextIOWrapper，对它 read(1) 会把 `\x1b[B` 三个字节
    一起吞进 Python 层缓冲区，之后 select 去查 OS 层的 fd 什么都查不到——
    真终端里表现为「按方向键没反应，然后冒出一串 ^[[B」。

    mock 的 read() 不经过任何缓冲，所以永远测不出这件事。管道有真 fd，
    能走完整的 os.read + select 路径。
    """

    def setUp(self) -> None:
        self._fds: list[int] = []

    def tearDown(self) -> None:
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                pass

    def make(self):
        read_fd, write_fd = os.pipe()
        self._fds += [read_fd, write_fd]
        stream = os.fdopen(read_fd, "rb", buffering=0)
        self._fds.remove(read_fd)  # 交给 stream 管
        return picker.RawKeys(stream=stream), write_fd

    def feed(self, write_fd: int, payload: bytes, delay: float = 0.06) -> None:
        def writer():
            time.sleep(0.05)
            for index in range(0, len(payload)):
                os.write(write_fd, payload[index : index + 1])
                time.sleep(0.03)

        threading.Thread(target=writer, daemon=True).start()

    def test_binds_to_the_fd_not_the_stream(self):
        keys, _ = self.make()
        with keys:
            self.assertIsInstance(keys.fd, int, "必须拿到真 fd，否则会退回带缓冲的流读取")

    def test_arrow_keys_are_read_as_single_events(self):
        """三个字节必须被当成一个方向键，而不是三次独立读取。"""
        keys, write_fd = self.make()
        self.feed(write_fd, b"\x1b[B")
        with keys:
            self.assertEqual(keys.read(), DOWN)

    def test_consecutive_arrows_do_not_get_lost(self):
        """手快连按两下，第二下不能被 TCSAFLUSH 之类的设置吃掉。"""
        keys, write_fd = self.make()
        self.feed(write_fd, b"\x1b[B\x1b[B\x1b[A\r")
        with keys:
            got = [keys.read() for _ in range(4)]
        self.assertEqual(got, [DOWN, DOWN, UP, ENTER])

    def test_lone_escape_still_cancels(self):
        keys, write_fd = self.make()
        self.feed(write_fd, b"\x1b")
        with keys:
            self.assertEqual(keys.read(), CANCEL)

    def test_eof_is_cancel(self):
        read_fd, write_fd = os.pipe()
        self._fds += [read_fd, write_fd]
        stream = os.fdopen(read_fd, "rb", buffering=0)
        self._fds.remove(read_fd)
        os.close(write_fd)
        self._fds.remove(write_fd)
        self.assertEqual(picker.RawKeys(stream=stream).read(), CANCEL)

    def test_raw_mode_is_left_off_on_a_pipe(self):
        """管道不是终端，切不了原始模式——但不能因此崩掉。"""
        keys, _ = self.make()
        with keys:
            pass
        self.assertIsNone(keys._saved)

    def test_end_to_end_selection_over_a_pipe(self):
        """整条链路：注入 fd 上的真实按键，走完整个选择流程。"""
        read_fd, write_fd = os.pipe()
        self._fds += [read_fd, write_fd]
        stream = os.fdopen(read_fd, "rb", buffering=0)
        self._fds.remove(read_fd)

        keys = picker.RawKeys(stream=stream)
        self.feed(write_fd, b"\x1b[B\x1b[B\r")
        with keys:
            frames: list[str] = []
            result = select(
                "选择模型", ["a", "b", "c"],
                keys=keys.read, write=frames.append, flush=lambda: None,
            )
        self.assertEqual(result, 2, "按下下回车应该选中第三项")


class TestFrameHeight(unittest.TestCase):
    """帧高度必须恒定。

    重画靠「光标上移 N 行」，N 和上一帧对不上就会越画越歪——
    用户看到的就是列表错位。
    """

    def test_height_is_constant_regardless_of_position(self):
        for total in (1, 2, 3, picker.MAX_VISIBLE, picker.MAX_VISIBLE + 1, 60):
            labels = [f"m{i}" for i in range(total)]
            heights = {len(picker._frame_lines(labels, i, total)) for i in range(total)}
            self.assertEqual(
                len(heights), 1, f"{total} 项时帧高会变：{heights}——重画必然错位"
            )

    def test_height_is_the_same_when_scrolling_starts(self):
        small = [f"m{i}" for i in range(picker.MAX_VISIBLE)]
        big = [f"m{i}" for i in range(picker.MAX_VISIBLE + 5)]
        self.assertEqual(
            len(picker._frame_lines(small, 0, len(small))),
            len(picker._frame_lines(big, 0, len(big))),
            "刚好装得下和需要滚动时，帧高应该一致",
        )

    def test_scroll_hints_occupy_a_line_even_when_empty(self):
        labels = ["a", "b"]
        lines = picker._frame_lines(labels, 0, 2)
        self.assertEqual(len(lines), 4, "2 项 + 上下提示行 = 4 行")

    def test_selection_is_always_visible(self):
        labels = [f"m{i:02d}" for i in range(40)]
        for index in (0, 5, 20, 39):
            text = ANSI.sub("", "\n".join(picker._frame_lines(labels, index, 40)))
            self.assertIn(f"m{index:02d}", text)


class TestCursorHandling(unittest.TestCase):
    def test_cursor_is_hidden_and_restored(self):
        frames: list[str] = []
        select(
            "t", ["a", "b"], keys=keys_for(DOWN, ENTER),
            write=frames.append, flush=lambda: None,
        )
        raw = "".join(frames)
        self.assertIn("\x1b[?25l", raw, "重画时要藏起光标")
        self.assertIn("\x1b[?25h", raw, "退出时必须把光标还回去")

    def test_cursor_restored_even_on_cancel(self):
        frames: list[str] = []
        select(
            "t", ["a"], keys=keys_for(CANCEL),
            write=frames.append, flush=lambda: None,
        )
        self.assertIn("\x1b[?25h", "".join(frames))

    def test_cursor_restored_when_the_key_reader_blows_up(self):
        """读按键时抛异常也必须还原光标和终端。"""
        frames: list[str] = []
        with self.assertRaises(RuntimeError):
            select(
                "t", ["a"], keys=lambda: (_ for _ in ()).throw(RuntimeError("炸")),
                write=frames.append, flush=lambda: None,
            )
        self.assertIn("\x1b[?25h", "".join(frames))

    def test_uses_cursor_previous_line_not_just_up(self):
        """F 会同时回到行首，A 不会——用 A 容易残留缩进。"""
        frames: list[str] = []
        select(
            "t", ["a", "b"], keys=keys_for(DOWN, ENTER),
            write=frames.append, flush=lambda: None,
        )
        self.assertIn("F", "".join(frames))


class TestNoNameCollisions(unittest.TestCase):
    """模块里的函数名不能撞上它导入的模块名。

    `def select(...)` 一度遮蔽了 `select` 模块，于是 `select.select(...)`
    变成「对函数取属性」，方向键直接报废。
    """

    def test_select_module_is_aliased(self):
        import types

        import va.picker as module

        self.assertIsInstance(
            module._select, types.ModuleType, "select 模块必须以别名导入"
        )
        self.assertTrue(callable(module._select.select))

    def test_imported_modules_are_not_shadowed(self):
        """模块里定义同名函数会遮蔽导入的模块。

        `def select(...)` 一度遮蔽了 `select` 模块，于是 `select.select(...)`
        变成「对函数取属性」——方向键直接报废，而报错信息完全指不到点上。
        """
        import types

        import va.picker as module

        # 只查这个模块真正用到的。多写一个不存在的名字只会制造假失败。
        for name in ("io", "os", "sys"):
            obj = getattr(module, name, None)
            self.assertIsNotNone(obj, f"va.picker 没有导入 {name}")
            self.assertIsInstance(
                obj, types.ModuleType,
                f"va.picker.{name} 被别的东西占了——模块内所有 {name}.* 引用都会失效",
            )


class TestTestCaseHygiene(unittest.TestCase):
    """测试类里不能有名字撞上 TestCase 自身的方法。

    我把一个辅助方法命名成 run，结果它覆盖了 TestCase.run，
    整个 runner 直接崩掉，而且 traceback 全指向 unittest 内部，
    完全看不出是自己的问题。
    """

    # 只禁那些**不该被覆盖**的：run 是 unittest 执行测试的入口，
    # assert* 是它用来断言的。setUp / tearDown / skipTest 本来就允许覆盖，
    # 把它们也算进来只会制造误报。
    RESERVED = {"run"}

    def test_no_helper_shadows_testcase_methods(self):
        import tests.test_picker as module

        offenders = []
        for name in dir(module):
            obj = getattr(module, name)
            if not (isinstance(obj, type) and issubclass(obj, unittest.TestCase)):
                continue
            for attr in vars(obj):
                if attr in self.RESERVED or (
                    attr.startswith("assert") and callable(getattr(obj, attr, None))
                ):
                    offenders.append(f"{name}.{attr}")
        self.assertEqual(offenders, [], f"这些方法名会覆盖 TestCase 自身：{offenders}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
