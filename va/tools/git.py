"""Git 工具：状态、差异、分支、提交、推送。

全部通过参数数组调用 git（不经过 shell），避免命令注入。
写操作标记为需要审批，读操作直接放行。
"""

from __future__ import annotations

import subprocess
from typing import Any

from ..util import preview, truncate
from ..workspace import Workspace
from .base import Tool, ToolError

GIT_TIMEOUT = 180


class _GitTool(Tool):
    def __init__(self, workspace: Workspace, ctx: Any = None) -> None:
        self.workspace = workspace
        # 只有提交用得上（测试门禁）。别的 git 工具不需要，所以是可选的。
        self.ctx = ctx

    # ------------------------------------------------------------------ inner

    def _git(self, *args: str, timeout: int = GIT_TIMEOUT) -> str:
        """执行 git 命令。失败时抛出带 stderr 的 ToolError。"""
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(self.workspace.root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env={
                    **_clean_env(),
                    "GIT_PAGER": "cat",
                    "GIT_TERMINAL_PROMPT": "0",  # 不让 git 卡在交互式认证
                },
            )
        except FileNotFoundError as exc:
            raise ToolError("系统里找不到 git 可执行文件") from exc
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"git {' '.join(args)} 超时（>{timeout}s）") from exc

        # 注意：绝不能用 strip()。porcelain 格式里行首空格是有语义的
        # （' M' = 未暂存的修改，'M ' = 已暂存的修改）。strip() 会把第一行的
        # 状态位吃掉，导致文件名少一个字符、暂存状态判断完全反过来。
        stdout = (result.stdout or "").rstrip("\n")
        stderr = (result.stderr or "").rstrip("\n")
        if result.returncode != 0:
            detail = stderr.strip() or stdout.strip() or "(无输出)"
            raise ToolError(f"git {' '.join(args)} 失败：\n{detail}")
        return stdout or stderr

    def _ensure_repo(self) -> None:
        try:
            self._git("rev-parse", "--is-inside-work-tree")
        except ToolError as exc:
            raise ToolError(
                f"{self.workspace.root} 不是一个 git 仓库。"
                f"如需初始化请先运行 git_init 工具或 `git init`。"
            ) from exc

    def _current_branch(self) -> str:
        """当前分支名。仓库尚无提交或处于游离 HEAD 时给出可读说明。"""
        try:
            name = self._git("symbolic-ref", "--short", "-q", "HEAD")
            if name:
                return name
        except ToolError:
            pass
        try:
            name = self._git("rev-parse", "--abbrev-ref", "HEAD")
            if name and name != "HEAD":
                return name
        except ToolError:
            pass
        return "（游离 HEAD 或无提交）"


def _clean_env() -> dict[str, str]:
    import os

    env = dict(os.environ)
    # 清掉可能干扰的仓库级覆盖
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(key, None)
    return env


class GitStatus(_GitTool):
    name = "git_status"
    description = "查看仓库状态：当前分支、已暂存/未暂存改动、未跟踪文件。"
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, **_: Any) -> str:
        self._ensure_repo()
        branch = self._current_branch()
        porcelain = self._git("status", "--porcelain=v1", "--untracked-files=all")
        if not porcelain:
            return f"分支：{branch}\n工作区干净，没有未提交的改动。"

        staged, unstaged, untracked = [], [], []
        for line in porcelain.splitlines():
            if len(line) < 4:
                continue
            code = line[:2]
            name = line[3:]  # 前两列是 XY 状态位，第三列是分隔空格
            if code == "??":
                untracked.append(name)
            else:
                if code[0] not in " ?":
                    staged.append(f"{code[0]} {name}")
                if code[1] not in " ?":
                    unstaged.append(f"{code[1]} {name}")

        blocks = [f"分支：{branch}"]
        if staged:
            blocks.append("已暂存:\n  " + "\n  ".join(staged))
        if unstaged:
            blocks.append("未暂存:\n  " + "\n  ".join(unstaged))
        if untracked:
            blocks.append("未跟踪:\n  " + "\n  ".join(untracked[:50]))
        return "\n".join(blocks)


class GitDiff(_GitTool):
    name = "git_diff"
    description = "查看代码差异。staged=true 看已暂存改动，否则看工作区未暂存改动。"
    parameters = {
        "type": "object",
        "properties": {
            "staged": {"type": "boolean", "description": "是否查看已暂存的改动（默认 false）"},
            "path": {"type": "string", "description": "只看某个文件/目录（可选）"},
            "stat_only": {"type": "boolean", "description": "只返回统计摘要（默认 false）"},
        },
        "required": [],
    }

    def run(
        self,
        staged: bool = False,
        path: str | None = None,
        stat_only: bool = False,
        **_: Any,
    ) -> str:
        self._ensure_repo()
        args = ["diff"]
        if staged:
            args.append("--cached")
        if stat_only:
            args.append("--stat")
        if path:
            target = self.workspace.resolve(path)
            args += ["--", self.workspace.relative(target)]
        output = self._git(*args)
        if not output:
            return "没有差异。" + ("（已暂存区）" if staged else "（工作区）")
        return truncate(output, limit=20_000, label="diff")


class GitLog(_GitTool):
    name = "git_log"
    description = "查看最近提交历史（单行格式）。"
    parameters = {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "返回条数，默认 15"},
        },
        "required": [],
    }

    def run(self, limit: int = 15, **_: Any) -> str:
        self._ensure_repo()
        count = max(1, min(int(limit), 200))
        return self._git("log", f"-{count}", "--oneline", "--decorate", "--graph")


class GitCreateBranch(_GitTool):
    name = "git_create_branch"
    description = (
        "创建并切换到新分支。\n"
        "**推荐用 ticket + title 让工具按团队约定生成分支名**"
        "（规则来自 workflow.yaml 的 branch 段）——那样不会写错格式。\n"
        "直接给 name 也行，但会被按约定校验，不合规会被拒并给出合规示例。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "分支名。给了 ticket+title 时可以省略，会自动生成",
            },
            "ticket": {
                "type": "string",
                "description": "需求号，例如 ORD-312。给了就按模板生成分支名",
            },
            "title": {"type": "string", "description": "需求标题，用于生成 slug"},
            "base": {"type": "string", "description": "起点分支或 commit（可选）"},
        },
        "required": [],
    }
    requires_approval = True
    opaque = True  # 切换分支会整体替换工作树内容，无法用文件快照还原

    def run(
        self,
        name: str = "",
        ticket: str = "",
        title: str = "",
        base: str | None = None,
        **_: Any,
    ) -> str:
        self._ensure_repo()

        from .. import conventions

        workflow = getattr(self.ctx, "workflow", None)

        # 给了需求号就按模板生成——比让人手拼可靠得多
        branch = name.strip()
        if not branch and ticket.strip():
            branch = conventions.build_branch_name(
                workflow, ticket=ticket.strip(), title=title.strip()
            )
        if not branch:
            raise ToolError(
                "要么给 name，要么给 ticket + title（后者会按团队约定生成）。"
            )

        problem = conventions.check_branch_name(branch, workflow)
        if problem:
            raise ToolError(problem)

        # 约定要求工作区干净才切
        if conventions.branch_requires_clean(workflow):
            dirty = self._git("status", "--porcelain").strip()
            if dirty:
                count = len(dirty.splitlines())
                raise ToolError(
                    f"团队约定切换分支前工作区要干净（branch.require_clean = true），"
                    f"现在有 {count} 个改动没提交。\n"
                    f"先提交或还原，再切分支——带着未提交的改动切分支很容易丢东西。"
                )

        if base is None:
            configured = conventions.branch_base(workflow)
            base = configured or None

        existing = self._git("branch", "--list", branch)
        if existing.strip():
            self._git("checkout", branch)
            return f"分支 {branch} 已存在，已切换到该分支。"

        args = ["checkout", "-b", branch]
        if base:
            args.append(base.strip())
        self._git(*args)
        return f"已创建并切换到分支 {branch}" + (f"（起点 {base}）" if base else "")


class GitCheckout(_GitTool):
    name = "git_checkout"
    description = "切换到已存在的分支。"
    parameters = {
        "type": "object",
        "properties": {"branch": {"type": "string", "description": "目标分支名"}},
        "required": ["branch"],
    }
    requires_approval = True
    opaque = True  # 切换分支会整体替换工作树内容

    def run(self, branch: str, **_: Any) -> str:
        self._ensure_repo()
        self._git("checkout", branch.strip())
        return f"已切换到分支 {branch}"


class GitCommit(_GitTool):
    name = "git_commit"
    description = (
        "提交改动。默认会先 `git add -A` 暂存全部改动再提交。"
        "message 请用简洁的祈使句描述「做了什么」。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": (
                    "提交信息。给了 kind+subject 时可以省略，会按团队模板生成。"
                    "**会按约定校验格式**，不合规会被拒"
                ),
            },
            "add_all": {"type": "boolean", "description": "提交前暂存全部改动（默认 true）"},
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "只提交这些路径（可选，设置后忽略 add_all）",
            },
            "ticket": {"type": "string", "description": "需求号，会按模板拼进 message"},
            "kind": {
                "type": "string",
                "description": "feat / fix / refactor / test / docs / chore / perf",
            },
            "scope": {"type": "string", "description": "影响范围，例如 order"},
            "subject": {"type": "string", "description": "一句话说做了什么"},
            "override_gate": {
                "type": "string",
                "description": (
                    "绕过测试门禁的理由。**只有在明确知道为什么可以跳过时才用**"
                    "（比如纯文档改动）。理由会写进会话记录，事后能查。"
                ),
            },
        },
        "required": [],
    }
    requires_approval = True

    def _check_gate(self, override: str) -> str:
        """提交前的测试门禁。

        **拦在工具层，不写在提示词里**——「请确保测试通过」是祈祷；
        提交这个动作本身失败才是门禁。

        返回一段说明（豁免时用），不改动则返回空串。
        """
        ctx = self.ctx
        ledger = getattr(ctx, "ledger", None) if ctx is not None else None
        if ledger is None:
            return ""

        workflow = getattr(ctx, "workflow", None)
        from ..gate import fingerprint

        current = fingerprint(self.workspace.root)
        verdict = ledger.evaluate(workflow, current=current)

        if verdict.ok:
            return ""

        if override.strip():
            # 豁免要留痕：写进会话记录，commit message 也会带标记。
            # **不阻止**，但让事后能发现。
            if ctx is not None:
                try:
                    ctx.ui.note(f"⚠ 提交绕过了测试门禁：{override.strip()}")
                except Exception:  # noqa: BLE001
                    pass
                # 记成**严重事故**：它会清零「连续无干预」的计数。
                # 绕过门禁这件事发生过一次就不能接受，不是「经常发生才不行」。
                try:
                    from ..autonomy import AutonomyLog

                    AutonomyLog(self.workspace.root).record(
                        "gate_override", detail=override.strip()
                    )
                except Exception:  # noqa: BLE001
                    pass
            return f"[绕过测试门禁: {override.strip()}]"

        raise ToolError(
            f"{verdict.describe()}\n\n"
            "**先把测试跑绿再提交。** 如果这次改动确实不需要测试"
            "（比如只改了文档），用 override_gate 说明理由——会记进会话记录。"
        )

    def run(
        self,
        message: str = "",
        add_all: bool = True,
        files: list[str] | None = None,
        ticket: str = "",
        kind: str = "",
        scope: str = "",
        subject: str = "",
        override_gate: str = "",
        **_: Any,
    ) -> str:
        note = self._check_gate(override_gate)
        self._ensure_repo()

        from .. import conventions

        workflow = getattr(self.ctx, "workflow", None)

        text = message.strip()
        if not text and subject.strip():
            text = conventions.build_commit_message(
                workflow,
                kind=kind.strip() or "feat",
                scope=scope.strip(),
                subject=subject.strip(),
                ticket=ticket.strip(),
            )
        if not text:
            raise ToolError(
                "要么给 message，要么给 kind + subject"
                "（后者会按团队模板生成，不会写错格式）。"
            )

        problem = conventions.check_commit_message(text, workflow)
        if problem:
            raise ToolError(problem)

        if note:
            # 豁免要**留在 commit 里**，不只是在会话记录里——
            # 事后查 git log 的人也该看得见
            text = f"{text} {note}"

        if files:
            paths = [self.workspace.relative(self.workspace.resolve(p)) for p in files]
            self._git("add", "--", *paths)
        elif add_all:
            self._git("add", "-A")

        status = self._git("status", "--porcelain")
        if not status:
            return "没有可提交的改动（暂存区为空）。"

        self._git("commit", "-m", text)
        head = self._git("log", "-1", "--oneline")
        return f"提交成功：{head}"


class GitPush(_GitTool):
    name = "git_push"
    description = "推送当前分支到远端。首次推送会自动设置 upstream。"
    parameters = {
        "type": "object",
        "properties": {
            "remote": {"type": "string", "description": "远端名，默认 origin"},
            "branch": {"type": "string", "description": "分支名，默认当前分支"},
            "set_upstream": {"type": "boolean", "description": "是否设置 upstream（默认 true）"},
        },
        "required": [],
    }
    requires_approval = True

    def run(
        self,
        remote: str = "origin",
        branch: str | None = None,
        set_upstream: bool = True,
        **_: Any,
    ) -> str:
        self._ensure_repo()
        target = (branch or self._current_branch()).strip()
        if target.startswith("（"):
            raise ToolError("当前处于游离 HEAD 或无提交状态，无法确定要推送的分支名，请显式指定 branch。")

        remotes = self._git("remote")
        if remote not in remotes.split():
            raise ToolError(
                f"远端 {remote!r} 不存在。现有远端：{remotes or '(无)'}。"
                f"请先用 run_bash 执行 git remote add {remote} <url>。"
            )

        args = ["push"]
        if set_upstream:
            args += ["--set-upstream"]
        args += [remote, target]
        output = self._git(*args, timeout=300)
        return f"已推送 {target} → {remote}\n{truncate(output, limit=4000, label='push 输出')}"


class GitInit(_GitTool):
    name = "git_init"
    description = "在当前工作区初始化 git 仓库（若尚未初始化）。"
    parameters = {"type": "object", "properties": {}, "required": []}
    requires_approval = True

    def run(self, **_: Any) -> str:
        try:
            self._git("rev-parse", "--is-inside-work-tree")
            return "已经是 git 仓库，无需初始化。"
        except ToolError:
            pass
        self._git("init")
        return f"已初始化 git 仓库，当前分支 {self._current_branch()}。"


GIT_TOOLS = (
    GitStatus,
    GitDiff,
    GitLog,
    GitCreateBranch,
    GitCheckout,
    GitCommit,
    GitPush,
    GitInit,
)
