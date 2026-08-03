/**
 * 移动端（尤其 Android）顶栏/系统手势条把终端底部遮住的修复。
 *
 * 根因：#terminal-container / .xterm 原来用 calc(100vh - 32px) 定高。
 * 100vh 是「地址栏/手势导航条完全收起时的最大视口高度」，这两样东西实际展开、
 * 占用屏幕空间时并不会让 100vh 跟着变小——于是容器比真正可见区域更高，底下
 * 几行被系统 UI 盖住（Android 上看起来像一条白色横条压在终端底部）。
 *
 * 主修复在 CSS（见 bin/patch-ttyd-index.py 的 INJECT_HEAD）：改用 @supports
 * 探测 + 100dvh（动态视口高度，随地址栏/手势条实际可见区域实时变化），保留
 * 100vh 给不支持 dvh 的老浏览器兜底。
 *
 * 这个文件是纯防御性补丁：dvh 重新计算理论上会让浏览器自然触发布局与
 * window resize，但为防某些老 Android WebView 内核不触发，监听
 * visualViewport 的 resize/scroll 变化，去抖后派发一个合成的 window
 * 'resize' 事件，强制 ttyd 自带的 fitAddon.fit() 重新测量容器尺寸。
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.WtViewport = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var DEBOUNCE_MS = 120;

  /** 浏览器是否认识 100dvh（纯粹用于诊断/测试，实际生效与否由 CSS @supports 决定） */
  function supportsDvh() {
    try {
      return typeof CSS !== "undefined" && !!CSS.supports && CSS.supports("height", "100dvh");
    } catch (e) {
      return false;
    }
  }

  function kickResize() {
    try {
      window.dispatchEvent(new Event("resize"));
      return true;
    } catch (e) {}
    try {
      var ev = document.createEvent("Event");
      ev.initEvent("resize", true, true);
      window.dispatchEvent(ev);
      return true;
    } catch (e2) {
      return false;
    }
  }

  /**
   * 挂 visualViewport 变化监听，去抖后触发一次合成 resize，让 ttyd 重新
   * measure 容器尺寸。幂等；没有 visualViewport（很老的浏览器）时安全返回
   * false，不影响其余功能。
   */
  function hookViewportResizeKick(opts) {
    opts = opts || {};
    if (typeof document === "undefined" || typeof window === "undefined") return false;
    if (document.documentElement._wtViewportHook) return true;
    var vv = opts.visualViewport || window.visualViewport;
    if (!vv || typeof vv.addEventListener !== "function") return false;
    document.documentElement._wtViewportHook = true;

    var timer = null;
    function scheduleKick() {
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () {
        timer = null;
        kickResize();
      }, opts.debounceMs || DEBOUNCE_MS);
    }

    vv.addEventListener("resize", scheduleKick);
    vv.addEventListener("scroll", scheduleKick);
    try {
      window.addEventListener("orientationchange", scheduleKick);
    } catch (e) {}
    return true;
  }

  return {
    supportsDvh: supportsDvh,
    kickResize: kickResize,
    hookViewportResizeKick: hookViewportResizeKick,
    DEBOUNCE_MS: DEBOUNCE_MS,
  };
});
