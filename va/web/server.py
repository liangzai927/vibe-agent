"""本地 HTTP + SSE 服务器：给 agent 套一个浏览器界面。

**为什么是「本地 web」而不是原生窗口**：标准库就够（零新依赖），Docker 里也能跑，
跨平台一致，而且浏览器天生能做的事情正是终端做不到的——拖拽文件、彩色 diff、
点按钮、滚动回看。想要真窗口的话可以用 ``va web --window`` 包一层 pywebview。

线程模型：

    主线程     http.server 在 serve_forever
    worker     一个任务跑在这里（同一时刻只有一个）
    SSE 连接   每个浏览器标签一条，从 EventBus 按序号读

审批在 worker 线程上阻塞，等浏览器 POST 回来。
"""

from __future__ import annotations

import json
import mimetypes
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..clipboard import default_filename, inbox_dir
from ..config import Config
from ..loop import Agent
from ..util import strip_ansi
from ..workspace import Workspace
from .bridge import ApprovalGate, EventBus, WebUI

ASSETS = Path(__file__).resolve().parent / "static"
AGENT_DIR = Path(__file__).resolve().parents[2]

# 前端能用的命令。和终端那套是同一批动作，只是入口不同。
HELP_TEXT = """可用命令：
/diff            看本次会话改过哪些文件
/undo [N]        撤销最近 N 次文件改动
/plan            看当前任务计划
/tools           列出可用工具
/model [名字]     查看或切换模型
/reset           清空对话历史
/help            显示这份帮助"""


class WebApp:
    """把 agent、事件总线、审批闸门绑在一起，供 HTTP 层调用。"""

    def __init__(
        self,
        cfg: Config,
        workspace: Workspace,
        *,
        auto_approve: bool = False,
        session=None,
        history=None,
        undo=None,
        plan=None,
        llm=None,
        registry=None,
        env_path: Path | None = None,
        models_dir: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.workspace = workspace
        self.bus = EventBus()
        self.gate = ApprovalGate()
        self.ui = WebUI(
            workspace,
            self.bus,
            self.gate,
            auto_approve=auto_approve,
            show_thinking=cfg.show_thinking,
        )
        self.agent = Agent(
            cfg,
            self.ui,
            workspace=workspace,
            registry=registry,
            llm=llm,
            session=session,
            history=history,
            undo=undo,
            plan=plan,
        )
        self._task_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self.started_at = time.time()
        self.env_path = env_path or (AGENT_DIR / ".env")
        # 和 env_path 一样可以重定向。测试必须能把这些指到临时目录去——
        # 不然「跑一次测试」就会把开发者真实的配置和模型缓存冲掉。
        self.models_dir = models_dir or AGENT_DIR

    # ------------------------------------------------------------- 模型

    def models(self) -> dict[str, Any]:
        from ..models import load_catalog

        catalog = load_catalog(self.models_dir)
        return {
            "current": self.cfg.model,
            "vision": self.cfg.vision_model,
            "catalog": [
                {
                    "id": m.id,
                    "summary": m.summary,
                    "supports_image": m.supports_image,
                    "context_window": m.context_window,
                }
                for m in (catalog.models if catalog else [])
            ],
            "fetched_at": catalog.fetched_at if catalog else 0,
        }

    def switch_model(self, name: str) -> None:
        """切换主模型并持久化。下次启动还是它。"""
        from ..config import write_env_values

        self.agent.set_model(name)
        try:
            write_env_values(self.env_path, {"VA_MODEL": name})
        except OSError:
            pass  # 写不进去不影响本次会话
        self.bus.publish("model", name=name)

    # ------------------------------------------------------------- 状态

    @property
    def busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def state(self) -> dict[str, Any]:
        return {
            "busy": self.busy,
            "workspace": str(self.workspace.root),
            "model": self.cfg.model,
            "base_url": self.cfg.base_url,
            "vision_model": self.cfg.vision_model,
            "session": self.agent.session_id or "",
            "seq": self.bus.seq,
            "auto_approve": self.ui.auto_approve,
            "awaiting_approval": self.gate.waiting,
        }

    # ------------------------------------------------------------- 任务

    def submit(self, text: str) -> tuple[bool, str]:
        """接收一条输入。返回 (是否受理, 说明)。"""
        text = (text or "").strip()
        if not text:
            return False, "空消息"
        if self.busy:
            return False, "上一个任务还在跑，等它结束"
        if text.startswith("/"):
            return self._command(text)

        self.bus.publish("user", text=text)
        self._worker = threading.Thread(
            target=self._run, args=(text,), name="va-task", daemon=True
        )
        self._worker.start()
        return True, "已开始"

    def _run(self, text: str) -> None:
        with self._task_lock:
            try:
                answer = self.agent.run(text)
            except Exception as exc:  # noqa: BLE001 - 任何异常都要让界面知道
                self.bus.publish(
                    "note", level="error", text=f"{type(exc).__name__}: {exc}"
                )
                answer = ""
            self.bus.publish("task_end", answer=answer or "")

    # ------------------------------------------------------------- 命令

    def _command(self, text: str) -> tuple[bool, str]:
        command, _, rest = text.partition(" ")
        rest = rest.strip()

        if command in ("/help", "/?"):
            self.bus.publish("note", level="info", text=HELP_TEXT)
            return True, "ok"

        if command == "/diff":
            changes = self.agent.undo.cumulative_diff()
            if not changes:
                self.bus.publish("note", level="info", text="本次会话还没有改动过文件")
                return True, "ok"
            for path, diff_text in changes:
                self.bus.publish("note", level="info", text=path)
                self.bus.publish("diff", text=diff_text)
            self.bus.publish("note", level="info", text=f"共 {len(changes)} 个文件有改动")
            return True, "ok"

        if command == "/undo":
            count = int(rest) if rest.isdigit() else 1
            message = self.agent.undo_last(count)
            self.bus.publish("note", level="info", text=message)
            return True, "ok"

        if command == "/plan":
            plan = self.agent.plan
            text_out = plan.render() if plan is not None else "还没有计划"
            self.bus.publish("plan", text=text_out)
            return True, "ok"

        if command == "/tools":
            rows = [
                [tool.name, (tool.description or "").splitlines()[0][:90]]
                for tool in self.agent.registry
            ]
            self.bus.publish("block", title=f"可用工具（{len(rows)} 个）", rows=rows)
            return True, "ok"

        if command == "/model":
            if not rest:
                self.bus.publish("note", level="info", text=f"当前模型：{self.cfg.model}")
                return True, "ok"
            self.agent.set_model(rest)
            self.bus.publish("note", level="success", text=f"已切换到 {rest}")
            return True, "ok"

        if command == "/reset":
            self.agent.reset()
            self.bus.publish("note", level="info", text="对话历史已清空")
            return True, "ok"

        self.bus.publish("note", level="warn", text=f"未知命令：{command}（/help 看列表）")
        return False, "未知命令"

    # ------------------------------------------------------------- 上传

    def save_upload(self, filename: str, payload: bytes) -> dict[str, Any]:
        """把浏览器拖进来的图片存进工作区。"""
        name = Path(filename or "").name or default_filename()
        if not name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
            name += ".png"
        target = inbox_dir(self.workspace.root) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

        size = None
        try:
            from PIL import Image

            with Image.open(target) as image:
                size = image.size
        except Exception:  # noqa: BLE001 - 量不出尺寸不影响上传
            size = None

        # 产物在 agent 自己的目录下（不在用户项目里），所以「相对路径」
        # 其实是给界面显示的路径：工作区内给相对路径，否则给 ~/.vibe-agent/…
        if self.workspace.contains(target):
            shown = self.workspace.relative(target)
        else:
            from ..paths import describe

            shown = describe(target)

        return {
            "path": str(target),
            "relative": shown,
            "bytes": len(payload),
            "size": list(size) if size else None,
        }


# --------------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "vibe-agent"
    protocol_version = "HTTP/1.1"

    app: WebApp  # 由 serve() 注入

    def log_message(self, fmt: str, *args: Any) -> None:
        """默认会把每个请求打到 stderr，本地单用户没必要刷屏。"""
        return

    # ----------------------------------------------------------- 工具

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _asset(self, name: str) -> None:
        path = (ASSETS / name).resolve()
        if not str(path).startswith(str(ASSETS.resolve())) or not path.is_file():
            self.send_error(404, "not found")
            return
        body = path.read_bytes()
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind in ("application/javascript",):
            kind += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ----------------------------------------------------------- GET

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        if parsed.path in ("/", "/index.html"):
            self._asset("index.html")
            return
        if parsed.path == "/api/state":
            self._json(self.app.state())
            return
        if parsed.path == "/api/models":
            self._json(self.app.models())
            return

        if parsed.path == "/api/raw":
            # 缩略图预览要用。走 Workspace 解析路径，天然被沙箱约束住，
            # 不会因为一个 ../ 就把工作区外的文件读出去。
            params = parse_qs(parsed.query)
            rel = (params.get("path") or [""])[0]
            from ..workspace import WorkspaceError

            try:
                target = self.app.workspace.resolve_readable(rel, must_exist=True)
            except (WorkspaceError, OSError):
                self.send_error(404, "not found")
                return
            # 只允许工作区和 agent 自己的目录。resolve_readable 本身不设限
            # （它服务于「用户指着任意路径的图」），这里必须自己收回来——
            # 否则一个 ../ 就能把整个文件系统读出去。
            if not (
                self.app.workspace.contains(target)
                or self.app.workspace.writable_output(target)
            ):
                self.send_error(404, "not found")
                return
            if not target.is_file():
                self.send_error(404, "not found")
                return
            body = target.read_bytes()
            kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/events":
            params = parse_qs(parsed.query)
            since = int((params.get("since") or ["0"])[0] or 0)
            self._sse(since)
            return
        self._asset(parsed.path.lstrip("/"))

    def _sse(self, since: int) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            while True:
                events = self.app.bus.wait(since, timeout=15)
                if not events:
                    # 心跳。没有它，中间的代理或浏览器会以为连接死了。
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                for event in events:
                    payload = json.dumps(event, ensure_ascii=False)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    since = event["seq"]
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return  # 浏览器关了标签页，正常

    # ----------------------------------------------------------- POST

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        if parsed.path == "/api/message":
            payload = self._body()
            ok, message = self.app.submit(str(payload.get("text", "")))
            self._json({"ok": ok, "message": message}, 200 if ok else 409)
            return

        if parsed.path == "/api/model":
            payload = self._body()
            name = str(payload.get("name", "")).strip()
            if not name:
                self._json({"ok": False, "error": "没有指定模型"}, 400)
                return
            self.app.switch_model(name)
            self._json({"ok": True, "model": name})
            return

        if parsed.path == "/api/approve":
            payload = self._body()
            resolved = self.app.gate.resolve(
                str(payload.get("id", "")), str(payload.get("answer", "no"))
            )
            self._json({"ok": resolved})
            return

        if parsed.path == "/api/upload":
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 64 * 1024 * 1024:
                self._json({"ok": False, "error": "文件为空或超过 64MB"}, 400)
                return
            name = self.headers.get("X-Filename") or "clip.png"
            from urllib.parse import unquote

            info = self.app.save_upload(unquote(name), self.rfile.read(length))
            self.app.bus.publish("note", level="success", text=f"已接收图片 {info['relative']}")
            self._json({"ok": True, **info})
            return

        self.send_error(404, "not found")


def _pick_port(preferred: int) -> int:
    """优先用指定端口；被占用就让系统给一个，免得「端口被占」直接失败。"""
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return probe.getsockname()[1]
    raise RuntimeError("找不到可用端口")


def serve(
    app: WebApp,
    *,
    port: int = 8420,
    host: str = "127.0.0.1",
    open_browser: bool = True,
    window: bool = False,
) -> int:
    """起服务。默认只监听回环地址——这东西能执行任意命令，不该暴露到局域网。"""
    chosen = _pick_port(port) if host == "127.0.0.1" else port
    Handler.app = app

    httpd = ThreadingHTTPServer((host, chosen), Handler)
    httpd.daemon_threads = True
    url = f"http://{host}:{chosen}/"

    app.bus.publish("note", level="info", text=f"界面地址 {url}")

    if window:
        started = _try_native_window(url)
        if started:
            httpd.serve_forever()
            return 0

    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def _try_native_window(url: str) -> bool:
    """有 pywebview 就开一个真窗口，没有就返回 False 让调用方走浏览器。"""
    try:
        import webview
    except ImportError:
        return False

    def run() -> None:
        webview.create_window("vibe-agent", url, width=1180, height=820)
        webview.start()

    threading.Thread(target=run, daemon=True).start()
    return True
