/**
 * 浏览器粘贴/拖放图片 → 管理 API 落盘并写入 macOS 剪贴板 → 向 PTY 发送 Ctrl+V
 * 供同机 Claude Code 像本地终端一样读取图片。
 *
 * 注意：必须选「最大」的 image/*，并避免用坏图覆盖系统剪贴板。
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
      var res = await fetch("/api/paste-image", {
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

  function hookPasteImage(getTerm) {
    if (typeof document === "undefined") return false;
    if (document.documentElement._wtPasteImage) return true;
    document.documentElement._wtPasteImage = true;

    document.addEventListener(
      "paste",
      function (ev) {
        var img = pickLargestImage(ev.clipboardData);
        if (!img) return;
        ev.preventDefault();
        ev.stopPropagation();
        uploadAndInject(img, getTerm, { preferNativeClipboard: true });
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
        uploadAndInject(img, getTerm, { forceClipboard: true, preferNativeClipboard: false });
      },
      true
    );

    return true;
  }

  return {
    hookPasteImage: hookPasteImage,
    uploadAndInject: uploadAndInject,
    pickImageFromDataTransfer: pickImageFromDataTransfer,
    pickLargestImage: pickLargestImage,
    MAX_BYTES: MAX_BYTES,
    MIN_PIXELS: MIN_PIXELS,
  };
});
