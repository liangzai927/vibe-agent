"""设计稿解析：把 UI 图转成可以直接照着写代码的文字规格。

**为什么需要它**：主模型往往不支持读图（deepseek-chat 就不支持）。如果不做这一层，
「照着设计图实现界面」这件事就无从谈起——agent 只能看着一张二进制文件干瞪眼。

做法是让多模态模型先做一次「翻译」：图 → 结构化规格 → 主模型照着写代码。
这样一次视觉调用就够，后续所有迭代都走文字通道。

**为什么规格要落盘**：规格通常不短（上千 token），而且要实现到一半、
上下文被压缩之后仍然需要它。落盘之后随时 read_file 就能取回，
不依赖它一直留在对话里。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..imaging import ImagingUnavailable, image_data_url
from ..util import truncate
from .base import Tool, ToolError
from .context import WorkspaceTool

DESIGN_DIR = ".va/designs"
MAX_SPEC_CHARS = 20_000

DESIGN_PROMPT = """你在把一张 UI 设计稿翻译成**可以直接照着写代码的文字规格**。

这份规格是给另一个**看不到图**的模型用的，所以：

- 凡是能用数字表达的，一律给数字：尺寸与间距用 px，颜色用 hex
- 颜色估不准就写「约 #xxxxxx（估算）」，不要编一个看起来很精确的值
- 看不清、被遮挡、图里本来就没有的，明确写「图中不可见」，不要脑补
- 用结构化的条目，不要写成散文或宣传稿
- 不要描述「这个设计传达了怎样的理念」，只写实现需要的信息

按下面的结构输出：

## 画布
- 图像尺寸、推断的基准视口宽度
- 背景色

## 结构树
用缩进表示嵌套层级，每个节点一行，格式：
`名称  [布局]  [尺寸]  [内/外边距]  [背景色 / 边框 / 圆角 / 阴影]`

## 组件明细
每个可见组件一小节，写清：
- 位置（属于哪个父容器、第几项）
- 尺寸与间距
- 颜色（hex，或「约 #xxxxxx（估算）」）
- 文字：内容、字号、字重、行高、颜色、对齐方式

## 交互状态
只写从图里真能看出来的（悬停、选中、禁用等）。看不出来就写「图中不可见」。

## 不确定的地方
明确列出无法从这张图判断的东西：精确色值、响应式行为、动效、被截断的部分、
图片压缩带来的误差。

## 实现要点
3~5 条，例如：该用 flex 还是 grid、哪些地方适合抽成组件、有哪些重复结构可以复用。
{focus}"""


class ReadDesign(WorkspaceTool):
    name = "read_design"
    description = (
        "把 UI 设计稿（截图/图片）解析成结构化的文字规格，供照着实现界面用。"
        "规格会自动保存到 .va/designs/ 下，上下文被压缩后可以重新 read_file 取回。"
        "需要配置 VA_VISION_MODEL——主模型通常不支持读图。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "image": {"type": "string", "description": "设计稿图片路径（相对工作区）"},
            "focus": {
                "type": "string",
                "description": "希望重点解析什么，例如「只看移动端布局」（可选）",
            },
            "save": {
                "type": "string",
                "description": f"规格保存路径（默认 {DESIGN_DIR}/<图片名>.md；传空字符串则不保存）",
            },
        },
        "required": ["image"],
    }
    requires_approval = False  # 只读图、写自己的产物目录，不碰业务代码

    def preview(self, args):
        return self.external_preview(args, "image")

    def needs_approval(self, args):
        """读工作区外的设计稿要先问一声——它会把图片内容发给视觉模型。"""
        return self.is_external(args.get("image"))

    def run(
        self,
        image: str,
        focus: str = "",
        save: str | None = None,
        **_: Any,
    ) -> str:
        cfg = self.require_vision()
        image_path = self.resolve_readable(image, what="设计稿")

        try:
            data_url = image_data_url(image_path)
        except (FileNotFoundError, ImagingUnavailable) as exc:
            raise ToolError(f"读取设计稿失败：{exc}") from exc

        prompt = DESIGN_PROMPT.format(
            focus=("\n补充关注点：" + focus.strip()) if focus.strip() else ""
        )

        from ..llm import LLM, LLMError

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]

        try:
            turn = LLM(cfg.vision_config()).complete(messages)
        except LLMError as exc:
            raise ToolError(
                f"视觉模型调用失败：{exc}\n"
                f"（当前视觉模型：{self.vision_label(cfg)} @ {cfg.vision_base_url or cfg.base_url}）"
            ) from exc

        spec = (turn.content or "").strip()
        if not spec:
            raise ToolError(
                "视觉模型返回了空内容。可能是这个模型不支持图片输入——"
                "确认 VA_VISION_MODEL 指向的是一个真正的多模态模型。"
            )

        saved_path, save_note = self._save_spec(spec, image_path, save)

        header = [
            f"已解析设计稿 {self.rel(image_path)}",
            f"  视觉模型  {self.vision_label(cfg)}",
        ]
        if saved_path is not None:
            header.append(
                f"  规格文件  {self.rel(saved_path)}"
                f"（{save_note}——上下文被压缩后可以重新 read_file 取回）"
            )
        elif save_note:
            # 保存失败时必须说出来。不要因为「路径为 None」就顺手把提示丢掉——
            # 那是两件不同的事。
            header.append(f"  ⚠ {save_note}")
        if len(spec) < 200:
            header.append(
                "  ⚠ 规格异常地短，可能模型没真正看图。可以换个更大的多模态模型重试。"
            )

        return "\n".join(header) + "\n\n" + truncate(spec, limit=MAX_SPEC_CHARS, label="规格")

    # ------------------------------------------------------------- 内部

    def _save_spec(
        self, spec: str, image_path: Path, save: str | None
    ) -> tuple[Path | None, str]:
        """返回 (保存路径或 None, 一句给用户看的说明)。

        save 传空字符串表示不保存，此时两个返回值都是空的。
        """
        if save is not None and not save.strip():
            return None, ""

        if save and save.strip():
            target = self.workspace.resolve(save.strip())
        else:
            target = self.workspace.resolve(f"{DESIGN_DIR}/{image_path.stem}.md")

        body = (
            f"<!-- 由 read_design 生成，来源：{self.rel(image_path)}"
            f" @ {time.strftime('%Y-%m-%d %H:%M:%S')} -->\n\n"
            f"# 设计稿规格 · {image_path.name}\n\n{spec}\n"
        )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        except OSError as exc:
            # 存不下来不该让整个解析失败——规格内容仍然会返回给模型
            return None, f"规格保存失败（不影响实现，完整内容已在上方给出）：{exc}"
        return target, "已保存"


DESIGN_TOOLS = (ReadDesign,)
