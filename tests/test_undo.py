"""撤销功能的测试。

重点不是「能还原」，而是三件容易被忽略的事：
* 同一个文件在一次检查点里只记**最早**的内容，否则多步修改会互相覆盖；
* 一步都没改文件时不该产生空检查点，否则 /undo 会莫名消耗掉一次；
* 命令类工具的副作用无法追踪，必须**如实告知**而不是假装撤销成功。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import va.history as history_module  # noqa: E402
from helpers import AgentTestCase, ScriptedLLM, tc, turn  # noqa: E402
from va.history import UndoStack  # noqa: E402


class TestUndoStack(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.stack = UndoStack(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_restores_modified_file(self):
        target = self.root / "f.py"
        target.write_text("original\n")

        checkpoint = self.stack.begin("edit")
        self.stack.capture(checkpoint, target)
        target.write_text("changed\n")
        self.stack.commit(checkpoint)

        taken, blocked = self.stack.undo(1)

        self.assertEqual(target.read_text(), "original\n")
        self.assertEqual(len(taken), 1)
        self.assertEqual(blocked, [])

    def test_removes_newly_created_file(self):
        target = self.root / "brand_new.py"

        checkpoint = self.stack.begin("write")
        self.stack.capture(checkpoint, target)
        target.write_text("new content\n")
        self.stack.commit(checkpoint)

        self.stack.undo(1)

        self.assertFalse(target.exists(), "撤销后新建的文件应被删除")

    def test_same_file_records_earliest_content_only(self):
        """一步内改同一个文件多次，撤销必须回到这一步之前的状态。"""
        target = self.root / "f.txt"
        target.write_text("v0\n")

        checkpoint = self.stack.begin("multi")
        self.stack.capture(checkpoint, target)
        target.write_text("v1\n")
        self.stack.capture(checkpoint, target)  # 第二次抓取不应覆盖
        target.write_text("v2\n")
        self.stack.commit(checkpoint)

        self.stack.undo(1)

        self.assertEqual(target.read_text(), "v0\n")

    def test_multiple_steps_restore_in_correct_order(self):
        target = self.root / "f.txt"
        target.write_text("v0\n")

        for value in ("v1", "v2", "v3"):
            checkpoint = self.stack.begin(value)
            self.stack.capture(checkpoint, target)
            target.write_text(f"{value}\n")
            self.stack.commit(checkpoint)

        self.stack.undo(3)

        self.assertEqual(target.read_text(), "v0\n")

    def test_partial_undo(self):
        target = self.root / "f.txt"
        target.write_text("v0\n")
        for value in ("v1", "v2"):
            checkpoint = self.stack.begin(value)
            self.stack.capture(checkpoint, target)
            target.write_text(f"{value}\n")
            self.stack.commit(checkpoint)

        self.stack.undo(1)

        self.assertEqual(target.read_text(), "v1\n")
        self.assertEqual(len(self.stack), 1)

    def test_empty_checkpoint_is_not_pushed(self):
        checkpoint = self.stack.begin("什么都没干")
        self.assertIsNone(self.stack.commit(checkpoint))
        self.assertEqual(len(self.stack), 0)

    def test_opaque_checkpoint_is_pushed_and_reported(self):
        checkpoint = self.stack.begin("run_bash")
        self.stack.mark_opaque(checkpoint, "run_bash 的副作用无法追踪")
        self.assertIsNotNone(self.stack.commit(checkpoint))
        self.assertEqual(len(self.stack), 1)

        taken, blocked = self.stack.undo(1)

        # 没有文件快照的步骤不计入「撤销了一次文件改动」，但要如实报告
        self.assertEqual(taken, [])
        self.assertTrue(blocked)
        self.assertIn("无法追踪", blocked[0])
        self.assertEqual(len(self.stack), 0)

    def test_oversized_file_marks_opaque(self):
        target = self.root / "huge.bin"
        target.write_text("x" * 100)

        original_limit = history_module.MAX_SNAPSHOT_BYTES
        history_module.MAX_SNAPSHOT_BYTES = 10
        try:
            checkpoint = self.stack.begin("huge")
            self.stack.capture(checkpoint, target)
        finally:
            history_module.MAX_SNAPSHOT_BYTES = original_limit

        self.assertTrue(checkpoint.opaque)
        self.assertIn("5MB", checkpoint.opaque_reason)

    def test_undo_on_empty_stack(self):
        taken, blocked = self.stack.undo(1)
        self.assertEqual(taken, [])
        self.assertEqual(blocked, [])

    def test_undo_more_than_available_is_clamped(self):
        target = self.root / "f.txt"
        target.write_text("v0\n")
        checkpoint = self.stack.begin("one")
        self.stack.capture(checkpoint, target)
        target.write_text("v1\n")
        self.stack.commit(checkpoint)

        taken, _ = self.stack.undo(99)

        self.assertEqual(len(taken), 1)
        self.assertEqual(target.read_text(), "v0\n")

    def test_cumulative_diff_spans_all_steps(self):
        target = self.root / "f.txt"
        target.write_text("line1\n")

        for value in ("line2", "line3"):
            checkpoint = self.stack.begin(value)
            self.stack.capture(checkpoint, target)
            target.write_text(target.read_text() + f"{value}\n")
            self.stack.commit(checkpoint)

        changes = self.stack.cumulative_diff()

        self.assertEqual(len(changes), 1)
        path, diff_text = changes[0]
        self.assertEqual(path, "f.txt")
        self.assertIn("+line2", diff_text)
        self.assertIn("+line3", diff_text)

    def test_describe_lists_files_and_codes(self):
        new_file = self.root / "added.py"
        checkpoint = self.stack.begin("write")
        self.stack.capture(checkpoint, new_file)
        new_file.write_text("x")
        self.stack.commit(checkpoint)

        text = self.stack.describe(checkpoint)
        self.assertIn("added.py", text)
        self.assertIn("A", text)  # 新增


class TestAgentUndo(AgentTestCase):
    def test_undo_reverts_edit(self):
        target = self.root / "app.py"
        target.write_text("value = 1\n")

        llm = ScriptedLLM(
            turn("", tc("edit_file", path="app.py", old_string="value = 1", new_string="value = 2")),
            turn("改好了。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("改成 2")
        self.assertEqual(target.read_text(), "value = 2\n")

        message = agent.undo_last()

        self.assertEqual(target.read_text(), "value = 1\n")
        self.assertIn("已还原", message)

    def test_undo_deletes_created_file(self):
        llm = ScriptedLLM(
            turn("", tc("write_file", path="new.txt", content="hello\n")),
            turn("建好了。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("建个文件")
        self.assertTrue((self.root / "new.txt").exists())

        agent.undo_last()

        self.assertFalse((self.root / "new.txt").exists())

    def test_undo_without_history(self):
        llm = ScriptedLLM(turn("只是聊了句天"))
        agent, _ = self.make_agent(llm)
        agent.run("你好")
        self.assertEqual(agent.undo_last(), "没有可撤销的步骤。")

    def test_undo_appends_note_so_model_knows_disk_changed(self):
        llm = ScriptedLLM(
            turn("", tc("write_file", path="a.txt", content="x\n")),
            turn("好了。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("写文件")
        before = len(agent.messages)

        agent.undo_last()

        self.assertEqual(len(agent.messages), before + 1)
        note = agent.messages[-1]
        self.assertEqual(note["role"], "user")
        self.assertIn("撤销", note["content"])
        self.assertIn("重新 read_file", note["content"])

    def test_two_steps_undone_one_at_a_time(self):
        target = self.root / "f.txt"
        target.write_text("v0\n")
        llm = ScriptedLLM(
            turn("", tc("edit_file", path="f.txt", old_string="v0", new_string="v1")),
            turn("", tc("edit_file", path="f.txt", old_string="v1", new_string="v2")),
            turn("两步都做完了。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("改两次")
        self.assertEqual(target.read_text(), "v2\n")

        agent.undo_last(1)
        self.assertEqual(target.read_text(), "v1\n")

        agent.undo_last(1)
        self.assertEqual(target.read_text(), "v0\n")

    def test_command_step_is_flagged_as_not_undoable(self):
        llm = ScriptedLLM(
            turn("", tc("run_bash", command="echo hello")),
            turn("跑完了。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("跑个命令")

        message = agent.undo_last()

        self.assertIn("未还原", message)
        self.assertIn("run_bash", message)

    def test_undo_skips_steps_that_changed_no_files(self):
        """真实场景：改完文件又跑命令验证，/undo 必须回退文件本身。

        这是我们第一次实盘时踩到的坑——撤销栈顶是「跑测试」这个没有文件快照的
        步骤，结果 /undo 把它消费掉、报一句「无法撤销」，而真正该回退的编辑
        纹丝不动。用户说撤销，指的是把文件改回来。
        """
        target = self.root / "f.txt"
        target.write_text("v0\n")
        llm = ScriptedLLM(
            turn("", tc("edit_file", path="f.txt", old_string="v0", new_string="v1")),
            turn("", tc("run_bash", command="python3 -m unittest 验证一下")),
            turn("做完了。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("改完并验证")
        self.assertEqual(target.read_text(), "v1\n")

        message = agent.undo_last()

        self.assertEqual(target.read_text(), "v0\n", "撤销被没有文件改动的步骤吃掉了")
        self.assertIn("已还原", message)

    def test_undo_count_counts_file_changes_not_steps(self):
        target = self.root / "f.txt"
        target.write_text("v0\n")
        llm = ScriptedLLM(
            turn("", tc("edit_file", path="f.txt", old_string="v0", new_string="v1")),
            turn("", tc("run_bash", command="echo 检查")),
            turn("", tc("edit_file", path="f.txt", old_string="v1", new_string="v2")),
            turn("", tc("run_bash", command="echo 再检查")),
            turn("都做完了。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("改两次各验证一次")
        self.assertEqual(target.read_text(), "v2\n")

        agent.undo_last(1)
        self.assertEqual(target.read_text(), "v1\n")

        agent.undo_last(1)
        self.assertEqual(target.read_text(), "v0\n")

    def test_denied_action_creates_no_checkpoint(self):
        llm = ScriptedLLM(
            turn("", tc("write_file", path="x.txt", content="nope")),
            turn("被拒绝了。"),
        )
        agent, _ = self.make_agent(llm, approve=False)
        agent.run("写文件")

        self.assertEqual(len(agent.undo), 0)
        self.assertEqual(agent.undo_last(), "没有可撤销的步骤。")

    def test_read_only_step_creates_no_checkpoint(self):
        (self.root / "a.txt").write_text("data\n")
        llm = ScriptedLLM(
            turn("", tc("read_file", path="a.txt")),
            turn("读完了。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("读文件")
        self.assertEqual(len(agent.undo), 0)

    def test_checkpoint_records_file_codes(self):
        (self.root / "old.txt").write_text("v0\n")
        # 两个工具调用放在同一轮里 —— 才算「一步」
        llm = ScriptedLLM(
            turn(
                "",
                tc("edit_file", path="old.txt", old_string="v0", new_string="v1"),
                tc("write_file", path="new.txt", content="hi\n"),
            ),
            turn("做完了。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("改一个建一个")

        checkpoints = agent.undo.list()
        self.assertEqual(len(checkpoints), 1)
        described = agent.undo.describe(checkpoints[0])
        self.assertIn("old.txt", described)
        self.assertIn("new.txt", described)

    def test_one_checkpoint_per_step_not_per_tool_call(self):
        """一轮里调 3 个工具算一步，不该产生 3 个检查点。"""
        llm = ScriptedLLM(
            turn(
                "",
                tc("write_file", path="a.txt", content="1\n"),
                tc("write_file", path="b.txt", content="2\n"),
                tc("write_file", path="c.txt", content="3\n"),
            ),
            turn("done"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("建三个文件")
        self.assertEqual(len(agent.undo), 1)

        agent.undo_last()
        for name in ("a.txt", "b.txt", "c.txt"):
            self.assertFalse((self.root / name).exists(), f"{name} 应被撤销删除")


if __name__ == "__main__":
    unittest.main(verbosity=2)
