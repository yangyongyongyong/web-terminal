#!/usr/bin/env python3
"""会话管理页 + API（7690）。进入终端需 SESSION_PIN；工作目录受 SESSION_PATH_ROOT 限制。"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("session_ticket", ROOT / "bin" / "session-ticket.py")
assert _spec and _spec.loader
session_ticket = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(session_ticket)


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    env_file = ROOT / ".env"
    if not env_file.exists():
        raise SystemExit(f"缺少 {env_file}")
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


ENV = load_env()
USER = ENV.get("TTYD_USER", "admin")
PASSWORD = ENV["TTYD_PASSWORD"]
PUBLIC_HOST = ENV.get("PUBLIC_HOST", "term.lucadesign.uk")
MANAGE_HOST = ENV.get("MANAGE_HOST", "127.0.0.1")
MANAGE_PORT = int(ENV.get("MANAGE_PORT", "7690"))
HOME = str(Path.home())
PATH_ROOT = str(Path(ENV.get("SESSION_PATH_ROOT") or HOME).expanduser())
DEFAULT_PATH = str(Path(ENV.get("SESSION_DEFAULT_PATH") or HOME).expanduser())
SESSION_CTL = str(ROOT / "bin" / "session-ctl.sh")
NAME_RE = re.compile(r"^[\w\-]{1,64}$")  # 含中文等 Unicode 字母数字；不含 . : / 空白
TTYD_PORT = int(ENV.get("TTYD_PORT", "7681"))
TTYD_BASE_PATH = (ENV.get("TTYD_BASE_PATH") or "/term").rstrip("/") or "/term"


def _lan_ip_rank(ip: str) -> int:
    """给候选 IPv4 打分：真正的家用/办公局域网段优先，VPN/基准/保留段靠后。
    分数越大越优先；<=0 表示不适合作为局域网直连地址。"""
    if not ip or ip.startswith("127.") or ip.startswith("169.254."):
        return -1
    try:
        a, b = (int(x) for x in ip.split(".")[:2])
    except ValueError:
        return -1
    if a == 192 and b == 168:
        return 100  # 最常见家用/办公 Wi-Fi
    if a == 10:
        return 90
    if a == 172 and 16 <= b <= 31:
        return 80
    if a == 198 and b in (18, 19):
        return 1   # 198.18/15 基准测试段：常被 VPN/透明代理占用，局域网设备通常不可达
    if a == 100 and 64 <= b <= 127:
        return 2   # 100.64/10 CGNAT：运营商级，局域网内多半不可直连
    return 50      # 其它可路由/私有地址：可用但不如标准私有段


def _candidate_ipv4s() -> list[str]:
    import socket

    found: list[str] = []

    def add(ip: str) -> None:
        if ip and ip not in found:
            found.append(ip)

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))  # 不真正发包，只让内核选出出口地址
            add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        add_all = socket.gethostbyname_ex(socket.gethostname())[2]
        for ip in add_all:
            add(ip)
    except OSError:
        pass
    # macOS/Linux：枚举所有网卡地址，覆盖 Wi-Fi/有线，避免只拿到 VPN 出口
    try:
        import subprocess

        out = subprocess.run(
            ["ifconfig"], capture_output=True, text=True, timeout=2
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("inet ") and "inet6" not in line:
                add(line.split()[1])
    except (OSError, subprocess.SubprocessError):
        pass
    return found


def detect_lan_ip() -> str:
    """返回本机最适合局域网直连的 IPv4;失败返回空串。用于让同网设备低延迟直连。
    优先标准私有段(192.168/10/172.16-31),排除 VPN 基准段与 CGNAT。"""
    best = ""
    best_rank = 0
    for ip in _candidate_ipv4s():
        r = _lan_ip_rank(ip)
        if r > best_rank:
            best_rank = r
            best = ip
    return best


MAX_PAGES = 2000  # 上限护栏：避免误填超大值把浏览器内存吃满
PAGES_FILE = ROOT / "run" / "scrollback-pages"  # 全局默认（管理页可改，运行时生效）


def _env_default_pages() -> int:
    raw = (ENV.get("SCROLLBACK_PAGES") or "30").split("#", 1)[0].strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 30


def default_pages() -> int:
    """全局默认回看页数：run/scrollback-pages > .env SCROLLBACK_PAGES > 30。
    每次读取文件，管理页改完立即生效，无需重启。"""
    try:
        raw = PAGES_FILE.read_text(encoding="utf-8").strip()
        n = int(raw)
        if 1 <= n <= MAX_PAGES:
            return n
    except (OSError, ValueError):
        pass
    return _env_default_pages()


def set_default_pages(pages: int) -> None:
    PAGES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PAGES_FILE.with_suffix(".tmp")
    tmp.write_text(f"{int(pages)}\n", encoding="utf-8")
    tmp.replace(PAGES_FILE)
ICON_SVG = ROOT / "web" / "wt-icon-manage.svg"
ICON_PNG = ROOT / "web" / "wt-icon-manage-64.png"


def favicon_links() -> str:
    """管理页原本没有 favicon（标签栏空白）。用 data URI 内联，避免额外请求与鉴权往返。"""
    try:
        svg = base64.b64encode(ICON_SVG.read_bytes()).decode()
        png = base64.b64encode(ICON_PNG.read_bytes()).decode()
    except OSError:
        return ""
    return (
        f'<link id="wt-favicon" rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,{svg}">'
        f'<link rel="alternate icon" type="image/png" sizes="64x64" href="data:image/png;base64,{png}">'
        f'<link rel="apple-touch-icon" href="data:image/png;base64,{png}">'
    )


PASTE_DIR = ROOT / "run" / "paste-images"
PASTE_MAX_BYTES = 12 * 1024 * 1024
PASTE_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

MANAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Web Terminal · 会话管理</title>
__FAVICON__
<style>
  :root {
    --bg: #0f1419; --panel: #1a222c; --line: #2a3542;
    --text: #e7ecf1; --muted: #8b9aab; --accent: #3d8bfd;
    --ok: #3dd68c; --warn: #f5a524; --danger: #f31260;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh;
    font: 14px/1.5 "IBM Plex Sans", "Source Han Sans SC", "PingFang SC", sans-serif;
    color: var(--text);
    background:
      radial-gradient(1200px 600px at 10% -10%, #1b3a5f 0%, transparent 55%),
      radial-gradient(900px 500px at 100% 0%, #14352c 0%, transparent 50%),
      var(--bg);
  }
  main { width: 100%; max-width: 2400px; margin: 0 auto; padding: 28px clamp(14px, 2vw, 36px) 72px; }
  header { margin-bottom: 28px; }
  h1 { margin: 0 0 6px; font-size: 28px; letter-spacing: -0.02em; }
  .sub { color: var(--muted); }
  .card {
    background: color-mix(in srgb, var(--panel) 88%, transparent);
    border: 1px solid var(--line); border-radius: 14px;
    padding: 18px 18px 8px; margin-bottom: 18px;
    backdrop-filter: blur(8px);
  }
  .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; }
  input[type=text], input[type=password], input[type=search] {
    flex: 1; min-width: 160px;
    background: #0c1117; color: var(--text);
    border: 1px solid var(--line); border-radius: 8px;
    padding: 10px 12px; font: inherit;
  }
  button {
    appearance: none; border: 0; cursor: pointer;
    border-radius: 8px; padding: 9px 14px; font: inherit; color: #fff;
    background: var(--accent);
  }
  button.secondary { background: #2b3645; }
  button.danger { background: var(--danger); }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  #listWrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; table-layout: fixed; min-width: 720px; }
  th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid var(--line); vertical-align: middle; }
  th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }
  th.chk, td.chk { width: 36px; }
  th.status-col, td.status-cell { width: 52px; }
  th.name-col, td.name-cell { width: 220px; }
  td.name-cell { overflow: hidden; }
  th.path-col { width: auto; }
  th.time-col, td.time-cell {
    width: 148px; white-space: nowrap;
    font-variant-numeric: tabular-nums;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px; color: var(--muted);
  }
  th.act-col, td.actions { width: 212px; }
  .path {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px; color: var(--muted);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .path:hover { white-space: normal; word-break: break-all; }
  .tag {
    display: inline-block; padding: 2px 8px; border-radius: 999px;
    font-size: 12px; background: #243041; color: var(--muted);
  }
  .tag.on { background: rgba(61,214,140,.15); color: var(--ok); }
  .tag.off { background: rgba(245,165,36,.12); color: var(--warn); }
  .actions {
    display: flex; gap: 8px; flex-wrap: nowrap; align-items: center;
    white-space: nowrap;
  }
  .actions button { padding: 7px 12px; flex: 0 0 auto; }
  .empty { color: var(--muted); padding: 16px 4px; }
  .flash { margin: 0 0 14px; padding: 10px 12px; border-radius: 8px; background: #1e2a38; color: var(--muted); }
  .flash.err { background: rgba(243,18,96,.12); color: #ff8fab; }
  .lanbar { margin: 0 0 14px; padding: 10px 12px; border-radius: 8px; background: rgba(46,160,67,.12); color: #7ee787; font-size: 13px; line-height: 1.6; }
  .lanbar code { background: rgba(0,0,0,.25); padding: 1px 6px; border-radius: 4px; color: #d2e6ff; user-select: all; }
  .lanbar .muted { color: var(--muted); }
  .hint { font-size: 12px; color: var(--muted); margin: -4px 0 10px; }
  input[type=checkbox] { width: 16px; height: 16px; accent-color: var(--accent); cursor: pointer; }
  th.sortable { cursor: pointer; user-select: none; }
  th.sortable:hover { color: var(--text); }
  th.sortable .arrow { opacity: 0.45; margin-left: 4px; font-size: 10px; }
  .dot {
    display: inline-block; width: 10px; height: 10px; border-radius: 50%;
    vertical-align: middle;
  }
  .dot.green { background: var(--ok); box-shadow: 0 0 0 3px rgba(61,214,140,.18); }
  .dot.red { background: var(--danger); box-shadow: 0 0 0 3px rgba(243,18,96,.18); }
  .status-cell { white-space: nowrap; }
  .name-edit {
    appearance: none; border: 0; background: transparent; color: var(--text);
    font: inherit; font-weight: 700; padding: 0; margin: 0; cursor: pointer;
    text-align: left; border-bottom: 1px dashed color-mix(in srgb, var(--muted) 55%, transparent);
    display: block; max-width: 100%;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .name-edit:hover { color: var(--accent); border-bottom-color: var(--accent); }
  th.pages-col, td.pages-cell { width: 96px; white-space: nowrap; }
  .pages-edit {
    appearance: none; border: 0; background: transparent; color: var(--text);
    font: inherit; padding: 0; margin: 0; cursor: pointer; text-align: left;
    border-bottom: 1px dashed color-mix(in srgb, var(--muted) 55%, transparent);
  }
  .pages-edit:hover { color: var(--accent); border-bottom-color: var(--accent); }
  .pages-edit.is-default { color: var(--muted); }
  .modal-mask {
    position: fixed; inset: 0; z-index: 10000;
    background: rgba(0,0,0,.55);
    display: flex; align-items: center; justify-content: center; padding: 20px;
  }
  .modal {
    width: min(400px, 100%); background: var(--panel);
    border: 1px solid var(--line); border-radius: 14px; padding: 20px;
  }
  .modal h3 { margin: 0 0 8px; font-size: 18px; }
  .modal p { margin: 0 0 14px; color: var(--muted); font-size: 13px; }
  .modal .row { margin-bottom: 0; margin-top: 14px; }
  .modal input { font-size: 16px; }
  [hidden] { display: none !important; }
</style>
</head>
<body>
<main>
  <header>
    <h1>会话管理</h1>
    <p class="sub">进入终端需二次验证（24 小时内免重复输入）· 新建使用默认工作目录</p>
  </header>
  <div id="flash" class="flash" hidden></div>
  <div id="lanBar" class="lanbar" hidden></div>

  <section class="card">
    <div class="row">
      <button id="btnCreate" type="button">新建会话</button>
      <input id="searchQ" type="search" placeholder="关键字检索：名称 / 路径" autocomplete="off">
    </div>
  </section>

  <section class="card">
    <div class="row" style="justify-content:space-between;align-items:center">
      <h2 style="margin:0;font-size:16px">会话</h2>
      <div class="actions">
        <button id="btnDefaultPages" class="secondary" type="button" title="设置全局默认回看页数">全局回看…</button>
        <button id="btnBulkDel" class="danger" type="button">删除选中</button>
      </div>
    </div>
    <p class="hint" style="margin-top:8px"><span class="dot green"></span> 可恢复 &nbsp;&nbsp; <span class="dot red"></span> 已断开 &nbsp;&nbsp; 回看列显示「默认 N」= 未单独配置，跟随全局</p>
    <div id="listWrap"><p class="empty">加载中…</p></div>
  </section>
</main>

<div id="pinModal" class="modal-mask" hidden>
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="pinTitle">
    <h3 id="pinTitle">安全验证</h3>
    <p id="pinHint">继续操作前请完成验证</p>
    <input id="pinInput" type="password" autocomplete="new-password" autocapitalize="off" spellcheck="false" placeholder="">
    <div class="row">
      <button id="pinCancel" class="secondary" type="button">取消</button>
      <button id="pinConfirm" type="button">确认进入</button>
    </div>
  </div>
</div>

<div id="nameModal" class="modal-mask" hidden>
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="nameTitle">
    <h3 id="nameTitle">新建会话</h3>
    <p id="nameHint">输入会话名称</p>
    <input id="nameInput" type="text" maxlength="64" autocomplete="off" placeholder="例如 work / web终端">
    <div class="row">
      <button id="nameCancel" class="secondary" type="button">取消</button>
      <button id="nameConfirm" type="button">下一步</button>
    </div>
  </div>
</div>

<div id="pagesModal" class="modal-mask" hidden>
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="pagesTitle">
    <h3 id="pagesTitle">回看页数</h3>
    <p id="pagesHint">设置本会话滚轮回看的历史页数</p>
    <input id="pagesInput" type="text" inputmode="numeric" autocomplete="off" placeholder="留空 = 用全局默认">
    <div class="row">
      <button id="pagesCancel" class="secondary" type="button">取消</button>
      <button id="pagesReset" class="secondary" type="button">恢复默认</button>
      <button id="pagesConfirm" type="button">保存</button>
    </div>
  </div>
</div>

<script>
const termBase = '/term/';
let pending = null;
let nameModalMode = null; // { type: 'create' } | { type: 'rename', oldName } | { type: 'clone', cwd, srcName }
let cache = { sessions: [], history: [], path_root: '', default_path: '' };
const rowSelected = new Set(); // name + \\u001e + cwd + \\u001e + live|dead
let lastRowChkIndex = null; // 多选：普通点击锚点，供 Shift 范围选取
let sortKey = 'status';
let sortDir = 1; // 1 asc, -1 desc

// 局域网直连：若当前不是经 Cloudflare 隧道（公网域名）访问，则没有 /term 路径路由，
// 需把票据返回的相对 /term/… 改写成 http://<本机>:<ttyd端口>/term/…，延迟更低。
function offTunnel() {
  const pub = cache && cache.public_host;
  return !!pub && location.hostname !== pub;
}

function resolveTermUrl(relUrl) {
  if (!offTunnel() || !cache.ttyd_port) return relUrl;
  try {
    // relUrl 形如 /term/?arg=...；保留其 path+query，仅换 host:port
    const u = new URL(relUrl, location.origin);
    return location.protocol + '//' + location.hostname + ':' + cache.ttyd_port + u.pathname + u.search;
  } catch (e) {
    return relUrl;
  }
}


function isValidSessionName(name) {
  return /^[\p{L}\p{N}_-]{1,64}$/u.test(name || '');
}

function flash(msg, isErr) {
  const el = document.getElementById('flash');
  el.hidden = !msg;
  el.textContent = msg || '';
  el.className = 'flash' + (isErr ? ' err' : '');
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

/** 2026-07-28T20:57:54+0800 → 20260728 20:57:54 */
function formatTime(raw) {
  const s = String(raw || '').trim();
  if (!s || s === '-') return '-';
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})/);
  if (m) return m[1] + m[2] + m[3] + ' ' + m[4] + ':' + m[5] + ':' + m[6];
  const d = new Date(s);
  if (!Number.isNaN(d.getTime())) {
    const p = n => String(n).padStart(2, '0');
    return '' + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate())
      + ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }
  return s;
}

function histKey(name, cwd) {
  return String(name || '') + '\u001e' + String(cwd || '');
}

function rowKey(r) {
  return histKey(r.name, r.cwd || '') + '\u001e' + (r.live ? 'live' : 'dead');
}

function parseRowKey(key) {
  const parts = String(key || '').split('\u001e');
  return {
    name: parts[0] || '',
    cwd: parts[1] || '',
    live: parts[2] === 'live',
  };
}

function q() {
  return (document.getElementById('searchQ').value || '').trim().toLowerCase();
}

function matchKw(s) {
  const kw = q();
  if (!kw) return true;
  const hay = [s.name, s.cwd, s.path, s.sid].filter(Boolean).join(' ').toLowerCase();
  return hay.includes(kw);
}

function mergedRows() {
  const live = (cache.sessions || []).map(s => ({
    name: s.name,
    cwd: s.cwd || s.cwd_now || '',
    path: s.cwd_now || s.cwd || '',
    live: true,
    clients: s.clients || 0,
    time: s.last_open || s.created || '',
    created: s.created || '',
    pages: Number(s.pages) || 0,
  }));
  const liveNames = new Set(live.map(s => s.name));
  const dead = (cache.history || [])
    .filter(h => !liveNames.has(h.name))
    .map(h => ({
      name: h.name,
      cwd: h.cwd || '',
      path: h.cwd || '',
      live: false,
      clients: 0,
      time: h.stopped || h.last_open || h.created || '',
      created: h.created || '',
      pages: Number(h.pages) || 0,
    }));
  return live.concat(dead);
}

function sortedRows(rows) {
  const dir = sortDir;
  const key = sortKey;
  return rows.slice().sort((a, b) => {
    let va, vb;
    if (key === 'status') {
      va = a.live ? 0 : 1;
      vb = b.live ? 0 : 1;
    } else if (key === 'name') {
      va = (a.name || '').toLowerCase();
      vb = (b.name || '').toLowerCase();
    } else if (key === 'path') {
      va = (a.path || '').toLowerCase();
      vb = (b.path || '').toLowerCase();
    } else {
      va = a.time || '';
      vb = b.time || '';
    }
    if (va < vb) return -1 * dir;
    if (va > vb) return 1 * dir;
    return (a.name || '').localeCompare(b.name || '');
  });
}

function sortArrow(col) {
  if (sortKey !== col) return '';
  return '<span class="arrow">' + (sortDir > 0 ? '▲' : '▼') + '</span>';
}

async function api(path, opts) {
  const res = await fetch(path, Object.assign({ credentials: 'same-origin' }, opts || {}));
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); } catch { data = { raw: text }; }
  if (!res.ok) {
    const err = new Error(data.error || text || res.statusText);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

function openTermTab(url) {
  const w = window.open(url, '_blank');
  if (!w) flash('浏览器拦截了新标签，请允许弹窗后重试', true);
  return w;
}

function askPin(item, hint) {
  pending = { item: item };
  document.getElementById('pinHint').textContent = hint || (
    (item.create ? '新建并进入「' : '进入「') + item.name + '」前请完成验证'
  );
  const modal = document.getElementById('pinModal');
  const input = document.getElementById('pinInput');
  modal.hidden = false;
  input.value = '';
  setTimeout(() => input.focus(), 50);
}

function closePin() {
  pending = null;
  document.getElementById('pinModal').hidden = true;
  document.getElementById('pinInput').value = '';
}

async function fetchTicketUrl(item, pin) {
  const body = { name: item.name, create: !!item.create };
  if (item.create && item.cwd) body.cwd = item.cwd;
  if (pin) body.pin = pin;
  const data = await api('/api/ticket', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return data.url;
}

async function tryOpen(item, hint) {
  try {
    const url = await fetchTicketUrl(item, '');
    openTermTab(resolveTermUrl(url));
    flash('已在新标签打开会话');
    await refresh();
  } catch (e) {
    if (e && e.data && e.data.need_pin) {
      askPin(item, hint);
      return;
    }
    flash(String(e.message || e), true);
  }
}

async function submitPin() {
  if (!pending || !pending.item) return;
  const pin = document.getElementById('pinInput').value || '';
  if (!pin) { flash('验证失败', true); return; }
  const item = pending.item;
  const btn = document.getElementById('pinConfirm');
  btn.disabled = true;
  try {
    const url = await fetchTicketUrl(item, pin);
    closePin();
    openTermTab(resolveTermUrl(url));
    flash('已在新标签打开会话');
    await refresh();
  } catch (e) {
    flash(String(e.message || e) === '需要验证' || (e.data && e.data.need_pin) ? '验证失败' : String(e.message || e), true);
    document.getElementById('pinInput').value = '';
    document.getElementById('pinInput').focus();
  } finally {
    btn.disabled = false;
  }
}

function selectedRows() {
  const byKey = new Map(mergedRows().map(r => [rowKey(r), r]));
  return [...rowSelected].map(k => byKey.get(k)).filter(Boolean);
}

function renderList() {
  const wrap = document.getElementById('listWrap');
  const all = mergedRows();
  const filtered = sortedRows(all.filter(matchKw));
  if (!all.length) {
    wrap.innerHTML = '<p class="empty">暂无会话，点击上方「新建会话」开始。</p>';
    return;
  }
  if (!filtered.length) {
    wrap.innerHTML = '<p class="empty">无匹配会话</p>';
    return;
  }
  const keys = filtered.map(rowKey);
  const allChecked = keys.length && keys.every(k => rowSelected.has(k));
  let html = '<table><thead><tr>' +
    '<th class="chk"><input type="checkbox" id="rowCheckAll"' + (allChecked ? ' checked' : '') + '></th>' +
    '<th class="sortable status-col" data-sort="status">状态' + sortArrow('status') + '</th>' +
    '<th class="sortable name-col" data-sort="name">名称' + sortArrow('name') + '</th>' +
    '<th class="sortable path-col" data-sort="path">路径' + sortArrow('path') + '</th>' +
    '<th class="pages-col" title="每会话可单独配置；未配置则用全局默认">回看</th>' +
    '<th class="sortable time-col" data-sort="time">时间' + sortArrow('time') + '</th>' +
    '<th class="act-col"></th></tr></thead><tbody>';
  for (const s of filtered) {
    const key = rowKey(s);
    const checked = rowSelected.has(key) ? ' checked' : '';
    const dot = s.live ? '<span class="dot green" title="可恢复"></span>' : '<span class="dot red" title="已断开"></span>';
    const defPages = Number(cache.default_pages) || 30;
    const inherited = !(s.pages > 0);
    const pagesLabel = inherited ? ('默认 ' + defPages) : (s.pages + ' 页');
    const pagesCls = inherited ? 'pages-edit is-default' : 'pages-edit';
    const pagesTitle = inherited
      ? '未单独配置，跟随全局默认 ' + defPages + ' 页；点击可单独设置'
      : '本会话单独配置 ' + s.pages + ' 页；点击可改回跟随全局';
    html += `<tr>
      <td class="chk"><input type="checkbox" class="row-chk" data-key="${esc(key)}"${checked}></td>
      <td class="status-cell">${dot}</td>
      <td class="name-cell"><button type="button" class="name-edit" data-rename="${esc(s.name)}" title="点击修改名称">${esc(s.name)}</button></td>
      <td class="path" title="${esc(s.path || '')}">${esc(s.path || '-')}</td>
      <td class="pages-cell"><button type="button" class="${pagesCls}" data-pages="${esc(key)}" title="${esc(pagesTitle)}">${esc(pagesLabel)}</button></td>
      <td class="time-cell">${esc(formatTime(s.time))}</td>
      <td class="actions">
        <button type="button" data-open-row="${esc(key)}">open</button>
        <button type="button" class="secondary" data-clone-name="${esc(s.name)}" data-clone-cwd="${esc(s.path || s.cwd || '')}" title="克隆：沿用此路径新建会话">克隆</button>
        ${s.live
          ? `<button type="button" class="danger" data-stop="${esc(s.name)}">停止</button>`
          : `<button type="button" class="danger" data-del="${esc(key)}">删除</button>`}
      </td>
    </tr>`;
  }
  html += '</tbody></table>';
  wrap.innerHTML = html;

  wrap.querySelectorAll('th.sortable').forEach(th => {
    th.onclick = () => {
      const col = th.getAttribute('data-sort');
      if (sortKey === col) sortDir = -sortDir;
      else { sortKey = col; sortDir = 1; }
      paint();
    };
  });

  const syncAll = () => {
    const boxes = [...wrap.querySelectorAll('.row-chk')];
    const allBox = document.getElementById('rowCheckAll');
    if (allBox) allBox.checked = boxes.length > 0 && boxes.every(b => b.checked);
  };
  wrap.querySelectorAll('.row-chk').forEach(box => {
    // Shift+点击时避免连带选中表格文字
    box.addEventListener('mousedown', (e) => {
      if (e.shiftKey) e.preventDefault();
    });
    box.addEventListener('click', (e) => {
      const boxes = [...wrap.querySelectorAll('.row-chk')];
      const idx = boxes.indexOf(box);
      if (e.shiftKey && lastRowChkIndex !== null && lastRowChkIndex !== idx) {
        const start = Math.min(lastRowChkIndex, idx);
        const end = Math.max(lastRowChkIndex, idx);
        const check = box.checked;
        for (let j = start; j <= end; j++) {
          boxes[j].checked = check;
          const key = boxes[j].getAttribute('data-key');
          if (check) rowSelected.add(key); else rowSelected.delete(key);
        }
      } else {
        const key = box.getAttribute('data-key');
        if (box.checked) rowSelected.add(key); else rowSelected.delete(key);
      }
      lastRowChkIndex = idx;
      syncAll();
    });
  });
  const allBox = document.getElementById('rowCheckAll');
  if (allBox) {
    allBox.onchange = () => {
      wrap.querySelectorAll('.row-chk').forEach(box => {
        box.checked = allBox.checked;
        const key = box.getAttribute('data-key');
        if (allBox.checked) rowSelected.add(key); else rowSelected.delete(key);
      });
      lastRowChkIndex = null;
    };
  }

  wrap.querySelectorAll('[data-open-row]').forEach(btn => {
    btn.onclick = () => {
      const meta = parseRowKey(btn.getAttribute('data-open-row'));
      const item = { name: meta.name, create: !meta.live, cwd: meta.live ? '' : (meta.cwd || '') };
      tryOpen(item, '');
    };
  });
  wrap.querySelectorAll('[data-rename]').forEach(btn => {
    btn.onclick = () => openRenameModal(btn.getAttribute('data-rename') || '');
  });
  wrap.querySelectorAll('[data-clone-name]').forEach(btn => {
    btn.onclick = () => openCloneModal(btn.getAttribute('data-clone-name') || '', btn.getAttribute('data-clone-cwd') || '');
  });
  wrap.querySelectorAll('[data-pages]').forEach(btn => {
    btn.onclick = () => openPagesModal(parseRowKey(btn.getAttribute('data-pages')));
  });
  wrap.querySelectorAll('[data-stop]').forEach(btn => {
    btn.onclick = async () => {
      const name = btn.getAttribute('data-stop');
      if (!confirm('停止会话「' + name + '」？其中的进程会被结束。')) return;
      try {
        await api('/api/sessions/' + encodeURIComponent(name), { method: 'DELETE' });
        flash('已停止 ' + name);
        await refresh();
      } catch (e) { flash(String(e.message || e), true); }
    };
  });
  wrap.querySelectorAll('[data-del]').forEach(btn => {
    btn.onclick = async () => {
      const meta = parseRowKey(btn.getAttribute('data-del'));
      if (!confirm('删除记录「' + meta.name + '」？仅删记录。')) return;
      try {
        const qs = new URLSearchParams({ name: meta.name, cwd: meta.cwd || '' });
        await api('/api/history?' + qs.toString(), { method: 'DELETE' });
        rowSelected.delete(btn.getAttribute('data-del'));
        flash('已删除 ' + meta.name);
        await refresh();
      } catch (e) { flash(String(e.message || e), true); }
    };
  });
}

function paint() {
  const gbtn = document.getElementById('btnDefaultPages');
  if (gbtn) gbtn.textContent = '全局回看：' + defaultPages() + ' 页';
  renderList();
}

function renderLanBar() {
  const el = document.getElementById('lanBar');
  if (!el) return;
  const lan = cache && cache.lan_ip;
  const mport = location.port || (location.protocol === 'https:' ? '443' : '80');
  if (!lan) { el.hidden = true; return; }
  if (offTunnel()) {
    // 已在局域网/本机直连：终端会以 http://<本机>:<ttyd端口> 打开，延迟更低
    el.innerHTML = '局域网直连模式已启用 <span class="muted">· 打开终端将走本机地址，延迟更低</span>';
  } else {
    // 经公网隧道访问：提示同网设备可换用更快的局域网地址
    el.innerHTML = '同一局域网内可改用更低延迟的地址访问本页：<code>http://' + esc(lan) + ':' + esc(mport) + '/</code>';
  }
  el.hidden = false;
}

async function refresh() {
  const data = await api('/api/sessions');
  cache = data;
  const alive = new Set(mergedRows().map(rowKey));
  for (const k of [...rowSelected]) {
    if (!alive.has(k)) rowSelected.delete(k);
  }
  renderLanBar();
  paint();
}

function closeNameModal() {
  nameModalMode = null;
  document.getElementById('nameModal').hidden = true;
  document.getElementById('nameInput').value = '';
}

function openNameModal() {
  nameModalMode = { type: 'create' };
  document.getElementById('nameTitle').textContent = '新建会话';
  document.getElementById('nameHint').textContent = '输入会话名称';
  document.getElementById('nameConfirm').textContent = '下一步';
  const modal = document.getElementById('nameModal');
  const input = document.getElementById('nameInput');
  modal.hidden = false;
  input.value = '';
  setTimeout(() => input.focus(), 50);
}

function openRenameModal(oldName) {
  if (!oldName) return;
  nameModalMode = { type: 'rename', oldName };
  document.getElementById('nameTitle').textContent = '重命名会话';
  document.getElementById('nameHint').textContent = '当前名称：' + oldName;
  document.getElementById('nameConfirm').textContent = '保存';
  const modal = document.getElementById('nameModal');
  const input = document.getElementById('nameInput');
  modal.hidden = false;
  input.value = oldName;
  setTimeout(() => { input.focus(); input.select(); }, 50);
}

function openCloneModal(srcName, cwd) {
  nameModalMode = { type: 'clone', cwd: cwd || '', srcName: srcName || '' };
  document.getElementById('nameTitle').textContent = '克隆会话';
  document.getElementById('nameHint').textContent = '沿用路径 ' + (cwd || '(默认)') + '，请输入新会话名';
  document.getElementById('nameConfirm').textContent = '创建';
  const modal = document.getElementById('nameModal');
  const input = document.getElementById('nameInput');
  modal.hidden = false;
  input.value = '';
  setTimeout(() => input.focus(), 50);
}

async function submitNameModal() {
  const name = (document.getElementById('nameInput').value || '').trim();
  if (!name) { flash('请输入会话名', true); return; }
  if (!isValidSessionName(name)) {
    flash('会话名支持中英文、数字、_ -，最长 64', true);
    return;
  }
  const mode = nameModalMode;
  if (mode && mode.type === 'rename') {
    if (name === mode.oldName) { closeNameModal(); return; }
    const btn = document.getElementById('nameConfirm');
    btn.disabled = true;
    try {
      await api('/api/rename', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ old_name: mode.oldName, new_name: name }),
      });
      closeNameModal();
      flash('已重命名为 ' + name);
      await refresh();
    } catch (e) {
      flash(String(e.message || e), true);
    } finally {
      btn.disabled = false;
    }
    return;
  }
  closeNameModal();
  const cwd = (mode && mode.type === 'clone') ? (mode.cwd || cache.default_path || '') : (cache.default_path || '');
  tryOpen({ name, create: true, cwd }, '');
}

// { scope:'session', name, cwd, live } | { scope:'global' }
let pagesModalTarget = null;

function defaultPages() {
  return Number(cache.default_pages) || 30;
}

function maxPages() {
  return Number(cache.max_pages) || 2000;
}

function closePagesModal() {
  pagesModalTarget = null;
  document.getElementById('pagesModal').hidden = true;
  document.getElementById('pagesInput').value = '';
}

function showPagesModal(input, cur) {
  input.value = cur > 0 ? String(cur) : '';
  document.getElementById('pagesModal').hidden = false;
  setTimeout(() => { input.focus(); input.select(); }, 50);
}

function openPagesModal(meta) {
  if (!meta || !meta.name) return;
  const row = mergedRows().find(r => r.name === meta.name && (r.cwd || '') === (meta.cwd || '') && r.live === meta.live);
  pagesModalTarget = { scope: 'session', name: meta.name, cwd: meta.cwd || '', live: meta.live };
  document.getElementById('pagesTitle').textContent = '回看页数 · ' + meta.name;
  document.getElementById('pagesHint').textContent =
    '本会话滚轮回看的历史页数。留空 = 不单独配置，跟随全局默认（当前 ' + defaultPages() + ' 页）。';
  document.getElementById('pagesInput').placeholder = '留空 = 跟随全局 ' + defaultPages();
  const rst = document.getElementById('pagesReset');
  rst.hidden = false;
  rst.textContent = '跟随全局';
  showPagesModal(document.getElementById('pagesInput'), row && row.pages > 0 ? row.pages : 0);
}

function openGlobalPagesModal() {
  pagesModalTarget = { scope: 'global' };
  const envDef = Number(cache.env_default_pages) || 30;
  document.getElementById('pagesTitle').textContent = '全局默认回看页数';
  document.getElementById('pagesHint').textContent =
    '所有「未单独配置」的会话都用这个值（1~' + maxPages() + ' 页）。改完立即生效，下次打开终端起作用。';
  document.getElementById('pagesInput').placeholder = '例如 ' + envDef;
  const rst = document.getElementById('pagesReset');
  rst.hidden = false;
  rst.textContent = '恢复 ' + envDef;
  showPagesModal(document.getElementById('pagesInput'), defaultPages());
}

async function savePages(pages) {
  if (!pagesModalTarget) return;
  const t = pagesModalTarget;
  const btn = document.getElementById('pagesConfirm');
  const rst = document.getElementById('pagesReset');
  btn.disabled = true; rst.disabled = true;
  try {
    const body = t.scope === 'global'
      ? { scope: 'global', pages }
      : { scope: 'session', name: t.name, cwd: t.cwd, live: t.live, pages };
    await api('/api/pages', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    closePagesModal();
    if (t.scope === 'global') {
      flash('全局默认已设为 ' + pages + ' 页，未单独配置的会话下次进入生效');
    } else if (pages === 0) {
      flash('「' + t.name + '」已改为跟随全局默认');
    } else {
      flash('已设置「' + t.name + '」回看 ' + pages + ' 页，下次进入生效');
    }
    await refresh();
  } catch (e) {
    flash(String(e.message || e), true);
  } finally {
    btn.disabled = false; rst.disabled = false;
  }
}

function resetPages() {
  if (!pagesModalTarget) return;
  // 会话级：0 = 清除配置跟随全局；全局级：回落 .env 里的初始默认
  if (pagesModalTarget.scope === 'global') savePages(Number(cache.env_default_pages) || 30);
  else savePages(0);
}

function submitPagesModal() {
  if (!pagesModalTarget) return;
  const raw = (document.getElementById('pagesInput').value || '').trim();
  const isGlobal = pagesModalTarget.scope === 'global';
  if (!raw) {
    if (isGlobal) { flash('全局默认必须填写页数', true); return; }
    savePages(0);   // 会话级留空 = 跟随全局
    return;
  }
  if (!/^\d+$/.test(raw)) { flash('页数必须是正整数', true); return; }
  const n = parseInt(raw, 10);
  if (n < 1) { flash('页数至少 1；如需跟随全局请留空', true); return; }
  if (n > maxPages()) { flash('页数最多 ' + maxPages(), true); return; }
  savePages(n);
}

document.getElementById('searchQ').addEventListener('input', paint);
document.getElementById('btnCreate').onclick = () => openNameModal();
document.getElementById('nameCancel').onclick = closeNameModal;
document.getElementById('nameConfirm').onclick = submitNameModal;
document.getElementById('nameInput').addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter') submitNameModal();
  if (ev.key === 'Escape') closeNameModal();
});
document.getElementById('nameModal').addEventListener('click', (ev) => {
  if (ev.target.id === 'nameModal') closeNameModal();
});
document.getElementById('pagesCancel').onclick = closePagesModal;
document.getElementById('pagesReset').onclick = resetPages;
document.getElementById('btnDefaultPages').onclick = openGlobalPagesModal;
document.getElementById('pagesConfirm').onclick = submitPagesModal;
document.getElementById('pagesInput').addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter') submitPagesModal();
  if (ev.key === 'Escape') closePagesModal();
});
document.getElementById('pagesModal').addEventListener('click', (ev) => {
  if (ev.target.id === 'pagesModal') closePagesModal();
});
document.getElementById('btnBulkDel').onclick = async () => {
  const rows = selectedRows();
  if (!rows.length) { flash('请先勾选会话', true); return; }
  if (!confirm('彻底删除选中的 ' + rows.length + ' 个会话？绿点会结束进程且不留记录，红点仅删记录。')) return;
  try {
    for (const r of rows) {
      if (r.live) {
        await api('/api/sessions/' + encodeURIComponent(r.name) + '?purge=1', { method: 'DELETE' });
      } else {
        const qs = new URLSearchParams({ name: r.name, cwd: r.cwd || '' });
        await api('/api/history?' + qs.toString(), { method: 'DELETE' });
      }
      rowSelected.delete(rowKey(r));
    }
    flash('已删除 ' + rows.length + ' 项');
    await refresh();
  } catch (e) { flash(String(e.message || e), true); }
};
document.getElementById('pinCancel').onclick = closePin;
document.getElementById('pinConfirm').onclick = () => submitPin();
document.getElementById('pinInput').addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter') submitPin();
  if (ev.key === 'Escape') closePin();
});
document.getElementById('pinModal').addEventListener('click', (ev) => {
  if (ev.target.id === 'pinModal') closePin();
});

refresh().catch(e => flash(String(e.message || e), true));
setInterval(() => { refresh().catch(() => {}); }, 8000);
</script>
</body>
</html>
"""

MANAGE_HTML = MANAGE_HTML.replace("__FAVICON__", favicon_links(), 1)


# ---------- 前台交互式工具识别 ----------
# tmux 的 #{pane_current_command} 不可靠：claude 会把进程名改成版本号（如 2.1.220），
# codex / cursor-agent 又都显示成 node。所以走 pane 的 tty，用 ps 找真正的前台进程，
# 再按完整 argv 判断是什么工具。
TOOL_RULES: list[tuple[str, str, str]] = [
    # (kind, 友好名, 在 argv 里匹配的正则)
    ("claude", "Claude", r"(^|/)claude(\s|$)"),
    ("codex", "Codex", r"(^|/)codex(\s|$|/)"),
    ("opencode", "OpenCode", r"(^|/)opencode(\s|$)"),
    ("cursor", "Cursor Agent", r"cursor-agent|(^|/)agent\s+--use-system-ca"),
    ("gemini", "Gemini CLI", r"(^|/)gemini(\s|$)"),
    ("aider", "Aider", r"(^|/)aider(\s|$)"),
    ("ipython", "IPython", r"(^|/)ipython[\d.]*(\s|$)"),
    ("python", "Python", r"(^|/)python[\d.]*(\s|$)"),
    ("node", "Node", r"(^|/)node(\s|$)"),
    ("deno", "Deno", r"(^|/)deno(\s|$)"),
    ("bun", "Bun", r"(^|/)bun(\s|$)"),
    ("scala", "Scala", r"(^|/)(scala|scala-cli|sbt|amm)[\d.]*(\s|$)"),
    ("ruby", "Ruby", r"(^|/)(irb|pry|ruby)(\s|$)"),
    ("php", "PHP", r"(^|/)php(\s|$)"),
    ("db", "psql", r"(^|/)psql(\s|$)"),
    ("db", "MySQL", r"(^|/)mysql(\s|$)"),
    ("db", "SQLite", r"(^|/)sqlite3?(\s|$)"),
    ("db", "Redis", r"(^|/)redis-cli(\s|$)"),
    ("db", "mongosh", r"(^|/)mongosh(\s|$)"),
    ("editor", "Neovim", r"(^|/)nvim(\s|$)"),
    ("editor", "Vim", r"(^|/)vim?(\s|$)"),
    ("editor", "Emacs", r"(^|/)emacs(\s|$)"),
    ("editor", "nano", r"(^|/)nano(\s|$)"),
    ("pager", "less", r"(^|/)(less|more)(\s|$)"),
    ("pager", "man", r"(^|/)man(\s|$)"),
    ("monitor", "top", r"(^|/)(top|htop|btop)(\s|$)"),
    ("k8s", "k9s", r"(^|/)k9s(\s|$)"),
    ("ssh", "SSH", r"(^|/)(ssh|mosh)(\s|$)"),
    ("tail", "tail -f", r"(^|/)tail\s+.*-f|(^|/)tail\s+-f"),
]
SHELL_NAMES = {"sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh", "login", "-zsh", "-bash"}


def label_process(args: str) -> dict[str, str]:
    """完整 argv → {kind, label, cmd}。shell 本身返回 kind=shell（前端不显示）。"""
    argv = (args or "").strip()
    if not argv:
        return {"kind": "", "label": "", "cmd": ""}
    argv0 = argv.split()[0]
    base = argv0.rsplit("/", 1)[-1].lstrip("-")
    if base in SHELL_NAMES or argv0.lstrip("-") in SHELL_NAMES:
        return {"kind": "shell", "label": "", "cmd": base}
    for kind, label, pattern in TOOL_RULES:
        if re.search(pattern, argv):
            # python3.14 / scala3 之类带版本的，把版本缀上去更直观
            m = re.match(r"^[a-zA-Z_-]+(\d+\.[\d.]+)$", base)
            if m and label.lower().rstrip("0123456789.") in base.lower():
                return {"kind": kind, "label": f"{label} {m.group(1)}", "cmd": base}
            return {"kind": kind, "label": label, "cmd": base}
    return {"kind": "other", "label": base, "cmd": base}


def tmux_pane(sid: str) -> dict[str, str]:
    cp = subprocess.run(
        ["tmux", "display-message", "-p", "-t", f"{sid}:", "#{pane_tty}\t#{pane_pid}"],
        capture_output=True,
        text=True,
        timeout=3,
        env={**os.environ, "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
    )
    if cp.returncode != 0:
        return {}
    parts = (cp.stdout or "").strip().split("\t")
    if len(parts) < 2:
        return {}
    return {"tty": parts[0], "pid": parts[1]}


def foreground_process(sid: str) -> dict[str, str]:
    """pane 里真正的前台进程：tty 上带 '+'（前台进程组）且父进程是 pane 内 shell 的那个。"""
    pane = tmux_pane(sid)
    tty = pane.get("tty", "")
    pane_pid = pane.get("pid", "")
    if not tty or not pane_pid:
        return {"kind": "", "label": "", "cmd": ""}
    cp = subprocess.run(
        ["ps", "-t", tty.replace("/dev/", ""), "-o", "pid=,ppid=,stat=,args="],
        capture_output=True,
        text=True,
        timeout=3,
    )
    if cp.returncode != 0:
        return {"kind": "", "label": "", "cmd": ""}
    rows = []
    for line in (cp.stdout or "").splitlines():
        fields = line.strip().split(None, 3)
        if len(fields) < 4:
            continue
        rows.append({"pid": fields[0], "ppid": fields[1], "stat": fields[2], "args": fields[3]})
    fg = [r for r in rows if "+" in r["stat"]]
    # 直接挂在 pane shell 下的前台进程 = 用户手敲进入的那个（再深的是它自己拉起的子进程）
    direct = [r for r in fg if r["ppid"] == pane_pid]
    if direct:
        return label_process(direct[0]["args"])
    if any(r["pid"] == pane_pid for r in fg):
        return {"kind": "shell", "label": "", "cmd": "shell"}  # 就在 shell 提示符上
    if fg:
        return label_process(fg[0]["args"])
    return {"kind": "shell", "label": "", "cmd": "shell"}


def run_ctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [SESSION_CTL, *args],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env={**os.environ, "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
    )


def list_sessions() -> list[dict]:
    cp = run_ctl("list")
    sessions = []
    for line in cp.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        name, sid, created, last_open, clients = parts[:5]
        cwd = parts[5] if len(parts) > 5 else ""
        cwd_now = parts[6] if len(parts) > 6 else ""
        pages_raw = parts[7] if len(parts) > 7 else ""
        try:
            pages = int(pages_raw)
        except (TypeError, ValueError):
            pages = 0
        sessions.append(
            {
                "name": name,
                "sid": sid,
                "created": created,
                "last_open": last_open,
                "clients": int(clients or 0),
                "cwd": "" if cwd in ("", "-") else cwd,
                "cwd_now": "" if cwd_now in ("", "-") else cwd_now,
                "pages": pages,
            }
        )
    return sessions


def list_history(exclude_live: bool = True) -> list[dict]:
    """按 name+cwd 去重；若当前会话已存在同名同路径则不显示历史。"""
    path = ROOT / "run" / "session-history.jsonl"
    if not path.exists():
        return []
    by_key: dict[tuple[str, str], dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = (str(obj.get("name") or ""), str(obj.get("cwd") or ""))
        by_key[key] = obj

    live_keys: set[tuple[str, str]] = set()
    if exclude_live:
        for s in list_sessions():
            live_keys.add((s["name"], s.get("cwd") or s.get("cwd_now") or ""))

    items = [v for k, v in by_key.items() if k not in live_keys]
    items.sort(key=lambda x: str(x.get("stopped") or ""), reverse=True)
    return items[:200]


def delete_history(name: str, cwd: str) -> int:
    cp = run_ctl("history-del", name, cwd)
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or "history-del failed")
    out = (cp.stdout or "").strip()
    if out.startswith("deleted:"):
        try:
            return int(out.split(":", 1)[1])
        except ValueError:
            return 0
    return 0


def drop_history_after_open(name: str, cwd: str) -> None:
    """从历史 open 成功后，同名同路径记录移出历史（已变成当前会话）。"""
    try:
        run_ctl("history-del", name, cwd or "")
    except Exception:
        pass


def rename_session(old_name: str, new_name: str) -> str:
    cp = run_ctl("rename", old_name, new_name)
    if cp.returncode != 0:
        raise RuntimeError((cp.stderr or cp.stdout or "rename failed").strip())
    return (cp.stdout or "").strip()


def set_session_pages(name: str, pages: int) -> None:
    cp = run_ctl("set-pages", name, str(pages))
    if cp.returncode != 0:
        raise RuntimeError((cp.stderr or cp.stdout or "set-pages failed").strip())


def set_history_pages(name: str, cwd: str, pages: int) -> None:
    cp = run_ctl("history-set-pages", name, cwd or "", str(pages))
    if cp.returncode != 0:
        raise RuntimeError((cp.stderr or cp.stdout or "history-set-pages failed").strip())


def session_pages(name: str) -> int:
    """当前会话页数：优先运行中会话，其次历史记录；0 表示未设置(用全局默认)。"""
    for s in list_sessions():
        if s["name"] == name:
            return int(s.get("pages") or 0)
    for h in list_history(exclude_live=False):
        if h.get("name") == name:
            try:
                return int(h.get("pages") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def term_url(name: str, ticket: str) -> str:
    url = f"/term/?arg={quote(name, safe='')}&arg={quote(ticket, safe='')}"
    # 未单独配置的会话跟随全局默认；始终显式带上，改全局后下次打开即生效
    pages = session_pages(name) or default_pages()
    if pages > 0:
        url += f"&pages={pages}"
    return url


def parse_cookies(header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (header or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def cleanup_paste_images(max_age_sec: int = 86400, keep_newest: int = 40) -> None:
    PASTE_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(PASTE_DIR.glob("wt-paste-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    now = time.time()
    for i, p in enumerate(files):
        try:
            age = now - p.stat().st_mtime
            if i >= keep_newest or age > max_age_sec:
                p.unlink(missing_ok=True)
        except OSError:
            pass


def png_dimensions(path: Path) -> tuple[int, int]:
    cp = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        return 0, 0
    w = h = 0
    for line in (cp.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("pixelWidth:"):
            try:
                w = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("pixelHeight:"):
            try:
                h = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    return w, h


def convert_to_png(src: Path) -> Path:
    """macOS sips 转 PNG；已是 png 则原样返回。"""
    if src.suffix.lower() == ".png":
        return src
    out = src.with_suffix(".png")
    cp = subprocess.run(
        ["sips", "-s", "format", "png", str(src), "--out", str(out)],
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0 or not out.is_file():
        raise RuntimeError((cp.stderr or cp.stdout or "sips 转换失败").strip())
    return out


def set_macos_clipboard_png(path: Path) -> bool:
    """把 PNG 写入 macOS 剪贴板，供同机 Claude Code Ctrl+V 读取。"""
    posix = str(path.resolve())
    # 用 NSData 写入，避免部分环境下 read…as PNGf 丢内容
    script = f'''
set pngPath to "{posix}"
set pngData to (read (POSIX file pngPath) as «class PNGf»)
set the clipboard to pngData
'''
    cp = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if cp.returncode == 0:
        return True
    # 兜底
    script2 = f'set the clipboard to (read (POSIX file "{posix}") as «class PNGf»)'
    cp2 = subprocess.run(["osascript", "-e", script2], capture_output=True, text=True)
    return cp2.returncode == 0


def save_paste_image(
    raw: bytes,
    mime: str,
    *,
    set_clipboard: bool = True,
    min_pixels: int = 64,
) -> tuple[Path, bool, int, int]:
    """保存图片；可选写入系统剪贴板。返回 (path, clipboard_ok, width, height)。"""
    if len(raw) > PASTE_MAX_BYTES:
        raise ValueError(f"图片过大（上限 {PASTE_MAX_BYTES // (1024 * 1024)}MB）")
    if not raw:
        raise ValueError("空图片")
    mime_l = (mime or "image/png").split(";", 1)[0].strip().lower()
    if not mime_l.startswith("image/"):
        raise ValueError("仅支持图片")
    ext = PASTE_MIME_EXT.get(mime_l, ".bin")
    if ext == ".bin":
        if raw.startswith(b"\x89PNG"):
            ext = ".png"
        elif raw.startswith(b"\xff\xd8"):
            ext = ".jpg"
        elif raw.startswith(b"RIFF") and b"WEBP" in raw[:16]:
            ext = ".webp"
        elif raw.startswith(b"GIF8"):
            ext = ".gif"
        else:
            raise ValueError(f"不支持的图片类型: {mime_l}")
    cleanup_paste_images()
    digest = hashlib.sha256(raw).hexdigest()[:12]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    PASTE_DIR.mkdir(parents=True, exist_ok=True)
    src = PASTE_DIR / f"wt-paste-{stamp}-{digest}{ext}"
    src.write_bytes(raw)
    png = convert_to_png(src)
    width, height = png_dimensions(png)
    pixels = width * height
    clip_ok = False
    # 过小图（常见于错误取到预览）禁止写剪贴板，避免覆盖用户原图
    if set_clipboard and pixels >= min_pixels:
        clip_ok = set_macos_clipboard_png(png)
    return png, clip_ok, width, height


class Handler(BaseHTTPRequestHandler):
    server_version = "WebTerminalManage/1.0"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _unauthorized(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Web Terminal"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Unauthorized")

    def _basic_ok(self) -> bool:
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(hdr[6:].encode("ascii")).decode("utf-8")
        except Exception:
            return False
        if ":" not in raw:
            return False
        u, p = raw.split(":", 1)
        return u == USER and p == PASSWORD

    def _check_auth(self) -> bool:
        if self._basic_ok():
            return True
        self._unauthorized()
        return False

    def _check_auth_or_unlock(self) -> bool:
        """终端页粘贴图片：Basic（浏览器缓存）或 PIN unlock Cookie 均可。"""
        if self._basic_ok() or self._has_valid_unlock():
            return True
        self._unauthorized()
        return False

    def _send(self, code: int, body: bytes, content_type: str, extra_headers: list[tuple[str, str]] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra_headers or []:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict | list, extra_headers: list[tuple[str, str]] | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8", extra_headers)

    def _html(self, code: int, html: str) -> None:
        body = html.encode("utf-8")
        self._send(code, body, "text/html; charset=utf-8")

    def _binary(self, code: int, body: bytes, ctype: str) -> None:
        # 图标可长缓存：内容随发布变化时页面里的 data URI 才是主路径
        self._send(code, body, ctype, [("Cache-Control", "public, max-age=86400")])

    def _unlock_cookie_header(self) -> str:
        token, max_age = session_ticket.issue_unlock(ENV)
        # 不强制 Secure：本机 http://127.0.0.1 也可用；公网 HTTPS 下 SameSite=Lax 足够
        return (
            f"{session_ticket.UNLOCK_COOKIE}={token}; Path=/; Max-Age={max_age}; "
            f"HttpOnly; SameSite=Lax"
        )

    def _has_valid_unlock(self) -> bool:
        cookies = parse_cookies(self.headers.get("Cookie", ""))
        return session_ticket.verify_unlock(cookies.get(session_ticket.UNLOCK_COOKIE, ""), ENV)

    def _authorize_pin_or_unlock(self, pin: str) -> tuple[bool, list[tuple[str, str]]]:
        """PIN 正确或 Cookie 未过期则放行；PIN 正确时刷新 24h Cookie。"""
        headers: list[tuple[str, str]] = []
        if pin:
            if not session_ticket.check_pin(pin, ENV):
                return False, []
            headers.append(("Set-Cookie", self._unlock_cookie_header()))
            return True, headers
        if self._has_valid_unlock():
            return True, []
        return False, []

    def _lan_cors_headers(self) -> list[tuple[str, str]]:
        """局域网直连时终端页在 ttyd 端口，粘贴图片是跨源请求。
        仅当 Origin 恰为本机同主机的 ttyd 端口时放行，并允许携带 Cookie。"""
        origin = self.headers.get("Origin", "")
        if not origin:
            return []
        try:
            u = urlparse(origin)
        except ValueError:
            return []
        if u.scheme not in ("http", "https") or not u.hostname:
            return []
        # loopback 也要放行：127.0.0.1:<ttyd> → 127.0.0.1:<manage> 同样是跨源（端口不同）
        want_port = str(u.port or "")
        if want_port != str(TTYD_PORT):
            return []
        return [
            ("Access-Control-Allow-Origin", origin),
            ("Access-Control-Allow-Credentials", "true"),
            ("Vary", "Origin"),
        ]

    def do_OPTIONS(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        cors = self._lan_cors_headers()
        # 局域网/跨端口直连时，终端页对 /api/* 的预检都要能过（带 Authorization 的
        # 请求会触发 preflight），放行范围仍只限同机 ttyd 源
        if path.startswith("/api/") and cors:
            self.send_response(204)
            for k, v in cors:
                self.send_header(k, v)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if not self._check_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path in ("/", "/manage"):
            self._html(200, MANAGE_HTML)
            return
        if path == "/api/sessions":
            self._json(
                200,
                {
                    "sessions": list_sessions(),
                    "history": list_history(),
                    "path_root": PATH_ROOT,
                    "default_path": DEFAULT_PATH,
                    "default_pages": default_pages(),
                    "env_default_pages": _env_default_pages(),
                    "max_pages": MAX_PAGES,
                    "lan_ip": detect_lan_ip(),
                    "ttyd_port": TTYD_PORT,
                    "term_base": TTYD_BASE_PATH + "/",
                    "public_host": PUBLIC_HOST,
                },
            )
            return
        if path in ("/favicon.ico", "/icon.png"):
            try:
                self._binary(200, ICON_PNG.read_bytes(), "image/png")
            except OSError:
                self._json(404, {"error": "no icon"})
            return
        if path == "/icon.svg":
            try:
                self._binary(200, ICON_SVG.read_bytes(), "image/svg+xml")
            except OSError:
                self._json(404, {"error": "no icon"})
            return
        if path == "/api/foreground":
            cors = self._lan_cors_headers()
            name = (parse_qs(parsed.query).get("name") or [""])[0].strip()
            if not NAME_RE.match(name):
                self._json(400, {"error": "无效会话名"}, cors)
                return
            sid = "wt-" + session_ticket.sanitize_name(name)
            try:
                info = foreground_process(sid)
            except (OSError, subprocess.SubprocessError):
                info = {"kind": "", "label": "", "cmd": ""}
            self._json(200, {"ok": True, "name": name, **info}, cors)
            return
        if path == "/api/health":
            self._json(
                200,
                {
                    "ok": True,
                    "public": PUBLIC_HOST,
                    "default_pages": default_pages(),
                    "lan_ip": detect_lan_ip(),
                    "ttyd_port": TTYD_PORT,
                },
            )
            return
        if path == "/api/unlock":
            self._json(200, {"ok": self._has_valid_unlock()})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/api/paste-image":
            if not self._check_auth_or_unlock():
                return
        elif not self._check_auth():
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            data = {}

        if path == "/api/ticket":
            pin = str(data.get("pin") or "")
            name = str(data.get("name") or "").strip()
            create = bool(data.get("create"))
            cwd = str(data.get("cwd") or "").strip()
            if not NAME_RE.match(name):
                self._json(400, {"error": "无效会话名"})
                return
            ok, extra = self._authorize_pin_or_unlock(pin)
            if not ok:
                self._json(403, {"error": "需要验证", "need_pin": True})
                return
            if create:
                args = ["create", name]
                if cwd:
                    args.append(cwd)
                cp = run_ctl(*args)
                if cp.returncode != 0:
                    self._json(400, {"error": (cp.stderr or cp.stdout or "create failed").strip()})
                    return
                drop_history_after_open(name, cwd)
            else:
                live = {s["name"] for s in list_sessions()}
                if name not in live:
                    self._json(404, {"error": "会话不存在或已停止"})
                    return
            try:
                ticket = session_ticket.issue(name, create=False, env=ENV)
            except ValueError as e:
                self._json(500, {"error": str(e)})
                return
            self._json(200, {"ok": True, "ticket": ticket, "url": term_url(name, ticket)}, extra)
            return

        if path == "/api/rename":
            old_name = str(data.get("old_name") or "").strip()
            new_name = str(data.get("new_name") or "").strip()
            if not NAME_RE.match(old_name) or not NAME_RE.match(new_name):
                self._json(400, {"error": "无效会话名"})
                return
            try:
                result = rename_session(old_name, new_name)
            except RuntimeError as e:
                self._json(400, {"error": str(e)})
                return
            self._json(200, {"ok": True, "result": result})
            return

        if path == "/api/pages":
            scope = str(data.get("scope") or "session").strip()
            try:
                pages = int(data.get("pages"))
            except (TypeError, ValueError):
                self._json(400, {"error": "页数必须是整数"})
                return

            if scope == "global":
                # 全局默认：必须是实数页，不能为 0（0 只在会话级表示“不配置”）
                if pages < 1 or pages > MAX_PAGES:
                    self._json(400, {"error": f"全局默认需在 1~{MAX_PAGES} 之间"})
                    return
                try:
                    set_default_pages(pages)
                except OSError as e:
                    self._json(500, {"error": f"写入全局默认失败: {e}"})
                    return
                self._json(200, {"ok": True, "scope": "global", "default_pages": pages})
                return

            name = str(data.get("name") or "").strip()
            cwd = str(data.get("cwd") or "").strip()
            live = bool(data.get("live"))
            if not NAME_RE.match(name):
                self._json(400, {"error": "无效会话名"})
                return
            # 0 = 清除本会话配置，回落全局默认
            if pages < 0 or pages > MAX_PAGES:
                self._json(400, {"error": f"页数需在 0~{MAX_PAGES} 之间(0=用全局默认)"})
                return
            try:
                if live:
                    set_session_pages(name, pages)
                else:
                    set_history_pages(name, cwd, pages)
            except RuntimeError as e:
                self._json(400, {"error": str(e)})
                return
            self._json(
                200,
                {"ok": True, "pages": pages, "inherited": pages == 0, "default_pages": default_pages()},
            )
            return

        if path == "/api/paste-image":
            cors = self._lan_cors_headers()
            mime = str(data.get("mime") or "image/png")
            b64 = str(data.get("image_base64") or "")
            if not b64:
                self._json(400, {"error": "缺少 image_base64"}, cors)
                return
            try:
                img_raw = base64.b64decode(b64, validate=False)
            except Exception:
                self._json(400, {"error": "base64 无效"}, cors)
                return
            # 默认写剪贴板；前端可传 set_clipboard=false 避免用坏图覆盖
            set_clip = data.get("set_clipboard", True)
            if isinstance(set_clip, str):
                set_clip = set_clip.strip().lower() in ("1", "true", "yes")
            else:
                set_clip = bool(set_clip)
            try:
                path_out, clip_ok, width, height = save_paste_image(
                    img_raw, mime, set_clipboard=set_clip
                )
            except ValueError as e:
                self._json(400, {"error": str(e)}, cors)
                return
            except RuntimeError as e:
                self._json(500, {"error": str(e)}, cors)
                return
            self._json(
                200,
                {
                    "ok": True,
                    "path": str(path_out),
                    "clipboard": clip_ok,
                    "width": width,
                    "height": height,
                    "bytes": len(img_raw),
                },
                cors,
            )
            return

        self._json(404, {"error": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._check_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/history" or path.startswith("/api/history"):
            qs = parse_qs(parsed.query)
            name = (qs.get("name") or [""])[0].strip()
            cwd = (qs.get("cwd") or [""])[0]
            if not name:
                self._json(400, {"error": "name required"})
                return
            try:
                n = delete_history(name, cwd)
            except RuntimeError as e:
                self._json(500, {"error": str(e)})
                return
            self._json(200, {"ok": True, "deleted": n})
            return
        if path.startswith("/api/sessions/"):
            name = unquote(path[len("/api/sessions/") :].strip("/"))
            if not name:
                self._json(400, {"error": "name required"})
                return
            qs = parse_qs(parsed.query)
            purge = (qs.get("purge") or [""])[0] in ("1", "true", "yes")
            args = ["kill", name]
            if purge:
                args.append("--purge")
            cp = run_ctl(*args)
            if cp.returncode != 0:
                self._json(500, {"error": cp.stderr.strip() or "kill failed"})
                return
            self._json(200, {"ok": True, "result": cp.stdout.strip()})
            return
        self._json(404, {"error": "not found"})


def main() -> None:
    try:
        session_ticket.get_pin(ENV)
    except ValueError as e:
        raise SystemExit(f".env 配置错误: {e}") from e
    httpd = ThreadingHTTPServer((MANAGE_HOST, MANAGE_PORT), Handler)
    print(f"manage listening on http://{MANAGE_HOST}:{MANAGE_PORT}", flush=True)
    print(f"path_root={PATH_ROOT} default_path={DEFAULT_PATH}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
