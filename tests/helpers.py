"""测试共享件：假 LLM、静音 UI、临时工作区基类。

放在这里是为了让各个测试文件不用各自抄一遍 —— 它们的行为必须完全一致，
否则「测试通过」这件事本身就不可信了。
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# --------------------------------------------------------------- 隔离真实目录
#
# agent 的产物（设计稿规格、截图、上传的图片）默认落在 ~/.vibe-agent/ 下。
# 测试绝不能往那儿写——那是开发者真实的数据，跑一次测试就给人家塞一堆文件。
# 这里统一切到临时目录，整个测试进程共用一份，退出时删掉。
#
# 注意必须在导入 va 之前设好：Workspace.__init__ 会读 VA_HOME。
# **必须幂等**：这个文件可能被以不同的模块名导入两次——
# `from helpers import X` 和 `from tests.helpers import X` 在 Python 眼里
# 是两个模块，于是整个文件会执行两遍。
#
# 曾经的后果很隐蔽：第二次执行又建了一个临时 home 并覆盖 VA_HOME，
# 而 reset_agent_home() 清的是**第一个**目录。于是清理变成了空操作，
# 测试之间互相污染（表现成「单独跑能过、全量跑就挂」）。
#
# 认已有的那个，不重复创建。
_TEST_HOME_MARKER = "va-test-home-"
_existing_home = (os.environ.get("VA_HOME") or "").strip()

if _existing_home and _TEST_HOME_MARKER in _existing_home:
    _TEST_HOME = Path(_existing_home)
else:
    _TEST_HOME = Path(tempfile.mkdtemp(prefix=_TEST_HOME_MARKER))
    os.environ["VA_HOME"] = str(_TEST_HOME)
    atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from va.config import Config  # noqa: E402
from va.loop import Agent  # noqa: E402
from va.messages import AssistantTurn, ToolCall  # noqa: E402
from va.tools import build_registry  # noqa: E402
from va.ui import UI  # noqa: E402
from va.workspace import Workspace  # noqa: E402

_counter = 0

SUMMARY_MARKER = "压缩成结构化摘要"


def tc(tool_name: str, **args) -> ToolCall:
    """构造一个工具调用。"""
    global _counter
    _counter += 1
    return ToolCall(
        id=f"call_{_counter}",
        name=tool_name,
        arguments=json.dumps(args, ensure_ascii=False),
    )


def turn(content: str = "", *calls: ToolCall) -> AssistantTurn:
    return AssistantTurn(content=content, tool_calls=list(calls), finish_reason="stop")


class ScriptedLLM:
    """按剧本返回预设轮次，并记录每次收到的消息。

    压缩上下文时主循环会发起一次「不带工具」的摘要请求；
    这里把它识别出来并返回固定摘要，不消耗剧本轮次。
    """

    def __init__(self, *turns: AssistantTurn) -> None:
        self.turns = list(turns)
        self.received: list[list[dict]] = []
        self.summaries = 0

    def complete(self, messages, tools=None, on_text=None, on_reasoning=None):
        self.received.append([dict(m) for m in messages])
        if tools is None and len(messages) == 1 and SUMMARY_MARKER in str(messages[0].get("content", "")):
            self.summaries += 1
            return turn(f"[压缩摘要 #{self.summaries}] 早前完成了若干步骤。")
        if not self.turns:
            return turn("(剧本已用尽)")
        nxt = self.turns.pop(0)
        if on_text and nxt.content:
            on_text(nxt.content)
        return nxt


class SilentUI(UI):
    """完全静音的 UI，只记录工具结果，用于集成测试。"""

    def __init__(self, workspace, approve: bool = True):
        super().__init__(workspace, auto_approve=approve, show_thinking=False)
        self.confirms: list[tuple[str, dict, str | None]] = []
        self.results: list[str] = []
        self.answers: list[str] = []
        # 提示类输出也记下来。不覆盖的话它们会直接打到 stdout——
        # 测试既断言不到，输出里还混进一堆本该被静音的行。
        self.messages: list[tuple[str, str]] = []

    def tool_result(self, text, ok):
        self.last_result = text
        self.last_ok = ok
        self.results.append(text)

    def tool_call(self, *a, **k):
        pass

    def assistant_header(self, *a, **k):
        pass

    def stream_text(self, *a, **k):
        pass

    def stream_thinking(self, *a, **k):
        pass

    def newline(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def success(self, *a, **k):
        pass

    def warn(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def show_plan(self, plan):
        pass

    def block(self, title, rows):
        pass

    def horizontal_rule(self, label=""):
        pass

    def answer(self, text):
        self.answers.append(text)

    def info(self, text):
        self.messages.append(("info", text))

    def warn(self, text):
        self.messages.append(("warn", text))

    def error(self, text):
        self.messages.append(("error", text))

    def success(self, text):
        self.messages.append(("success", text))

    def text(self, line=""):
        self.messages.append(("text", line))


class QuietUI(SilentUI):
    """静音 UI + 审批记录。approve=False 时拒绝所有需要审批的操作。"""

    def __init__(self, workspace, approve: bool = True):
        super().__init__(workspace, approve=approve)
        self.denied: list[str] = []
        self.preview: str | None = None

    def confirm(self, tool_name, args, detail, preview=None):
        self.preview = preview
        self.confirms.append((tool_name, args, preview))
        if self.auto_approve:
            return True
        self.denied.append(tool_name)
        return False


def reset_agent_home() -> None:
    """清空测试用的 agent home。

    它是进程级共享的临时目录。不清的话，上一个测试写下的
    ``designs/x.md`` 会让下一个测试的「这个文件不该存在」假失败。
    """
    if not _TEST_HOME.is_dir():
        return
    for child in _TEST_HOME.iterdir():
        if child.is_dir():
            # 不吞异常：清理失败会让下一个测试看到上一个的残留，
            # 表现成「单独跑能过、全量跑就挂」，极难查。
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)


class AgentTestCase(unittest.TestCase):
    """提供临时工作区 + 配置 + 装配好 Agent 的基类。"""

    def setUp(self) -> None:
        reset_agent_home()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = Config(
            workspace=self.root,
            model="fake",
            api_key="fake",
            base_url="http://localhost/v1",
        )
        self.workspace = Workspace(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_agent(self, llm, *, approve: bool = True, session=None, history=None, undo=None, plan=None):
        ui = QuietUI(self.workspace, approve=approve)
        agent = Agent(
            self.cfg,
            ui,
            workspace=self.workspace,
            registry=build_registry(self.workspace),
            llm=llm,
            session=session,
            history=history,
            undo=undo,
            plan=plan,
        )
        return agent, ui
