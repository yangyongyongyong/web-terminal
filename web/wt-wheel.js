/**
 * 终端滚轮/重连策略（对标 github.com/0xshawn/remote-shell）：
 * - 重连：term.reset + 服务端回放 scrollback，前端钉在底部
 * - 有本地 scrollback → 本地滚（不经公网）
 * - 应用开启鼠标上报 → 交给应用
 * - alternate 且无鼠标 → 吞掉，禁止滚轮变方向键
 * - Alt/Option → 强制放行
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.WtWheel = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function scrollbackLines(term) {
    var pages = 30;
    try {
      if (typeof window !== "undefined" && window.WT_SCROLLBACK_PAGES) {
        pages = Number(window.WT_SCROLLBACK_PAGES) || pages;
      }
    } catch (e) {}
    var rows = 50;
    try {
      if (term && term.rows) rows = term.rows;
    } catch (e) {}
    return Math.max(200, pages * rows);
  }

  function linesFromWheel(ev) {
    var dy = ev && ev.deltaY;
    if (!dy) return 0;
    var lines;
    if (ev.deltaMode === 1) lines = dy;
    else if (ev.deltaMode === 2) lines = dy * 25;
    else lines = dy / 16;
    lines = Math.round(lines);
    if (lines === 0) lines = dy > 0 ? 1 : -1;
    return lines;
  }

  function canLocalScroll(state) {
    if (!state) return false;
    if (state.activeIsAlternate) return false;
    var baseY = typeof state.baseY === "number" ? state.baseY : 0;
    var viewportY = typeof state.viewportY === "number" ? state.viewportY : baseY;
    var length = typeof state.length === "number" ? state.length : 0;
    var rows = typeof state.rows === "number" ? state.rows : 0;
    if (rows > 0 && length > rows) return true;
    return baseY > 0 || viewportY < baseY;
  }

  /**
   * @returns {'passthrough'|'local'|'swallow'}
   */
  function decideWheelAction(ev, state) {
    if (ev && ev.altKey) return "passthrough";
    if (state && state.mouseTracking && state.mouseTracking !== "none") return "passthrough";
    if (state && state.activeIsAlternate) return "swallow";
    if (canLocalScroll(state)) return "local";
    return "swallow";
  }

  function termStateFromXterm(term) {
    try {
      if (!term || !term.buffer) return null;
      var active = term.buffer.active;
      var mouseTracking = "none";
      try {
        if (term.modes && term.modes.mouseTrackingMode) mouseTracking = term.modes.mouseTrackingMode;
      } catch (e) {}
      return {
        activeIsAlternate: active === term.buffer.alternate || (active && active.type === "alternate"),
        baseY: active && typeof active.baseY === "number" ? active.baseY : 0,
        viewportY: active && typeof active.viewportY === "number" ? active.viewportY : 0,
        length: active && typeof active.length === "number" ? active.length : 0,
        rows: typeof term.rows === "number" ? term.rows : 0,
        mouseTracking: mouseTracking,
      };
    } catch (e) {
      return null;
    }
  }

  function ensureScrollback(term) {
    try {
      if (!term || !term.options) return;
      var want = scrollbackLines(term);
      // 精确设值：每会话页数可调大也可调小，不再只升不降
      if (Number(term.options.scrollback) !== want) term.options.scrollback = want;
      term.options.smoothScrollDuration = 0;
    } catch (e) {}
  }

  function isAtBottom(term) {
    try {
      var b = term && term.buffer && term.buffer.active;
      if (!b) return true;
      return b.viewportY >= b.baseY;
    } catch (e) {
      return true;
    }
  }

  function jumpToBottom(term) {
    if (!term) return;
    ensureScrollback(term);
    try {
      if (term.options) term.options.smoothScrollDuration = 0;
      if (typeof term.scrollToBottom === "function") term.scrollToBottom();
    } catch (e) {}
    try {
      var vp = term.element && term.element.querySelector(".xterm-viewport");
      if (vp) {
        vp.style.scrollBehavior = "auto";
        vp.scrollTop = vp.scrollHeight;
      }
    } catch (e) {}
  }

  function pinBottomDuringAttach(term, ms) {
    if (!term || !term.element) return false;
    ms = typeof ms === "number" ? ms : 3000;
    if (term._wtPinTimer) {
      clearInterval(term._wtPinTimer);
      term._wtPinTimer = null;
    }
    var until = Date.now() + ms;
    var userLeftBottom = false;

    function onWheel() {
      userLeftBottom = true;
    }
    try {
      term.element.addEventListener("wheel", onWheel, { capture: true, passive: true });
    } catch (e) {}

    jumpToBottom(term);
    try {
      requestAnimationFrame(function () {
        jumpToBottom(term);
      });
    } catch (e) {
      jumpToBottom(term);
    }

    term._wtPinTimer = setInterval(function () {
      if (userLeftBottom || Date.now() >= until) {
        clearInterval(term._wtPinTimer);
        term._wtPinTimer = null;
        try {
          term.element.removeEventListener("wheel", onWheel, { capture: true });
        } catch (e) {}
        return;
      }
      jumpToBottom(term);
    }, 32);
    return true;
  }

  /** remote-shell 模式：先 reset，再钉底等服务端回放 */
  function onSessionConnect(term, ms) {
    if (!term) return false;
    try {
      if (typeof term.reset === "function") term.reset();
    } catch (e) {}
    ensureScrollback(term);
    return pinBottomDuringAttach(term, ms || 3500);
  }

  function hookLocalWheel(getTerm) {
    var term = typeof getTerm === "function" ? getTerm() : getTerm;
    if (!term || !term.element || term.element._wtLocalWheel) return false;
    term.element._wtLocalWheel = true;
    ensureScrollback(term);
    term.element.addEventListener(
      "wheel",
      function (ev) {
        ensureScrollback(term);
        var action = decideWheelAction(ev, termStateFromXterm(term));
        if (action === "passthrough") return;
        ev.preventDefault();
        ev.stopImmediatePropagation();
        if (action === "local") {
          var lines = linesFromWheel(ev);
          if (lines) {
            try {
              term.scrollLines(lines);
            } catch (e) {}
          }
        }
      },
      { capture: true, passive: false }
    );
    return true;
  }

  return {
    linesFromWheel: linesFromWheel,
    canLocalScroll: canLocalScroll,
    decideWheelAction: decideWheelAction,
    termStateFromXterm: termStateFromXterm,
    ensureScrollback: ensureScrollback,
    scrollbackLines: scrollbackLines,
    isAtBottom: isAtBottom,
    jumpToBottom: jumpToBottom,
    pinBottomDuringAttach: pinBottomDuringAttach,
    onSessionConnect: onSessionConnect,
    hookLocalWheel: hookLocalWheel,
  };
});
