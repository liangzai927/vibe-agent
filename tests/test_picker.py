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


class TestTestCaseHygiene(unittest.TestCase):
    """测试类里不能有名字撞上 TestCase 自身的方法。

    我把一个辅助方法命名成 run，结果它覆盖了 TestCase.run，
    整个 runner 直接崩掉，而且 traceback 全指向 unittest 内部，
    完全看不出是自己的问题。
    """

    RESERVED = {
        "run", "setUp", "tearDown", "setUpClass", "tearDownClass",
        "setUpModule", "tearDownModule", "skipTest", "subTest", "addCleanup",
        "assertEqual", "assertTrue", "assertFalse", "assertIn", "assertRaises",
    }

    def test_no_helper_shadows_testcase_methods(self):
        import tests.test_picker as module

        offenders = []
        for name in dir(module):
            obj = getattr(module, name)
            if not (isinstance(obj, type) and issubclass(obj, unittest.TestCase)):
                continue
            for attr in vars(obj):
                if attr in self.RESERVED:
                    offenders.append(f"{name}.{attr}")
        self.assertEqual(offenders, [], f"这些方法名会覆盖 TestCase 自身：{offenders}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
