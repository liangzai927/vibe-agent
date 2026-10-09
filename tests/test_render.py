"""渲染与视觉对比工具的测试。

分两层：
* **纯逻辑** —— URL 解析、参数钳制、判定阈值、缺依赖时的报错文案。这些不依赖
  playwright / Pillow，任何环境都跑。
* **真渲染** —— 需要 playwright。装了就真跑一遍（渲染、抓控制台报错、截元素），
  没装就跳过，不会让整个测试套件变红。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402
from va import browser as browser_mod  # noqa: E402
from va import imaging  # noqa: E402
from va.tools import build_registry  # noqa: E402
from va.tools.base import ToolError  # noqa: E402
from va.tools.render import (  # noqa: E402
    CompareWithDesign,
    PixelDiff,
    RenderPage,
    _clamp,
    _verdict,
)

HAS_PLAYWRIGHT = browser_mod.playwright_installed()
HAS_PILLOW = imaging.pillow_available()

SIMPLE_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>测试页</title>
<style>body{margin:0;background:#fff;font-family:sans-serif}
#box{width:200px;height:100px;background:#0071e3}</style></head>
<body><div id="box"></div>
<script>console.error('这条是故意的控制台报错');</script>
</body></html>
"""


class TestPureLogic(AgentTestCase):
    def test_clamp(self):
        self.assertEqual(_clamp(10, 200, 5000), 200)
        self.assertEqual(_clamp(9999, 200, 5000), 5000)
        self.assertEqual(_clamp(800, 200, 5000), 800)

    def test_verdict_thresholds(self):
        self.assertIn("基本一致", _verdict(0.0))
        self.assertIn("基本一致", _verdict(0.4))
        self.assertIn("接近", _verdict(1.0))
        self.assertIn("明显差异", _verdict(5.0))
        self.assertIn("差异较大", _verdict(20.0))

    def test_to_url_passes_through_real_urls(self):
        tool = RenderPage(self.workspace, self.cfg)
        for url in ("http://localhost:3000", "https://example.com/a", "file:///tmp/x.html"):
            self.assertEqual(tool._to_url(url), url)

    def test_to_url_converts_workspace_path(self):
        (self.root / "page.html").write_text("<html></html>")
        tool = RenderPage(self.workspace, self.cfg)
        url = tool._to_url("page.html")
        self.assertTrue(url.startswith("file://"))
        self.assertTrue(url.endswith("page.html"))

    def test_to_url_rejects_missing_file(self):
        tool = RenderPage(self.workspace, self.cfg)
        with self.assertRaises(ToolError) as ctx:
            tool._to_url("nope.html")
        self.assertIn("找不到", str(ctx.exception))

    def test_to_url_rejects_empty(self):
        with self.assertRaises(ToolError):
            RenderPage(self.workspace, self.cfg)._to_url("   ")

    def test_output_path_goes_to_the_agent_home(self):
        """截图落在 agent 自己的目录，不进用户的项目。"""
        tool = RenderPage(self.workspace, self.cfg)
        path = tool._output_path(None, "http://localhost:3000/dashboard")
        self.assertTrue(self.workspace.writable_output(path), path)
        self.assertTrue(path.name.endswith(".png"))

    def test_output_path_explicit_is_respected(self):
        tool = RenderPage(self.workspace, self.cfg)
        path = tool._output_path("shots/a.png", "x.html")
        self.assertEqual(path, self.workspace.root / "shots" / "a.png")

    def test_render_tools_need_no_approval(self):
        registry = build_registry(self.workspace, cfg=self.cfg)
        for name in ("render_page", "pixel_diff", "compare_with_design"):
            tool = registry.get(name)
            self.assertIsNotNone(tool, f"{name} 没被注册")
            self.assertFalse(tool.requires_approval, f"{name} 不该要求审批（渲染是观察行为）")
            self.assertFalse(tool.opaque)
            self.assertEqual(tool.affected_paths({}), [], f"{name} 不该触发文件快照")

    def test_render_tools_are_registered_with_schemas(self):
        registry = build_registry(self.workspace, cfg=self.cfg)
        schema = registry.get("render_page").schema()
        self.assertEqual(schema["function"]["name"], "render_page")
        self.assertIn("target", schema["function"]["parameters"]["properties"])
        self.assertEqual(schema["function"]["parameters"]["required"], ["target"])


class TestMissingDependencies(AgentTestCase):
    """缺依赖时的报错必须自解释——容器里用户看不到 traceback 的上下文。"""

    def test_pixel_diff_without_pillow_explains_how_to_install(self):
        import va.tools.render as render_module

        original = render_module.pillow_available
        render_module.pillow_available = lambda: False
        try:
            tool = PixelDiff(self.workspace, self.cfg)
            with self.assertRaises(ToolError) as ctx:
                tool.run(baseline="a.png", actual="b.png")
            message = str(ctx.exception)
            self.assertIn("Pillow", message)
            self.assertIn("pip install", message)
        finally:
            render_module.pillow_available = original

    def test_compare_without_vision_model_explains_config(self):
        # self.cfg 默认没有配 vision_model
        tool = CompareWithDesign(self.workspace, self.cfg)
        with self.assertRaises(ToolError) as ctx:
            tool.run(design="a.png", rendered="b.png")
        message = str(ctx.exception)
        self.assertIn("VA_VISION_MODEL", message)
        self.assertIn("pixel_diff", message, "应该告诉用户有不需要模型的替代方案")

    def test_vision_config_derives_from_main(self):
        from va.config import Config

        cfg = Config(
            workspace=self.root,
            model="main-model",
            api_key="k",
            base_url="https://main.example/v1",
            vision_model="vision-model",
        )
        derived = cfg.vision_config()
        self.assertEqual(derived.model, "vision-model")
        self.assertEqual(derived.base_url, "https://main.example/v1", "未单独指定时应沿用主配置")
        self.assertEqual(derived.api_key, "k")

        cfg.vision_base_url = "https://vision.example/v1"
        cfg.vision_api_key = "vk"
        derived = cfg.vision_config()
        self.assertEqual(derived.base_url, "https://vision.example/v1")
        self.assertEqual(derived.api_key, "vk")

    def test_no_vision_model_means_no_derivation(self):
        from va.config import Config

        cfg = Config(workspace=self.root, model="m", api_key="k", base_url="b")
        self.assertFalse(cfg.has_vision)
        self.assertIs(cfg.vision_config(), cfg)


@unittest.skipUnless(HAS_PLAYWRIGHT, "需要 playwright：pip install -r requirements-browser.txt")
class TestRealRendering(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        browser_mod.reset_cache()
        self.page = self.root / "page.html"
        self.page.write_text(SIMPLE_PAGE)

    def test_renders_a_screenshot(self):
        tool = RenderPage(self.workspace, self.cfg)
        result = tool.run("page.html", output="shots/out.png", width=400, height=300)

        self.assertIn("已渲染", result)
        self.assertIn("视口      400x300", result)
        out = self.root / "shots" / "out.png"
        self.assertTrue(out.is_file(), "截图文件没生成")
        self.assertGreater(out.stat().st_size, 1000, "截图太小，可能是空白页")

    def test_captures_console_errors(self):
        tool = RenderPage(self.workspace, self.cfg)
        result = tool.run("page.html", output="shots/c.png", width=400, height=300)

        self.assertIn("控制台问题", result)
        self.assertIn("故意的控制台报错", result)

    def test_element_screenshot(self):
        tool = RenderPage(self.workspace, self.cfg)
        result = tool.run("page.html", output="shots/el.png", selector="#box", width=400, height=300)

        self.assertIn("元素 #box", result)
        from PIL import Image

        with Image.open(self.root / "shots" / "el.png") as img:
            self.assertEqual(img.size, (200, 100), "元素截图尺寸应等于元素本身")

    def test_missing_selector_gives_actionable_error(self):
        tool = RenderPage(self.workspace, self.cfg)
        with self.assertRaises(ToolError) as ctx:
            tool.run("page.html", selector="#does-not-exist", width=400, height=300)
        self.assertIn("找不到选择器", str(ctx.exception))
        self.assertIn("render_page", str(ctx.exception), "应该建议先截全页看看结构")

    def test_unreachable_url_explains_the_likely_cause(self):
        tool = RenderPage(self.workspace, self.cfg)
        with self.assertRaises(ToolError) as ctx:
            tool.run("http://127.0.0.1:1/nothing", timeout=4000)
        message = str(ctx.exception)
        self.assertIn("失败", message)

    def test_default_output_does_not_touch_the_project(self):
        """截图落在 agent 自己的目录，用户的项目保持干净。"""
        from va.paths import screenshots_dir

        tool = RenderPage(self.workspace, self.cfg)
        tool.run("page.html", width=300, height=200)

        created = list(screenshots_dir().glob("*.png"))
        self.assertTrue(created, "默认截图目录里没有文件")
        self.assertFalse(
            (self.root / ".va").exists(),
            "截图不该写进用户项目——git status 会一直脏着",
        )


@unittest.skipUnless(HAS_PILLOW, "需要 Pillow：pip install pillow")
class TestPixelDiffTool(AgentTestCase):
    def _solid(self, name: str, color: tuple[int, int, int], size=(100, 100)) -> Path:
        from PIL import Image

        path = self.root / name
        Image.new("RGB", size, color).save(path)
        return path

    def test_identical_images_report_zero(self):
        a = self._solid("a.png", (255, 255, 255))
        b = self._solid("b.png", (255, 255, 255))
        result = PixelDiff(self.workspace, self.cfg).run(baseline="a.png", actual="b.png")
        self.assertIn("基本一致", result)
        self.assertIn("0.00%", result)

    def test_different_images_report_difference(self):
        self._solid("a.png", (255, 255, 255))
        self._solid("b.png", (0, 0, 0))
        result = PixelDiff(self.workspace, self.cfg).run(baseline="a.png", actual="b.png")
        self.assertIn("差异较大", result)
        self.assertIn("100.00%", result)

    def test_partial_difference(self):
        from PIL import Image

        Image.new("RGB", (100, 100), (255, 255, 255)).save(self.root / "a.png")
        img = Image.new("RGB", (100, 100), (255, 255, 255))
        for x in range(50):
            for y in range(100):
                img.putpixel((x, y), (0, 0, 0))
        img.save(self.root / "b.png")

        result = PixelDiff(self.workspace, self.cfg).run(baseline="a.png", actual="b.png")
        self.assertIn("50.00%", result)

    def test_threshold_ignores_small_noise(self):
        from PIL import Image

        Image.new("RGB", (50, 50), (128, 128, 128)).save(self.root / "a.png")
        Image.new("RGB", (50, 50), (135, 135, 135)).save(self.root / "b.png")

        strict = PixelDiff(self.workspace, self.cfg).run(
            baseline="a.png", actual="b.png", threshold=0
        )
        lenient = PixelDiff(self.workspace, self.cfg).run(
            baseline="a.png", actual="b.png", threshold=16
        )
        self.assertIn("100.00%", strict)
        self.assertIn("基本一致", lenient, "小幅噪声在默认阈值下应被忽略")

    def test_heatmap_is_written(self):
        self._solid("a.png", (255, 255, 255))
        self._solid("b.png", (0, 0, 0))
        PixelDiff(self.workspace, self.cfg).run(
            baseline="a.png", actual="b.png", output="shots/diff.png"
        )
        self.assertTrue((self.root / "shots" / "diff.png").is_file())

    def test_size_mismatch_is_reported(self):
        from PIL import Image

        Image.new("RGB", (100, 100), (255, 255, 255)).save(self.root / "a.png")
        Image.new("RGB", (200, 150), (255, 255, 255)).save(self.root / "b.png")

        result = PixelDiff(self.workspace, self.cfg).run(baseline="a.png", actual="b.png")

        self.assertIn("基本一致", result)
        self.assertIn("缩放对齐", result, "尺寸不同必须明说，否则容易误判")

    def test_missing_file_is_graceful(self):
        self._solid("a.png", (255, 255, 255))
        with self.assertRaises(ToolError):
            PixelDiff(self.workspace, self.cfg).run(baseline="a.png", actual="nope.png")


if __name__ == "__main__":
    unittest.main(verbosity=2)
