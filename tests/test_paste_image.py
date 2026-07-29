#!/usr/bin/env python3
"""paste-image 落盘与类型校验回归（不依赖真实 osascript 写剪贴板成功）。"""
from __future__ import annotations

import importlib.util
import struct
import sys
import tempfile
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("manage_server", ROOT / "bin" / "manage-server.py")
assert spec and spec.loader
ms = importlib.util.module_from_spec(spec)


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


spec.loader.exec_module(ms)

# 1x1 PNG
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def make_png(w: int, h: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(
            ">I", zlib.crc32(tag + data) & 0xFFFFFFFF
        )

    raw = b"".join(b"\x00" + bytes([200, 30, 30]) * w for _ in range(h))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        paste_dir = Path(td)
        big = make_png(40, 40)

        with mock.patch.object(ms, "PASTE_DIR", paste_dir), mock.patch.object(
            ms, "set_macos_clipboard_png", return_value=True
        ), mock.patch.object(ms, "convert_to_png", side_effect=lambda p: p), mock.patch.object(
            ms, "png_dimensions", return_value=(40, 40)
        ):
            path, clip, w, h = ms.save_paste_image(big, "image/png")
            if not path.is_file():
                fail("未写出文件")
            if not clip:
                fail("正常尺寸应写剪贴板")
            if (w, h) != (40, 40):
                fail(f"尺寸应为 40x40，实际 {w}x{h}")

        # 1x1：默认禁止写剪贴板，避免覆盖用户原图
        with mock.patch.object(ms, "PASTE_DIR", paste_dir), mock.patch.object(
            ms, "set_macos_clipboard_png", return_value=True
        ) as set_clip, mock.patch.object(
            ms, "convert_to_png", side_effect=lambda p: p
        ), mock.patch.object(ms, "png_dimensions", return_value=(1, 1)):
            path, clip, w, h = ms.save_paste_image(PNG_1X1, "image/png")
            if clip:
                fail("1x1 不应写剪贴板")
            if set_clip.called:
                fail("1x1 不应调用 set_macos_clipboard_png")

        with mock.patch.object(ms, "PASTE_DIR", paste_dir), mock.patch.object(
            ms, "convert_to_png", side_effect=lambda p: p
        ), mock.patch.object(ms, "png_dimensions", return_value=(0, 0)):
            try:
                ms.save_paste_image(b"", "image/png")
                fail("空图片应报错")
            except ValueError:
                pass
            try:
                ms.save_paste_image(b"not-an-image", "application/octet-stream")
                fail("非图片 MIME 应报错")
            except ValueError:
                pass

    paste_js = (ROOT / "web" / "wt-paste-image.js").read_text(encoding="utf-8")
    for key in (
        "hookPasteImage",
        "/api/paste-image",
        "image_base64",
        r"\x16",
        "pickLargestImage",
        "set_clipboard",
        "MIN_PIXELS",
    ):
        if key not in paste_js:
            fail(f"前端缺少 {key}")

    print("OK: paste-image 落盘与前端脚本")


if __name__ == "__main__":
    main()
