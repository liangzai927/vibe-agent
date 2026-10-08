"""diff 预览的测试：既要 diff 本身正确，也要它在审批流程里真的被用上。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase, ScriptedLLM, tc, turn  # noqa: E402
from va.diffview import colorize, describe_change, diff_stats, make_diff, truncate_diff  # noqa: E402
from va.tools.fs import EditFile, ReadFile, WriteFile  # noqa: E402


class TestDiffEngine(unittest.TestCase):
    def test_basic_diff(self):
        text = make_diff("a\nb\n", "a\nc\n", "x.py")
        self.assertIn("--- a/x.py", text)
        self.assertIn("+++ b/x.py", text)
        self.assertIn("-b", text)
        self.assertIn("+c", text)
        self.assertIn("@@", text)

    def test_identical_content_gives_empty(self):
        self.assertEqual(make_diff("same\n", "same\n", "x.py"), "")

    def test_new_file_all_additions(self):
        text = make_diff("", "line1\nline2\n", "new.py")
        self.assertIn("+line1", text)
        self.assertIn("+line2", text)
        self.assertNotIn("\n-", text)

    def test_missing_trailing_newline_not_mangled(self):
        """经典坑：直接对 splitlines(keepends=True) 做 diff 会把末行拼成 '-b+c'。"""
        text = make_diff("a\nb", "a\nc", "x.py")
        lines = text.splitlines()
        self.assertIn("-b", lines)
        self.assertIn("+c", lines)
        self.assertNotIn("-b+c", text)

    def test_stats_counts_only_real_changes(self):
        text = make_diff("a\nb\nc\n", "a\nx\ny\nc\n", "f")
        self.assertEqual(diff_stats(text), (2, 1))

    def test_stats_ignores_file_headers(self):
        text = make_diff("a\n", "b\n", "f")
        added, removed = diff_stats(text)
        self.assertEqual((added, removed), (1, 1))

    def test_truncate_folds_middle(self):
        old = "\n".join(f"line{i}" for i in range(400))
        new = "\n".join(f"changed{i}" for i in range(400))
        text = make_diff(old, new, "big.py")
        folded = truncate_diff(text, max_lines=50)
        self.assertLess(len(folded.splitlines()), len(text.splitlines()))
        self.assertIn("省略", folded)

    def test_colorize_preserves_line_count(self):
        text = make_diff("a\nb\n", "c\nd\n", "f")
        self.assertEqual(len(colorize(text).splitlines()), len(text.splitlines()))

    def test_colorize_marks_additions_green_deletions_red(self):
        import va.util as util

        original = util._COLOR
        util._COLOR = True
        try:
            text = make_diff("a\n", "b\n", "f")
            colored = colorize(text)
            self.assertIn("\033[32m", colored, "新增行应为绿色")
            self.assertIn("\033[31m", colored, "删除行应为红色")
        finally:
            util._COLOR = original

    def test_describe_change_header_carries_stats(self):
        text = describe_change("x.py", "a\nb\n", "a\nc\n")
        self.assertIsNotNone(text)
        header = text.splitlines()[0]
        self.assertIn("x.py", header)
        self.assertIn("+1", header)
        self.assertIn("-1", header)

    def test_describe_change_none_when_identical(self):
        self.assertIsNone(describe_change("x.py", "a\n", "a\n"))


class TestToolPreview(AgentTestCase):
    """预览必须在真正的工具上工作，而不是只在 diff 引擎上。"""

    def test_write_preview_shows_before_and_after(self):
        (self.root / "f.txt").write_text("old\n")
        preview = WriteFile(self.workspace).preview({"path": "f.txt", "content": "new\n"})
        self.assertIsNotNone(preview)
        self.assertIn("-old", preview)
        self.assertIn("+new", preview)

    def test_write_preview_marks_new_file(self):
        preview = WriteFile(self.workspace).preview({"path": "brand.txt", "content": "hi\n"})
        self.assertIn("新文件", preview)
        self.assertIn("+hi", preview)

    def test_edit_preview_shows_exact_change(self):
        (self.root / "f.py").write_text("def f():\n    return 1\n")
        preview = EditFile(self.workspace).preview(
            {"path": "f.py", "old_string": "return 1", "new_string": "return 2"}
        )
        self.assertIn("-    return 1", preview)
        self.assertIn("+    return 2", preview)

    def test_edit_preview_warns_when_match_missing(self):
        (self.root / "f.py").write_text("abc\n")
        preview = EditFile(self.workspace).preview(
            {"path": "f.py", "old_string": "zzz", "new_string": "y"}
        )
        self.assertIn("找不到", preview)
        self.assertIn("会失败", preview)

    def test_edit_preview_warns_when_ambiguous(self):
        (self.root / "f.py").write_text("dup\ndup\n")
        preview = EditFile(self.workspace).preview(
            {"path": "f.py", "old_string": "dup", "new_string": "y"}
        )
        self.assertIn("不唯一", preview)

    def test_edit_preview_warns_when_noop(self):
        (self.root / "f.py").write_text("abc\n")
        preview = EditFile(self.workspace).preview(
            {"path": "f.py", "old_string": "abc", "new_string": "abc"}
        )
        self.assertIn("相同", preview)

    def test_preview_never_raises_on_bad_args(self):
        write = WriteFile(self.workspace)
        edit = EditFile(self.workspace)
        for tool in (write, edit):
            self.assertIsNone(tool.preview({}))
            self.assertIsNone(tool.preview({"path": "x"}))
            self.assertIsNone(tool.preview({"path": 123, "content": "x", "old_string": "a", "new_string": "b"}))

    def test_preview_outside_workspace_is_none(self):
        self.assertIsNone(WriteFile(self.workspace).preview({"path": "../../evil.txt", "content": "x"}))

    def test_affected_paths_targets_the_file(self):
        tool = WriteFile(self.workspace)
        # 注意用 workspace.root：它已经 resolve 过软链（macOS 上 /var -> /private/var）
        self.assertEqual(
            tool.affected_paths({"path": "a/b.txt"}),
            [self.workspace.root / "a" / "b.txt"],
        )
        self.assertEqual(tool.affected_paths({"path": "../../x"}), [])
        self.assertEqual(tool.affected_paths({}), [])
        self.assertEqual(tool.affected_paths({"path": 123}), [])

    def test_read_only_tool_has_no_preview_or_paths(self):
        tool = ReadFile(self.workspace)
        (self.root / "a.txt").write_text("x")
        self.assertIsNone(tool.preview({"path": "a.txt"}))
        self.assertEqual(tool.affected_paths({"path": "a.txt"}), [])


class TestPreviewInApproval(AgentTestCase):
    """最关键的一环：预览要真的出现在审批里。"""

    def test_agent_hands_diff_to_confirm(self):
        llm = ScriptedLLM(
            turn("", tc("write_file", path="a.txt", content="hello\n")),
            turn("完成。"),
        )
        agent, ui = self.make_agent(llm)
        agent.run("写个文件")

        self.assertIsNotNone(ui.preview, "审批时没有拿到预览")
        self.assertIn("+hello", ui.preview)

    def test_edit_approval_shows_diff_not_raw_args(self):
        (self.root / "f.py").write_text("value = 1\n")
        llm = ScriptedLLM(
            turn("", tc("edit_file", path="f.py", old_string="value = 1", new_string="value = 2")),
            turn("完成。"),
        )
        agent, ui = self.make_agent(llm)
        agent.run("改一下")
        self.assertIn("-value = 1", ui.preview)
        self.assertIn("+value = 2", ui.preview)

    def test_preview_exception_does_not_block_execution(self):
        llm = ScriptedLLM(
            turn("", tc("write_file", path="a.txt", content="hello\n")),
            turn("完成。"),
        )
        agent, _ = self.make_agent(llm)
        tool = agent.registry.get("write_file")

        def boom(args):
            raise RuntimeError("预览炸了")

        tool.preview = boom  # type: ignore[method-assign]
        answer = agent.run("写个文件")

        self.assertEqual(answer, "完成。")
        self.assertEqual((self.root / "a.txt").read_text(), "hello\n")

    def test_bash_without_preview_still_asks(self):
        llm = ScriptedLLM(
            turn("", tc("run_bash", command="echo hi")),
            turn("完成。"),
        )
        agent, ui = self.make_agent(llm, approve=False)
        agent.run("跑命令")
        self.assertIn("run_bash", ui.denied)


if __name__ == "__main__":
    unittest.main(verbosity=2)
