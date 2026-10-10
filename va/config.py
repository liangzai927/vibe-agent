"""配置加载：环境变量 + .env 文件。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

_TRUTHY = {"1", "true", "yes", "on"}


def load_dotenv(path: Path) -> None:
    """极简 .env 读取器：支持 KEY=VALUE、# 注释、export 前缀。

    已存在的环境变量优先，不会被 .env 覆盖。
    """
    if not path.is_file():
        return
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return

    for raw in raw_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if not key:
            continue
        value = value.strip()
        # 去掉行尾注释（仅在值未被引号包裹时）
        if value and value[0] not in "\"'":
            value = value.split(" #", 1)[0].strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _split_paths(raw: str | None) -> list[str]:
    """把 VA_READ_DIRS 拆成目录列表。

    用 os.pathsep 而不是逗号：POSIX 上的文件名可以合法地含逗号，
    而 pathsep（POSIX 是 ``:``）是这类列表的通用约定。
    也容忍用户顺手写逗号——Windows 上逗号更常见。
    """
    if not raw:
        return []
    parts: list[str] = []
    for chunk in raw.replace(",", os.pathsep).split(os.pathsep):
        chunk = chunk.strip()
        if chunk:
            parts.append(os.path.expanduser(chunk))
    # 去重但保持顺序
    seen: set[str] = set()
    out: list[str] = []
    for item in parts:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


@dataclass(slots=True)
class Config:
    workspace: Path
    model: str
    api_key: str
    base_url: str
    temperature: float = 0.0
    max_iterations: int = 50
    max_tokens: int | None = None
    auto_approve: bool = False
    show_thinking: bool = True
    # 估算 token 超过这个数就自动压缩历史（按字符数/3 粗估）
    context_budget: int = 60_000
    # 子 agent 最多嵌套几层。默认 1 = 主 agent 能委派，子 agent 不能再往下派。
    # 刻意不鼓励调大：失控的成本和难以追踪的调用链，换不来多少实际收益。
    max_delegate_depth: int = 1
    # ---- 允许读取工作区外文件的目录 ----
    # 设计稿、需求文档经常不在项目里（下载目录、桌面）。列在这里的目录
    # 读起来不再逐次询问；不列的每次都会问，并把完整路径摆给用户看。
    # 分隔符用 os.pathsep（POSIX 是 :，Windows 是 ;）。
    read_dirs: list[str] = field(default_factory=list)
    # 读工作区外文件的策略：all（默认，不问）/ listed（只放行列出的）/ workspace（不许越界）
    read_scope: str = "all"
    # 会话同步到 git 远端（可选）。为空表示只存本地。
    session_remote: str = ""
    # ---- 视觉模型（用于「截图 vs 设计图」比对，可选）----
    # 主模型多数不支持图片（如 deepseek-chat），所以单独配一个多模态模型。
    vision_model: str = ""
    vision_base_url: str = ""
    vision_api_key: str = ""

    @staticmethod
    def env_file(pkg_root: Path | None = None) -> Path:
        """配置写哪儿。

        顺序和 ``load()`` 保持一致：``VA_ENV_FILE`` 优先，
        否则用包目录下的 ``.env``。
        """
        explicit = (os.environ.get("VA_ENV_FILE") or "").strip()
        if explicit:
            return Path(explicit).expanduser()
        root = pkg_root or Path(__file__).resolve().parent.parent
        return root / ".env"

    @property
    def has_vision(self) -> bool:
        return bool(self.vision_model.strip())

    def vision_config(self) -> "Config":
        """派生出一个走视觉模型的配置副本（未配置则原样返回）。"""
        if not self.has_vision:
            return self
        return replace(
            self,
            model=self.vision_model.strip(),
            base_url=(self.vision_base_url or self.base_url).strip(),
            api_key=(self.vision_api_key or self.api_key).strip(),
        )

    @classmethod
    def load(cls, workspace: str | Path | None = None) -> "Config":
        pkg_root = Path(__file__).resolve().parent.parent

        # VA_ENV_FILE 显式指定配置文件。
        # 有了它，开发/测试/多套配置才能把配置指向别处——否则任何一次
        # 「跑一下试试」都会写到用户真实的那份 .env 上。
        explicit = (os.environ.get("VA_ENV_FILE") or "").strip()
        if explicit:
            load_dotenv(Path(explicit).expanduser())
        else:
            load_dotenv(Path.cwd() / ".env")
            load_dotenv(pkg_root / ".env")

        ws = workspace or os.environ.get("VA_WORKSPACE") or Path.cwd()
        ws_path = Path(ws).expanduser().resolve()

        # 工作区里的 .env 也要读。
        # 容器里 cwd 是 /app，用户仓库挂在 /workspace —— 只读 cwd 的话，
        # 用户照 .env.example 在仓库里配的东西完全不生效，很难排查。
        # 前面已经 setdefault 过的键不会被覆盖，所以环境变量优先级仍然最高。
        load_dotenv(ws_path / ".env")

        api_key = (
            os.environ.get("VA_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
            or ""
        ).strip()
        base_url = (
            os.environ.get("VA_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or DEFAULT_BASE_URL
        ).strip()
        model = (os.environ.get("VA_MODEL") or DEFAULT_MODEL).strip()

        max_tokens_raw = os.environ.get("VA_MAX_TOKENS", "").strip()

        return cls(
            workspace=ws_path,
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=float(os.environ.get("VA_TEMPERATURE", "0") or 0),
            max_iterations=int(os.environ.get("VA_MAX_ITERATIONS", "50") or 50),
            max_tokens=int(max_tokens_raw) if max_tokens_raw.isdigit() else None,
            auto_approve=(os.environ.get("VA_AUTO_APPROVE", "").strip().lower() in _TRUTHY),
            show_thinking=(os.environ.get("VA_SHOW_THINKING", "1").strip().lower() in _TRUTHY),
            context_budget=int(os.environ.get("VA_CONTEXT_BUDGET", "60000") or 60000),
            max_delegate_depth=max(0, int(os.environ.get("VA_MAX_DELEGATE_DEPTH", "1") or 1)),
            read_dirs=_split_paths(os.environ.get("VA_READ_DIRS")),
            read_scope=(os.environ.get("VA_READ_SCOPE") or "all").strip().lower(),
            session_remote=(os.environ.get("VA_SESSION_REMOTE") or "").strip(),
            vision_model=(os.environ.get("VA_VISION_MODEL") or "").strip(),
            vision_base_url=(os.environ.get("VA_VISION_BASE_URL") or "").strip(),
            vision_api_key=(os.environ.get("VA_VISION_API_KEY") or "").strip(),
        )

    def require_key(self) -> None:
        if not self.api_key:
            raise SystemExit(
                "缺少 API key。\n"
                "  方式一：在项目根目录创建 .env（参考 .env.example）\n"
                "  方式二：export VA_API_KEY=sk-xxx\n"
            )


def write_env_values(path: Path, values: dict[str, str]) -> Path:
    """把键值写进 .env，**保留文件里已有的其他内容和注释**。

    刻意做原地更新而不是重写整个文件——用户可能在里面写了自己的注释、
    临时开关、别的服务的 key，重写会把这些冲掉。

    已存在的键就地替换；不存在的追加到末尾。写完全文件权限设为 600。
    """
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    remaining = dict(values)

    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        # 被注释掉的同名键也一起替换——否则用户会看到两个同名项，搞不清哪个生效
        bare = key[2:].strip() if key.startswith("# ") else key
        if bare in remaining:
            out.append(f"{bare}={remaining.pop(bare)}")
        else:
            out.append(line)

    if remaining:
        if out and out[-1].strip():
            out.append("")
        for key, value in remaining.items():
            out.append(f"{key}={value}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)  # 里面有 key
    except OSError:
        pass
    return path
