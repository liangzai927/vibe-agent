"""配置加载：环境变量 + .env 文件。"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
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
    # ---- 视觉模型（用于「截图 vs 设计图」比对，可选）----
    # 主模型多数不支持图片（如 deepseek-chat），所以单独配一个多模态模型。
    vision_model: str = ""
    vision_base_url: str = ""
    vision_api_key: str = ""

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
