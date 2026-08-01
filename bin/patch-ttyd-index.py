#!/usr/bin/env python3
"""从 ttyd 默认 HTML 注入断线自动重连 + 管理页入口 + 滚轮策略，写出 web/ttyd-index.html"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STOCK = ROOT / "web" / "ttyd-stock.html"
OUT = ROOT / "web" / "ttyd-index.html"
WHEEL_JS = ROOT / "web" / "wt-wheel.js"
PASTE_JS = ROOT / "web" / "wt-paste-image.js"
COPY_JS = ROOT / "web" / "wt-copy.js"
SEARCH_JS = ROOT / "web" / "wt-search.js"
ICON_SVG = ROOT / "web" / "wt-icon-term.svg"
ICON_PNG = ROOT / "web" / "wt-icon-term-64.png"


def favicon_links() -> str:
    """ttyd 自带的 favicon 是黑底黑字，在深色标签栏里等于没有图标；换成高对比图标。

    走 data URI：终端页可能经隧道或局域网 ttyd 端口加载，省掉额外路由与跨源问题。
    """
    import base64

    svg = base64.b64encode(ICON_SVG.read_bytes()).decode()
    png = base64.b64encode(ICON_PNG.read_bytes()).decode()
    return (
        f'<link id="wt-favicon" rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,{svg}">'
        f'<link rel="alternate icon" type="image/png" sizes="64x64" href="data:image/png;base64,{png}">'
        f'<link rel="apple-touch-icon" href="data:image/png;base64,{png}">'
    )


def public_host() -> str:
    return _env_val("PUBLIC_HOST", "term.lucadesign.uk")


def manage_port() -> str:
    return _env_val("MANAGE_PORT", "7690")


def _env_val(key: str, default: str) -> str:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                val = line.split("=", 1)[1].strip().split("#", 1)[0].strip()
                return val or default
    return default


def wheel_js_for_inject() -> str:
    """嵌入浏览器：去掉 CommonJS 导出依赖即可。"""
    raw = WHEEL_JS.read_text(encoding="utf-8")
    if "decideWheelAction" not in raw or "swallow" not in raw:
        raise SystemExit(f"{WHEEL_JS} 缺少滚轮防方向键策略")
    return raw


def paste_js_for_inject() -> str:
    raw = PASTE_JS.read_text(encoding="utf-8")
    if "hookPasteImage" not in raw or "paste-image" not in raw:
        raise SystemExit(f"{PASTE_JS} 缺少图片粘贴逻辑")
    for need in ("hookKeys", "decideKeyAction", "decidePasteAction", "detectPlatform"):
        if need not in raw:
            raise SystemExit(f"{PASTE_JS} 缺少按系统分流的键位逻辑: {need}")
    return raw


def copy_js_for_inject() -> str:
    raw = COPY_JS.read_text(encoding="utf-8")
    for need in ("hookCopyOnSelect", "decideCopyAction", "copyText"):
        if need not in raw:
            raise SystemExit(f"{COPY_JS} 缺少选中即复制逻辑: {need}")
    return raw


def search_js_for_inject() -> str:
    raw = SEARCH_JS.read_text(encoding="utf-8")
    for need in ("hookSearch", "decideSearchKeyAction", "findMatches", "registerDecoration"):
        if need not in raw:
            raise SystemExit(f"{SEARCH_JS} 缺少搜索逻辑: {need}")
    return raw


INJECT_HEAD = r"""
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
  .xterm-viewport { scroll-behavior: auto !important; }
</style>
<div id="wt-bar">
  <strong>web-terminal</strong>
  <a id="wt-manage-link" href="https://__PUBLIC_HOST__/" target="_blank" rel="noopener">会话管理</a>
  <span id="wt-session"></span>
  <span id="wt-status">连接中…</span>
  <span id="wt-hint" style="color:#8b9aab">滚轮回看 · 支持粘贴图片</span>
</div>
<script id="wt-wheel">
window.WT_SCROLLBACK_PAGES = __SCROLLBACK_PAGES__;
(function () {
  // 每会话页数：管理页在 open 时把 &pages=N 拼进 URL，优先于全局默认
  try {
    var p = new URLSearchParams(location.search).get('pages');
    var n = p ? parseInt(p, 10) : 0;
    if (n && n > 0) window.WT_SCROLLBACK_PAGES = n;
  } catch (e) {}
  try {
    // 按键文案依赖客户端系统，等 WtPasteImage 载入后由 wt-reconnect 覆盖
    var hint = document.getElementById('wt-hint');
    if (hint) hint.textContent = '滚轮回看约' + window.WT_SCROLLBACK_PAGES + '页';
  } catch (e) {}
})();
__WHEEL_JS__
</script>
<script id="wt-api-base">
(function () {
  // 终端页可能经隧道(同源)或局域网直连(页面在 ttyd 端口)加载。
  // 同源→相对路径即可；局域网→图片粘贴等 API 必须打到管理端口。
  try {
    var pub = "__PUBLIC_HOST__";
    var mport = "__MANAGE_PORT__";
    if (location.hostname && location.hostname !== pub && mport) {
      window.WT_API_BASE = location.protocol + '//' + location.hostname + ':' + mport;
    } else {
      window.WT_API_BASE = '';
    }
  } catch (e) { window.WT_API_BASE = ''; }
})();
</script>
<script id="wt-paste-image">
__PASTE_JS__
</script>
<script id="wt-copy">
__COPY_JS__
</script>
<script id="wt-search">
__SEARCH_JS__
</script>
<script id="wt-reconnect">
(function () {
  document.body.classList.add('wt-has-bar');
  var statusEl = document.getElementById('wt-status');
  var sessionEl = document.getElementById('wt-session');
  var params = new URLSearchParams(location.search);
  var args = params.getAll('arg');
  var sessionName = args[0] || 'main';
  sessionEl.textContent = '会话: ' + sessionName;
  // 局域网直连时，管理入口改指向本机地址（否则隧道域名在局域网内不可达/走公网）
  try {
    if (window.WT_API_BASE) {
      var mlink = document.getElementById('wt-manage-link');
      if (mlink) mlink.href = window.WT_API_BASE + '/';
    }
  } catch (e) {}
  // 让 Chrome 标签页名 = 会话名，方便识别。shell 的 OSC 标题会不断改写它，故周期性钉回。
  try {
    document.title = sessionName;
    setInterval(function () {
      if (document.title !== sessionName) document.title = sessionName;
    }, 1000);
  } catch (e) {}

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
      function pin() {
        if (window.term && window.WtWheel && window.WtWheel.pinBottomDuringAttach) {
          window.WtWheel.pinBottomDuringAttach(window.term, 2000);
          return true;
        }
        return false;
      }
      if (!pin()) {
        var tries = 0;
        var waitTerm = setInterval(function () {
          tries += 1;
          if (pin() || tries > 40) clearInterval(waitTerm);
        }, 50);
      }
    });
    ws.addEventListener('close', function () {
      if (leaving) return;
      scheduleReload('连接断开');
    });
    ws.addEventListener('error', function () {});
    return ws;
  }
  WrappedWS.prototype = NativeWS.prototype;
  WrappedWS.CONNECTING = NativeWS.CONNECTING;
  WrappedWS.OPEN = NativeWS.OPEN;
  WrappedWS.CLOSING = NativeWS.CLOSING;
  WrappedWS.CLOSED = NativeWS.CLOSED;
  window.WebSocket = WrappedWS;

  var hookTries = 0;
  var keysHooked = false;
  var copyHooked = false;
  var searchHooked = false;
  var hookTimer = setInterval(function () {
    hookTries += 1;
    var ok = window.WtWheel && window.WtWheel.hookLocalWheel(function () { return window.term; });
    // 键位改写（非 mac 客户端 Ctrl+V 贴文本 / Alt+V 贴图）必须挂在 term 上
    if (!keysHooked && window.WtPasteImage && window.WtPasteImage.hookKeys) {
      keysHooked = !!window.WtPasteImage.hookKeys(function () { return window.term; });
    }
    // 选中即复制：走 xterm 的 onSelectionChange
    if (!copyHooked && window.WtCopy && window.WtCopy.hookCopyOnSelect) {
      copyHooked = !!window.WtCopy.hookCopyOnSelect(function () { return window.term; });
    }
    // 搜索：⌘F / Ctrl+F，document 捕获阶段处理，不占用 xterm 的自定义键处理器
    if (!searchHooked && window.WtSearch && window.WtSearch.hookSearch) {
      searchHooked = !!window.WtSearch.hookSearch(function () { return window.term; });
    }
    if ((ok && keysHooked && copyHooked && searchHooked) || hookTries > 80) clearInterval(hookTimer);
  }, 250);

  if (window.WtPasteImage && window.WtPasteImage.hookPasteImage) {
    window.WtPasteImage.hookPasteImage(function () { return window.term; });
  }

  // 顶栏提示按客户端系统给出正确按键
  try {
    if (window.WtPasteImage && window.WtPasteImage.hintText) {
      var hintEl = document.getElementById('wt-hint');
      if (hintEl) hintEl.textContent = window.WtPasteImage.hintText(window.WT_SCROLLBACK_PAGES);
    }
  } catch (e) {}
})();
</script>
"""


def default_pages() -> str:
    """全局默认回看页数：run/scrollback-pages(管理页可改) > .env SCROLLBACK_PAGES > 30。"""
    rt = ROOT / "run" / "scrollback-pages"
    if rt.exists():
        raw = rt.read_text(encoding="utf-8").strip()
        if raw.isdigit() and int(raw) >= 1:
            return raw
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("SCROLLBACK_PAGES="):
                val = (line.split("=", 1)[1].strip() or "30").split("#", 1)[0].strip()
                if val.isdigit() and int(val) >= 1:
                    return val
                break
    return "30"


def build_inject() -> str:
    pages = default_pages()
    inject = INJECT_HEAD.replace("__PUBLIC_HOST__", public_host())
    inject = inject.replace("__MANAGE_PORT__", manage_port())
    inject = inject.replace("__SCROLLBACK_PAGES__", pages)
    inject = inject.replace("__WHEEL_JS__", wheel_js_for_inject())
    inject = inject.replace("__PASTE_JS__", paste_js_for_inject())
    inject = inject.replace("__COPY_JS__", copy_js_for_inject())
    return inject.replace("__SEARCH_JS__", search_js_for_inject())


def main() -> int:
    if not STOCK.exists():
        print(f"缺少 {STOCK}，请先运行 bin/ensure-ttyd-index.sh", file=sys.stderr)
        return 1
    if not WHEEL_JS.exists():
        print(f"缺少 {WHEEL_JS}", file=sys.stderr)
        return 1
    if not PASTE_JS.exists():
        print(f"缺少 {PASTE_JS}", file=sys.stderr)
        return 1
    if not COPY_JS.exists():
        print(f"缺少 {COPY_JS}", file=sys.stderr)
        return 1
    if not SEARCH_JS.exists():
        print(f"缺少 {SEARCH_JS}", file=sys.stderr)
        return 1
    for icon in (ICON_SVG, ICON_PNG):
        if not icon.exists():
            print(f"缺少 {icon}", file=sys.stderr)
            return 1
    html = STOCK.read_text(encoding="utf-8")
    if 'id="wt-reconnect"' in html:
        print(f"{STOCK} 已是注入版，请删除后重新抓取 stock", file=sys.stderr)
        return 1
    if "</body>" not in html:
        print("stock HTML 无 </body>", file=sys.stderr)
        return 1
    # 替换 ttyd 默认 favicon（黑底黑字，深色标签栏里看不见）
    import re

    patched, n = re.subn(
        r'<link rel="icon" type="image/png" href="data:image/png;base64,[^"]+"\s*/?>',
        favicon_links(),
        html,
        count=1,
    )
    if n != 1:
        if "wt-favicon" not in patched:
            patched = patched.replace("</title>", "</title>" + favicon_links(), 1)
    patched = patched.replace("</body>", build_inject() + "</body>", 1)
    OUT.write_text(patched, encoding="utf-8")
    print(f"已写入 {OUT} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
