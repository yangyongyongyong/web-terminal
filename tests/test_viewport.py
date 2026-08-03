#!/usr/bin/env python3
"""移动端底部被系统 UI（Android 地址栏/手势导航条）遮住的修复。

根因：#terminal-container / .xterm 用 calc(100vh - 32px) 定高，100vh 是
「地址栏/手势条完全收起时的最大视口高度」，这两样东西实际展开占用屏幕空间时
并不会让 100vh 跟着变小 —— 容器比真正可见区域更高，底下几行被系统 UI 盖住。

修复：CSS 用 @supports 探测 + 100dvh（动态视口高度，随可见区域实时变化），
保留 100vh 给不支持的老浏览器兜底；再用 wt-viewport.js 监听 visualViewport
变化做一层防御性 kick，逼 ttyd 自带的 resize 处理器重新测量。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIEWPORT_JS = ROOT / "web" / "wt-viewport.js"
INDEX = ROOT / "web" / "ttyd-index.html"
DRIVER = ROOT / "tests" / "browser-keys.mjs"

BROWSER_EXPECTED = {
    "supportsDvhIsBoolean": "boolean",
    "noVisualViewport": "false",
    "flagNotSetYet": "true",
    "hooked": "true",
    "debouncedBurst": "1",
    "scrollTriggers": "1",
    "idempotent": "true vv2Ignored:true",
    "kickResizeDirect": "true fired:true",
}


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def test_source_keywords() -> None:
    if not VIEWPORT_JS.exists():
        fail("缺少 web/wt-viewport.js")
    raw = VIEWPORT_JS.read_text(encoding="utf-8")
    for key in ("hookViewportResizeKick", "supportsDvh", "kickResize", "visualViewport"):
        if key not in raw:
            fail(f"wt-viewport.js 缺少 {key}")
    print("OK: wt-viewport.js 关键逻辑齐全")


def test_index_css_and_inject() -> None:
    cp = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "patch-ttyd-index.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        fail(f"patch-ttyd-index 失败: {cp.stderr or cp.stdout}")
    html = INDEX.read_text(encoding="utf-8")

    # 100vh 兜底必须还在（老浏览器用它）
    if "calc(100vh - 32px)" not in html:
        fail("ttyd-index.html 丢了 100vh 兜底规则")
    # dvh 必须包在 @supports 里，且顺序在 vh 兜底之后（层叠时优先生效）
    supports_at = html.find("@supports (height: 100dvh)")
    if supports_at < 0:
        fail("ttyd-index.html 缺少 @supports (height: 100dvh) 探测")
    vh_at = html.find("calc(100vh - 32px)")
    if not (vh_at < supports_at):
        fail("100vh 兜底规则必须写在 @supports dvh 规则之前，否则层叠顺序会反过来")
    if "calc(100dvh - 32px)" not in html[supports_at:]:
        fail("@supports 块里缺少 100dvh 规则")

    for needle in ("id=\"wt-viewport\"", "WtViewport.hookViewportResizeKick", "hookViewportResizeKick"):
        if needle not in html:
            fail(f"ttyd-index.html 缺少注入: {needle}")
    print("OK: ttyd-index 已注入 dvh 修复与视口 kick")


def test_browser_scenarios() -> None:
    cp = subprocess.run(
        ["node", str(DRIVER), "viewport-harness.html"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    out = (cp.stdout or "").strip()
    if cp.returncode != 0:
        fail(f"浏览器回归执行失败: {out or cp.stderr}")
    if out.startswith("SKIP="):
        print(f"OK: 跳过浏览器视口回归（{out[5:]}）")
        return
    got = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if "ERR" in got:
        fail(f"浏览器内报错: {got['ERR']}")
    for key, want in BROWSER_EXPECTED.items():
        if key not in got:
            fail(f"缺少场景结果 {key}（实际: {out!r}）")
        if got[key] != want:
            fail(f"{key} 期望 {want!r}，实际 {got[key]!r}")
    print(f"OK: 浏览器视口回归 {len(BROWSER_EXPECTED)} 个场景")


if __name__ == "__main__":
    test_source_keywords()
    test_index_css_and_inject()
    test_browser_scenarios()
