"""工作流定义（阶段 A：流程即数据）。

**为什么需要它**：分支命名、commit 格式、mock 约定、测试命令、提测分支——
这些是**团队约定**，不是模型该猜的东西。以前只能写在 AGENTS.md 里靠模型
自觉，改不了、也 review 不了。

定义成数据之后：机器能校验、你能看见来源、agent 能提议修改、进 git 可回滚。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402
from va import workflow  # noqa: E402
from va.workflow import ConfigError, Workflow, deep_merge, load, template  # noqa: E402


class WorkflowTestCase(AgentTestCase):
    @property
    def project_config(self) -> Path:
        return self.root / ".vibe" / "workflow.yaml"

    @property
    def user_config(self) -> Path:
        from va.paths import agent_home

        return agent_home() / "workflow.yaml"

    def write_project(self, text: str) -> None:
        self.project_config.parent.mkdir(parents=True, exist_ok=True)
        self.project_config.write_text(text, encoding="utf-8")

    def write_user(self, text: str) -> None:
        self.user_config.parent.mkdir(parents=True, exist_ok=True)
        self.user_config.write_text(text, encoding="utf-8")


class TestDefaults(WorkflowTestCase):
    def test_no_config_at_all_still_works(self):
        wf = load(self.root)
        self.assertTrue(wf.usable)
        self.assertEqual(wf.sources, [])
        self.assertEqual(wf.branch_pattern, "feature/{ticket}-{slug}")

    def test_everything_reports_as_default(self):
        wf = load(self.root)
        self.assertEqual(wf.origin_of("branch.base"), "默认值")
        self.assertFalse(wf.is_configured("branch.base"))

    def test_defaults_cover_every_schema_field(self):
        """SCHEMA 里定义的字段都要有默认值，否则模板和校验会不一致。"""
        data = workflow.defaults()
        for section, fields in workflow.SCHEMA.items():
            self.assertIn(section, data)
            for name in fields:
                self.assertIn(name, data[section], f"{section}.{name} 没有默认值")


class TestMerging(WorkflowTestCase):
    def test_project_overrides_user(self):
        self.write_user("branch:\n  base: user-base\n")
        self.write_project("branch:\n  base: project-base\n")
        self.assertEqual(load(self.root).branch_base, "project-base")

    def test_user_applies_when_project_is_silent(self):
        self.write_user("branch:\n  base: user-base\n")
        self.write_project("commit:\n  require_ticket: false\n")
        wf = load(self.root)
        self.assertEqual(wf.branch_base, "user-base")
        self.assertFalse(wf.get("commit.require_ticket"))

    def test_origins_say_where_each_value_came_from(self):
        """「这条是哪来的」——调试配置时最想知道的事。"""
        self.write_user("branch:\n  base: user-base\ncommit:\n  require_ticket: false\n")
        self.write_project("branch:\n  base: project-base\n")

        wf = load(self.root)
        self.assertEqual(wf.origin_of("branch.base"), "项目级")
        self.assertEqual(wf.origin_of("commit.require_ticket"), "用户级")
        self.assertEqual(wf.origin_of("test.gate"), "默认值")

    def test_dicts_merge_recursively(self):
        merged = deep_merge({"a": {"x": 1, "y": 2}}, {"a": {"y": 3}})
        self.assertEqual(merged, {"a": {"x": 1, "y": 3}})

    def test_lists_replace_whole(self):
        """列表整体替换。

        「我加了第三条测试」不该变成「原来的三条加上第三条」——
        这种意外在配置里特别难查。
        """
        merged = deep_merge({"a": {"xs": [1, 2, 3]}}, {"a": {"xs": [9]}})
        self.assertEqual(merged["a"]["xs"], [9])


class TestValidation(WorkflowTestCase):
    def errors(self, wf: Workflow) -> list[str]:
        return [p.path for p in wf.problems if p.level == "error"]

    def warns(self, wf: Workflow) -> list[str]:
        return [p.path for p in wf.problems if p.level == "warn"]

    def test_clean_config_has_no_problems(self):
        self.write_project(
            "branch:\n  base: develop\n"
            "test:\n  start: npm run dev\n  logic: npm test\n"
        )
        self.assertTrue(load(self.root).usable)

    def test_wrong_type_is_an_error(self):
        self.write_project("branch:\n  base: 123\n")
        wf = load(self.root)
        self.assertIn("branch.base", self.errors(wf))
        self.assertFalse(wf.usable)

    def test_typo_is_a_warning_not_an_error(self):
        """拼错了要提醒，但不该让整个配置不能用——
        未知键向前兼容比严格报错更有用。"""
        self.write_project("branch:\n  pattren: typo\n")
        wf = load(self.root)
        self.assertIn("branch.pattren", self.warns(wf))
        self.assertTrue(wf.usable, "拼错一个键不该让整个配置失效")

    def test_bad_regex_is_reported(self):
        self.write_project('commit:\n  pattern: "([unclosed"\n')
        self.assertIn("commit.pattern", self.errors(load(self.root)))

    def test_branch_pattern_must_have_a_ticket(self):
        """没有工单号的分支名在提测流程里没法追溯需求。"""
        self.write_project('branch:\n  pattern: "feature/{slug}"\n')
        wf = load(self.root)
        self.assertIn("branch.pattern", self.errors(wf))
        self.assertIn("ticket", wf.problems[0].message)

    def test_bad_choice_is_reported(self):
        self.write_project("test:\n  gate: nonsense\n")
        problem = next(p for p in load(self.root).problems if p.path == "test.gate")
        self.assertIn("只能是", problem.message)

    def test_gate_without_tests_is_a_warning(self):
        """门禁开着却没有测试可跑——永远不会有东西被拦，是个配置漏洞。"""
        self.write_project("test:\n  gate: strict\n")
        self.assertIn("test.gate", self.warns(load(self.root)))

    def test_ready_port_without_start_is_a_warning(self):
        self.write_project("test:\n  ready_port: 5173\n")
        self.assertIn("test.ready_port", self.warns(load(self.root)))

    def test_wait_for_ci_without_branch_is_an_error(self):
        self.write_project("submit:\n  wait_for_ci: true\n  target_branch: ''\n")
        self.assertIn("submit.target_branch", self.errors(load(self.root)))

    def test_require_ticket_needs_the_placeholder(self):
        self.write_project(
            'commit:\n  require_ticket: true\n  template: "{type}: {subject}"\n'
        )
        self.assertIn("commit.template", self.errors(load(self.root)))

    def test_unsupported_version(self):
        self.write_project("version: 99\n")
        self.assertIn("version", self.errors(load(self.root)))


class TestSyntaxErrors(WorkflowTestCase):
    def test_yaml_error_reports_a_line_number(self):
        """配置错时「第几行」能省很多时间。"""
        self.write_project('branch:\n  pattern: "未闭合\n  base: main\n')
        with self.assertRaises(ConfigError) as ctx:
            load(self.root)
        self.assertIn("YAML", str(ctx.exception))
        self.assertGreater(ctx.exception.line, 0, "没给出错在哪一行")

    def test_non_mapping_toplevel(self):
        self.write_project("- 这是个列表\n")
        with self.assertRaises(ConfigError):
            load(self.root)

    def test_json_is_supported_and_allows_comments(self):
        """不想装 pyyaml 的可以用 JSON——但配置文件里「为什么定这条」
        比规则本身还重要，所以自己剥一层注释。"""
        path = self.root / ".vibe" / "workflow.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "{\n"
            "  // 我们从 develop 切\n"
            '  "branch": {"base": "develop"}  /* 行内注释 */\n'
            "}\n",
            encoding="utf-8",
        )
        self.assertEqual(load(self.root).branch_base, "develop")

    def test_missing_yaml_support_says_what_to_do(self):
        """没装 pyyaml 时要告诉用户两条出路，而不是抛个 ImportError。"""
        import builtins

        real_import = builtins.__import__

        def fake(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("no yaml")
            return real_import(name, *args, **kwargs)

        self.write_project("branch:\n  base: x\n")
        builtins.__import__ = fake
        try:
            with self.assertRaises(ConfigError) as ctx:
                load(self.root)
        finally:
            builtins.__import__ = real_import
        message = str(ctx.exception)
        self.assertIn("pyyaml", message)
        self.assertIn("json", message, "要给出不用装依赖的那条路")


class TestTemplate(WorkflowTestCase):
    def test_template_is_all_commented_out(self):
        """**不能把默认值写进文件。**

        写进去的话：① 每个键的来源都会变成「项目级」，看不出自己改了什么
        ② 以后工具改了默认值，老项目的文件还压着旧值——过半年才发现。
        """
        self.write_project(template())
        wf = load(self.root)
        self.assertEqual(len(wf.origins), 0, "模板里的值生效了")
        self.assertEqual(wf.branch_base, workflow.defaults()["branch"]["base"])

    def test_template_parses(self):
        self.write_project(template())
        self.assertTrue(load(self.root).usable, "模板自己就过不了校验")

    def test_template_documents_every_field(self):
        text = template()
        for section, fields in workflow.SCHEMA.items():
            for name, spec in fields.items():
                self.assertIn(spec.doc, text, f"{section}.{name} 的说明不在模板里")

    def test_uncommenting_one_line_takes_effect(self):
        # 模板里字段是注释、段落是真的——取消一行注释就该生效
        text = template().replace("  # base: main", "  base: develop")
        self.write_project(text)
        wf = load(self.root)
        self.assertEqual(wf.branch_base, "develop")
        self.assertEqual(wf.origin_of("branch.base"), "项目级")


class TestBranchName(WorkflowTestCase):
    def test_renders_from_the_template(self):
        wf = load(self.root)
        name = wf.render_branch_name(ticket="ORD-312", title="Order status filter")
        self.assertEqual(name, "feature/ORD-312-order-status-filter")

    def test_slug_is_cleaned_and_truncated(self):
        self.write_project("branch:\n  slug_max_length: 12\n")
        wf = load(self.root)
        name = wf.render_branch_name(ticket="X-1", title="A very long title indeed")
        slug = name.split("-", 2)[-1]
        self.assertLessEqual(len(slug), 12)

    def test_chinese_is_kept_by_default(self):
        """中文分支名 git 支持。有些 CI 解析不了——那时用 slug_ascii 收紧。"""
        wf = load(self.root)
        self.assertIn("订单", wf.render_branch_name(ticket="X-1", title="订单列表"))

    def test_ascii_mode_strips_chinese(self):
        self.write_project("branch:\n  slug_ascii: true\n")
        name = load(self.root).render_branch_name(ticket="ORD-312", title="订单列表筛选")
        self.assertNotIn("订单", name)
        self.assertIn("ord-312", name, "标题被过滤空了要用需求号兜底")

    def test_missing_ticket_still_produces_a_valid_name(self):
        name = load(self.root).render_branch_name(ticket="", title="something")
        self.assertIn("no-ticket", name)


class TestDescribe(WorkflowTestCase):
    def test_describe_shows_values_and_origins(self):
        self.write_user("branch:\n  base: user-base\n")
        text = load(self.root).describe()
        self.assertIn("branch.base = user-base", text)
        self.assertIn("用户级", text)
        self.assertIn("默认值", text, "没配过的也要列出来")

    def test_long_lists_are_summarised(self):
        text = load(self.root).describe()
        self.assertIn("…", text, "长列表要省略，不然一行刷屏")


if __name__ == "__main__":
    unittest.main(verbosity=2)
