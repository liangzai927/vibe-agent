"""不需要模型就能读的图片信息。

主模型往往不支持读图，而「再配一个多模态模型」对不少人是额外成本。
但设计稿里有一大半信息是**纯像素统计就能拿到**的：画布多大、用了哪几种颜色
（hex 值，写 CSS 直接能用）、各个区块大概在什么位置、占多大。

这些恰好就是还原界面最需要的东西。所以这个工具不调用任何 API，
断网也能跑，也不花一分钱。

**它读不出来的**（得靠视觉模型或人）：
    * 文字内容和字体
    * 「这个圆是头像还是按钮」这类语义
    * 精确到像素的间距（只能给到网格粒度）
"""

from __future__ import annotations

from typing import Any

from ..imaging import ImagingUnavailable, inspect_image, pillow_available
from ..util import truncate
from .base import Tool, ToolError
from .context import WorkspaceTool

# 每个符号代表一种主色。用得越靠前的符号代表占比越大。
SYMBOLS = (".", "#", "●", "○", "◆", "▲", "■", "+", "*", "×")

INSTALL_HINT = (
    "安装：pip install pillow（或 pip install -r requirements-browser.txt）"
)


class InspectImage(WorkspaceTool):
    name = "inspect_image"
    description = (
        "不借助任何模型，直接从图片像素里读出：画布尺寸、主色（hex，可直接写进 CSS）、"
        "内容区的像素边界、以及一张粗粒度的字符布局图。\n"
        "**主模型不支持读图时，先用它拿客观信息**，比凭想象写样式靠谱得多。\n"
        "它读不出文字内容和语义（那是 read_design 的活，需要视觉模型）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "图片路径（相对工作区）"},
            "cols": {
                "type": "integer",
                "description": "布局图的列数，默认 20。调大能看到更细的结构，但输出也变长",
            },
            "rows": {"type": "integer", "description": "布局图的行数，默认 12"},
            "colors": {"type": "integer", "description": "列出多少种主色，默认 8"},
        },
        "required": ["path"],
    }
    requires_approval = False  # 读工作区内的图不需要审批

    def preview(self, args):
        return self.external_preview(args, "path")

    def needs_approval(self, args):
        """只有读工作区外的图片才问。

        设计稿常常躺在 ~/Desktop 或 ~/Downloads，不在项目里。
        把那个目录加进 VA_READ_DIRS 就不用每次都问。
        """
        return self.is_external(args.get("path"))

    def run(
        self,
        path: str,
        cols: int = 20,
        rows: int = 12,
        colors: int = 8,
        **_: Any,
    ) -> str:
        if not pillow_available():
            raise ToolError(f"没有安装 Pillow，读不了图片像素。\n{INSTALL_HINT}")

        target = self.resolve_readable(path, what="图片")

        try:
            report = inspect_image(target, cols=cols, rows=rows, palette_size=colors)
        except (FileNotFoundError, ImagingUnavailable, OSError) as exc:
            raise ToolError(f"读取图片失败：{exc}") from exc

        return self._render(report, self.rel(target))

    # ------------------------------------------------------------- 渲染

    def _render(self, report: Any, label: str) -> str:
        lines = [
            f"图片 {label}"
            f"（{report.image_format}，{report.width}x{report.height}，"
            f"{report.size_bytes / 1024:.1f} KB）",
            "",
        ]

        # 主色
        lines.append(f"主色（{report.samples} 个采样格，按占比排序）：")
        symbol_of: dict[str, str] = {}
        for index, (hexv, ratio) in enumerate(report.palette):
            symbol = SYMBOLS[index] if index < len(SYMBOLS) else "?"
            symbol_of[hexv] = symbol
            bar = "█" * max(1, round(ratio * 40))
            tag = "  ← 背景" if hexv == report.background else ""
            lines.append(f"  {symbol} {hexv}  {ratio * 100:5.1f}%  {bar}{tag}")

        # 内容区
        if report.content_box:
            x0, y0, x1, y1 = report.content_box
            lines.append("")
            lines.append(
                f"内容区（偏离背景色的区域包围盒）："
                f"x {x0}~{x1}，y {y0}~{y1}  "
                f"（约 {x1 - x0}x{y1 - y0}，"
                f"左边距 {x0}，上边距 {y0}）"
            )
            lines.append(
                "  ⚠ 这是网格粒度的估算，不是精确测量；间距要用 render_page "
                "渲染出来后靠 pixel_diff 收敛"
            )
        else:
            lines.append("")
            lines.append("内容区：整张图都是同一个颜色（可能是空白图或纯色块）")

        # 布局图
        lines.append("")
        lines.append(f"粗粒度布局（{report.cols}x{report.rows}，每格是该处的主导色）：")
        for row in report.grid:
            lines.append("  " + " ".join(symbol_of.get(hexv, "?") for hexv in row))

        legend = "  ".join(
            f"{symbol_of[hexv]}={hexv}" for hexv, _ in report.palette[:6]
        )
        lines.append(f"  图例：{legend}")

        lines.append("")
        lines.append(
            "说明：以上全部来自像素统计，没有调用任何模型。"
            "文字内容、字体、语义（哪个是按钮）读不出来——那需要视觉模型（read_design）。"
        )
        return "\n".join(lines)


INSPECT_TOOLS = (InspectImage,)
