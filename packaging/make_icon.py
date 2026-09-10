# -*- coding: utf-8 -*-
"""
生成应用图标 app.ico —— 纯标准库实现，无需 Pillow。

图形：深色圆角方块 + 三根红绿 K 线（上升趋势），
      契合「美股期权策略推荐器」的交易属性。
用法：python make_icon.py [输出路径]
"""
from __future__ import annotations

import os
import struct
import sys

# ---- 配色 ----
BG_TOP = (30, 41, 59)       # #1e293b
BG_BOTTOM = (15, 23, 42)    # #0f172a
RED = (239, 68, 68)         # #ef4444  涨（中式配色）
GREEN = (34, 197, 94)       # #22c55e  跌
BASE = 1024                 # 基准渲染分辨率

# (中心x, 实体上, 实体下, 影线上, 影线下, 颜色)
CANDLES = [
    (0.295, 0.470, 0.700, 0.395, 0.775, RED),
    (0.500, 0.330, 0.570, 0.255, 0.650, GREEN),
    (0.705, 0.215, 0.415, 0.140, 0.500, RED),
]
BODY_W = 0.135
WICK_W = 0.030


def _in_round_rect(u: float, v: float, x0: float, y0: float,
                   x1: float, y1: float, r: float) -> bool:
    if not (x0 <= u <= x1 and y0 <= v <= y1):
        return False
    if (x0 + r) <= u <= (x1 - r) or (y0 + r) <= v <= (y1 - r):
        return True
    cx = x0 + r if u < x0 + r else x1 - r
    cy = y0 + r if v < y0 + r else y1 - r
    return (u - cx) ** 2 + (v - cy) ** 2 <= r * r


def render_base(n: int = BASE) -> bytearray:
    """渲染基准位图（RGBA，自上而下）。"""
    px = bytearray(n * n * 4)
    half_body = BODY_W / 2.0
    half_wick = WICK_W / 2.0
    for j in range(n):
        v = (j + 0.5) / n
        row = j * n * 4
        for i in range(n):
            u = (i + 0.5) / n
            k = row + i * 4
            if not _in_round_rect(u, v, 0.0, 0.0, 1.0, 1.0, 0.225):
                continue
            # 垂直渐变背景
            r = int(BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * v)
            g = int(BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * v)
            b = int(BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * v)
            # 叠加 K 线
            for cx, bt, bb, wt, wb, col in CANDLES:
                if abs(u - cx) <= half_body and bt <= v <= bb:
                    r, g, b = col
                elif abs(u - cx) <= half_wick and wt <= v <= wb:
                    r, g, b = col
            px[k] = r
            px[k + 1] = g
            px[k + 2] = b
            px[k + 3] = 255
    return px


def downsample(src: bytearray, src_n: int, size: int) -> bytearray:
    """面积平均降采样（支持任意比例）。"""
    out = bytearray(size * size * 4)
    scale = src_n / size
    for j in range(size):
        y0 = int(j * scale)
        y1 = max(y0 + 1, int((j + 1) * scale))
        for i in range(size):
            x0 = int(i * scale)
            x1 = max(x0 + 1, int((i + 1) * scale))
            sr = sg = sb = sa = 0
            cnt = 0
            for yy in range(y0, min(y1, src_n)):
                base = yy * src_n * 4
                for xx in range(x0, min(x1, src_n)):
                    k = base + xx * 4
                    sr += src[k]
                    sg += src[k + 1]
                    sb += src[k + 2]
                    sa += src[k + 3]
                    cnt += 1
            if cnt:
                k2 = (j * size + i) * 4
                out[k2] = sr // cnt
                out[k2 + 1] = sg // cnt
                out[k2 + 2] = sb // cnt
                out[k2 + 3] = sa // cnt
    return out


def write_ico(path: str, images: list[tuple[int, bytearray]]) -> None:
    """写出 ICO（32bpp BGRA + 1bpp AND 掩码）。"""
    n = len(images)
    entries = b""
    blob = b""
    offset = 6 + 16 * n
    for size, buf in images:
        bih = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                          len(buf), 0, 0, 0, 0)
        # XOR 位图：自下而上、BGRA
        xor = bytearray()
        for j in range(size - 1, -1, -1):
            base = j * size * 4
            for i in range(size):
                k = base + i * 4
                xor += bytes((buf[k + 2], buf[k + 1], buf[k], buf[k + 3]))
        # AND 掩码：1bpp，每行 4 字节对齐
        stride = ((size + 31) // 32) * 4
        mask = bytearray(stride * size)
        for j in range(size - 1, -1, -1):
            base = j * size * 4
            mrow = (size - 1 - j) * stride
            for i in range(size):
                if buf[base + i * 4 + 3] < 128:
                    mask[mrow + (i >> 3)] |= 0x80 >> (i & 7)
        img = bih + bytes(xor) + bytes(mask)
        w = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(img), offset)
        offset += len(img)
        blob += img
    with open(path, "wb") as f:
        f.write(struct.pack("<HHH", 0, 1, n) + entries + blob)


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "app.ico")

    print(f"[1/3] 渲染基准位图 {BASE}x{BASE} …")
    base = render_base(BASE)

    print("[2/3] 降采样多尺寸 …")
    sizes = [256, 128, 64, 48, 32, 16]
    images: list[tuple[int, bytearray]] = []
    cache: dict[int, bytearray] = {BASE: base}
    for s in sizes:
        images.append((s, downsample(base, BASE, s)))
        print(f"      {s}x{s}")

    print(f"[3/3] 写出 ICO → {out}")
    write_ico(out, images)
    print(f"完成，文件大小 {os.path.getsize(out)} 字节")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
