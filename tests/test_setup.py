"""首次配置向导与模型目录的测试。

向导是靠「问用户」推进的，所以测试的关键是把 ``ask`` / ``secret`` 注入进去——
否则这段代码永远只能靠人手点一遍来验证，而那正是最容易漏 case 的地方。

网络一律 mock 掉：这些测试要能在断网、没有 key 的机器上跑。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from va import models as models_module  # noqa: E402
from va import setup as setup_module  # noqa: E402
from va.config import write_env_values  # noqa: E402
from va.models import (  # noqa: E402
    FetchResult,
    ModelCatalog,
    ModelInfo,
    fetch_models,
    load_catalog,
    save_catalog,
    suggest_model,
)


class ScriptedAsker:
    """按剧本回答，并把问过的话记下来。"""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else ""

    def asked(self, needle: str) -> bool:
        return any(needle in p for p in self.prompts)


def catalog_models(*items) -> list[ModelInfo]:
    out = []
    for item in items:
        if isinstance(item, str):
            out.append(ModelInfo(id=item))
        else:
            out.append(ModelInfo(**item))
    return out


# --------------------------------------------------------------- 模型目录


class TestModelInfo(unittest.TestCase):
    def test_supports_image(self):
        self.assertTrue(ModelInfo(id="a", input_modalities=["text", "image"]).supports_image)
        self.assertFalse(ModelInfo(id="b", input_modalities=["text"]).supports_image)
        self.assertFalse(ModelInfo(id="c").supports_image)

    def test_summary_mentions_capabilities(self):
        info = ModelInfo(
            id="deepseek-flash",
            name="DeepSeek-V4.1-Flash",
            input_modalities=["text", "image"],
            context_window=1_048_576,
        )
        self.assertIn("DeepSeek-V4.1-Flash", info.summary)
        self.assertIn("1048k", info.summary)
        self.assertIn("可读图", info.summary)

    def test_summary_skips_redundant_name(self):
        self.assertEqual(ModelInfo(id="x", name="x").summary, "")

    def test_round_trip(self):
        info = ModelInfo(id="m", name="N", input_modalities=["image"], context_window=1000)
        self.assertEqual(ModelInfo.from_dict(info.to_dict()).input_modalities, ["image"])


class TestCatalog(unittest.TestCase):
    def test_round_trip_through_disk(self):
        with tempfile.TemporaryDirectory() as d:
            directory = Path(d)
            catalog = ModelCatalog(
                base_url="https://x/v1",
                models=catalog_models(
                    {"id": "a", "input_modalities": ["text", "image"]}, "b"
                ),
            )
            save_catalog(directory, catalog)

            loaded = load_catalog(directory)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.ids, ["a", "b"])
            self.assertTrue(loaded.find("a").supports_image)
            self.assertFalse(loaded.find("b").supports_image)
            self.assertIsNone(loaded.find("nope"))

    def test_vision_candidates(self):
        catalog = ModelCatalog(
            base_url="x",
            models=catalog_models(
                {"id": "seer", "input_modalities": ["text", "image"]},
                "blind",
            ),
        )
        self.assertEqual([m.id for m in catalog.vision_candidates], ["seer"])

    def test_missing_catalog_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(load_catalog(Path(d)))

    def test_corrupt_catalog_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            directory = Path(d)
            (directory / models_module.CATALOG_FILENAME).write_text("{ 坏掉的 json")
            self.assertIsNone(load_catalog(directory))

    def test_tolerates_plain_string_entries(self):
        """老版本缓存里 models 是字符串数组，升级后不能读不了。"""
        with tempfile.TemporaryDirectory() as d:
            directory = Path(d)
            (directory / models_module.CATALOG_FILENAME).write_text(
                json.dumps({"base_url": "x", "models": ["a", "b"]}), encoding="utf-8"
            )
            loaded = load_catalog(directory)
            self.assertEqual(loaded.ids, ["a", "b"])


class TestSuggestModel(unittest.TestCase):
    def test_known_hosts(self):
        self.assertEqual(suggest_model("https://api.deepseek.com/v1"), "deepseek-chat")
        self.assertEqual(suggest_model("https://api.openai.com/v1"), "gpt-4o")
        self.assertEqual(suggest_model("http://localhost:11434/v1"), "qwen2.5-coder:14b")

    def test_unknown_host_uses_fallback(self):
        self.assertEqual(suggest_model("https://unknown.example/v1"), "deepseek-chat")
        self.assertEqual(suggest_model("", fallback="自定义"), "自定义")


class TestFetchModels(unittest.TestCase):
    def test_empty_inputs_fail_without_network(self):
        self.assertFalse(fetch_models("", "k").ok)
        self.assertIn("base_url", fetch_models("", "k").why())
        self.assertFalse(fetch_models("https://x/v1", "").ok)
        self.assertIn("API key", fetch_models("https://x/v1", "").why())

    def test_connection_error_is_explained(self):
        import openai

        with mock.patch("openai.OpenAI") as client:
            client.return_value.models.list.side_effect = openai.APIConnectionError(
                request=mock.Mock()
            )
            result = fetch_models("https://nope.example/v1", "k")
        self.assertFalse(result.ok)
        self.assertIn("连不上", result.why())

    def test_missing_endpoint_says_manual(self):
        import openai

        with mock.patch("openai.OpenAI") as client:
            client.return_value.models.list.side_effect = openai.NotFoundError(
                "no /models", response=mock.Mock(status_code=404), body=None
            )
            result = fetch_models("https://x/v1", "k")
        self.assertFalse(result.ok)
        self.assertIn("手动", result.why())

    def test_parses_capabilities(self):
        item_full = mock.Mock(id="seer")
        item_full.model_extra = {
            "name": "Seer",
            "input_modalities": ["text", "image"],
            "context_window": 200_000,
        }
        item_plain = mock.Mock(id="plain")
        item_plain.model_extra = {}

        with mock.patch("openai.OpenAI") as client:
            client.return_value.models.list.return_value = mock.Mock(
                data=[item_plain, item_full, item_full]  # 故意重复
            )
            result = fetch_models("https://x/v1", "k")

        self.assertTrue(result.ok, result.why())
        self.assertEqual(len(result.models), 2, "重复的模型没去掉")
        seer = next(m for m in result.models if m.id == "seer")
        self.assertTrue(seer.supports_image)
        self.assertEqual(seer.context_window, 200_000)

    def test_embedding_models_sorted_last(self):
        items = []
        for name in ("text-embedding-3", "gpt-4o", "whisper-1"):
            item = mock.Mock(id=name)
            item.model_extra = {}
            items.append(item)

        with mock.patch("openai.OpenAI") as client:
            client.return_value.models.list.return_value = mock.Mock(data=items)
            result = fetch_models("https://x/v1", "k")

        self.assertEqual(result.models[0].id, "gpt-4o", "能干活的模型应该排前面")


# --------------------------------------------------------------- 向导


class WizardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.env_path = self.dir / ".env"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_wizard(self, *answers, models=None, ok=True, error="", verify=(True, "连通"), **kwargs):
        asker = ScriptedAsker(*answers)
        sayings: list[str] = []

        fetched = FetchResult(ok, models or [], error)
        with mock.patch.object(setup_module, "fetch_models", return_value=fetched):
            with mock.patch.object(setup_module, "_verify", return_value=verify):
                result = setup_module.run(
                    self.env_path,
                    ask=asker,
                    say=sayings.append,
                    secret=lambda prompt: "sk-secret",
                    directory=self.dir,
                    **kwargs,
                )
        return result, asker, "\n".join(sayings)

    def read_env(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in self.env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                out[key.strip()] = value.strip()
        return out


class TestWizardHappyPath(WizardTestCase):
    def test_preset_writes_all_three(self):
        result, _, _ = self.run_wizard("1", "")

        self.assertTrue(result.ok, result.error)
        env = self.read_env()
        self.assertEqual(env["VA_API_KEY"], "sk-secret")
        self.assertEqual(env["VA_BASE_URL"], "https://api.deepseek.com/v1")
        self.assertEqual(env["VA_MODEL"], "deepseek-chat")

    def test_custom_url_is_accepted(self):
        result, _, _ = self.run_wizard("4", "https://my.example/v1", "", "")

        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.read_env()["VA_BASE_URL"], "https://my.example/v1")

    def test_pasting_a_url_at_the_preset_prompt_works(self):
        """很多人会直接把地址粘进选择框，不该被当成无效输入。"""
        result, _, _ = self.run_wizard("https://api.openai.com/v1", "")

        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.read_env()["VA_BASE_URL"], "https://api.openai.com/v1")

    def test_missing_key_aborts(self):
        asker = ScriptedAsker("1")
        with mock.patch.object(setup_module, "fetch_models") as fetch:
            result = setup_module.run(
                self.env_path,
                ask=asker,
                say=lambda *_: None,
                secret=lambda prompt: "",
                directory=self.dir,
            )
        self.assertFalse(result.ok)
        self.assertIn("API key", result.error)
        fetch.assert_not_called()
        self.assertFalse(self.env_path.exists(), "没配成却写了文件")

    def test_verification_failure_still_saves_config(self):
        """配错了也要落盘——否则用户改了别的地方重试，得从头再填一遍。"""
        result, _, text = self.run_wizard("1", "", verify=(False, "401 未授权"))

        self.assertTrue(result.ok)
        self.assertFalse(result.verified)
        self.assertTrue(self.env_path.exists())


class TestWizardModelPicking(WizardTestCase):
    MODELS = catalog_models(
        {"id": "plain"},
        {"id": "seer", "input_modalities": ["text", "image"], "context_window": 200_000},
    )

    def test_defaults_to_suggested_model(self):
        result, _, _ = self.run_wizard("2", "", "n", models=self.MODELS)
        # OpenAI 预设的建议值是 gpt-4o，但清单里没有它 → 会补进列表当默认
        self.assertEqual(result.model, "gpt-4o")

    def test_picks_by_index(self):
        result, _, _ = self.run_wizard("1", "2", "n", models=self.MODELS)
        # 列表补入了 deepseek-chat 在最前，索引 2 是 plain
        self.assertEqual(result.model, "plain")

    def test_picks_by_name(self):
        result, _, _ = self.run_wizard("1", "seer", "n", models=self.MODELS)
        self.assertEqual(result.model, "seer")

    def test_blank_answer_uses_default(self):
        result, _, _ = self.run_wizard("1", "", "n", models=self.MODELS)
        self.assertEqual(result.model, "deepseek-chat")

    def test_suggested_default_is_added_to_the_list(self):
        """服务商的 /models 常常不列别名，但默认值确实能用。

        不补进去的话，界面会出现「回车用 X」而列表里没有 X 的尴尬。
        """
        _, _, text = self.run_wizard("1", "", "n", models=self.MODELS)
        self.assertIn("deepseek-chat", text)
        self.assertIn("服务商未列出", text)

    def test_fetch_failure_falls_back_to_typing(self):
        result, asker, text = self.run_wizard("1", "my-custom-model", "", ok=False, error="没有 /models 接口")

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.model, "my-custom-model")
        self.assertIn("拉不到模型清单", text)
        self.assertTrue(asker.asked("模型名"))


class TestWizardVisionModel(WizardTestCase):
    MODELS = catalog_models(
        "blind",
        {"id": "seer", "input_modalities": ["text", "image"]},
    )

    def test_auto_suggests_image_capable_model(self):
        result, asker, text = self.run_wizard("1", "blind", "y", models=self.MODELS)

        self.assertEqual(result.vision_model, "seer")
        self.assertIn("发现能读图的模型", text)
        self.assertIn("不需要另外申请", text, "应该点明同一个 key 就能用")
        self.assertIn("VA_VISION_MODEL", self.read_env())

    def test_declining_the_suggestion_asks_manually(self):
        result, asker, _ = self.run_wizard("1", "blind", "n", "my-vision", models=self.MODELS)
        self.assertEqual(result.vision_model, "my-vision")

    def test_no_image_capable_model_asks_manually(self):
        """清单里没有能读图的模型时，直接问用户填名字。

        注意这里第三个回答是**模型名**，不是 y/n——因为自动推荐那一步被跳过了。
        """
        result, asker, text = self.run_wizard("1", "", "my-vision", models=catalog_models("blind"))

        self.assertEqual(result.vision_model, "my-vision")
        self.assertNotIn("发现能读图的模型", text)
        self.assertTrue(asker.asked("视觉模型名"))
        self.assertEqual(self.read_env()["VA_VISION_MODEL"], "my-vision")

    def test_vision_prompt_can_be_skipped(self):
        result, _, _ = self.run_wizard("1", "", "", models=catalog_models("blind"))
        self.assertTrue(result.ok)
        self.assertEqual(result.vision_model, "")
        self.assertNotIn("VA_VISION_MODEL", self.read_env())


class TestWizardNonInteractive(WizardTestCase):
    def test_requires_base_url(self):
        result = setup_module.run(
            self.env_path, ask=lambda p: "", say=lambda *_: None,
            directory=self.dir, interactive=False,
        )
        self.assertFalse(result.ok)
        self.assertIn("base_url", result.error)

    def test_requires_api_key(self):
        result = setup_module.run(
            self.env_path, ask=lambda p: "", say=lambda *_: None,
            directory=self.dir, interactive=False,
            base_url="https://api.deepseek.com/v1",
        )
        self.assertFalse(result.ok)
        self.assertIn("api_key", result.error)

    def test_full_non_interactive_run(self):
        with mock.patch.object(
            setup_module, "fetch_models",
            return_value=FetchResult(True, catalog_models(
                {"id": "seer", "input_modalities": ["text", "image"]}
            )),
        ):
            with mock.patch.object(setup_module, "_verify", return_value=(True, "ok")):
                result = setup_module.run(
                    self.env_path, ask=lambda p: "", say=lambda *_: None,
                    directory=self.dir, interactive=False,
                    base_url="https://api.deepseek.com/v1",
                    api_key="sk-x",
                )

        self.assertTrue(result.ok, result.error)
        env = self.read_env()
        # 非交互模式下主模型保持主机名推断出来的默认值——不静默改成另一个，
        # 否则自动化脚本会拿到和预期不同的模型，很难排查。
        self.assertEqual(env["VA_MODEL"], "deepseek-chat")
        self.assertEqual(env["VA_VISION_MODEL"], "seer", "非交互模式下应自动采用可读图模型")


class TestCatalogIsSaved(WizardTestCase):
    def test_catalog_written_after_fetch(self):
        models = catalog_models({"id": "seer", "input_modalities": ["text", "image"]})
        self.run_wizard("1", "", "n", models=models)

        catalog = load_catalog(self.dir)
        self.assertIsNotNone(catalog)
        self.assertIn("seer", catalog.ids)

    def test_no_catalog_when_fetch_failed(self):
        self.run_wizard("1", "typed-model", "", ok=False)
        self.assertIsNone(load_catalog(self.dir), "拉失败就不该留一份空清单")


class TestEnvFileUpdate(unittest.TestCase):
    """向导靠 write_env_values 落盘，它不能把用户自己写的东西冲掉。"""

    def test_preserves_comments_and_other_keys(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / ".env"
            path.write_text(
                "# 我自己的注释\nVA_AUTO_APPROVE=1\nVA_API_KEY=old\n",
                encoding="utf-8",
            )
            write_env_values(path, {"VA_API_KEY": "new", "VA_MODEL": "m"})

            text = path.read_text(encoding="utf-8")
            self.assertIn("# 我自己的注释", text)
            self.assertIn("VA_AUTO_APPROVE=1", text)
            self.assertIn("VA_API_KEY=new", text)
            self.assertIn("VA_MODEL=m", text)
            self.assertNotIn("old", text)

    def test_replaces_commented_out_duplicates(self):
        """否则文件里会有两个同名项，看不出哪个生效。"""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / ".env"
            path.write_text("# VA_MODEL=旧值\n", encoding="utf-8")
            write_env_values(path, {"VA_MODEL": "新值"})

            text = path.read_text(encoding="utf-8")
            self.assertIn("VA_MODEL=新值", text)
            self.assertNotIn("旧值", text)

    def test_sets_restrictive_permissions(self):
        import os

        if os.name == "nt":
            self.skipTest("Windows 权限模型不同")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / ".env"
            write_env_values(path, {"VA_API_KEY": "sk-x"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
