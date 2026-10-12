"""截图稳定化与样式还原（生产工作流·阶段 G）。

**为什么必须稳定化**：`pixel_diff` 本身没问题，问题是喂给它的两张图不可比——
时间是动的、动画截到中间帧、随机数据每次不同。
不处理的话同一页面连截两次都能差 0.25%，**那个数字没有任何意义**，
门禁会被它反复误伤，最后你会习惯性地绕过它，等于没有。

实测：稳定化把同一页面的噪声从 0.247% 压到 0.000%，
而「改一个颜色」是 3.80%——信号远远盖过噪声。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402
from va import visual  # noqa: E402

try:
    from PIL import Image, ImageDraw

    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False


def make(directory: Path, name: str, *, block=None, size=(400, 300), color=(255, 255, 255)):
    image = Image.new("RGB", size, color)
    if block:
        ImageDraw.Draw(image).rectangle(block, fill=(255, 0, 0))
    path = directory / name
    image.save(path)
    return path


class TestStabilizeScript(unittest.TestCase):
    """注入脚本本身的正确性——它坏掉会让**所有**截图失败。"""

    def test_no_format_placeholders_remain(self):
        """**踩过的坑**：脚本用 `%` 格式化，而我在注释里写了「噪声 0.11%」——
        那个百分号直接把格式化搞崩了，所有截图报「not enough arguments」。
        改成 `replace` 之后没有转义规则，也就没有这类陷阱。"""
        self.assertNotIn("%(", visual.STABILIZE_JS)
        self.assertNotIn("%%", visual.STABILIZE_JS)

    def test_freezes_all_four_things(self):
        script = visual.STABILIZE_JS
        self.assertIn("FrozenDate", script, "没冻时间")
        self.assertIn("Math.random", script, "没冻随机")
        self.assertIn("animation-duration", script, "没停动画")
        self.assertIn("caret-color", script, "没停光标闪烁")

    def test_random_is_a_constant_not_a_sequence(self):
        """**序列看起来更"正确"，但实测压不到合格线。**

        两次截图之间 setInterval 触发的次数不同，序列走到第几个数就不同，
        文本还是每次都变（0.11%，超过 0.1% 的线）。
        常量才是对的——像素比对要的是确定性，不是真实性。
        """
        self.assertIn("Math.random = () => CONST", visual.STABILIZE_JS)


@unittest.skipUnless(HAS_PILLOW, "需要 Pillow")
class TestCompare(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())

    def test_identical_images_are_zero(self):
        a = make(self.dir, "a.png")
        b = make(self.dir, "b.png")
        result = visual.compare(a, b)
        self.assertEqual(result["ratio"], 0.0)

    def test_a_change_raises_the_ratio(self):
        a = make(self.dir, "a.png")
        b = make(self.dir, "b.png", block=(300, 30, 380, 120))
        self.assertGreater(visual.compare(a, b)["ratio"], 0.0)

    def test_regions_say_where(self):
        """「差异 3.2%」没法动手；「差异集中在右上角」才能改。"""
        a = make(self.dir, "a.png")
        b = make(self.dir, "b.png", block=(300, 30, 380, 120))
        regions = visual.compare(a, b)["regions"]
        self.assertTrue(regions)
        self.assertIn("上右", regions[0]["where"])

    def test_size_mismatch_is_reported_not_compared(self):
        """**750×1624 和 1440×900 是两个不同的东西**，比出来的百分比只会误导人。"""
        a = make(self.dir, "a.png", size=(400, 300))
        b = make(self.dir, "b.png", size=(200, 150))
        result = visual.compare(a, b)
        self.assertIsNone(result["ratio"], "尺寸不同还给了百分比")
        self.assertIn("尺寸不一致", result["note"])
        self.assertFalse(result["same_size"])

    def test_ratio_from_treats_mismatch_as_completely_different(self):
        """给门禁用：尺寸不一样当「完全不像」，不是当「一样」。"""
        a = make(self.dir, "a.png", size=(400, 300))
        b = make(self.dir, "b.png", size=(200, 150))
        self.assertEqual(visual.ratio_from(a, b), 1.0)

    def test_describe_mentions_the_threshold(self):
        a = make(self.dir, "a.png")
        b = make(self.dir, "b.png", block=(0, 0, 399, 299))
        text = visual.describe(visual.compare(a, b), target=0.01)
        self.assertIn("超出阈值", text)
        self.assertIn("从上往下改", text)

    def test_threshold_tolerates_antialiasing_noise(self):
        """抗锯齿会让边缘差一两个色阶——那不是「样式不对」。"""
        a = make(self.dir, "a.png", color=(255, 255, 255))
        b = make(self.dir, "b.png", color=(250, 250, 250))
        self.assertEqual(visual.compare(a, b, threshold=16)["ratio"], 0.0)
        self.assertGreater(visual.compare(a, b, threshold=2)["ratio"], 0.0)


class TestWorkflowConfig(AgentTestCase):
    def test_stabilization_is_on_by_default(self):
        from va.workflow import load

        self.assertTrue(load(self.root).get("test.visual_stabilize"))

    def test_freeze_time_is_configurable(self):
        from va.workflow import load

        config = self.root / ".vibe" / "workflow.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            'test:\n  visual_freeze_time: "2025-06-01T12:00:00"\n  visual_seed: 7\n'
        )
        wf = load(self.root)
        self.assertEqual(wf.get("test.visual_freeze_time"), "2025-06-01T12:00:00")
        self.assertEqual(wf.get("test.visual_seed"), 7)

    def test_stabilize_does_not_crash_on_a_bad_date(self):
        """日期写错了不该让截图失败——退回一个固定的时间戳就行。"""

        class FakePage:
            def add_init_script(self, script):
                self.script = script

            def evaluate(self, script, *args):
                self.script = script

        page = FakePage()
        visual.stabilize(page, frozen="不是日期")
        self.assertIn("FROZEN", page.script)


if __name__ == "__main__":
    unittest.main(verbosity=2)
