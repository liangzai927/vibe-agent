"""后台长驻进程（生产工作流·阶段 B）。

**为什么需要**：`run_bash` 是「跑完才返回」。起 `npm run dev` 这种永远不结束的
进程，它要么挂到超时、要么超时被杀。而跑不起项目，所有自动化测试都是空谈。

三个最容易做错的地方，每个都有测试盯着：
  1. 不读走 stdout → 子进程写满管道缓冲区后**卡死**
  2. 只杀父进程 → 孤儿继续占着端口
  3. 用 sleep 等就绪 → 慢机器不够、快机器白等、**服务崩了还继续走**
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import AgentTestCase  # noqa: E402
from va.process import ProcessManager, kill_group, port_is_free  # noqa: E402

#: 一个真的监听端口的服务
SERVER = """\
import http.server, socketserver, sys
port = int(sys.argv[1])
print("ready", flush=True)
socketserver.TCPServer.allow_reuse_address = True
socketserver.TCPServer(("127.0.0.1", port), http.server.SimpleHTTPRequestHandler).serve_forever()
"""


def free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class ProcessTestCase(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.manager = ProcessManager(self.root, limit=5)
        self.addCleanup(self.manager.stop_all)
        self.server = self.root / "server.py"
        self.server.write_text(SERVER, encoding="utf-8")

    def start_server(self, port: int, *, label: str = "web"):
        return self.manager.start(label, f"{sys.executable} {self.server} {port}")

    def wait(self, seconds: float = 0.8) -> None:
        time.sleep(seconds)


class TestStartAndRead(ProcessTestCase):
    def test_starts_and_captures_output(self):
        managed, error = self.manager.start("hello", "echo 你好")
        self.assertIsNotNone(managed, error)
        self.wait(0.6)
        self.assertIn("你好", managed.output_text())

    def test_handle_is_returned_and_reusable(self):
        """**句柄要跨工具调用存活**——启动之后还要在后续调用里读它、停它。"""
        managed, _ = self.manager.start("x", "sleep 5")
        self.assertEqual(managed.handle, "p1")
        self.assertIs(self.manager.get("p1"), managed)
        self.assertIsNotNone(self.manager.get("p1"))

    def test_exit_code_is_recorded(self):
        """**不能是 None**。stdout 关闭的瞬间进程可能还没被回收，
        直接 poll() 会拿到 None，日志里就会出现「退出码 None」这种废话。"""
        managed, _ = self.manager.start("bad", "exit 7")
        self.wait(0.8)
        self.assertEqual(managed.exit_code, 7)
        self.assertIn("退出码 7", managed.output_text())

    def test_incremental_read_uses_a_cursor(self):
        managed, _ = self.manager.start("count", "for i in 1 2 3; do echo $i; sleep 0.2; done")
        self.wait(0.4)
        first, cursor = managed.since(0)
        self.wait(0.6)
        second, _ = managed.since(cursor)

        self.assertTrue(first)
        self.assertTrue(second)
        # 增量不该重复第一次的内容
        self.assertNotIn(first[0], second)

    def test_empty_command_is_rejected(self):
        managed, error = self.manager.start("x", "   ")
        self.assertIsNone(managed)
        self.assertIn("为空", error)

    def test_bad_cwd_is_rejected(self):
        managed, error = self.manager.start("x", "echo hi", cwd=self.root / "nope")
        self.assertIsNone(managed)
        self.assertIn("不存在", error)


class TestOutputDoesNotDeadlock(ProcessTestCase):
    def test_a_chatty_process_does_not_hang(self):
        """**这条是整块功能的命门。**

        管道缓冲区满了之后子进程会**阻塞在写**上。不持续读走 stdout，
        一个话多的服务会在输出约 64KB 后整个卡死——而外面看起来
        只是「服务没起来」，极难查。

        这里让它输出远超缓冲区的量，确认它还能正常结束。
        """
        managed, _ = self.manager.start(
            "chatty",
            f'{sys.executable} -c "'
            "for i in range(20000): print('x' * 80)"
            '"',
        )
        deadline = time.time() + 30
        while managed.running and time.time() < deadline:
            time.sleep(0.1)

        self.assertFalse(managed.running, "话多的进程卡死了——说明 stdout 没被读走")
        self.assertEqual(managed.exit_code, 0)

    def test_output_buffer_is_bounded(self):
        """有上限，不然跑久了吃光内存。"""
        from va.process import OUTPUT_LINES

        managed, _ = self.manager.start(
            "flood", f'{sys.executable} -c "for i in range(5000): print(i)"'
        )
        self.wait(2.0)
        self.assertLessEqual(len(managed.tail(99999)), OUTPUT_LINES)


class TestReadiness(ProcessTestCase):
    def test_waits_until_the_port_answers(self):
        port = free_port()
        managed, _ = self.start_server(port)
        ok, message = self.manager.wait_for_port(port, handle=managed.handle, timeout=15)
        self.assertTrue(ok, message)
        self.assertFalse(port_is_free(port))

    def test_dying_process_fails_fast_with_its_output(self):
        """**不等满超时**。服务崩了要立刻知道——等 60 秒再报「超时」
        远不如立刻报「进程已退出，日志如下」。"""
        port = free_port()
        managed, _ = self.manager.start(
            "die", "echo 'Error: 端口被占用' >&2 && exit 1"
        )
        start = time.time()
        ok, message = self.manager.wait_for_port(port, handle=managed.handle, timeout=30)
        elapsed = time.time() - start

        self.assertFalse(ok)
        self.assertLess(elapsed, 5, "没有立刻失败，白等了")
        self.assertIn("退出", message)
        self.assertIn("端口被占用", message, "报错必须带真实日志")

    def test_timeout_reports_the_process_output(self):
        port = free_port()
        managed, _ = self.manager.start("idle", "echo 起了但没监听 && sleep 30")
        ok, message = self.manager.wait_for_port(port, handle=managed.handle, timeout=1)
        self.assertFalse(ok)
        self.assertIn("没监听", message, "超时也要带上日志，否则没法排查")

    def test_unknown_handle(self):
        ok, message = self.manager.wait_for_port(12345, handle="p99", timeout=1)
        self.assertFalse(ok)
        self.assertIn("没有这个进程", message)


class TestStopReleasesThePort(ProcessTestCase):
    def test_stop_kills_the_whole_group(self):
        """**只杀父进程会留下孤儿继续占着端口。**

        `npm run dev` 会派生子进程，真正的 server 在孙子层。
        下一次启动就会报「端口被占用」，而你会以为是代码问题。
        """
        port = free_port()
        # 用 sh -c 包一层，制造「父进程 → 子进程 → 真服务」的结构
        managed, _ = self.manager.start(
            "nested", f"sh -c '{sys.executable} {self.server} {port}'"
        )
        ok, message = self.manager.wait_for_port(port, handle=managed.handle, timeout=15)
        self.assertTrue(ok, message)

        self.manager.stop(managed.handle)
        time.sleep(1.2)

        self.assertTrue(port_is_free(port), "端口还占着——有孤儿进程活下来了")

    def test_force_stop(self):
        managed, _ = self.manager.start("stubborn", "sleep 30")
        ok, _ = self.manager.stop(managed.handle, force=True)
        self.assertTrue(ok)
        self.wait(0.5)
        self.assertFalse(managed.running)

    def test_stopping_an_already_dead_process_is_not_an_error(self):
        managed, _ = self.manager.start("quick", "exit 0")
        self.wait(0.6)
        ok, message = self.manager.stop(managed.handle)
        self.assertTrue(ok)
        self.assertIn("早就退出", message)

    def test_stop_unknown_handle(self):
        ok, message = self.manager.stop("p99")
        self.assertFalse(ok)
        self.assertIn("没有这个进程", message)

    def test_stop_all(self):
        self.manager.start("a", "sleep 30")
        self.manager.start("b", "sleep 30")
        self.assertEqual(self.manager.stop_all(), 2)


class TestConcurrencyLimit(ProcessTestCase):
    def test_limit_is_enforced(self):
        """防止把机器跑满。"""
        self.manager.limit = 3
        for index in range(3):
            self.manager.start(f"job{index}", "sleep 30")
        managed, error = self.manager.start("extra", "sleep 30")
        self.assertIsNone(managed)
        self.assertIn("上限", error)

    def test_finished_processes_free_up_slots(self):
        """退出的进程不该一直占着名额，否则跑几次就没法再起了。"""
        self.manager.limit = 1
        managed, _ = self.manager.start("once", "exit 0")
        self.wait(0.8)
        again, error = self.manager.start("twice", "sleep 10")
        self.assertIsNotNone(again, error)

    def test_manager_can_be_built_with_a_custom_limit(self):
        small = ProcessManager(self.root, limit=1)
        self.addCleanup(small.stop_all)
        self.assertIsNotNone(small.start("a", "sleep 10")[0])
        self.assertIsNone(small.start("b", "sleep 10")[0])


class TestProcessManagerWiring(AgentTestCase):
    def test_agent_has_a_process_manager(self):
        from va.loop import Agent
        from va.ui import UI

        agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)
        self.assertIsNotNone(agent.processes)
        self.assertIs(agent.agent_ctx.processes, agent.processes)

    def test_tools_are_registered(self):
        from va.loop import Agent
        from va.ui import UI

        agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)
        for name in (
            "start_process", "read_output", "wait_for_port",
            "stop_process", "list_processes",
        ):
            self.assertIsNotNone(agent.registry.get(name), f"{name} 没注册")

    def test_start_process_with_expect_port_waits_for_readiness(self):
        """一步到位：起了又等就绪，省掉单独一次 wait_for_port。"""
        from va.loop import Agent
        from va.ui import UI

        server = self.root / "s.py"
        server.write_text(SERVER, encoding="utf-8")
        port = free_port()
        agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)
        self.addCleanup(agent.processes.stop_all)

        result = agent.registry.get("start_process").run(
            command=f"{sys.executable} {server} {port}",
            label="web",
            expect_port=port,
            timeout=15,
        )
        self.assertIn("已就绪", result)

    def test_start_process_that_dies_reports_the_log(self):
        from va.loop import Agent
        from va.tools.base import ToolError
        from va.ui import UI

        agent = Agent(self.cfg, UI(self.workspace), workspace=self.workspace)
        self.addCleanup(agent.processes.stop_all)
        port = free_port()

        with self.assertRaises(ToolError) as ctx:
            agent.registry.get("start_process").run(
                command="echo '配置缺失' >&2 && exit 1",
                label="bad",
                expect_port=port,
                timeout=20,
            )
        self.assertIn("配置缺失", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
