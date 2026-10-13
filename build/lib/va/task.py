"""任务：状态、推进记录、挂起。

**为什么需要它**

之前所有能力（进程、浏览器、门禁、MCP）都是「干活的手」，
但**没有东西记住「这个任务走到哪了」**。`update_plan` 只是个便签，
它不驱动任何东西，也不跨会话。

要「监控」就得先有「任务」这个对象。

────────────────────────────────────────────────────────────────

**状态和门禁是代码，每一步「怎么做」是模型**

* 状态机：现在在哪、下一步是什么、门禁过没过 —— **确定性的**，
  因为「测试没通过不许提交」如果能被模型绕过，那就不叫门禁
* 具体怎么写代码、怎么查 bug —— **交给模型**，这没法用状态机表达

**这正好也是「流程可进化」的正确形态**：改的是状态机的定义（数据），
不是模型的行为（不可控）。

────────────────────────────────────────────────────────────────

**推进记录里为什么要记「有没有被干预」**

用户要的是「挣来的自主权」：先人工审核，连续一周无干预率 ≥95% 才开自动。

那**必须从现在开始记**。不记的话，一周后算不出这个数——
数据是事后补不回来的。

记录还对监控有用：**哪一步最常被干预，就是流程定义最该改的地方**。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: 任务状态，**顺序就是流程顺序**。
STATES = (
    "intake",      # 读需求，提取验收标准
    "branch",      # 建分支
    "plan",        # 拆步骤
    "implement",   # 写代码（含 mock）
    "integrate",   # 联调
    "test",        # 跑测试
    "commit",      # 提交
    "submit",      # 提测
    "done",        # 完事
)

STATE_LABELS = {
    "intake": "读需求",
    "branch": "建分支",
    "plan": "计划",
    "implement": "实现",
    "integrate": "联调",
    "test": "测试",
    "commit": "提交",
    "submit": "提测",
    "done": "完成",
    "blocked": "挂起",
}

#: 挂起是**旁路状态**：它记住「之前在哪个状态」，恢复时回到那里。
BLOCKED = "blocked"

#: 进入下一个状态前必须满足什么。键是「目标状态」。
#: 放在代码里而不是配置里，是因为这些是**机制**（比如门禁），
#: 不是团队约定——团队不该有权把「不许提交没测过的代码」关掉。
REQUIREMENTS: dict[str, str] = {
    "test": "代码写完了才能测",
    "commit": "测试通过才能提交",
    "submit": "提交了才能提测",
}

#: **每一步该做什么。**
#:
#: 这是「工作流」真正跑起来的地方——不是一开始把十条指令塞给模型
#: （那样它容易漏），而是**推进到那一步时才告诉它**。
#:
#: 可以在 workflow.yaml 的 steps 段里覆盖，这样「流程进化」就能改进它。
STEPS: dict[str, str] = {
    "intake": (
        "读需求，提取出**可验证的验收标准**。\n"
        "缺验收标准就**去问**，不要自己编——它是后面所有测试的判据，"
        "编一个出来最后就是「测了个假东西」。\n"
        "同时理清：涉及哪些模块、依赖哪些后端接口、有没有设计稿。"
    ),
    "branch": (
        "建分支。**用 ticket + title 让工具按团队约定生成**，别手拼。\n"
        "base 从 workflow.yaml 的 branch.base 取，切之前确认工作区干净。"
    ),
    "plan": (
        "拆步骤。**每一步都要能验证**（跑什么命令、看什么现象）。\n"
        "标出哪些步骤依赖后端——那些要挂起，不是硬做。"
    ),
    "implement": (
        "写代码。**有后端依赖的地方先用 mock**，按 workflow.yaml 的 mock 段约定，"
        "不要自己发明一套。\n"
        "写完之后把「在等哪些接口」记下来（后面 task_block 要用）。"
    ),
    "integrate": (
        "联调。先确认后端接口真的可用，再**对比 mock 和真实响应的结构**\n"
        "（字段名、类型、必填性）。\n"
        "**不一致就报差异，不要擅自猜后端意图。**"
    ),
    "test": (
        "跑测试。用 run_tests——它会记进门禁台账，没跑过提交不了。\n"
        "功能测试用 browser_run 按验收标准操作页面。\n"
        "**没过就修到过**，不要绕过（绕过会记成严重事故）。"
    ),
    "commit": (
        "提交。**用 kind + scope + subject + ticket 让工具按模板生成 message**，"
        "别手写。\n"
        "提交前确认测试门禁过了——没过的话 git_commit 会拒绝。"
    ),
    "submit": (
        "提测。用 submit_for_test——没配 submit.target_branch 的话它什么都不做，"
        "那是正常的。\n"
        "推之前会再过一次门禁。"
    ),
    "done": "完事了。可以跑 diagnose_workflow 看看这轮有没有暴露出流程问题。",
}

#: 「干预」的种类。**这个划分就是 95% 指标的分子分母**。
INTERVENTIONS = {
    "none": "无干预",
    "rejected": "否决了推进",
    "edited": "手改了产出",
    "interrupted": "打断/中止",
    "rerun": "要求重跑",
    "redirected": "改了方向",
}


@dataclass
class Transition:
    """一次状态推进。**「有没有被干预」就记在这。**"""

    frm: str
    to: str
    at: float = field(default_factory=time.time)
    trigger: str = "human"          # human | auto
    intervention: str = "none"      # INTERVENTIONS 的键
    note: str = ""
    seconds: float = 0.0            # 在上一个状态待了多久

    def describe(self) -> str:
        when = time.strftime("%m-%d %H:%M", time.localtime(self.at))
        move = f"{STATE_LABELS.get(self.frm, self.frm)} → {STATE_LABELS.get(self.to, self.to)}"
        bits = [f"{when}  {move}"]
        if self.trigger == "auto":
            bits.append("自动")
        if self.intervention != "none":
            bits.append(INTERVENTIONS.get(self.intervention, self.intervention))
        if self.note:
            bits.append(self.note[:60])
        return "  " + "  ".join(bits)

    @property
    def untouched(self) -> bool:
        return self.intervention == "none"


@dataclass
class Blocker:
    """挂起时在等什么。

    **恢复条件必须机器可判定。**
    「等后端」不行——那是永远等下去；「GET /api/orders 返回 200 且含 total」才行。
    """

    what: str
    check: str = ""            # 可判定的条件，人话描述
    kind: str = "backend"      # backend | design | human | other
    checked_at: float = 0.0
    last_result: str = ""

    def describe(self) -> str:
        last = f"，上次检查：{self.last_result}" if self.last_result else "，还没查过"
        return f"  ⏸ {self.what}（{self.kind}）：{self.check or '（没写判定条件）'}{last}"


@dataclass
class Task:
    """一个任务的完整状态。"""

    id: str
    title: str = ""
    workspace: str = ""
    state: str = STATES[0]
    #: 挂起之前在哪，恢复时回去
    resume_to: str = ""
    blockers: list[Blocker] = field(default_factory=list)
    transitions: list[Transition] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    #: 这个任务用哪个分支
    branch: str = ""

    # ---- 查询

    @property
    def label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    @property
    def blocked(self) -> bool:
        return self.state == BLOCKED

    @property
    def finished(self) -> bool:
        return self.state == "done"

    def next_state(self) -> str:
        if self.blocked:
            return self.resume_to
        try:
            index = STATES.index(self.state)
        except ValueError:
            return STATES[0]
        return STATES[index + 1] if index + 1 < len(STATES) else ""

    def progress(self) -> str:
        """一行看板：走完了哪些、现在在哪、还剩什么。"""
        parts = []
        for state in STATES:
            mark = "·"
            if self.blocked:
                done_until = STATES.index(self.resume_to) if self.resume_to in STATES else 0
                mark = "✓" if STATES.index(state) < done_until else "·"
            else:
                here = STATES.index(self.state) if self.state in STATES else 0
                mark = "✓" if STATES.index(state) < here else ("▶" if STATES.index(state) == here else "·")
            parts.append(f"{mark}{STATE_LABELS[state]}")
        return " → ".join(parts)

    # ---- 推进记录

    def record(
        self,
        to: str,
        *,
        trigger: str = "human",
        intervention: str = "none",
        note: str = "",
    ) -> Transition:
        if intervention not in INTERVENTIONS:
            intervention = "none"
        now = time.time()
        transition = Transition(
            frm=self.state,
            to=to,
            at=now,
            trigger=trigger,
            intervention=intervention,
            note=note,
            seconds=max(0.0, now - self.updated_at),
        )
        self.transitions.append(transition)
        self.updated_at = now
        return transition

    # ---- 统计（挣来的自主权靠它）

    def stats(self, *, since: float = 0.0) -> dict[str, Any]:
        """窗口内的无干预率。

        **`trigger` 只统计人工推进的那些**：自动推进本来就没人干预，
        算进去会把比例稀释成「看起来很好」，那个数字没有意义。
        """
        window = [t for t in self.transitions if t.at >= since]
        human = [t for t in window if t.trigger == "human"]
        untouched = [t for t in human if t.untouched]
        return {
            "transitions": len(human),
            "untouched": len(untouched),
            "rate": (len(untouched) / len(human)) if human else 1.0,
            "interventions": {
                kind: sum(1 for t in human if t.intervention == kind)
                for kind in INTERVENTIONS
                if kind != "none" and any(t.intervention == kind for t in human)
            },
        }

    def timeline(self, limit: int = 12) -> str:
        if not self.transitions:
            return "  （还没推进过）"
        return "\n".join(t.describe() for t in self.transitions[-limit:])

    # ---- 序列化

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        task = cls(id=str(data.get("id") or ""), title=str(data.get("title") or ""))
        task.workspace = str(data.get("workspace") or "")
        task.state = str(data.get("state") or STATES[0])
        task.resume_to = str(data.get("resume_to") or "")
        task.branch = str(data.get("branch") or "")
        task.created_at = float(data.get("created_at") or time.time())
        task.updated_at = float(data.get("updated_at") or task.created_at)
        for raw in data.get("blockers") or []:
            if isinstance(raw, dict):
                task.blockers.append(
                    Blocker(
                        what=str(raw.get("what") or ""),
                        check=str(raw.get("check") or ""),
                        kind=str(raw.get("kind") or "other"),
                        checked_at=float(raw.get("checked_at") or 0),
                        last_result=str(raw.get("last_result") or ""),
                    )
                )
        for raw in data.get("transitions") or []:
            if isinstance(raw, dict):
                task.transitions.append(
                    Transition(
                        frm=str(raw.get("frm") or ""),
                        to=str(raw.get("to") or ""),
                        at=float(raw.get("at") or 0),
                        trigger=str(raw.get("trigger") or "human"),
                        intervention=str(raw.get("intervention") or "none"),
                        note=str(raw.get("note") or ""),
                        seconds=float(raw.get("seconds") or 0),
                    )
                )
        return task

    def describe(self) -> str:
        lines = [
            f"任务 {self.id}  {self.title}".rstrip(),
            f"  状态：{self.label}" + (f"（挂起前是 {STATE_LABELS.get(self.resume_to, self.resume_to)}）" if self.blocked else ""),
            f"  进度：{self.progress()}",
        ]
        if self.branch:
            lines.append(f"  分支：{self.branch}")
        for blocker in self.blockers:
            lines.append(blocker.describe())
        nxt = self.next_state()
        if nxt:
            lines.append(f"  下一步：{STATE_LABELS.get(nxt, nxt)}")
        stats = self.stats()
        if stats["transitions"]:
            lines.append(
                f"  推进 {stats['transitions']} 次，其中 {stats['untouched']} 次无干预"
                f"（{stats['rate'] * 100:.0f}%）"
            )
        return "\n".join(lines)


# --------------------------------------------------------------- 存储


def step_instruction(state: str, workflow=None) -> str:
    """这一步该做什么。workflow.yaml 的 steps 段可以覆盖。

    **覆盖是「流程进化」的关键**：当诊断发现某一步老出问题，
    改的就是这里——改的是数据，不是模型的行为。
    """
    if workflow is not None:
        custom = workflow.get(f"steps.{state}", "")
        if isinstance(custom, str) and custom.strip():
            return custom.strip()
    return STEPS.get(state, "")


def tasks_dir(workspace: str | Path) -> Path:
    """任务状态放**数据根**，不放项目里。

    **我一开始把它放在项目的 `.vibe/tasks/` 下，理由是「任务状态是项目的
    事，不是某次对话的事」。那个理由把两件事混为一谈了：**

      · **团队约定**（`workflow.yaml`）→ 项目里，要进 git 共享
      · **agent 的运行状态**（任务、事故、变更日志）→ 数据根
        ——一样能跨会话、跨重启，但不污染用户的仓库

    放项目里的实际后果：每跑一个任务就多一个 json 文件，**无限累积**，
    而且用户会看到他的仓库里冒出陌生的目录。
    """
    from .paths import project_dir

    return project_dir(workspace) / "tasks"


def _slug(task_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in task_id)
    return safe.strip("-") or "task"


class TaskStore:
    """任务存在**项目里**（`.vibe/tasks/`）。

    不放在会话目录：**任务状态是项目的事，不是某次对话的事**。
    换个会话、过一周回来，它还在；进 git 还能团队共享。
    """

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)

    def path_for(self, task_id: str) -> Path:
        return tasks_dir(self.workspace) / f"{_slug(task_id)}.json"

    def load(self, task_id: str) -> Task | None:
        path = self.path_for(task_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        task = Task.from_dict(data)
        if not task.id:
            task.id = task_id
        return task

    def save(self, task: Task) -> Path:
        path = self.path_for(task.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(task.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def all(self) -> list[Task]:
        directory = tasks_dir(self.workspace)
        if not directory.is_dir():
            return []
        out: list[Task] = []
        for path in sorted(directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                out.append(Task.from_dict(data))
            except (OSError, ValueError):
                continue
        return sorted(out, key=lambda t: t.updated_at, reverse=True)

    def active(self) -> list[Task]:
        return [t for t in self.all() if not t.finished]


def open_task(
    workspace: str | Path, task_id: str, title: str = ""
) -> tuple[Task, bool]:
    """拿到任务，没有就新建。返回 ``(任务, 是不是新建的)``。"""
    store = TaskStore(workspace)
    task = store.load(task_id)
    if task is not None:
        if title and not task.title:
            task.title = title
        return task, False
    return (
        Task(id=task_id, title=title, workspace=str(workspace), branch=""),
        True,
    )
