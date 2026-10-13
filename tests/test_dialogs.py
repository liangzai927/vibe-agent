"""系统原生选择框。

**用户报过**：Windows 上选文件夹，报

    CommandNotFoundException
    + FullyQualifiedErrorId : CommandNotFoundException

根因是把提示语当成**命令行参数**传了：

    [shell, "-NoProfile", "-STA", "-Command", _WINDOWS_SCRIPT, prompt]

而 **`-Command` 会把后面所有东西都当成脚本体**——于是「选择文件夹」
被 PowerShell 当成一条命令去执行，当然找不到。`$args[0]` 也是空的。

改成走环境变量：没有引号、没有参数切分。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import va.dialogs as dialogs  # noqa: E402
from va.dialogs import CANCELLED  # noqa: E402


class FakeResult:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def capture(platform: str, shell_path: str, *, returncode: int = 0, stdout: str = "C:\\out"):
    """按指定平台跑一次 `pick_folder`，把传出去的东西抓下来。"""
    seen: dict = {}

    def fake_run(command, **kwargs):
        seen["argv"] = list(command)
        seen["env"] = dict(kwargs.get("env") or {})
        return FakeResult(returncode, stdout, "" if returncode == 0 else "炸了")

    def fake_which(name):
        # 各平台找的工具不一样：Windows 找 powershell/pwsh，
        # macOS 找 osascript，Linux 找 zenity/kdialog
        if name == Path(shell_path).stem.split(".")[0].lower():
            return shell_path
        if name == "osascript":
            return shell_path
        return None

    with mock.patch.object(dialogs, "_run", fake_run), mock.patch.object(
        dialogs.shutil, "which", fake_which
    ), mock.patch("platform.system", lambda: platform):
        seen["result"] = dialogs.pick_folder("选择文件夹")
    return seen


class TestWindowsPicker(unittest.TestCase):
    POWERSHELL = "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    PWSH = "C:/Program Files/PowerShell/7/pwsh.exe"

    def test_the_prompt_is_not_a_command_line_argument(self):
        """**这就是那个 bug。** `-Command` 吃掉后面所有东西，
        提示语会被当成命令去执行 → CommandNotFoundException。"""
        seen = capture("Windows", self.POWERSHELL)
        self.assertNotIn(
            "选择文件夹", seen["argv"], "提示语又被当成命令传进去了"
        )

    def test_the_prompt_travels_in_the_environment(self):
        seen = capture("Windows", self.POWERSHELL)
        self.assertEqual(seen["env"].get("VA_PICK_PROMPT"), "选择文件夹")

    def test_the_script_is_the_last_argument(self):
        """脚本后面不能再跟任何东西——跟了就会被当成脚本体的一部分。"""
        seen = capture("Windows", self.POWERSHELL)
        self.assertIn("-Command", seen["argv"])
        self.assertEqual(
            seen["argv"].index("-Command"), len(seen["argv"]) - 2,
            "`-Command` 后面应该只有脚本本身",
        )

    def test_sta_is_passed_to_windows_powershell(self):
        """WinForms 要求单线程单元，5.1 必须显式给 `-STA`。"""
        seen = capture("Windows", self.POWERSHELL)
        self.assertIn("-STA", seen["argv"])

    def test_sta_is_not_passed_to_pwsh(self):
        """PowerShell 7 默认就是 STA，而且不认识这个开关——传了它也会报错。"""
        seen = capture("Windows", self.PWSH)
        self.assertNotIn("-STA", seen["argv"])

    def test_the_script_loads_winforms(self):
        """不 `Add-Type` 的话 `System.Windows.Forms` 类型根本不存在。"""
        seen = capture("Windows", self.POWERSHELL)
        script = seen["argv"][-1]
        self.assertIn("Add-Type -AssemblyName System.Windows.Forms", script)
        self.assertIn("FolderBrowserDialog", script)

    def test_a_selected_path_comes_back(self):
        seen = capture("Windows", self.POWERSHELL, stdout="D:\\项目")
        self.assertTrue(seen["result"].ok)
        self.assertEqual(seen["result"].path, "D:\\项目")

    def test_cancel_is_recognised(self):
        seen = capture("Windows", self.POWERSHELL, stdout=CANCELLED)
        self.assertTrue(seen["result"].cancelled)

    def test_no_powershell_says_so(self):
        with mock.patch.object(dialogs.shutil, "which", lambda name: None), mock.patch(
            "platform.system", lambda: "Windows"
        ):
            result = dialogs.pick_folder("x")
        self.assertFalse(result.ok)
        self.assertIn("PowerShell", result.error)


class TestMacPicker(unittest.TestCase):
    def test_the_prompt_is_passed_as_an_argument(self):
        """macOS 这条是 `osascript -` 从 stdin 读脚本，参数是分开的——
        **和 Windows 的语义不一样**，不能照搬。"""
        seen = capture("Darwin", "/usr/bin/osascript", stdout="/Users/me/proj")
        # macOS 用的是字面量 "osascript"（走 PATH 解析），
        # 不是 which() 拿到的完整路径
        self.assertEqual(seen["argv"][0], "osascript")
        self.assertIn("选择文件夹", seen["argv"])
        self.assertNotIn("选择文件夹", seen["env"].values(),
                         "macOS 走参数，不该混用环境变量")


class TestOtherPlatforms(unittest.TestCase):
    def test_says_what_is_missing(self):
        with mock.patch.object(dialogs.shutil, "which", lambda name: None), mock.patch(
            "platform.system", lambda: "Linux"
        ):
            result = dialogs.pick_folder("x")
        self.assertFalse(result.ok)
        self.assertIn("zenity", result.error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
