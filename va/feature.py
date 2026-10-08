"""功能开发流水线：一条命令跑完「建分支 → 规划 → 编码 → 自测 → 自审 → 提交」。

**为什么要有这一层**：光有工具不等于有好结果。同一套工具，随手丢一句
「给列表加分页」和按「先读懂现状 → 拆计划 → 实现 → 自测 → 让别人审一遍」走，
结果差得很远。这一层做的事就是**把顺序固定下来**。

哪些是确定性的（代码写死）：
    * 建分支、提交前的状态检查、可选推送
哪些是交给模型的（写进任务书）：
    * 怎么拆计划、怎么写代码、跑什么测试、审出来的问题怎么改

刻意**不自动推送**：推送会把改动推给别人看到，这一步该由人决定。
需要的话显式传 ``--push``。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .util import bold, cyan, dim, green, red, yellow

BRANCH_PREFIX = "feat/"
MAX_SLUG_WORDS = 5


@dataclass
class FeatureRequest:
    requirement: str
    branch: str = ""
    base: str = ""
    doc: str = ""
    design: str = ""
    push: bool = False
    review: bool = True
    tests: str = ""
    no_branch: bool = False

    def resolved_branch(self) -> str:
        name = (self.branch or "").strip()
        if name:
            return name
        return default_branch_name(self.requirement)


def slugify(text: str) -> str:
    """从需求文字里挤出可读的 ASCII 分支名片段。

    中文需求抽不出 ASCII 词时返回空串——让调用方去挑别的方式，
    而不是硬编出一个 `feat/---` 这种玩意。
    """
    words = re.findall(r"[A-Za-z][A-Za-z0-9]*", text)
    if not words:
        return ""
    cleaned = [w.lower() for w in words if w.lower() not in _STOP_WORDS]
    if not cleaned:
        cleaned = [w.lower() for w in words]
    return "-".join(cleaned[:MAX_SLUG_WORDS])[:48].strip("-")


_STOP_WORDS = {
    "a", "an", "the", "to", "for", "of", "in", "on", "and", "or",
    "is", "are", "be", "with", "please", "add", "make", "let",
}


def default_branch_name(requirement: str) -> str:
    slug = slugify(requirement)
    if slug:
        return f"{BRANCH_PREFIX}{slug}"
    # 全中文需求：用一个时间戳，至少是合法且唯一的
    return f"{BRANCH_PREFIX}{time.strftime('%m%d-%H%M')}"


def build_task(req: FeatureRequest) -> str:
    """把流程写成任务书。顺序是刻意的——跳过自测和自审是这类工具最常见的失败方式。"""
    parts: list[str] = ["你正在执行一条**功能开发流水线**。严格按下面的顺序做，不要跳步。", ""]

    parts.append("## 1. 理解需求")
    if req.doc:
        parts.append(
            f"- 需求文档在 `{req.doc}`，先用 `read_document` 读它"
            f"（长文档先看目录，再按章节取用）。"
        )
    if req.design:
        parts.append(
            f"- 设计稿在 `{req.design}`，用 `read_design` 解析成文字规格，"
            f"规格会落盘到 `.va/designs/`，实现时对照它。"
        )
    parts.append(f"- 需求描述：{req.requirement.strip()}")
    parts.append(
        "- 读完用 `update_plan` 把工作拆成步骤。**不要跳过这一步**——"
        "不拆计划就开始写，长任务里你一定会绕回原地。"
    )

    parts.append("")
    parts.append("## 2. 先读懂现状")
    parts.append(
        "- 动手前用 `glob` / `grep` / `read_file` 摸清现有结构和代码风格。"
        "找最接近的现有实现当参照，沿用它的写法。"
    )
    parts.append("- 不要引入新的重型依赖，也不要顺手重构无关的代码。")

    parts.append("")
    parts.append("## 3. 实现")
    parts.append("- 按计划逐步做，每完成一步就更新计划状态。")
    parts.append("- 一次只推进一件事；卡住了把状态改成 blocked 并写清原因。")

    parts.append("")
    parts.append("## 4. 自测")
    if req.tests:
        parts.append(f"- 跑这个：`{req.tests}`")
    else:
        parts.append(
            "- 跑项目现有的测试/构建/lint（先用 `read_document README.md` "
            "或看配置文件确认命令）。"
        )
    parts.append(
        "- 有界面改动就用 `render_page` 看真实渲染结果，再和设计稿做 "
        "`pixel_diff`。凭代码想象 UI 一定会出错。"
    )
    parts.append("- **没通过就修到通过为止。不要跳过这一步，也不要只测顺利路径。**")
    parts.append("- 新写的逻辑要有对应的测试；如果项目本来没有测试，至少手动验证一次。")

    if req.review:
        parts.append("")
        parts.append("## 5. 让别人审一遍")
        parts.append(
            "- 用 `review_changes` 起一个独立上下文的审查者，**把需求原文一起传给它**——"
            "这样它才能判断「做对了没有」，而不只是「代码写得对不对」。"
        )
        parts.append("- 它有「必须修」的问题就修掉，然后回到第 4 步重新验证。")

    parts.append("")
    parts.append(f"## {'6' if req.review else '5'}. 提交")
    parts.append(
        "- 用 `git_commit` 提交，message 用祈使句说清楚做了什么。"
        "如果改动跨了多个不相关的方面，分成多个提交。"
    )

    parts.append("")
    parts.append("## 边界")
    parts.append("- 需求里没写清楚、又会影响结果的关键决策，**先问我**，不要自己拍板。")
    parts.append("- 发现需求本身矛盾、或与现有代码冲突，直接说出来，不要硬做。")
    parts.append("- 做到一半发现工作量远超预期，停下来告诉我，别硬撑。")
    parts.append(
        "- 最后汇报：改了什么、验证结果如何、审查者提了什么、还有什么遗留问题。"
    )

    return "\n".join(parts)


# --------------------------------------------------------------------- 执行


def create_branch(agent: Any, name: str, base: str, ui: Any) -> tuple[bool, str]:
    """用工具层建分支，这样审批和错误处理跟其他操作是同一套。"""
    tool = agent.registry.get("git_create_branch")
    if tool is None:
        return False, "git 工具没启用（--no-git），跳过建分支。"

    from .tools import safe_run

    result, ok = safe_run(tool, {"name": name, "base": base or None})
    return ok, result


def run(agent: Any, req: FeatureRequest, ui: Any) -> int:
    """执行整条流水线。返回退出码。

    所有输出都走 ui 层——哪一层自己 print，将来换界面时就会漏掉哪一层。
    """
    branch = req.resolved_branch()

    rows: list[tuple[str, str]] = [("需求", req.requirement.strip()[:70])]
    if req.doc:
        rows.append(("文档", req.doc))
    if req.design:
        rows.append(("设计稿", req.design))
    rows.append(("分支", branch if not req.no_branch else f"（不建分支，留在当前分支）"))
    if req.push:
        rows.append(("推送", "结束时自动推送"))
    ui.block("功能开发流水线", rows)

    if req.no_branch:
        ui.info("按要求不建分支，直接在当前分支上做。")
    else:
        ok, message = create_branch(agent, branch, req.base, ui)
        if not ok:
            ui.error(f"建分支失败：{message}")
            ui.info("可以用 --branch 指定别的名字，或加 --no-branch 直接在当前分支上做。")
            return 2
        ui.success(message)

    task = build_task(req)
    answer = agent.run(task)

    ui.horizontal_rule("流水线结束")
    if answer:
        ui.answer(answer)

    if req.push:
        _push(agent, branch, ui)
    else:
        ui.info("没有推送。确认改动没问题后，手动执行 git_push 或 `git push`。")

    plan = getattr(agent, "plan", None)
    if plan is not None and not plan.is_finished:
        ui.warn(f"计划里还有 {len(plan.remaining)} 步没完成，看一眼是不是漏了什么。")
        return 1
    return 0


def _push(agent: Any, branch: str, ui: Any) -> None:
    tool = agent.registry.get("git_push")
    if tool is None:
        ui.warn("git 工具没启用，跳过推送。")
        return
    from .tools import safe_run

    result, ok = safe_run(tool, {"branch": branch})
    if ok:
        ui.success(result)
    else:
        ui.error(f"推送失败：{result}")
        ui.info("分支和提交都在本地，修好远端配置后手动 git_push 即可。")


def describe_flags() -> str:
    return (
        "  --branch NAME    指定分支名（默认从需求里的英文词生成）\n"
        "  --base REF       从哪个提交/分支切出（默认当前 HEAD）\n"
        "  --no-branch      不建分支，直接在当前分支上做\n"
        "  --doc PATH       需求文档（Word/PDF/Markdown，用 read_document 读）\n"
        "  --design PATH    设计稿图片（用 read_design 解析）\n"
        "  --tests CMD      自测要跑的命令\n"
        "  --no-review      跳过独立审查这一步\n"
        "  --push           结束后推送到远端（默认不推）\n"
    )
