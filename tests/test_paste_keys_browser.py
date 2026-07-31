#!/usr/bin/env python3
"""真浏览器回归：按客户端系统分流的粘贴键位。

用本机 headless Chromium（CDP）跑 tests/fixtures/keys-harness.html，验证：
- 非 mac：Ctrl+V 必须放给浏览器原生粘贴（不能再发 \\x16 到 PTY）
- 非 mac：Alt+V 贴图；剪贴板 API 不可用时出引导浮层
- 非 mac：原生粘贴里「图+文」贴文本、「只有图」兜底贴图
- mac：键位与粘贴行为保持原样（⌘V 图优先，Ctrl+V/Alt+V 不拦截）
没装浏览器时自动跳过。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRIVER = ROOT / "tests" / "browser-keys.mjs"

EXPECTED = {
    "platform": "other",
    "hookKeys": "true",
    # ret:false = 让 xterm 早退；pd:false = 不 preventDefault；toPTY:0 = 没发控制字符
    "winCtrlV": "ret:false pd:false toPTY:0",
    "winCtrlAltV": 'ret:false pd:true toPTY:["\\u0016"]',
    "winAltV": 'pd:true posts:1 set_clipboard:true toPTY:["\\u0016"]',
    "winAltVoverlay": "true",
    "overlayPaste": 'prevented:true posts:1 closed:true toPTY:["\\u0016"]',
    "overlayNoImage": "prevented:true posts:0 closed:true status:剪贴板里没有图片",
    "winPasteImageAndText": "prevented:false posts:0",
    "winPasteImageOnly": "prevented:true posts:1 set_clipboard:true",
    "platform2": "mac",
    "macKeys": "ctrlV:true/pd:false altV:true/pd:false toPTY:0",
    "macPasteImageAndText": "prevented:true posts:1 set_clipboard:true",
    "macPasteTextOnly": "prevented:false posts:0",
}


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    cp = subprocess.run(
        ["node", str(DRIVER)], cwd=str(ROOT), capture_output=True, text=True, timeout=120
    )
    out = (cp.stdout or "").strip()
    if cp.returncode != 0:
        fail(f"浏览器回归执行失败: {out or cp.stderr}")
    if out.startswith("SKIP="):
        print(f"OK: 跳过浏览器键位回归（{out[5:]}）")
        return
    got = dict(
        line.split("=", 1) for line in out.splitlines() if "=" in line and not line.startswith("[")
    )
    if "ERR" in got:
        fail(f"浏览器内报错: {got['ERR']}")
    for key, want in EXPECTED.items():
        if key not in got:
            fail(f"缺少场景结果 {key}（实际输出: {out!r}）")
        if got[key] != want:
            fail(f"{key} 期望 {want!r}，实际 {got[key]!r}")
    print(f"OK: 浏览器键位回归 {len(EXPECTED)} 个场景")


if __name__ == "__main__":
    main()
