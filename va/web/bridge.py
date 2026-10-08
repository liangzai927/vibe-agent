"""Web 界面与 agent 之间的桥。

三件事：

``EventBus``      agent 在 worker 线程里产生事件，浏览器通过 SSE 消费。
                  用「有序日志 + 序号」而不是队列——浏览器会断线重连，
                  队列读过就没了，日志可以按序号重放。

``ApprovalGate``  审批必须**阻塞 worker 线程**直到人来点按钮。这是整个
                  web 化里最关键的一点：终端里 input() 天然阻塞，
                  到了 web 里如果不显式阻塞，agent 会一路跑到底，
                  审批形同虚设。

``WebUI``         把 UI 抽象类实现成「发事件 + 等待回答」。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from ..ui import UI
from ..util import preview, strip_ansi
from ..workspace import Workspace

# 审批等太久就当作拒绝。不能无限等——浏览器关掉之后 worker 线程会永远挂着。
APPROVAL_TIMEOUT = 30 * 60

# 流式文本先攒一攒再发。逐 token 发事件会把 SSE 打爆，也没必要——
# 人眼分辨不出 20ms 的差别，但服务器要为每个 token 做一次 JSON 序列化。
STREAM_FLUSH_CHARS = 240
STREAM_FLUSH_SECONDS = 0.06


class EventBus:
    """带序号的事件日志，支持断线重连后按序号补发。"""

    def __init__(self, limit: int = 6000) -> None:
        self._events: list[dict] = []
        self._limit = limit
        self._seq = 0
        self._cv = threading.Condition()

    @property
    def seq(self) -> int:
        with self._cv:
            return self._seq

    def publish(self, event_type: str, **payload: Any) -> int:
        with self._cv:
            self._seq += 1
            event = {"seq": self._seq, "type": event_type, **payload}
            self._events.append(event)
            if len(self._events) > self._limit:
                del self._events[: len(self._events) - self._limit]
            self._cv.notify_all()
            return self._seq

    def since(self, seq: int) -> list[dict]:
        with self._cv:
            return [event for event in self._events if event["seq"] > seq]

    def wait(self, seq: int, timeout: float = 20.0) -> list[dict]:
        """等到有比 seq 更新的事件，或者超时。

        超时是必要的：SSE 连接需要定期发心跳，否则中间的代理会掐掉它。
        """
        with self._cv:
            if not any(event["seq"] > seq for event in self._events):
                self._cv.wait(timeout)
            return [event for event in self._events if event["seq"] > seq]


class ApprovalGate:
    """让 worker 线程停在审批上，等浏览器那边的按钮。"""

    def __init__(self, timeout: float = APPROVAL_TIMEOUT) -> None:
        self.timeout = timeout
        self._pending: dict[str, str] = {}
        self._waiters: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._counter = 0

    def request(self, announce: Callable[[str], None]) -> tuple[str, str]:
        """阻塞直到有人回答。返回 (请求 id, 答案)。

        ``announce`` 拿到请求 id，负责把它发到浏览器。
        """
        with self._lock:
            self._counter += 1
            request_id = f"req-{self._counter}"
            waiter = threading.Event()
            self._waiters[request_id] = waiter

        announce(request_id)

        answered = waiter.wait(timeout=self.timeout)
        with self._lock:
            self._waiters.pop(request_id, None)
            answer = self._pending.pop(request_id, "")
        if not answered:
            return request_id, "timeout"
        return request_id, (answer or "no")

    def resolve(self, request_id: str, answer: str) -> bool:
        with self._lock:
            # 立刻摘掉 waiter。不摘的话，在 worker 被唤醒之前再 POST 一次
            # 同一个 id 会「成功」，而那个回答其实永远不会被读到。
            waiter = self._waiters.pop(request_id, None)
            if waiter is None:
                return False
            self._pending[request_id] = answer
            waiter.set()
            return True

    @property
    def waiting(self) -> int:
        with self._lock:
            return len(self._waiters)


class WebUI(UI):
    """把 UI 接口实现成「往浏览器推事件」。"""

    def __init__(
        self,
        workspace: Workspace,
        bus: EventBus,
        gate: ApprovalGate,
        *,
        auto_approve: bool = False,
        show_thinking: bool = True,
    ) -> None:
        super().__init__(workspace, auto_approve=auto_approve, show_thinking=show_thinking)
        self.bus = bus
        self.gate = gate
        self._buffer: list[str] = []
        self._buffer_kind = "text"
        # 必须是「现在」而不是 0——用 0 的话第一次推送就会被判定为过期，
        # 攒批完全失效，每个 token 都会立刻发出去。
        self._buffer_at = time.monotonic()
        self._open_message = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------- 流式缓冲

    def _flush(self) -> None:
        with self._lock:
            if not self._buffer:
                return
            piece = "".join(self._buffer)
            kind = self._buffer_kind
            self._buffer.clear()
            self._buffer_at = time.monotonic()
        self.bus.publish("thinking_delta" if kind == "thinking" else "text_delta", piece=piece)

    def _push(self, kind: str, piece: str) -> None:
        if not piece:
            return
        # 类型切换（正文 ↔ 思考）要先把攒下的发出去，否则两段会串在一起
        if self._buffer and kind != self._buffer_kind:
            self._flush()
        with self._lock:
            self._buffer_kind = kind
            self._buffer.append(piece)
            self._open_message = True
            total = sum(len(item) for item in self._buffer)
            stale = time.monotonic() - self._buffer_at > STREAM_FLUSH_SECONDS
        if total >= STREAM_FLUSH_CHARS or stale:
            self._flush()

    def _end_message(self) -> None:
        self._flush()
        # 只有真的流过内容才收尾。否则每次工具调用都会多一个空的 message_end，
        # 前端会凭空断出一段。
        if self._open_message:
            self._open_message = False
            self.bus.publish("message_end")

    # ------------------------------------------------------------- 基础

    def banner(self, model: str, base_url: str, session_id: str | None = None) -> None:
        self.bus.publish(
            "banner",
            model=model,
            base_url=base_url,
            workspace=str(self.workspace.root),
            session=session_id or "",
        )

    def info(self, text: str) -> None:
        self._end_message()
        self.bus.publish("note", level="info", text=text)

    def warn(self, text: str) -> None:
        self._end_message()
        self.bus.publish("note", level="warn", text=text)

    def error(self, text: str) -> None:
        self._end_message()
        self.bus.publish("note", level="error", text=text)

    def success(self, text: str) -> None:
        self._end_message()
        self.bus.publish("note", level="success", text=text)

    # ------------------------------------------------------------- 流式

    def assistant_header(self, step: int) -> None:
        self._end_message()
        self.bus.publish("step", step=step)

    def stream_text(self, piece: str) -> None:
        self._push("text", piece)

    def stream_thinking(self, piece: str) -> None:
        if not self.show_thinking:
            return
        self._push("thinking", piece)

    def _close_stream(self) -> None:
        self._flush()

    def _close_stream_thinking(self) -> None:
        self._flush()

    def newline(self) -> None:
        self._end_message()

    # ------------------------------------------------------------- 工具

    def tool_call(self, index: int, total: int, name: str, summary: str) -> None:
        self._end_message()
        self.bus.publish(
            "tool_call", index=index, total=total, name=name, summary=summary
        )

    def tool_result(self, text: str, ok: bool) -> None:
        lines = text.strip().splitlines() or ["(空)"]
        self.bus.publish(
            "tool_result",
            ok=ok,
            text="\n".join(lines[:60]),
            first=preview(lines[0], 200),
            total_lines=len(lines),
        )

    def show_plan(self, plan: Any) -> None:
        self._end_message()
        self.bus.publish("plan", text=plan.render())

    def block(self, title: str, rows: list[tuple[str, str]]) -> None:
        self._end_message()
        self.bus.publish("block", title=title, rows=[[k, v] for k, v in rows])

    def horizontal_rule(self, label: str = "") -> None:
        self._end_message()
        self.bus.publish("rule", label=label)

    def answer(self, text: str) -> None:
        self._end_message()
        self.bus.publish("answer", text=text)

    # ------------------------------------------------------------- 审批

    def confirm(
        self,
        tool_name: str,
        args: dict[str, Any],
        detail: str,
        preview: str | None = None,
    ) -> bool:
        self._end_message()

        if self.auto_approve or tool_name in self._always_allow:
            self.bus.publish(
                "note", level="warn", text=f"自动批准 {tool_name}  {detail}"
            )
            return True

        payload = {
            "tool": tool_name,
            "detail": detail,
            "args": {k: (v if isinstance(v, str) else str(v)) for k, v in args.items()},
            # preview 是终端着色的 ANSI 文本，浏览器里要重新着色，
            # 所以把转义序列剥掉再发。
            "preview": strip_ansi(preview) if preview else "",
        }

        def announce(request_id: str) -> None:
            self.bus.publish("approval", id=request_id, **payload)

        request_id, answer = self.gate.request(announce)
        self.bus.publish("approval_done", id=request_id, answer=answer)

        if answer == "timeout":
            self.bus.publish(
                "note", level="error", text=f"审批超时（{tool_name}），按拒绝处理"
            )
            return False
        if answer in ("a", "always"):
            self._always_allow.add(tool_name)
            return True
        return answer in ("y", "yes")

    def show_diff(self, diff_text: str) -> None:
        self._end_message()
        self.bus.publish("diff", text=strip_ansi(diff_text))

    # ------------------------------------------------------------- 其他

    def user_prompt(self) -> str:
        """web 端的输入由服务器驱动，这里不会被调用。"""
        raise RuntimeError("web 界面下不应调用 user_prompt")
