"""让 agent 把跨项目通用的东西记下来。

和 ``write_file`` 的区别：``write_file`` 改的是**这个项目**的项目文件；
``remember`` 写的是**跨项目**的长期记忆，以后每个项目、每次对话都会读到。

因为影响面这么大，它**必须经过审批**——用户得看见到底记了什么。
"""

from __future__ import annotations

from typing import Any

from .. import memory
from ..util import truncate
from .base import ToolError
from .context import WorkspaceTool


class Remember(WorkspaceTool):
    name = "remember"
    description = (
        "把信息记下来，以后每次对话都会自动读到。\n"
        "**两级作用域，想清楚再写**：\n"
        "  scope=global  所有项目通用（用户偏好、代码风格、内网镜像地址）\n"
        "  scope=project 只对这个项目成立（用的是 Django 不是 FastAPI、"
        "测试命令是 make check）\n"
        "判断标准：换一个项目还成立吗？成立就写 global，不成立就写 project。\n"
        "**不要记**：临时的中间结论、密码和密钥。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "这条记忆叫什么（会变成文件名），比如「偏好」「环境」",
            },
            "content": {
                "type": "string",
                "description": "记什么。写成给人看的短句，别写成代码注释",
            },
            "mode": {
                "type": "string",
                "enum": ["replace", "append"],
                "description": "replace 覆盖同名记忆（默认）；append 追加到已有内容后面",
            },
            "scope": {
                "type": "string",
                "enum": ["global", "project"],
                "description": (
                    "global = 所有项目通用（默认）；"
                    "project = 只对这个项目成立。换个项目还成立吗？"
                ),
            },
        },
        "required": ["name", "content"],
    }
    requires_approval = True

    def preview(self, args: dict[str, Any]) -> str | None:
        """审批框里直接显示要记的内容——比展示原始参数清楚得多。"""
        name = str(args.get("name") or "").strip()
        content = str(args.get("content") or "").strip()
        mode = args.get("mode") or "replace"
        scope = args.get("scope") or "global"
        if not name or not content:
            return None

        workspace = self.workspace.root
        target = memory.memory_dir(scope, workspace) / f"{memory.safe_name(name)}.md"
        existing = memory.find(name, scope=scope, workspace=workspace)
        verb = "追加到" if (mode == "append" and existing) else "写入"
        where = "所有项目通用" if scope == "global" else "只对这个项目生效"

        lines = [f"要{verb}记忆（{where}）：{target}", ""]
        if mode == "append" and existing:
            lines.append("── 已有内容 ──")
            lines.extend(existing.content.rstrip().splitlines()[-8:])
            lines.append("")
            lines.append("── 追加 ──")
        lines.extend(content.splitlines()[:20])
        return "\n".join(lines)

    def affected_paths(self, args: dict[str, Any]) -> list[str]:
        """记忆在 agent 自己的目录里，撤销栈管不到——如实不报，别假装能撤。"""
        return []

    def run(
        self,
        name: str,
        content: str,
        mode: str = "replace",
        scope: str = "global",
        **_: Any,
    ) -> str:
        name = (name or "").strip()
        content = (content or "").strip()
        if not name:
            raise ToolError("要给这条记忆起个名字")
        if not content:
            raise ToolError("记忆内容是空的")

        if mode not in ("replace", "append"):
            raise ToolError(f"mode 只能是 replace 或 append，收到 {mode!r}")
        if scope not in memory.SCOPES:
            raise ToolError(f"scope 只能是 global 或 project，收到 {scope!r}")

        # 记密码是最容易出的事故——记忆会进每一轮的提示词，还会被同步到远端
        if _looks_secret(content):
            raise ToolError(
                "这段内容看起来包含密钥或密码，不该写进记忆。\n"
                "记忆会进入每一轮的提示词，还可能被同步到 git 远端。\n"
                "凭据请放进 .env（它已经被 .gitignore 忽略）。"
            )

        try:
            path = memory.remember(
                name, content, mode=mode, scope=scope, workspace=self.workspace.root
            )
        except (OSError, ValueError) as exc:
            raise ToolError(f"写记忆失败：{exc}") from exc

        verb = "追加" if mode == "append" else "写入"
        if scope == "global":
            effect = "以后**每个项目**的对话都会读到它。"
        else:
            effect = f"以后这个项目（{self.workspace.root.name}）的对话都会读到它。"
        return (
            f"已{verb}记忆：{path}\n"
            f"{effect}\n"
            f"（想改或删，直接在设置里看，或者编辑那个文件）"
        )


# 明显像密钥的片段。宁可误报——误报只是让用户重写一次，
# 漏报是把密钥写进了每一轮提示词、还可能同步到远端。
_SECRET_PATTERNS = (
    "sk-", "ghp_", "github_pat_", "AKIA", "-----BEGIN",
    "password=", "passwd=", "token=", "secret=", "api_key=",
)


def _looks_secret(text: str) -> bool:
    lowered = text.lower()
    return any(pattern.lower() in lowered for pattern in _SECRET_PATTERNS)


MEMORY_TOOLS = (Remember,)
