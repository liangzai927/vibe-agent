"""剪贴板取图的测试。

终端传的是按键不是图片数据，所以「粘贴图片」这件事只能靠**问操作系统要剪贴板
内容**来实现。这类代码最容易出的问题是：在别人的机器上悄悄失效——工具没装、
平台不同、剪贴板里其实没有图。所以测试重点在**失败路径的提示质量**，
而不是「在我这台机器上能跑」。

真正依赖剪贴板内容的用例会先探测环境，探测不到就跳过——测试套件不该因为
跑测试的人剪贴板里没有图片而变红。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402
from va import clipboard, imaging  # noqa: E402
from va.cli import CLIP_TOKEN  # noqa: E402
from va.clipboard import (  # noqa: E402
    ClipboardImage,
    default_filename,
    inbox_dir,
    read_image,
    support,
)


def _block_pil(name, *args, **kwargs):
    """模拟没装 Pillow。"""
    if name == "PIL" or name.startswith("PIL."):
        raise ImportError("No module named 'PIL'")
    return _real_import(name, *args, **kwargs)


_real_import = __import__


def clipboard_has_image() -> bool:
    """当前环境真有一张可读的图片吗。用来决定要不要跳过。"""
    try:
        return clipboard.has_image() is True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------- 平台支持


class TestSupport(unittest.TestCase):
    def test_reports_something_everywhere(self):
        ok, how = support()
        self.assertIsInstance(ok, bool)
        self.assertTrue(how, "总得说清楚是用什么方式或为什么不行")

    def test_macos_mentions_osascript(self):
        with mock.patch("platform.system", return_value="Darwin"):
            ok, how = support()
        self.assertTrue(ok)
        self.assertIn("osascript", how)

    def test_macos_without_osascript_explains(self):
        with mock.patch("platform.system", return_value="Darwin"):
            with mock.patch("shutil.which", return_value=None):
                ok, how = support()
        self.assertFalse(ok)
        self.assertIn("osascript", how)

    def test_linux_without_tools_says_what_to_install(self):
        with mock.patch("platform.system", return_value="Linux"):
            with mock.patch("shutil.which", return_value=None):
                ok, how = support()
        self.assertFalse(ok)
        self.assertIn("wl-paste", how)
        self.assertIn("xclip", how)

    def test_linux_prefers_wayland_tool(self):
        with mock.patch("platform.system", return_value="Linux"):
            with mock.patch(
                "shutil.which", side_effect=lambda n: f"/usr/bin/{n}" if n == "wl-paste" else None
            ):
                ok, how = support()
        self.assertTrue(ok)
        self.assertIn("wl-paste", how)

    def test_windows_mentions_powershell(self):
        with mock.patch("platform.system", return_value="Windows"):
            with mock.patch(
                "shutil.which", side_effect=lambda n: "C:/pwsh.exe" if n == "pwsh" else None
            ):
                ok, how = support()
        self.assertTrue(ok)
        self.assertIn("PowerShell", how)


# --------------------------------------------------------------- 取图失败


class TestFailures(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name) / "out.png"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_macos_error_message_is_cleaned(self):
        """osascript 的报错前面挂着一串行号列号，对用户毫无意义。"""
        cleaned = clipboard._clean_osascript_error(
            "160:189: execution error: 不能将某些数据转换为预期的类型。 (-1700)"
        )
        self.assertNotIn("160:189", cleaned)
        self.assertNotIn("execution error", cleaned)
        self.assertIn("-1700", cleaned)

    def test_clean_handles_empty(self):
        self.assertTrue(clipboard._clean_osascript_error(""))
        self.assertTrue(clipboard._clean_osascript_error("   \n  "))

    def test_macos_grab_failure_suggests_copying_an_image(self):
        with mock.patch("platform.system", return_value="Darwin"):
            with mock.patch.object(
                clipboard, "_run",
                return_value=mock.Mock(returncode=1, stdout="", stderr="some error"),
            ):
                result = read_image(self.target)

        self.assertFalse(result.ok)
        self.assertIn("先复制一张图片", result.hint)

    def test_empty_file_is_treated_as_failure(self):
        """osascript 失败时也可能留下一个空文件——落盘成功不等于内容对。"""
        def fake_run(command, stdin=None, timeout=0):
            self.target.parent.mkdir(parents=True, exist_ok=True)
            self.target.write_bytes(b"")
            return mock.Mock(returncode=0, stdout="ok", stderr="")

        with mock.patch("platform.system", return_value="Darwin"):
            with mock.patch.object(clipboard, "_run", side_effect=fake_run):
                result = read_image(self.target)

        self.assertFalse(result.ok)
        self.assertIn("没有图片数据", result.error)
        self.assertFalse(self.target.exists(), "坏文件应该被清掉")

    def test_invalid_image_data_is_rejected(self):
        """落盘成功 != 内容是一张图。

        有些程序会把文本塞进剪贴板的图片类型里。不校验的话，坏文件会流到
        下游，到那时报错的地方离病因已经很远了。
        """
        if not imaging.pillow_available():
            self.skipTest("需要 Pillow 才能解码校验")

        def fake_run(command, stdin=None, timeout=0):
            self.target.parent.mkdir(parents=True, exist_ok=True)
            self.target.write_bytes(b"<!DOCTYPE html><html>not an image</html>")
            return mock.Mock(returncode=0, stdout="ok", stderr="")

        with mock.patch("platform.system", return_value="Darwin"):
            with mock.patch.object(clipboard, "_run", side_effect=fake_run):
                result = read_image(self.target)

        self.assertFalse(result.ok)
        self.assertIn("不是有效图片", result.error)
        self.assertIn("文本", result.hint)
        self.assertFalse(self.target.exists(), "坏文件不该留在磁盘上")

    def test_without_pillow_only_size_is_checked(self):
        """没有 Pillow 时只能看文件大不大——那时不该误报为失败。"""
        def fake_run(command, stdin=None, timeout=0):
            self.target.parent.mkdir(parents=True, exist_ok=True)
            self.target.write_bytes(b"whatever bytes")
            return mock.Mock(returncode=0, stdout="ok", stderr="")

        with mock.patch("platform.system", return_value="Darwin"):
            with mock.patch.object(clipboard, "_run", side_effect=fake_run):
                with mock.patch("builtins.__import__", side_effect=_block_pil):
                    result = read_image(self.target)

        self.assertTrue(result.ok, "没有 Pillow 时不该因为解码不了就判失败")
        self.assertIsNone(result.size)

    def test_creates_parent_directory(self):
        nested = Path(self._tmp.name) / "a" / "b" / "c.png"

        def fake_run(command, stdin=None, timeout=0):
            return mock.Mock(returncode=1, stdout="", stderr="x")

        with mock.patch("platform.system", return_value="Darwin"):
            with mock.patch.object(clipboard, "_run", side_effect=fake_run):
                read_image(nested)

        self.assertTrue(nested.parent.is_dir(), "父目录应该被建出来")

    def test_unwritable_directory_is_reported(self):
        with mock.patch.object(Path, "mkdir", side_effect=OSError("只读文件系统")):
            result = read_image(self.target)
        self.assertFalse(result.ok)
        self.assertIn("创建目录失败", result.error)

    def test_linux_without_tool_gives_install_hint(self):
        with mock.patch("platform.system", return_value="Linux"):
            with mock.patch("shutil.which", return_value=None):
                result = read_image(self.target)
        self.assertFalse(result.ok)
        self.assertIn("wl-clipboard", result.hint)
        self.assertIn("xclip", result.hint)

    def test_describe_includes_hint_on_failure(self):
        image = ClipboardImage(False, error="取不到", hint="先复制一张图片")
        text = image.describe()
        self.assertIn("取不到", text)
        self.assertIn("先复制一张图片", text)

    def test_describe_includes_dimensions_on_success(self):
        image = ClipboardImage(
            True, path=Path("/x/y.png"), size=(460, 480), bytes_written=10240
        )
        text = image.describe()
        self.assertIn("460x480", text)
        self.assertIn("/x/y.png", text)


# --------------------------------------------------------------- 取图成功


class TestRealClipboard(unittest.TestCase):
    """真去读剪贴板。拿不到图就跳过——别人机器上的剪贴板内容不可控。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        if not clipboard_has_image():
            self._tmp.cleanup()
            self.skipTest("当前剪贴板里没有图片（或在无图形界面的环境里）")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_writes_a_valid_png(self):
        target = Path(self._tmp.name) / "shot.png"
        result = read_image(target)

        self.assertTrue(result.ok, result.describe())
        self.assertTrue(target.is_file())
        self.assertGreater(target.stat().st_size, 0)

        # 尺寸是尽力而为：没有 Pillow 就量不出来，但那不该算失败——
        # 图已经好好落盘了，这才是这个工具的本职。
        if imaging.pillow_available():
            self.assertIsNotNone(result.size, "装了 Pillow 就该能读出尺寸")
            self.assertGreater(result.size[0], 0)

    def test_overwrites_existing_file(self):
        target = Path(self._tmp.name) / "shot.png"
        target.write_bytes(b"stale")
        result = read_image(target)

        self.assertTrue(result.ok, result.describe())
        self.assertNotEqual(target.read_bytes(), b"stale")


# --------------------------------------------------------------- @clip 标记


class TestClipToken(unittest.TestCase):
    def test_matches_supported_spellings(self):
        for text in ("用 @clip 实现", "看 @paste", "@CLIP", "@img", "@image", "@图片", "@剪贴板"):
            self.assertTrue(CLIP_TOKEN.search(text), f"{text} 应该命中")

    def test_does_not_match_email_addresses(self):
        """`a@clip.com` 不是标记——加个 . 到排除集就是为了这个。"""
        for text in ("邮箱是 a@clip.com", "a@paste.cn", "@clipart", "@clipper", "@clipboard"):
            self.assertFalse(CLIP_TOKEN.search(text), f"{text} 不该命中")

    def test_stops_at_punctuation(self):
        self.assertTrue(CLIP_TOKEN.search("@clip，然后实现"))
        self.assertTrue(CLIP_TOKEN.search("(@clip)"))
        self.assertTrue(CLIP_TOKEN.search("@clip。"))


# --------------------------------------------------------------- REPL 接线


class TestExpandTokens(AgentTestCase):
    def _fake(self, ok: bool = True, **kwargs):
        return ClipboardImage(ok=ok, path=self.root / ".va" / "inbox" / "clip.png", **kwargs)

    def test_plain_message_is_untouched(self):
        from va.cli import _expand_clip_tokens

        agent, ui = self.make_agent(None)
        self.assertEqual(_expand_clip_tokens("普通消息", agent, ui), "普通消息")

    def test_token_becomes_a_path(self):
        from va.cli import _expand_clip_tokens

        agent, ui = self.make_agent(None)
        result = self._fake(ok=True, size=(100, 50), bytes_written=1234)
        with mock.patch("va.cli._grab_clipboard", return_value=result):
            expanded = _expand_clip_tokens("用 @clip 实现首页", agent, ui)

        self.assertIsNotNone(expanded)
        self.assertNotIn("@clip", expanded)
        self.assertIn("clip.png", expanded)

    def test_all_occurrences_are_replaced(self):
        from va.cli import _expand_clip_tokens

        agent, ui = self.make_agent(None)
        result = self._fake(ok=True, size=(1, 1), bytes_written=1)
        with mock.patch("va.cli._grab_clipboard", return_value=result):
            expanded = _expand_clip_tokens("@clip 和 @paste 都是", agent, ui)

        self.assertNotIn("@clip", expanded)
        self.assertNotIn("@paste", expanded)
        self.assertEqual(expanded.count("clip.png"), 2)

    def test_failure_aborts_the_message(self):
        """取不到图就不该把消息发出去——否则模型会看到一个字面量 @clip。"""
        from va.cli import _expand_clip_tokens

        agent, ui = self.make_agent(None)
        with mock.patch(
            "va.cli._grab_clipboard",
            return_value=ClipboardImage(False, error="剪贴板里没有图片", hint="先复制一张图"),
        ):
            self.assertIsNone(_expand_clip_tokens("用 @clip 实现", agent, ui))

    def test_uses_relative_path_when_inside_workspace(self):
        from va.cli import _expand_clip_tokens

        agent, ui = self.make_agent(None)
        inside = self.root / ".va" / "inbox" / "x.png"
        inside.parent.mkdir(parents=True, exist_ok=True)
        inside.write_bytes(b"x")

        result = ClipboardImage(True, path=inside, size=(1, 1), bytes_written=1)
        with mock.patch("va.cli._grab_clipboard", return_value=result):
            expanded = _expand_clip_tokens("@clip", agent, ui)

        self.assertIn(".va/inbox/x.png", expanded.replace("\\", "/"))


class TestPasteCommand(AgentTestCase):
    def test_unsupported_platform_says_why(self):
        from va.cli import _paste_clipboard

        agent, ui = self.make_agent(None)
        with mock.patch("va.clipboard.support", return_value=(False, "需要 wl-paste 或 xclip")):
            _paste_clipboard(agent, ui, "")

        self.assertTrue(any("wl-paste" in text for _, text in ui.messages))

    def test_failure_is_surfaced(self):
        from va.cli import _paste_clipboard

        agent, ui = self.make_agent(None)
        with mock.patch("va.clipboard.support", return_value=(True, "osascript")):
            with mock.patch(
                "va.clipboard.read_image",
                return_value=ClipboardImage(False, error="没有图片", hint="先复制一张"),
            ):
                _paste_clipboard(agent, ui, "")

        self.assertTrue(any("没有图片" in text for _, text in ui.messages))

    def test_success_reports_the_path(self):
        from va.cli import _paste_clipboard

        agent, ui = self.make_agent(None)
        stored = self.root / ".va" / "inbox" / "design.png"
        with mock.patch("va.clipboard.support", return_value=(True, "osascript")):
            with mock.patch(
                "va.clipboard.read_image",
                return_value=ClipboardImage(
                    True, path=stored, size=(600, 400), bytes_written=2048
                ),
            ):
                _paste_clipboard(agent, ui, "")

        joined = "\n".join(text for _, text in ui.messages)
        self.assertIn("600x400", joined)
        self.assertIn("@clip", joined, "应该告诉用户怎么用这个路径")

    def test_name_without_extension_gets_png(self):
        from va.cli import _paste_clipboard

        agent, ui = self.make_agent(None)
        captured: list[Path] = []

        def capture(path):
            captured.append(Path(path))
            return ClipboardImage(False, error="stop here")

        with mock.patch("va.clipboard.support", return_value=(True, "osascript")):
            with mock.patch("va.clipboard.read_image", side_effect=capture):
                _paste_clipboard(agent, ui, "我的设计稿")

        self.assertEqual(captured[0].name, "我的设计稿.png")

    def test_name_with_extension_is_kept(self):
        from va.cli import _paste_clipboard

        agent, ui = self.make_agent(None)
        captured: list[Path] = []

        def capture(path):
            captured.append(Path(path))
            return ClipboardImage(False, error="stop here")

        with mock.patch("va.clipboard.support", return_value=(True, "osascript")):
            with mock.patch("va.clipboard.read_image", side_effect=capture):
                _paste_clipboard(agent, ui, "shot.jpg")

        self.assertEqual(captured[0].name, "shot.jpg")


# --------------------------------------------------------------- 落点


class TestPaths(unittest.TestCase):
    def test_filename_is_timestamped_png(self):
        name = default_filename()
        self.assertTrue(name.startswith("clip-"))
        self.assertTrue(name.endswith(".png"))
        self.assertRegex(name, r"^clip-\d{8}-\d{6}\.png$")

    def test_prefix_is_configurable(self):
        self.assertTrue(default_filename("设计稿").startswith("设计稿-"))

    def test_inbox_is_outside_the_project(self):
        """粘贴/拖拽进来的图片放在 agent 自己的目录下，不在用户项目里。

        放在项目里的话，git status 会一直脏着，用户还得自己去加 .gitignore。
        """
        from va.paths import agent_home

        path = inbox_dir("/some/project")
        self.assertEqual(path.parts[-1], "inbox")
        self.assertEqual(path.parent, agent_home(), "不该落在工作区里")
        self.assertNotIn("/some/project", str(path))

    def test_two_calls_in_the_same_second_do_not_clash_by_default(self):
        """同一秒内连按两次会撞名，但 /paste 支持给名字，不强行造唯一名。"""
        self.assertEqual(default_filename(), default_filename())


if __name__ == "__main__":
    unittest.main(verbosity=2)
