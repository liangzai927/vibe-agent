"""改 workflow.yaml —— 外科式编辑，**保留注释和排版**。

为什么不重新生成整个文件：模板里**字段全是注释**（每个字段上面一行说明），
重新生成等于把你手写的所有说明和备注都抹掉。配置文件不是数据文件，
注释是它的一半价值。

────────────────────────────────────────────────────────────────

**要处理的四种情况**，按常见程度排：

1. **键就在那儿**（`  base: main`）→ 换掉值
2. **键是注释掉的**（`  # base: main`，模板生成的长这样）→ 取消注释并设值
3. **段落在、键不在** → 在段落末尾插入
4. **段落都没有** → 追加到文件末尾

第 2 种最关键：模板生成的文件里**所有字段都是注释**，
不处理它的话「改一个已有配置」会变成「在文件末尾多出一个重复的段」。

**只改这一行，别的字符一个不动。** 这是这个模块唯一的存在理由。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Change:
    """一条要改的配置。"""

    path: str          # 例如 "mock.dir"
    value: Any
    reason: str = ""

    def describe(self) -> str:
        return f"{self.path} = {_render_value(self.value)}"


def _render_value(value: Any) -> str:
    """把 Python 值写成 YAML 标量。"""
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, list):
        if not value:
            return "[]"
        return json.dumps(value, ensure_ascii=False)
    text = str(value)
    if text == "":
        return '""'
    # 含特殊字符就加引号，免得被 YAML 解释成别的东西
    if any(ch in text for ch in ":#{}[]&*!|>%@`,") or text.strip() != text:
        return json.dumps(text, ensure_ascii=False)
    return text


def _split_path(path: str) -> tuple[str, str]:
    path = (path or "").strip()
    if "." not in path:
        raise ValueError(f"配置路径要写成 段.键 的形式，收到的是 {path!r}")
    section, _, key = path.partition(".")
    if not section or not key:
        raise ValueError(f"配置路径不完整：{path!r}")
    return section, key


# --------------------------------------------------------------- 读写


def set_values(text: str, changes: list[Change]) -> tuple[str, list[str]]:
    """在一份 YAML 文本上应用若干改动。返回 ``(新文本, 说明)``。

    逐条应用——**不重新解析再序列化**，那样会丢掉所有注释。
    """
    notes: list[str] = []
    for change in changes:
        section, key = _split_path(change.path)
        text, note = _set_one(text, section, key, change.value)
        notes.append(note)
    return text, notes


def _set_one(text: str, section: str, key: str, value: Any) -> tuple[str, str]:
    lines = text.splitlines()
    rendered = _render_value(value)

    section_at = _find_section(lines, section)

    if section_at is None:
        # 情况 4：段落都没有 → 追加
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{section}:")
        lines.append(f"  {key}: {rendered}")
        return "\n".join(lines) + "\n", f"新增段落 {section}，设 {key} = {rendered}"

    section_end = _section_end(lines, section_at)

    # 情况 1 / 2：键在那儿（可能被注释）
    for index in range(section_at + 1, section_end):
        found = _match_key(lines[index], key)
        if found is None:
            continue
        commented, indent = found
        old = _value_of(lines[index])
        lines[index] = f"{indent}{key}: {rendered}"
        verb = "取消注释并设置" if commented else "修改"
        return (
            "\n".join(lines) + "\n",
            f"{verb} {section}.{key}：{old} → {rendered}",
        )

    # 情况 3：段落里没这个键 → 插到段落末尾
    lines.insert(section_end, f"  {key}: {rendered}")
    return "\n".join(lines) + "\n", f"在 {section} 段新增 {key} = {rendered}"


def _find_section(lines: list[str], section: str) -> int | None:
    """找一个顶格的、未被注释的 ``section:``。"""
    pattern = re.compile(rf"^{re.escape(section)}\s*:\s*(#.*)?$")
    for index, line in enumerate(lines):
        if pattern.match(line):
            return index
    return None


def _section_end(lines: list[str], start: int) -> int:
    """段落从哪结束：下一个顶格的非空、非注释行。"""
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[:1].isspace():
            return index
    return len(lines)


def _match_key(line: str, key: str) -> tuple[bool, str] | None:
    """这一行是不是 ``key:``？

    返回 ``(是否被注释掉, 缩进)``；不是这个键就返回 None。
    **必须区分「注释掉的键」**——模板生成的文件里字段全是注释。
    """
    match = re.match(r"^(\s*)(#\s*)?([\w-]+)\s*:", line)
    if not match or match.group(3) != key:
        return None
    commented = match.group(2) is not None
    # **缩进取「注释符号之前」的那段空白**——注释和它描述的键在同一层级，
    # `  # base: main` 取消注释后应该是 `  base: main` 而不是 4 个空格。
    # （第一版我在这上面多加了 2 个空格，生成出 `    base: develop`。）
    indent = match.group(1)
    return commented, indent


def _value_of(line: str) -> str:
    """取出一行里现有的值（用于显示 diff）。"""
    match = re.match(r"^\s*#?\s*[\w-]+\s*:\s*(.*)$", line)
    if not match:
        return "（未知）"
    value = match.group(1).strip()
    return value if value else "（空）"


# --------------------------------------------------------------- 落盘


def apply_changes(
    workspace: str | Path,
    changes: list[Change],
    *,
    source: str = "agent",
) -> tuple[Path, list[str]]:
    """把改动写进项目配置，并记一笔「谁在什么时候因为什么改的」。

    **可归因**是必需的：半年后看到 `gate: warn`，你得知道
    「这是当初因为样式测试跑不起来才降的，不是随便定的」——
    否则你会不敢动它，或者误以为它该是 strict。
    """
    root = Path(workspace)
    config = root / ".vibe" / "workflow.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)

    original = config.read_text(encoding="utf-8") if config.is_file() else ""
    updated, notes = set_values(original, changes)

    config.write_text(updated, encoding="utf-8")

    # 变更日志放数据根：它是 agent 的记录，不是团队约定。
    # （配置本身仍然写进项目的 .vibe/workflow.yaml——那个是要进 git 的。）
    from .paths import project_dir

    log = project_dir(root) / "workflow-history.json"
    entries: list[dict[str, Any]] = []
    if log.is_file():
        try:
            entries = json.loads(log.read_text(encoding="utf-8")).get("changes") or []
        except (OSError, ValueError):
            entries = []
    entries.append(
        {
            "at": time.time(),
            "source": source,
            "changes": [
                {"path": c.path, "value": c.value, "reason": c.reason} for c in changes
            ],
        }
    )
    # **要自己建目录**——数据根那边的项目目录可能还不存在
    # （以前写项目的 .vibe/ 时它是现成的，搬过来就漏了这一步）
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        json.dumps({"changes": entries[-200:]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return config, notes


def history(workspace: str | Path) -> list[dict[str, Any]]:
    """改配置的历史——「这条配置为什么是现在这样」。"""
    from .paths import project_dir

    path = project_dir(workspace) / "workflow-history.json"
    if not path.is_file():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("changes") or []
    except (OSError, ValueError):
        return []


def diff_text(before: str, after: str) -> str:
    """给审批框看的 diff。"""
    import difflib

    lines = difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile="当前", tofile="改后", lineterm="", n=2,
    )
    out = [line for line in lines if not line.startswith(("---", "+++"))]
    return "\n".join(out) if out else "（没有变化）"
