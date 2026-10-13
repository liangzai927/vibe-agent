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

    def test_web_without_a_key_prints_advice_and_exits(self):
        import io
        from contextlib import redirect_stderr

        from va.cli import _web

        captured = io.StringIO()
        with redirect_stderr(captured):
            code = _web(["-C", str(self.root), "--no-browser", "--port", "0"])
        self.assertEqual(code, 2, "没配 key 该优雅退出，不是崩")
        message = captured.getvalue()
        self.assertIn("界面上填", message)
        self.assertIn("setup", message)

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
