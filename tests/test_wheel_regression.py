#!/usr/bin/env python3
"""防回归：滚轮不得再变成方向键；tmux mouse 保持 off；注入页必须带 swallow 策略。"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMUX_CONF = ROOT / "config" / "tmux.web.conf"
WHEEL_JS = ROOT / "web" / "wt-wheel.js"
INDEX = ROOT / "web" / "ttyd-index.html"
PATCH = ROOT / "bin" / "patch-ttyd-index.py"


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def ok(msg: str) -> None:
    print(f"OK: {msg}")


def test_tmux_mouse_off() -> None:
    text = TMUX_CONF.read_text(encoding="utf-8")
    matches = re.findall(r"^\s*set\s+-g\s+mouse\s+(\w+)", text, flags=re.M)
    if not matches:
        fail("tmux.web.conf 缺少 set -g mouse")
    if matches[-1] != "off":
        fail(f"tmux.web.conf 最终 mouse={matches[-1]}，必须为 off（否则公网滚轮卡顿且行为分裂）")
    ok("tmux mouse off")


def test_tmux_alternate_screen_on() -> None:
    """TUI 必须能用备用屏，否则重连会在普通 buffer 灌屏，看起来像滚动。"""
    text = TMUX_CONF.read_text(encoding="utf-8")
    alt = re.findall(r"^\s*setw\s+-g\s+alternate-screen\s+(\w+)", text, flags=re.M)
    if not alt or alt[-1] != "on":
        fail("tmux alternate-screen 必须为 on（Cursor/Claude TUI 重连否则会刷屏滚动）")
    if "smcup@" in text:
        fail("不应再禁用 smcup/rmcup（会逼 TUI 画在普通 buffer 上）")
    ok("tmux alternate-screen on")


def test_ttyd_scrollback_option() -> None:
    run = (ROOT / "bin" / "run-ttyd.sh").read_text(encoding="utf-8")
    if "scrollback=" not in run or "SCROLLBACK_PAGES" not in run:
        fail("run-ttyd.sh 必须把 SCROLLBACK_PAGES 传给 xterm scrollback")
    ok("ttyd scrollback 配置")


def test_wheel_policy_source() -> None:
    raw = WHEEL_JS.read_text(encoding="utf-8")
    for needle in (
        "decideWheelAction",
        "swallow",
        "passthrough",
        "local",
        "altKey",
        "activeIsAlternate",
        "onSessionConnect",
        "isAtBottom",
    ):
        if needle not in raw:
            fail(f"wt-wheel.js 缺少 {needle}")
    ok("wt-wheel.js 策略关键字齐全")


def test_attach_no_scroll_flood() -> None:
    """禁止 attach 前 capture-pane 灌历史：经 WS 逐行回放会像从顶滚到底。"""
    text = (ROOT / "bin" / "attach-session.sh").read_text(encoding="utf-8")
    if "capture-pane" in text and "replay" in text.lower():
        fail("attach-session.sh 不应再 capture-pane 回放（会造成刷新时刷屏滚动）")
    if "exec" not in text or "attach-session" not in text:
        fail("attach-session.sh 应直接 attach")
    ok("attach 不灌历史刷屏")


def test_decide_wheel_action_node() -> None:
    script = r"""
const w = require('./web/wt-wheel.js');
const assert = (c, m) => { if (!c) { console.error('ASSERT ' + m); process.exit(1); } };

// 回归核心：alternate / 无 scrollback 时必须 swallow，绝不能放行变成方向键
assert(w.decideWheelAction({deltaY: -100, altKey: false}, {activeIsAlternate: true, baseY: 0, viewportY: 0}) === 'swallow', 'alt-buffer swallow');
assert(w.decideWheelAction({deltaY: 100, altKey: false}, {activeIsAlternate: false, baseY: 0, viewportY: 0}) === 'swallow', 'no-history swallow');

// 有 scrollback：本地滚
assert(w.decideWheelAction({deltaY: -40, altKey: false}, {activeIsAlternate: false, baseY: 10, viewportY: 10}) === 'local', 'local scroll');
assert(w.decideWheelAction({deltaY: 40, altKey: false}, {activeIsAlternate: false, baseY: 10, viewportY: 3}) === 'local', 'local scroll mid');
assert(w.decideWheelAction({deltaY: -40, altKey: false}, {activeIsAlternate: false, baseY: 0, viewportY: 0, length: 120, rows: 40}) === 'local', 'length>rows local');

// 鼠标上报开启：交给应用
assert(w.decideWheelAction({deltaY: -40, altKey: false}, {activeIsAlternate: false, baseY: 10, viewportY: 10, mouseTracking: 'vt200'}) === 'passthrough', 'mouse tracking');

// Alt：放行给 TUI
assert(w.decideWheelAction({deltaY: -40, altKey: true}, {activeIsAlternate: true, baseY: 0, viewportY: 0}) === 'passthrough', 'alt passthrough');

console.log('node policy ok');
"""
    cp = subprocess.run(
        ["node", "-e", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        fail(f"node 策略单测失败: {cp.stderr or cp.stdout}")
    ok("decideWheelAction 单测")


def test_index_inject_contains_policy() -> None:
    # 确保按最新脚本生成
    cp = subprocess.run([sys.executable, str(PATCH)], cwd=str(ROOT), capture_output=True, text=True)
    if cp.returncode != 0:
        fail(f"patch-ttyd-index 失败: {cp.stderr or cp.stdout}")
    if not INDEX.exists():
        fail("缺少 ttyd-index.html")
    html = INDEX.read_text(encoding="utf-8")
    for needle in (
        'id="wt-wheel"',
        'id="wt-reconnect"',
        'id="wt-paste-image"',
        "decideWheelAction",
        "swallow",
        "WtWheel.hookLocalWheel",
        "WtPasteImage.hookPasteImage",
        "/api/paste-image",
    ):
        if needle not in html:
            fail(f"ttyd-index.html 缺少注入: {needle}")
    if "wt-paint-mask" in html or "正在恢复画面" in html:
        fail("不应再包含恢复画面遮罩")
    # 旧 bug：无 scrollback 时直接 return 放行 —— 不允许再出现这种模式
    if re.search(r"if\s*\(\s*!canLocalScroll\([^)]*\)\s*\)\s*return\s*;", html):
        fail("检测到旧逻辑：!canLocalScroll 时 return（会再次把滚轮变成方向键）")
    ok("ttyd-index 已注入策略且无遮罩")


def test_live_tmux_mouse_off() -> None:
    cp = subprocess.run(["tmux", "show", "-g", "mouse"], capture_output=True, text=True)
    if cp.returncode != 0:
        ok("当前无 tmux server，跳过 live mouse 检查")
        return
    line = (cp.stdout or "").strip()
    if line != "mouse off":
        fail(f"运行中 tmux {line!r}，期望 mouse off（执行: tmux set -g mouse off）")
    ok("运行中 tmux mouse off")


def main() -> int:
    test_tmux_mouse_off()
    test_tmux_alternate_screen_on()
    test_ttyd_scrollback_option()
    test_attach_no_scroll_flood()
    test_wheel_policy_source()
    test_decide_wheel_action_node()
    test_index_inject_contains_policy()
    test_live_tmux_mouse_off()
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
