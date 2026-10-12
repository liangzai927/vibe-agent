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
from dataclasses import dataclass, field
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
from ..refs import expand as expand_refs
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


@dataclass
class SessionRuntime:
    """一个会话的全部运行时状态。

    **为什么要保留多个而不是切换时重建**：撤销栈只在内存里
    （``UndoStack._stack``），重建就等于把 ``/undo`` 历史扔了。
    而撤销是改代码时最需要的东西之一。

    消息和计划在会话文件里，重建能恢复；撤销不行。所以留着。

    代价是内存——每个 agent 持完整消息历史。用 LRU 限制同时活着的数量，
    被挤掉的会话下次切换时重建（丢撤销历史，但那是很久没碰的会话）。
    """

    id: str
    workspace: Workspace
    ui: "WebUI"
    agent: Agent
    store: Any = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    worker: threading.Thread | None = None
    last_used: float = field(default_factory=time.time)

    @property
    def busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()


def _fuzzy_score(query: str, text: str) -> int:
    """子序列匹配的打分。0 表示不匹配。

    连续命中给高分、词边界给加分——这样打 `auth` 时
    `src/auth.ts` 排在 `src/authoring.md` 前面。
    """
    if not query:
        return 1
    if query in text:
        return 1000 - text.index(query)   # 越靠前越好

    score = 0
    position = 0
    streak = 0
    for char in query:
        found = text.find(char, position)
        if found < 0:
            return 0
        streak = streak + 1 if found == position else 0
        score += 10 + streak * 5
        if found == 0 or text[found - 1] in "/-_. ":
            score += 15          # 词边界
        position = found + 1
    return score


def _all_sessions():
    """跨工作区列出所有会话。管理接口要按 id 找到文件。"""
    from ..projects import index_sessions

    return index_sessions()


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
        # 每个会话一套（workspace + ui + agent）。切换会话只是重新指向，
        # 所以撤销栈、计划、打转记录都留着。
        self.runtimes: dict[str, SessionRuntime] = {}
        self._runtime_limit = 5
        self.active_id = ""
        self._llm = llm
        self._auto_approve = auto_approve

        # 还没开会话时的兜底 UI：命令（/help、/models）也得能答
        self._fallback_ui = WebUI(
            workspace,
            self.bus,
            self.gate,
            auto_approve=auto_approve,
            show_thinking=cfg.show_thinking,
        )
        self.ui = self._fallback_ui
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
        self._adopt(self.agent, workspace, session, self.ui)

        self.env_path = env_path or Config.env_file(AGENT_DIR)
        # 和 env_path 一样可以重定向。测试必须能把这些指到临时目录去——
        # 不然「跑一次测试」就会把开发者真实的配置和模型缓存冲掉。
        self.models_dir = models_dir or AGENT_DIR

    # ------------------------------------------------------------- 会话运行时

    def _adopt(
        self,
        agent: Agent,
        workspace: Workspace,
        session: Any,
        ui: "WebUI",
    ) -> SessionRuntime:
        """把一个已建好的 agent 收进运行时表，并设为 active。"""
        session_id = ""
        if session is not None:
            session_id = getattr(session.meta, "id", "") or ""
        if not session_id:
            session_id = f"live-{int(time.time() * 1000)}"

        # 首个会话复用的是构造时那个兜底 UI，它没有会话标识——
        # 补上，否则它发出的事件前端没法归类
        if not getattr(ui, "session_id", ""):
            ui.session_id = session_id

        runtime = SessionRuntime(
            id=session_id,
            workspace=workspace,
            ui=ui,
            agent=agent,
            store=session,
        )
        self.runtimes[session_id] = runtime
        self._activate(session_id)
        return runtime

    def _activate(self, session_id: str) -> None:
        """把 active 指向某个运行时。

        **整个切换就是这几行赋值。** 别处一律用 ``self.agent`` /
        ``self.ui`` / ``self.workspace``，所以不用改任何现有代码。
        """
        runtime = self.runtimes.get(session_id)
        if runtime is None:
            return
        runtime.last_used = time.time()
        self.active_id = session_id
        self.agent = runtime.agent
        self.ui = runtime.ui
        self.workspace = runtime.workspace
        self._evict()

    def _evict(self) -> None:
        """内存里最多留 ``_runtime_limit`` 个会话，挤掉最久没用的。

        被挤掉的会话下次切换时重建——消息和计划从文件恢复，
        **撤销栈会丢**（它只在内存里）。这是刻意的取舍：撤销最有用的是
        「刚刚那几步」，而刚刚用过的会话不会被挤掉。
        """
        if len(self.runtimes) <= self._runtime_limit:
            return
        ordered = sorted(self.runtimes.values(), key=lambda r: r.last_used)
        for runtime in ordered:
            if len(self.runtimes) <= self._runtime_limit:
                break
            if runtime.id == self.active_id or runtime.busy:
                continue
            self.runtimes.pop(runtime.id, None)

    def active(self) -> SessionRuntime | None:
        return self.runtimes.get(self.active_id)

    def open_session(self, session_id: str) -> dict[str, Any]:
        """切到某个会话。已经在内存里就直接指过去，否则从磁盘重建。"""
        session_id = (session_id or "").strip()
        if not session_id:
            return {"ok": False, "error": "没指定会话"}

        if session_id in self.runtimes:
            self._activate(session_id)
            return {"ok": True, "session": self.session_info(), "reused": True}

        from ..projects import index_sessions as _index_sessions

        entry = next((e for e in _index_sessions() if e.id == session_id), None)
        if entry is None:
            return {"ok": False, "error": f"找不到会话 {session_id}"}
        if not entry.workspace:
            return {"ok": False, "error": "这条会话没记工作区，打不开"}

        try:
            workspace = Workspace(
                entry.workspace,
                extra_read_roots=self.cfg.read_dirs,
                read_scope=self.cfg.read_scope,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"工作区打不开：{exc}"}

        try:
            store = SessionStore.open(Path(entry.workspace), session_id)
            _, history, plan = store.load()
        except (OSError, FileNotFoundError) as exc:
            return {"ok": False, "error": f"读不了这条会话：{exc}"}

        ui = WebUI(
            workspace,
            self.bus,
            self.gate,
            session_id=session_id,
            auto_approve=self._auto_approve,
            show_thinking=self.cfg.show_thinking,
        )
        agent = Agent(
            self.cfg,
            ui,
            workspace=workspace,
            llm=self._llm,
            session=store,
            history=history,
            plan=plan,
        )
        self.runtimes[session_id] = SessionRuntime(
            id=session_id, workspace=workspace, ui=ui, agent=agent, store=store
        )
        self._activate(session_id)
        return {"ok": True, "session": self.session_info(), "reused": False}

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
        from ..paths import data_root, describe, scratch_dir

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
        if "default_workspace" in payload:
            from ..projects import Registry

            raw = str(payload["default_workspace"] or "").strip()
            registry = Registry.load()
            if raw:
                target = Path(raw).expanduser()
                if not target.is_dir():
                    return {"ok": False, "error": f"不是有效目录：{target}"}
                registry.set_default(target)
                registry.add(target)
                notes.append(f"默认工作区设为 {target}")
            else:
                registry.set_default("")
                notes.append("默认工作区已恢复成数据目录下的 workspace/")
            changed.append("default_workspace")

        if payload.get("setup_data_repo"):
            from ..data_repo import run as run_setup

            url = str(payload.get("url") or "").strip()
            # 切换前的数据根：工作流会把里面的记忆和会话搬过去
            before_root = data_root()
            outcome = run_setup(url, local_root=before_root)
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

            # 正在用的这条会话也要跟着走，否则它还在往老路径写，
            # 自动同步推的是新克隆——看起来就是「同步成功但会话没进去」
            rebound = self._rebind_session()
            if rebound:
                notes.append(rebound)

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
        """**当前会话**在不在跑。"""
        runtime = self.active()
        return runtime.busy if runtime else False

    def any_busy(self) -> SessionRuntime | None:
        """有没有**任何**会话在跑。有就返回那个运行时。

        按定下的运行模型，同一时刻只跑一个任务。否则两个 agent 可能同时
        改一个项目——出问题的方式比单线程多得多，而且很难查。
        """
        for runtime in self.runtimes.values():
            if runtime.busy:
                return runtime
        return None

    def state(self) -> dict[str, Any]:
        return {
            "busy": self.busy,
            "workspace": str(self.workspace.root),
            "model": self.cfg.model,
            "base_url": self.cfg.base_url,
            "vision_model": self.cfg.vision_model,
            "session": self.agent.session_id or "",
            "session_info": self.session_info(),
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
        running = self.any_busy()
        if running is not None:
            if running.id == self.active_id:
                return False, "上一个任务还在跑，等它结束"
            return False, (
                f"另一个会话（{running.id[:12]}）还在跑。"
                f"同一时刻只跑一个任务——先等它结束，或者切过去看看"
            )
        if text.startswith("/"):
            return self._command(text)

        runtime = self.active()
        if runtime is None:
            return False, "没有打开的会话"

        # `@路径` 引用：在**提交时**展开，把文件内容并进消息。
        # 这样 agent 少两轮往返（先 glob 找、再 read_file 读），又快又省。
        expanded, refs = expand_refs(text, runtime.workspace.root)
        for ref in refs:
            if ref.ok:
                runtime.ui._emit(
                    "note", level="info",
                    text=f"已引用 {ref.path or ref.given}" + (f"（{ref.note}）" if ref.note and not ref.inlined else ""),
                )
            else:
                runtime.ui._emit("note", level="warn", text=ref.note)
        # 界面上显示用户原话，不显示展开后那一大坨
        runtime.ui._emit("user", text=text)
        runtime.worker = threading.Thread(
            target=self._run,
            args=(runtime, expanded, text),
            name=f"va-task-{runtime.id[:12]}",
            daemon=True,
        )
        runtime.worker.start()
        return True, "已开始"

    def _run(self, runtime: SessionRuntime, text: str, display: str = "") -> None:
        """跑一个会话的任务。

        **运行时是参数传进来的**，不读 ``self.active()``——用户可能在任务
        跑到一半切走，那时 ``self.agent`` 已经指向别的会话了，
        再拿它去跑就等于把两个会话的消息搅在一起。
        """
        with runtime.lock:
            try:
                answer = runtime.agent.run(text, display=display or text)
            except Exception as exc:  # noqa: BLE001 - 任何异常都要让界面知道
                runtime.ui._emit(
                    "note", level="error", text=f"{type(exc).__name__}: {exc}"
                )
                answer = ""
            runtime.ui._emit("task_end", answer=answer or "")

            # 回复结束 → 自动同步（后台线程，不阻塞界面）
            self.auto_sync_now()

    @property
    def _worker(self) -> threading.Thread | None:
        """当前会话的 worker。保留这个名字是为了不动 `busy` 等已有代码。"""
        runtime = self.active()
        return runtime.worker if runtime else None

    def history(self) -> list[dict[str, Any]]:
        """把已加载的会话记录转成界面能直接渲染的列表。

        **不做任何解释性加工**——只把内部的消息格式摊平成一条条
        「谁说了什么、调了什么工具、结果是什么」。加工会让历史和
        当初实际发生的事对不上，而历史的价值恰恰在于它忠实。
        """
        out: list[dict[str, Any]] = []
        for message in getattr(self.agent, "messages", []):
            role = message.get("role")
            if role == "system":
                continue
            if role == "user":
                text = message.get("content")
                if isinstance(text, str) and text.strip():
                    out.append({"kind": "user", "text": text})
            elif role == "assistant":
                text = message.get("content")
                if isinstance(text, str) and text.strip():
                    out.append({"kind": "assistant", "text": text})
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    out.append(
                        {
                            "kind": "tool",
                            "name": function.get("name") or "?",
                            "args": function.get("arguments") or "",
                        }
                    )
            elif role == "tool":
                text = message.get("content")
                out.append(
                    {
                        "kind": "result",
                        "text": text if isinstance(text, str) else str(text or ""),
                        "name": message.get("name") or "",
                    }
                )
        return out

    def workspaces(self) -> dict[str, Any]:
        """工作区注册表 + 每个工作区的会话数。

        会话数是现算的（扫目录），不是存的——存了就会和实际对不上。
        """
        from ..projects import Registry, index_sessions

        registry = Registry.load()
        counts: dict[str, int] = {}
        for entry in index_sessions():
            counts[entry.slug] = counts.get(entry.slug, 0) + 1

        rows = []
        for item in registry.all():
            rows.append(
                {
                    "path": item.path,
                    "slug": item.slug,
                    "name": item.name,
                    "sessions": counts.get(item.slug, 0),
                    "exists": item.exists,
                    "pinned": item.pinned,
                    "last_opened": item.last_opened,
                }
            )
        return {
            "workspaces": rows,
            "default": registry.effective_default(),
            "current": str(self.workspace.root),
        }

    def sessions_index(self) -> dict[str, Any]:
        """侧栏用的跨工作区会话索引。"""
        from ..paths import scratch_dir
        from ..projects import GROUP_DEFAULT, GROUP_UNGROUPED, GROUP_WORKSPACE
        from ..projects import group_sessions, index_sessions, relative_time

        entries = index_sessions()
        grouped = group_sessions(entries)

        def shape(items):
            return [
                {
                    "id": e.id,
                    "workspace": e.workspace,
                    "slug": e.slug,
                    "name": Path(e.workspace).name if e.workspace else "",
                    "title": e.title or e.preview or e.id,
                    "preview": e.preview,
                    "when": relative_time(e.updated_at),
                    "updated_at": e.updated_at,
                    "messages": e.message_count,
                    "active": e.id == self.active_session_id(),
                }
                for e in items
            ]

        from ..projects import Registry

        registry = Registry.load()
        known = {item.slug: item for item in registry.all()}

        # 按工作区分组：每个已知工作区一块，各自挂上它的会话
        blocks = []
        for entry in registry.all():
            items = [e for e in grouped.get(GROUP_WORKSPACE, []) if e.slug == entry.slug]
            blocks.append(
                {
                    "kind": GROUP_WORKSPACE,
                    "slug": entry.slug,
                    "name": entry.name,
                    "path": entry.path,
                    "exists": entry.exists,
                    "sessions": shape(items),
                }
            )

        # 注册表里没有、但有会话的工作区（比如换了机器）也要显示出来，
        # 否则那些会话就「消失」了
        seen = set(known)
        extra: dict[str, list] = {}
        for item in grouped.get(GROUP_WORKSPACE, []):
            if item.slug not in seen:
                extra.setdefault(item.slug, []).append(item)
        for slug, items in extra.items():
            blocks.append(
                {
                    "kind": GROUP_WORKSPACE,
                    "slug": slug,
                    "name": Path(items[0].workspace).name or slug,
                    "path": items[0].workspace,
                    "exists": True,
                    "sessions": shape(items),
                }
            )

        if grouped.get(GROUP_DEFAULT):
            blocks.append(
                {
                    "kind": GROUP_DEFAULT,
                    "slug": "default",
                    "name": "默认工作区",
                    "path": registry.effective_default(),
                    "exists": True,
                    "sessions": shape(grouped[GROUP_DEFAULT]),
                }
            )
        if grouped.get(GROUP_UNGROUPED):
            blocks.append(
                {
                    "kind": GROUP_UNGROUPED,
                    "slug": "ungrouped",
                    "name": "未分组",
                    "path": str(scratch_dir()),
                    "exists": True,
                    "sessions": shape(grouped[GROUP_UNGROUPED]),
                }
            )

        return {"blocks": blocks, "total": len(entries)}

    def active_session_id(self) -> str:
        store = getattr(self.agent, "session", None)
        return store.meta.id if store is not None else ""

    # ------------------------------------------------------------- 工作区与会话管理

    def find_files(self, query: str, *, limit: int = 20) -> dict[str, Any]:
        """给 `@` 自动补全用的文件列表。

        模糊匹配而不是前缀匹配——你多半记得「auth」不记得它在哪个目录下。
        匹配的是**相对路径**，所以打 `src/auth` 和打 `auth` 都能找到。
        """
        from ..refs import INLINE_SKIP_DIRS

        query = (query or "").strip().lstrip("@").lower()
        root = Path(self.workspace.root)
        found: list[tuple[int, str]] = []

        try:
            for path in root.rglob("*"):
                if len(found) > limit * 40:
                    break
                try:
                    rel = path.relative_to(root)
                except ValueError:
                    continue
                parts = rel.parts
                if any(part in INLINE_SKIP_DIRS for part in parts):
                    continue
                if any(part.startswith(".") and part not in (".github",) for part in parts[:-1]):
                    continue
                # 目录也列出来——`@src` 引用整个目录是有用的，
                # 而且补全到目录能继续往下点（`@src/` 再补全）
                is_dir = path.is_dir()
                text = str(rel) + ("/" if is_dir else "")
                score = _fuzzy_score(query, str(rel).lower()) if query else 1
                if is_dir and query:
                    # 目录降权，但**只在有关键词时**——空查询下
                    # 1-5 会变成负数被整个滤掉，结果就是「选不到文件夹」。
                    score -= 5
                if score > 0:
                    found.append((score, text, is_dir))
        except OSError:
            pass

        found.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
        return {
            "files": [
                {"path": name, "dir": is_dir} for _, name, is_dir in found[:limit]
            ]
        }

    def add_workspace(self, path: str) -> dict[str, Any]:
        """把一个文件夹加进注册表。

        路径由**系统原生选择框**给（浏览器拿不到真实路径），
        也允许手输——远程会话或者弹不出框的时候得有个退路。
        """
        from ..projects import Registry

        raw = (path or "").strip()
        if not raw:
            return {"ok": False, "error": "没选目录"}

        target = Path(raw).expanduser()
        if not target.is_dir():
            return {"ok": False, "error": f"不是有效目录：{target}"}

        entry = Registry.load().add(target)
        return {"ok": True, "workspace": {"slug": entry.slug, "path": entry.path}}

    def remove_workspace(self, slug: str) -> dict[str, Any]:
        """从注册表移除。**不删数据**——会话和记忆都还在磁盘上。"""
        from ..projects import Registry

        if not Registry.load().remove(slug):
            return {"ok": False, "error": f"注册表里没有 {slug}"}
        return {"ok": True}

    def create_session(
        self, workspace: str = "", title: str = ""
    ) -> dict[str, Any]:
        """新建一个会话。

        **默认建在当前会话的工作区里**——你正在某个项目里，
        点「新会话」自然是这个项目的新会话。想建到别处，先切过去。
        """
        if workspace == "ungrouped":
            # 「未分组」：不绑项目。给它一个真实目录（scratch），
            # 这样沙箱照常生效，不用为它开特例。
            from ..paths import scratch_dir

            target = scratch_dir()
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return {"ok": False, "error": f"建不出 scratch 目录：{exc}"}
        else:
            target = Path(workspace).expanduser() if workspace else Path(self.workspace.root)
        if not target.is_dir():
            return {"ok": False, "error": f"工作区不存在：{target}"}

        try:
            store = SessionStore.create(str(target), self.cfg.model)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"建不出会话：{exc}"}

        if title:
            store.set_title(title)

        try:
            ws = Workspace(
                str(target),
                extra_read_roots=self.cfg.read_dirs,
                read_scope=self.cfg.read_scope,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"工作区打不开：{exc}"}

        ui = WebUI(
            ws,
            self.bus,
            self.gate,
            session_id=store.meta.id,
            auto_approve=self._auto_approve,
            show_thinking=self.cfg.show_thinking,
        )
        agent = Agent(self.cfg, ui, workspace=ws, llm=self._llm, session=store, history=[])
        self.runtimes[store.meta.id] = SessionRuntime(
            id=store.meta.id, workspace=ws, ui=ui, agent=agent, store=store
        )
        self._activate(store.meta.id)
        # 通知界面：新会话来了
        self.bus.publish("session_opened", session=store.meta.id)
        return {"ok": True, "session": self.session_info()}

    def rename_session(self, session_id: str, title: str) -> dict[str, Any]:
        title = (title or "").strip()
        if not title:
            return {"ok": False, "error": "标题不能为空"}
        if len(title) > 80:
            title = title[:80]

        runtime = self.runtimes.get(session_id)
        if runtime is not None and runtime.store is not None:
            runtime.store.set_title(title)
            return {"ok": True, "title": title}

        for entry in _all_sessions():
            if entry.id == session_id:
                try:
                    store = SessionStore.open(Path(entry.workspace), session_id)
                    store.set_title(title)
                except (OSError, FileNotFoundError) as exc:
                    return {"ok": False, "error": f"改名失败：{exc}"}
                return {"ok": True, "title": title}
        return {"ok": False, "error": f"找不到会话 {session_id}"}

    def delete_session(self, session_id: str) -> dict[str, Any]:
        """删掉一个会话文件。

        **正在跑的不让删**——那个线程还持有它，删了会写到半个文件上。
        """
        runtime = self.runtimes.get(session_id)
        if runtime is not None and runtime.busy:
            return {"ok": False, "error": "这个会话正在跑，先等它结束"}

        target: Path | None = None
        if runtime is not None and runtime.store is not None:
            target = runtime.store.path
        else:
            for entry in _all_sessions():
                if entry.id == session_id:
                    target = Path(entry.path)
                    break

        if target is None:
            return {"ok": False, "error": f"找不到会话 {session_id}"}

        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            return {"ok": False, "error": f"删不掉：{exc}"}

        self.runtimes.pop(session_id, None)
        # 删的正好是当前会话：切到随便一条别的，没有就留空
        if session_id == self.active_id:
            remaining = [e for e in _all_sessions() if e.id != session_id]
            if remaining:
                self.open_session(remaining[0].id)
            else:
                self.active_id = ""
        return {"ok": True}

    def storage_report(self) -> list[dict[str, str]]:
        """存储自检：会话写哪、记忆写哪、会不会自动推。

        「配了远端但记录还写在本地」是排查起来最费劲的一类问题——
        配置对、路径算出来也对，只有真去 git 里看才发现什么都没有。
        所以启动时就把这三件事摆出来，别让人猜。
        """
        import os as _os

        from .. import session_sync
        from ..paths import describe
        from ..session import SessionStore

        rows: list[dict[str, str]] = []

        session_dir = SessionStore.default_dir(self.workspace.root)
        remote = (self.cfg.data_remote or "").strip()
        inside_repo = bool(remote) and str(session_dir).startswith(
            str(session_sync.remote_dir(remote, kind="data"))
        )
        rows.append(
            {
                "label": "会话写在",
                "value": describe(session_dir),
                "state": "ok" if (not remote or inside_repo) else "warn",
                "hint": ""
                if (not remote or inside_repo)
                else "配了记忆仓库，但会话没写进去——重启一下就会走仓库",
            }
        )

        if remote:
            rows.append(
                {
                    "label": "记忆仓库",
                    "value": remote,
                    "state": "ok",
                    "hint": "每次回复后自动推送" if self.cfg.auto_sync else "自动推送已关闭",
                }
            )
        else:
            rows.append(
                {
                    "label": "记忆仓库",
                    "value": "未配置（只存本地）",
                    "state": "info",
                    "hint": "想多台机器共享就在设置里配一个",
                }
            )

        # 项目里的老位置如果还留着记录，说明它们没被读也没被推
        legacy = SessionStore.legacy_dir(self.workspace.root)
        try:
            stranded = sorted(legacy.glob("*.jsonl")) if legacy.is_dir() else []
        except OSError:
            stranded = []
        if stranded and remote:
            rows.append(
                {
                    "label": "旧位置的记录",
                    "value": f"{len(stranded)} 条还在 {describe(legacy)}",
                    "state": "warn",
                    "hint": "重启后会自动搬进仓库",
                }
            )

        return rows

    def session_info(self) -> dict[str, Any]:
        """当前会话的概况，界面顶部显示用。"""
        from ..paths import describe
        store = getattr(self.agent, "session", None)
        if store is None:
            return {"saved": False, "id": "", "path": "", "title": "", "count": 0}
        meta = store.meta
        return {
            "saved": True,
            "id": meta.id,
            "path": describe(store.path),
            "title": meta.title or "",
            "count": meta.message_count,
            "created": meta.created_text,
        }

    def _rebind_session(self) -> str:
        """把**正在用的**这条会话挪到当前该在的位置。

        这是「同步成功但会话没进 git」的根因：会话存储在 ``va web`` 启动时
        就建好了，路径只算一次。之后在设置里改了存储位置，环境变量是变了，
        可这个 store 还指着老路径——消息继续写到老地方，推送自然没内容。

        返回一句给用户看的说明（没搬就返回空串）。
        """
        import shutil as _shutil

        from ..session import SessionStore

        store = getattr(self.agent, "session", None)
        if store is None:
            return ""

        target_dir = SessionStore.default_dir(self.workspace.root)
        if store.path.parent == target_dir:
            return ""

        target = target_dir / store.path.name
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if store.path.is_file() and not target.exists():
                _shutil.copy2(store.path, target)
            store.path = target
        except OSError as exc:
            return f"会话没能搬到新位置（{exc}），本次记录仍在旧路径"

        return f"当前会话已挪到 {target}"

    def _relocate(self, before_root: Path, after_root: Path) -> list[str]:
        """把旧位置的数据搬到新位置，并重定向当前会话。"""
        from ..data_repo import migrate

        notes: list[str] = []
        if str(before_root) != str(after_root):
            moved = migrate(before_root, after_root)
            if not moved.ok:
                notes.append(f"迁移没做完：{moved.error}")
            elif moved.copied or moved.skipped:
                notes.append(f"已把之前的内容搬过来：{moved.describe()}")
        rebound = self._rebind_session()
        if rebound:
            notes.append(rebound)
        return notes

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
        if parsed.path == "/api/workspaces":
            self._json(self.app.workspaces())
            return

        if parsed.path == "/api/sessions":
            self._json(self.app.sessions_index())
            return

        if parsed.path == "/api/storage":
            self._json({"rows": self.app.storage_report()})
            return

        if parsed.path == "/api/files":
            params = parse_qs(parsed.query)
            query = (params.get("q") or [""])[0]
            self._json(self.app.find_files(query))
            return

        if parsed.path == "/api/history":
            self._json({
                "messages": self.app.history(),
                "session": self.app.session_info(),
            })
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

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        from urllib.parse import unquote as _unquote

        if parsed.path.startswith("/api/workspaces/"):
            slug = _unquote(parsed.path[len("/api/workspaces/") :].strip("/"))
            result = self.app.remove_workspace(slug)
            self._json(result, 200 if result.get("ok") else 404)
            return

        if parsed.path.startswith("/api/sessions/"):
            session_id = _unquote(parsed.path[len("/api/sessions/") :].strip("/"))
            result = self.app.delete_session(session_id)
            self._json(result, 200 if result.get("ok") else 400)
            return

        self.send_error(404, "not found")

    def do_PATCH(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        from urllib.parse import unquote as _unquote

        if parsed.path.startswith("/api/sessions/"):
            session_id = _unquote(parsed.path[len("/api/sessions/") :].strip("/"))
            payload = self._body()
            result = self.app.rename_session(session_id, str(payload.get("title") or ""))
            self._json(result, 200 if result.get("ok") else 400)
            return

        self.send_error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/workspaces":
            payload = self._body()
            if payload.get("default"):
                result = self.app.update_settings(
                    {"default_workspace": payload.get("path") or ""}
                )
                self._json({"ok": result.get("ok", False), **result})
                return
            result = self.app.add_workspace(str(payload.get("path") or ""))
            self._json(result, 200 if result.get("ok") else 400)
            return

        if parsed.path == "/api/sessions":
            payload = self._body()
            result = self.app.create_session(
                str(payload.get("workspace") or ""), str(payload.get("title") or "")
            )
            self._json(result, 200 if result.get("ok") else 400)
            return

        if parsed.path.startswith("/api/sessions/") and parsed.path.endswith("/open"):
            from urllib.parse import unquote as _unquote

            session_id = _unquote(
                parsed.path[len("/api/sessions/") : -len("/open")].strip("/")
            )
            result = self.app.open_session(session_id)
            self._json(result, 200 if result.get("ok") else 404)
            return


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
