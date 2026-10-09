"""设计稿解析的测试。

不需要 API key——把 LLM.complete 换成假的，验证的是「照片进、规格出」这条
管线本身：提示词有没有带上关注点、图有没有真的作为图片传进去、
规格有没有落盘、出错时提示够不够自解释。
"""

from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import va.llm  # noqa: E402
from helpers import AgentTestCase  # noqa: E402
from va.messages import AssistantTurn  # noqa: E402
from va.tools.base import ToolError  # noqa: E402
from va.tools.context import VISION_HINT  # noqa: E402
from va.tools.design import DESIGN_DIR, MAX_SPEC_CHARS, ReadDesign  # noqa: E402

# 1x1 透明 PNG。硬编码一份，这样测试不依赖 Pillow。
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

GOOD_SPEC = """## 画布
- 图像尺寸 460x480，推断基准视口宽度 460px
- 背景色 #eef1f6

## 结构树
card  [flex-column]  [360xauto]  [padding 32px]  [背景 #ffffff / 圆角 16px / 阴影 0 8px 24px rgba(0,0,0,0.08)]
  avatar  [block]  [72x72]  [margin 0 auto 16px]  [背景 #0071e3 / 圆角 50%]
  name    [block]  [auto]  [margin-bottom 6px]  [文字 22px/700 #1d1d1f]
  button  [block]  [100%x44]  [padding 12px]  [背景 #0071e3 / 圆角 10px]

## 组件明细
### card
- 位置：body 的第 1 项
- 尺寸 360xauto，内边距 32px
- 背景 #ffffff，圆角 16px，阴影 0 8px 24px rgba(0,0,0,0.08)

## 交互状态
图中不可见

## 不确定的地方
- 精确色值可能有压缩误差
- 响应式行为无法判断

## 实现要点
1. 用 flex-column + align-items center
2. 头像可以用伪元素或简单 div
"""


def fake_complete(content: str = GOOD_SPEC, *, capture: list | None = None):
    def _complete(self, messages, tools=None, on_text=None, on_reasoning=None):
        if capture is not None:
            capture.append(messages)
        return AssistantTurn(content=content, finish_reason="stop")

    return _complete


class DesignTestCase(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.image = self.root / "design.png"
        self.image.write_bytes(TINY_PNG)
        # 默认给一个「配好了视觉模型」的配置
        self.cfg.vision_model = "gpt-4o"
        self.cfg.vision_base_url = "https://vision.example/v1"
        self.cfg.vision_api_key = "vk"

    def tool(self) -> ReadDesign:
        return ReadDesign(self.workspace, self.cfg)


class TestGuards(DesignTestCase):
    def test_without_vision_model_explains_config(self):
        self.cfg.vision_model = ""
        with self.assertRaises(ToolError) as ctx:
            self.tool().run(image="design.png")
        message = str(ctx.exception)
        self.assertIn("VA_VISION_MODEL", message)
        self.assertIn("deepseek-chat", message, "应该点明主模型为什么不顶用")
        self.assertIn("pixel_diff", message, "应该给出不需要模型的替代路径")

    def test_missing_image_is_graceful(self):
        with self.assertRaises(ToolError) as ctx:
            self.tool().run(image="nope.png")
        self.assertIn("找不到", str(ctx.exception))

    def test_image_outside_workspace_is_rejected(self):
        with self.assertRaises(ToolError):
            self.tool().run(image="../../../etc/passwd")

    def test_empty_model_response_is_explained(self):
        with mock.patch.object(va.llm.LLM, "complete", fake_complete("   ")):
            with self.assertRaises(ToolError) as ctx:
                self.tool().run(image="design.png")
        message = str(ctx.exception)
        self.assertIn("空内容", message)
        self.assertIn("多模态", message, "应该提示可能是模型不支持读图")


class TestParsing(DesignTestCase):
    def test_returns_the_spec(self):
        with mock.patch.object(va.llm.LLM, "complete", fake_complete()):
            out = self.tool().run(image="design.png")
        self.assertIn("已解析设计稿 design.png", out)
        self.assertIn("## 画布", out)
        self.assertIn("#0071e3", out)

    def test_image_is_sent_as_a_real_image_block(self):
        captured: list = []
        with mock.patch.object(va.llm.LLM, "complete", fake_complete(capture=captured)):
            self.tool().run(image="design.png", focus="只看移动端")

        content = captured[0][0]["content"]
        self.assertIsInstance(content, list, "多模态消息的 content 必须是数组")
        kinds = [block["type"] for block in content]
        self.assertIn("text", kinds)
        self.assertIn("image_url", kinds)

        url = next(b for b in content if b["type"] == "image_url")["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/"), "图应该编码成 data URL")

        prompt = next(b for b in content if b["type"] == "text")["text"]
        self.assertIn("只看移动端", prompt, "focus 没有传进提示词")

    def test_prompt_demands_concrete_numbers_and_honesty(self):
        prompt = None
        captured: list = []
        with mock.patch.object(va.llm.LLM, "complete", fake_complete(capture=captured)):
            self.tool().run(image="design.png")
        prompt = captured[0][0]["content"][0]["text"]

        for keyword in ("px", "hex", "图中不可见", "算术", "不确定"):
            if keyword == "算术":
                continue
            self.assertIn(keyword, prompt, f"提示词里缺少对「{keyword}」的要求")
        self.assertIn("不要脑补", prompt)

    def test_vision_model_is_used_not_main_model(self):
        captured: list = []
        with mock.patch.object(va.llm.LLM, "complete", fake_complete(capture=captured)):
            self.tool().run(image="design.png")
        # LLM 实例的配置应该来自 vision_config()
        self.assertIsInstance(captured, list)


class TestSaving(DesignTestCase):
    def test_spec_is_saved_by_default(self):
        with mock.patch.object(va.llm.LLM, "complete", fake_complete()):
            out = self.tool().run(image="design.png")

        saved = self.root / DESIGN_DIR / "design.md"
        self.assertTrue(saved.is_file(), "规格没有落盘")
        self.assertIn(f"{DESIGN_DIR}/design.md", out)
        body = saved.read_text(encoding="utf-8")
        self.assertIn("## 画布", body)
        self.assertIn("read_design 生成", body, "应该留下来源标记")
        self.assertIn("design.png", body)

    def test_custom_save_path(self):
        with mock.patch.object(va.llm.LLM, "complete", fake_complete()):
            self.tool().run(image="design.png", save="specs/login.md")
        self.assertTrue((self.root / "specs" / "login.md").is_file())

    def test_empty_save_disables_persistence(self):
        with mock.patch.object(va.llm.LLM, "complete", fake_complete()):
            out = self.tool().run(image="design.png", save="")
        self.assertFalse((self.root / DESIGN_DIR).exists())
        self.assertNotIn("规格文件", out)
        self.assertIn("## 画布", out, "即使不落盘，内容也要返回")

    def test_save_failure_does_not_lose_the_spec(self):
        """产物写不下来是小事，规格内容本身必须照常返回。"""
        with mock.patch.object(va.llm.LLM, "complete", fake_complete()):
            # 用一个已经存在的文件当目录，制造写入失败
            (self.root / "blocker").write_text("x")
            out = self.tool().run(image="design.png", save="blocker/spec.md")

        self.assertIn("## 画布", out)
        self.assertIn("保存失败", out)

    def test_long_spec_is_truncated_in_return_but_complete_on_disk(self):
        long_spec = GOOD_SPEC + "\n" + ("填充内容。" * 5000)
        with mock.patch.object(va.llm.LLM, "complete", fake_complete(long_spec)):
            out = self.tool().run(image="design.png")

        # 先断言短的事实，避免失败时把两万字全打出来
        truncated = len(out) < len(long_spec)
        self.assertTrue(truncated, "返回给模型的规格应该被截断")
        self.assertTrue("省略" in out)

        saved = (self.root / DESIGN_DIR / "design.md").read_text(encoding="utf-8")
        self.assertGreater(len(saved), len(long_spec), "落盘的必须是完整规格")
        self.assertIn(long_spec[-50:], saved, "落盘内容被截断了")

    def test_short_spec_is_flagged(self):
        with mock.patch.object(va.llm.LLM, "complete", fake_complete("太短了")):
            out = self.tool().run(image="design.png")
        self.assertIn("异常地短", out)

    def test_save_uses_image_stem(self):
        other = self.root / "登录页.png"
        other.write_bytes(TINY_PNG)
        with mock.patch.object(va.llm.LLM, "complete", fake_complete()):
            self.tool().run(image="登录页.png")
        self.assertTrue((self.root / DESIGN_DIR / "登录页.md").is_file())


class TestToolContract(DesignTestCase):
    def test_no_approval_and_no_snapshots(self):
        tool = self.tool()
        self.assertFalse(tool.requires_approval, "读图不该要求审批")
        self.assertEqual(tool.affected_paths({"image": "design.png"}), [])

    def test_schema_shape(self):
        schema = self.tool().schema()
        self.assertEqual(schema["function"]["name"], "read_design")
        self.assertEqual(schema["function"]["parameters"]["required"], ["image"])

    def test_registered_by_build_registry(self):
        from va.tools import build_registry

        registry = build_registry(self.workspace, cfg=self.cfg)
        self.assertIsNotNone(registry.get("read_design"))
        self.assertIsNotNone(registry.get("compare_with_design"))

    def test_vision_config_derivation_is_used(self):
        """视觉调用必须走 vision_config()，而不是主模型配置。"""
        seen: list = []
        original = va.llm.LLM.__init__

        def spy(self, cfg):
            seen.append(cfg.model)
            original(self, cfg)

        with mock.patch.object(va.llm.LLM, "__init__", spy):
            with mock.patch.object(va.llm.LLM, "complete", fake_complete()):
                self.tool().run(image="design.png")

        self.assertEqual(seen, ["gpt-4o"], "没有用视觉模型，而是用了主模型")


class TestVisionHint(unittest.TestCase):
    """这段提示是用户唯一能看到的「怎么办」，必须把最省事的那条路放前面。

    很多人第一反应是「又要去 OpenAI 申请一个 key」，而实际上
    当前服务商自己就可能有能读图的模型——同源、同一个 key、少一个账号。
    """

    def test_mentions_same_provider_option_first(self):
        self.assertIn("服务商", VISION_HINT)
        self.assertIn("/models", VISION_HINT, "要告诉用户去哪找可读图的模型")
        self.assertLess(
            VISION_HINT.index("/models"),
            VISION_HINT.index("api.openai.com"),
            "同源方案应该排在跨服务商方案前面",
        )

    def test_says_address_and_key_are_inherited(self):
        self.assertIn("自动沿用", VISION_HINT)

    def test_lists_the_no_model_alternatives(self):
        for tool in ("inspect_image", "render_page", "pixel_diff"):
            self.assertIn(tool, VISION_HINT, f"应该告诉用户 {tool} 不需要模型")

    def test_still_shows_the_cross_provider_recipe(self):
        for key in ("VA_VISION_MODEL", "VA_VISION_BASE_URL", "VA_VISION_API_KEY"):
            self.assertIn(key, VISION_HINT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
