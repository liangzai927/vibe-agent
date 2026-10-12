"""工作流定义。

**为什么需要它**：分支命名、commit 格式、mock 约定、测试命令、提测分支——
这些是**团队约定**，不是模型该猜的东西。以前只能写在 ``AGENTS.md`` 里
靠模型自觉，改不了、也 review 不了。

定义成数据之后：

* 机器能**校验**（分支名符不符合规则，不用问模型）
* 你能**看见**当前生效的是什么、每条来自哪个文件
* agent 能**提议修改**，改动以 diff 形式给你确认
* 进 git，有历史、能回滚

────────────────────────────────────────────────────────────────

两份配置，**项目级覆盖用户级**：

    <数据根>/workflow.yaml          用户级：你个人的习惯
    <项目>/.vibe/workflow.yaml      项目级：这个团队的约定（进 git）

合并规则：**深层字典递归合并，列表整体替换**。
列表递归合并很容易出意外——「我加了第三条测试」不该变成
「原来的三条加上第三条」。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

#: 项目里的配置文件名（相对工作区）
PROJECT_CONFIG_PATHS = (".vibe/workflow.yaml", ".vibe/workflow.yml", ".vibe/workflow.json")
#: 用户级配置（相对数据根）
USER_CONFIG_NAME = "workflow.yaml"

SUPPORTED_VERSIONS = (1,)


# --------------------------------------------------------------- 字段定义


@dataclass
class Field:
    #: **生效的默认值**。它的准则是「不配就不干预」——
    #: 一个没有 workflow.yaml 的项目不该被凭空要求 commit 格式。
    default: Any
    kind: type
    doc: str
    #: 额外的校验，返回 None 表示通过，否则返回错误说明
    check: Callable[[Any], str | None] | None = None
    #: 模板里展示的**推荐值**（不填就用 default）。
    #: 和 default 分开是因为「推荐你这样配」和「不配就强制」是两件事——
    #: 混在一起会导致没配过的项目也被强制。
    example: Any = None


def _regex_check(pattern: str) -> str | None:
    try:
        re.compile(pattern)
    except re.error as exc:
        return f"不是合法的正则：{exc}"
    return None


def _branch_pattern_check(pattern: str) -> str | None:
    bad = _regex_check(pattern)
    if bad:
        return bad
    # 模板里必须能定位到工单号——没有工单号的分支名在提测流程里没法追溯
    if "{ticket}" not in pattern:
        return "分支名模板里必须含 {ticket}（否则提测时无法追溯需求）"
    return None


#: 全部字段。**这份定义同时是三样东西的来源**：
#:   1. 默认值    2. 校验规则    3. 生成带注释的模板
#: 三处各写一份的话，迟早对不上。
SCHEMA: dict[str, dict[str, Field]] = {
    "branch": {
        "pattern": Field(
            "feature/{ticket}-{slug}",
            str,
            "分支名模板。可用占位符：{ticket} 需求号、{slug} 标题短横线化、{date} 日期",
            _branch_pattern_check,
        ),
        "base": Field("main", str, "从哪个分支切出"),
        "slug_max_length": Field(40, int, "slug 部分最长多少个字符"),
        "slug_ascii": Field(
            False,
            bool,
            "分支名的 slug 是否只保留 ASCII。中文分支名 git 支持，但有些 CI 和"
            "工单系统解析不了——团队在意就设 true（此时用需求号兜底）",
        ),
        "require_clean": Field(True, bool, "切换前要求工作区干净"),
    },
    "commit": {
        "pattern": Field(
            "",
            str,
            "commit message 的正则。**留空表示不校验格式**，"
            r"推荐：^(feat|fix|refactor|test|docs|chore|perf)(\([a-z0-9-]+\))?: .{4,72}$",
            _regex_check,
            example=r"^(feat|fix|refactor|test|docs|chore|perf)(\([a-z0-9-]+\))?: .{4,72}$",
        ),
        "template": Field(
            "{type}({scope}): {subject} (#{ticket})",
            str,
            "生成 message 用的模板",
        ),
        "require_ticket": Field(
            False, bool, "message 里必须带需求号（默认不强制）", example=True
        ),
        "forbid_words": Field(
            [],
            list,
            "message 里不许出现的词（默认不限制）",
            example=["WIP", "fix bug", "update"],
        ),
        "allow_no_verify": Field(
            False, bool, "是否允许 --no-verify 绕过 pre-commit hook（默认禁止）"
        ),
    },
    "mock": {
        "strategy": Field(
            "existing_convention",
            str,
            "mock 策略：existing_convention 跟随项目既有约定；fixed 用下面的 dir；none 不生成",
        ),
        "dir": Field("src/mocks", str, "fixed 策略下 mock 放哪"),
        "detect": Field(
            ["src/mocks/**", "**/*.mock.ts", "**/*.mock.js", "msw"],
            list,
            "existing_convention 策略下，靠这些线索识别项目已有的 mock 约定",
        ),
    },
    "test": {
        "start": Field("", str, "启动项目的命令，例如 npm run dev。留空表示不自动启动"),
        "ready_port": Field(0, int, "等这个端口可连接才算就绪（0 表示不探测）"),
        "ready_timeout": Field(60, int, "就绪探测最长等多少秒"),
        "logic": Field("", str, "逻辑测试命令，例如 npm run test:unit。留空表示不跑"),
        "e2e_base_url": Field(
            "http://localhost:5173", str, "功能测试的起始地址"
        ),
        "visual_threshold": Field(
            0.01, float, "样式还原的差异阈值（0.01 = 1%）"
        ),
        "visual_freeze_time": Field(
            "2026-01-01T00:00:00",
            str,
            "截图时把页面时间冻到这一刻。不冻的话日期/时间戳每次都在变，"
            "像素差异会虚高到没法用",
        ),
        "visual_seed": Field(
            42, int, "冻住随机数的种子。不冻的话随机数据每次不同"
        ),
        "visual_stabilize": Field(
            True,
            bool,
            "截图前是否做稳定化（冻时间/停动画/固定随机/等字体）。"
            "**关掉的话像素差异数字基本没有意义**",
        ),
        "visual_designs": Field("", str, "设计稿目录。留空表示不做样式还原"),
        "gate": Field(
            "strict",
            str,
            "测试门禁：strict 不过就不许提交；warn 只警告；off 关闭",
        ),
    },
    "mcp": {
        "servers": Field(
            {},
            dict,
            "MCP 服务器。键是名字，值是 {command, args, env}。"
            "例如 MasterGo：command=npx args=[-y,@mastergo/vibe-mcp@latest,--url=http://localhost:30678]",
        ),
        "approve": Field(
            "all",
            str,
            "调 MCP 工具要不要审批：all 都要（默认，因为有的工具会改外部系统，"
            "比如改设计画布）；read 只对看起来是写操作的要；none 都不要",
        ),
    },
    "submit": {
        # **全部留空 = 不提测，直接结束。**
        # 每个口子都是可选的：没配就跳过并说明「跳过了」，不是报错。
        # 提测是往外部推东西，不该有「默认帮你推到 test」这种行为。
        "target_branch": Field(
            "", str, "提测推到哪个分支。**留空 = 不提测**（直接结束，不报错）"
        ),
        "remote": Field("origin", str, "推到哪个远端"),
        "push_mode": Field(
            "direct", str, "direct 直接推；merge_request 开 MR（需要平台 API，暂未实现）"
        ),
        "source_branch": Field(
            "", str, "从哪个分支提测。留空 = 当前分支"
        ),
        "wait_for_ci": Field(False, bool, "推完是否等 CI 结果"),
        "ci_command": Field(
            "", str, "本地 CI 校验命令（可选）。留空则不跑"
        ),
        "ci_timeout_minutes": Field(20, int, "等 CI 的最长分钟数"),
        "notify_env": Field(
            "", str, "提测通知的 webhook 地址从哪个环境变量读。留空 = 不通知"
        ),
        "notify_template": Field(
            "提测：{task} → {branch}",
            str,
            "通知内容模板。可用 {task} {branch} {commit} {status}",
        ),
        "require_gate": Field(
            True, bool, "提测前要求测试门禁通过"
        ),
    },
}

#: 枚举型字段的可选值
CHOICES: dict[str, tuple[str, ...]] = {
    "mock.strategy": ("existing_convention", "fixed", "none"),
    "test.gate": ("strict", "warn", "off"),
    "mcp.approve": ("all", "read", "none"),
    "submit.push_mode": ("direct", "merge_request"),
}


# --------------------------------------------------------------- 校验结果


@dataclass
class Problem:
    path: str
    message: str
    source: str = ""
    level: str = "error"

    def describe(self) -> str:
        where = f"（{self.source}）" if self.source else ""
        return f"[{self.level}] {self.path}: {self.message}{where}"


# --------------------------------------------------------------- 加载


class ConfigError(Exception):
    """配置文件读不了或者语法错。"""

    def __init__(self, message: str, *, source: str = "", line: int = 0) -> None:
        self.source = source
        self.line = line
        detail = f"{source}" if source else ""
        if line:
            detail += f" 第 {line} 行"
        super().__init__(f"{message}（{detail}）" if detail else message)


def _parse_yaml(text: str, source: str) -> Any:
    try:
        import yaml  # type: ignore
    except ImportError:
        raise ConfigError(
            "读 .yaml 需要 pyyaml。装一个：pip install pyyaml\n"
            "或者把配置改成 workflow.json（不需要额外依赖）",
            source=source,
        )

    try:
        return yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001 - pyyaml 的异常类型不止一种
        # pyyaml 的错里带行号，尽量传出来——配置错时「第几行」能省很多时间
        mark = getattr(exc, "problem_mark", None)
        line = (mark.line + 1) if mark is not None else 0
        problem = getattr(exc, "problem", None) or str(exc)
        raise ConfigError(f"YAML 语法错误：{problem}", source=source, line=line) from exc


def _strip_json_comments(text: str) -> str:
    """去掉 // 和 /* */ 注释。

    JSON 原本没有注释，但配置文件里「为什么定这条规则」比规则本身还重要。
    自己剥一层，代价很小。
    """
    out: list[str] = []
    index = 0
    length = len(text)
    in_string = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if text.startswith("//", index):
            while index < length and text[index] != "\n":
                index += 1
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _parse_json(text: str, source: str) -> Any:
    try:
        return json.loads(_strip_json_comments(text))
    except ValueError as exc:
        raise ConfigError(f"JSON 语法错误：{exc}", source=source) from exc


def read_config_file(path: Path) -> tuple[dict[str, Any], str]:
    """读一份配置文件。返回 ``(内容, 来源标签)``。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"读不了配置文件：{exc}", source=str(path)) from exc

    suffix = path.suffix.lower()
    data = _parse_yaml(text, str(path)) if suffix in (".yaml", ".yml") else _parse_json(text, str(path))
    if data is None:
        return {}, str(path)
    if not isinstance(data, dict):
        raise ConfigError("配置的顶层必须是一个映射（键值对）", source=str(path))
    return data, str(path)


# --------------------------------------------------------------- 合并


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """深层合并：字典递归，**列表整体替换**。

    列表递归合并很容易出意外——「我加了第三条」不该变成
    「原来的三条加上第三条」。
    """
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(_flatten(value, path))
        else:
            out[path] = value
    return out


# --------------------------------------------------------------- 工作流


@dataclass
class Workflow:
    """一份生效的工作流定义。"""

    data: dict[str, Any] = field(default_factory=dict)
    #: 扁平路径 → 来源文件。界面显示「这条是哪来的」用
    origins: dict[str, str] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    # ---- 读取

    def get(self, path: str, fallback: Any = None) -> Any:
        node: Any = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return fallback
            node = node[part]
        return node

    def origin_of(self, path: str) -> str:
        """这个键是从哪个文件来的。没配过就返回「默认值」。"""
        return self.origins.get(path, "默认值")

    def is_configured(self, path: str) -> bool:
        return path in self.origins

    @property
    def usable(self) -> bool:
        return not any(p.level == "error" for p in self.problems)

    # ---- 便捷访问（常用的那几个）

    @property
    def branch_pattern(self) -> str:
        return str(self.get("branch.pattern", ""))

    @property
    def branch_base(self) -> str:
        return str(self.get("branch.base", "main"))

    @property
    def commit_pattern(self) -> str:
        return str(self.get("commit.pattern", ""))

    @property
    def commit_template(self) -> str:
        return str(self.get("commit.template", "{type}: {subject}"))

    @property
    def test_start(self) -> str:
        return str(self.get("test.start", ""))

    @property
    def gate_mode(self) -> str:
        return str(self.get("test.gate", "strict"))

    @property
    def submit_branch(self) -> str:
        return str(self.get("submit.target_branch", ""))

    def render_branch_name(self, *, ticket: str, title: str, today: str = "") -> str:
        """按模板生成分支名。slug 部分按配置处理。"""
        title = (title or "").strip()
        if self.get("branch.slug_ascii"):
            # **必须加 re.ASCII**：不加以外 \w 是 Unicode 语义，
            # 中文会被当成「单词字符」保留下来，等于没过滤
            slug = re.sub(r"[^\w-]+", "-", title, flags=re.ASCII).strip("-")
        else:
            slug = re.sub(r"[^\w\u4e00-\u9fff]+", "-", title).strip("-")
        limit = int(self.get("branch.slug_max_length", 40) or 40)
        slug = slug[:limit].strip("-").lower()
        # 标题全被过滤掉（中文标题 + slug_ascii）时用需求号兜底，
        # 总比一个叫 "task" 的分支强——至少还能追溯
        slug = slug or (ticket or "task").lower()
        return (
            self.branch_pattern.replace("{ticket}", ticket or "no-ticket")
            .replace("{slug}", slug)
            .replace("{date}", today)
        )

    # ---- 输出

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.data, ensure_ascii=False))

    def describe(self) -> str:
        """给界面/终端看的：当前生效的值 + 来源。"""
        flat = _flatten(self.data)
        lines = []
        for key in sorted(flat):
            value = flat[key]
            if isinstance(value, list):
                value = "、".join(str(v) for v in value[:3]) + ("…" if len(value) > 3 else "")
            lines.append(f"  {key} = {value}    ← {self.origin_of(key)}")
        return "\n".join(lines)


# --------------------------------------------------------------- 加载入口


def config_paths(workspace: str | Path | None = None) -> list[tuple[Path, str]]:
    """按**优先级从低到高**返回要读的文件。后面的覆盖前面的。"""
    from .paths import agent_home

    out: list[tuple[Path, str]] = []
    user = agent_home() / USER_CONFIG_NAME
    out.append((user, "用户级"))

    if workspace is not None:
        root = Path(workspace)
        for relative in PROJECT_CONFIG_PATHS:
            candidate = root / relative
            if candidate.is_file():
                out.append((candidate, "项目级"))
                break
    return out


def defaults() -> dict[str, Any]:
    """从 SCHEMA 生成默认值。"""
    return {
        section: {name: spec.default for name, spec in fields.items()}
        for section, fields in SCHEMA.items()
    }


def validate(data: dict[str, Any], *, source: str = "") -> list[Problem]:
    """按 SCHEMA 校验。返回问题列表（空的表示通过）。"""
    problems: list[Problem] = []

    version = data.get("version")
    if version is not None and version not in SUPPORTED_VERSIONS:
        problems.append(
            Problem(
                path="version",
                message=f"不支持的版本 {version!r}，本程序认 {SUPPORTED_VERSIONS}",
                source=source,
            )
        )

    for section, fields in SCHEMA.items():
        section_data = data.get(section)
        if section_data is None:
            continue
        if not isinstance(section_data, dict):
            problems.append(
                Problem(path=section, message="应该是一个映射（键值对）", source=source)
            )
            continue

        for name, value in section_data.items():
            path = f"{section}.{name}"
            spec = fields.get(name)
            if spec is None:
                # 未知键只警告不报错——向前兼容，也容忍手误之外的扩展
                problems.append(
                    Problem(
                        path=path,
                        message="未知配置项，会被忽略（是不是拼错了？）",
                        source=source,
                        level="warn",
                    )
                )
                continue

            if not _type_ok(value, spec.kind):
                problems.append(
                    Problem(
                        path=path,
                        message=f"类型不对：应该是 {spec.kind.__name__}，实际是 {type(value).__name__}",
                        source=source,
                    )
                )
                continue

            choice = CHOICES.get(path)
            if choice is not None and value not in choice:
                problems.append(
                    Problem(
                        path=path,
                        message=f"只能是 {' / '.join(choice)} 之一，实际是 {value!r}",
                        source=source,
                    )
                )
                continue

            if spec.check is not None:
                message = spec.check(value)
                if message:
                    problems.append(Problem(path=path, message=message, source=source))

    # 跨字段的检查
    problems.extend(_cross_checks(data, source))
    return problems


def _type_ok(value: Any, kind: type) -> bool:
    if kind is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind is int:
        # YAML 里写 60 是 int，写 60.0 也能接受
        return isinstance(value, int) and not isinstance(value, bool)
    if kind is bool:
        return isinstance(value, bool)
    return isinstance(value, kind)


def _cross_checks(data: dict[str, Any], source: str) -> list[Problem]:
    """单个字段看不出来的问题。"""
    problems: list[Problem] = []
    test = data.get("test") or {}
    submit = data.get("submit") or {}
    commit = data.get("commit") or {}

    if test.get("ready_port") and not test.get("start"):
        problems.append(
            Problem(
                path="test.ready_port",
                message="配了就绪端口，却没有 test.start——没人会去启动它",
                source=source,
                level="warn",
            )
        )

    gate = test.get("gate")
    if gate in ("strict", "warn") and not (test.get("logic") or test.get("visual_designs")):
        problems.append(
            Problem(
                path="test.gate",
                message=f"门禁是 {gate}，但没配任何测试（logic / visual_designs）——永远不会有东西可拦",
                source=source,
                level="warn",
            )
        )

    if submit.get("wait_for_ci") and not submit.get("target_branch"):
        problems.append(
            Problem(
                path="submit.target_branch",
                message="要等 CI 却没配提测分支",
                source=source,
            )
        )

    if commit.get("require_ticket") and "{ticket}" not in str(commit.get("template", "")):
        problems.append(
            Problem(
                path="commit.template",
                message="要求带需求号，但模板里没有 {ticket}",
                source=source,
            )
        )

    return problems


def load(workspace: str | Path | None = None) -> Workflow:
    """加载生效的工作流。

    **读不了就抛 ConfigError**（带文件名和行号）——配置错了要让用户立刻知道，
    不能悄悄退回默认值跑起来，那会让人以为规则生效了。
    """
    merged = defaults()
    origins: dict[str, str] = {}
    problems: list[Problem] = []
    sources: list[str] = []

    for path, label in config_paths(workspace):
        if not path.is_file():
            continue
        data, source = read_config_file(path)
        # 模板里段落是真实的、字段全注释，所以整段会解析成 null。
        # 那表示「这一段没配」，不是「把它设成 null」——直接丢掉。
        data = {k: v for k, v in data.items() if v is not None}
        sources.append(source)
        problems.extend(validate(data, source=source))

        merged = deep_merge(merged, data)
        # 记录每个叶子键来自哪（后读的覆盖先读的）
        for key in _flatten(data):
            origins[key] = label

    workflow = Workflow(data=merged, origins=origins, problems=problems, sources=sources)
    return workflow


def template() -> str:
    """生成一份带注释的 YAML 模板。

    **所有值都是注释掉的**，这是一份「参考文档」而不是「已经配好的文件」。

    两个理由：

    * **来源追踪才有意义**。如果模板把默认值全写进去，每个键的来源都会是
      「项目级」，你就看不出自己到底改了什么。
    * **不会被旧默认值钉住**。写死之后，以后工具改了默认值，老项目的文件
      还压着旧值——这种问题过半年才发现。

    想改哪条就取消哪条的注释。
    """
    lines = [
        "# vibe-agent 工作流定义",
        "#",
        "# 这是**团队约定**：分支怎么命名、commit 什么格式、mock 放哪、",
        "# 测试怎么跑、提测推到哪。写下来 agent 就照着做，不用每次交代。",
        "#",
        "# 放在  <项目>/.vibe/workflow.yaml  并提交进 git，",
        "# 或者 <数据根>/workflow.yaml 作为你个人的默认值。",
        "# 项目级覆盖用户级；字典递归合并，列表整体替换。",
        "#",
        "# **字段都在注释里**——这是参考，不是已经配好的文件。",
        "# 想改哪条就把那条的注释去掉（段落名不用动，它们是真的）。",
        "# 不改的走内置默认值。改完跑 `va workflow --check` 校验。",
        "",
        "# version: 1",
        "",
    ]
    for section, fields in SCHEMA.items():
        # **段落名不注释**：不然取消一个字段的注释后它没有父节点，
        # 生成出来的配置直接是错的（YAML 会把它当成顶层键）。
        lines.append(f"{section}:")
        for name, spec in fields.items():
            lines.append(f"  # {spec.doc}")
            choice = CHOICES.get(f"{section}.{name}")
            if choice:
                lines.append(f"  # 可选：{' / '.join(choice)}")
            shown = spec.default if spec.example is None else spec.example
            for line in _yaml_lines(name, shown, indent=2):
                lines.append(f"  # {line.strip()}")
            lines.append("  #")
        # 段落里全是注释时，YAML 会解析成 null。load 会把 null 段落当「没配」丢掉
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _blank_for(spec: Field) -> Any:
    if spec.kind is str:
        return ""
    if spec.kind is bool:
        return False
    if spec.kind is int:
        return 0
    if spec.kind is float:
        return 0.0
    return []


def _yaml_lines(name: str, value: Any, *, indent: int) -> list[str]:
    pad = " " * indent
    if isinstance(value, list):
        if not value:
            return [f"{pad}{name}: []"]
        out = [f"{pad}{name}:"]
        for item in value:
            out.append(f"{pad}  - {_scalar(item)}")
        return out
    if isinstance(value, bool):
        return [f"{pad}{name}: {'true' if value else 'false'}"]
    return [f"{pad}{name}: {_scalar(value)}"]


def _scalar(value: Any) -> str:
    if value == "":
        return '""'
    if isinstance(value, str):
        # 含特殊字符就加引号，免得被 YAML 解释成别的东西
        if any(ch in value for ch in ":#{}[]&*!|>%@`") or value.strip() != value:
            return json.dumps(value, ensure_ascii=False)
        return value
    return json.dumps(value, ensure_ascii=False)
