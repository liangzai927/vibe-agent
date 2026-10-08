"""容器化配置的一致性测试。

这些文件没法在这个环境里真跑（本机没有 Docker），但**能验的部分必须验**——
绝大多数容器化事故来自不一致，而不是来自复杂逻辑：

    * Dockerfile 引用了不存在的文件
    * .dockerignore 忘了排除 .env，密钥被烤进镜像层
    * .dockerignore 忘了排除 .venv，宿主机二进制被带进 Linux 镜像
    * 代码读了某个 VA_* 环境变量，compose 却没透传
    * entrypoint 没有可执行位

上面每一条都能在本地断言出来，所以就不该留给用户去踩。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"
COMPOSE = ROOT / "docker-compose.yml"
DOCKERIGNORE = ROOT / ".dockerignore"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestDockerfile(unittest.TestCase):
    def setUp(self) -> None:
        self.text = read(DOCKERFILE)

    def test_referenced_files_exist(self):
        """COPY 的源文件必须真实存在，否则构建到一半才炸。"""
        for name in re.findall(r"^COPY\s+(?:--\S+\s+)*(.+)$", self.text, re.MULTILINE):
            for token in name.split():
                if token.startswith("--") or token.startswith("."):
                    continue
                source = ROOT / token
                self.assertTrue(
                    source.exists(),
                    f"Dockerfile 里 COPY 了不存在的 {token}",
                )

    def test_declares_both_dependency_files(self):
        self.assertIn("requirements.txt", self.text)
        self.assertIn("requirements-browser.txt", self.text)

    def test_args_used_after_from_are_declared_after_from(self):
        """FROM 之前声明的 ARG 在 FROM 之后不可见——这是最常见的构建失败原因之一。"""
        _, _, body = self.text.partition("\nFROM ")
        _, _, rest = body.partition("\n")
        after_from = rest
        declared = set(re.findall(r"^ARG\s+(\w+)", after_from, re.MULTILINE))
        used = set(re.findall(r"\$\{?(\w+)\}?", after_from))
        # 允许 FROM 之后的 ARG 引用自身
        suspicious = {"WITH_NODE", "WITH_BROWSER", "NODE_MAJOR", "DEBIAN_FRONTEND"} - declared
        self.assertFalse(
            suspicious,
            f"这些 ARG 在 FROM 之后被使用但没有在 FROM 之后声明：{suspicious}",
        )

    def test_entrypoint_is_wired_up(self):
        self.assertIn("/app/docker/entrypoint.sh", self.text)
        self.assertIn("ENTRYPOINT", self.text)
        self.assertIn("tini", self.text, "PID 1 需要 init 来收割僵尸进程并正确传递信号")

    def test_browser_cache_path_is_shared(self):
        """Chromium 装的位置必须让非 root 用户也能读到。"""
        self.assertIn("PLAYWRIGHT_BROWSERS_PATH", self.text)
        self.assertIn("chmod -R a+rX", self.text)

    def test_app_runs_as_non_root_after_entrypoint(self):
        self.assertIn("gosu", self.text + read(ENTRYPOINT))

    def test_copy_uses_chown_so_files_are_not_root_owned(self):
        self.assertIn("--chown=va:va", self.text)

    def test_no_secrets_baked_in(self):
        # 只看真正会执行的指令，注释里的 `-e VA_API_KEY=sk-xxx` 是用法示例
        code = "\n".join(
            line for line in self.text.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertNotIn("VA_API_KEY=", code)
        self.assertNotIn("sk-", code)
        self.assertNotRegex(code, r"(?i)password\s*=")


class TestEntrypoint(unittest.TestCase):
    def setUp(self) -> None:
        self.text = read(ENTRYPOINT)

    def test_is_executable(self):
        self.assertTrue(
            ENTRYPOINT.stat().st_mode & 0o111,
            "entrypoint.sh 没有可执行位，容器会以 126 退出",
        )

    def test_bash_syntax_is_valid(self):
        result = subprocess.run(
            ["bash", "-n", str(ENTRYPOINT)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, f"语法错误：{result.stderr}")

    def test_uses_strict_mode(self):
        self.assertIn("set -euo pipefail", self.text)

    def test_aligns_uid_and_gid(self):
        self.assertIn("VA_UID", self.text)
        self.assertIn("VA_GID", self.text)
        self.assertIn("usermod", self.text)
        self.assertIn("groupmod", self.text)

    def test_drops_privileges_with_gosu(self):
        self.assertIn("gosu", self.text)

    def test_supports_runtime_browser_install(self):
        self.assertIn("VA_INSTALL_BROWSER", self.text)
        self.assertIn("playwright install", self.text)

    def test_sets_git_safe_directory(self):
        """挂载进来的仓库属主通常和容器内用户不一致，不设 git 会拒绝干活。"""
        self.assertIn("safe.directory", self.text)

    def test_no_swallowed_failures_on_critical_steps(self):
        """关键步骤不能被 || true 吞掉，否则现象和真因对不上。"""
        for line in self.text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            if "pip install" in stripped or "playwright install" in stripped:
                self.assertNotIn("|| true", stripped, f"关键步骤被吞掉：{stripped}")


class TestDockerignore(unittest.TestCase):
    def setUp(self) -> None:
        self.text = read(DOCKERIGNORE)

    def _patterns(self) -> list[str]:
        return [
            line.strip()
            for line in self.text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def test_excludes_env_files(self):
        """密钥绝不能被烤进镜像层。"""
        patterns = self._patterns()
        self.assertIn(".env", patterns)
        self.assertIn("!.env.example", patterns, ".env.example 是模板，应该保留")

    def test_excludes_virtualenv(self):
        """宿主机 venv 里是 macOS/其他架构的二进制，带进 Linux 镜像只会坏事。"""
        self.assertIn(".venv/", self._patterns())

    def test_excludes_sessions_and_caches(self):
        patterns = self._patterns()
        self.assertIn(".va/", patterns)
        self.assertIn("__pycache__/", patterns)
        self.assertIn(".git", patterns)

    def test_keeps_tests_so_container_can_self_check(self):
        patterns = self._patterns()
        self.assertNotIn("tests/", patterns, "测试应该在镜像里，方便 docker 里自检")


class TestCompose(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import yaml
        except ImportError:
            self.skipTest("需要 pyyaml")
        self.data = yaml.safe_load(read(COMPOSE))
        self.service = self.data["services"]["va"]

    def test_workspace_is_mounted(self):
        volumes = self.service["volumes"]
        self.assertTrue(
            any("/workspace" in v for v in volumes),
            "工作区没挂载，容器里的 agent 看不到用户的代码",
        )

    def test_home_is_a_named_volume(self):
        volumes = self.service["volumes"]
        self.assertTrue(
            any("/home/va" in v for v in volumes),
            "家目录没持久化，每次重建都要重新配 git、重下缓存",
        )
        self.assertIn("va-home", self.data.get("volumes", {}))

    def test_uid_gid_are_passed_through(self):
        env = self.service["environment"]
        self.assertIn("VA_UID", env)
        self.assertIn("VA_GID", env)

    def test_git_identity_is_passed_through(self):
        env = self.service["environment"]
        self.assertIn("GIT_USER_NAME", env)
        self.assertIn("GIT_USER_EMAIL", env)

    def test_tty_settings_for_interactive_use(self):
        self.assertTrue(self.service.get("stdin_open"))
        self.assertTrue(self.service.get("tty"))

    def test_no_deprecated_version_key(self):
        self.assertNotIn("version", self.data, "compose v2 已弃用顶层 version 字段")


class TestEnvVarConsistency(unittest.TestCase):
    """代码读的环境变量和 compose 透传的变量必须对得上。

    这是最容易出错、也最难手工发现的一类不一致：代码加了新的 VA_* 开关，
    compose 忘了加，于是「本地能跑、容器里怎么都不生效」。
    """

    def setUp(self) -> None:
        try:
            import yaml
        except ImportError:
            self.skipTest("需要 pyyaml")
        self.compose_env = set(yaml.safe_load(read(COMPOSE))["services"]["va"]["environment"])

    @staticmethod
    def _env_vars_read_by_code() -> set[str]:
        """环境变量不只在 Python 里读——entrypoint.sh 也读了一堆。"""
        found: set[str] = set()

        for path in (ROOT / "va").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            found |= set(re.findall(r'os\.environ\.get\(\s*["\']([A-Z_][A-Z0-9_]*)["\']', text))
            found |= set(re.findall(r'os\.environ\[\s*["\']([A-Z_][A-Z0-9_]*)["\']\s*\]', text))

        # shell: ${VA_FOO:-default} 是明确的环境变量读取；
        # 纯 $VA_FOO 要先排掉脚本自己赋值的局部变量（如 VA_HOME_DIR）。
        for name in ("entrypoint.sh",):
            script = ROOT / "docker" / name
            if not script.is_file():
                continue
            text = script.read_text(encoding="utf-8")
            found |= set(re.findall(r"\$\{(VA_[A-Z0-9_]+)[:\-+]", text))
            assigned = set(re.findall(r"^\s*(?:local\s+)?(VA_[A-Z0-9_]+)=", text, re.MULTILINE))
            used = set(re.findall(r"\$(VA_[A-Z0-9_]+)\b", text))
            found |= used - assigned

        return found

    def test_every_va_var_read_by_code_is_passed_by_compose(self):
        read_by_code = self._env_vars_read_by_code()
        va_vars = {v for v in read_by_code if v.startswith("VA_")}

        # 代码里有默认值、且用户几乎不需要覆盖的，允许不进 compose
        optional_ok = {
            "VA_APP_DIR",       # 镜像内路径，由 Dockerfile 固定
            "VA_HOME",          # 同上
            "VA_FORCE_COLOR",   # 调试用
            "VA_USER",          # 同上
            "VA_WORKSPACE",     # compose 里已作为工作目录，但透传也无妨
        }
        missing = va_vars - self.compose_env - optional_ok

        self.assertFalse(
            missing,
            f"这些环境变量代码会读，但 docker-compose.yml 没有透传：{sorted(missing)}\n"
            f"结果会是「本地能跑、容器里不生效」。",
        )

    def test_compose_does_not_invent_unknown_vars(self):
        read_by_code = self._env_vars_read_by_code()
        known = read_by_code | {
            "GIT_USER_NAME",
            "GIT_USER_EMAIL",
            "PLAYWRIGHT_BROWSERS_PATH",
            "VA_WORKSPACE",
        }
        unknown = self.compose_env - known
        self.assertFalse(
            unknown,
            f"compose 透传了代码根本不读的变量（多半是拼错了）：{sorted(unknown)}",
        )

    def test_scanner_actually_sees_the_entrypoint(self):
        """防止扫描器本身失效导致上面两个测试变成永远通过的空壳。"""
        found = self._env_vars_read_by_code()
        for expected in ("VA_UID", "VA_GID", "VA_INSTALL_BROWSER"):
            self.assertIn(expected, found, f"扫描器没抓到 entrypoint.sh 里的 {expected}")

    def test_model_key_aliases_are_also_passed(self):
        """config.py 接受 OPENAI_API_KEY / DEEPSEEK_API_KEY 作兜底。

        只透传 VA_API_KEY 的话，用户按本地习惯配了别名，容器里就会「怎么都不生效」。
        """
        for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_BASE_URL"):
            self.assertIn(name, self.compose_env, f"{name} 没透传：本地能跑，容器里不行")


class TestMakefile(unittest.TestCase):
    def setUp(self) -> None:
        if not shutil.which("make"):
            self.skipTest("需要 make")
        self.text = read(ROOT / "Makefile")

    def test_declares_phony_targets(self):
        self.assertIn(".PHONY:", self.text)

    def test_dry_run_of_help_succeeds(self):
        result = subprocess.run(
            ["make", "-n", "help"], cwd=ROOT, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_targets_reference_existing_paths(self):
        for path in ("run.py", "requirements.txt", "requirements-browser.txt", "tests"):
            self.assertTrue((ROOT / path).exists())
            self.assertIn(path, self.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
