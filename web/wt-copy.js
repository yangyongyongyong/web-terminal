/**
 * 选中即复制：终端里用鼠标选中文本，自动写入客户端剪贴板，并给出「已复制」反馈。
 *
 * 要点：
 * - 用 xterm 官方 onSelectionChange 事件 + 防抖，拖选过程中不反复写剪贴板
 * - 优先 navigator.clipboard.writeText（HTTPS/localhost）；局域网明文 http 下
 *   退回 execCommand('copy') 隐形 textarea 方案，仍在用户手势的激活窗口内
 * - 反馈：鼠标附近浮出「已复制 N 字」气泡 + 顶栏状态短暂提示，随后恢复原状态
 * - 同一段选中不重复提示；点击清空选中后重置，便于再次复制同样内容
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.WtCopy = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var DEBOUNCE_MS = 120;
  /** 超过这个字符数不自动复制：⌘A 全选整屏回看可能有几十万字符 */
  var MAX_CHARS = 100000;
  var TOAST_MS = 1200;
  var TOAST_ID = "wt-copy-toast";
  var lastText = "";
  var suppressedUntil = 0; // 搜索等功能用 term.select 高亮时临时抑制自动复制
  var lastPointer = null;
  var toastTimer = null;
  var statusTimer = null;

  /**
   * 是否值得复制（纯函数）。
   * @returns {'copy'|'skip'|'too-big'}
   */
  function decideCopyAction(ctx) {
    ctx = ctx || {};
    if (!ctx.hasSelection) return "skip";
    var text = String(ctx.text == null ? "" : ctx.text);
    if (!text.replace(/\s+/g, "").length) return "skip"; // 纯空白：多半是误拖
    if (ctx.suppressed) return "skip"; // 程序化选中（如搜索高亮）不该进剪贴板
    if (ctx.lastText && text === ctx.lastText) return "skip"; // 同一段选中不重复复制
    if (text.length > (ctx.maxChars || MAX_CHARS)) return "too-big";
    return "copy";
  }

  /** 「已复制 3 行 · 42 字」（纯函数，便于单测） */
  function describeCopied(text) {
    var s = String(text == null ? "" : text);
    var chars = s.length;
    var lines = s.split("\n").length;
    if (lines > 1) return "已复制 " + lines + " 行 · " + chars + " 字";
    return "已复制 " + chars + " 字";
  }

  function isMacLike() {
    try {
      if (typeof window !== "undefined" && window.WtPasteImage && window.WtPasteImage.detectPlatform) {
        return window.WtPasteImage.detectPlatform() === "mac";
      }
    } catch (e) {}
    try {
      return /mac|iphone|ipad|ipod/i.test(navigator.platform || navigator.userAgent || "");
    } catch (e) {}
    return false;
  }

  function manualCopyKey() {
    // 非 mac 的 Ctrl+C 在终端里是 SIGINT，别误导用户
    return isMacLike() ? "⌘C" : "Ctrl+Shift+C";
  }

  function manualCopyHint() {
    return "复制失败，请用 " + manualCopyKey();
  }

  var baseStatus = null; // 连续复制时保留真正的原状态（如「已连接」）

  function setStatus(text, cls, restoreMs) {
    var el = typeof document !== "undefined" ? document.getElementById("wt-status") : null;
    if (!el) return;
    if (!statusTimer) {
      // 只有当前没有待恢复的提示时，才把眼前的状态当成原状态
      baseStatus = { text: el.textContent, cls: el.className };
    }
    el.textContent = text;
    el.className = cls || "";
    if (!restoreMs) return;
    if (statusTimer) clearTimeout(statusTimer);
    statusTimer = setTimeout(function () {
      statusTimer = null;
      // 期间若有别的状态（如重连/贴图）写入，就不要覆盖回去
      if (el.textContent === text && baseStatus) {
        el.textContent = baseStatus.text;
        el.className = baseStatus.cls;
      }
      baseStatus = null;
    }, restoreMs);
  }

  /** execCommand 兜底：局域网明文 http 下没有 navigator.clipboard */
  function legacyCopy(text) {
    if (typeof document === "undefined" || !document.body) return false;
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("aria-hidden", "true");
    ta.setAttribute(
      "style",
      "position:fixed;left:-9999px;top:0;width:1px;height:1px;opacity:0;padding:0;border:0;"
    );
    document.body.appendChild(ta);
    var ok = false;
    try {
      ta.focus({ preventScroll: true });
      ta.select();
      ok = !!document.execCommand("copy");
    } catch (e) {
      ok = false;
    }
    try {
      if (ta.parentNode) ta.parentNode.removeChild(ta);
    } catch (e) {}
    return ok;
  }

  async function copyText(text) {
    if (!text) return false;
    try {
      if (typeof navigator !== "undefined" && navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (e) {
      // 权限被拒或非安全上下文：继续走兜底
    }
    return legacyCopy(text);
  }

  function showToast(msg, ok) {
    if (typeof document === "undefined" || !document.body) return null;
    var el = document.getElementById(TOAST_ID);
    if (!el) {
      el = document.createElement("div");
      el.id = TOAST_ID;
      document.body.appendChild(el);
    }
    var pos = lastPointer || {};
    var x = typeof pos.x === "number" ? pos.x : 24;
    var y = typeof pos.y === "number" ? pos.y : 48;
    el.textContent = msg;
    el.setAttribute(
      "style",
      "position:fixed;z-index:10040;pointer-events:none;" +
        "left:" + Math.max(8, Math.min(x + 12, (window.innerWidth || 800) - 160)) + "px;" +
        "top:" + Math.max(8, Math.min(y + 14, (window.innerHeight || 600) - 40)) + "px;" +
        "padding:4px 10px;border-radius:999px;white-space:nowrap;" +
        "background:rgba(18,24,32,.94);border:1px solid " + (ok ? "#3dd68c" : "#f07178") + ";" +
        "color:" + (ok ? "#7ee787" : "#f07178") + ";" +
        "font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;" +
        "box-shadow:0 4px 14px rgba(0,0,0,.35);opacity:1;transition:opacity .25s ease;"
    );
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      toastTimer = null;
      el.style.opacity = "0";
      setTimeout(function () {
        if (el.style.opacity === "0" && el.parentNode) el.parentNode.removeChild(el);
      }, 300);
    }, TOAST_MS);
    return el;
  }

  /** 供测试/外部直接触发一次「读选中 → 复制 → 反馈」 */
  async function copySelection(term) {
    if (!term || typeof term.getSelection !== "function") return { ok: false, action: "skip" };
    var text = "";
    try {
      text = term.getSelection() || "";
    } catch (e) {
      text = "";
    }
    var hasSelection = typeof term.hasSelection === "function" ? !!term.hasSelection() : !!text;
    var action = decideCopyAction({
      hasSelection: hasSelection,
      text: text,
      lastText: lastText,
      suppressed: Date.now() < suppressedUntil,
    });
    if (action === "skip") {
      if (!hasSelection) lastText = ""; // 选中被清空：允许再次复制同样内容
      return { ok: false, action: "skip" };
    }
    if (action === "too-big") {
      lastText = text; // 别每次抖动都再提示一遍
      var big = "选中过大，未自动复制（" + manualCopyKey() + "）";
      showToast(big, false);
      setStatus(big, "warn", 2500);
      return { ok: false, action: "too-big" };
    }
    var ok = await copyText(text);
    if (ok) {
      lastText = text;
      var msg = describeCopied(text);
      showToast(msg, true);
      setStatus(msg, "", TOAST_MS + 300);
    } else {
      showToast(manualCopyHint(), false);
      setStatus(manualCopyHint(), "err", 2000);
    }
    return { ok: ok, action: "copy", text: text };
  }

  /** 选中即复制：挂 onSelectionChange（覆盖拖选/双击词/三击行/全选） */
  function hookCopyOnSelect(getTerm) {
    var term = typeof getTerm === "function" ? getTerm() : getTerm;
    if (!term || typeof term.onSelectionChange !== "function") return false;
    if (term._wtCopyHook) return true;
    term._wtCopyHook = true;

    // 应用开了鼠标上报（Claude Code 等 TUI）时仍要能拖选：
    // Windows/Linux 是 Shift+拖选（xterm 内置）；mac 的 Option+拖选默认关着，这里打开。
    try {
      if (term.options) term.options.macOptionClickForcesSelection = true;
    } catch (e) {}

    if (typeof document !== "undefined") {
      // 气泡跟着鼠标出现；capture 保证即使 xterm 吞掉事件也能记录
      document.addEventListener(
        "mousemove",
        function (ev) {
          lastPointer = { x: ev.clientX, y: ev.clientY };
        },
        true
      );
      document.addEventListener(
        "mouseup",
        function (ev) {
          lastPointer = { x: ev.clientX, y: ev.clientY };
        },
        true
      );
    }

    var timer = null;
    term.onSelectionChange(function () {
      if (timer) clearTimeout(timer);
      // 防抖：拖选过程中 onSelectionChange 会连续触发，只在停下来后复制一次
      timer = setTimeout(function () {
        timer = null;
        copySelection(typeof getTerm === "function" ? getTerm() : term);
      }, DEBOUNCE_MS);
    });
    return true;
  }

  /** 之后 ms 毫秒内的选中不自动复制（搜索高亮期间用） */
  function suppress(ms) {
    suppressedUntil = Date.now() + (Number(ms) || 500);
  }

  function resetForTest() {
    suppressedUntil = 0;
    lastText = "";
    lastPointer = null;
    if (statusTimer) clearTimeout(statusTimer);
    statusTimer = null;
    baseStatus = null;
  }

  return {
    decideCopyAction: decideCopyAction,
    describeCopied: describeCopied,
    copyText: copyText,
    copySelection: copySelection,
    hookCopyOnSelect: hookCopyOnSelect,
    showToast: showToast,
    legacyCopy: legacyCopy,
    manualCopyHint: manualCopyHint,
    manualCopyKey: manualCopyKey,
    suppress: suppress,
    MAX_CHARS: MAX_CHARS,
    resetForTest: resetForTest,
    TOAST_ID: TOAST_ID,
    DEBOUNCE_MS: DEBOUNCE_MS,
  };
});
