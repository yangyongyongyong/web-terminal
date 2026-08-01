/**
 * 顶栏显示「当前正在用的交互式工具」：Claude / Codex / Python / Scala / vim …
 *
 * 判定放在服务端（/api/foreground）：tmux 的 #{pane_current_command} 不可靠
 * （claude 把进程名改成版本号、codex 与 cursor-agent 都显示 node），服务端按
 * pane 的 tty 找真正的前台进程，再按完整 argv 命名。
 *
 * 前端只负责轮询与着色：页面不可见时不轮询；连续失败退避，别刷日志。
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.WtTool = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var POLL_MS = 2500;
  var BACKOFF_MS = 15000;
  var MAX_FAILS = 3;

  var KIND_COLORS = {
    claude: "#e0855b",
    codex: "#8ec7ff",
    cursor: "#c792ea",
    opencode: "#56b6c2",
    gemini: "#a3b8ff",
    aider: "#e6c07b",
    python: "#ffd75f",
    ipython: "#ffd75f",
    node: "#7ee787",
    deno: "#7ee787",
    bun: "#7ee787",
    scala: "#f07178",
    ruby: "#f07178",
    php: "#a3b8ff",
    db: "#61afef",
    editor: "#98c379",
    pager: "#8b9aab",
    monitor: "#e6c07b",
    k8s: "#61afef",
    ssh: "#56b6c2",
    tail: "#8b9aab",
    other: "#9aa7b4",
  };

  function colorForKind(kind) {
    return KIND_COLORS[kind] || KIND_COLORS.other;
  }

  /** 该不该显示（纯函数）：shell 提示符上、拿不到信息时都不显示 */
  function shouldShow(info) {
    if (!info || info.ok === false) return false;
    var kind = String(info.kind || "");
    if (!kind || kind === "shell") return false;
    return !!String(info.label || "").length;
  }

  /** 渲染文案（纯函数） */
  function badgeText(info) {
    return shouldShow(info) ? "▸ " + info.label : "";
  }

  function renderTool(el, info) {
    if (!el) return "";
    var text = badgeText(info);
    el.textContent = text;
    el.style.color = text ? colorForKind(info && info.kind) : "";
    el.style.fontWeight = text ? "700" : "";
    el.title = text ? "当前前台进程: " + ((info && info.cmd) || "") : "";
    return text;
  }

  function apiBase() {
    try {
      if (typeof window !== "undefined" && window.WT_API_BASE) return window.WT_API_BASE;
    } catch (e) {}
    return "";
  }

  function toolUrl(name) {
    return apiBase() + "/api/foreground?name=" + encodeURIComponent(name || "");
  }

  async function fetchTool(name) {
    var res = await fetch(toolUrl(name), { credentials: "include" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    return await res.json();
  }

  /** 轮询并渲染到 #wt-tool；返回 stop 函数 */
  function hookToolIndicator(name, opts) {
    opts = opts || {};
    if (typeof document === "undefined") return null;
    var el = document.getElementById(opts.elementId || "wt-tool");
    if (!el || !name) return null;
    var pollMs = opts.pollMs || POLL_MS;
    var fails = 0;
    var timer = null;
    var stopped = false;

    async function tick() {
      if (stopped) return;
      if (document.hidden) {
        schedule(pollMs);
        return;
      }
      try {
        var info = await fetchTool(name);
        fails = 0;
        renderTool(el, info);
        schedule(pollMs);
      } catch (e) {
        fails += 1;
        if (fails >= MAX_FAILS) renderTool(el, null); // 别一直显示过期状态
        schedule(fails >= MAX_FAILS ? BACKOFF_MS : pollMs);
      }
    }

    function schedule(ms) {
      if (stopped) return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(tick, ms);
    }

    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && !stopped) {
        fails = 0;
        schedule(0);
      }
    });

    tick();
    return function stop() {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }

  return {
    colorForKind: colorForKind,
    shouldShow: shouldShow,
    badgeText: badgeText,
    renderTool: renderTool,
    toolUrl: toolUrl,
    fetchTool: fetchTool,
    hookToolIndicator: hookToolIndicator,
    KIND_COLORS: KIND_COLORS,
    POLL_MS: POLL_MS,
  };
});
