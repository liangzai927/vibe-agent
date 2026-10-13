"""跑测试并把结果记进台账。

**门禁需要「测试发生过」这个事实。** `run_bash` 跑什么都行，
它不知道自己在跑测试；所以另有一个明确的入口。

结果怎么判定，分两层：

1. **退出码**——这是唯一可靠的信号。0 就是过，非 0 就是不过。
2. **从输出里捞通过/失败数**——纯粹为了给人看。
   捞不到也没关系，按「一次运行」记。

**不解析出具体数字也照样能当门禁**：门禁要的是「跑过且过了」，
不是「一共 42 个用例」。把解析当成锦上添花，不要当成前提。
"""

from __future__ import annotations

import re
import subprocess
import time
from typing import Any

from ..gate import KIND_LABELS, fingerprint
from ..util import truncate
from .base import ToolError
from .context import AgentContext, WorkspaceTool

#: 从测试输出里捞计数的常见写法。
#: 顺序有意义——先匹配信息量大的。
_COUNT_PATTERNS = (
    # pytest: "3 failed, 42 passed in 1.2s"
    re.compile(r"(?P<failed>\d+)\s+failed[,\s]+(?P<passed>\d+)\s+passed", re.I),
    re.compile(r"(?P<passed>\d+)\s+passed[,\s]+(?P<failed>\d+)\s+failed", re.I),
    # jest: "Tests:       1 failed, 5 passed, 6 total"
    re.compile(r"Tests:\s*(?:(?P<failed>\d+)\s+failed,\s*)?(?P<passed>\d+)\s+passed", re.I),
    # vitest / mocha
    re.compile(r"(?P<passed>\d+)\s+passing", re.I),
    re.compile(r"(?P<failed>\d+)\s+failing", re.I),
    # pytest 最常见的结尾："42 passed in 1.2s" / "3 failed"
    # **这条漏过一次**：只写了 passing，结果 "42 passed" 认不出来，
    # 数字退化成「一次运行」——门禁不受影响，但人看到的数是错的
    re.compile(r"(?P<passed>\d+)\s+passed", re.I),
    re.compile(r"(?P<failed>\d+)\s+failed", re.I),
)

#: 默认超时。测试可能跑很久，给得比一般命令宽。
DEFAULT_TIMEOUT = 900


def parse_counts(output: str, exit_code: int) -> tuple[int, int]:
    """从输出里猜通过/失败数。猜不到就按「一次运行」记。

    **猜错不如不猜**：宁可记成 1/0，也不要因为解析出个奇怪的数字
    让门禁做出错误判断。所以只有真的匹配到才算。
    """
    passed = failed = 0
    seen = False
    for pattern in _COUNT_PATTERNS:
        for match in pattern.finditer(output):
            seen = True
            groups = match.groupdict()
            if groups.get("passed"):
                passed += int(groups["passed"])
            if groups.get("failed"):
                failed += int(groups["failed"])
        if seen:
            break

    if seen:
        return passed, failed
    # 没解析出来：用退出码表态，计成「一次运行」
    return (1, 0) if exit_code == 0 else (0, 1)


class RunTests(WorkspaceTool):
    name = "run_tests"
    description = (
        "跑项目配置好的测试（命令来自 workflow.yaml 的 test 段），并把结果记进门禁台账。\n"
        "kind=logic 跑 test.logic；kind=e2e 用浏览器按验收标准操作页面（用 browser_run）；\n"
        "kind=visual 做样式还原比对。\n"
        "**提交前必须跑通**——git_commit 会检查台账，没跑或没过会被拒。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["logic", "e2e", "visual"],
                "description": "跑哪一类。默认 logic",
            },
            "command": {
                "type": "string",
                "description": "临时覆盖配置里的命令（可选）",
            },
            "timeout": {
                "type": "integer",
                "description": f"超时秒数，默认 {DEFAULT_TIMEOUT}",
            },
            "note": {
                "type": "string",
                "description": "给这次运行加个说明，会记进台账",
            },
        },
        "required": [],
    }

    def __init__(self, ctx: AgentContext) -> None:
        super().__init__(ctx.workspace, ctx.cfg)
        self.ctx = ctx

    def _command_for(self, kind: str) -> str:
        workflow = self.ctx.workflow
        if workflow is None:
            return ""
        return str(workflow.get(f"test.{kind}", "") or "").strip()

    def run(
        self,
        kind: str = "logic",
        command: str = "",
        timeout: int = DEFAULT_TIMEOUT,
        note: str = "",
        **_: Any,
    ) -> str:
        kind = (kind or "logic").strip().lower()
        if kind not in KIND_LABELS:
            raise ToolError(
                f"不认识 kind={kind!r}。可用：{' / '.join(KIND_LABELS)}"
            )

        cmd = (command or self._command_for(kind)).strip()
        if not cmd:
            raise ToolError(
                f"没配置 {KIND_LABELS.get(kind, kind)} 的命令。\n"
                f"在项目 .vibe/workflow.yaml 里加：\n"
                f"  test:\n    {kind}: <你的命令>"
            )

        seconds = max(1, min(int(timeout), 3600))
        started = time.time()
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=str(self.workspace.root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=seconds,
                env={
                    **_environ(),
                    "CI": "1",          # 很多测试框架靠它切到非交互模式
                    "FORCE_COLOR": "0",  # 别上色，输出要能直接读
                },
            )
        except subprocess.TimeoutExpired:
            self._record(kind, 0, 1, note or f"超时（{seconds}s）")
            raise ToolError(
                f"{KIND_LABELS.get(kind, kind)} 跑了 {seconds} 秒还没结束，已中断。\n"
                f"命令：{cmd}"
            )
        except OSError as exc:
            raise ToolError(f"跑不起来：{exc}") from exc

        output = (result.stdout or "") + (result.stderr or "")
        passed, failed = parse_counts(output, result.returncode)
        elapsed = time.time() - started

        # 退出码说了算：解析出来的数字可能不全（比如只跑了部分用例）
        if result.returncode != 0 and failed == 0:
            failed = 1
            passed = 0

        run = self._record(
            kind,
            passed,
            failed,
            note or (f"退出码 {result.returncode}" if result.returncode else ""),
        )

        label = KIND_LABELS.get(kind, kind)
        mark = "✓" if run.ok else "✗"
        head = (
            f"{mark} {label}：{passed} 通过 / {failed} 失败"
            f"（退出码 {result.returncode}，{elapsed:.1f}s）"
        )
        tail = truncate(output.strip() or "（没有输出）", limit=4000, label="测试输出")
        hint = ""
        if not run.ok:
            hint = (
                "\n\n门禁没过——先修到绿再提交。"
                "如果这次改动确实不需要测试，提交时用 override_gate 说明理由。"
            )
        return f"{head}\n\n{tail}{hint}"

    def _record(self, kind: str, passed: int, failed: int, detail: str):
        ledger = getattr(self.ctx, "ledger", None)
        if ledger is None:
            return _FakeRun(kind, passed, failed)
        return ledger.record(
            kind,
            passed=passed,
            failed=failed,
            fingerprint=fingerprint(self.workspace.root),
            detail=detail,
        )


class _FakeRun:
    """没有台账时（子 agent 之类的路径）也要能返回个结果。"""

    def __init__(self, kind: str, passed: int, failed: int) -> None:
        self.kind = kind
        self.passed = passed
        self.failed = failed

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.passed > 0


def _environ() -> dict[str, str]:
    import os

    return dict(os.environ)


RUN_TESTS_TOOL = (RunTests,)
