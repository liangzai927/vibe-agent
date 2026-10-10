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
import os
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..clipboard import default_filename, inbox_dir
from ..paths import project_slug
from ..session import SessionStore
from ..config import Config, replace
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
        self.env_path = env_path or Config.env_file(AGENT_DIR)
        # 和 env_path 一样可以重定向。测试必须能把这些指到临时目录去——
        # 不然「跑一次测试」就会把开发者真实的配置和模型缓存冲掉。
        self.models_dir = models_dir or AGENT_DIR

    # ------------------------------------------------------------- 设置

    def settings(self) -> dict[str, Any]:
        """当前设置。**绝不返回 api_key**——这东西会发到浏览器。"""
        from ..data_repo import describe_steps as data_repo_steps
        from ..memory import stats as memory_stats
        from ..paths import agent_home, data_root, describe

        session_dir = SessionStore.default_dir(self.workspace.root)
        try:
            legacy = (
                SessionStore.legacy_dir(self.workspace.root).resolve()
                == Path(session_dir).resolve()
            )
        except (OSError, ValueError):
            legacy = False

        return {
            "session_dir": str(session_dir),
            "session_dir_display": describe(Path(session_dir)),
            "session_dir_is_legacy": legacy,
            "session_dir_default": describe(
                agent_home() / "sessions" / project_slug(self.workspace.root)
            ),
            "read_dirs": [str(d) for d in self.workspace.extra_read_roots if d != self.workspace.home],
            "read_scope": self.workspace.read_scope,
            "session_remote": self.cfg.session_remote,
            "data_remote": self.cfg.data_remote,
            "auto_sync": self.cfg.auto_sync,
            "data_repo_steps": data_repo_steps(),
            "data_root": describe(data_root()),
            "memory": memory_stats(self.workspace.root),
            "picker": self._picker_status(),
            "home": str(self.workspace.home),
            "home_display": describe(self.workspace.home),
            "vision_model": self.cfg.vision_model,
            "auto_approve": self.ui.auto_approve,
            "show_thinking": self.cfg.show_thinking,
            "env_path": str(self.env_path),
        }

    @staticmethod
    def _picker_status() -> dict[str, Any]:
        from ..dialogs import available

        ok, how = available()
        return {"available": ok, "how": how}

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        """校验 → 写 .env → 热应用能应用的。返回改了什么、哪些要重启。"""
        from ..config import write_env_values

        values: dict[str, str] = {}
        changed: list[str] = []
        restart: list[str] = []
        notes: list[str] = []

        # ---- 会话保存位置 ----
        if "session_dir" in payload:
            raw = str(payload["session_dir"] or "").strip()
            if raw:
                target = Path(raw).expanduser()
                try:
                    target.mkdir(parents=True, exist_ok=True)
                    if not os.access(target, os.W_OK):
                        return {"ok": False, "error": f"目录不可写：{target}"}
                except OSError as exc:
                    return {"ok": False, "error": f"建不了这个目录：{exc}"}
                values["VA_SESSION_DIR"] = raw
                # SessionStore.default_dir 每次都读环境变量，所以新会话立刻生效
                os.environ["VA_SESSION_DIR"] = str(target)
                # 本地目录和远端互斥
                values["VA_SESSION_REMOTE"] = ""
                os.environ.pop("VA_SESSION_REMOTE", None)
                notes.append(f"新会话将保存到 {target}")
            else:
                values["VA_SESSION_DIR"] = ""
                os.environ.pop("VA_SESSION_DIR", None)
                notes.append("已恢复默认位置（项目外）")
            changed.append("session_dir")

        # ---- 初始化记忆仓库：整个工作流在 va/data_repo.py 里，这里只调它 ----
        if payload.get("setup_data_repo"):
            from ..data_repo import run as run_setup

            url = str(payload.get("url") or "").strip()
            outcome = run_setup(url)
            if not outcome.ok:
                # 失败就不写配置——不然会变成「配了远端但数据还在本地」
                return {
                    "ok": False,
                    "error": outcome.describe(),
                    "steps": [
                        {"key": s.key, "title": s.title, "ok": s.ok, "detail": s.detail}
                        for s in outcome.steps
                    ],
                }

            values["VA_DATA_REMOTE"] = url
            os.environ["VA_DATA_REMOTE"] = url
            for key in ("VA_SESSION_REMOTE", "VA_SESSION_DIR"):
                values[key] = ""
                os.environ.pop(key, None)
            # 初始化成功后默认打开自动同步——用户要的就是「以后每次都自动存」
            values["VA_AUTO_SYNC"] = "1"
            os.environ["VA_AUTO_SYNC"] = "1"
            self.cfg.auto_sync = True
            self.cfg.data_remote = url
            changed.extend(["data_remote", "auto_sync"])
            # **必须先落盘再返回**：这个分支以前直接 return，
            # 跳过了后面统一的写 .env 步骤——结果是仓库初始化成功、
            # 配置却没保存，重启后 data_remote 又是空的，自动同步永远不触发。
            try:
                write_env_values(self.env_path, values)
            except OSError as exc:
                return {"ok": False, "error": f"仓库初始化好了，但配置写不进 {self.env_path}：{exc}"}

            return {
                "ok": True,
                "message": outcome.message,
                "steps": [
                    {"key": s.key, "title": s.title, "ok": s.ok, "detail": s.detail}
                    for s in outcome.steps
                ],
                "settings": self.settings(),
            }

        if "auto_sync" in payload:
            on = bool(payload["auto_sync"])
            values["VA_AUTO_SYNC"] = "1" if on else "0"
            os.environ["VA_AUTO_SYNC"] = "1" if on else "0"
            self.cfg.auto_sync = on
            changed.append("auto_sync")

        # ---- 数据仓库（一个仓库放全部数据）----
        if "data_remote" in payload:
            from ..session_sync import ensure_clone

            remote = str(payload["data_remote"] or "").strip()
            if remote:
                ready = ensure_clone(remote, kind="data")
                if not ready.ok:
                    # 克隆失败就不写进配置，否则会出现「配了远端但数据还在本地」
                    return {"ok": False, "error": f"连不上这个 git 仓库：{ready.detail}"}
                values["VA_DATA_REMOTE"] = remote
                os.environ["VA_DATA_REMOTE"] = remote
                # 数据仓库是「全都放这儿」，把单独指定的那几个清掉，免得打架
                for key in ("VA_SESSION_REMOTE", "VA_SESSION_DIR"):
                    values[key] = ""
                    os.environ.pop(key, None)
                notes.append(
                    f"记忆和所有项目的会话都会进 {remote}，按项目分目录。"
                )
            else:
                values["VA_DATA_REMOTE"] = ""
                os.environ.pop("VA_DATA_REMOTE", None)
                notes.append("已改回只存本地")
            changed.append("data_remote")

        # ---- 会话远端（单独给会话一个仓库，进阶用法）----
        if "session_remote" in payload:
            from ..session_sync import ensure_clone

            remote = str(payload["session_remote"] or "").strip()
            if remote:
                ready = ensure_clone(remote)
                if not ready.ok:
                    # 克隆失败就**不要**写进配置。否则会出现「我明明配了远端，
                    # 记录却还在本地」这种最难查的状态。
                    return {
                        "ok": False,
                        "error": f"连不上这个 git 仓库：{ready.detail}",
                    }
                values["VA_SESSION_REMOTE"] = remote
                os.environ["VA_SESSION_REMOTE"] = remote
                # 远端和本地目录是互斥的，写远端时把本地那个清掉，免得下次
                # 让人以为配的是本地
                values["VA_SESSION_DIR"] = ""
                os.environ.pop("VA_SESSION_DIR", None)
                notes.append(f"会话将同步到 {remote}")
            else:
                values["VA_SESSION_REMOTE"] = ""
                os.environ.pop("VA_SESSION_REMOTE", None)
                notes.append("已改回只存本地")
            changed.append("session_remote")

        # ---- 读取范围 ----
        if "read_scope" in payload:
            from ..workspace import READ_SCOPES

            scope = str(payload["read_scope"] or "").strip().lower()
            if scope not in READ_SCOPES:
                return {"ok": False, "error": f"未知的读取范围：{scope}"}
            values["VA_READ_SCOPE"] = scope
            changed.append("read_scope")
            restart.append("读取范围（工作区已经建好了，要重启才认）")

        # ---- 外部可读目录 ----
        if "read_dirs" in payload:
            entries = [
                str(item).strip()
                for item in (payload["read_dirs"] or [])
                if str(item).strip()
            ]
            for entry in entries:
                resolved = Path(entry).expanduser()
                if not resolved.is_dir():
                    return {"ok": False, "error": f"不是有效目录：{entry}"}
            values["VA_READ_DIRS"] = os.pathsep.join(entries)
            changed.append("read_dirs")
            restart.append("可读目录（工作区已经建好了，要重启才认）")

        # ---- 视觉模型：能热生效 ----
        if "vision_model" in payload:
            vision = str(payload["vision_model"] or "").strip()
            values["VA_VISION_MODEL"] = vision
            os.environ["VA_VISION_MODEL"] = vision
            self.cfg = replace(self.cfg, vision_model=vision)
            self.agent.cfg = self.cfg
            if hasattr(self.agent, "agent_ctx"):
                self.agent.agent_ctx.cfg = self.cfg
            changed.append("vision_model")

        # ---- 自动批准 / 思考过程：能热生效 ----
        if "auto_approve" in payload:
            flag = bool(payload["auto_approve"])
            values["VA_AUTO_APPROVE"] = "1" if flag else "0"
            os.environ["VA_AUTO_APPROVE"] = "1" if flag else "0"
            self.ui.auto_approve = flag
            changed.append("auto_approve")

        if "show_thinking" in payload:
            flag = bool(payload["show_thinking"])
            values["VA_SHOW_THINKING"] = "1" if flag else "0"
            os.environ["VA_SHOW_THINKING"] = "1" if flag else "0"
            self.cfg = replace(self.cfg, show_thinking=flag)
            self.ui.show_thinking = flag
            changed.append("show_thinking")

        if values:
            try:
                write_env_values(self.env_path, values)
            except OSError as exc:
                return {"ok": False, "error": f"写不进 {self.env_path}：{exc}"}

        self.bus.publish("settings", changed=changed, restart=restart, notes=notes)
        return {"ok": True, "changed": changed, "restart": restart, "notes": notes}

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

            # 回复结束 → 自动同步（后台线程，不阻塞界面）
            self.auto_sync_now()

    def auto_sync_now(self) -> None:
        """回复结束后自动同步一次。

        放在**后台线程**里：推送可能要几秒，不能让用户干等。
        失败只记一条提示，不打断对话——同步失败不该影响正在做的事。
        """
        from ..data_repo import auto_sync_enabled, sync_now

        if not auto_sync_enabled(self.cfg):
            return

        def work() -> None:
            try:
                result = sync_now(self.cfg)
                if result.ok:
                    self.bus.publish("note", level="info", text="已自动同步到记忆仓库")
                else:
                    self.bus.publish(
                        "note",
                        level="warn",
                        text=f"自动同步失败（不影响使用）：{result.describe()}",
                    )
            except Exception as exc:  # noqa: BLE001
                self.bus.publish("note", level="warn", text=f"自动同步出错：{exc}")

        threading.Thread(target=work, daemon=True).start()

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
        if parsed.path == "/api/settings":
            self._json(self.app.settings())
            return

        if parsed.path == "/api/pick-folder":
            # 浏览器给不出真实路径，所以由服务端弹系统原生选择框
            from ..dialogs import pick_folder

            params = parse_qs(parsed.query)
            prompt = (params.get("prompt") or ["选择文件夹"])[0]
            result = pick_folder(prompt)
            self._json(
                {
                    "ok": result.ok,
                    "path": result.path,
                    "cancelled": result.cancelled,
                    "error": result.error,
                }
            )
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

        self.app.bus.subscribe()
        try:
            # 超时也是「多久发现浏览器没了」的上限：断开只有在下次写入时
            # 才会暴露。5 秒对本地服务来说代价可以忽略，但断线感知快得多——
            # 这直接决定了「关掉标签页后 agent 还要空转多久」。
            while True:
                events = self.app.bus.wait(since, timeout=5)
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
        finally:
            # 最后一个人走了：把还挂着的审批按拒绝处理。
            # 不这么做的话 worker 会干等到超时，界面上表现为
            # 「一直显示运行中，发什么都说上一个任务还在跑」。
            if self.app.bus.unsubscribe() == 0:
                cancelled = self.app.gate.cancel_all("no")
                if cancelled:
                    self.app.bus.publish(
                        "note",
                        level="warn",
                        text=f"浏览器断开了，{cancelled} 个待审批已按拒绝处理",
                    )

    # ----------------------------------------------------------- POST

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        if parsed.path == "/api/message":
            payload = self._body()
            ok, message = self.app.submit(str(payload.get("text", "")))
            self._json({"ok": ok, "message": message}, 200 if ok else 409)
            return

        if parsed.path == "/api/settings":
            payload = self._body()
            result = self.app.update_settings(payload)
            self._json(result, 200 if result.get("ok") else 400)
            return

        if parsed.path == "/api/session-sync":
            # 推送是外部可见的动作，所以只在明确点「同步」时才做
            from ..session_sync import sync

            if self.app.cfg.data_remote:
                result = sync(self.app.cfg.data_remote, kind="data")
            else:
                result = sync(self.app.cfg.session_remote)
            self._json({"ok": result.ok, "message": result.describe()})
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
