"""浏览器操作原语：点击、输入、等待、断言。

`render_page` 只能渲染截图。要「操作功能测试」就得能点、能输入、能断言。

────────────────────────────────────────────────────────────────

**为什么是一个工具收一串动作，而不是七个工具各干一件事**

一次动作一个工具调用 = 一次模型往返。一个十步的登录流程就是十次往返——
慢，而且每一步都要重新把上下文发给模型。

所以这里收的是**动作数组**：一次启动、一次往返跑完整条流程。

**定位方式优先用 role / text 而不是 CSS**

* 和「验收标准是人话」更接近（「点『保存』按钮」→ role=button, name=保存）
* CSS 类名一改就失效，而角色和文案是产品的一部分，不会随便动

**失败时必须给出足够排查的信息**：哪个动作、定位到了几个元素、
当时的截图、控制台报错。只说「失败了」等于把问题原样退回给人。
"""

from __future__ import annotations

import time
from typing import Any

from .. import browser as browser_mod
from ..util import truncate
from .base import ToolError
from .context import WorkspaceTool

#: 默认视口。和 render_page 一致，方便截图直接做像素对比。
DEFAULT_WIDTH = 1440
DEFAULT_HEIGHT = 900
DEFAULT_TIMEOUT_MS = 15_000

#: 一次最多多少动作。防止模型写出跑不完的长序列。
MAX_ACTIONS = 60

#: 这些动作**不用定位元素**。对它们跑去算「匹配到几个」会得到荒谬的结果——
#: 比如 snapshot 失败时报告「匹配到 12 个元素」，把人引到完全错误的方向。
NO_LOCATOR = frozenset({"goto", "snapshot", "press", "wait_idle"})


def _locator(page, action: dict[str, Any]):
    """把 ``by`` + ``value`` 变成 Playwright 的定位器。

    认不出来就抛错并列出可用的 by——别让模型猜。
    """
    by = str(action.get("by") or "text").strip().lower()
    value = str(action.get("value") or "")
    name = str(action.get("name") or "")

    if by == "role":
        if not value:
            raise ToolError("by=role 时要给 value，例如 value=button、value=textbox")
        return page.get_by_role(value, name=name) if name else page.get_by_role(value)
    if by == "text":
        return page.get_by_text(value, exact=bool(action.get("exact")))
    if by == "label":
        return page.get_by_label(value, exact=bool(action.get("exact")))
    if by == "placeholder":
        return page.get_by_placeholder(value)
    if by == "testid":
        return page.get_by_test_id(value)
    if by == "title":
        return page.get_by_title(value)
    if by == "css":
        return page.locator(value)
    raise ToolError(
        f"不认识的 by={by!r}。可用：role / text / label / placeholder / testid / title / css"
    )


def _describe(action: dict[str, Any]) -> str:
    """给一个动作写一句人话，报错时用。"""
    doing = str(action.get("do") or "?")
    by = action.get("by") or "text"
    value = action.get("value") or action.get("url") or ""
    extra = ""
    if doing == "type":
        extra = f" 输入 {action.get('text', '')!r}"
    elif doing == "select":
        extra = f" 选择 {action.get('option', '')!r}"
    elif doing == "assert":
        bits = []
        if action.get("text") is not None:
            bits.append(f"文本含 {action['text']!r}")
        if action.get("count") is not None:
            bits.append(f"数量 = {action['count']}")
        if action.get("visible") is not None:
            bits.append("可见" if action["visible"] else "不可见")
        extra = " " + "、".join(bits)
    return f"{doing}（{by}={value}）{extra}".strip()


class BrowserRun(WorkspaceTool):
    #: 由 tools/__init__ 注入。有 ctx 时会把功能测试结果记进门禁台账——
    #: 它**就是**功能测试的产生者，让调用方另外再报一次是多余的。
    def __init__(self, workspace, cfg=None, ctx=None) -> None:
        super().__init__(workspace, cfg)
        self.ctx = ctx

    name = "browser_run"
    description = (
        "在浏览器里打开页面并执行一串操作，用于**功能测试**。\n"
        "一次调用跑完整条流程（不是一次一个动作）——十步的流程一次往返搞定。\n"
        "\n"
        "支持的动作（do 的取值）：\n"
        "  goto      打开网址（actions 里的第一个通常是它，或者直接用 url 参数）\n"
        "  click     点击元素\n"
        "  type      往输入框里打字（默认先清空）\n"
        "  select    下拉选择，用 option 给值\n"
        "  press     按键，例如 value=Enter\n"
        "  wait      等元素出现/消失\n"
        "  wait_idle 等网络静默（接口请求都回来了）\n"
        "  assert    断言：text 文本包含、count 元素个数、visible 可见性\n"
        "  snapshot  截图（存到 agent 的 screenshots 目录）\n"
        "\n"
        "定位方式（by 的取值）：role（推荐，用 value=button/ textbox 等加 name）、\n"
        "text、label、placeholder、testid、title、css。\n"
        "\n"
        "失败时会返回：哪个动作失败、匹配到几个元素、当时的截图、控制台报错。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "起始地址。给了就自动当成第一个 goto",
            },
            "actions": {
                "type": "array",
                "description": "要执行的动作序列",
                "items": {
                    "type": "object",
                    "properties": {
                        "do": {
                            "type": "string",
                            "enum": [
                                "goto", "click", "type", "select", "press",
                                "wait", "wait_idle", "assert", "snapshot",
                            ],
                        },
                        "by": {
                            "type": "string",
                            "enum": [
                                "role", "text", "label", "placeholder",
                                "testid", "title", "css",
                            ],
                        },
                        "value": {"type": "string", "description": "定位用的值"},
                        "name": {"type": "string", "description": "by=role 时的可访问名称"},
                        "text": {"type": "string", "description": "type 要输入的内容 / assert 期望的文本"},
                        "option": {"type": "string", "description": "select 选哪个值"},
                        "count": {"type": "integer", "description": "assert 期望的元素个数"},
                        "visible": {"type": "boolean", "description": "assert 期望的可见性"},
                        "state": {
                            "type": "string",
                            "enum": ["visible", "hidden", "attached"],
                            "description": "wait 等什么状态，默认 visible",
                        },
                        "url": {"type": "string", "description": "goto 的地址"},
                        "label": {"type": "string", "description": "snapshot 的文件名"},
                        "timeout": {"type": "integer", "description": "这个动作的超时毫秒"},
                        "clear": {"type": "boolean", "description": "type 前是否清空，默认 true"},
                    },
                    "required": ["do"],
                },
            },
            "width": {"type": "integer", "description": f"视口宽度，默认 {DEFAULT_WIDTH}"},
            "height": {"type": "integer", "description": f"视口高度，默认 {DEFAULT_HEIGHT}"},
            "timeout": {"type": "integer", "description": f"默认超时毫秒，默认 {DEFAULT_TIMEOUT_MS}"},
            "stop_on_error": {
                "type": "boolean",
                "description": "出错就停（默认 true）。设 false 会继续跑完，用于一次看清所有问题",
            },
        },
        "required": ["actions"],
    }
    #: 它会开浏览器、点页面——但不改文件。审批交给 render 那一套的直觉：
    #: 这是只读性质的观察，不弹审批（真要有副作用也是被测系统自己的事）
    requires_approval = False

    def run(self, **kwargs: Any) -> str:
        url = str(kwargs.get("url") or "").strip()
        actions = kwargs.get("actions") or []
        if not isinstance(actions, list) or not actions:
            raise ToolError("actions 不能是空的")
        if len(actions) > MAX_ACTIONS:
            raise ToolError(f"动作太多了（{len(actions)}），一次最多 {MAX_ACTIONS} 个")

        if url:
            actions = [{"do": "goto", "url": url}, *actions]

        width = int(kwargs.get("width") or DEFAULT_WIDTH)
        height = int(kwargs.get("height") or DEFAULT_HEIGHT)
        default_timeout = int(kwargs.get("timeout") or DEFAULT_TIMEOUT_MS)
        stop_on_error = kwargs.get("stop_on_error")
        stop_on_error = True if stop_on_error is None else bool(stop_on_error)

        # detect() 自己会开一个 sync_playwright 实例，而 sync API 不允许重入——
        # 必须在 with entry() 外面先探测好
        info = browser_mod.detect()
        if not info.available:
            raise ToolError(f"{info.error}")

        entry = browser_mod.sync_playwright()
        console: list[str] = []
        page_errors: list[str] = []
        failed_requests: list[str] = []
        results: list[dict[str, Any]] = []
        shots: list[str] = []
        stopped_at = -1

        with entry() as p:
            try:
                browser = browser_mod.launch(p, info=info)
            except browser_mod.BrowserUnavailable as exc:
                raise ToolError(str(exc)) from exc

            try:
                context = browser.new_context(
                    viewport={"width": width, "height": height},
                    ignore_https_errors=True,
                )
                page = context.new_page()
                page.on(
                    "console",
                    lambda msg: console.append(f"[{msg.type}] {msg.text}")
                    if msg.type in ("error", "warning")
                    else None,
                )
                page.on("pageerror", lambda exc: page_errors.append(str(exc)))
                page.on(
                    "requestfailed",
                    lambda req: failed_requests.append(
                        f"{req.url} — {(req.failure or '未知原因')}"
                    ),
                )

                for index, action in enumerate(actions, 1):
                    if not isinstance(action, dict):
                        results.append({"n": index, "ok": False, "what": "?", "error": "动作必须是对象"})
                        if stop_on_error:
                            stopped_at = index
                            break
                        continue

                    try:
                        note = self._perform(page, action, default_timeout, shots)
                        results.append(
                            {"n": index, "ok": True, "what": _describe(action), "note": note}
                        )
                    except Exception as exc:  # noqa: BLE001 - 各种 playwright 异常都要变成可读信息
                        detail = self._explain(page, action, exc)
                        results.append(
                            {
                                "n": index,
                                "ok": False,
                                "what": _describe(action),
                                "error": detail,
                                "locating": str(action.get("do") or "").lower() not in NO_LOCATOR,
                            }
                        )
                        if stop_on_error:
                            stopped_at = index
                            break
            finally:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass

        report = self._report(results, stopped_at, console, page_errors, failed_requests, shots)

        # **有动作失败就抛错**，而不是「成功返回一段写着失败的报告」。
        # 两个理由：
        #   1. 语义上「测试失败」就是失败——把它当成功会让模型以为通过了
        #   2. 后面的测试门禁要能**机器判定**，靠读文本里有没有 ✗ 太脆弱
        # 完整报告原样放进错误信息，信息一点不少。
        self._record(results)

        if any(not item["ok"] for item in results):
            raise ToolError(report)
        return report

    def _record(self, results: list[dict[str, Any]]) -> None:
        """把这次操作的结果记进门禁台账。

        只有**带断言的动作**才算「测试」——一次纯点击不构成验证。
        没有断言的序列不记，免得把「随便点了两下」当成功能测试通过。
        """
        ledger = getattr(self.ctx, "ledger", None) if self.ctx is not None else None
        if ledger is None:
            return
        asserts = [r for r in results if str(r.get("what", "")).startswith("assert")]
        if not asserts:
            return

        from ..gate import fingerprint

        passed = sum(1 for r in asserts if r["ok"])
        failed = len(asserts) - passed
        failures = "；".join(
            f"{r['what']}: {r.get('error', '')[:80]}" for r in asserts if not r["ok"]
        )
        ledger.record(
            "e2e",
            passed=passed,
            failed=failed,
            fingerprint=fingerprint(self.workspace.root),
            detail=failures,
        )

    # ---- 单个动作

    def _perform(
        self,
        page,
        action: dict[str, Any],
        default_timeout: int,
        shots: list[str],
    ) -> str:
        doing = str(action.get("do") or "").strip().lower()
        timeout = int(action.get("timeout") or default_timeout)

        if doing == "goto":
            target = str(action.get("url") or action.get("value") or "").strip()
            if not target:
                raise ToolError("goto 要给 url")
            page.goto(target, timeout=timeout, wait_until="domcontentloaded")
            return f"已打开 {target}"

        if doing == "wait_idle":
            page.wait_for_load_state("networkidle", timeout=timeout)
            return "网络已静默"

        if doing == "press":
            key = str(action.get("value") or "Enter")
            page.keyboard.press(key)
            return f"按了 {key}"

        if doing == "snapshot":
            path = self._shot(page, action.get("label") or "step")
            shots.append(path)
            return f"截图 {path}"

        locator = _locator(page, action)

        if doing == "click":
            locator.first.click(timeout=timeout)
            return "已点击"

        if doing == "type":
            text = str(action.get("text") or "")
            if action.get("clear", True):
                locator.first.fill("", timeout=timeout)
            locator.first.type(text, timeout=timeout)
            return f"输入了 {text!r}"

        if doing == "select":
            option = str(action.get("option") or action.get("option_value") or "")
            locator.first.select_option(option, timeout=timeout)
            return f"选择了 {option!r}"

        if doing == "wait":
            state = str(action.get("state") or "visible")
            locator.first.wait_for(state=state, timeout=timeout)
            return f"等到 {state}"

        if doing == "assert":
            return self._assert(page, locator, action, timeout)

        raise ToolError(f"不认识的动作 do={doing!r}")

    def _assert(self, page, locator, action: dict[str, Any], timeout: int) -> str:
        checks: list[str] = []
        problems: list[str] = []

        if action.get("text") is not None:
            expected = str(action["text"])
            try:
                locator.first.wait_for(state="attached", timeout=timeout)
                content = locator.first.inner_text(timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                raise ToolError(f"等不到这个元素：{exc}") from exc
            if expected in content:
                checks.append(f"文本含 {expected!r}")
            else:
                problems.append(
                    f"期望文本包含 {expected!r}，实际是 {content.strip()[:200]!r}"
                )

        if action.get("count") is not None:
            expected_count = int(action["count"])
            actual = locator.count()
            if actual == expected_count:
                checks.append(f"数量 = {expected_count}")
            else:
                problems.append(f"期望 {expected_count} 个元素，实际 {actual} 个")

        if action.get("visible") is not None:
            want = bool(action["visible"])
            actual_visible = locator.first.is_visible() if locator.count() else False
            if actual_visible == want:
                checks.append("可见" if want else "不可见")
            else:
                problems.append(
                    f"期望{'可见' if want else '不可见'}，实际{'可见' if actual_visible else '不可见'}"
                )

        if not checks and not problems:
            raise ToolError("assert 至少要给 text / count / visible 之一")

        if problems:
            raise ToolError("；".join(problems))
        return "、".join(checks)

    def _shot(self, page, label: str) -> str:
        from ..paths import screenshots_dir

        target_dir = screenshots_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch if ch.isalnum() or ch in "-_中文" else "-" for ch in label)[:40]
        path = target_dir / f"e2e-{safe}-{int(time.time() * 1000) % 100000}.png"
        page.screenshot(path=str(path), full_page=False)
        return str(path)

    # ---- 报错要好用

    def _explain(self, page, action: dict[str, Any], exc: Exception) -> str:
        """把 Playwright 的异常翻译成「能动手改」的信息。

        **最关键的是「匹配到几个元素」**：0 个（定位错了）和 8 个
        （定位太宽）是完全不同的问题，而 Playwright 的原始报错里
        这个信息淹没在一大段里。
        """
        message = str(exc).strip().splitlines()
        head = message[0] if message else type(exc).__name__

        # 断言失败本来就已经是清楚的说明，别再加噪音
        if isinstance(exc, ToolError):
            head = str(exc)

        parts = [head]

        # 只在真的需要定位的动作上做定位诊断
        if str(action.get("do") or "").strip().lower() in NO_LOCATOR:
            return "；".join(parts)

        try:
            locator = _locator(page, action)
            count = locator.count()
            parts.append(f"匹配到 {count} 个元素")
            if count == 0:
                parts.append(
                    "→ 定位不对。先用 snapshot 截图看看实际结构，"
                    "或者放宽定位（例如 by=text 改成 by=role）"
                )
            elif count > 1:
                parts.append(f"→ 定位太宽，命中了 {count} 个。加上 name / 更精确的文本")
        except Exception:  # noqa: BLE001
            pass

        return "；".join(parts)

    def _report(
        self,
        results: list[dict[str, Any]],
        stopped_at: int,
        console: list[str],
        page_errors: list[str],
        failed_requests: list[str],
        shots: list[str],
    ) -> str:
        passed = sum(1 for r in results if r["ok"])
        failed = [r for r in results if not r["ok"]]

        head = f"{passed}/{len(results)} 个动作通过"
        if stopped_at > 0:
            head += f"（第 {stopped_at} 个失败后停止）"

        lines = [head, ""]
        for item in results:
            mark = "✓" if item["ok"] else "✗"
            lines.append(f"  {mark} {item['n']}. {item['what']}")
            if item.get("note"):
                lines.append(f"      {item['note']}")
            if item.get("error"):
                lines.append(f"      {item['error']}")

        if page_errors:
            lines.append("")
            lines.append("页面抛出的异常：")
            lines.extend(f"  {line[:200]}" for line in page_errors[:5])

        if console:
            lines.append("")
            lines.append("控制台：")
            lines.extend(f"  {line[:200]}" for line in console[:8])

        if failed_requests:
            lines.append("")
            lines.append("加载失败的请求：")
            lines.extend(f"  {line[:200]}" for line in failed_requests[:5])

        if shots:
            lines.append("")
            lines.append("截图：")
            lines.extend(f"  {path}" for path in shots)

        if failed:
            lines.append("")
            # 只在**真的用了定位**的动作上才提定位建议。
            # goto 失败时说「看匹配到几个元素」是误导——它压根不定位。
            if any(item.get("locating") for item in failed):
                lines.append(
                    "有动作失败。**先看截图和「匹配到几个元素」**——"
                    "0 个通常是定位写错了，多个通常是定位太宽。"
                )
            else:
                lines.append("有动作失败。看上面的错误信息和加载失败的请求。")

        return truncate("\n".join(lines), limit=8000, label="browser_run 输出")


BROWSER_TOOLS = (BrowserRun,)
