"""截图稳定化与样式还原比对。

**为什么必须稳定化**

`pixel_diff` 本身没问题，问题是**喂给它的两张图不可比**：
时间是动的、动画截到中间帧、随机数据每次不同、字体还没加载完就截了。
不处理这些，同一页面连截两次都能差 5%——**那个数字没有任何意义**，
门禁会被它反复误伤，最后你会习惯性地绕过它，等于没有。

处理之后合格线是：**同一页面连截两次，差异 < 0.1%**。
达不到就说明还有没冻住的东西。

────────────────────────────────────────────────────────────────

**比对结果要能指导修改**

「差异 3.2%」没法动手。「差异集中在右上角那块（y 40-180, x 980-1360）」
才能改。所以除了百分比还要给**差异最大的几个区域**。

**尺寸不一致要明说**

设计稿 750×1624（移动端）和渲染图 1440×900（桌面）硬比的结果
毫无意义——那是两张不同的图。宁可报告「尺寸不一致」也不要比出一个百分比。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: 注入页面的稳定化脚本。
#:
#: **四件事**：
#:  1. 冻住时间——`new Date()` 每次都一样，日期/时间戳不再抖
#:  2. 冻住随机——`Math.random` 变成可复现的伪随机
#:  3. 停掉动画和过渡——不然会截到中间帧
#:  4. 停掉光标闪烁——输入框的光标会让那一个像素块每次不同
STABILIZE_JS = r"""
(() => {
  // 1. 时间
  const FROZEN = __FROZEN__;
  const RealDate = Date;
  class FrozenDate extends RealDate {
    constructor(...args) {
      if (args.length === 0) super(FROZEN);
      else super(...args);
    }
    static now() { return FROZEN; }
  }
  window.Date = FrozenDate;

  // 2. 随机 —— **返回常量，不是「固定种子的序列」**
  //
  //    序列看起来更"正确"，但两次截图之间 setInterval 触发的次数不同，
  //    序列走到第几个数就不同，文本还是每次都变（实测噪声 0.11%，
  //    压不到 0.1% 的合格线）。
  //
  //    代价：页面上所有随机数变成同一个值。对**像素比对**来说这是对的
  //    ——要的就是确定性，不是真实性。
  const CONST = __CONST__;
  Math.random = () => CONST;

  // 3. 动画 / 过渡 / 光标
  const style = document.createElement("style");
  style.id = "__va_stabilize";
  style.textContent = `
    *, *::before, *::after {
      animation-duration: 0s !important;
      animation-delay: 0s !important;
      animation-iteration-count: 1 !important;
      transition-duration: 0s !important;
      transition-delay: 0s !important;
      caret-color: transparent !important;
      scroll-behavior: auto !important;
    }
    html { scroll-behavior: auto !important; }
  `;
  document.head.appendChild(style);

  // 4. 记一下，方便排查「到底注入没有」
  window.__vaStabilized = true;
})();
"""


def stabilize(page, *, frozen: str = "2026-01-01T00:00:00", seed: int = 42) -> None:
    """在截图前把页面「冻住」。

    调用时机：页面加载完之后、截图之前。太早的话框架还没渲染，
    太晚的话动画已经跑了一半。
    """
    import time as _time

    try:
        stamp = int(
            _time.mktime(_time.strptime(frozen, "%Y-%m-%dT%H:%M:%S"))
        ) * 1000
    except (ValueError, TypeError):
        stamp = 1767225600000  # 2026-01-01

    # **用 replace 而不是 % 格式化**：注释里出现一个百分号就会把格式化搞崩
    # （我自己就踩了——注释里写了「实测噪声 0.11%」，直接把注入脚本弄坏了）。
    # 没有转义规则，就没有转义陷阱。
    script = (
        STABILIZE_JS.replace("__FROZEN__", str(stamp))
        .replace("__CONST__", repr(((int(seed) or 42) % 1000 + 1) / 1001))
    )
    try:
        page.add_init_script(script)   # 之后的导航也生效
        page.evaluate(script)          # 当前这页立刻生效
    except Exception:  # noqa: BLE001 - 稳定化失败不该让截图失败
        pass


def wait_for_fonts(page, timeout: int = 5000) -> bool:
    """等字体加载完。

    不等的话首次截图会用后备字体——**字形不同，像素差异会很大**，
    而且第二次就「好了」，看起来像是随机波动。
    """
    try:
        page.evaluate(
            """(ms) => Promise.race([
                document.fonts ? document.fonts.ready : Promise.resolve(),
                new Promise((r) => setTimeout(r, ms)),
            ])""",
            timeout,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def settle(page, *, delay_ms: int = 0) -> None:
    """给渲染留一点时间。稳定化之后动画是 0 秒，给一点点就够。"""
    if delay_ms > 0:
        try:
            page.wait_for_timeout(delay_ms)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------- 比对


def compare(
    baseline: str | Path,
    actual: str | Path,
    *,
    threshold: int = 16,
    regions: int = 3,
    grid: int = 8,
) -> dict[str, Any]:
    """逐像素比对。返回一个能直接给人看的结构。

    ``threshold`` 是单通道差值超过多少算不同。默认 16——**故意留了容差**，
    因为抗锯齿和次像素渲染会让边缘有一两个色阶的差别，那不是「样式不对」。
    """
    from PIL import Image, ImageChops

    base = Image.open(baseline).convert("RGB")
    real = Image.open(actual).convert("RGB")

    result: dict[str, Any] = {
        "baseline": f"{base.width}x{base.height}",
        "actual": f"{real.width}x{real.height}",
        "same_size": base.size == real.size,
        "ratio": None,
        "regions": [],
        "note": "",
    }

    if base.size != real.size:
        # **不硬比。** 750x1624 和 1440x900 是两个不同的东西，
        # 比出来的百分比只会误导人。
        result["note"] = (
            f"尺寸不一致：基准 {base.width}×{base.height}，"
            f"实际 {real.width}×{real.height}。**没有比对**——\n"
            f"先让两者一致（调视口、或把设计稿缩放到同尺寸）再比，"
            f"否则那个百分比没有意义。"
        )
        return result

    diff = ImageChops.difference(base, real).convert("L")
    # 单通道容差
    mask = diff.point(lambda value: 255 if value > threshold else 0)
    histogram = mask.histogram()
    changed = histogram[255] if len(histogram) > 255 else 0
    total = base.width * base.height
    result["ratio"] = changed / total if total else 0.0
    result["changed_pixels"] = changed
    result["total_pixels"] = total

    if changed:
        result["regions"] = _top_regions(mask, base.size, count=regions, grid=grid)
    return result


def _top_regions(mask, size: tuple[int, int], *, count: int, grid: int) -> list[dict]:
    """把差异按网格切开，返回差异最密的几块。

    「差异 3.2%」没法动手；「差异集中在右上角 y40-180 x980-1360」才能改。
    """
    width, height = size
    cell_w = max(1, width // grid)
    cell_h = max(1, height // grid)
    pixels = mask.load()

    buckets: list[dict] = []
    for row in range(grid):
        for col in range(grid):
            x0, y0 = col * cell_w, row * cell_h
            x1 = width if col == grid - 1 else x0 + cell_w
            y1 = height if row == grid - 1 else y0 + cell_h
            hits = 0
            # 采样步长：大图上逐像素扫太慢，网格定位不需要那么精细
            step = max(1, min(cell_w, cell_h) // 24)
            for y in range(y0, y1, step):
                for x in range(x0, x1, step):
                    if pixels[x, y]:
                        hits += 1
            if hits:
                buckets.append(
                    {
                        "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0,
                        "hits": hits,
                        "where": _where(row, col, grid),
                    }
                )

    buckets.sort(key=lambda item: item["hits"], reverse=True)
    top = buckets[:count]
    for bucket in top:
        bucket.pop("hits", None)
    return top


def _where(row: int, col: int, grid: int) -> str:
    vertical = "上" if row < grid / 3 else ("中" if row < grid * 2 / 3 else "下")
    horizontal = "左" if col < grid / 3 else ("中" if col < grid * 2 / 3 else "右")
    if vertical == "中" and horizontal == "中":
        return "正中"
    return f"{vertical}{horizontal}"


def describe(result: dict[str, Any], *, target: float = 0.01) -> str:
    """把比对结果说成人话。"""
    if result["ratio"] is None:
        return result["note"]

    ratio = result["ratio"]
    mark = "✓" if ratio <= target else "✗"
    lines = [
        f"{mark} 差异 {ratio * 100:.2f}%（阈值 {target * 100:.2f}%）"
        f"｜{result['changed_pixels']} / {result['total_pixels']} 像素不同"
    ]

    if result["regions"]:
        lines.append("")
        lines.append("差异最大的几块：")
        for region in result["regions"]:
            lines.append(
                f"  {region['where']}：y {region['y']}-{region['y'] + region['h']}，"
                f"x {region['x']}-{region['x'] + region['w']}"
            )
    lines.append("")
    if ratio <= target:
        lines.append("在阈值内。")
    else:
        lines.append(
            "超出阈值。**从上往下改**——先动差异最大的那块，改完再比一次。"
        )
    return "\n".join(lines)


def ratio_from(baseline: str | Path, actual: str | Path, threshold: int = 16) -> float:
    """只要一个数（给门禁用）。尺寸不一致时返回 1.0——**当作完全不像**。"""
    result = compare(baseline, actual, threshold=threshold)
    return 1.0 if result["ratio"] is None else result["ratio"]
