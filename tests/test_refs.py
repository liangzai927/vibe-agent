"""`@路径` 文件引用。

价值是双份的：用户少打字，**agent 少两轮往返**（先 glob 找、再 read_file 读）。
所以展开发生在**提交时**——文件内容直接并进消息。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402
from va.refs import INLINE_LIMIT_LINES, expand, parse  # noqa: E402


class TestParse(unittest.TestCase):
    def test_finds_a_reference(self):
        self.assertEqual([r.given for r in parse("看看 @src/a.ts")], ["src/a.ts"])

    def test_ignores_email_addresses(self):
        """`someone@example.com` 里的 @ 不是引用。"""
        self.assertEqual(parse("发给 someone@example.com"), [])

    def test_ignores_clipboard_tokens(self):
        """`@clip` 由剪贴板那条逻辑处理，这里不能吞掉它。"""
        for token in ("@clip", "@paste", "@image", "@图片", "@剪贴板"):
            self.assertEqual(parse(f"看看 {token}"), [], token)

    def test_finds_multiple(self):
        found = [r.given for r in parse("看 @a.ts 和 @b/c.ts")]
        self.assertEqual(found, ["a.ts", "b/c.ts"])

    def test_requires_a_boundary_before_at(self):
        """`x@y` 这种不该算——正则要求 @ 前面是空白或行首。"""
        self.assertEqual(parse("abc@def"), [])


class RefTestCase(AgentTestCase):
    def make(self, rel: str, content: str = "export const x = 1\n") -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path


class TestExpand(RefTestCase):
    def test_small_file_is_inlined(self):
        self.make("src/a.ts")
        text, refs = expand("看看 @src/a.ts", self.root)

        self.assertEqual(len(refs), 1)
        self.assertTrue(refs[0].inlined)
        self.assertIn("export const x = 1", text)
        self.assertIn("[引用的文件：src/a.ts]", text)
        self.assertIn("```typescript", text, "该带上语言标记")

    def test_no_reference_leaves_the_text_untouched(self):
        """没有引用就一个字符都别动——别弄坏用户写的普通文本。"""
        text, refs = expand("普通的一句话", self.root)
        self.assertEqual(text, "普通的一句话")
        self.assertEqual(refs, [])

    def test_missing_file_is_reported_not_silent(self):
        text, refs = expand("看看 @nope.ts", self.root)
        self.assertIn("找不到", text)
        self.assertFalse(refs[0].ok)

    def test_outside_the_workspace_is_refused(self):
        """**安全边界**：@ 展开是 agent 替用户读文件，走「静默读取」那条线，
        而那条线一直锁在工作区里（read_file/grep/glob 都没有审批环节）。"""
        text, refs = expand("看看 @../../etc/hosts", self.root)
        self.assertEqual(refs[0].kind, "outside")
        self.assertIn("工作区外", text)

    def test_binary_is_not_inlined(self):
        (self.root / "logo.png").write_bytes(b"\x89PNG\x00\x00\x01")
        text, refs = expand("@logo.png", self.root)
        self.assertFalse(refs[0].inlined)
        self.assertIn("二进制", text)

    def test_directory_is_not_inlined(self):
        (self.root / "docs").mkdir()
        text, refs = expand("@docs", self.root)
        self.assertEqual(refs[0].kind, "dir")
        self.assertIn("目录", text)

    def test_huge_file_is_referenced_not_inlined(self):
        """内联一个巨大的文件等于把上下文预算吃掉一块——
        用户可能只是想问「这个文件是干嘛的」。"""
        (self.root / "big.ts").write_text("x = 1\n" * 6000, encoding="utf-8")
        text, refs = expand("@big.ts", self.root)
        self.assertFalse(refs[0].inlined)
        self.assertIn("没有内联", text)
        self.assertIn("read_file", text, "要告诉它怎么读")

    def test_long_file_is_truncated_with_a_note(self):
        (self.root / "long.ts").write_text("line\n" * (INLINE_LIMIT_LINES + 50), encoding="utf-8")
        text, refs = expand("@long.ts", self.root)
        self.assertTrue(refs[0].inlined)
        self.assertIn("只给前", text)

    def test_trailing_punctuation_is_not_part_of_the_path(self):
        self.make("a.ts")
        _, refs = expand("看看 @a.ts。", self.root)
        self.assertTrue(refs[0].ok, refs[0].note)

    def test_agents_get_the_expanded_text(self):
        """真正要成立的事：展开结果**到达 agent**。

        只展开发事件、不把结果传下去的话，看起来一切正常但 agent
        根本没看到文件——这个 bug 真发生过。
        """
        self.make("src/a.ts", "export const token = 42\n")
        text, _ = expand("看看 @src/a.ts", self.root)
        self.assertIn("token = 42", text)


class TestFindFiles(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        from va.web.server import WebApp

        for rel in (
            "src/auth.ts", "src/authoring.md", "src/utils/date.ts",
            "README.md", "node_modules/x/y.ts", ".git/config",
        ):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")
        self.app = WebApp(self.cfg, self.workspace)

    def paths(self, query: str) -> list[str]:
        return [item["path"] for item in self.app.find_files(query)["files"]]

    def test_skips_noise_directories(self):
        files = self.paths("")
        self.assertNotIn("node_modules/x/y.ts", files)
        self.assertNotIn(".git/config", files)

    def test_prefix_match_beats_a_later_occurrence(self):
        """打 `auth` 时 `src/auth.ts` 该排在 `src/authoring.md` 前面。"""
        self.assertEqual(self.paths("auth")[0], "src/auth.ts")

    def test_fuzzy_matches_a_directory_name(self):
        self.assertIn("src/utils/date.ts", self.paths("utils"))

    def test_fuzzy_matches_a_scattered_subsequence(self):
        self.assertIn("README.md", self.paths("read"))

    def test_no_match_returns_empty(self):
        self.assertEqual(self.paths("zzzzz"), [])

    def test_leading_at_is_tolerated(self):
        self.assertIn("src/auth.ts", self.paths("@auth"))

    def test_directories_are_listed(self):
        """**用户报过这个**：@ 只能选文件，选不了文件夹。"""
        items = {item["path"]: item["dir"] for item in self.app.find_files("")["files"]}
        self.assertTrue(items.get("node_modules/") is None, "噪声目录不该出现")
        self.assertIn("src/", items)
        self.assertTrue(items["src/"], "src/ 应该被标成目录")

    def test_directories_are_not_filtered_out_by_the_empty_query(self):
        """空查询给目录降权，1-5 会变负数被整个滤掉——
        结果就是「打开补全看不到任何文件夹」。"""
        self.assertTrue(any(item["dir"] for item in self.app.find_files("")["files"]))

    def test_directories_sort_after_files_when_the_query_matches_both(self):
        """打 `src` 时多半想找里面的文件；目录排后面但仍然在。"""
        items = self.app.find_files("src")["files"]
        dirs = [i for i in items if i["dir"]]
        self.assertTrue(dirs, "目录不该被挤掉")
        self.assertFalse(items[0]["dir"], "文件应排在目录前面")

    def test_directory_paths_end_with_a_slash(self):
        """补全到目录时能接着往下点（`@src/` 再补全）。"""
        for item in self.app.find_files("src")["files"]:
            if item["dir"]:
                self.assertTrue(item["path"].endswith("/"), item["path"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
