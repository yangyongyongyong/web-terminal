#!/usr/bin/env python3
"""前台交互式工具识别：argv → 工具名，以及 tty 上的前台进程挑选。

为什么不用 tmux 的 #{pane_current_command}：claude 会把进程名改成版本号
（实测显示 2.1.220），codex 和 cursor-agent 又都显示成 node。所以按 pane 的
tty 用 ps 找带 '+' 的前台进程，且父进程是 pane 内 shell 的那个，再看完整 argv。

下面的 ps 输出都是线上真实抓的。
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("manage_server", ROOT / "bin" / "manage-server.py")
assert spec and spec.loader
ms = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ms)


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


LABEL_CASES = [
    # (argv, kind, label)
    ("claude", "claude", "Claude"),
    ("claude -c", "claude", "Claude"),
    ("node /opt/homebrew/bin/codex", "codex", "Codex"),
    ("/opt/homebrew/lib/node_modules/@openai/codex/.../bin/codex", "codex", "Codex"),
    ("opencode", "opencode", "OpenCode"),
    (
        "/Users/u/.local/bin/agent --use-system-ca /Users/u/.local/share/cursor-agent/versions/x/index.js",
        "cursor",
        "Cursor Agent",
    ),
    ("python3", "python", "Python"),
    ("/opt/homebrew/opt/python@3.14/bin/python3.14", "python", "Python 3.14"),
    ("ipython", "ipython", "IPython"),
    ("node", "node", "Node"),
    ("scala", "scala", "Scala"),
    ("scala3", "scala", "Scala"),
    ("sbt", "scala", "Scala"),
    ("irb", "ruby", "Ruby"),
    ("psql -U postgres db", "db", "psql"),
    ("sqlite3 a.db", "db", "SQLite"),
    ("vim foo.py", "editor", "Vim"),
    ("nvim", "editor", "Neovim"),
    ("less README.md", "pager", "less"),
    ("man ps", "pager", "man"),
    ("top -o cpu", "monitor", "top"),
    ("ssh box", "ssh", "SSH"),
    ("some-unknown-binary --flag", "other", "some-unknown-binary"),
    # shell 本身不算工具，前端不显示
    ("/bin/zsh -l", "shell", ""),
    ("-zsh", "shell", ""),
    ("bash", "shell", ""),
    ("fish", "shell", ""),
    ("", "", ""),
]

# 线上真实 ps -t <tty> -o pid=,ppid=,stat=,args= 输出
PS_CLAUDE = """ 30086 78956 Ss   /bin/zsh -l
 31237 30086 S+   claude
 31666 31237 S+   node /Users/u/wps-skills/wps-office-mcp/dist/index.js
"""
PS_CODEX = """ 10422 78957 S+   node /opt/homebrew/bin/codex
 10424 10422 S+   /opt/homebrew/lib/node_modules/@openai/codex/vendor/bin/codex
 10559 10424 S    /Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node_repl
 78957 78956 Ss   /bin/zsh -l
"""
PS_CURSOR = """ 52312 78956 Ss   /bin/zsh -l
 53271 52312 S+   /Users/u/.local/bin/agent --use-system-ca /Users/u/.local/share/cursor-agent/versions/x/index.js --continue
"""
PS_IDLE_SHELL = """ 78957 78956 Ss+  /bin/zsh -l
"""
PS_ONLY_BACKGROUND = """ 78957 78956 Ss   /bin/zsh -l
 90001 78957 S    sleep 100
"""


def fake_run(ps_output: str, tty: str = "/dev/ttys001", pane_pid: str = "78957"):
    def _run(cmd, *a, **kw):
        if cmd[0] == "tmux":
            return subprocess.CompletedProcess(cmd, 0, f"{tty}\t{pane_pid}\n", "")
        if cmd[0] == "ps":
            return subprocess.CompletedProcess(cmd, 0, ps_output, "")
        raise AssertionError(f"意外的命令: {cmd}")

    return _run


def test_label_process() -> None:
    for argv, kind, label in LABEL_CASES:
        got = ms.label_process(argv)
        if got["kind"] != kind or got["label"] != label:
            fail(f"{argv!r} 期望 kind={kind} label={label}，实际 {got}")
    print(f"OK: argv → 工具名 {len(LABEL_CASES)} 例")


def test_foreground_picks_direct_child() -> None:
    cases = [
        (PS_CLAUDE, "/dev/ttys005", "30086", "claude", "Claude"),   # 不取更深的 MCP 子进程
        (PS_CODEX, "/dev/ttys001", "78957", "codex", "Codex"),      # 不取 vendor 子进程
        (PS_CURSOR, "/dev/ttys004", "52312", "cursor", "Cursor Agent"),
        (PS_IDLE_SHELL, "/dev/ttys001", "78957", "shell", ""),      # 光标在提示符上
        (PS_ONLY_BACKGROUND, "/dev/ttys001", "78957", "shell", ""), # 只有后台任务
    ]
    for ps_out, tty, pane_pid, kind, label in cases:
        with mock.patch.object(ms.subprocess, "run", side_effect=fake_run(ps_out, tty, pane_pid)):
            got = ms.foreground_process("wt-x")
        if got["kind"] != kind or got["label"] != label:
            fail(f"tty={tty} 期望 kind={kind} label={label}，实际 {got}")
    print(f"OK: 前台进程挑选 {len(cases)} 例")


def test_foreground_tolerates_failures() -> None:
    def tmux_fails(cmd, *a, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "no server")

    with mock.patch.object(ms.subprocess, "run", side_effect=tmux_fails):
        got = ms.foreground_process("wt-x")
    if got["label"] or got["kind"]:
        fail(f"tmux 挂了应返回空，实际 {got}")

    def ps_fails(cmd, *a, **kw):
        if cmd[0] == "tmux":
            return subprocess.CompletedProcess(cmd, 0, "/dev/ttys001\t1\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "boom")

    with mock.patch.object(ms.subprocess, "run", side_effect=ps_fails):
        got = ms.foreground_process("wt-x")
    if got["label"]:
        fail(f"ps 挂了应返回空 label，实际 {got}")
    print("OK: tmux/ps 失败时不炸")


def test_lan_cors_covers_loopback() -> None:
    """回归：127.0.0.1:<ttyd> → 127.0.0.1:<manage> 也是跨源，必须给 CORS 头，
    否则本机直接开终端页时贴图/前台识别都会 Failed to fetch。"""

    class FakeHandler:
        def __init__(self, origin: str) -> None:
            self.headers = {"Origin": origin}

        _lan_cors_headers = ms.Handler._lan_cors_headers

    ttyd_port = ms.TTYD_PORT
    ok = FakeHandler(f"http://127.0.0.1:{ttyd_port}")._lan_cors_headers()
    if not any(k == "Access-Control-Allow-Origin" for k, _ in ok):
        fail("loopback ttyd 源必须放行 CORS")
    if not any(k == "Access-Control-Allow-Credentials" for k, _ in ok):
        fail("跨源请求要带凭据，必须放行 credentials")
    lan = FakeHandler(f"http://192.168.1.20:{ttyd_port}")._lan_cors_headers()
    if not lan:
        fail("局域网 ttyd 源必须放行 CORS")
    for bad in ("http://evil.example.com", f"http://127.0.0.1:{ttyd_port + 1}", "https://term.example.com"):
        if FakeHandler(bad)._lan_cors_headers():
            fail(f"非 ttyd 源不该放行: {bad}")
    if FakeHandler("")._lan_cors_headers():
        fail("无 Origin 不该放行")
    print("OK: CORS 只放行同机 ttyd 源（含 loopback）")


BROWSER_EXPECTED = {
    "badgeClaude": "▸ Claude",
    "badgeShell": '""',
    "badgeNull": '""',
    "badgeNoLabel": '""',
    "badgeNotOk": '""',
    "colors": "#e0855b,#8ec7ff,#ffd75f,#9aa7b4",
    "urlEncoded": "/api/foreground?name=web%E7%BB%88%E7%AB%AF",
    "urlWithBase": "http://192.168.1.20:7690/api/foreground?name=a",
    "render": "▸ Python 3.14 color:rgb(255, 215, 95) title:当前前台进程: python3.14",
    "renderShellClears": '"" color:""',
    "poll": "▸ Claude requests>0:true",
    "switch": "▸ Python 3.14 color:rgb(255, 215, 95)",
    "backToShell": '""',
    "recovered": "▸ Claude",
    "afterFailures": '"" thrown:3 backedOff:true',
    "stopped": "true",
    "noTermNeeded": "true",
}


def test_index_inject() -> None:
    cp = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "patch-ttyd-index.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        fail(f"patch-ttyd-index 失败: {cp.stderr or cp.stdout}")
    html = (ROOT / "web" / "ttyd-index.html").read_text(encoding="utf-8")
    for needle in ('id="wt-tool"', "WtTool.hookToolIndicator", "/api/foreground", "badgeText"):
        if needle not in html:
            fail(f"ttyd-index.html 缺少注入: {needle}")
    print("OK: ttyd-index 已注入顶栏工具徽标")


def test_browser_scenarios() -> None:
    cp = subprocess.run(
        ["node", str(ROOT / "tests" / "browser-keys.mjs"), "tool-harness.html"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    out = (cp.stdout or "").strip()
    if cp.returncode != 0:
        fail(f"浏览器回归执行失败: {out or cp.stderr}")
    if out.startswith("SKIP="):
        print(f"OK: 跳过浏览器徽标回归（{out[5:]}）")
        return
    got = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if "ERR" in got:
        fail(f"浏览器内报错: {got['ERR']}")
    for key, want in BROWSER_EXPECTED.items():
        if key not in got:
            fail(f"缺少场景结果 {key}（实际: {out!r}）")
        if got[key] != want:
            fail(f"{key} 期望 {want!r}，实际 {got[key]!r}")
    print(f"OK: 浏览器徽标回归 {len(BROWSER_EXPECTED)} 个场景")


if __name__ == "__main__":
    test_label_process()
    test_foreground_picks_direct_child()
    test_foreground_tolerates_failures()
    test_lan_cors_covers_loopback()
    test_index_inject()
    test_browser_scenarios()
