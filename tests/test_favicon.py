#!/usr/bin/env python3
"""防回归：两个页面都必须有「看得见」的 favicon。

ttyd 自带图标是黑底黑字（平均亮度 42/255），在 Chrome 深色标签栏里等于没图标；
管理页原本压根没有 favicon。这里校验：
1. 图标源文件存在、PNG 尺寸正确、且足够亮/有对比（能在深色标签栏看清）
2. 生成的 ttyd-index.html 用的是我们的图标，且不再残留 ttyd 默认那张
3. 管理页 HTML 内联了 favicon，且 /favicon.ico 路由能出图
"""
from __future__ import annotations

import importlib.util
import struct
import subprocess
import sys
import zlib
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TTYD_DEFAULT_ICON_PREFIX = "iVBORw0KGgoAAAANSUhEUgAAACAAAAAcCAYAAAAAwr0i"  # 32x28 黑底图标


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def decode_png(path: Path) -> tuple[int, int, list[tuple[int, int, int, int]]]:
    raw_file = path.read_bytes()
    pos, idat, ihdr = 8, b"", None
    while pos < len(raw_file):
        ln = struct.unpack(">I", raw_file[pos : pos + 4])[0]
        tag = raw_file[pos + 4 : pos + 8]
        data = raw_file[pos + 8 : pos + 8 + ln]
        if tag == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", data)
        elif tag == b"IDAT":
            idat += data
        pos += 12 + ln
    if not ihdr:
        fail(f"{path} 不是合法 PNG")
    w, h, _depth, ctype = ihdr[0], ihdr[1], ihdr[2], ihdr[3]
    bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    stride = w * bpp
    raw = zlib.decompress(idat)
    out = bytearray()
    prev = bytearray(stride)
    i = 0
    for _ in range(h):
        f = raw[i]
        i += 1
        line = bytearray(raw[i : i + stride])
        i += stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 255
        out += line
        prev = line
    px = [
        (out[p], out[p + 1], out[p + 2], out[p + 3] if bpp == 4 else 255)
        for p in range(0, len(out), bpp)
    ]
    return w, h, px


def luminance(c: tuple[int, int, int, int]) -> float:
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def test_icon_assets() -> None:
    for name in ("term", "manage"):
        svg = ROOT / "web" / f"wt-icon-{name}.svg"
        png = ROOT / "web" / f"wt-icon-{name}-64.png"
        if not svg.exists() or not png.exists():
            fail(f"缺少图标源文件: {svg.name} / {png.name}")
        if "<svg" not in svg.read_text(encoding="utf-8"):
            fail(f"{svg.name} 不是 SVG")
        w, h, px = decode_png(png)
        if (w, h) != (64, 64):
            fail(f"{png.name} 期望 64x64，实际 {w}x{h}")
        vis = [c for c in px if c[3] > 24]
        lums = [luminance(c) for c in vis]
        avg = sum(lums) / len(lums)
        # ttyd 默认图标平均亮度 42；这里要求主体明显更亮，且深浅对比够大
        if avg < 90:
            fail(f"{png.name} 平均亮度仅 {avg:.0f}，深色标签栏里会看不见（需 >= 90）")
        if max(lums) - min(lums) < 80:
            fail(f"{png.name} 明暗对比不足（{max(lums) - min(lums):.0f} < 80）")
        if len(vis) / (w * h) < 0.8:
            fail(f"{png.name} 实心面积过小（{100 * len(vis) / (w * h):.0f}%）")
        main = Counter((c[0] // 32 * 32, c[1] // 32 * 32, c[2] // 32 * 32) for c in vis).most_common(1)
        print(f"OK: 图标 {png.name} 64x64 平均亮度 {avg:.0f} 主色 {main[0][0]}")


def test_ttyd_index_icon() -> None:
    cp = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "patch-ttyd-index.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        fail(f"patch-ttyd-index 失败: {cp.stderr or cp.stdout}")
    html = (ROOT / "web" / "ttyd-index.html").read_text(encoding="utf-8")
    if 'id="wt-favicon"' not in html:
        fail("ttyd-index.html 没有换上自定义 favicon")
    if 'rel="alternate icon"' not in html or "apple-touch-icon" not in html:
        fail("ttyd-index.html 缺少 PNG 兜底图标（Safari/旧浏览器）")
    if TTYD_DEFAULT_ICON_PREFIX in html:
        fail("ttyd 默认黑底 favicon 仍在，深色标签栏里会看不见")
    print("OK: ttyd-index 已换成高对比 favicon")


def test_manage_page_icon() -> None:
    spec = importlib.util.spec_from_file_location("manage_server", ROOT / "bin" / "manage-server.py")
    assert spec and spec.loader
    ms = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ms)
    if "__FAVICON__" in ms.MANAGE_HTML:
        fail("管理页 favicon 占位没被替换")
    if 'id="wt-favicon"' not in ms.MANAGE_HTML or "apple-touch-icon" not in ms.MANAGE_HTML:
        fail("管理页 HTML 缺少 favicon 链接")
    if not ms.favicon_links().startswith("<link"):
        fail("favicon_links() 没产出链接")
    print("OK: 管理页已内联 favicon")


if __name__ == "__main__":
    test_icon_assets()
    test_ttyd_index_icon()
    test_manage_page_icon()
