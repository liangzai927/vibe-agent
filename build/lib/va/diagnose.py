"""诊断：找出「哪里不对」。

**它不能知道什么是对的。**

工作流定义里写的是团队约定。约定对不对，只有你们知道——模型内部没有
任何东西能告诉它「commit 应该带 scope」。所以这里只做一件事：

    **发现「有东西在反复出问题」，并指出它在哪。**

「正确的是什么」来自你的反馈、失败信号、统计——agent 负责把你的判断
固化成配置，不是自己发明流程。

────────────────────────────────────────────────────────────────

**哪些信号是机器能定的**（这个模块管的）：

* **干预热点** —— 哪个状态的干预率异常高。那一步的定义就有问题。
  这是最可靠的一类：它是统计出来的，不靠猜。
* **严重事故** —— 绕过门禁、推错分支。发生过一次就不能接受。
* **配置指向不存在的东西** —— `mock.dir` 指的目录不在、`test.logic`
  的命令跑不了、`branch.base` 的分支不存在。**配置和现实脱节**是最常见的
  一类问题，而且完全可判定。

**哪些只能靠模型**（不在这个模块里）：

* 你一句「mock 别放那儿」对应到哪个配置项
* 一个从没见过的失败模式该怎么改流程

那部分交给 agent 读这份报告 + 你的话，然后用 propose_workflow_change 落地。
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 干预率超过这个就算热点
HOTSPOT_RATE = 0.5
#: 至少要有这么多次样本才下结论
HOTSPOT_MIN_SAMPLES = 3


@dataclass
class Signal:
    """一个「有东西不对」的观察。

    **故意不直接给「该怎么改」**——那要结合你的意图。这里只说到
    「哪里不对、依据是什么」，把判断留给人。
    """

    kind: str
    what: str
    evidence: str
    suggestion: str = ""

    def describe(self) -> str:
        lines = [f"· {self.what}", f"    依据：{self.evidence}"]
        if self.suggestion:
            lines.append(f"    可能相关：{self.suggestion}")
        return "\n".join(lines)


@dataclass
class Diagnosis:
    signals: list[Signal] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.signals

    def report(self) -> str:
        if self.clean:
            return (
                "没发现明显问题。\n"
                f"（检查了：{'、'.join(self.checked)}）"
            )
        lines = [f"发现 {len(self.signals)} 个信号：", ""]
        for signal in self.signals:
            lines.append(signal.describe())
            lines.append("")
        lines.append(
            "**这些只是「哪里不对」，不是「该怎么改」。** 改法要结合你的意图——"
            "想让我改的话，说说你希望它是什么样，或者直接讲哪个信号不对。"
        )
        return "\n".join(lines).rstrip()


# --------------------------------------------------------------- 采集


def diagnose(workspace: str | Path, workflow=None, tasks: list[Any] | None = None) -> Diagnosis:
    root = Path(workspace)
    result = Diagnosis()

    _hotspots(result, tasks or [])
    _incidents(result, root)
    _config_vs_reality(result, root, workflow)
    _effects(result, root, tasks or [])

    return result


def _effects(result: Diagnosis, root: Path, tasks: list[Any]) -> None:
    """之前那些配置改动，到底有没有用。"""
    result.checked.append("历史改动的效果")
    for effect in change_effects(root, tasks, limit=5):
        if effect.comparable and effect.after_rate > effect.before_rate + 0.1:
            result.signals.append(
                Signal(
                    kind="ineffective_change",
                    what=f"改过的 {effect.path} 之后干预率反而上升了",
                    evidence=effect.describe().replace("\n", " "),
                    suggestion="考虑回滚这次改动",
                )
            )


def _hotspots(result: Diagnosis, tasks: list[Any]) -> None:
    """哪个状态最常被干预。

    **这是最可靠的一类信号**：它是统计出来的，不靠猜。
    哪一步干预率异常高，那一步的定义就有问题。
    """
    result.checked.append("干预热点")

    by_target: dict[str, dict[str, int]] = {}
    for task in tasks:
        for transition in task.transitions:
            if transition.trigger != "human":
                continue
            bucket = by_target.setdefault(transition.to, {"total": 0, "touched": 0})
            bucket["total"] += 1
            if not transition.untouched:
                bucket["touched"] += 1

    from .task import STATE_LABELS

    for state, counts in by_target.items():
        if counts["total"] < HOTSPOT_MIN_SAMPLES:
            continue
        rate = counts["touched"] / counts["total"]
        if rate <= HOTSPOT_RATE:
            continue
        label = STATE_LABELS.get(state, state)
        result.signals.append(
            Signal(
                kind="hotspot",
                what=f"「{label}」这一步 {counts['touched']}/{counts['total']} 次被干预"
                f"（{rate * 100:.0f}%）",
                evidence="任务推进记录里的干预标记",
                suggestion=(
                    f"这一步的前置条件、产物定义、或者它和前后步骤的划分可能不对。"
                    f"相关配置：看 workflow.yaml 里有没有和「{label}」对应的段"
                ),
            )
        )


def _incidents(result: Diagnosis, root: Path) -> None:
    result.checked.append("严重事故")

    from .autonomy import SEVERE_KINDS, AutonomyLog

    incidents = AutonomyLog(root).incidents()
    recent = [i for i in incidents if i.at >= time.time() - 30 * 86400]
    if not recent:
        return

    by_kind: dict[str, int] = {}
    for incident in recent:
        by_kind[incident.kind] = by_kind.get(incident.kind, 0) + 1

    for kind, count in by_kind.items():
        label = SEVERE_KINDS.get(kind, kind)
        result.signals.append(
            Signal(
                kind="incident",
                what=f"最近 30 天「{label}」发生了 {count} 次",
                evidence=".vibe/autonomy.json",
                suggestion=(
                    "如果门禁总被绕过，说明那条门禁配错了（比如项目根本跑不了"
                    "那类测试）——**一条永远被绕过的门禁比没有门禁更糟**，"
                    "它会训练出无脑 override 的习惯"
                    if kind == "gate_override"
                    else ""
                ),
            )
        )


def _config_vs_reality(result: Diagnosis, root: Path, workflow) -> None:
    """配置指向的东西还在不在。

    **配置和现实脱节是最常见的一类问题**，而且完全可判定：
    目录改了名、脚本删了、分支合了——配置还指着旧地方。
    """
    if workflow is None:
        return
    result.checked.append("配置与实际是否一致")

    # mock 目录
    strategy = str(workflow.get("mock.strategy", "")).strip()
    mock_dir = str(workflow.get("mock.dir", "")).strip()
    if strategy == "fixed" and mock_dir and not (root / mock_dir).exists():
        candidates = _similar_dirs(root, mock_dir)
        result.signals.append(
            Signal(
                kind="config_reality",
                what=f"mock.strategy = fixed，但 mock.dir（{mock_dir}）不存在",
                evidence=f"{root / mock_dir} 不存在",
                suggestion=(
                    f"项目里像 mocks 的目录：{'、'.join(candidates)}"
                    if candidates
                    else "确认一下 mock 到底放哪"
                ),
            )
        )

    # 分支 base
    base = str(workflow.get("branch.base", "")).strip()
    if base and _is_git(root) and not _branch_exists(root, base):
        result.signals.append(
            Signal(
                kind="config_reality",
                what=f"branch.base 配的是 {base}，但这个分支不存在",
                evidence=f"git branch --list {base} 是空的",
                suggestion="切分支时会直接失败——改成实际存在的分支",
            )
        )

    # 提测目标
    target = str(workflow.get("submit.target_branch", "")).strip()
    if target and base == target and base:
        result.signals.append(
            Signal(
                kind="config_reality",
                what=f"branch.base 和 submit.target_branch 都是 {target}",
                evidence="两者配成了同一个分支",
                suggestion=(
                    "提测通常是从特性分支推到集成分支。如果从提测分支切、"
                    "又推回提测分支，那中间没有隔离"
                ),
            )
        )

    # 测试命令
    command = str(workflow.get("test.logic", "")).strip()
    if command:
        missing = _missing_script(root, command)
        if missing:
            result.signals.append(
                Signal(
                    kind="config_reality",
                    what=f"test.logic 引用的 {missing} 不在 package.json 里",
                    evidence=f"命令：{command}",
                    suggestion="门禁会因为命令本身跑不起来而永远不过",
                )
            )


# --------------------------------------------------------------- 小工具


def _is_git(root: Path) -> bool:
    return (root / ".git").exists()


def _branch_exists(root: Path, name: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "branch", "--list", name],
            cwd=str(root), capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return True      # 查不了就当它存在，别误报
    return bool(result.stdout.strip())


def _similar_dirs(root: Path, wanted: str) -> list[str]:
    """找个名字像的目录——「你是不是想说这个」。"""
    name = Path(wanted).name.lower().strip("/")
    out: list[str] = []
    try:
        for path in root.rglob("*"):
            if len(out) >= 3:
                break
            if not path.is_dir():
                continue
            if any(part in {".git", "node_modules", "dist", "build"} for part in path.parts):
                continue
            here = path.name.lower()
            if here == name or (name and (name in here or here in name)) and here:
                out.append(str(path.relative_to(root)))
    except OSError:
        pass
    return out


def _missing_script(root: Path, command: str) -> str:
    """test.logic 里 `npm run xxx` 的 xxx 在 package.json 里吗。"""
    import json
    import re

    match = re.search(r"\b(?:npm|pnpm|yarn)\s+run\s+([\w:-]+)", command)
    if not match:
        return ""
    script = match.group(1)

    package = root / "package.json"
    if not package.is_file():
        return ""
    try:
        data = json.loads(package.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if script in (data.get("scripts") or {}):
        return ""
    return f"npm run {script}"


# --------------------------------------------------------------- 效果验证


@dataclass
class ChangeEffect:
    """一次配置改动，改完之后干预率变好了还是变坏了。

    **这是闭环的关键一步。** 没有它，「自我进化」就只是「不断改配置」——
    改对了留着、改错了回滚，得有个依据。

    依据就是**同一个指标**：干预率既是「哪里不对」的探测器，
    也是「改对了吗」的验证器。一个指标，两个用途。
    """

    path: str
    changed_at: float
    reason: str
    before_rate: float = 0.0
    after_rate: float = 0.0
    before_samples: int = 0
    after_samples: int = 0

    @property
    def comparable(self) -> bool:
        """两边样本都够才谈得上比较。"""
        return self.before_samples >= 3 and self.after_samples >= 3

    def describe(self) -> str:
        when = time.strftime("%m-%d", time.localtime(self.changed_at))
        head = f"{when}  改了 {self.path}（{self.reason or '没写理由'}）"
        if not self.comparable:
            return (
                f"{head}\n"
                f"    还看不出效果：改动前 {self.before_samples} 次、"
                f"改动后 {self.after_samples} 次推进（两边都要至少 3 次）"
            )

        delta = self.after_rate - self.before_rate
        if delta < -0.1:
            verdict = "→ **变好了**，留着"
        elif delta > 0.1:
            verdict = "→ **变差了**，考虑回滚"
        else:
            verdict = "→ 没有明显变化"
        return (
            f"{head}\n"
            f"    干预率 {self.before_rate * 100:.0f}%（{self.before_samples} 次）"
            f" → {self.after_rate * 100:.0f}%（{self.after_samples} 次）"
            f"    {verdict}"
        )


def change_effects(
    workspace: str | Path, tasks: list[Any] | None = None, *, limit: int = 10
) -> list[ChangeEffect]:
    """每次配置改动前后的干预率对比。

    **样本不够就直说看不出效果**，不硬给一个结论——
    拿两三次推进去判断一次配置改动好不好，那是自欺欺人。
    """
    from .workflow_edit import history

    entries = history(workspace)
    if not entries:
        return []

    transitions = []
    for task in tasks or []:
        transitions.extend(
            t for t in task.transitions if t.trigger == "human"
        )
    transitions.sort(key=lambda t: t.at)

    out: list[ChangeEffect] = []
    for entry in entries[-limit:]:
        at = float(entry.get("at") or 0)
        # 只看改动前 7 天 / 后 7 天，太长会被别的事情稀释
        before = [t for t in transitions if at - 7 * 86400 <= t.at < at]
        after = [t for t in transitions if at <= t.at < at + 7 * 86400]

        for raw in entry.get("changes") or []:
            out.append(
                ChangeEffect(
                    path=str(raw.get("path") or ""),
                    changed_at=at,
                    reason=str(raw.get("reason") or entry.get("source") or ""),
                    before_rate=_touched_rate(before),
                    after_rate=_touched_rate(after),
                    before_samples=len(before),
                    after_samples=len(after),
                )
            )
    return out


def _touched_rate(items: list[Any]) -> float:
    if not items:
        return 0.0
    touched = sum(1 for t in items if not t.untouched)
    return touched / len(items)
