"""无模型图片读取的测试。

这个工具的价值在于「主模型不支持读图时，至少先拿到客观信息」。
所以测试盯的是：像素统计准不准、边界情况（纯色图、缺失依赖）处理得对不对、
以及输出的符号图能不能被正确还原。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402
from va import imaging  # noqa: E402
from va.tools.base import ToolError  # noqa: E402
from va.tools.inspect import SYMBOLS, InspectImage  # noqa: E402

HAS_PILLOW = imaging.pillow_available()


class TestGuards(AgentTestCase):
    def test_without_pillow_gives_install_hint(self):
        import va.tools.inspect as module

        original = module.pillow_available
        module.pillow_available = lambda: False
        try:
            with self.assertRaises(ToolError) as ctx:
                InspectImage(self.workspace, self.cfg).run(path="a.png")
        finally:
            module.pillow_available = original

        message = str(ctx.exception)
        self.assertIn("Pillow", message)
        self.assertIn("pip install", message)

    def test_missing_file(self):
        if not HAS_PILLOW:
            self.skipTest("需要 Pillow")
        with self.assertRaises(ToolError) as ctx:
            InspectImage(self.workspace, self.cfg).run(path="nope.png")
        self.assertIn("找不到", str(ctx.exception))

    def test_no_approval_no_snapshots(self):
        tool = InspectImage(self.workspace, self.cfg)
        self.assertFalse(tool.requires_approval)
        self.assertEqual(tool.affected_paths({}), [])

    def test_registered_and_available_to_subagents(self):
        from va.tools import build_registry
        from va.tools.delegate import READ_ONLY_TOOLS

        registry = build_registry(self.workspace, cfg=self.cfg)
        self.assertIsNotNone(registry.get("inspect_image"))
        self.assertIn(
            "inspect_image", READ_ONLY_TOOLS, "子 agent 做调研时也该能看图"
        )


@unittest.skipUnless(HAS_PILLOW, "需要 Pillow")
class TestInspection(AgentTestCase):
    def _solid(self, name: str, color, size=(200, 100)) -> Path:
        from PIL import Image

        path = self.root / name
        Image.new("RGB", size, color).save(path)
        return path

    def _two_band(self, name: str = "bands.png") -> Path:
        """上白下蓝——用来验证内容区和背景判定。"""
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (200, 200), (240, 240, 240))
        ImageDraw.Draw(image).rectangle([50, 50, 150, 150], fill=(0, 100, 220))
        path = self.root / name
        image.save(path)
        return path

    def test_reports_dimensions_and_format(self):
        self._solid("a.png", (255, 255, 255), size=(320, 240))
        report = imaging.inspect_image(self.root / "a.png")
        self.assertEqual((report.width, report.height), (320, 240))
        self.assertEqual(report.image_format, "PNG")

    def test_grid_has_requested_shape(self):
        self._solid("a.png", (10, 20, 30))
        report = imaging.inspect_image(self.root / "a.png", cols=8, rows=5)
        self.assertEqual(len(report.grid), 5)
        self.assertTrue(all(len(row) == 8 for row in report.grid))

    def test_grid_shape_is_clamped(self):
        self._solid("a.png", (10, 20, 30))
        tiny = imaging.inspect_image(self.root / "a.png", cols=1, rows=1)
        self.assertGreaterEqual(tiny.cols, 4)
        self.assertGreaterEqual(tiny.rows, 3)
        huge = imaging.inspect_image(self.root / "a.png", cols=9999, rows=9999)
        self.assertLessEqual(huge.cols, 80)

    def test_solid_image_reports_background_and_no_content_box(self):
        self._solid("flat.png", (18, 52, 86))
        report = imaging.inspect_image(self.root / "flat.png")

        self.assertEqual(len(report.palette), 1)
        self.assertEqual(report.background, report.palette[0][0])
        self.assertAlmostEqual(report.palette[0][1], 1.0, places=5)
        self.assertIsNone(report.content_box, "纯色图不该报出内容区")

    def test_finds_content_box(self):
        self._two_band()
        report = imaging.inspect_image(self.root / "bands.png", cols=20, rows=20)

        self.assertIsNotNone(report.content_box)
        x0, y0, x1, y1 = report.content_box
        # 方块在 50~150，网格粒度下允许有偏差
        self.assertLessEqual(abs(x0 - 50), 20, f"左边距估歪了：{x0}")
        self.assertLessEqual(abs(x1 - 150), 20, f"右边距估歪了：{x1}")
        self.assertLess(x1, 200)
        self.assertGreater(x0, 0)

    def test_palette_is_sorted_by_share(self):
        self._two_band()
        report = imaging.inspect_image(self.root / "bands.png")
        ratios = [ratio for _, ratio in report.palette]
        self.assertEqual(ratios, sorted(ratios, reverse=True))

    def test_palette_is_limited(self):
        self._two_band()
        report = imaging.inspect_image(self.root / "bands.png", palette_size=3)
        self.assertLessEqual(len(report.palette), 3)

    def test_colors_are_hex(self):
        self._two_band()
        report = imaging.inspect_image(self.root / "bands.png")
        for hexv, _ in report.palette:
            self.assertRegex(hexv, r"^#[0-9a-f]{6}$")


@unittest.skipUnless(HAS_PILLOW, "需要 Pillow")
class TestRendering(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (200, 200), (245, 245, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle([40, 40, 160, 160], fill=(255, 255, 255))
        draw.ellipse([80, 60, 120, 100], fill=(0, 113, 227))
        (self.root / "mock.png").write_bytes(b"")
        image.save(self.root / "mock.png")

    def tool(self) -> InspectImage:
        return InspectImage(self.workspace, self.cfg)

    def test_output_has_all_sections(self):
        out = self.tool().run(path="mock.png")
        for section in ("主色", "内容区", "粗粒度布局", "图例"):
            self.assertIn(section, out)

    def test_output_reports_dimensions(self):
        out = self.tool().run(path="mock.png")
        self.assertIn("200x200", out)

    def test_background_is_marked(self):
        out = self.tool().run(path="mock.png")
        self.assertIn("← 背景", out)

    def test_layout_map_line_count_matches_rows(self):
        out = self.tool().run(path="mock.png", cols=10, rows=6)
        start = out.index("粗粒度布局")
        legend = out.index("图例：")
        body = out[start:legend].splitlines()[1:]
        body = [line for line in body if line.strip()]
        self.assertEqual(len(body), 6, f"布局图应该有 6 行，实际 {len(body)}")

    def test_layout_map_only_uses_known_symbols(self):
        out = self.tool().run(path="mock.png", cols=8, rows=5)
        start = out.index("粗粒度布局")
        legend = out.index("图例：")
        body = out[start:legend].splitlines()[1:]
        allowed = set(SYMBOLS) | {"?"}
        for line in body:
            for char in line.strip().split():
                self.assertIn(char, allowed, f"出现了没定义的符号：{char!r}")

    def test_mentions_limitations(self):
        """不能让模型以为这工具能读出文字和语义。"""
        out = self.tool().run(path="mock.png")
        self.assertIn("读不出来", out)
        self.assertIn("read_design", out, "应该指出语义要靠视觉模型")

    def test_mentions_it_used_no_model(self):
        out = self.tool().run(path="mock.png")
        self.assertIn("没有调用任何模型", out)

    def test_warns_about_estimation_precision(self):
        out = self.tool().run(path="mock.png")
        self.assertIn("估算", out, "内容区是网格粒度估算，必须说清楚")

    def test_solid_image_says_so(self):
        from PIL import Image

        Image.new("RGB", (100, 100), (7, 7, 7)).save(self.root / "flat.png")
        out = self.tool().run(path="flat.png")
        self.assertIn("同一个颜色", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
