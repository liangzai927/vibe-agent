"""图片处理：编码成模型能吃的 data URL、像素级差异对比。

Pillow 是**可选依赖**。导入失败时不抛裸的 ImportError，而是给出可执行的
安装命令——打包成容器后用户看不到 traceback 背后的上下文，提示必须自解释。
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from pathlib import Path

INSTALL_HINT = "安装：pip install pillow（或 pip install -r requirements-browser.txt）"

# 主流多模态模型对长边的容忍度大致在 1568~2048，超过只会被服务端再缩一次
DEFAULT_MAX_EDGE = 1568
DEFAULT_MAX_BYTES = 4_000_000

_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class ImagingUnavailable(RuntimeError):
    """缺少 Pillow。"""

    def __init__(self, message: str = "需要 Pillow 才能做图片处理。", hint: str = INSTALL_HINT) -> None:
        super().__init__(message)
        self.hint = hint


def pillow_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("PIL") is not None


def _image():
    try:
        from PIL import Image
    except ImportError as exc:
        raise ImagingUnavailable() from exc
    return Image


def _image_chops():
    try:
        from PIL import ImageChops
    except ImportError as exc:
        raise ImagingUnavailable() from exc
    return ImageChops


# --------------------------------------------------------------- data URL


def guess_mime(path: Path) -> str:
    return _MIME.get(path.suffix.lower(), "image/png")


def _raw_data_url(path: Path) -> str:
    return f"data:{guess_mime(path)};base64,{base64.b64encode(path.read_bytes()).decode()}"


def image_data_url(
    path: str | Path,
    *,
    max_edge: int = DEFAULT_MAX_EDGE,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str:
    """把图片编码成 data URL，必要时缩放/转码，避免请求体过大。

    没有 Pillow 时退化为原样编码——能不能发出去交给服务端判断，
    而不是在这里硬失败。
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"图片不存在：{path}")

    raw_size = path.stat().st_size
    if not pillow_available():
        return _raw_data_url(path)

    Image = _image()
    with Image.open(path) as img:
        within_edge = max(img.size) <= max_edge
        if within_edge and raw_size <= max_bytes:
            return _raw_data_url(path)

        # 需要处理：转 RGB（JPEG 不支持 alpha），缩放，再按体积逐步降质量
        frame = img.convert("RGB")
        scale = min(1.0, max_edge / max(frame.size))
        if scale < 1.0:
            frame = frame.resize(
                (max(1, int(frame.width * scale)), max(1, int(frame.height * scale))),
                Image.LANCZOS,
            )

        quality = 85
        while True:
            buffer = io.BytesIO()
            frame.save(buffer, format="JPEG", quality=quality, optimize=True)
            if buffer.tell() <= max_bytes or quality <= 40:
                break
            quality -= 15

    return f"data:image/jpeg;base64,{base64.b64encode(buffer.getvalue()).decode()}"


# --------------------------------------------------------------- 像素对比


@dataclass
class DiffResult:
    ratio: float  # 差异像素占比 0~1
    changed: int
    total: int
    mean_delta: float
    size: tuple[int, int]
    resized: bool
    heatmap: Path | None = None

    @property
    def percent(self) -> float:
        return self.ratio * 100

    def summary(self) -> str:
        parts = [
            f"差异像素 {self.percent:.2f}%（{self.changed}/{self.total}）",
            f"平均通道差 {self.mean_delta:.1f}",
            f"基准尺寸 {self.size[0]}x{self.size[1]}",
        ]
        if self.resized:
            parts.append("两张图尺寸不同，已把后者缩放对齐")
        if self.heatmap:
            parts.append(f"差异图 {self.heatmap}")
        return " · ".join(parts)


def pixel_diff(
    baseline: str | Path,
    actual: str | Path,
    *,
    output: str | Path | None = None,
    threshold: int = 16,
    resize: bool = True,
) -> DiffResult:
    """逐像素比较两张图。

    threshold 是单通道差值超过多少才算「不同」——留一点余量是为了避开
    抗锯齿和字体渲染带来的噪声，否则同一个页面两次截图都能报出差异。
    """
    Image = _image()
    ImageChops = _image_chops()

    base_path, actual_path = Path(baseline), Path(actual)
    for path in (base_path, actual_path):
        if not path.is_file():
            raise FileNotFoundError(f"图片不存在：{path}")

    with Image.open(base_path) as handle:
        base = handle.convert("RGB")
    with Image.open(actual_path) as handle:
        other = handle.convert("RGB")

    resized = False
    if base.size != other.size:
        if not resize:
            raise ValueError(
                f"两张图尺寸不同：{base.size[0]}x{base.size[1]} vs "
                f"{other.size[0]}x{other.size[1]}。请用相同视口重新渲染。"
            )
        other = other.resize(base.size, Image.LANCZOS)
        resized = True

    diff = ImageChops.difference(base, other).convert("L")
    histogram = diff.histogram()
    total = base.size[0] * base.size[1]
    changed = sum(histogram[threshold + 1 :])
    ratio = changed / total if total else 0.0
    mean_delta = sum(i * count for i, count in enumerate(histogram)) / total if total else 0.0

    heatmap_path: Path | None = None
    if output:
        heatmap_path = Path(output)
        heatmap_path.parent.mkdir(parents=True, exist_ok=True)
        # 底图压暗，差异像素涂红，一眼能看出「差在哪」
        faded = Image.blend(base.convert("L").convert("RGB"), Image.new("RGB", base.size, (0, 0, 0)), 0.65)
        mask = diff.point(lambda value: 255 if value > threshold else 0)
        highlight = Image.new("RGB", base.size, (255, 48, 48))
        Image.composite(highlight, faded, mask).save(heatmap_path)

    return DiffResult(
        ratio=ratio,
        changed=changed,
        total=total,
        mean_delta=mean_delta,
        size=base.size,
        resized=resized,
        heatmap=heatmap_path,
    )


# --------------------------------------------------------------- 无模型解析


@dataclass
class ImageReport:
    """不借助任何模型，直接从像素里读出来的信息。

    为什么需要它：主模型往往不支持读图，而「配一个多模态模型」对很多人来说
    是额外成本。但设计稿里有一大半信息是**纯像素统计就能拿到**的——
    画布尺寸、主色（hex）、各个区块的位置和大小。这些恰好就是写 CSS 最需要的。
    """

    width: int
    height: int
    image_format: str
    mode: str
    size_bytes: int
    cols: int
    rows: int
    palette: list[tuple[str, float]]  # (hex, 占比)
    grid: list[list[str]]  # 每格的主导色 hex
    background: str
    content_box: tuple[int, int, int, int] | None  # 内容区的像素边界
    samples: int


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _quantize(rgb: tuple[int, int, int], step: int = 16) -> tuple[int, int, int]:
    """把相近的颜色并到一起。抗锯齿和压缩噪声会让同一种颜色散成几十个值。"""
    return tuple(min(255, (channel + step // 2) // step * step) for channel in rgb)  # type: ignore[return-value]


def inspect_image(
    path: str | Path,
    *,
    cols: int = 20,
    rows: int = 12,
    palette_size: int = 8,
) -> ImageReport:
    """纯像素统计：尺寸、主色、粗粒度布局。完全不需要模型。"""
    Image = _image()

    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"图片不存在：{target}")

    cols = max(4, min(int(cols), 80))
    rows = max(3, min(int(rows), 60))

    with Image.open(target) as handle:
        image_format = handle.format or "?"
        mode = handle.mode
        width, height = handle.size
        # BOX 滤波器下采样 = 每格取区域均值，正好是我们要的
        grid_image = handle.convert("RGB").resize((cols, rows), Image.BOX)
        # 用 tobytes 而不是 getdata：后者在 Pillow 14 会被移除
        raw = grid_image.tobytes()

    pixels = [(raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw), 3)]

    cells = [
        [_quantize(pixels[row * cols + col]) for col in range(cols)] for row in range(rows)
    ]

    # 主色按出现次数排序。网格只有几百格，够用了。
    counts: dict[tuple[int, int, int], int] = {}
    for row in cells:
        for color in row:
            counts[color] = counts.get(color, 0) + 1
    total = cols * rows
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    palette = [(_hex(color), count / total) for color, count in ranked[:palette_size]]

    background = _hex(ranked[0][0]) if ranked else "#000000"
    background_rgb = ranked[0][0] if ranked else (0, 0, 0)

    # 内容区：偏离背景色的格子构成的包围盒。用来估卡片的位置和大小。
    content_box = None
    hit_rows = [r for r, row in enumerate(cells) if any(c != background_rgb for c in row)]
    hit_cols = [
        c for c in range(cols) if any(cells[r][c] != background_rgb for r in range(rows))
    ]
    if hit_rows and hit_cols:
        content_box = (
            int(min(hit_cols) / cols * width),
            int(min(hit_rows) / rows * height),
            int((max(hit_cols) + 1) / cols * width),
            int((max(hit_rows) + 1) / rows * height),
        )

    return ImageReport(
        width=width,
        height=height,
        image_format=image_format,
        mode=mode,
        size_bytes=target.stat().st_size,
        cols=cols,
        rows=rows,
        palette=palette,
        grid=[[_hex(c) for c in row] for row in cells],
        background=background,
        content_box=content_box,
        samples=total,
    )
