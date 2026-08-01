/**
 * 终端内文本搜索：mac ⌘F / Windows·Linux Ctrl+F 打开搜索条。
 *
 * ttyd 打包的 xterm 没带 SearchAddon，这里自己实现：
 * - 按 isWrapped 把折行合并成「逻辑行」再搜，跨行断开的词也能命中
 * - 命中位置用 cell 遍历换算，宽字符（中日韩）列号不会错位
 * - 全部命中用 registerMarker + registerDecoration 上底色；当前命中额外用
 *   term.select 高亮并滚动到可见处（任何 renderer 都稳）
 * - 键盘只在 document 捕获阶段处理，不占用 attachCustomKeyEventHandler
 *   （那个 xterm 只允许一个，已被贴图键位改写占用）
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.WtSearch = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var BAR_ID = "wt-search-bar";
  var INPUT_ID = "wt-search-input";
  var COUNT_ID = "wt-search-count";
  var DEBOUNCE_MS = 120;
  var MAX_MATCHES = 2000; // 超过就只保留前 N 个，避免超大回看卡顿
  var MAX_DECORATIONS = 400; // 底色装饰更贵，只画前 N 个

  var state = {
    open: false,
    query: "",
    matches: [],
    index: -1,
    decorations: [],
    markers: [],
    timer: null,
  };

  function isMacLike(navLike) {
    try {
      if (!navLike && typeof window !== "undefined" && window.WtPasteImage && window.WtPasteImage.detectPlatform) {
        return window.WtPasteImage.detectPlatform() === "mac";
      }
    } catch (e) {}
    var nav = navLike || (typeof navigator !== "undefined" ? navigator : null);
    if (!nav) return false;
    var p = "";
    try {
      p = (nav.userAgentData && nav.userAgentData.platform) || nav.platform || nav.userAgent || "";
    } catch (e) {}
    return /mac|iphone|ipad|ipod/i.test(String(p));
  }

  /**
   * 键位决策（纯函数）：mac 用 ⌘F，其它平台用 Ctrl+F。
   * @returns {'open'|'next'|'prev'|'close'|'default'}
   */
  function decideSearchKeyAction(ev, platform) {
    if (!ev || (ev.type && ev.type !== "keydown")) return "default";
    var mac = platform ? platform === "mac" : isMacLike();
    var key = String(ev.key || "").toLowerCase();
    var isF = key === "f" || ev.code === "KeyF" || ev.keyCode === 70;
    if (isF && !ev.altKey && !ev.shiftKey) {
      // mac 上 Ctrl+F 是 readline 的前移光标，必须留给 PTY
      if (mac ? ev.metaKey && !ev.ctrlKey : ev.ctrlKey && !ev.metaKey) return "open";
    }
    if (!ev.searchOpen) return "default"; // 搜索条没开时，下面这些键不拦
    if (key === "escape" || ev.keyCode === 27) return "close";
    if (key === "enter" || ev.keyCode === 13) return ev.shiftKey ? "prev" : "next";
    if (isF && (mac ? ev.metaKey : ev.ctrlKey)) return "open";
    return "default";
  }

  /** 把折行合并成逻辑行；返回 [{text, rows:[绝对行号...]}] */
  function collectLogicalLines(term) {
    var out = [];
    if (!term || !term.buffer || !term.buffer.active) return out;
    var buf = term.buffer.active;
    var cur = null;
    for (var y = 0; y < buf.length; y++) {
      var line = buf.getLine(y);
      if (!line) continue;
      var text = line.translateToString(false);
      if (line.isWrapped && cur) {
        cur.text += text;
        cur.rows.push(y);
      } else {
        if (cur) out.push(cur);
        cur = { text: text, rows: [y] };
      }
    }
    if (cur) out.push(cur);
    return out;
  }

  /**
   * 在逻辑行里找命中（纯函数）。大小写不敏感、纯文本（不当正则）。
   * @returns {{line:number,index:number,length:number}[]}
   */
  function findMatches(lines, query, opts) {
    opts = opts || {};
    var max = opts.max || MAX_MATCHES;
    var out = [];
    var q = String(query == null ? "" : query);
    if (!q) return out;
    var needle = opts.caseSensitive ? q : q.toLowerCase();
    for (var i = 0; i < lines.length; i++) {
      var raw = String((lines[i] && lines[i].text) || "");
      var hay = opts.caseSensitive ? raw : raw.toLowerCase();
      var from = 0;
      while (from <= hay.length - needle.length) {
        var at = hay.indexOf(needle, from);
        if (at < 0) break;
        out.push({ line: i, index: at, length: q.length });
        if (out.length >= max) return out;
        from = at + Math.max(1, needle.length);
      }
    }
    return out;
  }

  /** 逻辑行内的字符串下标 → 具体 (行, 列)，按 cell 走，兼容宽字符 */
  function locateCell(term, logical, index) {
    if (!term || !logical) return null;
    var buf = term.buffer.active;
    var acc = 0;
    for (var r = 0; r < logical.rows.length; r++) {
      var y = logical.rows[r];
      var line = buf.getLine(y);
      if (!line) continue;
      for (var x = 0; x < line.length; x++) {
        var cell = line.getCell(x);
        if (!cell) continue;
        if (cell.getWidth && cell.getWidth() === 0) continue; // 宽字符右半格
        var chars = cell.getChars ? cell.getChars() : "";
        var len = chars.length || 1;
        if (acc + len > index) return { row: y, col: x };
        acc += len;
      }
    }
    return null;
  }

  function clearHighlights() {
    for (var i = 0; i < state.decorations.length; i++) {
      try {
        state.decorations[i].dispose();
      } catch (e) {}
    }
    for (var j = 0; j < state.markers.length; j++) {
      try {
        state.markers[j].dispose();
      } catch (e) {}
    }
    state.decorations = [];
    state.markers = [];
  }

  /** 给所有命中上底色（best-effort：装饰 API 不可用就只留当前命中的选中高亮） */
  function paintMatches(term, positions) {
    clearHighlights();
    if (!term || typeof term.registerMarker !== "function" || typeof term.registerDecoration !== "function") return 0;
    var buf = term.buffer.active;
    var cursorAbs = buf.baseY + buf.cursorY;
    var painted = 0;
    for (var i = 0; i < positions.length && painted < MAX_DECORATIONS; i++) {
      var pos = positions[i];
      if (!pos) continue;
      try {
        var marker = term.registerMarker(pos.row - cursorAbs);
        if (!marker) continue;
        var dec = term.registerDecoration({
          marker: marker,
          x: pos.col,
          width: pos.width,
          backgroundColor: pos.current ? "#f5a524" : "#4b5d2a",
          foregroundColor: pos.current ? "#101418" : "#e7ecf1",
          layer: "top",
        });
        state.markers.push(marker);
        if (dec) state.decorations.push(dec);
        painted += 1;
      } catch (e) {}
    }
    return painted;
  }

  function setCount(text) {
    var el = typeof document !== "undefined" ? document.getElementById(COUNT_ID) : null;
    if (el) el.textContent = text;
  }

  /** 命中数 → 计数文案（纯函数） */
  function describeCount(total, index) {
    if (!total) return "无匹配";
    return index + 1 + "/" + total;
  }

  function suppressCopy() {
    // 搜索用 term.select 高亮，别让「选中即复制」把每个命中都写进剪贴板
    try {
      if (typeof window !== "undefined" && window.WtCopy && window.WtCopy.suppress) {
        window.WtCopy.suppress(600);
      }
    } catch (e) {}
  }

  function focusMatch(term, i) {
    if (!state.matches.length) return;
    var total = state.matches.length;
    state.index = ((i % total) + total) % total;
    var m = state.matches[state.index];
    var start = locateCell(term, m.logical, m.index);
    var end = locateCell(term, m.logical, m.index + m.length - 1);
    if (!start) return;
    var width = end && end.row === start.row ? end.col - start.col + 1 : m.length;

    var positions = [];
    for (var k = 0; k < state.matches.length; k++) {
      var mk = state.matches[k];
      var p = k === state.index ? start : locateCell(term, mk.logical, mk.index);
      if (!p) continue;
      var pe = locateCell(term, mk.logical, mk.index + mk.length - 1);
      positions.push({
        row: p.row,
        col: p.col,
        width: pe && pe.row === p.row ? pe.col - p.col + 1 : mk.length,
        current: k === state.index,
      });
    }
    paintMatches(term, positions);

    suppressCopy();
    try {
      term.select(start.col, start.row, width);
    } catch (e) {}
    try {
      var buf = term.buffer.active;
      var viewTop = buf.viewportY;
      var rows = term.rows || 24;
      if (start.row < viewTop || start.row >= viewTop + rows) {
        term.scrollToLine(Math.max(0, start.row - Math.floor(rows / 2)));
      }
    } catch (e) {}
    setCount(describeCount(total, state.index));
  }

  function runSearch(term, query, keepIndex) {
    state.query = query;
    clearHighlights();
    if (!query) {
      state.matches = [];
      state.index = -1;
      setCount("");
      try {
        term.clearSelection();
      } catch (e) {}
      return 0;
    }
    var logicals = collectLogicalLines(term);
    var found = findMatches(logicals, query);
    state.matches = found.map(function (m) {
      return { logical: logicals[m.line], index: m.index, length: m.length };
    });
    if (!state.matches.length) {
      state.index = -1;
      setCount("无匹配");
      suppressCopy();
      try {
        term.clearSelection();
      } catch (e) {}
      return 0;
    }
    // 默认跳到最靠后的命中（终端里新内容在下面，通常先关心最近的）
    var start = keepIndex && state.index >= 0 ? Math.min(state.index, state.matches.length - 1) : state.matches.length - 1;
    focusMatch(term, start);
    return state.matches.length;
  }

  function buildBar(getTerm) {
    var bar = document.getElementById(BAR_ID);
    if (bar) return bar;
    bar = document.createElement("div");
    bar.id = BAR_ID;
    bar.setAttribute(
      "style",
      "position:fixed;z-index:10045;top:40px;right:14px;display:flex;align-items:center;gap:6px;" +
        "padding:6px 8px;border-radius:10px;background:rgba(18,24,32,.96);border:1px solid #2a3542;" +
        "box-shadow:0 6px 20px rgba(0,0,0,.45);font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;color:#e7ecf1;"
    );
    bar.innerHTML =
      '<input id="' + INPUT_ID + '" type="search" placeholder="搜索终端内容" autocomplete="off" spellcheck="false" ' +
      'style="width:190px;background:#0c1117;color:#e7ecf1;border:1px solid #2a3542;border-radius:6px;' +
      'padding:5px 8px;font:inherit;outline:none">' +
      '<span id="' + COUNT_ID + '" style="min-width:52px;text-align:center;color:#8b9aab"></span>' +
      '<button data-wt-search="prev" title="上一个 (Shift+回车)" style="all:unset;cursor:pointer;padding:2px 6px;border-radius:5px;background:#2b3645;color:#e7ecf1">↑</button>' +
      '<button data-wt-search="next" title="下一个 (回车)" style="all:unset;cursor:pointer;padding:2px 6px;border-radius:5px;background:#2b3645;color:#e7ecf1">↓</button>' +
      '<button data-wt-search="close" title="关闭 (Esc)" style="all:unset;cursor:pointer;padding:2px 6px;border-radius:5px;background:#2b3645;color:#8b9aab">✕</button>';
    document.body.appendChild(bar);

    var input = bar.querySelector("#" + INPUT_ID);
    input.addEventListener("input", function () {
      if (state.timer) clearTimeout(state.timer);
      var val = input.value;
      state.timer = setTimeout(function () {
        state.timer = null;
        runSearch(getTerm(), val, false);
      }, DEBOUNCE_MS);
    });
    bar.addEventListener("click", function (ev) {
      var act = ev.target && ev.target.getAttribute && ev.target.getAttribute("data-wt-search");
      if (!act) return;
      if (act === "close") closeSearch(getTerm());
      else if (act === "next") focusMatch(getTerm(), state.index + 1);
      else if (act === "prev") focusMatch(getTerm(), state.index - 1);
    });
    return bar;
  }

  function openSearch(getTerm) {
    if (typeof document === "undefined" || !document.body) return false;
    var term = typeof getTerm === "function" ? getTerm() : getTerm;
    var bar = buildBar(typeof getTerm === "function" ? getTerm : function () { return term; });
    bar.style.display = "flex";
    state.open = true;
    var input = document.getElementById(INPUT_ID);
    if (input) {
      // 选中终端里的文字后按 ⌘F：直接拿它当关键词
      try {
        var sel = term && term.getSelection ? term.getSelection() : "";
        if (sel && sel.length <= 200 && sel.indexOf("\n") < 0) input.value = sel;
      } catch (e) {}
      input.focus();
      input.select();
      if (input.value) runSearch(term, input.value, false);
    }
    return true;
  }

  function closeSearch(getTerm) {
    var term = typeof getTerm === "function" ? getTerm() : getTerm;
    state.open = false;
    state.matches = [];
    state.index = -1;
    state.query = "";
    clearHighlights();
    var bar = typeof document !== "undefined" ? document.getElementById(BAR_ID) : null;
    if (bar) bar.style.display = "none";
    setCount("");
    suppressCopy();
    try {
      if (term && term.clearSelection) term.clearSelection();
      if (term && term.focus) term.focus();
    } catch (e) {}
    return true;
  }

  function hookSearch(getTerm) {
    if (typeof document === "undefined") return false;
    var term = typeof getTerm === "function" ? getTerm() : getTerm;
    if (!term) return false;
    if (document.documentElement._wtSearchHook) return true;
    document.documentElement._wtSearchHook = true;

    document.addEventListener(
      "keydown",
      function (ev) {
        var probe = {
          type: "keydown",
          key: ev.key,
          code: ev.code,
          keyCode: ev.keyCode,
          ctrlKey: ev.ctrlKey,
          metaKey: ev.metaKey,
          altKey: ev.altKey,
          shiftKey: ev.shiftKey,
          searchOpen: state.open,
        };
        var action = decideSearchKeyAction(probe);
        if (action === "default") return;
        // Esc/回车只在搜索框里生效，别抢终端里的按键
        var inBar = ev.target && ev.target.id === INPUT_ID;
        if ((action === "close" || action === "next" || action === "prev") && !inBar) return;
        ev.preventDefault();
        ev.stopPropagation();
        if (action === "open") openSearch(getTerm);
        else if (action === "close") closeSearch(getTerm);
        else if (action === "next") focusMatch(typeof getTerm === "function" ? getTerm() : term, state.index + 1);
        else if (action === "prev") focusMatch(typeof getTerm === "function" ? getTerm() : term, state.index - 1);
      },
      true
    );
    return true;
  }

  function stateForTest() {
    return { open: state.open, query: state.query, total: state.matches.length, index: state.index, decorations: state.decorations.length };
  }

  return {
    decideSearchKeyAction: decideSearchKeyAction,
    findMatches: findMatches,
    describeCount: describeCount,
    collectLogicalLines: collectLogicalLines,
    locateCell: locateCell,
    runSearch: runSearch,
    focusMatch: focusMatch,
    openSearch: openSearch,
    closeSearch: closeSearch,
    hookSearch: hookSearch,
    stateForTest: stateForTest,
    BAR_ID: BAR_ID,
    INPUT_ID: INPUT_ID,
    COUNT_ID: COUNT_ID,
    DEBOUNCE_MS: DEBOUNCE_MS,
    MAX_MATCHES: MAX_MATCHES,
  };
});
