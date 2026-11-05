"""应用图标生成：纯 Python 画一张蜡烛图标记，直接输出 PNG / ICO 字节。

手机 APK 的桌面图标与电脑端程序图标共用这一份画法，保证两端看起来是同一个软件。
不依赖任何图像库，构建工具链删除后仍然可以重新生成。
"""
import math
import struct
import zlib

BG_TOP = (22, 35, 58)
BG_BOT = (13, 24, 41)
UP = (226, 74, 61)
DOWN = (26, 178, 116)
TREND = (76, 141, 255)

# 蜡烛：中心 x、实体上下沿、影线上下沿（单位坐标 0..1）
CANDLES = [
    (0.27, 0.44, 0.66, 0.30, 0.74, UP),
    (0.50, 0.30, 0.52, 0.22, 0.62, DOWN),
    (0.73, 0.26, 0.48, 0.18, 0.58, UP),
]
TREND_POINTS = [(0.20, 0.70), (0.38, 0.55), (0.56, 0.62), (0.80, 0.34)]
BODY_HALF = 0.062
WICK_HALF = 0.016


def _sd_round_rect(px, py, cx, cy, hw, hh, r):
    dx = abs(px - cx) - (hw - r)
    dy = abs(py - cy) - (hh - r)
    ax = max(dx, 0.0)
    ay = max(dy, 0.0)
    return min(max(dx, dy), 0.0) + math.hypot(ax, ay) - r


def _sd_rect(px, py, cx, cy, hw, hh):
    return _sd_round_rect(px, py, cx, cy, hw, hh, 0.0)


def _sd_segment(px, py, ax, ay, bx, by, hw):
    px, py = px - ax, py - ay
    bx, by = bx - ax, by - ay
    denom = bx * bx + by * by
    t = 0.0 if denom == 0 else max(0.0, min(1.0, (px * bx + py * by) / denom))
    return math.hypot(px - t * bx, py - t * by) - hw


def _coverage(sd, aa):
    if sd <= -aa:
        return 1.0
    if sd >= aa:
        return 0.0
    return 0.5 - 0.5 * (sd / aa)


def _over(dst, color, alpha):
    r, g, b, a = color
    a *= alpha
    if a <= 0:
        return dst
    inv = 1.0 - a
    return (
        r * a + dst[0] * inv,
        g * a + dst[1] * inv,
        b * a + dst[2] * inv,
        a + dst[3] * inv,
    )


def render(size):
    """返回 size×size 的 RGBA 字节串。"""
    aa = 0.75 / size
    out = bytearray(size * size * 4)
    for y in range(size):
        py = (y + 0.5) / size
        mix = py
        base = tuple(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * mix for i in range(3)) + (1.0,)
        row = y * size
        for x in range(size):
            px = (x + 0.5) / size
            color = (0.0, 0.0, 0.0, 0.0)
            cover = _coverage(_sd_round_rect(px, py, 0.5, 0.5, 0.5, 0.5, 0.225), aa)
            color = _over(color, base, cover)
            line = 0.92
            for i in range(len(TREND_POINTS) - 1):
                ax, ay = TREND_POINTS[i]
                bx, by = TREND_POINTS[i + 1]
                c = _coverage(_sd_segment(px, py, ax, ay, bx, by, 0.026), aa)
                if c:
                    color = _over(color, TREND + (line,), c)
            for cx, top, bot, wtop, wbot, rgb in CANDLES:
                c = _coverage(_sd_rect(px, py, cx, (wtop + wbot) / 2, WICK_HALF, (wbot - wtop) / 2), aa)
                if c:
                    color = _over(color, rgb + (0.85,), c)
                c = _coverage(_sd_round_rect(px, py, cx, (top + bot) / 2, BODY_HALF, (bot - top) / 2, 0.012), aa)
                if c:
                    color = _over(color, rgb + (1.0,), c)
            i = (row + x) * 4
            out[i] = max(0, min(255, round(color[0])))
            out[i + 1] = max(0, min(255, round(color[1])))
            out[i + 2] = max(0, min(255, round(color[2])))
            out[i + 3] = max(0, min(255, round(color[3] * 255)))
    return bytes(out)


def _chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def png_bytes(size):
    raw = render(size)
    stride = size * 4
    lines = b"".join(b"\x00" + raw[y * stride:(y + 1) * stride] for y in range(size))
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(lines, 9))
            + _chunk(b"IEND", b""))


def ico_bytes(sizes):
    images = [(s, png_bytes(s)) for s in sizes]
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    body = b""
    offset = 6 + 16 * count
    for s, data in images:
        dim = 0 if s >= 256 else s
        body += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return header + body + b"".join(data for _, data in images)


if __name__ == "__main__":
    open("icon-preview.png", "wb").write(png_bytes(256))
    print("icon-preview.png written")
