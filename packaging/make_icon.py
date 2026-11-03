"""生成打包用的图标 ``packaging/sisp2.ico``（纯标准库，无需 Pillow）。

图案：深色圆角方块底 + 天蓝色盘片（外环 + 中心轴），识别度够、纯几何形状
不依赖字体。运行 ``python packaging/make_icon.py`` 即可覆盖重建。
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

SIZE = 256
SUPERSAMPLE = 4

BG = (31, 41, 55)  # #1f2937
RING = (56, 189, 248)  # #38bdf8
HUB = (226, 232, 240)  # #e2e8f0


def _rounded_rect(x: float, y: float, radius: float) -> float:
    """圆角方块的有符号距离：<=0 表示在形状内部。"""
    cx = cy = SIZE / 2
    dx = abs(x - cx) - (SIZE / 2 - radius)
    dy = abs(y - cy) - (SIZE / 2 - radius)
    ox, oy = max(dx, 0.0), max(dy, 0.0)
    return math.hypot(ox, oy) + min(max(dx, dy), 0.0) - radius


def _ring(x: float, y: float, r_out: float, r_in: float) -> float:
    d = math.hypot(x - SIZE / 2, y - SIZE / 2)
    return max(r_in - d, d - r_out)


def _color_at(x: float, y: float) -> tuple[float, float, float, float]:
    """返回 (r, g, b, a)，a 为 0..1 覆盖率，用于边界抗锯齿。"""
    if _rounded_rect(x, y, 44.0) > 0:
        return (0, 0, 0, 0.0)
    if math.hypot(x - SIZE / 2, y - SIZE / 2) <= 13.0:
        return (*HUB, 1.0)
    if _ring(x, y, 92.0, 68.0) <= 0:
        return (*RING, 1.0)
    return (*BG, 1.0)


def render() -> bytes:
    """渲染 BGRA 像素（行优先，从上到下）。"""
    pixels = bytearray()
    step = 1.0 / SUPERSAMPLE
    offsets = [(i + 0.5) * step for i in range(SUPERSAMPLE)]
    for py in range(SIZE):
        for px in range(SIZE):
            r = g = b = a = 0.0
            for oy in offsets:
                for ox in offsets:
                    cr, cg, cb, ca = _color_at(px + ox, py + oy)
                    r += cr * ca
                    g += cg * ca
                    b += cb * ca
                    a += ca
            samples = SUPERSAMPLE * SUPERSAMPLE
            if a <= 0:
                pixels += b"\x00\x00\x00\x00"
                continue
            pixels += bytes(
                (
                    min(255, round(b / a)),
                    min(255, round(g / a)),
                    min(255, round(r / a)),
                    min(255, round(a / samples * 255)),
                )
            )
    return bytes(pixels)


def build_ico(path: Path) -> Path:
    bgra = render()
    # ICO 里的 DIB 高度要写两倍（像素 + AND 掩码），掩码可以全 0
    mask_row = (SIZE + 31) // 32 * 4
    bitmap = bytearray()
    for row in range(SIZE - 1, -1, -1):  # BMP 自底向上
        bitmap += bgra[row * SIZE * 4 : (row + 1) * SIZE * 4]
    bitmap += b"\x00" * (mask_row * SIZE)

    header = struct.pack("<IiiHHIIiiII", 40, SIZE, SIZE * 2, 1, 32, 0, len(bitmap), 0, 0, 0, 0)
    dib = header + bitmap
    entry = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(dib), 22)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<HHH", 0, 1, 1) + entry + dib)
    return path


if __name__ == "__main__":
    target = build_ico(Path(__file__).with_name("sisp2.ico"))
    print(f"图标已生成：{target}（{target.stat().st_size} 字节）")
