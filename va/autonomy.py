"""挣来的自主权。

用户要的：「刚开始需要我人工审核，当运行一段时间、连续一周任务完成度达到
95% 满意度（几乎不需要我干预方向、纠正问题）就可以自动推进。」

**这是「挣来的」而不是「配置出来的」**：不是设一个开关就完事，
而是要先证明它值得信任。所以要有度量。

────────────────────────────────────────────────────────────────

**「满意度」必须变成可度量的东西**

    满意度 ≈ 无干预推进的比例

「干预」是一串**能自动记录的事件**：否决过推进、手改过产出、打断过、
要求重跑、改了方向。这些在阶段 E1 的推进记录里已经有了。

**只有人工推进的那些计入统计。** 自动推进本来就没人干预，
算进分母会把比例稀释成「看起来很好」——那个数字没有意义。

────────────────────────────────────────────────────────────────

**光有比例不够，还得有「严重事故」**

极端例子：40 次推进里 38 次无干预，但其中一次是**提交了没测过的代码
并推到了 test 分支**。比例 95%，实际不可接受。

所以：**有严重事故 → 连续计数清零，重新观察。**
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: 严重事故的种类。**会清零连续计数**。
#:
#: 判断标准是「这件事发生过一次就不能接受」，而不是「经常发生才不行」。
SEVERE_KINDS = {
    "gate_override": "绕过测试门禁提交了",
    "wrong_branch": "推到了非提测目标的分支",
    "outside_write": "写了工作区外的文件",
    "destructive": "删了不该删的东西",
}

#: 不算事故的（算「干预」，计入比例但不清零）。
#: 格式问题属于这一类——它说明约定没写清楚，不是危险动作。
NOT_SEVERE = {
    "commit_format": "commit 格式不合规范",
    "branch_name": "分支名不合规范",
}


@dataclass
class Incident:
    kind: str
    detail: str = ""
    at: float = field(default_factory=time.time)
    task: str = ""

    def describe(self) -> str:
        when = time.strftime("%m-%d %H:%M", time.localtime(self.at))
        label = SEVERE_KINDS.get(self.kind, self.kind)
        tail = f"：{self.detail[:70]}" if self.detail else ""
        return f"{when}  {label}{tail}"


@dataclass
class Readiness:
    """「能不能开自动推进」的判定。"""

    ready: bool = False
    rate: float = 0.0
    transitions: int = 0
    untouched: int = 0
    window_days: int = 7
    threshold: float = 0.95
    incidents: list[Incident] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    mode: str = "manual"

    def describe(self) -> str:
        lines = []
        if self.mode == "auto":
            lines.append("推进模式：**自动**")
        else:
            lines.append("推进模式：人工审核")

        if self.transitions == 0:
            lines.append(
                f"  还没法判断——最近 {self.window_days} 天没有人工推进记录。"
            )
            return "\n".join(lines)

        lines.append(
            f"  最近 {self.window_days} 天：{self.untouched}/{self.transitions} 次推进无干预"
            f"（{self.rate * 100:.0f}%，门槛 {self.threshold * 100:.0f}%）"
        )

        if self.incidents:
            lines.append(f"  严重事故 {len(self.incidents)} 次（清零重算）：")
            lines.extend(f"    {incident.describe()}" for incident in self.incidents[:5])
        else:
            lines.append("  期间没有严重事故")

        if self.ready:
            lines.append("")
            lines.append(
                "→ **可以开自动推进了。** 在 workflow.yaml 里设 "
                "autonomy.mode = auto。"
            )
        else:
            lines.append("")
            lines.append("→ 还不到开自动的时候：" + "；".join(self.reasons))
        return "\n".join(lines)


class AutonomyLog:
    """严重事故的记录。存在项目里，跨会话。"""

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)

    @property
    def path(self) -> Path:
        return self.workspace / ".vibe" / "autonomy.json"

    def incidents(self) -> list[Incident]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        out = []
        for raw in data.get("incidents") or []:
            if isinstance(raw, dict):
                out.append(
                    Incident(
                        kind=str(raw.get("kind") or ""),
                        detail=str(raw.get("detail") or ""),
                        at=float(raw.get("at") or 0),
                        task=str(raw.get("task") or ""),
                    )
                )
        return out

    def record(self, kind: str, *, detail: str = "", task: str = "") -> Incident:
        incident = Incident(kind=kind, detail=detail, task=task)
        items = self.incidents()
        items.append(incident)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"incidents": [asdict(item) for item in items[-200:]]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return incident


def _config(workflow, path: str, fallback: Any) -> Any:
    if workflow is None:
        return fallback
    return workflow.get(path, fallback)


def evaluate(
    tasks: list[Any],
    workspace: str | Path,
    workflow=None,
    *,
    now: float | None = None,
) -> Readiness:
    """算「能不能开自动推进」。

    两条同时满足才行：**比例达标** 且 **期间没有严重事故**。
    """
    now = now or time.time()
    window_days = int(_config(workflow, "autonomy.window_days", 7) or 7)
    threshold = float(_config(workflow, "autonomy.threshold", 0.95) or 0.95)
    mode = str(_config(workflow, "autonomy.mode", "manual") or "manual")

    since = now - window_days * 86400

    # 跨任务汇总人工推进记录
    total = 0
    untouched = 0
    for task in tasks:
        stats = task.stats(since=since)
        total += stats["transitions"]
        untouched += stats["untouched"]

    incidents = [
        item for item in AutonomyLog(workspace).incidents() if item.at >= since
    ]

    readiness = Readiness(
        transitions=total,
        untouched=untouched,
        rate=(untouched / total) if total else 0.0,
        window_days=window_days,
        threshold=threshold,
        incidents=incidents,
        mode=mode,
    )

    if total == 0:
        readiness.reasons.append("没有可判断的推进记录")
        return readiness

    if total < 5:
        readiness.reasons.append(f"样本太少（只有 {total} 次推进，至少要 5 次）")
    if readiness.rate < threshold:
        readiness.reasons.append("无干预率没到门槛")
    if incidents:
        readiness.reasons.append(f"期间有 {len(incidents)} 次严重事故")

    readiness.ready = not readiness.reasons
    return readiness
