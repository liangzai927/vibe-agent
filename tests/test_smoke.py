"""冒烟测试：用脚本化的假 LLM 驱动真实的工具与主循环，不需要 API key。

运行：python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase, QuietUI, ScriptedLLM, tc, turn  # noqa: E402
from va.config import Config  # noqa: E402
from va.loop import Agent  # noqa: E402
from va.messages import AssistantTurn, ToolCall  # noqa: E402
from va.tools import build_registry  # noqa: E402
from va.tools.git import GitStatus  # noqa: E402
from va.ui import UI  # noqa: E402
from va.workspace import Workspace, WorkspaceError  # noqa: E402


# --------------------------------------------------------------------- 基础


class TestBasics(AgentTestCase):
    def test_direct_answer_without_tools(self):
        llm = ScriptedLLM(turn("你好，我是 agent。"))
        agent, _ = self.make_agent(llm)
        self.assertEqual(agent.run("打个招呼"), "你好，我是 agent。")
        self.assertEqual(len(llm.received), 1)

    def test_system_prompt_contains_workspace(self):
        llm = ScriptedLLM(turn("ok"))
        agent, _ = self.make_agent(llm)
        agent.run("hi")
        system = llm.received[0][0]
        self.assertEqual(system["role"], "system")
        self.assertIn(str(self.root), system["content"])
        self.assertIn("run_bash", system["content"])

    def test_tools_are_sent_to_model(self):
        llm = ScriptedLLM(turn("ok"))
        agent, _ = self.make_agent(llm)
        captured = {}
        original = llm.complete

        def spy(messages, tools=None, **kw):
            captured["tools"] = tools
            return original(messages, tools=tools, **kw)

        llm.complete = spy  # type: ignore[assignment]
        agent.run("hi")
        names = {t["function"]["name"] for t in captured["tools"]}
        self.assertIn("read_file", names)
        self.assertIn("git_commit", names)


# --------------------------------------------------------------------- 文件


class TestFileTools(AgentTestCase):
    def test_write_then_read_roundtrip(self):
        llm = ScriptedLLM(
            turn("", tc("write_file", path="src/hello.py", content="print('hi')\n")),
            turn("", tc("read_file", path="src/hello.py")),
            turn("搞定。"),
        )
        agent, _ = self.make_agent(llm)
        answer = agent.run("创建 src/hello.py")

        self.assertEqual(answer, "搞定。")
        written = self.root / "src" / "hello.py"
        self.assertTrue(written.is_file())
        self.assertEqual(written.read_text(), "print('hi')\n")

        # read_file 的结果应带行号回灌给模型
        tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
        self.assertIn("1\tprint('hi')", tool_msgs[-1]["content"])

    def test_edit_file_exact_replace(self):
        target = self.root / "app.py"
        target.write_text("def main():\n    return 1\n")
        llm = ScriptedLLM(
            turn("", tc("edit_file", path="app.py", old_string="return 1", new_string="return 2")),
            turn("改好了。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("把返回值改成 2")
        self.assertEqual(target.read_text(), "def main():\n    return 2\n")

    def test_edit_file_reports_missing_match(self):
        (self.root / "a.txt").write_text("hello\n")
        llm = ScriptedLLM(
            turn("", tc("edit_file", path="a.txt", old_string="not-there", new_string="x")),
            turn("失败，我换个方式。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("改文件")

        tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
        self.assertIn("找不到 old_string", tool_msgs[0]["content"])

    def test_edit_file_rejects_ambiguous_match(self):
        (self.root / "b.txt").write_text("dup\ndup\n")
        llm = ScriptedLLM(
            turn("", tc("edit_file", path="b.txt", old_string="dup", new_string="x")),
            turn("好的。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("改")
        tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
        self.assertIn("不唯一", tool_msgs[0]["content"])
        self.assertEqual((self.root / "b.txt").read_text(), "dup\ndup\n")

    def test_read_missing_file_is_graceful(self):
        llm = ScriptedLLM(
            turn("", tc("read_file", path="nope.py")),
            turn("文件不存在。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("读文件")
        tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
        self.assertIn("不存在", tool_msgs[0]["content"])

    def test_list_dir_and_glob_and_grep(self):
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "one.py").write_text("def alpha():\n    pass\n")
        (self.root / "pkg" / "two.py").write_text("def beta():\n    pass\n")
        (self.root / "readme.md").write_text("# doc\n")

        llm = ScriptedLLM(
            turn("", tc("glob", pattern="**/*.py")),
            turn("", tc("grep", pattern=r"def \w+")),
            turn("", tc("list_dir", path="pkg")),
            turn("看完了。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("看看项目")

        outputs = [m["content"] for m in agent.messages if m["role"] == "tool"]
        self.assertIn("pkg/one.py", outputs[0])
        self.assertIn("def alpha", outputs[1])
        self.assertIn("one.py", outputs[2])


# --------------------------------------------------------------------- 沙箱


class TestSandbox(AgentTestCase):
    def test_write_outside_workspace_blocked(self):
        llm = ScriptedLLM(
            turn("", tc("write_file", path="../../evil.txt", content="bad")),
            turn("被挡住了。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("越界写文件")
        tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
        self.assertIn("越界", tool_msgs[0]["content"])
        self.assertFalse((self.root.parent.parent / "evil.txt").exists())

    def test_absolute_path_outside_blocked(self):
        llm = ScriptedLLM(
            turn("", tc("read_file", path="/etc/hosts")),
            turn("不行。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("读系统文件")
        tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
        self.assertIn("越界", tool_msgs[0]["content"])

    def test_forbidden_command_blocked(self):
        llm = ScriptedLLM(
            turn("", tc("run_bash", command="rm -rf /")),
            turn("被拒了。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("删库")
        tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
        self.assertIn("安全策略拒绝", tool_msgs[0]["content"])


# --------------------------------------------------------------------- 审批


class TestApproval(AgentTestCase):
    def test_denied_write_does_not_touch_disk(self):
        llm = ScriptedLLM(
            turn("", tc("write_file", path="x.txt", content="nope")),
            turn("用户拒绝了，我不写了。"),
        )
        agent, ui = self.make_agent(llm, approve=False)
        agent.run("写个文件")
        self.assertFalse((self.root / "x.txt").exists())
        self.assertIn("write_file", ui.denied)
        tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
        self.assertIn("用户拒绝", tool_msgs[0]["content"])

    def test_read_does_not_require_approval(self):
        (self.root / "a.txt").write_text("data\n")
        llm = ScriptedLLM(turn("", tc("read_file", path="a.txt")), turn("读到了。"))
        agent, ui = self.make_agent(llm, approve=False)
        agent.run("读文件")
        self.assertEqual(ui.denied, [])


# --------------------------------------------------------------------- 容错


class TestRobustness(AgentTestCase):
    def test_unknown_tool_reports_options(self):
        llm = ScriptedLLM(
            turn("", ToolCall(id="c1", name="teleport", arguments="{}")),
            turn("工具不存在。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("用不存在的工具")
        tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
        self.assertIn("不存在名为", tool_msgs[0]["content"])
        self.assertIn("read_file", tool_msgs[0]["content"])

    def test_bad_json_arguments_recovered(self):
        llm = ScriptedLLM(
            turn("", ToolCall(id="c1", name="read_file", arguments="{not json")),
            turn("参数错了。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("乱传参数")
        tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
        self.assertIn("JSON", tool_msgs[0]["content"])

    def test_wrong_argument_name_is_graceful(self):
        llm = ScriptedLLM(
            turn("", tc("read_file", file="a.txt")),
            turn("参数名错了。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("传错参数名")
        tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
        self.assertIn("参数不匹配", tool_msgs[0]["content"])

    def test_tool_exception_does_not_crash_loop(self):
        llm = ScriptedLLM(
            turn("", tc("read_file", path=".")),  # 目录
            turn("那是目录。"),
        )
        agent, _ = self.make_agent(llm)
        answer = agent.run("读目录")
        self.assertEqual(answer, "那是目录。")

    def test_max_iterations_respected(self):
        self.cfg.max_iterations = 3
        # 参数必须逐步不同：一样的话会被打转检测先拦下，就测不到步数上限了
        llm = ScriptedLLM(*[turn("", tc("list_dir", path=f"dir{i}")) for i in range(10)])
        agent, _ = self.make_agent(llm)
        answer = agent.run("无限循环")
        self.assertIn("上限", answer)
        self.assertEqual(len(llm.received), 3)


# --------------------------------------------------------------------- shell


class TestLoopDetection(AgentTestCase):
    """原地打转的检测。

    用户要的是「别因为步数到了就停」，但**真的卡住了还是得停**——
    再给 200 步也只会重复同样的无效动作。所以防的是打转，不是干活多。
    """

    def test_identical_calls_are_detected(self):
        llm = ScriptedLLM(*[turn("", tc("list_dir", path="same")) for _ in range(9)])
        agent, _ = self.make_agent(llm)
        answer = agent.run("原地转圈")
        self.assertIn("原地打转", answer)
        self.assertLess(len(llm.received), 9, "不该把剧本跑完")

    def test_varied_calls_are_not_flagged(self):
        """每步内容不同就是正常干活，跑多少步都不该被拦。"""
        llm = ScriptedLLM(
            *[turn("", tc("list_dir", path=f"dir{i}")) for i in range(8)],
            turn("做完了。"),
        )
        agent, _ = self.make_agent(llm)
        answer = agent.run("看很多目录")
        self.assertEqual(answer, "做完了。")

    def test_interleaved_calls_are_not_a_loop(self):
        """中间插了别的动作说明它在推进——只有**连续**相同才算卡住。"""
        pattern = [
            turn("", tc("list_dir", path="a")),
            turn("", tc("list_dir", path="b")),
        ] * 4
        llm = ScriptedLLM(*pattern, turn("做完了。"))
        agent, _ = self.make_agent(llm)
        self.assertEqual(agent.run("来回看两个目录"), "做完了。")

    def test_threshold_is_configurable(self):
        self.cfg.loop_repeat_limit = 0  # 关掉检测
        llm = ScriptedLLM(*[turn("", tc("list_dir", path="same")) for _ in range(6)],
                          turn("做完了。"))
        agent, _ = self.make_agent(llm)
        self.assertEqual(agent.run("关掉检测"), "做完了。")

    def test_higher_threshold_allows_more_repeats(self):
        self.cfg.loop_repeat_limit = 5
        llm = ScriptedLLM(*[turn("", tc("list_dir", path="same")) for _ in range(4)],
                          turn("做完了。"))
        agent, _ = self.make_agent(llm)
        self.assertEqual(agent.run("允许重复几次"), "做完了。")

    def test_reports_which_tool_looped(self):
        llm = ScriptedLLM(*[turn("", tc("run_bash", command="ls")) for _ in range(5)])
        agent, _ = self.make_agent(llm)
        answer = agent.run("重复跑命令")
        self.assertIn("run_bash", answer, "要说清楚是哪个工具在打转")

    def test_default_iteration_limit_is_generous(self):
        """上限是防跑飞的兜底，不该是「任务能做多大」的边界。"""
        from va.config import Config

        self.assertGreaterEqual(Config.load(self.root).max_iterations, 100)


class TestNoStepNoise(AgentTestCase):
    """不再把轮次当进度展示。

    一个正常任务跑几十轮是常态，标出轮次只会让人以为「怎么又要一步」。
    """

    def test_terminal_ui_does_not_print_step_numbers(self):
        import io
        import contextlib
        from va.ui import UI

        ui = UI(self.workspace)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            ui.assistant_header(1)
            ui.assistant_header(7)
        output = buffer.getvalue()
        self.assertNotIn("第 1 步", output)
        self.assertNotIn("第 7 步", output)
        self.assertNotIn("步", output)

    def test_web_ui_does_not_emit_step_events(self):
        from va.web.bridge import ApprovalGate, EventBus, WebUI

        bus = EventBus()
        ui = WebUI(self.workspace, bus, ApprovalGate(timeout=5))
        ui.assistant_header(3)
        self.assertEqual(
            [e for e in bus.since(0) if e["type"] == "step"],
            [],
            "不该再发步号事件",
        )


class TestShell(AgentTestCase):
    def test_run_bash_captures_output_and_exit_code(self):
        llm = ScriptedLLM(
            turn("", tc("run_bash", command="echo hello-agent")),
            turn("命令跑通了。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("跑个命令")
        out = [m["content"] for m in agent.messages if m["role"] == "tool"][0]
        self.assertIn("hello-agent", out)
        self.assertIn("[exit code: 0]", out)

    def test_run_bash_reports_failure(self):
        llm = ScriptedLLM(
            turn("", tc("run_bash", command="exit 3")),
            turn("失败了。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("失败命令")
        out = [m["content"] for m in agent.messages if m["role"] == "tool"][0]
        self.assertIn("[exit code: 3]", out)

    def test_run_bash_cwd_confined(self):
        llm = ScriptedLLM(
            turn("", tc("run_bash", command="pwd", cwd="/tmp")),
            turn("不行。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("换目录执行")
        out = [m["content"] for m in agent.messages if m["role"] == "tool"][0]
        self.assertIn("越界", out)


# --------------------------------------------------------------------- 上下文


class TestCompaction(AgentTestCase):
    def test_compaction_preserves_system_and_recent(self):
        self.cfg.context_budget = 100  # 极低，强制触发
        filler = "x" * 400
        llm = ScriptedLLM(
            *[turn("", tc("run_bash", command=f"echo {filler} {i}")) for i in range(12)],
            turn("完成。"),
        )
        agent, _ = self.make_agent(llm)
        answer = agent.run("产生大量输出")

        self.assertEqual(answer, "完成。")
        self.assertGreater(agent.compactions, 0)
        self.assertGreater(llm.summaries, 0, "压缩时应真的调用模型生成摘要")
        # 系统提示词必须原样保留
        self.assertEqual(agent.messages[0]["role"], "system")
        self.assertIn(str(self.root), agent.messages[0]["content"])
        # 第二段必须是 user 开头的摘要，且不能出现孤儿 tool 消息
        self.assertEqual(agent.messages[1]["role"], "user")
        self.assertIn("压缩摘要", agent.messages[1]["content"])

    def test_no_orphan_tool_messages_after_compaction(self):
        self.cfg.context_budget = 100
        filler = "y" * 400
        llm = ScriptedLLM(
            *[turn("", tc("run_bash", command=f"echo {filler} {i}")) for i in range(14)],
            turn("done"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("刷屏")
        self.assertGreater(agent.compactions, 0)

        seen_call_ids: set[str] = set()
        for message in agent.messages:
            if message["role"] == "assistant" and message.get("tool_calls"):
                seen_call_ids |= {c["id"] for c in message["tool_calls"]}
            elif message["role"] == "tool":
                self.assertIn(
                    message["tool_call_id"],
                    seen_call_ids,
                    "出现了没有对应 assistant 调用的 tool 消息",
                )


# --------------------------------------------------------------------- git


class TestGitTools(AgentTestCase):
    def _git_env(self) -> dict:
        import os

        return {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }

    def _init_repo(self, *, initial_commit: bool = True) -> None:
        env = self._git_env()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, capture_output=True, env=env)
        if initial_commit:
            (self.root / ".gitkeep").write_text("")
            subprocess.run(["git", "add", "-A"], cwd=self.root, check=True, capture_output=True, env=env)
            subprocess.run(
                ["git", "commit", "-q", "-m", "init"], cwd=self.root, check=True, capture_output=True, env=env
            )

    def _current_branch(self) -> str:
        return subprocess.run(
            ["git", "symbolic-ref", "--short", "-q", "HEAD"],
            cwd=self.root,
            capture_output=True,
            text=True,
            env=self._git_env(),
        ).stdout.strip()

    def test_git_status_and_commit(self):
        self._init_repo()
        (self.root / "f.txt").write_text("v1\n")
        llm = ScriptedLLM(
            turn("", tc("git_status")),
            turn("", tc("git_commit", message="add f.txt")),
            turn("提交完成。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("提交文件")

        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=self.root, capture_output=True, text=True
        ).stdout
        self.assertIn("add f.txt", log)

    def test_git_status_reports_branch_name(self):
        self._init_repo()
        llm = ScriptedLLM(turn("", tc("git_status")), turn("看完了。"))
        agent, _ = self.make_agent(llm)
        agent.run("查状态")
        out = [m["content"] for m in agent.messages if m["role"] == "tool"][0]
        self.assertIn("分支：", out)
        self.assertNotIn("分支：HEAD", out)

    def test_git_status_on_empty_repo_is_readable(self):
        self._init_repo(initial_commit=False)
        llm = ScriptedLLM(turn("", tc("git_status")), turn("还没提交过。"))
        agent, _ = self.make_agent(llm)
        agent.run("查状态")
        out = [m["content"] for m in agent.messages if m["role"] == "tool"][0]
        # 未提交的新仓库也应给出真实分支名，而不是 "HEAD"
        self.assertIn("分支：main", out)
        self.assertNotIn("分支：HEAD", out)

    def test_git_create_branch(self):
        self._init_repo()
        llm = ScriptedLLM(
            turn("", tc("git_create_branch", name="feat/demo")),
            turn("分支建好了。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("建分支")

        self.assertEqual(self._current_branch(), "feat/demo")

    def test_git_create_branch_switches_if_exists(self):
        self._init_repo()
        subprocess.run(["git", "branch", "existing"], cwd=self.root, check=True, env=self._git_env())
        llm = ScriptedLLM(
            turn("", tc("git_create_branch", name="existing")),
            turn("切过去了。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("切分支")
        out = [m["content"] for m in agent.messages if m["role"] == "tool"][0]
        self.assertIn("已存在", out)
        self.assertEqual(self._current_branch(), "existing")

    def test_git_push_without_remote_is_graceful(self):
        self._init_repo()
        llm = ScriptedLLM(
            turn("", tc("git_push")),
            turn("没有远端。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("推送")
        out = [m["content"] for m in agent.messages if m["role"] == "tool"][0]
        self.assertIn("远端", out)

    def test_git_tool_outside_repo_is_graceful(self):
        llm = ScriptedLLM(
            turn("", tc("git_status")),
            turn("不是仓库。"),
        )
        agent, _ = self.make_agent(llm)
        agent.run("查状态")
        out = [m["content"] for m in agent.messages if m["role"] == "tool"][0]
        self.assertIn("不是一个 git 仓库", out)

    # ------------------------------------------- porcelain 解析回归测试
    # 背景：真实使用中 git_status 曾把 ' M'（未暂存）误判成 'M '（已暂存），
    # 并且文件名被吃掉首字符（__pycache__ -> _pycache__）。
    # 根因是 _git() 对 stdout 做了 strip()，破坏了 porcelain 行首的语义空格。

    def test_porcelain_leading_space_preserved(self):
        self._init_repo()
        (self.root / ".gitkeep").write_text("modified")
        raw = GitStatus(self.workspace)._git("status", "--porcelain=v1", "--untracked-files=all")
        self.assertTrue(
            raw.startswith(" M "),
            f"porcelain 行首状态位被破坏，实际为 {raw[:20]!r}",
        )

    def test_unstaged_change_not_misreported_as_staged(self):
        self._init_repo()
        (self.root / ".gitkeep").write_text("modified")
        (self.root / "__private.py").write_text("x")  # 未跟踪

        llm = ScriptedLLM(turn("", tc("git_status")), turn("看完了。"))
        agent, _ = self.make_agent(llm)
        agent.run("查状态")
        out = [m["content"] for m in agent.messages if m["role"] == "tool"][0]

        self.assertIn("未暂存", out)
        self.assertNotIn("已暂存", out, "未暂存的修改被误判成了已暂存")
        # 文件名不能被吃掉首字符
        self.assertIn(".gitkeep", out)
        self.assertIn("__private.py", out)

    def test_staged_change_reported_as_staged(self):
        self._init_repo()
        (self.root / ".gitkeep").write_text("modified")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True, env=self._git_env())

        llm = ScriptedLLM(turn("", tc("git_status")), turn("看完了。"))
        agent, _ = self.make_agent(llm)
        agent.run("查状态")
        out = [m["content"] for m in agent.messages if m["role"] == "tool"][0]

        self.assertIn("已暂存", out)
        self.assertNotIn("未暂存", out)
        self.assertIn(".gitkeep", out)

    def test_untracked_file_reported_in_full(self):
        self._init_repo()
        (self.root / "new_file.py").write_text("x")
        llm = ScriptedLLM(turn("", tc("git_status")), turn("看完了。"))
        agent, _ = self.make_agent(llm)
        agent.run("查状态")
        out = [m["content"] for m in agent.messages if m["role"] == "tool"][0]
        self.assertIn("未跟踪", out)
        self.assertIn("new_file.py", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
