"""读工作区外文件的测试。

用户的设计稿和需求文档经常不在项目里（下载目录、桌面），所以必须允许越界。
但沙箱是**刻意**做的安全属性，放开它得说清楚放开了什么、没放开什么：

    允许越界   read_design / inspect_image / read_document / pixel_diff 的基准图
               —— 用户明确指着一个文件说「读它」，而且每次都要点同意
    仍然禁止   所有写操作，以及 read_file / grep / glob 这类「自己找路」的读取
               —— 它们没有审批环节，放开等于让 prompt injection 直接读走
                  ~/.ssh/id_rsa

这个边界很容易在后续改动里被无意破坏，所以下面既有「能读」的测试，
也有「必须读不了」的测试。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402
from va.config import _split_paths  # noqa: E402
from va.tools.base import ToolError  # noqa: E402
from va.workspace import Workspace, WorkspaceError  # noqa: E402


class TestSplitPaths(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(_split_paths(None), [])
        self.assertEqual(_split_paths(""), [])
        self.assertEqual(_split_paths("  "), [])

    def test_pathsep(self):
        self.assertEqual(_split_paths("/a:/b"), ["/a", "/b"])

    def test_commas_also_work(self):
        """很多人顺手写逗号，不该因为分隔符不对就静默失效。"""
        self.assertEqual(_split_paths("/a,/b"), ["/a", "/b"])

    def test_mixed_separators(self):
        self.assertEqual(_split_paths("/a, /b:/c"), ["/a", "/b", "/c"])

    def test_expands_user(self):
        result = _split_paths("~/Desktop")
        self.assertFalse(result[0].startswith("~"), "~ 应该被展开")
        self.assertTrue(result[0].endswith("Desktop"))

    def test_deduplicates_keeping_order(self):
        self.assertEqual(_split_paths("/a:/a:/b"), ["/a", "/b"])

    def test_blank_entries_are_dropped(self):
        self.assertEqual(_split_paths("/a::/b:"), ["/a", "/b"])


class TestWorkspaceReadable(AgentTestCase):
    """边界判定。这一组是安全属性的守卫。"""

    def setUp(self) -> None:
        super().setUp()
        self.outside = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-outside"
        self.outside.mkdir(exist_ok=True)
        self.outside_file = self.outside / "secret.txt"
        self.outside_file.write_text("不该被读到", encoding="utf-8")

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.outside, ignore_errors=True)
        super().tearDown()

    def test_inside_is_readable(self):
        ws = Workspace(self.root)
        (self.root / "a.txt").write_text("x", encoding="utf-8")
        self.assertTrue(ws.readable("./a.txt"))
        self.assertTrue(ws.readable(self.root / "a.txt"))
        self.assertFalse(ws.needs_external_approval("./a.txt"))

    def test_outside_is_readable_but_needs_approval(self):
        ws = Workspace(self.root)
        self.assertFalse(ws.readable(self.outside_file))
        self.assertTrue(ws.needs_external_approval(self.outside_file))

    def test_relative_escape_needs_approval(self):
        """`../x` 也是相对路径，但它能走出去——不能因为「相对」就放行。"""
        ws = Workspace(self.root)
        self.assertTrue(ws.needs_external_approval("../whatever.txt"))
        self.assertTrue(ws.needs_external_approval(f"../{self.outside.name}/secret.txt"))

    def test_inner_dotdot_that_stays_inside_is_fine(self):
        ws = Workspace(self.root)
        (self.root / "sub").mkdir()
        (self.root / "a.txt").write_text("x", encoding="utf-8")
        self.assertFalse(ws.needs_external_approval("sub/../a.txt"))

    def test_extra_root_is_pre_approved(self):
        ws = Workspace(self.root, extra_read_roots=[self.outside])
        self.assertTrue(ws.readable(self.outside_file))
        self.assertFalse(ws.needs_external_approval(self.outside_file))

    def test_extra_root_covers_subdirectories(self):
        ws = Workspace(self.root, extra_read_roots=[self.outside])
        nested = self.outside / "deep" / "nested"
        nested.mkdir(parents=True, exist_ok=True)
        self.assertTrue(ws.readable(nested / "x.png"))

    def test_extra_root_does_not_leak_to_siblings(self):
        ws = Workspace(self.root, extra_read_roots=[self.outside])
        sibling = self.outside.parent / "somewhere-else"
        self.assertFalse(ws.readable(sibling / "x.png"))

    def test_nonexistent_extra_root_is_ignored(self):
        ws = Workspace(self.root, extra_read_roots=["/definitely/not/here"])
        self.assertEqual(ws.extra_read_roots, [])

    def test_file_as_extra_root_is_ignored(self):
        """写成一个文件而不是目录，不该被当成放行。"""
        (self.root / "notadir.txt").write_text("x", encoding="utf-8")
        ws = Workspace(self.root, extra_read_roots=[str(self.root / "notadir.txt")])
        self.assertEqual(ws.extra_read_roots, [])

    def test_resolve_is_still_strict(self):
        """核心安全属性：resolve() 不许越界。"""
        ws = Workspace(self.root)
        with self.assertRaises(WorkspaceError):
            ws.resolve("../escape.txt")

    def test_resolve_readable_allows_outside(self):
        ws = Workspace(self.root)
        resolved = ws.resolve_readable(self.outside_file, must_exist=True)
        self.assertEqual(resolved, self.outside_file.resolve())

    def test_resolve_readable_still_requires_existence(self):
        ws = Workspace(self.root)
        with self.assertRaises(WorkspaceError):
            ws.resolve_readable(self.outside / "nope.png", must_exist=True)


class TestToolApproval(AgentTestCase):
    """各工具的 needs_approval 判定。"""

    def setUp(self) -> None:
        super().setUp()
        from va.tools import build_registry

        self.registry = build_registry(self.workspace, cfg=self.cfg)
        self.outside = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-ext"
        self.outside.mkdir(exist_ok=True)
        self.ext_png = self.outside / "设计稿.png"
        # 造一个真 PNG，免得工具因为文件坏了报别的错
        self.ext_png.write_bytes(_tiny_png())
        (self.root / "inner.png").write_bytes(_tiny_png())

        # 这个文件必须**真的存在**。之前漏了它，于是 read_file 报的是
        # 「文件不存在」，assertRaises 照样通过——测试看着是绿的，
        # 实际根本没验证沙箱。安全测试最怕这种空转。
        self.secret = self.outside / "secret.txt"
        self.secret.write_text("SENSITIVE-DATA", encoding="utf-8")

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.outside, ignore_errors=True)
        super().tearDown()

    def approval(self, tool_name: str, **args) -> bool:
        return self.registry.get(tool_name).needs_approval(args)

    def test_inside_needs_no_approval(self):
        for name, key in (
            ("inspect_image", "path"),
            ("read_design", "image"),
            ("read_document", "path"),
        ):
            self.assertFalse(
                self.approval(name, **{key: "inner.png"}),
                f"{name} 读工作区内的文件不该问",
            )

    def test_outside_needs_approval(self):
        for name, key in (
            ("inspect_image", "path"),
            ("read_design", "image"),
            ("read_document", "path"),
        ):
            self.assertTrue(
                self.approval(name, **{key: str(self.ext_png)}),
                f"{name} 读工作区外的文件必须先问",
            )

    def test_writers_never_get_the_external_path(self):
        """写操作永远不能越界——这是整个沙箱的底线。"""
        writer = self.registry.get("write_file")
        with self.assertRaises((ToolError, WorkspaceError)):
            writer.run(path="../escape.txt", content="x")
        self.assertFalse((self.outside / "escape.txt").exists())

    def test_silent_text_reads_stay_sandboxed(self):
        """read_file / grep 没有审批环节，放开等于给 prompt injection 开后门。

        前置断言「文件确实存在」是刻意的：不存在的话，工具会因为
        「找不到」而报错，assertRaises 照样通过——那就测了个寂寞。
        """
        self.assertTrue(self.secret.is_file(), "目标文件必须真实存在，否则这个测试是空转")
        self.assertTrue(self.ext_png.is_file())

        for name, args in (
            ("read_file", {"path": str(self.secret)}),
            ("grep", {"pattern": "SENSITIVE", "path": str(self.outside)}),
            ("glob", {"pattern": "*.txt", "path": str(self.outside)}),
        ):
            tool = self.registry.get(name)
            self.assertFalse(tool.needs_approval(args), f"{name} 不该有审批环节")
            with self.assertRaises(
                (ToolError, WorkspaceError), msg=f"{name} 居然能读工作区外"
            ):
                tool.run(**args)

    def test_pre_approved_dir_skips_the_prompt(self):
        from va.tools import build_registry
        from va.tools.inspect import InspectImage

        workspace = Workspace(self.root, extra_read_roots=[self.outside])
        tool = InspectImage(workspace)
        self.assertFalse(tool.needs_approval({"path": str(self.ext_png)}))

    def test_external_preview_explains_how_to_stop_being_asked(self):
        tool = self.registry.get("inspect_image")
        text = tool.preview({"path": str(self.ext_png)})
        self.assertIsNotNone(text)
        self.assertIn("VA_READ_DIRS", text, "要告诉用户怎么免掉这个询问")
        self.assertIn(str(self.outside), text)
        self.assertIn("锁死", text, "要说清楚哪些仍然不能越界")

    def test_inside_preview_is_silent(self):
        tool = self.registry.get("inspect_image")
        self.assertIsNone(tool.preview({"path": "inner.png"}))

    @unittest.skipUnless(
        __import__("va.imaging", fromlist=["pillow_available"]).pillow_available(),
        "需要 Pillow",
    )
    def test_it_actually_reads_the_external_image(self):
        """审批通过之后，读要真的读得到。"""
        tool = self.registry.get("inspect_image")
        out = tool.run(path=str(self.ext_png))
        self.assertIn("设计稿.png", out)
        self.assertIn("主色", out)


class TestSubagentInheritsReadDirs(AgentTestCase):
    """子 agent 必须继承父的额外读取目录。

    否则「读 ~/Desktop 那张设计稿」在主对话里能过，一委派出去就失效——
    这种不一致最难查。
    """

    def test_child_workspace_keeps_extra_roots(self):
        from va.tools.context import AgentContext
        from va.tools.delegate import child_registry
        from va.plan import PlanBoard

        outside = Path(self._tmp.name).parent / f"{Path(self._tmp.name).name}-inh"
        outside.mkdir(exist_ok=True)
        try:
            workspace = Workspace(self.root, extra_read_roots=[outside])
            ctx = AgentContext(
                workspace=workspace,
                cfg=self.cfg,
                plan_board=PlanBoard(),
                depth=0,
                max_depth=2,
            )
            registry = child_registry(ctx, "read", PlanBoard())
            tool = registry.get("inspect_image")
            self.assertIsNotNone(tool)
            self.assertFalse(
                tool.needs_approval({"path": str(outside / "x.png")}),
                "子 agent 丢掉了父的放行目录",
            )
        finally:
            import shutil

            shutil.rmtree(outside, ignore_errors=True)


def _tiny_png() -> bytes:
    """一张 1x1 的真 PNG，让 Pillow 能解码。"""
    import base64

    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
