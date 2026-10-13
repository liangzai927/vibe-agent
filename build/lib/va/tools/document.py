"""需求文档读取。

**为什么不能只用 read_file**：产品给的需求文档常常是 Word 或 PDF，那是二进制；
而且几十页的东西没法一次性塞进上下文，硬塞的结果是关键约束被挤掉。

这个工具做两件事：
1. 把各种格式统一抽成带结构的文本（标题转成 Markdown 标题，表格转成文本表）；
2. **先给目录和篇幅，再按需取用**——长文档不会被一次性倒进上下文。

格式支持是分层的：Markdown / 纯文本 / HTML 零依赖，Word 和 PDF 需要
`requirements-docs.txt` 里的包。缺依赖时给出安装命令，而不是抛 ImportError。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..util import truncate
from .base import Tool, ToolError
from .context import WorkspaceTool

MAX_DOCUMENT_BYTES = 50_000_000  # PDF 可能很大，比普通文本文件宽松
MAX_RETURN_CHARS = 20_000
OUTLINE_LIMIT = 60

DOC_HINT = (
    "安装文档解析依赖：pip install -r requirements-docs.txt"
    "（Word 需要 python-docx，PDF 需要 pypdf）"
)

TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".text", ".log", ".csv", ".tsv"}
HTML_SUFFIXES = {".html", ".htm", ".xhtml"}
DOCX_SUFFIXES = {".docx"}
PDF_SUFFIXES = {".pdf"}


@dataclass
class Document:
    path: Path
    kind: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def chars(self) -> int:
        return len(self.text)


class DocumentError(Exception):
    """抽取失败，带可执行的修复建议。"""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


# --------------------------------------------------------------------- 抽取


def _read_plain(path: Path) -> Document:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise DocumentError(f"读取失败：{exc}") from exc
    return Document(path, "文本", text)


def _read_html(path: Path) -> Document:
    raw = path.read_text(encoding="utf-8", errors="replace")
    # 先干掉 script/style，否则正文里全是 JS
    raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)
    # 标题保留成 Markdown 形式，方便后面抽目录
    for level in range(1, 7):
        raw = re.sub(
            rf"(?is)<h{level}[^>]*>(.*?)</h{level}>",
            lambda m, n=level: "\n" + "#" * n + " " + _strip_tags(m.group(1)) + "\n",
            raw,
        )
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?i)</(p|div|li|tr|table|section)>", "\n", raw)
    text = _strip_tags(raw)
    text = html.unescape(text)
    return Document(path, "HTML", _tidy(text))


def _strip_tags(raw: str) -> str:
    return re.sub(r"<[^>]+>", "", raw)


def _read_docx(path: Path) -> Document:
    try:
        import docx  # type: ignore
    except ImportError as exc:
        raise DocumentError("没有安装 python-docx，读不了 .docx", DOC_HINT) from exc

    try:
        document = docx.Document(str(path))
    except Exception as exc:  # noqa: BLE001 - python-docx 抛的异常类型很杂
        raise DocumentError(f"解析 .docx 失败：{exc}") from exc

    parts: list[str] = []
    for para in document.paragraphs:
        text = (para.text or "").strip()
        if not text:
            continue
        style = getattr(getattr(para, "style", None), "name", "") or ""
        heading = re.match(r"Heading\s*(\d+)", style)
        if heading:
            level = min(int(heading.group(1)), 6)
            parts.append("#" * level + " " + text)
        else:
            parts.append(text)

    for index, table in enumerate(document.tables, 1):
        rows: list[str] = []
        for row in table.rows:
            cells = [(cell.text or "").strip().replace("\n", " ") for cell in row.cells]
            rows.append(" | ".join(cells))
        if rows:
            parts.append(f"\n<!-- 表格 {index} -->")
            parts.extend(rows)

    return Document(
        path,
        "Word",
        _tidy("\n\n".join(parts)),
        {"段落": len(document.paragraphs), "表格": len(document.tables)},
    )


def _read_pdf(path: Path) -> Document:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError as exc:
        raise DocumentError("没有安装 pypdf，读不了 .pdf", DOC_HINT) from exc

    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001
        raise DocumentError(f"解析 .pdf 失败：{exc}") from exc

    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception as exc:  # noqa: BLE001
            raise DocumentError("这个 PDF 有密码，解不开", "先去掉密码再读") from exc

    chunks: list[str] = []
    empty_pages = 0
    for number, page in enumerate(reader.pages, 1):
        try:
            content = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - 单页失败不该让整份文档报废
            content = ""
        if not content.strip():
            empty_pages += 1
        chunks.append(f"<!-- 第 {number} 页 -->\n{content.strip()}")

    return Document(
        path,
        "PDF",
        _tidy("\n\n".join(chunks)),
        {
            "页数": len(reader.pages),
            "无法提取文字的页": empty_pages,
        },
    )


HANDLERS: dict[str, Callable[[Path], Document]] = {
    "text": _read_plain,
    "html": _read_html,
    "docx": _read_docx,
    "pdf": _read_pdf,
}


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def detect_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in DOCX_SUFFIXES:
        return "docx"
    if suffix in PDF_SUFFIXES:
        return "pdf"
    if suffix in HTML_SUFFIXES:
        return "html"
    return "text"


# --------------------------------------------------------------------- 结构


@dataclass
class Heading:
    level: int
    title: str
    line: int


def find_headings(text: str) -> list[Heading]:
    found: list[Heading] = []
    for index, line in enumerate(text.splitlines()):
        match = re.match(r"^(#{1,6})\s+(.*\S)\s*$", line)
        if match:
            found.append(Heading(len(match.group(1)), match.group(2), index))
    return found


def outline_text(headings: list[Heading], limit: int = OUTLINE_LIMIT) -> str:
    if not headings:
        return "（这份文档没有可识别的标题结构；可以直接用 offset/limit 分段读）"
    lines = []
    for heading in headings[:limit]:
        lines.append("  " + "  " * (heading.level - 1) + f"{heading.title}")
    if len(headings) > limit:
        lines.append(f"  … 还有 {len(headings) - limit} 个标题")
    return "\n".join(lines)


def slice_section(text: str, headings: list[Heading], keyword: str) -> tuple[str, str] | None:
    """按标题关键词取一节。返回 (标题, 内容) 或 None。"""
    needle = keyword.strip().lower()
    if not needle:
        return None
    for index, heading in enumerate(headings):
        if needle in heading.title.lower():
            start = heading.line + 1
            end = len(text.splitlines())
            for later in headings[index + 1 :]:
                if later.level <= heading.level:
                    end = later.line
                    break
            lines = text.splitlines()[start:end]
            return heading.title, "\n".join(lines).strip()
    return None


class ReadDocument(WorkspaceTool):
    name = "read_document"
    description = (
        "读取需求文档 / 规格说明，支持 Markdown、纯文本、HTML、Word(.docx)、PDF。"
        "长文档不会一次性全给出：默认先返回目录和前一段，"
        "再用 section 按标题取用，或用 offset/limit 按字符区间取用。"
        "拿到需求后先通读并拆成计划，再动手写代码。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文档路径（相对工作区）"},
            "section": {
                "type": "string",
                "description": "按标题取某一节，例如「接口设计」。给了它就用它，忽略 offset/limit",
            },
            "offset": {"type": "integer", "description": "从第几个字符开始（默认 0）"},
            "limit": {"type": "integer", "description": f"最多返回多少字符（默认 {MAX_RETURN_CHARS}）"},
            "outline": {"type": "boolean", "description": "只返回目录和篇幅，不返回正文"},
        },
        "required": ["path"],
    }
    requires_approval = False

    def preview(self, args):
        return self.external_preview(args, "path")

    def needs_approval(self, args):
        """读工作区外的文档要先问。

        需求文档和设计稿一样，经常不在项目里（下载目录、桌面）。
        但文本文件越界的风险比图片高——问一声，把路径摆给用户看。
        """
        return self.is_external(args.get("path"))

    def run(
        self,
        path: str,
        section: str | None = None,
        offset: int = 0,
        limit: int = MAX_RETURN_CHARS,
        outline: bool = False,
        **_: Any,
    ) -> str:
        target = self.resolve_readable(path, what="文档")
        if target.is_dir():
            raise ToolError(f"{path} 是目录，不是文档")

        size = target.stat().st_size
        if size > MAX_DOCUMENT_BYTES:
            raise ToolError(
                f"文档过大（{size / 1e6:.1f}MB）。先拆分成几份，"
                f"或者用 PDF/Word 工具导出成更小的片段。"
            )

        kind = detect_kind(target)
        try:
            document = HANDLERS[kind](target)
        except DocumentError as exc:
            message = str(exc)
            if exc.hint:
                message += "\n" + exc.hint
            raise ToolError(message) from exc

        if kind == "pdf" and document.meta.get("无法提取文字的页"):
            scanned = document.meta["无法提取文字的页"]
            if scanned == document.meta.get("页数"):
                raise ToolError(
                    "这份 PDF 一页文字都没提取出来——很可能是扫描件（图片）。"
                    "需要先做 OCR，或者把页面截图后用 read_design 让视觉模型读。"
                )

        headings = find_headings(document.text)
        header = self._header(document, headings)

        if outline:
            return header + "\n\n目录：\n" + outline_text(headings)

        if section and section.strip():
            found = slice_section(document.text, headings, section)
            if found is None:
                available = "、".join(h.title for h in headings[:10]) or "（无标题结构）"
                raise ToolError(
                    f"在 {path} 里找不到标题包含「{section}」的章节。\n"
                    f"可用标题：{available}\n"
                    f"（用 outline=true 可以看完整目录）"
                )
            title, body = found
            return (
                header
                + f"\n\n── {title} ──\n"
                + truncate(body, limit=max(1, int(limit)), label="章节")
            )

        start = max(0, int(offset))
        span = max(1, int(limit))
        chunk = document.text[start : start + span]
        if not chunk.strip():
            raise ToolError(
                f"offset={start} 已经超过文档长度（共 {document.chars} 字符）。"
            )

        body = f"── 正文（第 {start}-{start + len(chunk)} 字符 / 共 {document.chars}）──\n{chunk}"
        if start + len(chunk) < document.chars:
            body += (
                f"\n\n… 后面还有 {document.chars - start - len(chunk)} 字符。"
                f"继续读用 offset={start + len(chunk)}；"
                f"想按章节取用先看 outline=true 的目录。"
            )
        return header + "\n\n" + body

    # ------------------------------------------------------------- 内部

    def _header(self, document: Document, headings: list[Heading]) -> str:
        lines = [
            f"文档 {self.rel(document.path)}（{document.kind}，{document.path.stat().st_size / 1024:.1f} KB）",
            f"  篇幅  {document.chars:,} 字符 · {len(document.text.splitlines()):,} 行 · {len(headings)} 个标题",
        ]
        for key, value in document.meta.items():
            lines.append(f"  {key}  {value}")
        if len(headings) > 1:
            lines.append(f"  目录  见下方（共 {len(headings)} 节）")
        return "\n".join(lines)


DOCUMENT_TOOLS = (ReadDocument,)
