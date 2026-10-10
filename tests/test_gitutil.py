"""git 报错的翻译。

起因是一个真实的坏消息：用户推送失败，界面上显示
「推送失败：and the repository exists.」——那是 git 报错的**最后一行套话**，
真正的病因（`Connection closed by ... port 22`）在第一行，被丢掉了。

这个翻译被 session_sync（把会话同步到 git）用着，所以留在这里守着。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from va.gitutil import explain as explain_error  # noqa: E402


class TestExplain(unittest.TestCase):
    """把 git 的 stderr 翻成人话。

    起因是一个真实的坏消息：用户推送失败，界面上显示
    「推送失败：and the repository exists.」——那是 git 报错的**最后一行套话**，
    真正的病因（`Connection closed by ... port 22`）在第一行，被丢掉了。
    """

    # git 报错的固定尾巴，任何情况下都不该成为结论
    FILLER = (
        "Please make sure you have the correct access rights\n"
        "and the repository exists."
    )

    def test_never_returns_the_boilerplate_tail(self):
        stderr = (
            "Connection closed by 203.0.113.1 port 22\n"
            "fatal: Could not read from remote repository.\n\n" + self.FILLER
        )
        message = explain_error(stderr)
        self.assertNotIn("repository exists", message)
        self.assertNotIn("correct access rights", message)

    def test_identifies_a_blocked_ssh_port(self):
        stderr = (
            "Connection closed by 203.0.113.1 port 22\n"
            "fatal: Could not read from remote repository.\n\n" + self.FILLER
        )
        message = explain_error(stderr)
        self.assertIn("连不上", message)
        self.assertIn("HTTPS", message, "要给出可操作的替代方案")

    def test_identifies_missing_ssh_key(self):
        stderr = (
            "git@github.com: Permission denied (publickey).\n"
            "fatal: Could not read from remote repository."
        )
        self.assertIn("SSH key", explain_error(stderr))

    def test_identifies_timeout(self):
        self.assertIn("超时", explain_error("ssh: connect to host github.com port 22: Connection timed out"))

    def test_identifies_dns_failure(self):
        self.assertIn("解析", explain_error("ssh: Could not resolve hostname github.com"))

    def test_identifies_missing_repo(self):
        self.assertIn("不存在", explain_error("remote: Repository not found."))

    def test_identifies_rejected_push(self):
        stderr = (
            "! [rejected]        main -> main (non-fast-forward)\n"
            "error: failed to push some refs to 'git@github.com:x/y.git'"
        )
        self.assertIn("拉取", explain_error(stderr))

    def test_falls_back_to_the_first_meaningful_line(self):
        """没有已知模式时，取第一条有信息量的，而不是最后一条。"""
        stderr = "something weird happened\nfatal: Could not read from remote repository.\n\n" + self.FILLER
        self.assertEqual(explain_error(stderr), "something weird happened")

    def test_empty_input(self):
        self.assertTrue(explain_error(""))
        self.assertTrue(explain_error("   \n  "))

    def test_patterns_look_at_all_lines_not_just_the_last(self):
        """病因几乎总在第一行——只看最后一行必然错过。"""
        for first_line, expected in (
            ("Connection closed by x port 22", "连不上"),
            ("ssh: connect to host github.com port 22: Connection timed out", "超时"),
            ("git@github.com: Permission denied (publickey).", "SSH key"),
        ):
            stderr = first_line + "\nfatal: Could not read from remote repository.\n" + self.FILLER
            self.assertIn(expected, explain_error(stderr), f"漏了：{first_line}")

