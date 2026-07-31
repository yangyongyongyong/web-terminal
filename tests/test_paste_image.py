#!/usr/bin/env python3
"""paste-image 落盘与类型校验回归（不依赖真实 osascript 写剪贴板成功）。"""
from __future__ import annotations

import importlib.util
import struct
import subprocess
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
        # 按客户端系统分流键位
        "hookKeys",
        "detectPlatform",
        "decideKeyAction",
        "decidePasteAction",
        "attachCustomKeyEventHandler",
        "navigator.clipboard",
        "wt-paste-overlay",
    ):
        if key not in paste_js:
            fail(f"前端缺少 {key}")

    print("OK: paste-image 落盘与前端脚本")


def test_key_policy_node() -> None:
    """按系统分流的键位/粘贴决策单测：mac 不受影响，Windows 才改键。"""
    script = r"""
const m = require('./web/wt-paste-image.js');
const assert = (c, msg) => { if (!c) { console.error('ASSERT ' + msg); process.exit(1); } };
const kd = (o) => Object.assign({ type: 'keydown', code: 'KeyV' }, o);

// mac 客户端：任何组合都不拦截（⌘V 走原生 paste，Ctrl+V 由 xterm 透传 ）
assert(m.decideKeyAction(kd({ ctrlKey: true }), 'mac') === 'default', 'mac ctrl+v default');
assert(m.decideKeyAction(kd({ altKey: true }), 'mac') === 'default', 'mac alt+v default');
assert(m.decideKeyAction(kd({ metaKey: true }), 'mac') === 'default', 'mac cmd+v default');

// 非 mac：Ctrl+V 走浏览器原生粘贴，Alt+V 贴图，Ctrl+Alt+V 发原始 ^V
assert(m.decideKeyAction(kd({ ctrlKey: true }), 'other') === 'native-paste', 'win ctrl+v');
assert(m.decideKeyAction(kd({ altKey: true }), 'other') === 'image-paste', 'win alt+v');
assert(m.decideKeyAction(kd({ ctrlKey: true, altKey: true }), 'other') === 'raw-ctrl-v', 'win ctrl+alt+v');
assert(m.decideKeyAction(kd({ ctrlKey: true, shiftKey: true }), 'other') === 'default', 'win ctrl+shift+v 不拦截');
assert(m.decideKeyAction(kd({ metaKey: true }), 'other') === 'default', 'win meta+v 不拦截');
assert(m.decideKeyAction(kd({}), 'other') === 'default', '裸 v 不拦截');
assert(m.decideKeyAction(kd({ ctrlKey: true, code: 'KeyC' }), 'other') === 'default', 'ctrl+c 不拦截');
assert(m.decideKeyAction(kd({ ctrlKey: true, type: 'keyup' }), 'other') === 'default', 'keyup 不处理');
assert(m.decideKeyAction(kd({ ctrlKey: true, code: undefined, keyCode: 86 }), 'other') === 'native-paste', 'keyCode 兜底');
assert(m.decideKeyAction(kd({ ctrlKey: true, code: undefined, key: 'V' }), 'other') === 'native-paste', 'key 兜底');

// paste 事件决策
assert(m.decidePasteAction({ platform: 'mac', hasImage: true, hasText: true }) === 'image', 'mac 图优先');
assert(m.decidePasteAction({ platform: 'other', hasImage: true, hasText: true }) === 'text', 'win 文本优先');
assert(m.decidePasteAction({ platform: 'other', hasImage: true, hasText: false }) === 'image', 'win 只有图则贴图');
assert(m.decidePasteAction({ platform: 'other', hasImage: false, hasText: true }) === 'text', '无图必文本');
assert(m.decidePasteAction({ platform: 'mac', hasImage: false, hasText: false }) === 'text', '无图必文本(mac)');
assert(m.decidePasteAction({ platform: 'other', hasImage: true, hasText: true, forceImage: true }) === 'image', '浮层强制贴图');

// 平台识别
assert(m.detectPlatform({ platform: 'MacIntel', userAgent: 'Mozilla/5.0 (Macintosh)' }) === 'mac', 'MacIntel');
assert(m.detectPlatform({ platform: 'Win32', userAgent: 'Mozilla/5.0 (Windows NT 10.0)' }) === 'other', 'Win32');
assert(m.detectPlatform({ platform: 'Linux x86_64', userAgent: 'Mozilla/5.0 (X11; Linux)' }) === 'other', 'Linux');
assert(m.detectPlatform({ platform: '', userAgent: 'Mozilla/5.0 (iPad; CPU OS 17_0)' }) === 'mac', 'iPad UA');
assert(m.detectPlatform({ userAgentData: { platform: 'Windows' }, platform: 'MacIntel' }) === 'other', 'userAgentData 优先');

// 上传选项：只有 mac 的原生粘贴才信任本机剪贴板
assert(m.uploadOptsFor('mac', false).preferNativeClipboard === true, 'mac 保护剪贴板');
assert(m.uploadOptsFor('other', false).forceClipboard === true, 'win 必须写 mac 剪贴板');
assert(m.uploadOptsFor('mac', true).forceClipboard === true, '强制模式写剪贴板');

// 顶栏文案
assert(m.hintText(60, 'mac').indexOf('⌘V') >= 0, 'mac 文案');
assert(m.hintText(60, 'other').indexOf('Alt+V') >= 0, 'win 文案');
assert(m.hintText(60, 'other').indexOf('贴文本') >= 0, 'win 文案含贴文本');

console.log('node key policy ok');
"""
    cp = subprocess.run(["node", "-e", script], cwd=str(ROOT), capture_output=True, text=True)
    if cp.returncode != 0:
        fail(f"node 键位单测失败: {cp.stderr or cp.stdout}")
    print("OK: 按系统分流的键位决策单测")


def test_index_inject_keys() -> None:
    """注入页必须挂上键位钩子，且不再硬编码 mac-only 的提示。"""
    patch = ROOT / "bin" / "patch-ttyd-index.py"
    cp = subprocess.run([sys.executable, str(patch)], cwd=str(ROOT), capture_output=True, text=True)
    if cp.returncode != 0:
        fail(f"patch-ttyd-index 失败: {cp.stderr or cp.stdout}")
    html = (ROOT / "web" / "ttyd-index.html").read_text(encoding="utf-8")
    for needle in ("WtPasteImage.hookKeys", "WtPasteImage.hintText", "decideKeyAction", "wt-paste-overlay"):
        if needle not in html:
            fail(f"ttyd-index.html 缺少注入: {needle}")
    if "Ctrl+V 可贴图" in html.split("<script", 1)[0]:
        fail("顶栏静态文案仍写死 mac-only 的 Ctrl+V 贴图")
    print("OK: ttyd-index 已注入键位钩子")


if __name__ == "__main__":
    main()
    test_key_policy_node()
    test_index_inject_keys()
