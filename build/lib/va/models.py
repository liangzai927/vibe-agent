"""模型目录：从服务商拉取可用模型并缓存下来。

**为什么要存**：换模型是很常见的操作——便宜的干活、贵的看图、本地的不花钱。
但每次都要去翻文档找模型名太烦。首次配置时拉一次，之后 ``/models`` 就能看到，
``/model`` 直接切。

**为什么优先问接口而不是内置清单**：内置清单一定会过时。OpenAI 兼容服务基本都
提供 ``GET /models``，直接问它最准。拉不到时才让用户手输，而不是编一个可能错的。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

CATALOG_FILENAME = ".models.json"
FETCH_TIMEOUT = 20.0

# 只用来给**建议值**，不用来当清单。真正的清单从 /models 拉。
SUGGESTED = {
    "deepseek.com": "deepseek-chat",
    "openai.com": "gpt-4o",
    "localhost": "qwen2.5-coder:14b",
    "127.0.0.1": "qwen2.5-coder:14b",
    "ollama": "qwen2.5-coder:14b",
}

# 这些模型明显不支持工具调用，列出来但排在后面，免得被当成默认值。
_LIKELY_NO_TOOLS = ("embedding", "whisper", "tts", "dall-e", "moderation", "rerank")


def suggest_model(base_url: str, fallback: str = "deepseek-chat") -> str:
    lowered = (base_url or "").lower()
    for needle, model in SUGGESTED.items():
        if needle in lowered:
            return model
    return fallback


@dataclass
class ModelInfo:
    """一个模型的元信息。

    重点是 ``input_modalities``——它直接告诉我们这个模型能不能读图。
    这比让用户自己去猜「哪个模型支持视觉」靠谱得多。
    """

    id: str
    name: str = ""
    input_modalities: list[str] = field(default_factory=list)
    context_window: int = 0

    @property
    def supports_image(self) -> bool:
        return "image" in self.input_modalities

    @property
    def summary(self) -> str:
        bits = []
        if self.name and self.name != self.id:
            bits.append(self.name)
        if self.context_window:
            bits.append(f"{self.context_window // 1000}k 上下文")
        if self.supports_image:
            bits.append("可读图")
        return "  ".join(bits)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "input_modalities": self.input_modalities,
            "context_window": self.context_window,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ModelInfo":
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            input_modalities=[str(m) for m in data.get("input_modalities", [])],
            context_window=int(data.get("context_window", 0) or 0),
        )


@dataclass
class FetchResult:
    ok: bool
    models: list[ModelInfo] = field(default_factory=list)
    error: str = ""

    def why(self) -> str:
        if self.ok:
            return ""
        return self.error or "未知原因"


@dataclass
class ModelCatalog:
    """一份缓存的模型清单。"""

    base_url: str
    models: list[ModelInfo]
    fetched_at: float = field(default_factory=time.time)
    source: str = "api"  # api | manual

    @property
    def ids(self) -> list[str]:
        return [m.id for m in self.models]

    @property
    def vision_candidates(self) -> list[ModelInfo]:
        """能读图的模型。用来给用户推荐视觉模型。"""
        return [m for m in self.models if m.supports_image]

    def find(self, model_id: str) -> ModelInfo | None:
        for model in self.models:
            if model.id == model_id:
                return model
        return None

    @property
    def age_text(self) -> str:
        delta = max(0.0, time.time() - self.fetched_at)
        if delta < 3600:
            return f"{int(delta // 60)} 分钟前"
        if delta < 86400:
            return f"{int(delta // 3600)} 小时前"
        return f"{int(delta // 86400)} 天前"

    def to_dict(self) -> dict:
        return {
            "base_url": self.base_url,
            "models": [m.to_dict() for m in self.models],
            "fetched_at": self.fetched_at,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ModelCatalog":
        return cls(
            base_url=str(data.get("base_url", "")),
            models=[
                ModelInfo.from_dict(m) if isinstance(m, dict) else ModelInfo(id=str(m))
                for m in data.get("models", [])
                if m
            ],
            fetched_at=float(data.get("fetched_at", 0.0)),
            source=str(data.get("source", "api")),
        )


def catalog_path(agent_dir: Path) -> Path:
    return Path(agent_dir) / CATALOG_FILENAME


def load_catalog(agent_dir: Path) -> ModelCatalog | None:
    path = catalog_path(agent_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return ModelCatalog.from_dict(data)


def save_catalog(agent_dir: Path, catalog: ModelCatalog) -> Path:
    path = catalog_path(agent_dir)
    path.write_text(
        json.dumps(catalog.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _sort_key(name: str) -> tuple[int, str]:
    """把明显不能干活的模型排到后面，免得被当成默认值。"""
    lowered = name.lower()
    useless = any(token in lowered for token in _LIKELY_NO_TOOLS)
    return (1 if useless else 0, lowered)


def fetch_models(
    base_url: str,
    api_key: str,
    *,
    timeout: float = FETCH_TIMEOUT,
) -> FetchResult:
    """问服务商要可用模型列表。

    失败时**不抛异常**——首次配置向导里，「拉不到」是很正常的一种情况
    （服务商不支持这个接口、网络不通、key 已经错了），需要的是能读懂的原因，
    而不是一个 traceback。
    """
    if not (base_url or "").strip():
        return FetchResult(False, error="没有填写 base_url")
    if not (api_key or "").strip():
        return FetchResult(False, error="没有填写 API key")

    try:
        import openai
        from openai import OpenAI
    except ImportError:
        return FetchResult(False, error="没有安装 openai SDK")

    try:
        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,  # 拉清单没必要重试，失败了就让用户手输
        )
        page = client.models.list()
    except openai.AuthenticationError:
        return FetchResult(False, error="API key 被拒绝（401）——检查 key 和服务地址是否配套")
    except openai.NotFoundError:
        return FetchResult(False, error="这个服务没有 /models 接口，需要手动填模型名")
    except openai.APIConnectionError:
        return FetchResult(False, error=f"连不上 {base_url}——检查地址和网络")
    except openai.APIStatusError as exc:
        return FetchResult(False, error=f"服务返回 {exc.status_code}：{str(exc)[:120]}")
    except Exception as exc:  # noqa: BLE001 - 向导里任何失败都该变成一句话
        return FetchResult(False, error=f"{type(exc).__name__}: {str(exc)[:120]}")

    seen: dict[str, ModelInfo] = {}
    for item in page.data:
        model_id = str(getattr(item, "id", "") or "")
        if not model_id or model_id in seen:
            continue
        # 这些字段不是 OpenAI 规范的一部分，用 getattr 兜底——
        # DeepSeek 会返回，别家可能没有，没有就当作纯文本模型。
        raw = getattr(item, "model_extra", None) or {}
        modalities = raw.get("input_modalities") or []
        seen[model_id] = ModelInfo(
            id=model_id,
            name=str(raw.get("name") or ""),
            input_modalities=[str(m) for m in modalities],
            context_window=int(raw.get("context_window") or 0),
        )

    if not seen:
        return FetchResult(False, error="服务返回了空的模型列表")
    models = sorted(seen.values(), key=lambda m: _sort_key(m.id))
    return FetchResult(True, models=models)
