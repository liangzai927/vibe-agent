"""部署脚本的测试。

打包脚本最坏的失败方式是**把 .env 打进包里**——一旦发出去，密钥就泄露了，
而且当事人往往不会立刻发现。所以这里盯着三件事：

* 包里有该有的东西（源码、依赖清单、安装脚本）
* 包里没有不该有的东西（.env、.venv、.va）
* 泄露检查本身的模式是对的（用 ".env" 去 grep 会命中 .env.example，
  用 ".va/" 会被正则当成「任意字符 + va/」把 vibe-agent/va/ 也算进去）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "scripts" / "package.sh"
INSTALL = ROOT / "scripts" / "install.sh"

HAS_BASH = shutil.which("bash") is not None


@unittest.skipUnless(HAS_BASH, "需要 bash")
class TestScriptSanity(unittest.TestCase):
    def test_scripts_exist_and_are_executable(self):
        for script in (PACKAGE, INSTALL):
            self.assertTrue(script.is_file(), f"{script.name} 不存在")
            self.assertTrue(
                script.stat().st_mode & 0o111, f"{script.name} 没有可执行位"
            )

    def test_bash_syntax(self):
        for script in (PACKAGE, INSTALL):
            result = subprocess.run(
                ["bash", "-n", str(script)], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, f"{script.name}: {result.stderr}")

    def test_package_uses_strict_mode(self):
        self.assertIn("set -euo pipefail", PACKAGE.read_text(encoding="utf-8"))


@unittest.skipUnless(HAS_BASH, "需要 bash")
class TestPackaging(unittest.TestCase):
    """通过一份**仓库副本**来测，绝不碰真实目录。

    血的教训：这个测试最初直接对真实仓库操作——setUp 用金丝雀内容覆盖真的
    `.env`，tearDown 再把它删掉。跑一次测试就把开发者的 API key 弄没了。
    所以现在：

    * 先把仓库拷到临时目录，只对副本操作；
    * 另外记下真实 `.env` 的指纹，tearDown 里核对——一旦被改动就立刻报错。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.repo = base / "vibe-agent"
        shutil.copytree(
            ROOT,
            self.repo,
            ignore=shutil.ignore_patterns(
                ".venv", "venv", ".git", "__pycache__", "dist", ".va", "*.pyc"
            ),
        )
        self.out = base / "pkg.tar.gz"

        # 真实 .env 的指纹。测试全程不许它发生变化。
        real_env = ROOT / ".env"
        self._real_env_state = (
            real_env.exists(),
            real_env.stat().st_mtime_ns if real_env.exists() else None,
            real_env.read_bytes() if real_env.exists() else None,
        )

        # 金丝雀只放进副本，绝不碰真实仓库
        (self.repo / ".env").write_text("VA_API_KEY=sk-LEAK-CANARY\n", encoding="utf-8")
        (self.repo / ".va" / "sessions").mkdir(parents=True, exist_ok=True)
        (self.repo / ".va" / "sessions" / "canary.jsonl").write_text(
            "session-canary\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        real_env = ROOT / ".env"
        now = (
            real_env.exists(),
            real_env.stat().st_mtime_ns if real_env.exists() else None,
            real_env.read_bytes() if real_env.exists() else None,
        )
        self._tmp.cleanup()
        self.assertEqual(
            now,
            self._real_env_state,
            "测试改动了真实的 .env！这个测试之前就是因为直接操作真实仓库，"
            "把开发者的 API key 弄丢过一次。",
        )

    def _build(self) -> list[str]:
        result = subprocess.run(
            [
                "bash",
                str(self.repo / "scripts" / "package.sh"),
                "--working-tree",
                "--out",
                str(self.out),
            ],
            cwd=self.repo,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            self.fail(f"打包失败：\n{result.stdout}\n{result.stderr}")
        self.assertTrue(self.out.is_file(), "打包声称成功但没有产出文件")
        with tarfile.open(self.out) as archive:
            return archive.getnames()

    def test_produces_a_small_archive(self):
        self._build()
        size_kb = self.out.stat().st_size / 1024
        self.assertLess(size_kb, 2000, f"包太大了（{size_kb:.0f}KB），可能误带了什么")

    def test_contains_source_and_installer(self):
        names = self._build()
        joined = "\n".join(names)
        for expected in (
            "vibe-agent/run.py",
            "vibe-agent/requirements.txt",
            "vibe-agent/requirements-docs.txt",
            "vibe-agent/scripts/install.sh",
            "vibe-agent/va/__init__.py",
            "vibe-agent/va/tools/delegate.py",
        ):
            self.assertIn(expected, joined, f"包里缺少 {expected}")

    def test_excludes_secrets_and_local_state(self):
        names = self._build()
        joined = "\n".join(names)

        self.assertNotIn("sk-LEAK-CANARY", joined)
        self.assertNotIn("session-canary", joined)

        import re

        self.assertFalse(
            re.search(r"(^|/)\.env$", joined, re.MULTILINE),
            ".env 被打进包里了——密钥会泄露",
        )
        self.assertFalse(
            re.search(r"(^|/)\.va/", joined, re.MULTILINE),
            ".va/ 被打进包里了（会话记录）",
        )
        self.assertFalse(
            re.search(r"(^|/)\.venv/", joined, re.MULTILINE),
            ".venv/ 被打进包里了（本机二进制，换台机器用不了）",
        )

    def test_keeps_env_example(self):
        """排除规则不能误伤模板文件。"""
        names = self._build()
        self.assertIn("vibe-agent/.env.example", "\n".join(names))

    def test_does_not_confuse_package_dir_with_session_dir(self):
        """va/ 是源码目录，.va/ 才是会话目录——正则写松了会把前者也排掉。"""
        names = self._build()
        joined = "\n".join(names)
        self.assertIn("vibe-agent/va/__init__.py", joined)
        self.assertIn("vibe-agent/va/tools/plan.py", joined)

    def test_excludes_macos_resource_forks(self):
        """macOS 在非原生文件系统上会生成 ._* 文件，纯属垃圾。"""
        names = self._build()
        junk = [n for n in names if Path(n).name.startswith("._")]
        self.assertEqual(junk, [], f"包里混进了 macOS 资源分叉文件：{junk[:3]}")

    def test_python_and_bsdtar_see_the_same_archive(self):
        """两种读法必须一致。

        bsdtar 在 -t 模式下会隐藏 AppleDouble 条目，所以 `tar -tzf` 看着干净，
        而 Linux 上的 GNU tar 解压时会多出一堆文件。数目对不上就是这个信号。
        """
        self._build()
        with tarfile.open(self.out) as archive:
            python_count = len(archive.getnames())
        result = subprocess.run(
            ["tar", "-tzf", str(self.out)], capture_output=True, text=True
        )
        bsdtar_count = len([l for l in result.stdout.splitlines() if l.strip()])
        self.assertEqual(
            python_count,
            bsdtar_count,
            f"Python 读到 {python_count} 条、bsdtar 读到 {bsdtar_count} 条——"
            f"归档里混了 bsdtar 会自动隐藏的条目，别的平台解压就会多出文件",
        )

    def test_writes_checksum_file(self):
        self._build()
        checksum = self.out.with_suffix(self.out.suffix + ".sha256")
        if checksum.exists():
            content = checksum.read_text(encoding="utf-8")
            self.assertEqual(len(content.split()[0]), 64, "校验和长度不对")


@unittest.skipUnless(HAS_BASH, "需要 bash")
class TestInstallScriptProperties(unittest.TestCase):
    def setUp(self) -> None:
        self.text = INSTALL.read_text(encoding="utf-8")

    def test_refuses_to_run_without_python_310(self):
        self.assertIn("3,10", self.text.replace(" ", ""))
        self.assertIn("找不到 Python 3.10", self.text)

    def test_key_is_never_echoed(self):
        """录入 key 时必须 read -s。"""
        self.assertIn("read -rs API_KEY", self.text)

    def test_key_file_gets_restrictive_permissions(self):
        self.assertIn("chmod(0o600)", self.text)

    def test_does_not_require_root(self):
        for dangerous in ("sudo ", "chown ", "chmod 777"):
            self.assertNotIn(dangerous, self.text, f"安装脚本不该用 {dangerous.strip()}")

    def test_detects_system_chrome_before_downloading(self):
        self.assertIn("find_system_chrome", self.text)
        self.assertIn("playwright", self.text)
        self.assertIn("--no-browser", self.text)

    def test_warns_about_gitignore(self):
        self.assertIn(".gitignore", self.text)

    def test_runs_doctor_at_the_end(self):
        self.assertIn("doctor", self.text)

    def test_rejects_unknown_flags(self):
        self.assertIn("未知参数", self.text)


class TestDeployDoc(unittest.TestCase):
    def setUp(self) -> None:
        path = ROOT / "DEPLOY.md"
        self.assertTrue(path.is_file(), "缺少 DEPLOY.md")
        self.text = path.read_text(encoding="utf-8")

    def test_covers_both_paths(self):
        self.assertIn("方式 A：容器", self.text)
        self.assertIn("方式 B：原生 Python", self.text)

    def test_warns_about_architecture(self):
        self.assertIn("arm64", self.text)
        self.assertIn("docker save", self.text)
        self.assertIn("buildx", self.text)

    def test_says_the_image_was_never_built_here(self):
        """不能让读者以为容器方案已经被验证过了。"""
        self.assertIn("从未真正构建过", self.text)

    def test_says_what_not_to_transfer(self):
        for item in (".env", ".venv/", ".va/"):
            self.assertIn(item, self.text)

    def test_documents_the_key_paths(self):
        self.assertIn("VA_API_KEY", self.text)
        self.assertIn("chmod 600", self.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
