"""环境自检。

「在任何机器上都能跑」的前提，是能快速回答一个问题：这台机器现在还缺什么。
doctor 就是回答这个的——逐项检查，缺什么直接给出可执行的修复命令，而不是
等用户跑到一半撞上一句莫名其妙的报错。

退出码：有「必需」项失败时返回 1，否则 0。方便 CI 里当门禁用。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import run_hint
from . import browser as browser_mod
from .config import Config
from .session import SessionStore
from .util import bold, cyan, dim, green, red, yellow
from .workspace import Workspace

REQUIRED = "required"
OPTIONAL = "optional"

OK = "ok"
FAIL = "fail"
SKIP = "skip"

_MARK = {OK: "✓", FAIL: "✗", SKIP: "–"}


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""
    hint: str = ""
    level: str = OPTIONAL

    @property
    def is_blocking(self) -> bool:
        return self.status == FAIL and self.level == REQUIRED


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    def add(self, result: Result) -> Result:
        self.results.append(result)
        return result

    @property
    def failed_required(self) -> list[Result]:
        return [r for r in self.results if r.is_blocking]

    def count(self, level: str, status: str | None = None) -> int:
        return sum(
            1
            for r in self.results
            if r.level == level and (status is None or r.status == status)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": not self.failed_required,
            "results": [asdict(r) for r in self.results],
        }


# --------------------------------------------------------------------- 工具


def _shell(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    text = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    return proc.returncode, text.splitlines()[0] if text else ""


def _module_available(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _writable(directory: Path) -> tuple[bool, str]:
    probe = directory / ".va-write-probe"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, ""
    except OSError as exc:
        return False, str(exc)


# --------------------------------------------------------------------- 检查


def check_python() -> Result:
    version = platform.python_version()
    ok = sys.version_info >= (3, 10)
    return Result(
        "Python",
        OK if ok else FAIL,
        f"{version}  ({platform.system()} {platform.machine()})",
        "" if ok else "需要 Python 3.10 或更高版本",
        REQUIRED,
    )


def check_workspace(workspace: Workspace) -> Result:
    ok, error = _writable(workspace.root)
    return Result(
        "工作区",
        OK if ok else FAIL,
        f"{workspace.root}",
        "" if ok else f"目录不可写：{error}",
        REQUIRED,
    )


def check_session_dir(cfg: Config) -> Result:
    directory = SessionStore.default_dir(cfg.workspace)
    ok, error = _writable(directory)

    # 说清楚在不在项目里。用户在 doctor 里最想知道的正是这个——
    # 「我的会话到底存哪了，会不会把 git 弄脏」。
    try:
        inside = directory.resolve().is_relative_to(Path(cfg.workspace).resolve())
    except (OSError, ValueError):
        inside = False

    if not ok:
        hint = f"目录不可写：{error}"
    elif inside:
        hint = "在项目内（旧位置）。想搬到项目外：见 README「文件都放在哪」"
    else:
        hint = "在项目外，不会弄脏 git status"

    return Result("会话目录", OK if ok else FAIL, str(directory), hint, REQUIRED)


def check_git() -> Result:
    if not shutil.which("git"):
        return Result("git", FAIL, "未找到 git", "安装 git 后才能提交代码、查看差异", REQUIRED)
    code, version = _shell(["git", "--version"])
    return Result("git", OK if code == 0 else FAIL, version or "版本未知", "", REQUIRED)


def check_git_identity() -> Result:
    if not shutil.which("git"):
        return Result("git 身份", SKIP, "git 不可用", "", OPTIONAL)
    _, name = _shell(["git", "config", "--global", "user.name"])
    _, email = _shell(["git", "config", "--global", "user.email"])
    if name and email:
        return Result("git 身份", OK, f"{name} <{email}>", "", OPTIONAL)
    missing = "、".join(x for x, v in (("user.name", name), ("user.email", email)) if not v)
    # 标为「可选」是刻意的：没配 git 身份 agent 照样能读写代码、跑测试，
    # 只是提交不了。标成必需会让 CI 里 doctor 永远返回非零。
    return Result(
        "git 身份",
        FAIL,
        f"未配置 {missing}",
        "agent 提交代码时会失败（其他功能不受影响）。设置："
        "git config --global user.name '你的名字' && git config --global user.email 'you@example.com'",
        OPTIONAL,
    )


def check_ripgrep() -> Result:
    if not shutil.which("rg"):
        return Result("搜索加速", SKIP, "未安装 ripgrep", "可选。装了 grep 工具会快很多（apt install ripgrep / brew install ripgrep）")
    code, version = _shell(["rg", "--version"])
    return Result("搜索加速", OK if code == 0 else SKIP, version or "ripgrep")


def check_node() -> Result:
    if not shutil.which("node"):
        return Result("Node.js", SKIP, "未安装", "可选。构建前端项目时才需要。容器镜像已内置（WITH_NODE=1）")
    code, version = _shell(["node", "--version"])
    detail = version or "版本未知"
    if code == 0 and shutil.which("npm"):
        detail += f"  /  npm {_shell(['npm', '--version'])[1]}"
    return Result("Node.js", OK if code == 0 else SKIP, detail)


def check_playwright() -> Result:
    if not browser_mod.playwright_installed():
        return Result(
            "playwright",
            SKIP,
            "未安装",
            "可选。需要渲染页面/截图对比时安装：pip install -r requirements-browser.txt",
        )
    try:
        from importlib.metadata import version

        ver = version("playwright")
    except Exception:  # noqa: BLE001
        ver = "已安装"
    return Result("playwright", OK, ver)


def check_pillow() -> Result:
    if not _module_available("PIL"):
        return Result("Pillow", SKIP, "未安装", "可选。像素级截图对比需要：pip install pillow")
    try:
        import PIL

        return Result("Pillow", OK, getattr(PIL, "__version__", "已安装"))
    except Exception:  # noqa: BLE001
        return Result("Pillow", SKIP, "导入失败")


def check_browser(*, probe: bool = True) -> Result:
    if not browser_mod.playwright_installed():
        return Result("浏览器", SKIP, "playwright 未安装，跳过", "")

    info = browser_mod.detect(force=True, probe=probe)
    if info.available:
        detail = info.label
        if info.tried:
            detail += f"  （先试失败：{'、'.join(info.tried)}）"
        return Result("浏览器", OK, detail)
    return Result("浏览器", FAIL, info.error, info.hint, OPTIONAL)


def check_vision(cfg: Config) -> Result:
    if not cfg.vision_model:
        return Result(
            "视觉模型",
            SKIP,
            "未配置",
            "可选。让 agent 把截图和设计图做比对时需要一个多模态模型，"
            "设置 VA_VISION_MODEL / VA_VISION_BASE_URL / VA_VISION_API_KEY",
        )
    return Result("视觉模型", OK, f"{cfg.vision_model}  @ {cfg.vision_base_url or cfg.base_url}")


def check_api(cfg: Config, *, timeout: float = 30.0) -> Result:
    if not cfg.api_key:
        return Result(
            "模型连通",
            SKIP,
            "未配置 API key",
            "设置 VA_API_KEY，或把 .env.example 复制成 .env 填写",
            OPTIONAL,
        )
    from .llm import LLM, LLMError

    started = time.time()
    try:
        turn = LLM(cfg).complete([{"role": "user", "content": "回复两个字：连通"}])
    except LLMError as exc:
        return Result("模型连通", FAIL, str(exc)[:200], "检查 VA_API_KEY / VA_BASE_URL / VA_MODEL", REQUIRED)
    except Exception as exc:  # noqa: BLE001
        return Result("模型连通", FAIL, f"{type(exc).__name__}: {exc}"[:200], "", REQUIRED)
    elapsed = time.time() - started
    return Result("模型连通", OK, f"{cfg.model}   {elapsed:.2f}s   → {turn.content.strip()[:20]!r}", "", REQUIRED)


# --------------------------------------------------------------------- 入口


def run(
    cfg: Config,
    workspace: Workspace,
    *,
    offline: bool = False,
    quick: bool = False,
    as_json: bool = False,
) -> int:
    report = Report()
    report.add(check_python())
    report.add(check_workspace(workspace))
    report.add(check_session_dir(cfg))
    report.add(check_git())
    report.add(check_git_identity())
    report.add(check_ripgrep())
    report.add(check_node())
    report.add(check_playwright())
    report.add(check_browser(probe=not quick))
    report.add(check_pillow())
    report.add(check_vision(cfg))
    if not offline:
        report.add(check_api(cfg))

    if as_json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 1 if report.failed_required else 0

    _render(report, cfg)
    return 1 if report.failed_required else 0


def _render(report: Report, cfg: Config) -> None:
    print()
    print(bold("  vibe-agent 环境自检"))
    print(dim("  " + "─" * 62))
    print(dim(f"  工作区 {cfg.workspace}"))
    print()

    for level, title in ((REQUIRED, "必需"), (OPTIONAL, "可选")):
        items = [r for r in report.results if r.level == level]
        if not items:
            continue
        print(bold(f"  {title}"))
        for item in items:
            print("    " + _format(item))
        print()

    blocking = report.failed_required
    req_total = report.count(REQUIRED)
    req_ok = report.count(REQUIRED, OK)
    opt_total = report.count(OPTIONAL)
    opt_ok = report.count(OPTIONAL, OK)

    if blocking:
        print(red(f"  ✗ {len(blocking)} 项必需检查未通过，agent 无法正常工作。"))
        print(dim(f"    按上面的提示逐项修复后重跑：{run_hint('doctor')}"))
    else:
        print(green("  ✓ 必需项全部通过。"))
    print(dim(f"  必需 {req_ok}/{req_total} · 可选 {opt_ok}/{opt_total} 可用"))
    print()


def _format(item: Result) -> str:
    mark = _MARK.get(item.status, "?")
    colored = {
        OK: green(mark),
        FAIL: red(mark),
        SKIP: dim(mark),
    }.get(item.status, mark)

    name = f"{item.name:<12}"
    detail = item.detail
    line = f"{colored} {name} {detail}"
    if item.hint:
        line += "\n" + dim(f"        {item.hint}")
    return line
