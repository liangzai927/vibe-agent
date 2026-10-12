"""提测：推送、等 CI、通知。

**核心约定：每一步都是可选的，没配就跳过并说明「跳过了」，不是报错。**

提测是往**外部**推东西——推错了收不回来。所以这里不设「合理的默认值」：
没配 `target_branch` 就**不提测**，而不是「默认推到 test」。
一个刚接入的项目不该因为跑了 `va workflow` 就被推到 test 分支去。

────────────────────────────────────────────────────────────────

**每一步的结果都要说清楚**，包括「跳过了」：

    提测
      · 目标分支：未配置 → 跳过（不提测）
      · CI：未配置 → 跳过
      · 通知：未配置 → 跳过

而不是什么都不输出，让人以为跑了。

**CI 失败就停**：不继续往下走。推都推出去了、CI 红了还发通知说「提测完成」，
比不通知更糟。
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Step:
    """提测里的一步。``skipped`` 是**正常结果**，不是失败。"""

    name: str
    ok: bool = False
    skipped: bool = False
    detail: str = ""
    output: str = ""

    def line(self) -> str:
        if self.skipped:
            return f"  · {self.name}：跳过（{self.detail}）"
        mark = "✓" if self.ok else "✗"
        return f"  {mark} {self.name}：{self.detail}"


@dataclass
class SubmitResult:
    steps: list[Step] = field(default_factory=list)
    ok: bool = True
    stopped_at: str = ""

    @property
    def did_anything(self) -> bool:
        return any(not step.skipped for step in self.steps)

    def report(self) -> str:
        lines = ["提测" if self.did_anything else "提测（配置为空，什么都没做）"]
        lines.extend(step.line() for step in self.steps)

        for step in self.steps:
            if not step.skipped and not step.ok and step.output:
                lines.append("")
                lines.append(f"{step.name} 的输出：")
                lines.append(step.output[:3000])

        if not self.ok:
            lines.append("")
            lines.append(
                f"**在「{self.stopped_at}」这一步停下**——后面的事没做。"
            )
        return "\n".join(lines)


class Submitter:
    """按配置执行提测。没配的步骤一律跳过。"""

    def __init__(self, workspace: str | Path, workflow=None, gate=None) -> None:
        self.workspace = Path(workspace)
        self.workflow = workflow
        self.gate = gate

    def _get(self, path: str, fallback: Any = None) -> Any:
        if self.workflow is None:
            return fallback
        return self.workflow.get(path, fallback)

    # ---- 主流程

    def run(self, *, task: str = "", commit: str = "") -> SubmitResult:
        result = SubmitResult()

        target = str(self._get("submit.target_branch", "") or "").strip()
        if not target:
            # **没配就是不做，不报错。** 而且要说清楚是「跳过」不是「失败」。
            result.steps.append(
                Step("目标分支", skipped=True, detail="未配置 submit.target_branch")
            )
            result.steps.append(Step("CI", skipped=True, detail="同上，没有提测目标"))
            result.steps.append(Step("通知", skipped=True, detail="同上"))
            return result

        # ① 门禁（可选）
        gate_step = self._check_gate()
        result.steps.append(gate_step)
        if not gate_step.ok and not gate_step.skipped:
            result.ok = False
            result.stopped_at = "测试门禁"
            result.steps.append(Step("推送", skipped=True, detail="门禁没过"))
            return result

        # ② 本地 CI 校验（可选）
        ci_step = self._run_ci_command()
        result.steps.append(ci_step)
        if not ci_step.ok and not ci_step.skipped:
            result.ok = False
            result.stopped_at = "本地 CI 校验"
            result.steps.append(Step("推送", skipped=True, detail="本地校验没过"))
            return result

        # ③ 推送
        push_step = self._push(target)
        result.steps.append(push_step)
        if not push_step.ok:
            result.ok = False
            result.stopped_at = "推送"
            result.steps.append(Step("通知", skipped=True, detail="推送失败"))
            return result

        # ④ 等 CI（可选）
        wait_step = self._wait_for_ci()
        result.steps.append(wait_step)
        if not wait_step.ok and not wait_step.skipped:
            result.ok = False
            result.stopped_at = "CI"
            result.steps.append(Step("通知", skipped=True, detail="CI 没过"))
            return result

        # ⑤ 通知（可选）
        result.steps.append(self._notify(task=task, branch=target, commit=commit))
        return result

    # ---- 各步骤

    def _check_gate(self) -> Step:
        if not self._get("submit.require_gate", True):
            return Step("测试门禁", skipped=True, detail="配置里关掉了 require_gate")
        if self.gate is None:
            return Step("测试门禁", skipped=True, detail="没有测试台账")

        from .gate import fingerprint

        verdict = self.gate.evaluate(
            self.workflow, current=fingerprint(self.workspace)
        )
        if verdict.ok:
            return Step("测试门禁", ok=True, detail=verdict.summary.splitlines()[0])
        return Step("测试门禁", ok=False, detail="；".join(verdict.problems)[:300])

    def _run_ci_command(self) -> Step:
        command = str(self._get("submit.ci_command", "") or "").strip()
        if not command:
            return Step("本地校验", skipped=True, detail="未配置 submit.ci_command")

        timeout = int(self._get("submit.ci_timeout_minutes", 20) or 20) * 60
        try:
            result = subprocess.run(
                command, shell=True, cwd=str(self.workspace),
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return Step("本地校验", ok=False, detail=f"超时（{timeout // 60} 分钟）")
        except OSError as exc:
            return Step("本地校验", ok=False, detail=str(exc))

        output = ((result.stdout or "") + (result.stderr or "")).strip()
        if result.returncode == 0:
            return Step("本地校验", ok=True, detail="通过", output=output[-1500:])
        return Step(
            "本地校验", ok=False,
            detail=f"失败（退出码 {result.returncode}）", output=output[-3000:],
        )

    def _push(self, target: str) -> Step:
        remote = str(self._get("submit.remote", "origin") or "origin").strip()
        mode = str(self._get("submit.push_mode", "direct") or "direct").strip()
        source = str(self._get("submit.source_branch", "") or "").strip()

        if mode == "merge_request":
            # 平台 API 各不一样，不假装实现了。**明确说没做**比默默 direct 推好。
            return Step(
                "推送", ok=False,
                detail=(
                    "push_mode = merge_request，但这个能力还没实现"
                    "（要对接具体平台的 API）。改成 direct 或者手工开 MR。"
                ),
            )

        if not source:
            source = self._current_branch()
            if not source:
                return Step("推送", ok=False, detail="拿不到当前分支名")

        try:
            result = subprocess.run(
                ["git", "push", remote, f"{source}:{target}"],
                cwd=str(self.workspace), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=180,
                env={**_env(), "GIT_TERMINAL_PROMPT": "0"},
            )
        except subprocess.TimeoutExpired:
            return Step("推送", ok=False, detail="超时（180s）")
        except OSError as exc:
            return Step("推送", ok=False, detail=str(exc))

        output = ((result.stdout or "") + (result.stderr or "")).strip()
        if result.returncode == 0:
            return Step(
                "推送", ok=True, detail=f"{remote} {source} → {target}", output=output
            )

        from .gitutil import explain

        return Step(
            "推送", ok=False,
            detail=explain(output) or f"失败（退出码 {result.returncode}）",
            output=output,
        )

    def _wait_for_ci(self) -> Step:
        """等 CI。

        **没有平台 API 时没法真等**——本地也没有「CI 状态」这个东西。
        所以这里只做能做到的：如果配了 ci_command，那是我们自己跑的；
        真·远端 CI 需要平台 API，**明确说没做**，而不是假装等过了。
        """
        if not self._get("submit.wait_for_ci", False):
            return Step("CI", skipped=True, detail="未配置 wait_for_ci")
        return Step(
            "CI", ok=False,
            detail=(
                "wait_for_ci = true，但读取远端 CI 状态需要对接具体平台的 API，"
                "这个能力还没实现。**本地校验请用 ci_command**，"
                "或者把 wait_for_ci 关掉、推完自己看流水线。"
            ),
        )

    def _notify(self, *, task: str, branch: str, commit: str = "") -> Step:
        env_name = str(self._get("submit.notify_env", "") or "").strip()
        if not env_name:
            return Step("通知", skipped=True, detail="未配置 submit.notify_env")

        import os

        url = os.environ.get(env_name, "").strip()
        if not url:
            return Step(
                "通知", skipped=True,
                detail=f"环境变量 {env_name} 是空的（配了名字但没给地址）",
            )

        template = str(
            self._get("submit.notify_template", "提测：{task} → {branch}")
            or "{task} → {branch}"
        )
        text = (
            template.replace("{task}", task or "（未命名）")
            .replace("{branch}", branch)
            .replace("{commit}", commit or "")
            .replace("{status}", "已推送")
        )

        payload = json.dumps({"text": text, "msgtype": "text", "content": text}).encode()
        request = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                code = response.status
        except urllib.error.HTTPError as exc:
            # **通知失败不影响提测本身**——代码已经推出去了，
            # 不能因为消息没发出去就说提测失败。
            return Step("通知", ok=True, detail=f"发送失败（HTTP {exc.code}），但不影响提测")
        except Exception as exc:  # noqa: BLE001 - 网络各种错
            return Step("通知", ok=True, detail=f"发送失败（{exc}），但不影响提测")

        return Step("通知", ok=True, detail=f"已发送（HTTP {code}）")

    def _current_branch(self) -> str:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(self.workspace), capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""


def _env() -> dict[str, str]:
    import os

    return dict(os.environ)
