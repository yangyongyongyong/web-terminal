#!/usr/bin/env python3
"""从 ttyd 默认 HTML 注入断线自动重连 + 管理页入口，写出 web/ttyd-index.html"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STOCK = ROOT / "web" / "ttyd-stock.html"
OUT = ROOT / "web" / "ttyd-index.html"


def public_host() -> str:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("PUBLIC_HOST="):
                return line.split("=", 1)[1].strip() or "term.lucadesign.uk"
    return "term.lucadesign.uk"


INJECT = r"""
<style id="wt-chrome">
  #wt-bar {
    position: fixed; top: 0; left: 0; right: 0; z-index: 9999;
    display: flex; align-items: center; gap: 10px;
    height: 32px; padding: 0 12px;
    background: rgba(18, 18, 18, 0.92); color: #ddd;
    font: 12px/32px ui-monospace, SFMono-Regular, Menlo, Monaco, monospace;
    border-bottom: 1px solid #333;
  }
  #wt-bar a { color: #8ec7ff; text-decoration: none; }
  #wt-bar a:hover { text-decoration: underline; }
  #wt-status { margin-left: auto; color: #9ad29a; }
  #wt-status.warn { color: #e6c07b; }
  #wt-status.err { color: #f07178; }
  body.wt-has-bar { padding-top: 32px !important; box-sizing: border-box; }
  body.wt-has-bar #terminal-container,
  body.wt-has-bar .xterm { height: calc(100vh - 32px) !important; }
</style>
<div id="wt-bar">
  <strong>web-terminal</strong>
  <a href="https://__PUBLIC_HOST__/" target="_blank" rel="noopener">会话管理</a>
  <span id="wt-session"></span>
  <span id="wt-status">连接中…</span>
</div>
<script id="wt-reconnect">
(function () {
  document.body.classList.add('wt-has-bar');
  var statusEl = document.getElementById('wt-status');
  var sessionEl = document.getElementById('wt-session');
  var params = new URLSearchParams(location.search);
  var args = params.getAll('arg');
  var sessionName = args[0] || 'main';
  sessionEl.textContent = '会话: ' + sessionName;

  var MAX_DELAY = 30000;
  var delay = 1000;
  var leaving = false;
  var timer = null;
  var openedOnce = false;

  function setStatus(text, cls) {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.className = cls || '';
  }

  function markLeaving() { leaving = true; if (timer) clearTimeout(timer); }
  window.addEventListener('pagehide', markLeaving);
  window.addEventListener('beforeunload', markLeaving);

  function scheduleReload(reason) {
    if (leaving) return;
    if (timer) clearTimeout(timer);
    setStatus(reason + '，' + Math.round(delay / 1000) + 's 后重连', 'warn');
    var wait = delay;
    delay = Math.min(delay * 2, MAX_DELAY);
    timer = setTimeout(function () {
      if (!leaving) location.reload();
    }, wait);
  }

  window.addEventListener('online', function () {
    if (!leaving && openedOnce) {
      delay = 1000;
      scheduleReload('网络已恢复');
    }
  });

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && openedOnce && !leaving && timer) {
      clearTimeout(timer);
      timer = null;
      delay = 1000;
      setStatus('页面可见，立即重连…', 'warn');
      location.reload();
    }
  });

  var NativeWS = window.WebSocket;
  function WrappedWS(url, protocols) {
    var ws = (protocols === undefined) ? new NativeWS(url) : new NativeWS(url, protocols);
    ws.addEventListener('open', function () {
      openedOnce = true;
      delay = 1000;
      if (timer) { clearTimeout(timer); timer = null; }
      setStatus('已连接', '');
    });
    ws.addEventListener('close', function () {
      if (leaving) return;
      scheduleReload('连接断开');
    });
    ws.addEventListener('error', function () {
      // 覆盖 ttyd 默认「error 后禁用自动重连」；真正重连由 close/reload 负责
    });
    return ws;
  }
  WrappedWS.prototype = NativeWS.prototype;
  WrappedWS.CONNECTING = NativeWS.CONNECTING;
  WrappedWS.OPEN = NativeWS.OPEN;
  WrappedWS.CLOSING = NativeWS.CLOSING;
  WrappedWS.CLOSED = NativeWS.CLOSED;
  window.WebSocket = WrappedWS;
})();
</script>
"""


def main() -> int:
    if not STOCK.exists():
        print(f"缺少 {STOCK}，请先运行 bin/ensure-ttyd-index.sh", file=sys.stderr)
        return 1
    html = STOCK.read_text(encoding="utf-8")
    if "id=\"wt-reconnect\"" in html:
        OUT.write_text(html, encoding="utf-8")
        print(f"已是注入版，复制到 {OUT}")
        return 0
    if "</body>" not in html:
        print("stock HTML 无 </body>", file=sys.stderr)
        return 1
    inject = INJECT.replace("__PUBLIC_HOST__", public_host())
    patched = html.replace("</body>", inject + "</body>", 1)
    OUT.write_text(patched, encoding="utf-8")
    print(f"已写入 {OUT} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
