"""长期记忆的测试。

记忆和别的功能不一样的地方：它**每一轮都会进提示词**。所以写错了
不是「这条记录没用」，而是「以后每一次对话都被污染」。测试要盯住这点。

两级作用域是设计核心：
    全局   所有项目都读        换项目还成立吗？成立 → 全局
    项目级 只有这个项目读      不成立 → 项目级
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase, ScriptedLLM, turn  # noqa: E402
from va import memory  # noqa: E402
from va.paths import data_root, global_memory_dir, project_memory_dir  # noqa: E402
from va.prompts import build_system_prompt  # noqa: E402
from va.tools import build_registry  # noqa: E402
from va.tools.base import ToolError  # noqa: E402
from va.workspace import Workspace  # noqa: E402


class TestLayout(AgentTestCase):
    """先按项目分，项目下再分会话和偏好。"""

    def test_global_memory_is_outside_projects(self):
        self.assertEqual(global_memory_dir(), data_root() / "memory")

    def test_project_memory_lives_under_the_project(self):
        projects = self.root / "alpha"
        projects.mkdir()
        target = project_memory_dir(projects)
        self.assertIn("projects", str(target))
        self.assertEqual(target.name, "memory")
        self.assertLess(
            str(target).index("projects"), str(target).index("memory"),
            "要先按项目分，再分 memory/sessions",
        )

    def test_two_projects_do_not_share_project_memory(self):
        a, b = self.root / "a", self.root / "b"
        a.mkdir()
        b.mkdir()
        self.assertNotEqual(project_memory_dir(a), project_memory_dir(b))

    def test_sessions_and_memory_sit_side_by_side(self):
        from va.paths import sessions_dir

        project = self.root / "same"
        project.mkdir()
        self.assertEqual(sessions_dir(project).parent, project_memory_dir(project).parent)

    def test_same_name_different_path_projects_do_not_collide(self):
        """两个都叫 app 的项目在不同的目录下，数据不能混。"""
        one = self.root / "one" / "app"
        two = self.root / "two" / "app"
        one.mkdir(parents=True)
        two.mkdir(parents=True)
        self.assertNotEqual(project_memory_dir(one), project_memory_dir(two))


class TestScopes(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.root / "proj-a"
        self.b = self.root / "proj-b"
        self.a.mkdir()
        self.b.mkdir()

    def test_global_is_read_by_every_project(self):
        memory.remember("风格", "提交信息用中文", scope="global")
        for project in (self.a, self.b):
            self.assertIn("提交信息用中文", memory.render_for_prompt(project))

    def test_project_memory_is_isolated(self):
        memory.remember("栈", "这个项目用 Django", scope="project", workspace=self.a)
        self.assertIn("Django", memory.render_for_prompt(self.a))
        self.assertNotIn("Django", memory.render_for_prompt(self.b), "不该串到别的项目")

    def test_render_marks_which_scope_each_entry_is(self):
        """出问题时用户要能看出这条是全局的还是本项目的。"""
        memory.remember("风格", "用中文", scope="global")
        memory.remember("栈", "用 Django", scope="project", workspace=self.a)
        text = memory.render_for_prompt(self.a)
        self.assertIn("全局", text)
        self.assertIn("本项目", text)

    def test_global_comes_before_project(self):
        """越具体的越靠后，模型更容易遵守。"""
        memory.remember("风格", "全局的那条", scope="global")
        memory.remember("栈", "项目的那条", scope="project", workspace=self.a)
        text = memory.render_for_prompt(self.a)
        self.assertLess(text.index("全局的那条"), text.index("项目的那条"))

    def test_project_scope_without_workspace_falls_back_to_global(self):
        """宁可能读到点东西，也别静默写到一个谁都不看的地方。"""
        self.assertEqual(memory.memory_dir("project"), memory.memory_dir("global"))

    def test_bad_scope_is_rejected(self):
        with self.assertRaises(ValueError):
            memory.remember("x", "y", scope="nonsense")

    def test_unknown_scope_reads_as_global(self):
        self.assertEqual(memory.memory_dir("乱写"), memory.memory_dir("global"))


class TestRemember(AgentTestCase):
    def test_writes_and_reads_back(self):
        path = memory.remember("偏好", "内容")
        self.assertTrue(path.is_file())
        self.assertEqual(memory.find("偏好").content.strip(), "内容")

    def test_append_accumulates(self):
        memory.remember("偏好", "第一条")
        memory.remember("偏好", "第二条", mode="append")
        self.assertIn("第一条", memory.find("偏好").content)
        self.assertIn("第二条", memory.find("偏好").content)

    def test_replace_overwrites(self):
        memory.remember("偏好", "旧的")
        memory.remember("偏好", "新的")
        self.assertNotIn("旧的", memory.find("偏好").content)

    def test_empty_content_is_rejected(self):
        with self.assertRaises(ValueError):
            memory.remember("偏好", "   ")

    def test_forget(self):
        memory.remember("偏好", "内容")
        self.assertTrue(memory.forget("偏好"))
        self.assertIsNone(memory.find("偏好"))
        self.assertFalse(memory.forget("偏好"), "删不存在的该返回 False")

    def test_chinese_names_are_kept(self):
        """记忆标题本来就该是人能看懂的话，不该被转成拼音或哈希。"""
        path = memory.remember("代码风格约定", "x")
        self.assertEqual(path.stem, "代码风格约定")

    def test_path_traversal_is_neutralised(self):
        """名字里塞路径分隔符不能写到目录外面去。"""
        path = memory.remember("../../evil", "x")
        self.assertEqual(path.parent, memory.memory_dir())
        self.assertNotIn("..", path.name)

    def test_weird_characters_are_cleaned(self):
        self.assertNotIn("/", memory.safe_name("a/b"))
        self.assertNotIn("\x00", memory.safe_name("a\x00b"))
        self.assertTrue(memory.safe_name("   "), "空名字也要兜住")


class TestPromptInjection(AgentTestCase):
    def test_memory_reaches_the_system_prompt(self):
        memory.remember("风格", "提交信息用中文祈使句", scope="global")
        ws = Workspace(self.root)
        text = build_system_prompt(ws, build_registry(ws).catalog())
        self.assertIn("长期记忆", text)
        self.assertIn("提交信息用中文祈使句", text)

    def test_no_memory_means_no_section(self):
        ws = Workspace(self.root)
        text = build_system_prompt(ws, build_registry(ws).catalog())
        self.assertNotIn("长期记忆", text, "没有记忆时不该留一个空标题")

    def test_project_memory_also_reaches_it(self):
        memory.remember("栈", "这个项目用 Django", scope="project", workspace=self.root)
        ws = Workspace(self.root)
        text = build_system_prompt(ws, build_registry(ws).catalog())
        self.assertIn("Django", text)

    def test_oversized_memory_is_capped_and_says_so(self):
        """记忆每轮都进提示词。涨到几万字时每轮都在烧钱。

        超了**不能静默丢**——那会让人以为记下来了。
        """
        for i in range(12):
            memory.remember(f"条目{i}", "x" * 3000)
        text = memory.render_for_prompt(self.root)
        self.assertLessEqual(len(text), memory.MAX_PROMPT_CHARS + 800)
        self.assertIn("没放进来", text, "截断了就要说，不能悄悄吞掉")
        self.assertIn("read_file", text, "要告诉模型怎么读全文")


class TestRememberTool(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tool = build_registry(Workspace(self.root), cfg=self.cfg).get("remember")

    def test_registered_and_needs_approval(self):
        self.assertIsNotNone(self.tool, "remember 没注册")
        self.assertTrue(
            self.tool.requires_approval,
            "记忆会影响以后每一次对话，必须让用户看见记了什么",
        )

    def test_writes_a_global_memory(self):
        result = self.tool.run(name="风格", content="用中文")
        self.assertIn("每个项目", result)
        self.assertIn("用中文", memory.find("风格").content)

    def test_writes_a_project_memory(self):
        result = self.tool.run(name="栈", content="用 Django", scope="project")
        self.assertIn("这个项目", result)
        self.assertIsNotNone(memory.find("栈", scope="project", workspace=self.root))

    def test_rejects_empty_name(self):
        with self.assertRaises(ToolError):
            self.tool.run(name="  ", content="x")

    def test_rejects_empty_content(self):
        with self.assertRaises(ToolError):
            self.tool.run(name="x", content="  ")

    def test_rejects_bad_scope(self):
        with self.assertRaises(ToolError) as ctx:
            self.tool.run(name="x", content="y", scope="乱写")
        self.assertIn("scope", str(ctx.exception))

    def test_refuses_to_store_secrets(self):
        """记忆会进每一轮提示词，还可能被同步到 git 远端。

        密钥写进去就不只是「记错了一件事」——它会跟着每一轮请求发出去。
        """
        for content in (
            "api_key=sk-abc123456789",
            "token=ghp_abcdefghijklmnop",
            "password=hunter2",
            "-----BEGIN RSA PRIVATE KEY-----",
        ):
            with self.assertRaises(ToolError, msg=f"漏了：{content}"):
                self.tool.run(name="临时", content=content)

    def test_normal_content_is_not_flagged(self):
        """别误伤——提到「密码」这个词本身不该被拦。"""
        self.tool.run(name="约定", content="不要把密码写进代码，用环境变量")

    def test_preview_shows_the_content_not_raw_json(self):
        text = self.tool.preview({"name": "风格", "content": "提交信息用中文"})
        self.assertIn("提交信息用中文", text)
        self.assertNotIn('"content"', text)

    def test_preview_shows_scope(self):
        self.assertIn("所有项目", self.tool.preview({"name": "a", "content": "b"}))
        self.assertIn(
            "这个项目",
            self.tool.preview({"name": "a", "content": "b", "scope": "project"}),
        )

    def test_affected_paths_is_empty(self):
        """记忆在 agent 自己的目录里，撤销栈管不到——不能假装能撤。"""
        self.assertEqual(self.tool.affected_paths({"name": "a", "content": "b"}), [])


class TestDataRepoLayout(AgentTestCase):
    """一个仓库放全部数据，但**先按项目分**。"""

    def setUp(self) -> None:
        super().setUp()
        import subprocess

        self.origin = self.root / "ai-memory.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        os.environ["VA_DATA_REMOTE"] = str(self.origin)
        self.addCleanup(os.environ.pop, "VA_DATA_REMOTE", None)

        self.a = self.root / "proj-alpha"
        self.b = self.root / "proj-beta"
        self.a.mkdir()
        self.b.mkdir()

    def test_repo_is_organised_project_first(self):
        import subprocess

        from va.paths import sessions_dir
        from va.session import SessionStore
        from va.session_sync import ensure_clone, sync

        url = str(self.origin)
        self.assertTrue(ensure_clone(url, kind="data").ok)

        memory.remember("风格", "全局的", scope="global")
        memory.remember("栈", "A 项目的", scope="project", workspace=self.a)
        for project in (self.a, self.b):
            store = SessionStore.create(project, "m")
            store.append_message({"role": "user", "content": "hi"})
            store.append_end("done", 1)

        self.assertTrue(sync(url, kind="data").ok, sync(url, kind="data").describe())

        # core.quotepath=false：不然中文文件名会被转义成 \351\243\216，
        # 断言里根本认不出来
        listing = subprocess.run(
            ["git", "-c", "core.quotepath=false", "ls-tree", "-r", "--name-only", "HEAD"],
            cwd=str(self.origin), capture_output=True, text=True,
        ).stdout
        # 会话必须在 projects/ 下面，而不是平铺在根上
        self.assertIn("projects/", listing)
        self.assertNotRegex(
            listing, r"(?m)^sessions/", "会话不该平铺在仓库根下"
        )
        # A 的偏好只落在 A 的目录里
        self.assertIn(memory.safe_name("栈"), listing)
        lines = [line for line in listing.splitlines() if "栈" in line]
        self.assertTrue(lines)
        self.assertTrue(all("proj-alpha" in line for line in lines), lines)

    def test_local_default_also_uses_projects_dir(self):
        os.environ.pop("VA_DATA_REMOTE", None)
        from va.paths import project_dir, sessions_dir

        self.assertEqual(sessions_dir(self.a).parent, project_dir(self.a))


class TestLegacySessionsAreNotLost(AgentTestCase):
    """老版本把会话放在 <home>/sessions/<slug>/。

    新布局是 <home>/projects/<slug>/sessions/。**有老数据就继续用老位置**——
    目录不整齐远不如把用户的记录弄丢严重。
    """

    def test_keeps_using_the_legacy_dir_when_it_has_records(self):
        from va.paths import agent_home, project_slug, sessions_dir

        legacy = agent_home() / "sessions" / project_slug(self.root)
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "20260101-000000-abcd.jsonl").write_text("{}\n", encoding="utf-8")

        self.assertEqual(sessions_dir(self.root), legacy)

    def test_moves_to_the_new_layout_when_there_is_nothing_to_lose(self):
        from va.paths import agent_home, project_slug, sessions_dir

        legacy = agent_home() / "sessions" / project_slug(self.root)
        legacy.mkdir(parents=True, exist_ok=True)  # 空目录，没记录

        target = sessions_dir(self.root)
        self.assertNotEqual(target, legacy)
        self.assertIn("projects", str(target))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestTestHarness(unittest.TestCase):
    """测试基础设施自身的守卫。

    这条是补出来的，起因很隐蔽：``tests/test_feature.py`` 里写了
    ``from tests.helpers import QuietUI``，而别处是 ``from helpers import ...``
    ——在 Python 眼里这是**两个模块**，于是 ``helpers.py`` 执行两遍。

    第二遍又建了一个临时 home 并覆盖 ``VA_HOME``，而 ``reset_agent_home()``
    清的是第一个。后果：**清理变成空操作**，测试之间互相污染，
    表现成「单独跑能过、全量跑就挂」——记忆就是这么串起来的。
    """

    def test_helpers_is_idempotent_across_module_names(self):
        import importlib.util
        import os as _os

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import helpers as first  # noqa: PLC0415

        spec = importlib.util.spec_from_file_location(
            "tests.helpers", Path(__file__).resolve().parent / "helpers.py"
        )
        second = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(second)

        self.assertEqual(
            first._TEST_HOME,
            second._TEST_HOME,
            "helpers 被以两个模块名导入时又建了一个临时 home——"
            "清理会清错目录，测试之间就开始互相污染",
        )
        self.assertEqual(_os.environ["VA_HOME"], str(first._TEST_HOME))

    def test_no_test_imports_helpers_under_a_package_name(self):
        """别处不许写 ``tests.helpers``——那正是双重导入的来源。"""
        import re as _re

        # 只认真实的 import 语句——注释、文档字符串里提到它不算
        pattern = _re.compile(r"^\s*(from|import)\s+tests\.helpers\b")
        offenders = []
        for path in (Path(__file__).resolve().parent).glob("*.py"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.match(line):
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual(offenders, [], f"这些地方用了 tests.helpers：{offenders}")

    def test_reset_actually_clears_the_home_in_use(self):
        """清理的目录必须就是 agent 实际在用的那个。"""
        from va.paths import agent_home

        import helpers as module  # noqa: PLC0415

        probe = agent_home() / "probe-dir"
        probe.mkdir(parents=True, exist_ok=True)
        (probe / "x.txt").write_text("x", encoding="utf-8")

        module.reset_agent_home()
        self.assertFalse(probe.exists(), "reset_agent_home 没清掉实际在用的 home")
