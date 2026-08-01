/**
 * 浏览器粘贴/拖放图片 → 管理 API 落盘并写入 macOS 剪贴板 → 向 PTY 发送 Ctrl+V
 * 供同机 Claude Code 像本地终端一样读取图片。
 *
 * 注意：必须选「最大」的 image/*，并避免用坏图覆盖系统剪贴板。
 *
 * 键位按「客户端系统」分流（服务端始终是 macOS）：
 * - mac 客户端：⌘V 走浏览器 paste（图优先）；Ctrl+V 由 xterm 透传 \x16，Claude Code 读本机剪贴板
 * - 非 mac 客户端：Ctrl+V = 贴文本（剪贴板只有图时兜底贴图）；Alt+V = 贴图；Ctrl+Alt+V = 发原始 ^V
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.WtPasteImage = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var MAX_BYTES = 12 * 1024 * 1024;
  /** 小于此像素面积视为预览/坏图，不覆盖系统剪贴板 */
  var MIN_PIXELS = 64;
  var busy = false;
  var OVERLAY_ID = "wt-paste-overlay";
  var OVERLAY_TIMEOUT_MS = 20000;
  /** 引导浮层期间：下一次 paste 强制走贴图 */
  var forceImageUntil = 0;
  /** 最近一次上传的 promise（事件回调无法被 await，测试/串行化用） */
  var lastUpload = null;

  function trackUpload(promise) {
    lastUpload = promise;
    return promise;
  }

  function pendingUpload() {
    return lastUpload;
  }

  /** 客户端系统：'mac'（含 iPad/iPhone，⌘V 语义）| 'other'（Windows/Linux 等） */
  function detectPlatform(navLike) {
    var nav = navLike || (typeof navigator !== "undefined" ? navigator : null);
    if (!nav) return "other";
    var plat = "";
    try {
      plat = (nav.userAgentData && nav.userAgentData.platform) || nav.platform || "";
    } catch (e) {}
    var ua = "";
    try {
      ua = nav.userAgent || "";
    } catch (e) {}
    if (/mac|iphone|ipad|ipod/i.test(String(plat))) return "mac";
    if (/Macintosh|iPhone|iPad|iPod/.test(String(ua))) return "mac";
    return "other";
  }

  function isVKey(ev) {
    if (!ev) return false;
    if (ev.code === "KeyV") return true;
    if (ev.keyCode === 86) return true;
    return String(ev.key || "").toLowerCase() === "v";
  }

  /**
   * 键位决策（纯函数）。
   * @returns {'default'|'native-paste'|'image-paste'|'raw-ctrl-v'}
   */
  function decideKeyAction(ev, platform) {
    if (!ev) return "default";
    if (ev.type && ev.type !== "keydown") return "default";
    var p = platform || ev.platform || detectPlatform();
    if (p === "mac") return "default"; // mac 客户端一律不拦截，保持原有手感
    if (!isVKey(ev)) return "default";
    if (ev.metaKey) return "default";
    if (ev.ctrlKey && ev.altKey) return "raw-ctrl-v"; // 逃生阀：vim 可视块等
    if (ev.ctrlKey && !ev.altKey && !ev.shiftKey) return "native-paste";
    if (ev.altKey && !ev.ctrlKey) return "image-paste";
    return "default";
  }

  /**
   * paste 事件决策（纯函数）：贴图还是把事件让给 xterm 贴文本。
   * @returns {'image'|'text'}
   */
  function decidePasteAction(ctx) {
    ctx = ctx || {};
    if (!ctx.hasImage) return "text";
    if (ctx.forceImage) return "image";
    if ((ctx.platform || "other") === "mac") return "image";
    return ctx.hasText ? "text" : "image"; // 非 mac：文本优先，只有图时兜底贴图
  }

  /** 顶栏提示文案：按客户端系统给出正确按键 */
  function hintText(pages, platform) {
    var n = Number(pages) || 30;
    var p = platform || detectPlatform();
    if (p === "mac") return "滚轮回看约" + n + "页 · ⌘V/Ctrl+V 贴图 · 选中即复制 · ⌘F 搜索";
    return "滚轮回看约" + n + "页 · Ctrl+V 贴文本 · Alt+V 贴图 · 选中即复制 · Ctrl+F 搜索";
  }

  /**
   * 上传选项：mac 客户端的图通常已在本机剪贴板（别用缩略图覆盖）；
   * 非 mac 客户端的图只存在于对端剪贴板，必须落盘并写入 mac 剪贴板。
   */
  function uploadOptsFor(platform, forced) {
    if (platform === "mac" && !forced) return { preferNativeClipboard: true };
    return { forceClipboard: true, preferNativeClipboard: false };
  }

  function setStatus(text, cls) {
    var el = typeof document !== "undefined" ? document.getElementById("wt-status") : null;
    if (!el) return;
    el.textContent = text;
    el.className = cls || "";
  }

  function blobToBase64(blob) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () {
        var s = String(reader.result || "");
        var i = s.indexOf(",");
        resolve(i >= 0 ? s.slice(i + 1) : s);
      };
      reader.onerror = function () {
        reject(reader.error || new Error("read failed"));
      };
      reader.readAsDataURL(blob);
    });
  }

  function imageDimensions(blob) {
    return new Promise(function (resolve) {
      if (!blob || typeof createImageBitmap !== "function") {
        resolve({ width: 0, height: 0 });
        return;
      }
      createImageBitmap(blob)
        .then(function (bmp) {
          var w = bmp.width || 0;
          var h = bmp.height || 0;
          try {
            bmp.close();
          } catch (e) {}
          resolve({ width: w, height: h });
        })
        .catch(function () {
          resolve({ width: 0, height: 0 });
        });
    });
  }

  /**
   * 收集所有 image/*，选字节最大的（避免 HTML 拖放预览/缩略图抢先）。
   */
  function collectImageCandidates(dt) {
    var list = [];
    if (!dt) return list;
    if (dt.files && dt.files.length) {
      for (var i = 0; i < dt.files.length; i++) {
        var f = dt.files[i];
        if (f && f.type && f.type.indexOf("image/") === 0) list.push(f);
      }
    }
    if (dt.items && dt.items.length) {
      for (var j = 0; j < dt.items.length; j++) {
        var it = dt.items[j];
        if (it.kind === "file" && it.type && it.type.indexOf("image/") === 0) {
          var file = it.getAsFile();
          if (file) list.push(file);
        }
      }
    }
    return list;
  }

  function clipboardHasText(dt) {
    if (!dt) return false;
    try {
      if (typeof dt.getData === "function") {
        var t = dt.getData("text/plain");
        return !!(t && String(t).length);
      }
    } catch (e) {}
    try {
      var types = dt.types;
      if (types) {
        for (var i = 0; i < types.length; i++) {
          if (String(types[i]) === "text/plain") return true;
        }
      }
    } catch (e) {}
    return false;
  }

  /** navigator.clipboard.read() 结果里取第一张图 */
  async function firstImageFromClipboardItems(items) {
    if (!items || !items.length) return null;
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      var types = (it && it.types) || [];
      for (var j = 0; j < types.length; j++) {
        var ty = String(types[j]);
        if (ty.indexOf("image/") !== 0) continue;
        try {
          var blob = await it.getType(ty);
          if (blob) return blob;
        } catch (e) {}
      }
    }
    return null;
  }

  function pickLargestImage(dt) {
    var list = collectImageCandidates(dt);
    if (!list.length) return null;
    list.sort(function (a, b) {
      return (b.size || 0) - (a.size || 0);
    });
    return list[0];
  }

  /** 兼容旧名 */
  function pickImageFromDataTransfer(dt) {
    return pickLargestImage(dt);
  }

  function sendCtrlV(term) {
    if (!term) return false;
    try {
      if (typeof term.input === "function") {
        term.input("\x16");
        return true;
      }
    } catch (e) {}
    return false;
  }

  function pastePath(term, path) {
    if (!term || !path) return false;
    try {
      if (typeof term.paste === "function") {
        term.paste(path);
        return true;
      }
      if (typeof term.input === "function") {
        term.input(path);
        return true;
      }
    } catch (e) {}
    return false;
  }

  function sleep(ms) {
    return new Promise(function (resolve) {
      setTimeout(resolve, ms);
    });
  }

  async function uploadAndInject(fileOrBlob, getTerm, opts) {
    opts = opts || {};
    if (busy) return { ok: false, error: "busy" };
    if (!fileOrBlob) return { ok: false, error: "no image" };
    if (fileOrBlob.size > MAX_BYTES) {
      setStatus("图片过大（上限 12MB）", "err");
      return { ok: false, error: "too large" };
    }
    busy = true;
    setStatus("正在粘贴图片…", "warn");
    try {
      var dim = await imageDimensions(fileOrBlob);
      var pixels = (dim.width || 0) * (dim.height || 0);
      var trustClipboardWrite = pixels >= MIN_PIXELS || opts.forceClipboard === true;

      // 同机：系统剪贴板往往已有完整原图。若事件里只有缩略图，只发 Ctrl+V，绝不覆盖剪贴板。
      if (!trustClipboardWrite && opts.preferNativeClipboard !== false) {
        var term0 = typeof getTerm === "function" ? getTerm() : null;
        if (sendCtrlV(term0)) {
          setStatus("已尝试粘贴（未覆盖剪贴板，事件图过小）", "warn");
          return { ok: true, path: "", clipboard: true, skipped_overwrite: true };
        }
      }

      var b64 = await blobToBase64(fileOrBlob);
      var apiBase = "";
      try {
        if (typeof window !== "undefined" && window.WT_API_BASE) apiBase = window.WT_API_BASE;
      } catch (e) {}
      var res = await fetch(apiBase + "/api/paste-image", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mime: fileOrBlob.type || "image/png",
          image_base64: b64,
          filename: fileOrBlob.name || "",
          // 服务端：过小且未 force 时不要写系统剪贴板
          set_clipboard: trustClipboardWrite,
          client_width: dim.width || 0,
          client_height: dim.height || 0,
        }),
      });
      var data = {};
      try {
        data = await res.json();
      } catch (e) {
        data = {};
      }
      if (!res.ok) {
        var msg = (data && data.error) || "上传失败 " + res.status;
        setStatus(msg, "err");
        return { ok: false, error: msg };
      }

      // 服务端判定图太小未写剪贴板时：仍尝试 Ctrl+V（用用户原剪贴板）
      var term = typeof getTerm === "function" ? getTerm() : null;
      await sleep(80);
      var used = sendCtrlV(term);
      if (!used && data.path) {
        used = pastePath(term, data.path);
        if (used) setStatus("已粘贴路径 " + data.path, "warn");
      } else if (used && data.clipboard) {
        setStatus(
          "已粘贴图片 " + (data.width || "?") + "×" + (data.height || "?"),
          ""
        );
      } else if (used) {
        setStatus("已发送粘贴键（剪贴板未改写）", "warn");
      } else {
        setStatus("图片已保存: " + (data.path || ""), "warn");
      }
      return {
        ok: true,
        path: data.path || "",
        clipboard: !!data.clipboard,
        width: data.width || 0,
        height: data.height || 0,
      };
    } catch (err) {
      var em = String((err && err.message) || err);
      setStatus("粘贴图片失败: " + em, "err");
      return { ok: false, error: em };
    } finally {
      busy = false;
    }
  }

  function refocusTerm() {
    try {
      if (typeof window !== "undefined" && window.term && typeof window.term.focus === "function") {
        window.term.focus();
      }
    } catch (e) {}
  }

  function isOverlayOpen() {
    if (typeof document === "undefined") return false;
    return !!document.getElementById(OVERLAY_ID);
  }

  function closeOverlay() {
    forceImageUntil = 0;
    if (typeof document === "undefined") return;
    var el = document.getElementById(OVERLAY_ID);
    if (el && el.parentNode) el.parentNode.removeChild(el);
    refocusTerm();
  }

  /**
   * 非安全上下文（局域网 http）或权限被拒时：让用户手动按 Ctrl+V，
   * 焦点落在浮层里的隐形 textarea 上，原生 paste 事件即可拿到图。
   */
  function openPasteOverlay() {
    if (typeof document === "undefined" || !document.body) return false;
    if (isOverlayOpen()) return true;
    forceImageUntil = Date.now() + OVERLAY_TIMEOUT_MS;

    var mask = document.createElement("div");
    mask.id = OVERLAY_ID;
    mask.setAttribute(
      "style",
      "position:fixed;inset:0;z-index:10050;background:rgba(0,0,0,.55);" +
        "display:flex;align-items:center;justify-content:center;"
    );
    var panel = document.createElement("div");
    panel.setAttribute(
      "style",
      "background:#1a222c;color:#e7ecf1;border:1px solid #2a3542;border-radius:12px;" +
        "padding:18px 22px;text-align:center;max-width:80vw;" +
        "font:14px/1.7 -apple-system,'Segoe UI',system-ui,sans-serif;"
    );
    panel.innerHTML =
      '<div style="font-weight:700;margin-bottom:4px">粘贴图片</div>' +
      "<div>请按 <b>Ctrl+V</b> 把剪贴板里的图片贴进来</div>" +
      '<div style="color:#8b9aab;font-size:12px;margin-top:6px">Esc 取消 · 20 秒后自动关闭</div>';
    var sink = document.createElement("textarea");
    sink.setAttribute(
      "style",
      "position:absolute;left:0;top:0;width:1px;height:1px;opacity:0;border:0;padding:0;resize:none;"
    );
    sink.setAttribute("aria-hidden", "true");

    mask.appendChild(panel);
    mask.appendChild(sink);
    document.body.appendChild(mask);
    try {
      sink.focus();
    } catch (e) {}

    mask.addEventListener("mousedown", function (ev) {
      if (ev.target === mask) {
        closeOverlay();
        setStatus("已取消贴图", "warn");
      }
    });
    sink.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" || ev.keyCode === 27) {
        closeOverlay();
        setStatus("已取消贴图", "warn");
      }
    });
    setStatus("等待 Ctrl+V：把图片贴进来", "warn");
    setTimeout(function () {
      if (isOverlayOpen()) {
        closeOverlay();
        setStatus("贴图已超时取消", "warn");
      }
    }, OVERLAY_TIMEOUT_MS);
    return true;
  }

  /** Alt+V：先试异步剪贴板 API，不可用/被拒则退回引导浮层 */
  async function pasteImageFromClipboard(getTerm) {
    var canRead = false;
    try {
      canRead =
        typeof navigator !== "undefined" &&
        navigator.clipboard &&
        typeof navigator.clipboard.read === "function";
    } catch (e) {}
    if (!canRead) {
      openPasteOverlay();
      return { ok: false, error: "need manual paste" };
    }
    var blob = null;
    try {
      var items = await navigator.clipboard.read();
      blob = await firstImageFromClipboardItems(items);
    } catch (e) {
      openPasteOverlay(); // 权限被拒 / 非安全上下文
      return { ok: false, error: "need manual paste" };
    }
    if (!blob) {
      setStatus("剪贴板里没有图片", "warn");
      return { ok: false, error: "no image" };
    }
    return trackUpload(uploadAndInject(blob, getTerm, { forceClipboard: true, preferNativeClipboard: false }));
  }

  /**
   * 按客户端系统改键：非 mac 下 Ctrl+V 让浏览器原生粘贴（xterm 不再发 \x16），
   * Alt+V 贴图，Ctrl+Alt+V 发原始 ^V。
   */
  function hookKeys(getTerm) {
    var term = typeof getTerm === "function" ? getTerm() : getTerm;
    if (!term || typeof term.attachCustomKeyEventHandler !== "function") return false;
    if (term._wtKeyHook) return true;
    term._wtKeyHook = true;
    var platform = detectPlatform();
    term.attachCustomKeyEventHandler(function (ev) {
      var action = decideKeyAction(ev, platform);
      if (action === "default") return true;
      // 关键：native-paste 不能 preventDefault，否则浏览器不会产生 paste 事件
      if (action === "native-paste") return false;
      try {
        ev.preventDefault();
      } catch (e) {}
      if (action === "raw-ctrl-v") {
        sendCtrlV(typeof getTerm === "function" ? getTerm() : term);
        return false;
      }
      if (action === "image-paste") {
        pasteImageFromClipboard(getTerm);
        return false;
      }
      return false;
    });
    return true;
  }

  function hookPasteImage(getTerm) {
    if (typeof document === "undefined") return false;
    if (document.documentElement._wtPasteImage) return true;
    document.documentElement._wtPasteImage = true;

    document.addEventListener(
      "paste",
      function (ev) {
        // 往普通输入框（如搜索框）里粘贴：完全交给浏览器
        var tgt = ev.target;
        if (!isOverlayOpen() && tgt && tgt.tagName === "INPUT") return;
        var forceImage = isOverlayOpen() || Date.now() < forceImageUntil;
        var img = pickLargestImage(ev.clipboardData);
        var platform = detectPlatform();
        var action = decidePasteAction({
          platform: platform,
          hasImage: !!img,
          hasText: clipboardHasText(ev.clipboardData),
          forceImage: forceImage,
        });
        if (forceImage) {
          // 引导浮层内的粘贴：无论有没有图都不能漏给终端
          ev.preventDefault();
          ev.stopPropagation();
          closeOverlay();
          if (!img) {
            setStatus("剪贴板里没有图片", "warn");
            return;
          }
          trackUpload(uploadAndInject(img, getTerm, uploadOptsFor(platform, true)));
          return;
        }
        if (action !== "image") return; // 文本：交给 xterm 正常粘贴
        ev.preventDefault();
        ev.stopPropagation();
        trackUpload(uploadAndInject(img, getTerm, uploadOptsFor(platform, false)));
      },
      true
    );

    document.addEventListener(
      "dragover",
      function (ev) {
        if (!ev.dataTransfer) return;
        var types = ev.dataTransfer.types;
        var ok = false;
        if (types) {
          for (var i = 0; i < types.length; i++) {
            if (types[i] === "Files" || String(types[i]).indexOf("image/") === 0) ok = true;
          }
        }
        if (!ok) return;
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "copy";
      },
      true
    );

    document.addEventListener(
      "drop",
      function (ev) {
        var img = pickLargestImage(ev.dataTransfer);
        if (!img) return;
        ev.preventDefault();
        ev.stopPropagation();
        // 拖放文件不在系统剪贴板：必须落盘并写入剪贴板
        trackUpload(uploadAndInject(img, getTerm, { forceClipboard: true, preferNativeClipboard: false }));
      },
      true
    );

    return true;
  }

  return {
    hookPasteImage: hookPasteImage,
    hookKeys: hookKeys,
    uploadAndInject: uploadAndInject,
    pickImageFromDataTransfer: pickImageFromDataTransfer,
    pickLargestImage: pickLargestImage,
    clipboardHasText: clipboardHasText,
    detectPlatform: detectPlatform,
    decideKeyAction: decideKeyAction,
    decidePasteAction: decidePasteAction,
    hintText: hintText,
    uploadOptsFor: uploadOptsFor,
    pasteImageFromClipboard: pasteImageFromClipboard,
    openPasteOverlay: openPasteOverlay,
    closeOverlay: closeOverlay,
    isOverlayOpen: isOverlayOpen,
    pendingUpload: pendingUpload,
    MAX_BYTES: MAX_BYTES,
    MIN_PIXELS: MIN_PIXELS,
    OVERLAY_ID: OVERLAY_ID,
  };
});
