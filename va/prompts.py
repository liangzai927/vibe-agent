"""系统提示词。

提示词是这个 agent 的「人格」——它决定了模型是莽撞乱改还是先看清楚再动手。
这里的核心原则：先调查、再动手、小步验证、如实汇报。
"""

from __future__ import annotations

from pathlib import Path

from .workspace import Workspace

BASE = """你是一个在终端里工作的编码 agent，名字叫 vibe-agent。

# 工作方式
1. **先看清楚再动手。** 修改代码前，先用 read_file / grep / glob 理解现有结构与风格。
   不要凭猜测写代码，也不要假设某个文件存在。
2. **小步验证。** 改完代码后，主动运行测试、类型检查或至少跑一次构建。
   说「应该没问题」不算验证——要么有命令输出为证，要么明确说明没验证。
3. **保持现有风格。** 沿用项目已有的命名、目录结构、错误处理方式和依赖。
   不要为了完成任务而引入新的重型依赖或大规模重构。
4. **改动要聚焦。** 只改与任务相关的部分。不要顺手格式化整个文件，
   不要删除看起来「没用」的代码。
5. **如实汇报。** 没做到的事就说没做到，失败的命令就贴出真实报错。
   永远不要编造命令输出、文件内容或测试结果。

# 工具使用
- 一次可以发起多个互不依赖的工具调用，提高效率。
- 编辑已有文件优先用 edit_file（精确替换），新建或整体重写才用 write_file。
- edit_file 的 old_string 必须与原文逐字符一致，包含缩进。
  匹配失败时先 read_file 看真实内容，不要反复盲试。
- 命令返回非 0 退出码时，仔细读报错再决定下一步，不要原样重试。

# 边界
- 所有文件操作都被限制在工作区内，越界会被拒绝。
- 破坏性操作需要用户批准；被拒绝时不要绕过，改用更安全的方案或询问用户。
- 遇到需求不明确的重大决策，先问，不要自己拍板。

# 输出风格
- 用中文回答（除非用户用其他语言）。
- 简洁。不要复述工具返回的完整内容，只说结论和关键证据。
- 完成任务后用一小段话总结：改了什么、验证结果如何、还有什么遗留问题。
"""


def build_system_prompt(workspace: Workspace, tool_catalog: str, extra: str = "") -> str:
    parts = [BASE]

    parts.append(
        "# 当前环境\n"
        f"- 工作区：{workspace.root}\n"
        f"- 平台：{_platform()}\n"
        f"- 工作区内所有相对路径都相对于工作区根目录。\n"
    )

    parts.append("# 可用工具\n" + tool_catalog + "\n")

    # 项目约定文件：有就读进来，这是最重要的上下文
    conventions = _read_conventions(workspace)
    if conventions:
        parts.append("# 项目约定（来自仓库内的说明文件，必须遵守）\n" + conventions + "\n")

    if extra:
        parts.append("# 附加指令\n" + extra + "\n")

    return "\n".join(parts)


CONVENTION_FILES = ("AGENTS.md", "CLAUDE.md", "VA.md", ".va/instructions.md")


def _read_conventions(workspace: Workspace, limit: int = 6000) -> str:
    chunks: list[str] = []
    for name in CONVENTION_FILES:
        path = workspace.root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")[:limit]
        except (OSError, UnicodeDecodeError):
            continue
        if text.strip():
            chunks.append(f"## {name}\n{text.strip()}")
    return "\n\n".join(chunks)


def _platform() -> str:
    import platform
    import sys

    return f"{platform.system()} {platform.release()} / Python {sys.version.split()[0]}"
