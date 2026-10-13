"""团队约定的校验与生成：分支名、commit message。

规则来自 `workflow.yaml`（阶段 A），这里只负责**执行**它。

────────────────────────────────────────────────────────────────

**为什么要在工具层校验，而不是在提示词里说「请遵守命名规范」**

和测试门禁同一个道理：提示词是祈祷。`git_create_branch` 拿到不合规的名字
**直接拒绝并给出合规示例**，才是真的约束。

**拒绝时必须给可用的例子。** 只说「不合规」会让人（和模型）反复试错——
给出「照这个改：`feature/ORD-312-order-filter`」才是有用的反馈。

**没配规则就不校验。** 不能因为一个项目没写 workflow.yaml 就挡住它干活。
"""

from __future__ import annotations

import re
import time
from typing import Any

#: commit 的 type 白名单。约定的地方在 pattern 里，这里只是生成时的默认值。
DEFAULT_TYPES = ("feat", "fix", "refactor", "test", "docs", "chore", "perf")


def _get(workflow, path: str, fallback: Any = None) -> Any:
    if workflow is None:
        return fallback
    return workflow.get(path, fallback)


# --------------------------------------------------------------- 分支名


def branch_pattern(workflow) -> str:
    return str(_get(workflow, "branch.pattern", "") or "").strip()


def build_branch_name(
    workflow, *, ticket: str = "", title: str = "", today: str = ""
) -> str:
    """按模板生成分支名。直接用工作流对象自己的实现，避免两套逻辑。"""
    if workflow is not None and hasattr(workflow, "render_branch_name"):
        return workflow.render_branch_name(
            ticket=ticket, title=title, today=today or time.strftime("%Y%m%d")
        )
    # 没配工作流时的合理默认
    slug = re.sub(r"[^\w\u4e00-\u9fff]+", "-", (title or "").strip()).strip("-").lower()
    return f"feature/{ticket or 'no-ticket'}-{slug or 'task'}"


def check_branch_name(name: str, workflow) -> str:
    """合规返回空串，否则返回**可直接照做**的修改建议。

    分支名比 commit message 难改（改一次要重推），所以拒绝的措辞要能给
    一个能直接用的名字。
    """
    pattern = branch_pattern(workflow)
    if not pattern:
        return ""      # 没配规则就不管

    example = build_branch_name(workflow, ticket="ORD-312", title="示例标题")

    # 模板里有占位符，没法直接当正则用——先把它变成「形状匹配」：
    # 把 {ticket} {slug} {date} 换成宽松的通配，其余部分原样（转义）
    shape = pattern
    for token in ("{ticket}", "{slug}", "{date}"):
        shape = shape.replace(token, "\x00")
    parts = [re.escape(part) for part in shape.split("\x00")]
    regex = "^" + "[^/]+".join(parts) + "$"

    if re.match(regex, name):
        return ""

    reasons = []
    if "{ticket}" in pattern and "/" in name:
        # 前缀对不对？
        prefix = pattern.split("{")[0]
        if prefix and not name.startswith(prefix):
            reasons.append(f"应该以 {prefix!r} 开头")
    return (
        f"分支名 {name!r} 不符合团队约定（模板 {pattern!r}）。\n"
        f"照这个改：{example}\n"
        f"或者用 ticket + title 让工具自动生成。"
    )


# --------------------------------------------------------------- commit


def commit_pattern(workflow) -> str:
    return str(_get(workflow, "commit.pattern", "") or "").strip()


def build_commit_message(
    workflow,
    *,
    kind: str = "feat",
    scope: str = "",
    subject: str = "",
    ticket: str = "",
) -> str:
    """按模板拼一条 message。"""
    template = str(
        _get(workflow, "commit.template", "{type}({scope}): {subject} (#{ticket})")
        or "{type}({scope}): {subject}"
    )
    scope_part = f"({scope})" if scope else ""
    text = (
        template.replace("{type}", kind or "feat")
        .replace("{scope}", scope or "")
        .replace("{subject}", subject or "")
        .replace("{ticket}", ticket or "")
    )
    # 没 scope 时模板里的 "()" 要清掉，否则出来是 "feat(): xxx"
    text = re.sub(r"\(\s*\)", "", text)
    # 没 ticket 时尾部的 "(#)" 也要清掉
    text = re.sub(r"\s*\(#\)\s*$", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def check_commit_message(message: str, workflow) -> str:
    """合规返回空串，否则返回修改建议。"""
    text = (message or "").strip()
    if not text:
        return "提交信息不能为空。"

    pattern = commit_pattern(workflow)
    example = build_commit_message(
        workflow, kind="feat", scope="order", subject="列表支持按状态筛选", ticket="ORD-312"
    )

    if pattern:
        try:
            compiled = re.compile(pattern)
        except re.error:
            compiled = None      # 配置里的正则本身坏了——那是阶段 A 该校验的事
        if compiled is not None and not compiled.match(text):
            return (
                f"提交信息不符合团队约定。\n"
                f"约定格式：{pattern}\n"
                f"照这个改：{example}"
            )

    # 禁用词
    forbidden = _get(workflow, "commit.forbid_words", []) or []
    lowered = text.lower()
    for word in forbidden:
        word = str(word).strip()
        if word and word.lower() in lowered:
            return (
                f"提交信息里不该出现 {word!r}——它没说清做了什么。\n"
                f"照这个改：{example}"
            )

    # 必须带需求号
    if _get(workflow, "commit.require_ticket", False):
        if not re.search(r"(#|[A-Z]{2,}-\d+)", text):
            return (
                f"提交信息要带需求号（团队约定 commit.require_ticket = true）。\n"
                f"照这个改：{example}"
            )

    return ""


def branch_requires_clean(workflow) -> bool:
    return bool(_get(workflow, "branch.require_clean", False))


def branch_base(workflow) -> str:
    return str(_get(workflow, "branch.base", "") or "").strip()
