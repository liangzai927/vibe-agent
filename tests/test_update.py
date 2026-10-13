"""`va update` —— 自己更新自己。

**难点不在「拉新代码」，在「认出当初是怎么装的」。**
pipx / pip / git clone / 压缩包，四种装法的更新方式完全不同，
弄错了轻则没更新，重则把环境搞坏。

所以这里重点测**检测**和**「认不出来时怎么说」**——
而不是真的去联网更新。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import va.update as update  # noqa: E402
from va.update import Install, current_revision, detect, plan  # noqa: E402


class TestDetection(unittest.TestCase):
    def test_source_checkout_is_recognised(self):
        """旁边有 .git 和 run.py → 从源码跑。"""
        install = detect()
        self.assertEqual(install.kind, "git", install.reason)
        self.assertTrue(install.can_self_update)
        self.assertIsNotNone(install.root)

    def test_git_install_plans_a_pull(self):
        commands, why = plan(Install(kind="git", root=Path("/tmp/x")))
        self.assertEqual(commands, ["git pull --ff-only"])
        self.assertIn("/tmp/x", why)

    def test_pipx_install_plans_a_forced_reinstall(self):
        """**`--force` 是必须的**：从 git 装的时候 `pipx upgrade`
        有时会用缓存的旧版本。"""
        install = Install(kind="pipx", source="git+https://example.com/x.git")
        commands, _ = plan(install)
        self.assertEqual(len(commands), 1)
        self.assertIn("pipx install --force", commands[0])
        self.assertIn("git+https://example.com/x.git", commands[0])

    def test_pip_install_plans_an_upgrade(self):
        install = Install(kind="pip", source="git+https://example.com/x.git")
        commands, _ = plan(install)
        self.assertIn("pip install --upgrade", commands[0])


class TestCannotSelfUpdate(unittest.TestCase):
    """**认不出来就明说怎么做，不硬猜一个命令跑下去。**

    猜错了可能把用户的环境弄坏——压缩包装的没有来源信息，
    猜成 `pip install` 会装出一个和原来不一样的东西。
    """

    def assert_gives_advice(self, install: Install) -> str:
        commands, note = plan(install)
        self.assertEqual(commands, [], "不该给出命令")
        self.assertTrue(note.strip(), "得说点什么")
        return note

    def test_tarball_install_says_how(self):
        note = self.assert_gives_advice(Install(kind="unknown"))
        self.assertIn("install.sh", note)
        self.assertIn("pipx", note)

    def test_local_install_points_at_the_source_dir(self):
        note = self.assert_gives_advice(
            Install(kind="local", source="file:///home/me/vibe-agent")
        )
        self.assertIn("本地目录", note)

    def test_package_without_a_recorded_source(self):
        note = self.assert_gives_advice(Install(kind="pipx", source=""))
        self.assertIn("pipx install --force", note)

    def test_unknown_kind_cannot_self_update(self):
        self.assertFalse(Install(kind="unknown").can_self_update)
        self.assertFalse(Install(kind="local").can_self_update)

    def test_known_kinds_can(self):
        for kind in ("git", "pipx", "pip"):
            self.assertTrue(Install(kind=kind).can_self_update, kind)


class TestDirectUrl(unittest.TestCase):
    """pip 会把安装来源写进 `direct_url.json`，所以能问出来——
    **不用让用户记自己当初是怎么装的**。"""

    def test_vcs_install_is_detected(self):
        payload = {
            "url": "https://github.com/liangzai927/vibe-agent.git",
            "vcs_info": {"vcs": "git", "requested_revision": "main"},
        }
        with mock.patch.object(update, "_direct_url", lambda: payload), mock.patch.object(
            update.Path, "exists", lambda self: False
        ), mock.patch.object(update.Path, "is_file", lambda self: False):
            install = detect()
        self.assertIn(install.kind, ("pip", "pipx"))
        self.assertEqual(install.source, payload["url"])
        self.assertTrue(install.can_self_update)

    def test_local_dir_install_is_detected(self):
        payload = {"url": "file:///home/me/vibe-agent", "dir_info": {}}
        with mock.patch.object(update, "_direct_url", lambda: payload), mock.patch.object(
            update.Path, "exists", lambda self: False
        ), mock.patch.object(update.Path, "is_file", lambda self: False):
            install = detect()
        self.assertEqual(install.kind, "local")
        self.assertFalse(install.can_self_update, "本地目录装的没法自动更新")

    def test_missing_metadata_is_not_fatal(self):
        with mock.patch.object(update, "_direct_url", lambda: {}), mock.patch.object(
            update.Path, "exists", lambda self: False
        ), mock.patch.object(update.Path, "is_file", lambda self: False):
            install = detect()
        self.assertIn(install.kind, ("pip", "pipx"))

    def test_broken_json_is_not_fatal(self):
        """元数据坏了不该让 `va update` 崩——认不出来就说认不出来。"""
        from importlib.metadata import PackageNotFoundError

        def boom(name):
            raise PackageNotFoundError(name)

        with mock.patch("importlib.metadata.distribution", boom):
            self.assertEqual(update._direct_url(), {})

        class BadMeta:
            def read_text(self, name):
                return "{ 不是 json"

        with mock.patch("importlib.metadata.distribution", lambda name: BadMeta()):
            self.assertEqual(update._direct_url(), {}, "坏 JSON 该被吞掉")


class TestVersion(unittest.TestCase):
    def test_git_revision_is_a_short_hash(self):
        install = detect()
        revision = current_revision(install)
        self.assertTrue(revision)
        if install.kind == "git":
            self.assertLessEqual(len(revision), 12)
            self.assertNotIn(" ", revision)

    def test_unknown_install_falls_back_to_package_version(self):
        self.assertTrue(current_revision(Install(kind="unknown")))


class TestCheckDoesNotModify(unittest.TestCase):
    """`--check` 只查不动。**`git fetch` 只更新远端引用，不改工作区。**"""

    def test_check_on_a_non_git_install_explains(self):
        fresh, note = update.check(Install(kind="pipx", source="git+x"))
        self.assertFalse(fresh)
        self.assertIn("更新", note)

    def test_check_never_runs_a_merge(self):
        """检查阶段不能出现 pull / merge 这类会改工作区的命令。"""
        import inspect

        source = inspect.getsource(update.check)
        for dangerous in ("pull", "merge", "reset", "checkout"):
            self.assertNotIn(
                f'"{dangerous}"', source,
                f"检查阶段跑了 {dangerous}，那会改工作区",
            )


class TestCommandWiring(unittest.TestCase):
    def test_subcommand_is_registered(self):
        source = (
            Path(__file__).resolve().parent.parent / "va" / "cli.py"
        ).read_text(encoding="utf-8")
        self.assertIn('("update", _update)', source)

    def test_help_mentions_it(self):
        source = (
            Path(__file__).resolve().parent.parent / "va" / "cli.py"
        ).read_text(encoding="utf-8")
        self.assertIn("va update", source)

    def test_non_interactive_does_not_update_by_itself(self):
        """**被脚本调起来时不擅自更新**——环境变了用户却不知道。"""
        source = (
            Path(__file__).resolve().parent.parent / "va" / "cli.py"
        ).read_text(encoding="utf-8")
        self.assertIn("非交互环境，没有执行", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
