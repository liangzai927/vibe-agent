"""agent 提议改配置（生产工作流·阶段 A 收尾）。

**不是直接写文件，是提议**：算 diff → 你确认 → 落盘 + 记「为什么改的」。

这是阶段 I（自进化）的落地端——I 负责从反馈和统计推导该改什么，
这里负责把改动**安全地**落下去。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402
from va.tools.base import ToolError  # noqa: E402
from va.workflow import load, template  # noqa: E402
from va.workflow_edit import (  # noqa: E402
    Change,
    apply_changes,
    diff_text,
    history,
    set_values,
)


class TestSurgicalEditing(unittest.TestCase):
    """改 YAML 时**保留注释**——配置文件不是数据文件，注释是它的一半价值。"""

    def test_replaces_an_existing_value(self):
        text = "branch:\n  # 说明\n  base: main\n  pattern: feature/x\n"
        out, notes = set_values(text, [Change("branch.base", "develop")])
        self.assertIn("  base: develop", out)
        self.assertIn("# 说明", out, "注释被抹掉了")
        self.assertIn("main → develop", notes[0])

    def test_uncomments_a_commented_key(self):
        """**最要紧的一种**：模板生成的文件里字段全是注释。

        不处理它，「改一个已有配置」会变成「在文件末尾多出一个重复的段」。
        """
        text = "branch:\n  # 从哪个分支切出\n  # base: main\n"
        out, notes = set_values(text, [Change("branch.base", "develop")])
        self.assertIn("  base: develop", out)
        self.assertNotIn("# base:", out)
        self.assertIn("取消注释", notes[0])

    def test_uncommenting_keeps_the_right_indentation(self):
        """**踩过的坑**：我把「注释符之前的空白」又加了两个空格，
        生成出 `    base: develop`（4 空格）。"""
        out, _ = set_values("branch:\n  # base: main\n", [Change("branch.base", "develop")])
        self.assertIn("\n  base: develop", out)
        self.assertNotIn("    base: develop", out)

    def test_inserts_a_missing_key_into_its_section(self):
        text = "branch:\n  base: main\n\ncommit:\n  template: x\n"
        out, notes = set_values(text, [Change("branch.slug_ascii", True)])
        self.assertIn("在 branch 段新增", notes[0])
        self.assertLess(out.index("slug_ascii"), out.index("commit:"), "插错段落了")

    def test_appends_a_missing_section(self):
        out, notes = set_values("", [Change("mcp.approve", "read")])
        self.assertIn("新增段落", notes[0])
        self.assertIn("mcp:\n  approve: read", out)

    def test_leaves_other_lines_untouched(self):
        text = "branch:\n  # 好好的一条注释\n  base: main\n# 末尾注释\n"
        out, _ = set_values(text, [Change("branch.base", "develop")])
        self.assertIn("# 好好的一条注释", out)
        self.assertIn("# 末尾注释", out)

    def test_renders_booleans_and_lists(self):
        out, _ = set_values(
            "",
            [
                Change("branch.slug_ascii", True),
                Change("commit.forbid_words", ["WIP", "fix bug"]),
                Change("test.gate", "warn"),
            ],
        )
        self.assertIn("slug_ascii: true", out)
        self.assertIn('forbid_words: ["WIP", "fix bug"]', out)
        self.assertIn("gate: warn", out)

    def test_bad_path_is_rejected(self):
        with self.assertRaises(ValueError):
            set_values("", [Change("nodot", 1)])

    def test_diff_shows_the_change(self):
        text = diff_text("a: 1\n", "a: 2\n")
        self.assertIn("-a: 1", text)
        self.assertIn("+a: 2", text)


class TestApplyAndHistory(AgentTestCase):
    def test_editing_the_generated_template_round_trips(self):
        """**真加载一遍**——文本改对了不等于配置生效了。"""
        config = self.root / ".vibe" / "workflow.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(template(), encoding="utf-8")

        self.assertEqual(load(self.root).branch_base, "main")
        apply_changes(self.root, [Change("branch.base", "develop", reason="从 develop 切")])

        workflow = load(self.root)
        self.assertEqual(workflow.branch_base, "develop")
        self.assertEqual(workflow.origin_of("branch.base"), "项目级")
        self.assertEqual(workflow.origin_of("branch.pattern"), "默认值", "别的字段被误改了")

    def test_comments_survive(self):
        config = self.root / ".vibe" / "workflow.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(template(), encoding="utf-8")
        before = config.read_text(encoding="utf-8").count("#")

        apply_changes(self.root, [Change("branch.base", "develop")])
        self.assertGreaterEqual(config.read_text(encoding="utf-8").count("#"), before - 1)

    def test_history_records_who_and_why(self):
        """**可归因**：半年后看到 `gate: warn`，你得知道为什么。"""
        apply_changes(
            self.root,
            [Change("test.gate", "warn", reason="样式测试跑不起来")],
            source="用户反馈",
        )
        entries = history(self.root)
        self.assertEqual(len(entries), 1)
        change = entries[0]["changes"][0]
        self.assertEqual(change["path"], "test.gate")
        self.assertEqual(change["reason"], "样式测试跑不起来")
        self.assertEqual(entries[0]["source"], "用户反馈")

    def test_history_accumulates(self):
        apply_changes(self.root, [Change("test.gate", "warn")])
        apply_changes(self.root, [Change("branch.base", "dev")])
        self.assertEqual(len(history(self.root)), 2)

    def test_bad_history_file_is_not_fatal(self):
        path = self.root / ".vibe" / "workflow-history.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ 不是 json", encoding="utf-8")
        self.assertEqual(history(self.root), [])
        apply_changes(self.root, [Change("branch.base", "dev")])
        self.assertEqual(len(history(self.root)), 1)


class TestTool(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        from va.loop import Agent
        from va.ui import UI

        self.agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)
        self.tool = self.agent.registry.get("propose_workflow_change")

    def test_registered_and_requires_approval(self):
        self.assertIsNotNone(self.tool)
        self.assertTrue(self.tool.requires_approval, "改团队约定必须你点头")

    def test_preview_shows_a_diff_not_json(self):
        """批准配置改动之前，你要看的是「哪一行变成什么」。"""
        text = self.tool.preview(
            {"changes": [{"path": "mock.dir", "value": "src/__mocks__"}], "summary": "对不上"}
        )
        self.assertIn("---", text + "---")
        self.assertIn("+  dir: src/__mocks__", text)
        self.assertIn("对不上", text)
        self.assertNotIn('"path"', text, "显示的是原始 JSON 而不是 diff")

    def test_unknown_path_says_what_is_available(self):
        text = self.tool.preview({"changes": [{"path": "mock.dri", "value": "x"}]})
        self.assertIn("没有", text)
        self.assertIn("mock.dir", text, "要提示最接近的那个")

    def test_type_mismatch_blocks_before_writing(self):
        with self.assertRaises(ToolError) as ctx:
            self.tool.run(changes=[{"path": "branch.slug_max_length", "value": "四十"}])
        self.assertIn("int", str(ctx.exception))
        self.assertFalse((self.root / ".vibe" / "workflow.yaml").is_file(), "拦住却还是写了")

    def test_cross_field_problems_are_caught(self):
        """「要求带需求号但模板里没有 {ticket}」这种单个字段看不出来的问题。"""
        with self.assertRaises(ToolError) as ctx:
            self.tool.run(
                changes=[
                    {"path": "commit.require_ticket", "value": True},
                    {"path": "commit.template", "value": "{type}: {subject}"},
                ]
            )
        self.assertIn("ticket", str(ctx.exception))

    def test_applies_and_reports(self):
        output = self.tool.run(
            changes=[{"path": "mock.dir", "value": "src/__mocks__", "reason": "用户说的"}],
            summary="mock 目录和实际不符",
        )
        self.assertIn("已修改", output)
        self.assertEqual(load(self.root).get("mock.dir"), "src/__mocks__")
        self.assertIn("下次会话生效", output)

    def test_empty_changes(self):
        with self.assertRaises(ToolError):
            self.tool.run(changes=[])


if __name__ == "__main__":
    unittest.main(verbosity=2)
