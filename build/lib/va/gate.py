"""测试门禁：不绿不许提交。

**为什么这是个「机制」而不是一个「工具」**

提示词里写「请确保测试通过」是**祈祷**——模型可能看错、可能乐观、
可能忘了跑。要让它真的成立，必须让**提交这个动作本身**在测试没过时失败。

所以门禁拦在 `git_commit` 里面，不是写在提示词里。

────────────────────────────────────────────────────────────────

**两条判定，缺一不可**：

1. **该跑的测试都跑了，而且通过**
   哪些「该跑」由 workflow.yaml 的 `test` 段决定。
   没配测试的项目门禁自动关闭——不能因为它没配就挡住提交。

2. **测试发生在最后一次代码改动之后**
   这条容易被忽略，但它挡的是最危险的一种情况：
   **改完代码 → 拿旧结果过门禁 → 提交了从没测过的代码**。

   实现上用**指纹**：测试时记下当时的代码版本，提交前重算一遍。
   不一样就说明代码变过了，之前那次测试不算数。

指纹取的是「将要提交的东西」——用 git 状态算，而不是猜。
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

#: 一次 git 调用的超时。指纹要快，慢了会拖累每次提交。
GIT_TIMEOUT = 20

#: 测试种类。顺序就是展示顺序。
KINDS = ("logic", "e2e", "visual")

KIND_LABELS = {
    "logic": "逻辑测试",
    "e2e": "功能测试",
    "visual": "样式还原",
}


@dataclass
class TestRun:
    kind: str
    passed: int
    failed: int
    fingerprint: str
    at: float = field(default_factory=time.time)
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.passed > 0

    def describe(self) -> str:
        label = KIND_LABELS.get(self.kind, self.kind)
        when = time.strftime("%H:%M:%S", time.localtime(self.at))
        mark = "✓" if self.ok else "✗"
        return f"{mark} {label}  {self.passed} 通过 / {self.failed} 失败  （{when}）"


@dataclass
class Verdict:
    ok: bool
    problems: list[str] = field(default_factory=list)
    summary: str = ""

    def describe(self) -> str:
        lines = [self.summary]
        for problem in self.problems:
            lines.append(f"  ✗ {problem}")
        return "\n".join(lines)


# --------------------------------------------------------------- 指纹


def fingerprint(workspace: Path) -> str:
    """当前工作区的「代码版本」。

    **用 git 状态算，因为那正好是「将要提交的东西」。**
    只看我们自己的写操作是不够的——你可能在编辑器里手改了一个文件，
    那次改动同样会让之前的测试结果失效。

    非 git 仓库返回空串：门禁会退化成「必须跑过一次测试」。
    """
    root = Path(workspace)
    try:
        listing = subprocess.run(
            ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if listing.returncode != 0:
        return ""

    paths = []
    for entry in listing.stdout.split("\0"):
        if len(entry) > 3:
            # porcelain 格式：XY<space>path
            paths.append(entry[3:].strip())

    digest = hashlib.sha256()
    for relative in sorted(set(paths)):
        digest.update(relative.encode("utf-8", "replace"))
        digest.update(b"\0")
        target = root / relative
        try:
            if target.is_file():
                digest.update(str(target.stat().st_size).encode())
                digest.update(b"\0")
                # 内容敏感：同样大小但内容改了也要算变化
                with target.open("rb") as handle:
                    while chunk := handle.read(65536):
                        digest.update(chunk)
            else:
                digest.update(b"<dir-or-gone>")
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return digest.hexdigest()[:16]


# --------------------------------------------------------------- 台账


class TestLedger:
    """这个会话里跑过的测试。

    **会话级、不持久化**：进程重启后门禁要求重跑。这是安全的默认——
    重启意味着上下文丢了，重跑一遍没什么损失，而拿旧结果提交有损失。
    """

    def __init__(self) -> None:
        self.runs: list[TestRun] = []

    def record(
        self,
        kind: str,
        *,
        passed: int,
        failed: int,
        fingerprint: str = "",
        detail: str = "",
    ) -> TestRun:
        run = TestRun(
            kind=kind,
            passed=max(0, int(passed)),
            failed=max(0, int(failed)),
            fingerprint=fingerprint,
            detail=detail[:2000],
        )
        self.runs.append(run)
        return run

    def latest(self, kind: str) -> TestRun | None:
        for run in reversed(self.runs):
            if run.kind == kind:
                return run
        return None

    def clear(self) -> None:
        self.runs.clear()

    def describe(self) -> str:
        if not self.runs:
            return "还没跑过测试。"
        return "\n".join(run.describe() for run in self.runs[-8:])

    # ---- 判定

    def required_kinds(self, workflow) -> list[str]:
        """按工作流定义算出「要跑哪些」。

        没配的就不要求——**不能因为一个项目没写测试配置就挡住它提交**。
        """
        if workflow is None:
            return []
        needed: list[str] = []
        if str(workflow.get("test.logic", "") or "").strip():
            needed.append("logic")
        if str(workflow.get("test.e2e_base_url", "") or "").strip() and str(
            workflow.get("test.start", "") or ""
        ).strip():
            # 配了启动命令和地址才谈得上功能测试
            needed.append("e2e")
        if str(workflow.get("test.visual_designs", "") or "").strip():
            needed.append("visual")
        return needed

    def gate_mode(self, workflow) -> str:
        if workflow is None:
            return "off"
        return str(workflow.get("test.gate", "strict"))

    def evaluate(self, workflow, *, current: str = "") -> Verdict:
        """提交前判定。"""
        mode = self.gate_mode(workflow)
        if mode == "off":
            return Verdict(ok=True, summary="测试门禁已关闭")

        needed = self.required_kinds(workflow)
        if not needed:
            return Verdict(
                ok=True,
                summary="没配测试，门禁不拦（在 workflow.yaml 的 test 段里配）",
            )

        problems: list[str] = []
        lines: list[str] = []

        for kind in needed:
            label = KIND_LABELS.get(kind, kind)
            run = self.latest(kind)
            if run is None:
                problems.append(f"{label}：**从来没跑过**")
                lines.append(f"  · {label}  未跑")
                continue
            lines.append(f"  {run.describe()}")
            if not run.ok:
                problems.append(
                    f"{label}：{run.failed} 个失败"
                    + (f"（{run.detail.splitlines()[0][:80]}）" if run.detail else "")
                )
            elif current and run.fingerprint and run.fingerprint != current:
                # **这条挡的是最危险的情况**：改完代码，拿旧结果过门禁
                problems.append(
                    f"{label}：跑完之后代码又改过（测试结果已失效，需要重跑）"
                )

        if not problems:
            return Verdict(ok=True, summary="测试门禁通过：\n" + "\n".join(lines))

        verdict = Verdict(
            ok=False,
            problems=problems,
            summary="测试门禁没过：\n" + "\n".join(lines),
        )
        return verdict
