"""浏览器操作原语（生产工作流·阶段 C）。

`render_page` 只能渲染截图。要「操作功能测试」就得能点、能输入、能断言。

**为什么是一个工具收一串动作**：一次动作一个工具调用 = 一次模型往返。
十步的登录流程就是十次往返。所以这里收动作数组，一次跑完。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402

try:
    import playwright.sync_api  # noqa: F401

    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


PAGE = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<label for="status">状态</label>
<select id="status" onchange="render()">
  <option value="">全部</option><option value="done">已完成</option>
</select>
<label for="kw">关键词</label><input id="kw" placeholder="搜索">
<button onclick="render()">查询</button>
<ul id="list"></ul>
<p id="empty" style="display:none">暂无数据</p>
<script>
const all = [
  {st:'done',name:'A'},{st:'done',name:'B'},{st:'todo',name:'C'},
];
function render(){
  const st=document.getElementById('status').value;
  const kw=document.getElementById('kw').value;
  const rows=all.filter(o=>(!st||o.st===st)&&(!kw||o.name.includes(kw)));
  document.getElementById('list').innerHTML =
    rows.map(o=>`<li data-testid="row">${o.name}</li>`).join('');
  document.getElementById('empty').style.display = rows.length?'none':'block';
}
render();
</script></body></html>
"""


@unittest.skipUnless(HAS_PLAYWRIGHT, "需要 playwright（.venv 里有）")
class BrowserTestCase(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        from va.loop import Agent
        from va.ui import UI

        self.page = self.root / "index.html"
        self.page.write_text(PAGE, encoding="utf-8")
        self.url = f"file://{self.page}"
        self.agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)
        self.tool = self.agent.registry.get("browser_run")
        self.addCleanup(self.agent.processes.stop_all)

    def run_actions(self, *actions, **kwargs) -> str:
        """跑一串动作。**失败会抛错**（报告在错误信息里），这里统一转成文本。"""
        from va.tools.base import ToolError

        try:
            return self.tool.run(url=self.url, actions=list(actions), **kwargs)
        except ToolError as exc:
            return str(exc)

    def run_expecting_error(self, *actions, **kwargs) -> str:
        from va.tools.base import ToolError

        with self.assertRaises(ToolError) as ctx:
            self.tool.run(url=self.url, actions=list(actions), **kwargs)
        return str(ctx.exception)


class TestHappyPath(BrowserTestCase):
    def test_a_whole_flow_in_one_call(self):
        """一次调用跑完整条流程——这是这个工具的设计目的。"""
        out = self.run_actions(
            {"do": "assert", "by": "testid", "value": "row", "count": 3},
            {"do": "select", "by": "label", "value": "状态", "option": "done"},
            {"do": "assert", "by": "testid", "value": "row", "count": 2},
            {"do": "type", "by": "placeholder", "value": "搜索", "text": "A"},
            {"do": "click", "by": "role", "value": "button", "name": "查询"},
            {"do": "assert", "by": "testid", "value": "row", "count": 1},
        )
        self.assertIn("个动作通过", out)
        self.assertNotIn("✗", out, "整条流程都该通过")

    def test_reports_every_step(self):
        out = self.run_actions(
            {"do": "assert", "by": "testid", "value": "row", "count": 3}
        )
        self.assertIn("1. goto", out, "url 参数该自动变成第一个 goto")
        self.assertIn("assert", out)
        self.assertIn("数量 = 3", out)

    def test_snapshot_produces_a_file(self):
        out = self.run_actions({"do": "snapshot", "label": "初始"})
        self.assertIn("截图", out)
        from va.paths import screenshots_dir

        self.assertTrue(list(screenshots_dir().glob("e2e-*.png")))


class TestLocatorStrategies(BrowserTestCase):
    def assert_all_passed(self, out: str) -> None:
        # url 参数会自动补一个 goto，所以别断言具体的动作数
        self.assertIn("个动作通过", out)
        self.assertNotIn("✗", out)

    def test_role_with_name(self):
        self.assert_all_passed(self.run_actions(
            {"do": "click", "by": "role", "value": "button", "name": "查询"}
        ))

    def test_text(self):
        self.assert_all_passed(
            self.run_actions({"do": "click", "by": "text", "value": "查询"})
        )

    def test_label(self):
        self.assert_all_passed(self.run_actions(
            {"do": "select", "by": "label", "value": "状态", "option": "done"}
        ))

    def test_placeholder(self):
        self.assert_all_passed(self.run_actions(
            {"do": "type", "by": "placeholder", "value": "搜索", "text": "x"}
        ))

    def test_css(self):
        self.assert_all_passed(
            self.run_actions({"do": "click", "by": "css", "value": "#list"})
        )

    def test_unknown_by_says_what_is_allowed(self):
        out = self.run_actions({"do": "click", "by": "nonsense", "value": "x"})
        self.assertIn("可用", out)


class TestFailureDiagnostics(BrowserTestCase):
    def test_a_failed_action_raises(self):
        """**失败就是失败**——不能「成功返回一段写着失败的报告」，
        那会让模型以为通过了，后面的门禁也没法机器判定。"""
        self.run_expecting_error({"do": "click", "by": "text", "value": "没有这个按钮"})

    def test_zero_matches_points_at_the_locator(self):
        """**0 个（定位错了）和 8 个（定位太宽）是完全不同的问题。**
        原始报错里这个信息淹没在一大段里，所以要单独拎出来。"""
        out = self.run_expecting_error({"do": "click", "by": "text", "value": "没有这个按钮"})
        self.assertIn("匹配到 0 个元素", out)

    def test_too_many_matches_points_at_the_locator(self):
        out = self.run_expecting_error({"do": "assert", "by": "testid", "value": "row", "count": 99})
        self.assertIn("期望 99 个元素，实际 3 个", out)

    def test_assert_text_mismatch_shows_both(self):
        out = self.run_expecting_error(
            {"do": "assert", "by": "css", "value": "#list", "text": "不存在的内容"}
        )
        self.assertIn("期望文本包含", out)
        self.assertIn("实际是", out)

    def test_non_locator_actions_do_not_report_a_match_count(self):
        """**踩过的坑**：snapshot 失败时报「匹配到 12 个元素」——
        它压根不需要定位，这个数字只会把人引向错误方向。"""
        # 用 goto 一个不存在的地址——它是非定位动作，且一定会失败
        from va.tools.base import ToolError

        with self.assertRaises(ToolError) as ctx:
            self.tool.run(
                actions=[{"do": "goto", "url": "file:///nope/nope.html", "timeout": 3000}]
            )
        self.assertNotIn("匹配到", str(ctx.exception))

    def test_console_errors_are_surfaced(self):
        bad = self.root / "bad.html"
        bad.write_text(
            "<html><body><script>console.error('接口 500');</script></body></html>",
            encoding="utf-8",
        )
        out = self.tool.run(url=f"file://{bad}", actions=[{"do": "wait_idle"}])
        self.assertIn("接口 500", out)


class TestFlowControl(BrowserTestCase):
    def test_stops_at_the_first_failure_by_default(self):
        out = self.run_expecting_error(
            {"do": "click", "by": "text", "value": "不存在"},
            {"do": "click", "by": "text", "value": "查询"},
        )
        self.assertIn("失败后停止", out)
        self.assertNotIn("已点击", out, "停止后不该继续跑后面的动作")

    def test_continue_mode_reports_all_problems(self):
        """一次看清所有问题，而不是修一个跑一次。"""
        out = self.run_expecting_error(
            {"do": "click", "by": "text", "value": "不存在A"},
            {"do": "click", "by": "text", "value": "不存在B"},
            stop_on_error=False,
        )
        self.assertIn("不存在A", out)
        self.assertIn("不存在B", out)


class TestGuards(BrowserTestCase):
    def test_empty_actions(self):
        from va.tools.base import ToolError

        with self.assertRaises(ToolError):
            self.tool.run(url=self.url, actions=[])

    def test_too_many_actions(self):
        from va.tools.base import ToolError

        with self.assertRaises(ToolError) as ctx:
            self.tool.run(url=self.url, actions=[{"do": "wait_idle"}] * 100)
        self.assertIn("太多", str(ctx.exception))

    def test_unknown_action(self):
        from va.tools.base import ToolError

        with self.assertRaises(ToolError) as ctx:
            self.tool.run(url=self.url, actions=[{"do": "fly"}])
        self.assertIn("不认识的动作", str(ctx.exception))

    def test_assert_needs_something_to_check(self):
        from va.tools.base import ToolError

        with self.assertRaises(ToolError) as ctx:
            self.tool.run(
                url=self.url,
                actions=[{"do": "assert", "by": "testid", "value": "row"}],
            )
        self.assertIn("至少要给", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
