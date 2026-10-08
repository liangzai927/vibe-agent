"""环境自检与浏览器探测的测试。

这两个模块的价值全在「报错好不好读」上——容器里用户看不到 traceback 的上下文，
所以提示必须自解释。测试也主要盯着这一点。
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402
from va import browser as browser_mod  # noqa: E402
from va import doctor  # noqa: E402


class TestReport(unittest.TestCase):
    def test_counts_by_level_and_status(self):
        report = doctor.Report()
        report.add(doctor.Result("a", doctor.OK, level=doctor.REQUIRED))
        report.add(doctor.Result("b", doctor.FAIL, level=doctor.REQUIRED))
        report.add(doctor.Result("c", doctor.SKIP, level=doctor.OPTIONAL))
        report.add(doctor.Result("d", doctor.OK, level=doctor.OPTIONAL))

        self.assertEqual(report.count(doctor.REQUIRED), 2)
        self.assertEqual(report.count(doctor.REQUIRED, doctor.OK), 1)
        self.assertEqual(report.count(doctor.OPTIONAL), 2)
        self.assertEqual(len(report.failed_required), 1)

    def test_optional_failure_is_not_blocking(self):
        report = doctor.Report()
        report.add(doctor.Result("浏览器", doctor.FAIL, level=doctor.OPTIONAL))
        self.assertEqual(report.failed_required, [])
        self.assertTrue(report.to_dict()["ok"])

    def test_required_failure_blocks(self):
        report = doctor.Report()
        report.add(doctor.Result("git", doctor.FAIL, level=doctor.REQUIRED))
        self.assertEqual(len(report.failed_required), 1)
        self.assertFalse(report.to_dict()["ok"])

    def test_to_dict_is_json_serialisable(self):
        report = doctor.Report()
        report.add(doctor.Result("x", doctor.OK, detail="d", hint="h"))
        text = json.dumps(report.to_dict(), ensure_ascii=False)
        self.assertIn('"name": "x"', text)


class TestWritableHelper(unittest.TestCase):
    def test_unwritable_path_reports_oserror(self):
        # /dev/null 是文件不是目录，在它下面建目录必然失败
        ok, error = doctor._writable(Path("/dev/null/nope"))
        self.assertFalse(ok)
        self.assertTrue(error)


class TestChecks(AgentTestCase):
    def test_python_ok(self):
        result = doctor.check_python()
        self.assertEqual(result.status, doctor.OK)
        self.assertEqual(result.level, doctor.REQUIRED)

    def test_workspace_ok(self):
        result = doctor.check_workspace(self.workspace)
        self.assertEqual(result.status, doctor.OK)

    def test_session_dir_ok(self):
        result = doctor.check_session_dir(self.cfg)
        self.assertEqual(result.status, doctor.OK)
        self.assertIn(".va", result.detail)

    def test_git_present_on_this_machine(self):
        result = doctor.check_git()
        self.assertIn(result.status, (doctor.OK, doctor.FAIL))

    def test_git_identity_when_missing_gives_config_command(self):
        with mock.patch.object(doctor, "_shell", return_value=(1, "")):
            result = doctor.check_git_identity()
        self.assertEqual(result.status, doctor.FAIL)
        self.assertIn("git config --global user.name", result.hint)

    def test_git_identity_when_present(self):
        with mock.patch.object(doctor, "_shell", side_effect=[(0, "张三"), (0, "z@example.com")]):
            result = doctor.check_git_identity()
        self.assertEqual(result.status, doctor.OK)
        self.assertIn("张三", result.detail)

    def test_api_without_key_is_skipped_not_failed(self):
        self.cfg.api_key = ""
        result = doctor.check_api(self.cfg)
        self.assertEqual(result.status, doctor.SKIP)
        self.assertIn("VA_API_KEY", result.hint)

    def test_vision_not_configured_mentions_env_var(self):
        result = doctor.check_vision(self.cfg)
        self.assertEqual(result.status, doctor.SKIP)
        self.assertIn("VA_VISION_MODEL", result.hint)

    def test_vision_configured(self):
        self.cfg.vision_model = "gpt-4o"
        self.cfg.vision_base_url = "https://api.openai.com/v1"
        result = doctor.check_vision(self.cfg)
        self.assertEqual(result.status, doctor.OK)
        self.assertIn("gpt-4o", result.detail)

    def test_playwright_check_never_raises(self):
        result = doctor.check_playwright()
        self.assertIn(result.status, (doctor.OK, doctor.SKIP))

    def test_pillow_check_never_raises(self):
        result = doctor.check_pillow()
        self.assertIn(result.status, (doctor.OK, doctor.SKIP))

    def test_ripgrep_check_never_raises(self):
        result = doctor.check_ripgrep()
        self.assertIn(result.status, (doctor.OK, doctor.SKIP))

    def test_node_check_never_raises(self):
        result = doctor.check_node()
        self.assertIn(result.status, (doctor.OK, doctor.SKIP))


class TestDoctorRun(AgentTestCase):
    def test_offline_run_produces_report_and_exit_code(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = doctor.run(self.cfg, self.workspace, offline=True, quick=True)

        output = buffer.getvalue()
        self.assertIn("环境自检", output)
        self.assertIn("必需", output)
        self.assertIn(code, (0, 1))

    def test_json_mode_is_machine_readable(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            doctor.run(self.cfg, self.workspace, offline=True, quick=True, as_json=True)

        payload = json.loads(buffer.getvalue())
        self.assertIn("ok", payload)
        names = [r["name"] for r in payload["results"]]
        self.assertIn("Python", names)
        self.assertIn("工作区", names)

    def test_blocking_failure_returns_nonzero(self):
        with mock.patch.object(doctor, "check_git", return_value=doctor.Result(
            "git", doctor.FAIL, "没有 git", "装一个", doctor.REQUIRED
        )):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = doctor.run(self.cfg, self.workspace, offline=True, quick=True)
        self.assertEqual(code, 1)
        self.assertIn("无法正常工作", buffer.getvalue())


class TestBrowserDetection(unittest.TestCase):
    def setUp(self) -> None:
        browser_mod.reset_cache()

    def tearDown(self) -> None:
        browser_mod.reset_cache()

    def test_label_for(self):
        self.assertEqual(browser_mod.label_for("channel", "chrome"), "系统 chrome")
        self.assertEqual(browser_mod.label_for("bundled", ""), "Playwright 自带 Chromium")
        self.assertEqual(browser_mod.label_for("executable", "/usr/bin/chromium"), "chromium")

    def test_explicit_channel_takes_priority(self):
        with mock.patch.dict(os.environ, {"VA_BROWSER_CHANNEL": "msedge"}, clear=False):
            candidates = browser_mod._candidates()
        self.assertEqual(candidates[0], ("channel", "msedge"))

    def test_explicit_executable_beats_channel(self):
        env = {"VA_BROWSER_EXECUTABLE": "/opt/chrome", "VA_BROWSER_CHANNEL": "chrome"}
        with mock.patch.dict(os.environ, env, clear=False):
            candidates = browser_mod._candidates()
        self.assertEqual(candidates[0], ("executable", "/opt/chrome"))
        self.assertEqual(candidates[1], ("channel", "chrome"))

    def test_bundled_is_the_last_resort(self):
        env = {"VA_BROWSER_CHANNEL": "", "VA_BROWSER_EXECUTABLE": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(browser_mod, "_system_browser", return_value=None):
                candidates = browser_mod._candidates()
        self.assertEqual(candidates[-1], ("bundled", ""))

    def test_candidates_are_deduplicated(self):
        with mock.patch.dict(os.environ, {"VA_BROWSER_CHANNEL": "chrome"}, clear=False):
            candidates = browser_mod._candidates()
        self.assertEqual(len(candidates), len(set(candidates)))

    @unittest.skipIf(browser_mod.playwright_installed(), "本机装了 playwright，跳过缺失路径测试")
    def test_missing_playwright_gives_install_hint(self):
        info = browser_mod.detect(force=True)
        self.assertFalse(info.available)
        self.assertIn("playwright", info.error)
        self.assertIn("pip install", info.hint)

    def test_detect_without_probe_reports_first_candidate(self):
        if not browser_mod.playwright_installed():
            self.skipTest("需要 playwright")
        info = browser_mod.detect(force=True, probe=False)
        self.assertTrue(info.available)
        self.assertTrue(info.label)

    def test_launch_raises_with_hint_when_unavailable(self):
        info = browser_mod.BrowserInfo(available=False, error="没有浏览器", hint="装一个")
        with self.assertRaises(browser_mod.BrowserUnavailable) as ctx:
            browser_mod.launch(None, info=info)
        self.assertEqual(ctx.exception.hint, "装一个")

    def test_error_message_includes_real_reason_not_just_class_name(self):
        """Playwright 的异常类名就叫 Error，只报类名等于没报。"""
        self.assertEqual(browser_mod._short_error(ValueError("具体原因在这里")), "具体原因在这里")
        self.assertEqual(browser_mod._short_error(ValueError()), "ValueError")


class TestImagingHelpers(unittest.TestCase):
    def test_guess_mime(self):
        from va import imaging

        self.assertEqual(imaging.guess_mime(Path("a.png")), "image/png")
        self.assertEqual(imaging.guess_mime(Path("a.jpg")), "image/jpeg")
        self.assertEqual(imaging.guess_mime(Path("a.webp")), "image/webp")
        self.assertEqual(imaging.guess_mime(Path("a.unknown")), "image/png")

    def test_missing_file_raises_filenotfound(self):
        from va import imaging

        with self.assertRaises(FileNotFoundError):
            imaging.image_data_url(Path("/nonexistent/a.png"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
