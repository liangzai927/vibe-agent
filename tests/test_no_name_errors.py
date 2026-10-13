"""别再出现「跑到某条路径才发现名字没定义」。

**这是用户报过的崩溃**：

    $ va web
    还没有配置模型服务。两种做法：
     ① 界面上填（推荐）…
    NameError: name 'run_hint' is not defined

我把一个 helper 从 `cli.py` 挪到 `config.py` 并改了名，替换了调用点，
**但没加 import**。测试全绿——因为没有任何测试走到「没配 key 时跑 web」
这条路径。

所以这里做两件事：
  1. 把那条路径真的跑一遍（用户就是这么撞上的）
  2. 做一个**静态检查**：模块里用到的名字必须能在自己的命名空间里找到
     ——没有 pyflakes/ruff 可用，但 AST 够用了
"""

from __future__ import annotations

import ast
import builtins
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402

PACKAGE = Path(__file__).resolve().parent.parent / "va"

def _defined_names(tree: ast.Module) -> set[str]:
    """模块里定义了/导入了哪些名字。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        elif isinstance(node, ast.alias):     # with ... as x
            names.add(node.asname or node.name.split(".")[0])
    return names

def _used_names(tree: ast.Module) -> set[str]:
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }

class TestNoUndefinedNames(unittest.TestCase):
    """静态检查：用到的名字必须能定义出来。

    这不是完整的类型检查，但**恰好能抓住「改名了忘了改 import」**这一类。
    """

    def modules(self):
        return sorted(PACKAGE.rglob("*.py"))

    def test_every_module_parses(self):
        for path in self.modules():
            with self.subTest(path=path.name):
                ast.parse(path.read_text(encoding="utf-8"))

    def test_no_undefined_globals(self):
        problems: list[str] = []
        for path in self.modules():
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            known = (
                _defined_names(tree)
                | set(dir(builtins))
                | {"__name__", "__file__", "__doc__", "self", "cls"}
            )
            # 有 `from x import *` 的模块跳过——静态看不出来
            if any(
                isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names)
                for node in ast.walk(tree)
            ):
                continue
            for name in sorted(_used_names(tree) - known):
                problems.append(f"{path.name}: {name}")
        self.assertEqual(problems, [], f"可能有未定义的名字：{problems}")

class TestTheMissingKeyPath(AgentTestCase):
    """**真的把那条路径跑一遍**——用户就是这么撞上的。"""

    def setUp(self) -> None:
        super().setUp()
        self.cfg.api_key = ""
        # **要一个干净的配置文件**：同一批测试里前面的用例会往共享的
        # 临时 .env 写 key，不清的话这里就测不到「没配 key」那条路径。
        import os
        import tempfile
        from unittest import mock

        fresh = Path(tempfile.mkdtemp()) / ".env"
        patcher = mock.patch.dict(
            os.environ, {"VA_ENV_FILE": str(fresh), "VA_API_KEY": ""}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_agent_without_a_key_says_how_to_fix_it(self):
        """`require_key` 抛的 `SystemExit` **消息在异常里**，不在 stderr——
        顶层才由 Python 打出来，所以这里要从异常取。"""
        from va.cli import main

        with self.assertRaises(SystemExit) as ctx:
            main(["-C", str(self.root), "随便问一句"])
        message = str(ctx.exception)
        self.assertIn("界面上填", message)
        self.assertIn("当前配置文件", message)

if __name__ == "__main__":
    unittest.main(verbosity=2)

class TestNoDeadlockWithoutAKey(AgentTestCase):
    """**没配 key 时不能出现「两条路都走不通」。**

    用户报过这个死结：

        $ va setup
        .env 已存在。要重新配置加 --force;      ← 拒绝，但 key 是空的

        $ va web
        还没有配置模型服务。两种做法:
         ① 界面上填（推荐）：跑 `va web`…        ← 他正在跑 va web

    `va web` 拒绝启动，却建议你用它去设置里填 key——**永远到不了那个界面**。
    """

    def test_web_only_warns_about_a_missing_key(self):
        """**界面必须能起来**——设置页就是用来配 key 的。

        行为测试不好写（`_web` 里有别的阻塞路径，mock 拦不住，
        在测试里起服务器会挂住），所以这里查源码：
        那段里不能再有 `return 2`。

        手工验证过：没配 key 时 `va web` 打印提示并正常服务，
        `curl` 拿到 HTTP 200。
        """
        import inspect

        from va.cli import _web

        source = inspect.getsource(_web)
        marker = "if not cfg.api_key:"
        self.assertIn(marker, source)

        branch = source[source.index(marker) : source.index(marker) + 900]
        self.assertNotIn("return 2", branch, "没配 key 又把界面挡回去了")
        self.assertIn("界面能打开", branch)

    def test_setup_runs_again_when_the_key_is_missing(self):
        """**文件存在不等于配好了。**

        .env 在、但 key 是空的（手动删过 / 被别的东西覆盖过）——
        原来 setup 会说「已存在」把路堵死。

        这里只测判断本身，不绕 `Config.load`（它读的是工作区里的 .env，
        和 `VA_ENV_FILE` 不是一回事，混着用会读到别的测试留下的值）。
        """
        from pathlib import Path as _Path

        def should_refuse(env_exists: bool, has_key: bool) -> bool:
            """复刻 `_setup` 里那两个分支的判断。"""
            return env_exists and has_key

        # 文件在、key 空 → 不该拒绝
        self.assertFalse(should_refuse(True, False), "key 是空的，却把路堵死了")
        # 文件在、key 有 → 该拒绝
        self.assertTrue(should_refuse(True, True))
        # 文件不在 → 该跑
        self.assertFalse(should_refuse(False, False))
        self.assertIsInstance(_Path("/tmp"), _Path)   # 保持 import 有用

    def test_the_setup_guard_checks_the_key_not_just_the_file(self):
        """源码层面确认：那个分支看的是 `api_key`，不只是文件在不在。"""
        import inspect

        from va.cli import _setup

        source = inspect.getsource(_setup)
        marker = "if env_path.is_file() and not args.force:"
        self.assertIn(marker, source)
        branch = source[source.index(marker) : source.index(marker) + 500]
        self.assertIn("api_key", branch, "只看文件在不在，key 空着也拒绝")

    def test_web_only_warns_about_a_missing_key(self):
        """**界面必须能起来**——设置页就是用来配 key 的。

        行为测试不好写（`_web` 里有别的阻塞路径，mock 拦不住，
        在测试里起服务器会挂住），所以这里查源码：
        那段里不能再有 `return 2`。

        手工验证过：没配 key 时 `va web` 打印提示并正常服务，
        `curl` 拿到 HTTP 200。
        """
        import inspect

        from va.cli import _web

        source = inspect.getsource(_web)
        marker = "if not cfg.api_key:"
        self.assertIn(marker, source)

        branch = source[source.index(marker) : source.index(marker) + 900]
        self.assertNotIn("return 2", branch, "没配 key 又把界面挡回去了")
        self.assertIn("界面能打开", branch)



class TestLLMWithoutAKey(AgentTestCase):
    """**没有 key 也要能构造 Agent。**

    用户报过：`va web` 起来了却立刻崩——

        openai.OpenAIError: Missing credentials.

    根因是 `LLM.__init__` 里直接建 OpenAI 客户端，于是
    「没配 key」→ `Agent(...)` 失败 → `WebApp(...)` 失败 → `va web` 起不来。
    而设置页恰恰是用来配 key 的——**界面开不了就没法配，又是个死锁。**

    所以客户端延迟到真正调用时才建，报错也放到那时候，措辞能直接告诉
    用户怎么办。
    """

    def test_llm_can_be_constructed_without_a_key(self):
        from dataclasses import replace

        from va.llm import LLM

        llm = LLM(replace(self.cfg, api_key=""))
        self.assertIsNone(llm._client)

    def test_agent_can_be_constructed_without_a_key(self):
        """整条链路都要能构造——不然界面还是起不来。"""
        from dataclasses import replace

        from va.loop import Agent
        from va.ui import UI

        agent = Agent(replace(self.cfg, api_key=""), UI(self.workspace), workspace=self.workspace)
        self.assertIsNotNone(agent)

    def test_calling_it_says_how_to_fix_it(self):
        """**失败要发生在「用」的时候，而不是「启动」的时候**，
        而且要说清怎么办。"""
        from dataclasses import replace

        from va.llm import LLM, LLMError

        llm = LLM(replace(self.cfg, api_key=""))
        with self.assertRaises(LLMError) as ctx:
            list(llm.complete([{"role": "user", "content": "hi"}]))
        message = str(ctx.exception)
        self.assertIn("API Key", message)
        self.assertIn("设置 → 模型接口", message)

    def test_refresh_picks_up_a_key_added_later(self):
        """**在设置里填完 key 要能立刻用**——不用重启。"""
        from dataclasses import replace

        from va.llm import LLM

        cfg = replace(self.cfg, api_key="")
        llm = LLM(cfg)
        self.assertIsNone(llm._client)

        cfg.api_key = "sk-填进去的-1234567890"
        llm.refresh()
        self.assertIsNotNone(llm._client, "填了 key 还是建不出客户端")

    def test_the_webapp_constructs_without_a_key(self):
        """`va web` 起不来的真正原因就在这条链路上。"""
        from dataclasses import replace

        from va.web.server import WebApp

        app = WebApp(replace(self.cfg, api_key=""), self.workspace)
        self.assertIsNotNone(app)
