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

import os
import re
import shutil
import subprocess
import sys
import tempfile
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


def image_env_names() -> set[str]:
    """镜像里用 ENV 设置过的变量名（含续行）。"""
    joined = read(DOCKERFILE).replace("\\\n", " ")
    names: set[str] = set()
    for line in joined.splitlines():
        stripped = line.strip()
        if stripped.startswith("ENV "):
            for token in stripped[4:].split():
                names.add(token.split("=", 1)[0])
    return names


class TestDockerfile(unittest.TestCase):
    def setUp(self) -> None:
        self.text = read(DOCKERFILE)
        # 注释里的示例（例如 `-v "$PWD:/workspace"`）不该被当成真实引用
        self.code = "\n".join(
            line for line in self.text.splitlines() if not line.lstrip().startswith("#")
        )

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

    @staticmethod
    def _split_at_from(text: str) -> tuple[str, str, str]:
        """切成 (FROM 之前, FROM 那一行, FROM 之后)。

        之前写成两段式很容易错位——FROM 那一行既不属于「之前」也不属于
        「之后」，单独拿出来最不容易出错。
        """
        before, sep, rest = text.partition("\nFROM ")
        if not sep:
            return text, "", ""
        from_line, _, after = rest.partition("\n")
        return before, from_line, after

    @classmethod
    def _declared_after_from(cls, after: str) -> set[str]:
        """收集 FROM 之后由 ARG / ENV 声明的变量名（含续行）。"""
        joined = after.replace("\\\n", " ")
        names: set[str] = set()
        for line in joined.splitlines():
            stripped = line.strip()
            if not stripped.startswith(("ARG ", "ENV ")):
                continue
            body = stripped.split(None, 1)[1]
            for token in body.split():
                names.add(token.split("=", 1)[0])
        return names

    def test_args_used_after_from_are_declared_after_from(self):
        """FROM 之前声明的 ARG 在 FROM 之后不可见——这是最常见的构建失败原因之一。

        从实际出现的 $VAR / ${VAR} 反推需要哪些声明，而不是硬编码一份名单，
        否则改了写法（比如 ARG 换成 ENV）测试就会误报。
        """
        before, from_line, after = self._split_at_from(self.code)
        used = set(re.findall(r"\$\{?([A-Z_][A-Z0-9_]*)\}?", after))
        declared = self._declared_after_from(after)

        self.assertTrue(used, "解析器没找到任何变量引用，测试本身失效了")
        undeclared = used - declared
        self.assertFalse(
            undeclared,
            f"这些变量在 FROM 之后被引用，但没有在 FROM 之后用 ARG/ENV 声明：{sorted(undeclared)}",
        )

    def test_python_version_arg_is_declared_before_from(self):
        """反过来：FROM 里用到的变量必须在 FROM 之前声明。"""
        before, from_line, _ = self._split_at_from(self.code)
        used_in_from = set(re.findall(r"\$\{?([A-Z_][A-Z0-9_]*)\}?", from_line))

        self.assertTrue(
            used_in_from,
            f"解析器没从 FROM 行里找到变量，测试本身失效了（from 行：{from_line!r}）",
        )
        for name in used_in_from:
            self.assertRegex(
                before,
                rf"(?m)^ARG\s+{re.escape(name)}\b",
                f"{name} 在 FROM 里被使用，但 FROM 之前没有 ARG 声明它",
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

    def test_declares_pythonpath(self):
        """compose 把 working_dir 设成 /workspace，cwd 就不在 /app 了。

        没有 PYTHONPATH 的话 `python3 -m va` 直接 ModuleNotFoundError——
        而 README 里推荐的入口正是这条路。
        """
        self.assertRegex(self.code, r"(?m)^\s*PYTHONPATH=/app")

    def test_build_args_accept_truthy_values(self):
        """`--build-arg WITH_BROWSER=true` 不该被静默忽略。"""
        for name in ("WITH_NODE", "WITH_BROWSER"):
            self.assertRegex(
                self.code,
                rf"{name}\" in|{name}\" =|{name}\" }}",
                f"{name} 的开关判断没找到",
            )
        self.assertIn("true|TRUE|yes|YES|on|ON", self.code)
        self.assertIn("跳过", self.code, "关闭可选组件时应该明确说一声，而不是静默跳过")

    def test_node_download_is_checksum_verified(self):
        self.assertIn("SHASUMS256.txt", self.code)
        self.assertIn("sha256sum -c -", self.code, "下载的 Node 包没有校验完整性")

    def test_env_var_names(self):
        names = image_env_names()
        self.assertIn("VA_WORKSPACE", names)
        self.assertIn("PLAYWRIGHT_BROWSERS_PATH", names)
        self.assertIn("PYTHONPATH", names)

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


class TestEntrypointExecution(unittest.TestCase):
    """真的把 entrypoint 跑起来。

    本机不是 Linux 也不是 root，走不到 UID 对齐那段分支；但「非 root 时会不会
    崩」「缺依赖时是报错还是优雅跳过」这些控制流是可以实测的，而且恰恰是最容易
    写错的地方。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.workspace = base / "workspace"
        self.home = base / "home"
        self.home.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *args: str, workspace: Path | None = None, **extra_env: str):
        env = {
            **os.environ,
            "VA_WORKSPACE": str(workspace or self.workspace),
            "HOME": str(self.home),
            # 别去改开发机的全局 git 配置
            "VA_GIT_SAFE_DIRECTORY": "0",
            **extra_env,
        }
        return subprocess.run(
            ["bash", str(ENTRYPOINT), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def _run_without_workspace_env(self, *args: str):
        """模拟 compose 里裸 key 把镜像 ENV 删掉的情形：环境里根本没有 VA_WORKSPACE。"""
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("VA_WORKSPACE", "VA_GIT_SAFE_DIRECTORY")
        }
        env["HOME"] = str(self.home)
        env["VA_GIT_SAFE_DIRECTORY"] = "0"
        return subprocess.run(
            ["bash", str(ENTRYPOINT), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def test_executes_the_command(self):
        result = self._run("bash", "-c", "echo 来自容器内部")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("来自容器内部", result.stdout)

    def test_creates_workspace_when_missing(self):
        self.assertFalse(self.workspace.exists())
        self._run("true")
        self.assertTrue(self.workspace.is_dir(), "VA_WORKSPACE 不存在时应自动创建")

    def test_survives_missing_workspace_env_var(self):
        """compose 的裸 key 形式会把镜像 ENV 删掉。

        在 `set -u` 下裸用 $VA_WORKSPACE 会让容器以 "unbound variable" 退出 1。
        测试机不是 root，建不出 /workspace（那是环境限制），所以这里只断言
        「不是死在变量未定义上」；默认值逻辑由 test_workspace_env_has_safe_default
        做静态校验。
        """
        result = self._run_without_workspace_env("true")
        self.assertNotIn("unbound variable", result.stderr, "变量未定义导致退出")
        self.assertNotIn("VA_WORKSPACE:", result.stderr, "变量未定义导致退出")

    def test_workspace_env_has_safe_default(self):
        # 注释里也会提到 $VA_WORKSPACE，先剥掉
        code = "\n".join(
            line
            for line in read(ENTRYPOINT).splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertIn(
            "${VA_WORKSPACE:-",
            code,
            "VA_WORKSPACE 必须带默认值，否则镜像 ENV 被删掉时 set -u 会直接退出",
        )
        self.assertNotIn(
            "$VA_WORKSPACE",
            code,
            "不允许裸用 $VA_WORKSPACE —— 统一走带默认值的 WS 变量",
        )

    def test_wraps_non_executable_args_as_va_arguments(self):
        """`docker compose run va doctor` 是替换 CMD 而不是追加。

        于是 entrypoint 收到 ["doctor"]，直接 exec 会去找一个叫 doctor 的可执行
        文件。这里验证兜底逻辑会把它变成 `python3 -m va doctor`。
        """
        result = subprocess.run(
            ["bash", str(ENTRYPOINT), "doctor", "--offline", "--quick"],
            capture_output=True,
            text=True,
            timeout=120,
            env={
                **os.environ,
                "HOME": str(self.home),
                "VA_WORKSPACE": str(self.workspace),
                "VA_GIT_SAFE_DIRECTORY": "0",
                # 让 python3 -m va 在测试机上也找得到包
                "PYTHONPATH": str(ROOT),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("环境自检", result.stdout, "参数没被兜底成 va 的参数")

    def test_wraps_task_string_as_va_argument(self):
        """`docker compose run va "修个 bug"` 同样走兜底。"""
        result = subprocess.run(
            ["bash", str(ENTRYPOINT), "--version"],
            capture_output=True,
            text=True,
            timeout=120,
            env={
                **os.environ,
                "HOME": str(self.home),
                "VA_WORKSPACE": str(self.workspace),
                "VA_GIT_SAFE_DIRECTORY": "0",
                "PYTHONPATH": str(ROOT),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("vibe-agent", result.stdout)

    def test_existing_executable_is_not_wrapped(self):
        result = self._run("bash", "-c", "echo 直接执行 bash")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("直接执行 bash", result.stdout)
        self.assertNotIn("No module named", result.stderr)

    def test_browser_install_without_root_is_skipped_not_fatal(self):
        """非 root 时装不了系统库——要提示，但不能因此整个容器起不来。"""
        result = self._run("bash", "-c", "echo 仍然继续执行", VA_INSTALL_BROWSER="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("仍然继续执行", result.stdout)
        self.assertIn("需要以 root 启动", result.stderr)

    def test_truthy_values_are_accepted_for_browser_install(self):
        """VA_INSTALL_BROWSER=true 不能被静默忽略，得跟代码里的开关语义一致。"""
        result = self._run("true", VA_INSTALL_BROWSER="true")
        self.assertIn("需要以 root 启动", result.stderr)

    def test_browser_install_fully_disabled_stays_quiet(self):
        result = self._run("true", VA_INSTALL_BROWSER="0")
        self.assertNotIn("VA_INSTALL_BROWSER", result.stderr)

    def test_warns_when_git_identity_is_missing(self):
        """容器里没有开发机的 ~/.gitconfig，不提就等着 agent 提交时才失败。"""
        result = self._run("true")
        self.assertIn("git 身份", result.stderr)
        self.assertIn("GIT_USER_EMAIL", result.stderr)

    def test_no_warning_when_git_identity_is_provided(self):
        result = self._run("true", GIT_USER_NAME="测试", GIT_USER_EMAIL="t@example.com")
        self.assertNotIn("没配 git 身份", result.stderr)
        # 应该真的写进了容器内的全局配置
        configured = subprocess.run(
            ["git", "config", "--global", "user.email"],
            capture_output=True,
            text=True,
            env={**os.environ, "HOME": str(self.home)},
        )
        self.assertEqual(configured.stdout.strip(), "t@example.com")

    def test_safe_directory_is_written_when_enabled(self):
        result = self._run(
            "true", workspace=self.workspace, VA_GIT_SAFE_DIRECTORY="1"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        config = (self.home / ".gitconfig").read_text(encoding="utf-8")
        self.assertIn("safe", config)
        self.assertIn("*", config)

    def test_safe_directory_does_not_accumulate(self):
        """家目录是持久卷；用 --add 的话每次启动追加一行，.gitconfig 会一直涨。"""
        for _ in range(3):
            self._run("true", VA_GIT_SAFE_DIRECTORY="1")
        config = (self.home / ".gitconfig").read_text(encoding="utf-8")
        occurrences = config.count("directory = *")
        self.assertEqual(occurrences, 1, f"safe.directory 被重复写了 {occurrences} 次")


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

    def test_does_not_bare_key_image_env_vars(self):
        """给镜像已用 ENV 设过的变量写裸 key，docker 会把它当成「删除该变量」。

        这个坑很隐蔽：`- VA_WORKSPACE` 在宿主机没导出该变量时，会把镜像的
        `ENV VA_WORKSPACE=/workspace` 抹掉，entrypoint 在 `set -u` 下直接退出 1。
        PLAYWRIGHT_BROWSERS_PATH 同理，被删后 playwright 会回落到 ~/.cache，
        找不到构建时装在 /opt 的 Chromium。
        """
        image_env = image_env_names()
        offenders = [key for key in self.service["environment"] if key in image_env]
        self.assertFalse(
            offenders,
            f"compose 用裸 key 透传了镜像已 ENV 设置的变量：{offenders}。\n"
            f"docker 会把镜像的值删掉。改成 `NAME=${{NAME:-默认值}}`，或者干脆不写。",
        )

    def test_no_container_name(self):
        """固定 container_name 会让两个 `docker compose run` 并发冲突。"""
        self.assertNotIn(
            "container_name",
            self.service,
            "去掉 container_name，compose run 才能并行跑",
        )


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
