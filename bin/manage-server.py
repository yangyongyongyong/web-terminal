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
  main { max-width: min(1320px, 96vw); margin: 0 auto; padding: 32px 24px 80px; }
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
  th.name-col, td.name-cell { width: 18%; }
  th.path-col { width: auto; }
  th.time-col, td.time-cell {
    width: 148px; white-space: nowrap;
    font-variant-numeric: tabular-nums;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px; color: var(--muted);
  }
  th.act-col, td.actions { width: 148px; }
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
  }
  .name-edit:hover { color: var(--accent); border-bottom-color: var(--accent); }
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
        <button id="btnBulkDel" class="danger" type="button">删除选中</button>
      </div>
    </div>
    <p class="hint" style="margin-top:8px"><span class="dot green"></span> 可恢复 &nbsp;&nbsp; <span class="dot red"></span> 已断开</p>
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

<script>
const termBase = '/term/';
let pending = null;
let nameModalMode = null; // { type: 'create' } | { type: 'rename', oldName }
let cache = { sessions: [], history: [], path_root: '', default_path: '' };
const rowSelected = new Set(); // name + \\u001e + cwd + \\u001e + live|dead
let lastRowChkIndex = null; // 多选：普通点击锚点，供 Shift 范围选取
let sortKey = 'status';
let sortDir = 1; // 1 asc, -1 desc

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
    openTermTab(url);
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
    openTermTab(url);
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
    '<th class="sortable time-col" data-sort="time">时间' + sortArrow('time') + '</th>' +
    '<th class="act-col"></th></tr></thead><tbody>';
  for (const s of filtered) {
    const key = rowKey(s);
    const checked = rowSelected.has(key) ? ' checked' : '';
    const dot = s.live ? '<span class="dot green" title="可恢复"></span>' : '<span class="dot red" title="已断开"></span>';
    html += `<tr>
      <td class="chk"><input type="checkbox" class="row-chk" data-key="${esc(key)}"${checked}></td>
      <td class="status-cell">${dot}</td>
      <td class="name-cell"><button type="button" class="name-edit" data-rename="${esc(s.name)}" title="点击修改名称">${esc(s.name)}</button></td>
      <td class="path" title="${esc(s.path || '')}">${esc(s.path || '-')}</td>
      <td class="time-cell">${esc(formatTime(s.time))}</td>
      <td class="actions">
        <button type="button" data-open-row="${esc(key)}">open</button>
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
  renderList();
}

async function refresh() {
  const data = await api('/api/sessions');
  cache = data;
  const alive = new Set(mergedRows().map(rowKey));
  for (const k of [...rowSelected]) {
    if (!alive.has(k)) rowSelected.delete(k);
  }
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
  tryOpen({ name, create: true, cwd: cache.default_path || '' }, '');
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
        sessions.append(
            {
                "name": name,
                "sid": sid,
                "created": created,
                "last_open": last_open,
                "clients": int(clients or 0),
                "cwd": "" if cwd in ("", "-") else cwd,
                "cwd_now": "" if cwd_now in ("", "-") else cwd_now,
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


def term_url(name: str, ticket: str) -> str:
    return f"/term/?arg={quote(name, safe='')}&arg={quote(ticket, safe='')}"


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
                },
            )
            return
        if path == "/api/health":
            self._json(200, {"ok": True, "public": PUBLIC_HOST})
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

        if path == "/api/paste-image":
            mime = str(data.get("mime") or "image/png")
            b64 = str(data.get("image_base64") or "")
            if not b64:
                self._json(400, {"error": "缺少 image_base64"})
                return
            try:
                img_raw = base64.b64decode(b64, validate=False)
            except Exception:
                self._json(400, {"error": "base64 无效"})
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
                self._json(400, {"error": str(e)})
                return
            except RuntimeError as e:
                self._json(500, {"error": str(e)})
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
