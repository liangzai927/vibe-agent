"""首次配置向导。

**为什么要有它**：手动编辑 `.env` 有三个坑——不知道要填哪几项、不知道 base_url
该写什么、填错了只看到一句 401。向导把这三件事一次问清楚，并且**顺手把服务商
支持的模型清单拉下来存好**，之后换模型只要 ``/model`` 一条命令。

设计上刻意把「问用户」抽成可注入的 ``ask`` / ``secret``，
这样整个流程能在测试里跑完，不需要真的坐在终端前敲键盘。
"""

from __future__ import annotations

import getpass
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import run_hint
from .models import ModelCatalog, fetch_models, save_catalog, suggest_model
from .util import bold, cyan, dim, green, red, yellow

# 预设服务。第四个是出口——不预设就没有出口，用户会被困在选项里。
PRESETS = (
    ("1", "DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat"),
    ("2", "OpenAI", "https://api.openai.com/v1", "gpt-4o"),
    ("3", "本地 Ollama", "http://localhost:11434/v1", "qwen2.5-coder:14b"),
    ("4", "自定义", "", ""),
)

MODEL_LIST_SHOWN = 20


@dataclass
class SetupResult:
    ok: bool
    env_path: Path | None = None
    base_url: str = ""
    model: str = ""
    models: list[str] = field(default_factory=list)
    vision_model: str = ""
    verified: bool = False
    error: str = ""


def agent_dir() -> Path:
    """agent 自己的目录（`va/` 的上一层）。"""
    return Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------- 小工具


def _pick_preset(ask: Callable[[str], str], say: Callable[[str], None]) -> tuple[str, str, str]:
    say("")
    say(bold("  选一个模型服务："))
    for key, label, url, model in PRESETS:
        hint = url or "（手动填写）"
        say(f"    [{key}] {label:<12} {hint}")
    say("")
    raw = ask("  > ").strip() or "1"
    for key, label, url, model in PRESETS:
        if raw == key:
            return url, model, label
    # 用户可能直接粘了个 URL
    if raw.startswith("http"):
        return raw, suggest_model(raw), "自定义"
    return "", "", "自定义"


def choose_model(
    models: list,
    default: str,
    ask: Callable[[str], str],
    say: Callable[[str], None],
    *,
    interactive: bool = True,
) -> str:
    """从拉到的清单里挑一个。交互时给编号列表，非交互时用默认。

    models 是 ModelInfo 列表（或空列表——有些服务没有 /models 接口）。
    """
    ids = [getattr(m, "id", str(m)) for m in models]

    if not ids:
        if not interactive:
            return default
        typed = ask(f"  模型名 [{default}]: ").strip()
        return typed or default

    if not interactive:
        return default if default in ids else ids[0]

    say("")
    say(f"  发现 {len(ids)} 个可用模型：")
    shown = models[:MODEL_LIST_SHOWN]
    for index, model in enumerate(shown, 1):
        model_id = getattr(model, "id", str(model))
        summary = getattr(model, "summary", "")
        tags = []
        if model_id == default:
            tags.append("建议")
        if getattr(model, "supports_image", False):
            tags.append("可读图")
        suffix = dim("  ← " + " / ".join(tags)) if tags else ""
        detail = dim(f"    {summary}") if summary and summary != model_id else ""
        say(f"    [{index:>2}] {model_id}{suffix}")
        if detail:
            say(detail)
    if len(ids) > MODEL_LIST_SHOWN:
        say(dim(f"    … 还有 {len(ids) - MODEL_LIST_SHOWN} 个，输入名字可以直接指定"))

    say("")
    raw = ask(f"  选哪个？输入序号或模型名，回车用「{default}」: ").strip()
    if not raw:
        return default
    if raw.isdigit() and 1 <= int(raw) <= len(shown):
        return getattr(shown[int(raw) - 1], "id", str(shown[int(raw) - 1]))
    return raw


def _verify(base_url: str, api_key: str, model: str, timeout: float = 30.0) -> tuple[bool, str]:
    """真发一次请求确认配置能用。配完不验证等于没配。"""
    from .config import Config
    from .llm import LLM, LLMError

    probe = Config(
        workspace=Path.cwd(),
        model=model,
        api_key=api_key,
        base_url=base_url,
        max_iterations=1,
    )
    try:
        turn = LLM(probe).complete([{"role": "user", "content": "回复两个字：连通"}])
    except LLMError as exc:
        return False, str(exc)[:200]
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:180]}"
    return True, (turn.content or "").strip()[:40]


# --------------------------------------------------------------------- 主流程


def run(
    env_path: Path | None = None,
    *,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
    secret: Callable[[str], str] | None = None,
    directory: Path | None = None,
    api_key: str = "",
    base_url: str = "",
    model: str = "",
    vision_model: str = "",
    interactive: bool = True,
    verify: bool = True,
) -> SetupResult:
    """跑一遍配置向导。``interactive=False`` 时全部值必须从参数给。"""
    directory = directory or agent_dir()
    env_path = env_path or (directory / ".env")
    secret = secret or getpass.getpass

    # ---------------------------------------------------------- 1. 服务地址
    label = "自定义"
    if not base_url and interactive:
        base_url, suggested_model, label = _pick_preset(ask, say)
        model = model or suggested_model
    elif not base_url:
        return SetupResult(False, error="非交互模式下必须提供 base_url")

    if not base_url:
        typed = ask("  base_url（例如 https://api.deepseek.com/v1）: ").strip()
        if not typed:
            return SetupResult(False, error="没有填写 base_url")
        base_url = typed
        model = model or suggest_model(base_url)
    base_url = base_url.strip()

    model = model or suggest_model(base_url)

    # ---------------------------------------------------------- 2. API key
    if not api_key:
        if not interactive:
            return SetupResult(False, error="非交互模式下必须提供 api_key")
        say("")
        say(dim("  key 不会显示在屏幕上，也不会进 shell 历史。"))
        api_key = secret(f"  粘贴 {label} 的 API key: ").strip()
    if not api_key:
        return SetupResult(False, error="没有填写 API key")

    # ---------------------------------------------------------- 3. 拉模型清单
    say("")
    say(dim(f"  正在向 {base_url} 查询可用模型…"))
    fetched = fetch_models(base_url, api_key)

    catalog_source = "api"
    if fetched.ok:
        readable = [m.id for m in fetched.models if m.supports_image]
        hint = f"，其中 {len(readable)} 个能读图" if readable else ""
        say(green(f"  ✓ 拿到 {len(fetched.models)} 个模型{hint}"))
        choices = list(fetched.models)

        # 服务商的 /models 未必列全——别名（比如 deepseek-chat）常常不在里面，
        # 但它实际能用。不补进去的话界面上会出现「回车用 X」而列表里没有 X 的尴尬。
        if model and model not in [m.id for m in choices]:
            from .models import ModelInfo

            choices.insert(
                0,
                ModelInfo(id=model, name="服务商未列出这个别名，但通常可用（最后会验证）"),
            )
    else:
        say(yellow(f"  ! 拉不到模型清单：{fetched.why()}"))
        say(dim("    没关系，手动填模型名即可。"))
        choices = []
        catalog_source = "manual"

    chosen = choose_model(choices, model, ask, say, interactive=interactive)

    # ---------------------------------------------------------- 4. 视觉模型
    # 关键：如果服务商自己就有能读图的模型，直接推荐它——同一个 key 就能用，
    # 不需要用户再去别家申请。这一点很多人不知道。
    vision_candidates = [m for m in (choices or []) if getattr(m, "supports_image", False)]
    if not vision_model and vision_candidates:
        pick = vision_candidates[0]
        say("")
        say(green(f"  ✓ 发现能读图的模型：{pick.id}"))
        say(dim("    用它做视觉模型，agent 就能读懂设计图上的文字和语义。"))
        say(dim("    同一个 key 就能用，不需要另外申请。"))
        if not interactive:
            vision_model = pick.id
        else:
            answer = ask(f"  用它做视觉模型吗？[Y/n] ").strip().lower()
            if answer in ("", "y", "yes"):
                vision_model = pick.id

    if not vision_model and interactive:
        say("")
        say(dim("  可选：配一个多模态模型，才能读懂设计图上的文字和语义。"))
        say(dim("  不配也能做 UI——inspect_image 和 pixel_diff 都不需要模型。"))
        typed = ask("  视觉模型名（回车跳过）: ").strip()
        vision_model = typed

    # ---------------------------------------------------------- 5. 写入 .env
    from .config import write_env_values

    values = {
        "VA_API_KEY": api_key,
        "VA_BASE_URL": base_url,
        "VA_MODEL": chosen,
    }
    if vision_model:
        values["VA_VISION_MODEL"] = vision_model
        # 视觉模型和主模型同源时不必重复写地址和 key——
        # config 会自动沿用主配置。只有跨服务商才需要单独指定。
    try:
        write_env_values(env_path, values)
    except OSError as exc:
        return SetupResult(False, error=f"写入 {env_path} 失败：{exc}")

    # 缓存清单，供 /models 和 /model 用
    if choices:
        try:
            save_catalog(
                directory,
                ModelCatalog(base_url=base_url, models=choices, source=catalog_source),
            )
        except OSError:
            pass  # 缓存失败不影响使用

    result = SetupResult(
        ok=True,
        env_path=env_path,
        base_url=base_url,
        model=chosen,
        models=choices,
        vision_model=vision_model,
    )

    # ---------------------------------------------------------- 6. 验证
    if verify:
        say("")
        say(dim("  正在验证配置…"))
        ok, detail = _verify(base_url, api_key, chosen)
        result.verified = ok
        if ok:
            say(green(f"  ✓ 连通（{chosen} → {detail!r}）"))
        else:
            say(red(f"  ✗ 连不上：{detail}"))
            say(dim(f"    配置已经写进 {env_path}，改好之后重跑：{run_hint('doctor')}"))

    say("")
    say(bold("  配置完成"))
    say(f"    文件    {env_path}")
    say(f"    服务    {base_url}")
    say(f"    模型    {chosen}")
    if choices:
        say(f"    可选    {len(choices)} 个（进入 agent 后用 /models 看，/model 切换）")
    if vision_model:
        say(f"    视觉    {vision_model}")
    say("")
    return result
