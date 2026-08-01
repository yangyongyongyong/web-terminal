#!/usr/bin/env python3
"""选中即复制：纯函数单测 + 注入校验 + 真浏览器场景回归。

覆盖点：
- 拖选过程只复制一次（防抖），同一段选中不重复提示，清空后可再次复制
- 纯空白选中不动剪贴板；超大选中（⌘A 全选回看）不静默塞满剪贴板
- 局域网明文 http（无 navigator.clipboard）退回 execCommand('copy')
- 反馈：气泡 + 顶栏状态，气泡自动消失且状态栏恢复原文案
- 失败提示不能建议非 mac 用 Ctrl+C（终端里那是 SIGINT）
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COPY_JS = ROOT / "web" / "wt-copy.js"
INDEX = ROOT / "web" / "ttyd-index.html"
DRIVER = ROOT / "tests" / "browser-keys.mjs"

BROWSER_EXPECTED = {
    "decide_ok": "copy",
    "decide_blank": "skip",
    "decide_nosel": "skip",
    "decide_dup": "skip",
    "describe": "已复制 2 字 / 已复制 2 行 · 4 字",
    "hook": "true",
    "hookIdempotent": "true",
    # 挂钩子时必须打开 mac 的 Option+拖选，否则 TUI 里根本选不中
    "macOptionSelect": "true",
    "drag": 'writes:1 text:"hello world" toast:已复制 11 字 status:已复制 11 字',
    "dupSelection": "writes:0",
    "cleared": "writes:0",
    "recopyAfterClear": 'writes:1 text:"hello world"',
    "blankSelection": "writes:0",
    "multiline": "writes:1 toast:已复制 3 行 · 17 字",
    "toastFades": "true",
    "statusRestored": "已连接",
    "tooBig": "writes:0 toast:选中过大，未自动复制（⌘C）",
    "legacyFallback": 'exec:["copy"] toast:已复制 13 字',
    "copyFail": "复制失败，请用 ⌘C | status:复制失败，请用 ⌘C",
    "hintNoBareCtrlC": "true",
    "winHint": "Ctrl+Shift+C",
}


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def test_source_keywords() -> None:
    if not COPY_JS.exists():
        fail("缺少 web/wt-copy.js")
    raw = COPY_JS.read_text(encoding="utf-8")
    for key in (
        "hookCopyOnSelect",
        "decideCopyAction",
        "describeCopied",
        "onSelectionChange",
        "clipboard.writeText",
        "execCommand",
        "MAX_CHARS",
        "DEBOUNCE_MS",
    ):
        if key not in raw:
            fail(f"wt-copy.js 缺少 {key}")
    if "hasSelection" not in raw:
        fail("wt-copy.js 必须用 term.hasSelection 判定，避免误清剪贴板")
    print("OK: wt-copy.js 关键逻辑齐全")


def test_pure_functions_node() -> None:
    script = r"""
const c = require('./web/wt-copy.js');
const assert = (cond, msg) => { if (!cond) { console.error('ASSERT ' + msg); process.exit(1); } };

assert(c.decideCopyAction({ hasSelection: true, text: 'abc' }) === 'copy', '有选中就复制');
assert(c.decideCopyAction({ hasSelection: false, text: 'abc' }) === 'skip', '无选中不动剪贴板');
assert(c.decideCopyAction({ hasSelection: true, text: '' }) === 'skip', '空文本跳过');
assert(c.decideCopyAction({ hasSelection: true, text: ' \n\t ' }) === 'skip', '纯空白跳过');
assert(c.decideCopyAction({ hasSelection: true, text: 'abc', lastText: 'abc' }) === 'skip', '同段不重复');
assert(c.decideCopyAction({ hasSelection: true, text: 'abd', lastText: 'abc' }) === 'copy', '换了内容要复制');
assert(c.decideCopyAction({ hasSelection: true, text: 'x'.repeat(c.MAX_CHARS + 1) }) === 'too-big', '超大跳过');
assert(c.decideCopyAction({ hasSelection: true, text: 'x'.repeat(c.MAX_CHARS) }) === 'copy', '边界内仍复制');
assert(c.decideCopyAction({ hasSelection: true, text: 'abcd', maxChars: 3 }) === 'too-big', 'maxChars 可覆盖');

assert(c.describeCopied('abc') === '已复制 3 字', '单行文案');
assert(c.describeCopied('a\nb') === '已复制 2 行 · 3 字', '多行文案');
assert(c.describeCopied('') === '已复制 0 字', '空文案不炸');

// 终端里 Ctrl+C 是 SIGINT，任何平台的提示都不能是裸 Ctrl+C
assert(c.manualCopyHint().indexOf('Ctrl+C') < 0, '不能提示裸 Ctrl+C');
assert(typeof c.hookCopyOnSelect === 'function', '导出挂载函数');
assert(c.hookCopyOnSelect(null) === false, '没有 term 时安全返回 false');
assert(c.hookCopyOnSelect({}) === false, 'term 缺少 onSelectionChange 时返回 false');

console.log('node copy policy ok');
"""
    cp = subprocess.run(["node", "-e", script], cwd=str(ROOT), capture_output=True, text=True)
    if cp.returncode != 0:
        fail(f"node 复制策略单测失败: {cp.stderr or cp.stdout}")
    print("OK: 选中即复制决策单测")


def test_index_inject() -> None:
    cp = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "patch-ttyd-index.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        fail(f"patch-ttyd-index 失败: {cp.stderr or cp.stdout}")
    html = INDEX.read_text(encoding="utf-8")
    for needle in ('id="wt-copy"', "WtCopy.hookCopyOnSelect", "decideCopyAction", "选中即复制"):
        if needle not in html:
            fail(f"ttyd-index.html 缺少注入: {needle}")
    print("OK: ttyd-index 已注入选中即复制")


def test_browser_scenarios() -> None:
    cp = subprocess.run(
        ["node", str(DRIVER), "copy-harness.html"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    out = (cp.stdout or "").strip()
    if cp.returncode != 0:
        fail(f"浏览器回归执行失败: {out or cp.stderr}")
    if out.startswith("SKIP="):
        print(f"OK: 跳过浏览器复制回归（{out[5:]}）")
        return
    got = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if "ERR" in got:
        fail(f"浏览器内报错: {got['ERR']}")
    for key, want in BROWSER_EXPECTED.items():
        if key not in got:
            fail(f"缺少场景结果 {key}（实际: {out!r}）")
        if got[key] != want:
            fail(f"{key} 期望 {want!r}，实际 {got[key]!r}")
    print(f"OK: 浏览器复制回归 {len(BROWSER_EXPECTED)} 个场景")


if __name__ == "__main__":
    test_source_keywords()
    test_pure_functions_node()
    test_index_inject()
    test_browser_scenarios()
