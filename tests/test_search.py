#!/usr/bin/env python3
"""终端内搜索：纯函数单测 + 注入校验 + 真浏览器场景回归。

要点：
- mac 用 ⌘F，Windows/Linux 用 Ctrl+F；mac 的 Ctrl+F 必须留给 PTY（readline 前移光标）
- 折行（isWrapped）合并成逻辑行后再搜，跨行断开的词也要命中
- 宽字符（中日韩）不能把列号算错，否则高亮会错位
- 全部命中上底色装饰，当前命中另一种颜色 + 选中高亮 + 滚动到可见
- 搜索用的 term.select 不能触发「选中即复制」污染剪贴板
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEARCH_JS = ROOT / "web" / "wt-search.js"
INDEX = ROOT / "web" / "ttyd-index.html"
DRIVER = ROOT / "tests" / "browser-keys.mjs"

BROWSER_EXPECTED = {
    "logicalLines": '1 text:"abc needle xyz"',
    "wrappedMatch": '[{"line":0,"index":4,"length":6}]',
    "cjkIndex": "2",
    "cjkCell": '{"row":0,"col":3}',  # a=0, 中占 1~2 两格, needle 从第 3 格开始
    "hook": "true",
    "hookIdempotent": "true",
    "winCmdF": "prevented:false open:false",
    "winCtrlF": "prevented:true open:true visible:true focused:true",
    "search": "total:3 count:3/3 decorations:3 select:[13,2,6]",
    "suppressCalled": "true",
    "currentDecorationColor": "1",
    "next": "1/3",
    "prev": "3/3",
    "enterOutsideBar": "prevented:false",
    "noMatch": "无匹配 decorations:0 cleared:true",
    "escape": "prevented:true open:false hidden:true termFocused:true",
    "macCtrlF": "prevented:false open:false",
    "macCmdF": "prevented:true open:true",
    "seedFromSelection": "again count:1/1",
}


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def test_source_keywords() -> None:
    if not SEARCH_JS.exists():
        fail("缺少 web/wt-search.js")
    raw = SEARCH_JS.read_text(encoding="utf-8")
    for key in (
        "decideSearchKeyAction",
        "findMatches",
        "collectLogicalLines",
        "locateCell",
        "registerDecoration",
        "registerMarker",
        "scrollToLine",
        "isWrapped",
        "WtCopy",
    ):
        if key not in raw:
            fail(f"wt-search.js 缺少 {key}")
    if ".attachCustomKeyEventHandler(" in raw:  # 注释里提到没问题，调用才是问题
        fail("搜索不能占用 attachCustomKeyEventHandler（xterm 只允许一个，已被键位改写占用）")
    print("OK: wt-search.js 关键逻辑齐全")


def test_pure_functions_node() -> None:
    script = r"""
const s = require('./web/wt-search.js');
const assert = (c, m) => { if (!c) { console.error('ASSERT ' + m); process.exit(1); } };
const kd = (o) => Object.assign({ type: 'keydown', code: 'KeyF', key: 'f' }, o);

// 键位：mac ⌘F / 其它 Ctrl+F；mac 的 Ctrl+F 留给 PTY
assert(s.decideSearchKeyAction(kd({ metaKey: true }), 'mac') === 'open', 'mac ⌘F 开');
assert(s.decideSearchKeyAction(kd({ ctrlKey: true }), 'mac') === 'default', 'mac Ctrl+F 不拦');
assert(s.decideSearchKeyAction(kd({ ctrlKey: true }), 'other') === 'open', 'win Ctrl+F 开');
assert(s.decideSearchKeyAction(kd({ metaKey: true }), 'other') === 'default', 'win ⌘F 不拦');
assert(s.decideSearchKeyAction(kd({ ctrlKey: true, altKey: true }), 'other') === 'default', 'Ctrl+Alt+F 不拦');
assert(s.decideSearchKeyAction(kd({ ctrlKey: true, code: 'KeyG' , key: 'g' }), 'other') === 'default', '其它键不拦');
assert(s.decideSearchKeyAction({ type: 'keyup', key: 'f', ctrlKey: true }, 'other') === 'default', 'keyup 不处理');

// 搜索条关着时，Esc/回车不能被抢
assert(s.decideSearchKeyAction({ type: 'keydown', key: 'Escape' }, 'other') === 'default', '关着时 Esc 不拦');
assert(s.decideSearchKeyAction({ type: 'keydown', key: 'Enter' }, 'other') === 'default', '关着时回车不拦');
assert(s.decideSearchKeyAction({ type: 'keydown', key: 'Escape', searchOpen: true }, 'other') === 'close', '开着时 Esc 关闭');
assert(s.decideSearchKeyAction({ type: 'keydown', key: 'Enter', searchOpen: true }, 'other') === 'next', '回车下一个');
assert(s.decideSearchKeyAction({ type: 'keydown', key: 'Enter', shiftKey: true, searchOpen: true }, 'other') === 'prev', 'Shift+回车上一个');

// 命中查找：大小写不敏感、非正则、可重叠推进
const lines = [{ text: 'foo bar FOO' }, { text: 'nothing' }, { text: 'xfoox' }];
const hits = s.findMatches(lines, 'foo');
assert(hits.length === 3, '大小写不敏感命中 3 处，实际 ' + hits.length);
assert(hits[0].line === 0 && hits[0].index === 0, '第一处位置');
assert(hits[1].index === 8, '同行第二处位置');
assert(hits[2].line === 2 && hits[2].index === 1, '第三处位置');
assert(s.findMatches(lines, '').length === 0, '空关键词不命中');
assert(s.findMatches(lines, 'FOO', { caseSensitive: true }).length === 1, '区分大小写');
assert(s.findMatches([{ text: 'a.c abc' }], 'a.c').length === 1, '点号按字面匹配，不当正则');
assert(s.findMatches([{ text: 'aaaa' }], 'aa').length === 2, '推进步长不重复卡死');
assert(s.findMatches([{ text: 'x'.repeat(50) }], 'x', { max: 5 }).length === 5, 'max 上限生效');

// 计数文案
assert(s.describeCount(0, -1) === '无匹配', '无匹配文案');
assert(s.describeCount(3, 0) === '1/3', '计数文案');
assert(s.describeCount(3, 2) === '3/3', '计数文案末尾');

// 没有 term 时不炸
assert(s.hookSearch(null) === false, '无 term 返回 false');
assert(s.collectLogicalLines(null).length === 0, '无 term 逻辑行为空');

console.log('node search policy ok');
"""
    cp = subprocess.run(["node", "-e", script], cwd=str(ROOT), capture_output=True, text=True)
    if cp.returncode != 0:
        fail(f"node 搜索单测失败: {cp.stderr or cp.stdout}")
    print("OK: 搜索决策与命中查找单测")


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
    for needle in ('id="wt-search"', "WtSearch.hookSearch", "decideSearchKeyAction", "⌘F 搜索"):
        if needle not in html:
            fail(f"ttyd-index.html 缺少注入: {needle}")
    print("OK: ttyd-index 已注入搜索")


def test_browser_scenarios() -> None:
    cp = subprocess.run(
        ["node", str(DRIVER), "search-harness.html"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    out = (cp.stdout or "").strip()
    if cp.returncode != 0:
        fail(f"浏览器回归执行失败: {out or cp.stderr}")
    if out.startswith("SKIP="):
        print(f"OK: 跳过浏览器搜索回归（{out[5:]}）")
        return
    got = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if "ERR" in got:
        fail(f"浏览器内报错: {got['ERR']}")
    for key, want in BROWSER_EXPECTED.items():
        if key not in got:
            fail(f"缺少场景结果 {key}（实际: {out!r}）")
        if got[key] != want:
            fail(f"{key} 期望 {want!r}，实际 {got[key]!r}")
    print(f"OK: 浏览器搜索回归 {len(BROWSER_EXPECTED)} 个场景")


if __name__ == "__main__":
    test_source_keywords()
    test_pure_functions_node()
    test_index_inject()
    test_browser_scenarios()
