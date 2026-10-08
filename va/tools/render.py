"""渲染与视觉对比工具 —— 让 agent 能「看见」自己做出来的界面。

闭环是：**改代码 → render_page → 和设计图对比 → 按差异改 → 再渲染**。

三个工具分工明确：

    render_page          把页面渲染成 PNG，顺带把控制台报错捞出来
    pixel_diff           纯图像计算，给出客观的差异百分比（不花 token）
    compare_with_design  调多模态模型，给出「哪里不像、该怎么改」的描述

设计上的两个刻意选择：

1. **图片不进主对话。** 主模型往往不支持读图（deepseek-chat 就不支持），
   而且 base64 塞进消息历史会撑爆上下文和会话文件。所以读图这件事只发生在
   compare_with_design 内部，对外只返回文字结论。

2. **客观指标和主观判断分开。** pixel_diff 给数字（有没有更接近），
   compare_with_design 给语义（哪里不像）。前者可以拿来当收敛判据，
   后者用来决定下一步改什么。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from .. import browser as browser_mod
from ..config import Config
from ..imaging import ImagingUnavailable, image_data_url, pillow_available, pixel_diff
from ..util import truncate
from ..workspace import Workspace, WorkspaceError
from .base import Tool, ToolError

DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 800
DEFAULT_TIMEOUT_MS = 30_000
MAX_TIMEOUT_MS = 180_000
SCREENSHOT_DIR = ".va/screenshots"
DEFAULT_DIFF_DIR = ".va/screenshots/diffs"


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


def _browser_hint(exc: Exception) -> str:
    hint = getattr(exc, "hint", "")
    return f"\n{hint}" if hint else ""


class _RenderTool(Tool):
    """渲染类工具的公共部分。"""

    def __init__(self, workspace: Workspace, cfg: Config | None = None) -> None:
        self.workspace = workspace
        self._cfg = cfg

    def config(self) -> Config:
        if self._cfg is None:
            self._cfg = Config.load(self.workspace.root)
        return self._cfg

    def resolve(self, path: str, *, what: str = "文件") -> Path:
        """解析工作区内路径，把 WorkspaceError 转成带上下文的 ToolError。"""
        try:
            return self.workspace.resolve(path, must_exist=True)
        except WorkspaceError as exc:
            raise ToolError(f"找不到{what} {path!r}：{exc}") from exc

    def _to_url(self, target: str) -> str:
        text = (target or "").strip()
        if not text:
            raise ToolError("target 不能为空。给 URL（http://localhost:3000）或工作区内的文件路径。")
        if text.startswith(("http://", "https://", "file://", "data:")):
            return text
        try:
            path = self.workspace.resolve(text, must_exist=True)
        except WorkspaceError as exc:
            raise ToolError(
                f"找不到要渲染的文件：{text}\n（{exc}）\n"
                f"如果是本地开发服务器，请直接给 http://localhost:<端口> 这样的 URL。"
            ) from exc
        return path.as_uri()


class RenderPage(_RenderTool):
    name = "render_page"
    description = (
        "用无头浏览器渲染一个页面并截图，返回截图路径、页面尺寸、控制台报错和资源加载失败。"
        "target 可以是本地 HTML 文件路径，也可以是运行中的开发服务器 URL"
        "（如 http://localhost:5173）。做 UI 时必须靠它看到真实效果，不要凭代码想象。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "本地文件路径（相对工作区）或 http(s) URL",
            },
            "output": {
                "type": "string",
                "description": f"截图保存路径（默认 {SCREENSHOT_DIR}/ 下自动命名）",
            },
            "width": {"type": "integer", "description": f"视口宽度，默认 {DEFAULT_WIDTH}"},
            "height": {"type": "integer", "description": f"视口高度，默认 {DEFAULT_HEIGHT}"},
            "full_page": {"type": "boolean", "description": "截取整页（默认 false，只截视口）"},
            "selector": {"type": "string", "description": "只截某个元素，如 '#app' 或 '.card'（可选）"},
            "wait_for": {"type": "string", "description": "等某个选择器出现再截图（可选）"},
            "wait_ms": {"type": "integer", "description": "截图前额外等待毫秒数，给动画留时间（默认 0）"},
            "timeout": {"type": "integer", "description": f"页面加载超时毫秒，默认 {DEFAULT_TIMEOUT_MS}"},
        },
        "required": ["target"],
    }

    def run(
        self,
        target: str,
        output: str | None = None,
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
        full_page: bool = False,
        selector: str | None = None,
        wait_for: str | None = None,
        wait_ms: int = 0,
        timeout: int = DEFAULT_TIMEOUT_MS,
        **_: Any,
    ) -> str:
        url = self._to_url(target)
        viewport_w = _clamp(width, 200, 5000)
        viewport_h = _clamp(height, 200, 5000)
        timeout_ms = _clamp(timeout, 1000, MAX_TIMEOUT_MS)
        out_path = self._output_path(output, target)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # 必须先探测、再进 playwright 上下文。
        # detect() 自己会开一个 sync_playwright 实例，而 sync API 不允许重入——
        # 在 with entry() 里面调 detect() 会让所有候选浏览器都以 "Error" 失败。
        info = browser_mod.detect()
        if not info.available:
            raise ToolError(f"{info.error}{_browser_hint(info)}")

        entry = browser_mod.sync_playwright()
        console: list[str] = []
        failures: list[str] = []
        page_errors: list[str] = []

        with entry() as p:
            try:
                browser = browser_mod.launch(p, info=info)
            except browser_mod.BrowserUnavailable as exc:
                raise ToolError(f"{exc}{_browser_hint(exc)}") from exc

            try:
                context = browser.new_context(
                    viewport={"width": viewport_w, "height": viewport_h},
                    ignore_https_errors=True,
                )
                page = context.new_page()
                page.on(
                    "console",
                    lambda msg: console.append(f"[{msg.type}] {msg.text}")
                    if msg.type in ("error", "warning")
                    else None,
                )
                page.on("pageerror", lambda exc: page_errors.append(str(exc)))
                page.on(
                    "requestfailed",
                    lambda req: failures.append(
                        f"{req.url} — {(req.failure or '未知原因')}"
                    ),
                )

                response = self._goto(page, url, timeout_ms)
                if wait_for:
                    try:
                        page.wait_for_selector(wait_for, timeout=timeout_ms)
                    except Exception as exc:  # noqa: BLE001
                        raise ToolError(
                            f"等不到选择器 {wait_for!r} 出现（{timeout_ms}ms 超时）：{exc}"
                        ) from exc
                if wait_ms and wait_ms > 0:
                    page.wait_for_timeout(min(int(wait_ms), 60_000))

                if selector:
                    element = page.query_selector(selector)
                    if element is None:
                        raise ToolError(
                            f"页面上找不到选择器 {selector!r}。先 render_page 截全页看看实际结构。"
                        )
                    element.screenshot(path=str(out_path))
                    shot_note = f"元素 {selector}"
                else:
                    page.screenshot(path=str(out_path), full_page=bool(full_page))
                    shot_note = "整页" if full_page else "视口"

                title = ""
                page_size = (viewport_w, viewport_h)
                try:
                    title = page.title()
                    page_size = page.evaluate(
                        "() => [document.documentElement.scrollWidth,"
                        " document.documentElement.scrollHeight]"
                    )
                except Exception:  # noqa: BLE001
                    pass
            finally:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass
                browser.close()

        return self._report(
            url=url,
            out_path=out_path,
            shot_note=shot_note,
            viewport=(viewport_w, viewport_h),
            page_size=tuple(page_size),
            title=title,
            status=getattr(response, "status", None),
            full_page=bool(full_page),
            console=console,
            page_errors=page_errors,
            failures=failures,
        )

    # ------------------------------------------------------------- 内部

    @staticmethod
    def _goto(page, url: str, timeout_ms: int):
        try:
            return page.goto(url, wait_until="load", timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            hint = ""
            if url.startswith("http") and ("ERR_CONNECTION_REFUSED" in text or "ECONNREFUSED" in text):
                hint = "\n服务没起来：确认开发服务器已经在跑，且端口和 URL 一致。"
            elif "ERR_FILE_NOT_FOUND" in text:
                hint = "\n文件路径不对。检查路径是否相对工作区根目录。"
            raise ToolError(f"打开 {url} 失败：{text.splitlines()[0]}{hint}") from exc

    def _output_path(self, output: str | None, target: str) -> Path:
        if output:
            return self.workspace.resolve(output)
        stem = re.sub(r"[^A-Za-z0-9]+", "-", target).strip("-")[-40:] or "page"
        return self.workspace.resolve(f"{SCREENSHOT_DIR}/{stem}-{time.strftime('%H%M%S')}.png")

    def _report(
        self,
        *,
        url: str,
        out_path: Path,
        shot_note: str,
        viewport: tuple[int, int],
        page_size: tuple[int, int],
        title: str,
        status: Any,
        full_page: bool,
        console: list[str],
        page_errors: list[str],
        failures: list[str],
    ) -> str:
        try:
            size_bytes = out_path.stat().st_size
        except OSError:
            size_bytes = 0

        rel = self.workspace.relative(out_path)
        lines = [f"已渲染 {url}"]
        lines.append(f"  截图      {rel}  ({shot_note}, {size_bytes / 1024:.1f} KB)")
        lines.append(f"  视口      {viewport[0]}x{viewport[1]}")
        if page_size != viewport:
            lines.append(f"  页面尺寸  {page_size[0]}x{page_size[1]}")
        if title:
            lines.append(f"  标题      {title}")
        if status is not None:
            lines.append(f"  HTTP      {status}")

        if page_size[1] > viewport[1] and not full_page and not shot_note.startswith("元素"):
            lines.append(
                f"  提示      页面比视口高（{page_size[1]} > {viewport[1]}），"
                f"只截到了首屏；要完整效果请设 full_page=true"
            )

        errors = page_errors + console
        if errors:
            lines.append(f"\n  控制台问题 {len(errors)} 条：")
            for item in errors[:15]:
                lines.append("    " + truncate(item, 240))
            if len(errors) > 15:
                lines.append(f"    … 还有 {len(errors) - 15} 条")
            lines.append("  （页面报错经常就是布局崩掉的原因，优先处理）")

        if failures:
            lines.append(f"\n  资源加载失败 {len(failures)} 条：")
            for item in failures[:10]:
                lines.append("    " + truncate(item, 200))

        return "\n".join(lines)


class PixelDiff(_RenderTool):
    name = "pixel_diff"
    description = (
        "逐像素比较两张截图，返回差异百分比并生成一张差异热力图。"
        "不需要模型、不花 token，适合用来判断「改了之后是不是更接近设计稿了」。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "baseline": {"type": "string", "description": "基准图路径（通常是设计稿）"},
            "actual": {"type": "string", "description": "实际渲染图路径"},
            "output": {"type": "string", "description": f"差异图保存路径（默认 {DEFAULT_DIFF_DIR}/ 下）"},
            "threshold": {
                "type": "integer",
                "description": "单通道差值超过多少算不同，默认 16。调高可忽略抗锯齿噪声",
            },
        },
        "required": ["baseline", "actual"],
    }

    def run(
        self,
        baseline: str,
        actual: str,
        output: str | None = None,
        threshold: int = 16,
        **_: Any,
    ) -> str:
        if not pillow_available():
            raise ToolError(
                "没有安装 Pillow，无法做像素对比。\n"
                "安装：pip install pillow（或 pip install -r requirements-browser.txt）"
            )

        base_path = self.resolve(baseline, what="基准图")
        actual_path = self.resolve(actual, what="渲染图")

        if output:
            out_path = self.workspace.resolve(output)
        else:
            stem = actual_path.stem
            out_path = self.workspace.resolve(f"{DEFAULT_DIFF_DIR}/{stem}-diff.png")

        try:
            result = pixel_diff(
                base_path,
                actual_path,
                output=out_path,
                threshold=_clamp(threshold, 0, 255),
            )
        except (ValueError, FileNotFoundError, ImagingUnavailable) as exc:
            raise ToolError(f"像素对比失败：{exc}") from exc

        verdict = _verdict(result.percent)
        return (
            f"{verdict}\n"
            f"  {result.summary()}\n"
            f"  热力图中红色像素就是两张图不一致的位置"
        )

    def preview(self, args: dict[str, Any]) -> str | None:
        return None

    def affected_paths(self, args: dict[str, Any]) -> list[Path]:
        return []  # 只写差异图，不算业务改动


def _verdict(percent: float) -> str:
    if percent < 0.5:
        return f"✓ 基本一致（{percent:.2f}%）"
    if percent < 3:
        return f"△ 接近，还有细节差异（{percent:.2f}%）"
    if percent < 10:
        return f"△ 有明显差异（{percent:.2f}%）"
    return f"✗ 差异较大（{percent:.2f}%）"


COMPARE_PROMPT = """你在做 UI 还原度评审。

下面有**两张图**：第一张是设计稿，第二张是当前代码渲染出来的实际效果。
请对比它们，按下面的结构回答。只写你真正看到的东西，不要脑补。

## 整体相似度
高 / 中 / 低，加一句话理由。

## 明显差异
按「位置 → 现象 → 应该是什么」列出，最多 8 条，按严重程度排序。
重点看：布局与对齐、间距、字号字重、颜色、圆角与边框、图标、缺失或多余的元素。

## 最该先改的三件事
1.
2.
3.

## 看不出来的地方
如果有区域因为分辨率、遮挡或状态不同而无法判断，如实说明。
如果两张图基本一致，就直接说「基本一致」，不要为了凑数编造差异。
{focus}"""


class CompareWithDesign(_RenderTool):
    name = "compare_with_design"
    description = (
        "把设计稿和实际渲染截图交给多模态模型做还原度评审，返回「哪里不像、该先改什么」。"
        "需要配置 VA_VISION_MODEL；没配的话改用 pixel_diff 做客观像素对比。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "design": {"type": "string", "description": "设计稿图片路径（相对工作区）"},
            "rendered": {"type": "string", "description": "render_page 产出的截图路径"},
            "focus": {"type": "string", "description": "希望重点看什么，例如「只看移动端布局」（可选）"},
            "output": {"type": "string", "description": "差异热力图保存路径（可选）"},
        },
        "required": ["design", "rendered"],
    }

    def run(
        self,
        design: str,
        rendered: str,
        focus: str = "",
        output: str | None = None,
        **_: Any,
    ) -> str:
        cfg = self.config()
        if not cfg.has_vision:
            raise ToolError(
                "没有配置视觉模型，无法做设计稿比对。\n"
                "主模型通常不支持读图（deepseek-chat 就不支持），需要单独配一个多模态模型：\n"
                "  VA_VISION_MODEL=gpt-4o\n"
                "  VA_VISION_BASE_URL=https://api.openai.com/v1\n"
                "  VA_VISION_API_KEY=sk-...\n"
                "不想配模型的话，用 pixel_diff 做客观的像素对比（不需要模型）。"
            )

        design_path = self.resolve(design, what="设计稿")
        rendered_path = self.resolve(rendered, what="渲染截图")

        # 先给一个客观数字，避免模型的描述和实际差得太远
        pixel_note = ""
        try:
            diff = pixel_diff(
                design_path,
                rendered_path,
                output=self.workspace.resolve(output) if output else None,
            )
            pixel_note = f"客观像素差异：{diff.percent:.2f}%"
            if diff.resized:
                pixel_note += "（两图尺寸不同，已缩放对齐——请留意设计稿是否用了不同视口）"
        except Exception as exc:  # noqa: BLE001 - 客观指标拿不到不该阻断主观评审
            pixel_note = f"（像素对比不可用：{exc}）"

        prompt = COMPARE_PROMPT.format(focus=("\n补充关注点：" + focus.strip()) if focus.strip() else "")

        try:
            design_url = image_data_url(design_path)
            rendered_url = image_data_url(rendered_path)
        except (FileNotFoundError, ImagingUnavailable) as exc:
            raise ToolError(f"读取图片失败：{exc}") from exc

        from ..llm import LLM, LLMError

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": design_url}},
                    {"type": "image_url", "image_url": {"url": rendered_url}},
                ],
            }
        ]

        try:
            turn = LLM(cfg.vision_config()).complete(messages)
        except LLMError as exc:
            raise ToolError(
                f"视觉模型调用失败：{exc}\n"
                f"（当前视觉模型：{cfg.vision_model} @ {cfg.vision_base_url or cfg.base_url}）"
            ) from exc

        return (
            f"{pixel_note}\n"
            f"视觉模型：{cfg.vision_model}（图中顺序：先设计稿，后实际渲染）\n\n"
            f"{turn.content.strip()}"
        )


RENDER_TOOLS = (RenderPage, PixelDiff, CompareWithDesign)
