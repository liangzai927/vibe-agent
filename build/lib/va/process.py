"""后台长驻进程。

**为什么需要**：``run_bash`` 是「跑完才返回」。起 ``npm run dev`` 这种
永远不结束的进程，它要么挂到超时、要么超时被杀。而**跑不起项目，
所有自动化测试都是空谈**。

所以需要另一套模型：启动 → 读日志 → 等就绪 → 停止。

────────────────────────────────────────────────────────────────

**三个必须做对的地方**（做错了会以很难查的方式坏掉）：

1. **必须持续读走 stdout**。管道缓冲区满了之后，子进程会**阻塞在写**上——
   一个话多的 dev server 会在输出约 64KB 后整个卡死，而你从外面看
   只是「服务没起来」。所以每个进程配一个读线程。

2. **必须杀进程组，不能只杀父进程**。``npm run dev`` 会派生子进程
   （真正的 vite/node 在孙子层）。只杀 npm 会留下孤儿**继续占着端口**，
   下一次启动就会「端口被占用」。

3. **就绪必须是探测，不能是 sleep**。慢机器上 5 秒不够，快机器上白等；
   更要命的是**服务崩了也会继续往下走**。探测的同时要盯着进程死活。
"""

from __future__ import annotations

import atexit
import os
import signal
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

#: 每个进程保留多少行输出。话多的服务能刷几万行，不设上限会吃掉内存。
OUTPUT_LINES = 2000
#: 最多同时跑几个。防止把机器跑满。
DEFAULT_LIMIT = 5
#: 就绪探测的间隔和默认上限
PROBE_INTERVAL = 0.2
DEFAULT_READY_TIMEOUT = 60


@dataclass
class ManagedProcess:
    handle: str
    label: str
    command: str
    cwd: Path
    process: subprocess.Popen
    started_at: float = field(default_factory=time.time)
    exit_code: int | None = None
    _lines: deque = field(default_factory=lambda: deque(maxlen=OUTPUT_LINES))
    _cursor: int = 0          # 已经读过多少行（增量读用它）
    _total: int = 0           # 累计产生了多少行（含被挤掉的）
    _reader: threading.Thread | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ---- 状态

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def age(self) -> float:
        return time.time() - self.started_at

    # ---- 输出

    def feed(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)
            self._total += 1

    def since(self, cursor: int) -> tuple[list[str], int]:
        """读增量。``cursor`` 传上次返回的值。

        返回 ``(新行, 新游标)``。**游标是累计计数而不是缓冲区下标**——
        缓冲区有上限，被挤掉之后下标会错位。
        """
        with self._lock:
            dropped = max(0, self._total - len(self._lines))
            start = max(cursor - dropped, 0)
            return list(self._lines)[start:], self._total

    def tail(self, count: int = 40) -> list[str]:
        with self._lock:
            return list(self._lines)[-count:]

    def output_text(self, count: int = 40) -> str:
        return "\n".join(self.tail(count))

    def summary(self) -> str:
        state = "运行中" if self.running else f"已退出（码 {self.exit_code}）"
        return (
            f"{self.handle}  {self.label}  [{state}]  pid={self.pid}  "
            f"已跑 {self.age:.0f}s\n  $ {self.command}\n  在 {self.cwd}"
        )


class ProcessManager:
    """一个会话的全部后台进程。"""

    def __init__(self, workspace: Path, *, limit: int = DEFAULT_LIMIT) -> None:
        self.workspace = Path(workspace)
        self.limit = limit
        self._items: dict[str, ManagedProcess] = {}
        self._counter = 0
        self._lock = threading.Lock()
        atexit.register(self.stop_all)

    # ---- 查询

    def get(self, handle: str) -> ManagedProcess | None:
        return self._items.get((handle or "").strip())

    def all(self) -> list[ManagedProcess]:
        return list(self._items.values())

    def running(self) -> list[ManagedProcess]:
        return [p for p in self._items.values() if p.running]

    # ---- 启动

    def start(
        self,
        label: str,
        command: str,
        *,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[ManagedProcess | None, str]:
        """启动一个后台进程。返回 ``(进程, 错误说明)``。"""
        command = (command or "").strip()
        if not command:
            return None, "命令为空"

        with self._lock:
            # 已经退出的不算占用名额——否则跑几次就没法再起了
            live = [p for p in self._items.values() if p.running]
            if len(live) >= self.limit:
                names = "、".join(f"{p.handle}({p.label})" for p in live)
                return None, (
                    f"后台进程已达上限 {self.limit} 个：{names}。"
                    f"先 stop_process 停掉不用的"
                )
            self._counter += 1
            handle = f"p{self._counter}"

        workdir = Path(cwd) if cwd else self.workspace
        if not workdir.is_dir():
            return None, f"目录不存在：{workdir}"

        merged_env = {
            **os.environ,
            # 让子进程别分页、别上色——日志要能直接读
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "TERM": "dumb",
            # **关掉 Python 的输出缓冲**。不关的话 print 出来的东西
            # 会卡在缓冲区里，日志看起来「什么都没输出」
            "PYTHONUNBUFFERED": "1",
            # 让 npm/vite 之类知道自己在非交互环境里
            "CI": os.environ.get("CI", ""),
            "FORCE_COLOR": "0",
            **(env or {}),
        }

        try:
            process = subprocess.Popen(
                command,
                shell=True,
                cwd=str(workdir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,               # 行缓冲，日志能尽快出来
                start_new_session=True,   # **独立进程组**，才能整组杀
                env=merged_env,
            )
        except OSError as exc:
            return None, f"启动失败：{exc}"

        managed = ManagedProcess(
            handle=handle,
            label=label or command.split()[0],
            command=command,
            cwd=workdir,
            process=process,
        )
        managed._reader = threading.Thread(
            target=self._read_loop, args=(managed,), name=f"va-proc-{handle}", daemon=True
        )
        managed._reader.start()
        self._items[handle] = managed
        return managed, ""

    def _read_loop(self, managed: ManagedProcess) -> None:
        """**必须持续读**。管道缓冲区满了子进程会阻塞在写上面。"""
        stream = managed.process.stdout
        if stream is None:
            return
        try:
            for line in stream:
                managed.feed(line.rstrip("\n"))
        except (OSError, ValueError):
            pass
        finally:
            # **要 wait 而不是 poll**：stdout 关闭的瞬间进程可能还没被回收，
            # poll() 会返回 None，日志里就会出现「退出码 None」这种没用的信息。
            try:
                managed.exit_code = managed.process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                managed.exit_code = managed.process.poll()
            managed.feed(f"⟨进程结束，退出码 {managed.exit_code}⟩")

    # ---- 停止

    def stop(self, handle: str, *, force: bool = False) -> tuple[bool, str]:
        managed = self.get(handle)
        if managed is None:
            return False, f"没有这个进程：{handle}"
        if not managed.running:
            managed.exit_code = managed.process.poll()
            return True, f"{handle} 早就退出了（码 {managed.exit_code}）"

        kill_group(managed.process, force=force)
        managed.exit_code = managed.process.poll()
        return True, f"已停止 {handle}（{managed.label}）"

    def stop_all(self) -> int:
        count = 0
        for managed in list(self._items.values()):
            if managed.running:
                kill_group(managed.process)
                count += 1
        return count

    # ---- 就绪探测

    def wait_for_port(
        self,
        port: int,
        *,
        handle: str = "",
        host: str = "127.0.0.1",
        timeout: float = DEFAULT_READY_TIMEOUT,
    ) -> tuple[bool, str]:
        """等端口可连接。

        **两个都不能省**：
        * 探测而不是 sleep——慢机器上固定等待不够，快机器上白等
        * 每轮盯着进程死活——服务崩了要立刻知道，而不是傻等到超时
        """
        managed = self.get(handle) if handle else None
        if handle and managed is None:
            return False, f"没有这个进程：{handle}"
        if not port:
            return False, "没给端口号"

        started = time.monotonic()
        deadline = started + max(1.0, timeout)
        last_error = ""
        while time.monotonic() < deadline:
            # 进程死了就别等了——**这条决定了报错是否可读**：
            # 等满 60 秒再报「超时」远不如立刻报「进程已退出，日志如下」
            if managed is not None and not managed.running:
                managed.exit_code = managed.process.poll()
                return False, (
                    f"{managed.label} 在端口 {port} 就绪前就退出了"
                    f"（码 {managed.exit_code}）。输出：\n{managed.output_text(30)}"
                )

            try:
                with socket.create_connection((host, port), timeout=0.5):
                    waited = time.monotonic() - started
                    return True, f"端口 {port} 已就绪（等了 {waited:.1f}s）"
            except OSError as exc:
                last_error = str(exc)

            time.sleep(PROBE_INTERVAL)

        hint = ""
        if managed is not None:
            hint = f"\n{managed.label} 的输出：\n{managed.output_text(30)}"
        return False, (
            f"等了 {timeout:.0f} 秒，端口 {port} 还是连不上（{last_error}）{hint}"
        )


def kill_group(process: subprocess.Popen, *, force: bool = False) -> None:
    """杀掉**整个进程组**。

    只杀父进程会留下孤儿子进程继续占着端口——下一次启动就报
    「端口被占用」，而你会以为是代码问题。
    """
    try:
        pgid = os.getpgid(process.pid)
    except OSError:
        pgid = None

    signal_to_send = signal.SIGKILL if force else signal.SIGTERM
    try:
        if pgid is not None:
            os.killpg(pgid, signal_to_send)
        else:
            process.send_signal(signal_to_send)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.terminate()
        except OSError:
            return

    if force:
        return

    # 给 5 秒优雅退出（前端构建进程要收尾），否则强杀
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            if pgid is not None:
                os.killpg(pgid, signal.SIGKILL)
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    """端口现在没人占。测完 stop_process 用。"""
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return False
    except OSError:
        return True
