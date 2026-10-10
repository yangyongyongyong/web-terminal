#!/usr/bin/env python3
"""会话管理页 + API（7690）。进入终端需先登录（票据只签发给已登录用户）；工作目录受 SESSION_PATH_ROOT 限制。"""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import importlib.util
import json
import os
import re
import subprocess
import sys
import stat as _stat
import threading
import time
import uuid
import zipfile
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
PUBLIC_HOST = ENV.get("PUBLIC_HOST", "term.example.com")  # 真实域名在 .env；占位符不入库
MANAGE_HOST = ENV.get("MANAGE_HOST", "127.0.0.1")
MANAGE_PORT = int(ENV.get("MANAGE_PORT", "7690"))
HOME = str(Path.home())
PATH_ROOT = str(Path(ENV.get("SESSION_PATH_ROOT") or HOME).expanduser())
DEFAULT_PATH = str(Path(ENV.get("SESSION_DEFAULT_PATH") or HOME).expanduser())
SESSION_CTL = str(ROOT / "bin" / "session-ctl.sh")
NAME_RE = re.compile(r"^[\w\-]{1,64}$")  # 含中文等 Unicode 字母数字；不含 . : / 空白
TTYD_PORT = int(ENV.get("TTYD_PORT", "7681"))
TTYD_BASE_PATH = (ENV.get("TTYD_BASE_PATH") or "/term").rstrip("/") or "/term"


def _lan_ip_rank(ip: str) -> int:
    """给候选 IPv4 打分：真正的家用/办公局域网段优先，VPN/基准/保留段靠后。
    分数越大越优先；<=0 表示不适合作为局域网直连地址。"""
    if not ip or ip.startswith("127.") or ip.startswith("169.254."):
        return -1
    try:
        a, b = (int(x) for x in ip.split(".")[:2])
    except ValueError:
        return -1
    if a == 192 and b == 168:
        return 100  # 最常见家用/办公 Wi-Fi
    if a == 10:
        return 90
    if a == 172 and 16 <= b <= 31:
        return 80
    if a == 198 and b in (18, 19):
        return 1   # 198.18/15 基准测试段：常被 VPN/透明代理占用，局域网设备通常不可达
    if a == 100 and 64 <= b <= 127:
        return 2   # 100.64/10 CGNAT：运营商级，局域网内多半不可直连
    return 50      # 其它可路由/私有地址：可用但不如标准私有段


def _active_iface_ipv4s() -> list[str]:
    """macOS：ifconfig -l 列网卡 + ipconfig getifaddr 取地址。
    只有真实已配置(DHCP/手动)的网卡才有返回值，天然排除 utun 隧道与 lo0，
    比解析 ifconfig 全量 inet 更准——后者会把 VPN 的 198.18.0.1 也算进来。"""
    ips: list[str] = []
    try:
        names = subprocess.run(
            ["ifconfig", "-l"], capture_output=True, text=True, timeout=2
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return ips
    for nic in names:
        if nic.startswith(("lo", "utun", "gif", "stf", "awdl", "llw", "bridge")):
            continue
        try:
            ip = subprocess.run(
                ["ipconfig", "getifaddr", nic],
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if ip and ip not in ips:
            ips.append(ip)
    return ips


def _candidate_ipv4s() -> list[str]:
    import socket

    found: list[str] = []

    def add(ip: str) -> None:
        if ip and ip not in found:
            found.append(ip)

    # 真实活动网卡（Wi-Fi/有线）优先，隧道地址不会出现在这里
    for ip in _active_iface_ipv4s():
        add(ip)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))  # 不真正发包，只让内核选出出口地址
            add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        add_all = socket.gethostbyname_ex(socket.gethostname())[2]
        for ip in add_all:
            add(ip)
    except OSError:
        pass
    # 兜底：解析 ifconfig 全量 inet（可能含隧道地址，靠打分与活动网卡优先排后）
    try:
        out = subprocess.run(
            ["ifconfig"], capture_output=True, text=True, timeout=2
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("inet ") and "inet6" not in line:
                add(line.split()[1])
    except (OSError, subprocess.SubprocessError):
        pass
    return found


def _probe_lan() -> tuple[str, list[str]]:
    """返回 (最佳局域网 IP, 全部可用候选)。活动网卡优先，其次按私有段打分。
    候选仅保留标准私有段(192.168/10/172.16-31)，排除 VPN 基准段与 CGNAT。"""
    active = _active_iface_ipv4s()
    cands = [ip for ip in _candidate_ipv4s() if _lan_ip_rank(ip) >= 80]
    cands.sort(key=lambda ip: (ip in active, _lan_ip_rank(ip)), reverse=True)
    return (cands[0] if cands else ""), cands


# 后台周期探测：DHCP 续租/切换网络会让地址变化，页面不能显示旧地址。
# 同时避免每次 /api/sessions（5s 一次）都 fork 一批 ifconfig/ipconfig 子进程。
LAN_REFRESH_SEC = 20
_lan_lock = threading.Lock()
_lan_cache: dict = {"ip": "", "candidates": [], "ts": 0.0}


def _lan_refresh_once() -> None:
    ip, cands = _probe_lan()
    with _lan_lock:
        prev = _lan_cache.get("ip")
        _lan_cache["ip"] = ip
        _lan_cache["candidates"] = cands
        _lan_cache["ts"] = time.time()
    if ip != prev:
        sys.stderr.write(f"[lan] 局域网地址变化: {prev or '(空)'} -> {ip or '(空)'}\n")


def _lan_refresh_loop() -> None:
    while True:
        time.sleep(LAN_REFRESH_SEC)
        try:
            _lan_refresh_once()
        except Exception as e:  # 探测失败不能拖垮服务
            sys.stderr.write(f"[lan] 探测失败: {e}\n")


def start_lan_watcher() -> None:
    try:
        _lan_refresh_once()  # 启动即同步探一次，首个请求就有值
    except Exception as e:
        sys.stderr.write(f"[lan] 首次探测失败: {e}\n")
    threading.Thread(target=_lan_refresh_loop, name="lan-watcher", daemon=True).start()


def detect_lan_ip() -> str:
    """返回缓存的最佳局域网 IPv4（后台每 20s 刷新）；失败返回空串。"""
    with _lan_lock:
        ip = _lan_cache.get("ip") or ""
        ts = float(_lan_cache.get("ts") or 0)
    if time.time() - ts > LAN_REFRESH_SEC * 3:
        _lan_refresh_once()  # 兜底：后台线程若异常退出，同步补一次
        with _lan_lock:
            ip = _lan_cache.get("ip") or ""
    return ip


def lan_candidates() -> list[str]:
    with _lan_lock:
        return list(_lan_cache.get("candidates") or [])


MAX_PAGES = 2000  # 上限护栏：避免误填超大值把浏览器内存吃满
PAGES_FILE = ROOT / "run" / "scrollback-pages"  # 全局默认（管理页可改，运行时生效）


def _env_default_pages() -> int:
    raw = (ENV.get("SCROLLBACK_PAGES") or "30").split("#", 1)[0].strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 30


def default_pages() -> int:
    """全局默认回看页数：run/scrollback-pages > .env SCROLLBACK_PAGES > 30。
    每次读取文件，管理页改完立即生效，无需重启。"""
    try:
        raw = PAGES_FILE.read_text(encoding="utf-8").strip()
        n = int(raw)
        if 1 <= n <= MAX_PAGES:
            return n
    except (OSError, ValueError):
        pass
    return _env_default_pages()


def set_default_pages(pages: int) -> None:
    PAGES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PAGES_FILE.with_suffix(".tmp")
    tmp.write_text(f"{int(pages)}\n", encoding="utf-8")
    tmp.replace(PAGES_FILE)
ICON_SVG = ROOT / "web" / "wt-icon-manage.svg"
ICON_PNG = ROOT / "web" / "wt-icon-manage-64.png"


def favicon_links() -> str:
    """管理页原本没有 favicon（标签栏空白）。用 data URI 内联，避免额外请求与鉴权往返。"""
    try:
        svg = base64.b64encode(ICON_SVG.read_bytes()).decode()
        png = base64.b64encode(ICON_PNG.read_bytes()).decode()
    except OSError:
        return ""
    return (
        f'<link id="wt-favicon" rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,{svg}">'
        f'<link rel="alternate icon" type="image/png" sizes="64x64" href="data:image/png;base64,{png}">'
        f'<link rel="apple-touch-icon" href="data:image/png;base64,{png}">'
    )


PASTE_DIR = ROOT / "run" / "paste-images"
PASTE_MAX_BYTES = 12 * 1024 * 1024
PASTE_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

# ---- 流量统计 ----
# 终端页在浏览器侧统计 WebSocket 实际收发字节（这才是真正过网的量），
# 按 5s 增量上报到此处，按「本地日期 + 会话名」累加落盘。
TRAFFIC_FILE = ROOT / "run" / "traffic.json"
TRAFFIC_KEEP_DAYS = 30
_traffic_lock = threading.Lock()


def _today_key() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def _load_traffic() -> dict:
    try:
        raw = json.loads(TRAFFIC_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _prune_traffic(data: dict) -> dict:
    """只保留最近 TRAFFIC_KEEP_DAYS 天，避免文件无限增长。"""
    keys = sorted(k for k in data if isinstance(data.get(k), dict))
    for old in keys[:-TRAFFIC_KEEP_DAYS] if len(keys) > TRAFFIC_KEEP_DAYS else []:
        data.pop(old, None)
    return data


def add_traffic(name: str, rx: int, tx: int) -> dict:
    """累加某会话今日流量；返回 {today_rx, today_tx, session_rx, session_tx}。
    session_* 为该会话跨天累计（所有日期求和），供终端页显示"本会话累计"。"""
    if rx < 0 or tx < 0:
        raise ValueError("流量增量不能为负")
    with _traffic_lock:
        data = _prune_traffic(_load_traffic())
        day = data.setdefault(_today_key(), {})
        if not isinstance(day, dict):
            day = {}
            data[_today_key()] = day
        cur = day.setdefault(name, {"rx": 0, "tx": 0})
        if not isinstance(cur, dict):
            cur = {"rx": 0, "tx": 0}
            day[name] = cur
        cur["rx"] = int(cur.get("rx", 0)) + rx
        cur["tx"] = int(cur.get("tx", 0)) + tx
        try:
            TRAFFIC_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = TRAFFIC_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(TRAFFIC_FILE)  # 原子替换，避免并发读到半截文件
        except OSError as e:
            raise RuntimeError(f"写入流量统计失败: {e}") from e
        return _summarize(data, name)


def _summarize(data: dict, name: str = "") -> dict:
    day = data.get(_today_key(), {})
    if not isinstance(day, dict):
        day = {}
    today_rx = sum(int(v.get("rx", 0)) for v in day.values() if isinstance(v, dict))
    today_tx = sum(int(v.get("tx", 0)) for v in day.values() if isinstance(v, dict))
    s_rx = s_tx = 0
    if name:
        for d in data.values():
            if isinstance(d, dict) and isinstance(d.get(name), dict):
                s_rx += int(d[name].get("rx", 0))
                s_tx += int(d[name].get("tx", 0))
    return {
        "today_rx": today_rx,
        "today_tx": today_tx,
        "today_total": today_rx + today_tx,
        "session_rx": s_rx,
        "session_tx": s_tx,
        "session_total": s_rx + s_tx,
    }


def traffic_summary(name: str = "") -> dict:
    with _traffic_lock:
        return _summarize(_load_traffic(), name)


# ---- 文件上传 ----
# 上传落到服务端家目录 ~/Downloads；原始字节流直写，不走 base64（省 33% 膨胀）。
UPLOAD_DIR = Path.home() / "Downloads"
UPLOAD_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 单文件 2GB 上限
UPLOAD_BLOCK = 1024 * 1024

# 危险文件名：路径穿越 / 隐藏文件 / Windows 保留设备名
_UPLOAD_NAME_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_UPLOAD_WIN_DEV = re.compile(
    r'(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)'
)
_UPLOAD_EXT_BLOCK = {
    ".app", ".dmg", ".pkg", ".command", ".scpt", ".workflow", ".applescript",
    ".sh", ".bash", ".zsh", ".fish", ".ksh",
    ".exe", ".msi", ".bat", ".cmd", ".ps1", ".scr", ".vbs", ".js", ".jse",
    ".wsf", ".wsh", ".hta", ".jar", ".apk",
}
_UPLOAD_MAX_PATH = 400  # 落盘相对路径长度上限（macOS APFS 文件名≤255）


def _upload_rel_ok(rel: str) -> bool:
    """校验前端传来的相对路径：仅允许安全子路径，拒绝穿越/绝对路径/隐藏文件。"""
    if not rel or len(rel) > _UPLOAD_MAX_PATH:
        return False
    if rel.startswith("/") or rel.startswith("\\"):
        return False
    parts = rel.split("/")
    for i, p in enumerate(parts):
        if not p or p in (".", ".."):
            return False
        # 非法字符逐段检查（整串查会把合法的 / 分隔符也拦掉）
        if _UPLOAD_NAME_BAD.search(p):
            return False
        if p.startswith(".") and not (i == len(parts) - 1 and p == ".DS_Store"):
            # 允许 .DS_Store（拖整个文件夹常见）但拒绝其余隐藏文件/目录
            return False
        if i == len(parts) - 1:
            if _UPLOAD_WIN_DEV.match(p):
                return False
            ext = ("." + p.rsplit(".", 1)[-1].lower()) if "." in p else ""
            if ext in _UPLOAD_EXT_BLOCK:
                return False
    return True


def _unique_dest(dir_path: Path, filename: str) -> Path:
    """同名不覆盖：name.zip → name (1).zip → name (2).zip …"""
    dest = dir_path / filename
    if not dest.exists():
        return dest
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        stem, ext = filename, ""
    else:
        ext = "." + ext
    for i in range(1, 10000):
        cand = dir_path / f"{stem} ({i}){ext}"
        if not cand.exists():
            return cand
    raise RuntimeError("同名文件过多，无法生成不冲突的文件名")


def upload_dir_for(user: str) -> Path:
    """用户上传根目录：主用户 ~/Downloads（兼容旧数据），其他用户 ~/Downloads/<user>。"""
    if is_primary(user):
        return UPLOAD_DIR
    return UPLOAD_DIR / user


def upload_hist_file_for(user: str) -> Path:
    """用户上传历史文件：主用户沿用全局文件（兼容），其他用户独立。"""
    if is_primary(user):
        return UPLOAD_HISTORY_FILE
    return ROOT / "run" / f"upload-history-{session_ticket.sanitize_name(user)}.json"


def save_upload_stream(rel: str, length: int, rfile, user: str = "") -> tuple[Path, int]:
    """把请求体原始字节流写入用户上传目录/<rel>，流式落盘不占内存。
    返回 (最终路径, 实际字节数)。同名自动改名，绝不覆盖已有文件。"""
    if not _upload_rel_ok(rel):
        raise ValueError("不支持的文件名或路径")
    if length <= 0:
        raise ValueError("空文件")
    if length > UPLOAD_MAX_BYTES:
        raise ValueError(f"单文件超过 {UPLOAD_MAX_BYTES // (1024**3)}GB 上限")
    base = upload_dir_for(user)
    dest_dir = base
    rel_parts = rel.split("/")
    if len(rel_parts) > 1:
        dest_dir = base.joinpath(*rel_parts[:-1])
    dest_dir.mkdir(parents=True, exist_ok=True)
    # 目标目录不得越过用户上传根（防御纵深，正常到不了这里）
    real_dl = base.resolve()
    real_dir = dest_dir.resolve()
    # 目标目录必须等于上传根本身或位于其内部
    if real_dir != real_dl and real_dl not in real_dir.parents:
        raise ValueError("非法的目标目录")
    dest = _unique_dest(dest_dir, rel_parts[-1])
    written = 0
    try:
        with dest.open("wb") as f:
            while written < length:
                chunk = rfile.read(min(UPLOAD_BLOCK, length - written))
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
        if written != length:
            dest.unlink(missing_ok=True)
            raise ConnectionError(f"上传中断：收到 {written}/{length} 字节")
    except ValueError:
        raise
    except OSError as e:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"写入失败: {e}") from e
    record_upload(dest, written, user)
    return dest, written


def overwrite_upload_stream(rel: str, length: int, rfile, user: str = "") -> tuple[Path, int]:
    """覆盖写入用户上传目录/<rel>（重新上传历史文件）。同名直接覆盖，不改名。
    先写临时文件再原子替换：中途失败不会把原文件写坏。"""
    if not _upload_rel_ok(rel):
        raise ValueError("不支持的文件名或路径")
    if length <= 0:
        raise ValueError("空文件")
    if length > UPLOAD_MAX_BYTES:
        raise ValueError(f"单文件超过 {UPLOAD_MAX_BYTES // (1024**3)}GB 上限")
    dest = _resolve_upload_rel(rel, user)
    if not dest.exists():
        raise ValueError("目标文件不存在（可能已被删除），请重新上传")
    if dest.is_dir():
        raise ValueError("目标是目录，不能覆盖")
    tmp = dest.with_name(dest.name + ".wt-tmp")
    written = 0
    try:
        with tmp.open("wb") as f:
            while written < length:
                chunk = rfile.read(min(UPLOAD_BLOCK, length - written))
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
        if written != length:
            raise ConnectionError(f"上传中断：收到 {written}/{length} 字节")
        os.replace(tmp, dest)  # 原子替换，旧版本被完整覆盖
    except ValueError:
        tmp.unlink(missing_ok=True)
        raise
    except (ConnectionError, OSError) as e:
        tmp.unlink(missing_ok=True)
        if isinstance(e, ConnectionError):
            raise
        raise RuntimeError(f"写入失败: {e}") from e
    record_upload(dest, written, user)
    return dest, written


# ---- 上传历史：保留最近 10 条，供复制服务器全路径 ----
UPLOAD_HISTORY_FILE = ROOT / "run" / "upload-history.json"
UPLOAD_HISTORY_KEEP = 10
_upload_hist_lock = threading.Lock()


def load_upload_history(user: str = "") -> list[dict]:
    f = upload_hist_file_for(user)
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def new_upload_batch_dir(user: str = "") -> Path:
    """每次上传批次新建独立子目录 <用户上传根>/上传_<时间戳>。
    解压/整理只影响本批文件，不污染上传根其它内容。
    用服务端时钟命名（客户端时间不可信）；同一秒多批自动加 -2/-3 序号。"""
    base = upload_dir_for(user)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    for i in range(100):
        name = f"上传_{stamp}" if i == 0 else f"上传_{stamp}-{i}"
        d = base / name
        try:
            d.mkdir(parents=True, exist_ok=False)
            return d
        except FileExistsError:
            continue
    raise RuntimeError("无法创建上传目录")


def record_upload(dest: Path, size: int, user: str = "") -> None:
    """追加一条上传记录到该用户的历史，只保留最近 KEEP 条。失败不影响上传结果。"""
    item = {
        "path": str(dest),
        "rel": dest.name,
        "bytes": size,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
    }
    hist_file = upload_hist_file_for(user)
    try:
        with _upload_hist_lock:
            hist = load_upload_history(user)
            hist.append(item)
            hist_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = hist_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(hist[-UPLOAD_HISTORY_KEEP:], ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(hist_file)  # 原子替换
    except OSError as e:
        sys.stderr.write(f"[upload] 记录历史失败(不影响文件): {e}\n")


def prune_upload_history(user: str = "") -> list[dict]:
    """清理不存在于磁盘的记录，供前端展示前调用（按用户）。"""
    hist_file = upload_hist_file_for(user)
    with _upload_hist_lock:
        hist = load_upload_history(user)
        kept = [h for h in hist if Path(h.get("path", "")).exists()]
        if len(kept) != len(hist):
            try:
                hist_file.write_text(
                    json.dumps(kept[-UPLOAD_HISTORY_KEEP:], ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
            except OSError:
                pass
        return kept[-UPLOAD_HISTORY_KEEP:]


def _resolve_upload_rel(rel: str, user: str = "") -> Path:
    """把历史记录里的相对路径安全解析到该用户上传根内的绝对路径。
    复用上传的路径校验（拒绝穿越/绝对路径/隐藏文件），并二次确认
    解析结果确实位于该用户上传根内（防 symlink 逃逸）。"""
    if not _upload_rel_ok(rel):
        raise ValueError("不支持的文件名或路径")
    base = upload_dir_for(user)
    dest = base.joinpath(*rel.split("/"))
    real_dl = base.resolve()
    real_dest = dest.resolve(strict=False)
    if real_dest != real_dl and real_dl not in real_dest.parents:
        raise ValueError("非法的目标路径")
    return dest


def remove_upload_file(path: str, user: str = "") -> dict:
    """删除上传历史对应的文件（仅限该用户上传根内），同步清理历史记录。"""
    base = upload_dir_for(user)
    try:
        rel = str(Path(path).resolve().relative_to(base.resolve()))
    except ValueError:
        raise ValueError("只能删除自己上传目录内的文件")
    dest = _resolve_upload_rel(rel, user)
    if not dest.exists():
        _prune_hist_record(str(dest), user)
        return {"ok": True, "already_gone": True, "rel": rel}
    if dest.is_dir():
        raise ValueError("是目录不是文件，请手动删除")
    dest.unlink()
    _prune_hist_record(str(dest), user)
    sys.stderr.write(f"[upload] deleted {rel}\n")
    return {"ok": True, "rel": rel}


def _prune_hist_record(abs_path: str, user: str = "") -> None:
    """从该用户的上传历史里移除一条记录（按绝对路径匹配）。"""
    hist_file = upload_hist_file_for(user)
    with _upload_hist_lock:
        hist = load_upload_history(user)
        kept = [h for h in hist if h.get("path") != abs_path]
        if len(kept) != len(hist):
            try:
                hist_file.write_text(
                    json.dumps(kept[-UPLOAD_HISTORY_KEEP:], ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
            except OSError:
                pass


# ---- 服务端文件浏览 / 预览 / 下载 ----
# 浏览范围由 FS_PATH_ROOT 单独控制（默认 /，即全盘；可收紧到某个子目录），
# 与 SESSION_PATH_ROOT（限制新建会话工作目录）互不影响。
# 所有入口都先过 _fs_resolve：resolve 后必须仍在根内，防穿越/symlink 逃逸。
FS_ROOT = Path(str(ENV.get("FS_PATH_ROOT") or "/")).expanduser().resolve()
FS_LIST_MAX = 2000          # 单目录最多列出条目
FS_TEXT_MAX = 2 * 1024 * 1024   # 文本预览上限 2MB
FS_IMAGE_MAX = 64 * 1024 * 1024 # 图片预览/下载上限 64MB
FS_DOWNLOAD_MAX = 2 * 1024 ** 3 # 下载上限 2GB（流式）

_FS_TEXT_EXT = {
    ".txt", ".md", ".markdown", ".log", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".env", ".csv", ".tsv",
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".java", ".kt", ".kts", ".go", ".rs",
    ".rb", ".php", ".swift", ".m", ".mm", ".sh", ".bash", ".zsh", ".fish",
    ".sql", ".html", ".htm", ".css", ".scss", ".less", ".xml", ".svg",
    ".gitignore", ".gitattributes", ".dockerfile", ".makefile", ".cmake",
}
_FS_TEXT_NAMES = {"Makefile", "Dockerfile", "LICENSE", "README", "CHANGELOG", ".gitignore", ".env"}
_FS_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".avif"}


def _fs_resolve(rel: str) -> Path:
    """把 URL 里的相对路径安全解析到根内绝对路径。空串=根目录。
    拒绝绝对路径；resolve 后必须位于根内（防 ../ 穿越 与 symlink 逃逸）。"""
    rel = (rel or "").strip().lstrip("/")
    if len(rel) > 1024:
        raise ValueError("路径过长")
    if rel.startswith("\\") or "\x00" in rel:
        raise ValueError("非法路径")
    p = FS_ROOT.joinpath(*[seg for seg in rel.split("/") if seg and seg != "."]) if rel else FS_ROOT
    real = p.resolve(strict=False)
    if real != FS_ROOT and FS_ROOT not in real.parents:
        raise ValueError("路径越界")
    return real


def _fs_rel_from_abs(user_input: str) -> str:
    """用户输入的绝对路径 → 根内相对路径（供跳转）。
    接受三种：绝对路径（必须在根内）、~/ 开头、已是相对路径。
    用 os.path.normpath 消化冗余段后统一走 _fs_resolve 的根内校验。"""
    s = (user_input or "").strip()
    if not s:
        return ""
    if s.startswith("~/"):
        s = str(Path.home() / s[2:])
    if s.startswith("/"):
        try:
            abs_p = Path(os.path.normpath(s)).resolve(strict=False)
        except OSError as e:
            raise ValueError(f"无效路径: {e}")
        root = FS_ROOT
        if abs_p == root or root in abs_p.parents:
            rel = str(abs_p.relative_to(root))
            return "" if rel == "." else rel
        raise ValueError("路径不在可浏览范围内（" + str(root) + " 内）")
    # 相对路径：交给 _fs_resolve 校验（含 ../ 穿越拦截）
    real = _fs_resolve(s)
    return str(real.relative_to(FS_ROOT)) if real != FS_ROOT else ""


def _fs_kind(p: Path) -> str:
    name = p.name.lower()
    ext = p.suffix.lower()
    if ext in _FS_IMAGE_EXT:
        return "image"
    if ext in _FS_TEXT_EXT or name in _FS_TEXT_NAMES or (not ext and name.startswith(".")):
        return "text"
    return "binary"


FS_ZIP_CONFIRM = 1024 ** 3   # 文件夹超过 1GB 前端需二次确认
FS_SIZE_TIMEOUT = 15.0       # 统计大小最长耗时，超时按“至少 N”返回
_zip_slots = threading.BoundedSemaphore(2)   # 同时最多 2 路打包，防止拖垮服务


def _fs_walk(top: Path):
    """遍历 top 下内容：不跟随软链接、不跨文件系统（避开 /proc /sys 等伪文件系统）。
    产出 (kind, path, rel_in_top, size)：kind 为 'dir' | 'file'。目录先于其内容产出。"""
    try:
        dev = top.stat().st_dev
    except OSError:
        return
    stack = [(top, "")]
    while stack:
        d, prefix = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            entries = list(it)
        entries.sort(key=lambda e: e.name)
        subdirs = []
        for e in entries:
            rel = f"{prefix}{e.name}"
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if _stat.S_ISDIR(st.st_mode):
                if st.st_dev != dev:
                    continue
                yield "dir", Path(e.path), rel, 0
                subdirs.append((Path(e.path), rel + "/"))
            elif _stat.S_ISREG(st.st_mode):
                yield "file", Path(e.path), rel, st.st_size
        stack.extend(reversed(subdirs))


def fs_dir_stats(top: Path, timeout: float = FS_SIZE_TIMEOUT) -> dict:
    """统计文件夹：总字节、文件数、目录数；超时则 complete=False（大小为已统计部分，即“至少”）。"""
    deadline = time.monotonic() + timeout
    size = files = dirs = 0
    complete = True
    for kind, _p, _rel, sz in _fs_walk(top):
        if kind == "dir":
            dirs += 1
        else:
            files += 1
            size += sz
        if (files + dirs) % 256 == 0 and time.monotonic() > deadline:
            complete = False
            break
    return {"size": size, "files": files, "dirs": dirs, "complete": complete}


class _ZipSink:
    """给 zipfile 的不可 seek 输出：直接写 socket，边压边发。"""
    def __init__(self, wfile):
        self._w = wfile
        self.sent = 0

    def write(self, b: bytes) -> int:
        self._w.write(b)
        self.sent += len(b)
        return len(b)

    def flush(self) -> None:
        self._w.flush()


def fs_stream_zip(top: Path, wfile) -> tuple[int, int]:
    """把文件夹流式压成 zip 写入 wfile。返回 (文件数, 已发送字节)。
    顶层目录名保留为压缩包内的根目录；读不了的文件直接跳过。"""
    root_name = top.name or "root"
    sink = _ZipSink(wfile)
    n = 0
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as zf:
        zi = zipfile.ZipInfo(root_name + "/", time.localtime(top.stat().st_mtime)[:6])
        zi.external_attr = (0o40755 << 16) | 0x10
        zf.writestr(zi, b"")
        for kind, path, rel, _sz in _fs_walk(top):
            arc = f"{root_name}/{rel}"
            if kind == "dir":
                try:
                    zi = zipfile.ZipInfo(arc + "/", time.localtime(path.stat().st_mtime)[:6])
                except (OSError, ValueError):
                    zi = zipfile.ZipInfo(arc + "/")
                zi.external_attr = (0o40755 << 16) | 0x10
                zf.writestr(zi, b"")
                continue
            try:
                src = path.open("rb")   # 先打开：无权限的文件整条跳过，不留残缺条目
            except OSError:
                continue
            with src:
                try:
                    zi = zipfile.ZipInfo.from_file(path, arc)
                except (OSError, ValueError):
                    continue
                zi.compress_type = zipfile.ZIP_DEFLATED
                with zf.open(zi, "w") as dst:
                    while True:
                        try:
                            chunk = src.read(UPLOAD_BLOCK)
                        except OSError:
                            break
                        if not chunk:
                            break
                        dst.write(chunk)
            n += 1
    return n, sink.sent


def fs_list(rel: str) -> dict:
    """列目录：目录在前、名称不区分大小写排序；附带每项可预览类型。"""
    d = _fs_resolve(rel)
    if not d.exists():
        raise FileNotFoundError("目录不存在")
    if not d.is_dir():
        raise ValueError("不是目录")
    try:
        entries = list(d.iterdir())
    except PermissionError as e:
        raise PermissionError("无权访问该目录") from e
    def sort_key(ep: Path):
        try:
            return (not ep.is_dir(), ep.name.lower())
        except OSError:
            return (True, ep.name.lower())
    entries.sort(key=sort_key)
    total = len(entries)
    entries = entries[: FS_LIST_MAX]
    items = []
    for ep in entries:
        try:
            st = ep.stat()
        except OSError:
            continue
        rel_child = str(ep.relative_to(FS_ROOT))
        item = {
            "name": ep.name,
            "dir": ep.is_dir(),
            "size": 0 if ep.is_dir() else st.st_size,
            "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            "rel": rel_child,
        }
        if not item["dir"]:
            item["kind"] = _fs_kind(ep)
            # 预览大小超限就只给下载
            item["previewable"] = (item["kind"] == "text" and st.st_size <= FS_TEXT_MAX) or (
                item["kind"] == "image" and st.st_size <= FS_IMAGE_MAX
            )
        items.append(item)
    rel_out = ""
    if d != FS_ROOT:
        try:
            rel_out = str(d.relative_to(FS_ROOT))
        except ValueError:
            rel_out = ""
    return {"root": str(FS_ROOT), "rel": rel_out,
            "total": total, "truncated": total > len(items), "items": items}


MANAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Web Terminal · 会话管理</title>
__FAVICON__
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
  .layout { display: flex; min-height: 100vh; }
  .side {
    width: 220px; flex: none; position: sticky; top: 0; height: 100vh;
    display: flex; flex-direction: column; padding: 22px 14px 16px;
    background: color-mix(in srgb, var(--panel) 92%, transparent);
    border-right: 1px solid var(--line); backdrop-filter: blur(8px);
  }
  .brand { margin: 0 8px 22px; font-size: 18px; font-weight: 700; letter-spacing: -0.01em; }
  .brand small { display: block; margin-top: 2px; font-size: 12px; font-weight: 400; color: var(--muted); letter-spacing: 0; }
  .nav { display: flex; flex-direction: column; gap: 4px; flex: 1; }
  .navitem {
    display: block; padding: 10px 12px; border-radius: 8px; color: var(--muted);
    text-decoration: none; border-left: 3px solid transparent; cursor: pointer;
  }
  .navitem:hover { background: rgba(255,255,255,.05); color: var(--text); }
  .navitem.active { background: rgba(61,139,253,.14); color: var(--text); border-left-color: var(--accent); font-weight: 600; }
  .userbox { border-top: 1px solid var(--line); padding: 14px 8px 0; display: flex; flex-direction: column; gap: 10px; }
  .userbox .who { color: var(--muted); font-size: 13px; word-break: break-all; }
  .userbox .who b { color: var(--text); font-weight: 600; }
  .userbox button { width: 100%; }
  main { flex: 1; min-width: 0; width: 100%; max-width: 2400px; margin: 0 auto; padding: 28px clamp(14px, 2vw, 36px) 72px; }
  .viewtitle { margin: 0 0 18px; font-size: 24px; letter-spacing: -0.02em; }
  @media (max-width: 760px) {
    .layout { flex-direction: column; }
    .side { width: auto; height: auto; position: static; flex-direction: row; flex-wrap: wrap; align-items: center; gap: 8px; padding: 10px 12px; border-right: 0; border-bottom: 1px solid var(--line); }
    .brand { margin: 0 8px 0 0; font-size: 16px; }
    .brand small { display: none; }
    .nav { flex-direction: row; flex: 1 1 100%; order: 3; overflow-x: auto; gap: 6px; }
    .navitem { white-space: nowrap; border-left: 0; border-bottom: 3px solid transparent; border-radius: 6px 6px 0 0; padding: 8px 12px; }
    .navitem.active { border-bottom-color: var(--accent); }
    .userbox { border-top: 0; padding: 0; flex-direction: row; align-items: center; margin-left: auto; }
    .userbox button { width: auto; padding: 6px 10px; }
  }
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
  textarea.promptbox {
    display: block; width: 100%; min-height: 520px; box-sizing: border-box; resize: vertical;
    background: #0c1117; color: var(--text); caret-color: var(--accent);
    border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 12.5px; line-height: 1.55; tab-size: 4;
  }
  textarea.promptbox:focus { outline: none; border-color: var(--accent); }
  textarea.promptbox::selection { background: rgba(61,139,253,.4); }
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
  th.name-col, td.name-cell { width: 220px; }
  td.name-cell { overflow: hidden; }
  th.path-col { width: auto; }
  th.time-col, td.time-cell {
    width: 148px; white-space: nowrap;
    font-variant-numeric: tabular-nums;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px; color: var(--muted);
  }
  th.act-col, td.actions { width: 212px; }
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
  /* 弹窗内的错误提示：弹窗遮罩会挡住页面顶部 flash，错误必须就地显示 */
  .modal-err { margin: -4px 0 10px; font-size: 12px; color: #ff8fab; word-break: break-all; }
  .lanbar { margin: 0 0 14px; padding: 10px 12px; border-radius: 8px; background: rgba(46,160,67,.12); color: #7ee787; font-size: 13px; line-height: 1.6; }
  .lanbar code { background: rgba(0,0,0,.25); padding: 1px 6px; border-radius: 4px; color: #d2e6ff; user-select: all; }
  .trafficbar {
    margin: 10px 0 0; font-size: 13px; color: var(--muted);
    display: flex; gap: 16px; flex-wrap: wrap; align-items: baseline;
  }
  .trafficbar b { color: var(--text); font-weight: 600; font-variant-numeric: tabular-nums; }
  .trafficbar .sep { color: var(--line); }
  .updrop {
    margin: 12px 0 8px; padding: 22px 16px; text-align: center;
    border: 1.5px dashed var(--line); border-radius: 10px;
    color: var(--muted); font-size: 13px; transition: border-color .15s, background .15s;
  }
  .updrop.over { border-color: var(--accent); background: rgba(61,139,253,.08); color: var(--text); }
  .upprogress { margin: 10px 0 14px; font-size: 13px; }
  .upprogress .bar {
    height: 6px; border-radius: 3px; background: #243041; overflow: hidden; margin: 8px 0 4px;
  }
  .upprogress .bar > i {
    display: block; height: 100%; width: 0;
    background: var(--accent); transition: width .2s;
  }
  .upprogress .item { color: var(--muted); font-size: 12px; word-break: break-all; }
  .upprogress .done { color: var(--ok); }
  .upprogress .fail { color: var(--warn); }
  .uphistory { margin: 4px 0 8px; }
  .uphistory h3 {
    margin: 10px 0 6px; font-size: 12px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.04em; font-weight: 600;
  }
  .uphistory ul { list-style: none; margin: 0; padding: 0; }
  .uphistory li {
    display: flex; gap: 8px; align-items: center;
    padding: 6px 8px; border-radius: 8px; font-size: 13px;
  }
  .uphistory li:hover { background: rgba(255,255,255,.03); }
  .uphistory .fname {
    flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
  }
  .uphistory .fmeta { color: var(--muted); font-size: 11px; white-space: nowrap; }
  .uphistory .cp {
    appearance: none; border: 0; cursor: pointer; border-radius: 6px;
    padding: 4px 10px; font-size: 12px; background: #2b3645; color: var(--text);
  }
  .uphistory .cp:hover { background: var(--accent); }
  .uphistory .cp.ok { background: rgba(61,214,140,.2); color: var(--ok); }
  .uphistory button.op {
    appearance: none; border: 0; cursor: pointer; border-radius: 6px;
    padding: 4px 10px; font-size: 12px; background: #2b3645; color: var(--text);
  }
  .uphistory button.op:hover:not(:disabled) { background: var(--accent); }
  .uphistory button.op:disabled { opacity: .5; cursor: default; }
  .uphistory button.op.del:hover:not(:disabled) { background: var(--danger); }
  .uphistory .histpager {
    display: flex; align-items: center; gap: 10px; justify-content: flex-end;
    padding: 6px 8px 2px; font-size: 12px; color: var(--muted);
  }
  .uphistory .histpager .pg {
    appearance: none; border: 1px solid var(--line); cursor: pointer;
    border-radius: 6px; padding: 3px 10px; font-size: 12px;
    background: #2b3645; color: var(--text);
  }
  .uphistory .histpager .pg:hover:not(:disabled) { background: var(--accent); }
  .uphistory .histpager .pg:disabled { opacity: .4; cursor: default; }
  .fscrumb {
    font: 12px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace;
    color: var(--muted); margin: 2px 0 8px; word-break: break-all;
  }
  .fscrumb a { color: var(--accent); text-decoration: none; cursor: pointer; }
  .fscrumb a:hover { text-decoration: underline; }
  .fslist { max-height: 480px; overflow-y: auto; border: 1px solid var(--line); border-radius: 8px; }
  .fslist table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .fslist th {
    position: sticky; top: 0; background: var(--panel);
    text-align: left; font-weight: 600; color: var(--muted);
    padding: 8px 10px; border-bottom: 1px solid var(--line); font-size: 12px;
  }
  .fslist td { padding: 6px 10px; border-bottom: 1px solid rgba(255,255,255,.04); }
  .fslist tr:hover td { background: rgba(255,255,255,.03); }
  .fslist .dname { cursor: pointer; color: var(--accent); }
  .fslist .dname.file { color: var(--text); }
  .fslist .sz { color: var(--muted); white-space: nowrap; font-size: 12px; }
  .fslist .ops { white-space: nowrap; }
  .fslist button.op2 {
    appearance: none; border: 0; cursor: pointer; border-radius: 6px;
    padding: 3px 9px; font-size: 12px; background: #2b3645; color: var(--text); margin-right: 4px;
  }
  .fslist button.op2:hover { background: var(--accent); }
  .fsview-mask {
    position: fixed; inset: 0; background: rgba(5,8,12,.82);
    z-index: 10050; display: flex; align-items: center; justify-content: center;
  }
  .fsview {
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    width: min(92vw, 1100px); max-height: 88vh; display: flex; flex-direction: column;
  }
  .fsview .vhead {
    display: flex; align-items: center; gap: 10px;
    padding: 12px 16px; border-bottom: 1px solid var(--line);
  }
  .fsview .vhead b { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
  .fsview .vbody { flex: 1; overflow: auto; padding: 12px 16px; }
  .fsview pre {
    margin: 0; font: 12px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace;
    white-space: pre-wrap; word-break: break-all; color: var(--text);
  }
  .fsview img { max-width: 100%; max-height: 70vh; display: block; margin: 0 auto; }
  .fsview .vfoot { padding: 8px 16px; color: var(--muted); font-size: 12px; border-top: 1px solid var(--line); }
  .lanbar .muted { color: var(--muted); }
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
    display: block; max-width: 100%;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .name-edit:hover { color: var(--accent); border-bottom-color: var(--accent); }
  th.pages-col, td.pages-cell { width: 96px; white-space: nowrap; }
  .pages-edit {
    appearance: none; border: 0; background: transparent; color: var(--text);
    font: inherit; padding: 0; margin: 0; cursor: pointer; text-align: left;
    border-bottom: 1px dashed color-mix(in srgb, var(--muted) 55%, transparent);
  }
  .pages-edit:hover { color: var(--accent); border-bottom-color: var(--accent); }
  .pages-edit.is-default { color: var(--muted); }
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
<div class="layout">
<aside class="side">
  <div class="brand">Web Terminal<small>会话管理</small></div>
  <nav class="nav" id="nav">
    <a class="navitem" data-view="sessions" href="#sessions">会话</a>
    <a class="navitem" data-view="upload" href="#upload">上传</a>
    <a class="navitem" data-view="files" href="#files">下载</a>
    <a class="navitem" data-view="llm" href="#llm">接入 AI</a>
    <a class="navitem" data-view="users" href="#users" id="navUsers" hidden>用户管理</a>
  </nav>
  <div class="userbox">
    <span id="whoami" class="who"></span>
    <button id="btnLogout" class="secondary" type="button" title="退出当前账号">退出登录</button>
  </div>
</aside>
<main>
  <div id="flash" class="flash" hidden></div>

  <div class="view" data-view="sessions">
    <h1 class="viewtitle">会话</h1>
    <div id="trafficBar" class="trafficbar" hidden></div>
    <div id="lanBar" class="lanbar" hidden></div>
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
        <button id="btnDefaultPages" class="secondary" type="button" title="设置全局默认回看页数">全局回看…</button>
        <button id="btnBulkDel" class="danger" type="button">删除选中</button>
      </div>
    </div>
    <p class="hint" style="margin-top:8px"><span class="dot green"></span> 可恢复 &nbsp;&nbsp; <span class="dot red"></span> 已断开 &nbsp;&nbsp; 回看列显示「默认 N」= 未单独配置，跟随全局</p>
    <div id="listWrap"><p class="empty">加载中…</p></div>
  </section>

  </div>

  <div class="view" data-view="upload" hidden>
    <h1 class="viewtitle">上传</h1>
  <section class="card">
    <div class="row" style="justify-content:space-between;align-items:center">
      <h2 style="margin:0;font-size:16px">上传到服务器</h2>
      <div class="actions">
        <button id="btnUpFile" class="secondary" type="button">选择文件</button>
        <button id="btnUpDir" class="secondary" type="button">选择文件夹</button>
      </div>
    </div>
    <div id="upDrop" class="updrop">
      拖放文件或整个文件夹到此处
      <div class="muted" style="margin-top:4px">每批自动新建 ~/Downloads/上传_日期-time 子文件夹 · 文件夹保留目录结构 · 互不影响其它文件</div>
    </div>
    <div id="upProgress" class="upprogress" hidden></div>
    <div id="upHistory" class="uphistory" hidden></div>
    <input id="upFileInput" type="file" multiple hidden>
    <input id="upDirInput" type="file" webkitdirectory directory multiple hidden>
  </section>

  </div>

  <div class="view" data-view="files" hidden>
    <h1 class="viewtitle">下载</h1>
  <section class="card">
    <div class="row" style="justify-content:space-between;align-items:center">
      <h2 style="margin:0;font-size:16px">服务端文件 <span class="sub" style="font-size:12px;font-weight:400">文件直接下载，文件夹打包为 zip</span></h2>
      <div class="actions">
        <button id="fsRefresh" class="secondary" type="button">刷新</button>
        <button id="fsUpDir" class="secondary" type="button">上一级</button>
        <button id="fsRoot" class="secondary" type="button">根目录</button>
      </div>
    </div>
    <div id="fsCrumb" class="fscrumb"></div>
    <div class="row" style="margin-bottom:8px">
      <input id="fsPathInput" type="text" placeholder="输入绝对路径（如 /Users/xxx/web-terminal）或相对路径，回车跳转" autocomplete="off" autocapitalize="off" spellcheck="false">
      <button id="fsGo" type="button">跳转</button>
    </div>
    <div id="fsList" class="fslist"><p class="empty">加载中…</p></div>
  </section>
  </div>

  <div class="view" data-view="llm" hidden>
    <h1 class="viewtitle">接入你的 AI</h1>
  <section class="card">
    <div class="row" style="justify-content:space-between;align-items:center">
      <h2 style="margin:0;font-size:16px">让你的 AI 编程工具接管这个终端 <span class="sub" style="font-size:12px;font-weight:400">复制一段设置提示词，粘贴给 Claude Code / Codex / Cursor 等即可</span></h2>
      <div class="actions">
        <button id="llmCopy" type="button">复制设置提示词</button>
        <button id="llmReload" class="secondary" type="button">刷新</button>
      </div>
    </div>
    <p class="sub" style="margin:8px 0">内容已按当前访问地址自动填好，不含账号密码：agent 会向你索取，你直接告诉它即可。想限制权限，可让管理员在「用户管理」里为 agent 单独建一个账号。agent 会下载服务器自带的命令行工具 wt，用它执行命令、读屏、传文件；也附带了 Chrome CDP 方式。命令行工具由服务器提供且已测试，agent 直接下载使用即可。</p>
    <textarea id="llmText" class="promptbox" readonly spellcheck="false" placeholder="加载中…"></textarea>
  </section>
  </div>

  <div class="view" data-view="users" hidden>
    <h1 class="viewtitle">用户管理</h1>
  <section class="card">
    <div class="row" style="justify-content:space-between;align-items:center">
      <h2 style="margin:0;font-size:16px">用户管理</h2>
      <div class="actions"><button id="usRefresh" class="secondary" type="button">刷新</button></div>
    </div>
    <div class="row" style="margin-bottom:8px">
      <input id="usName" type="text" placeholder="新用户名（字母/数字/短横线）" maxlength="32" autocomplete="off" autocapitalize="off" spellcheck="false">
      <input id="usPass" type="password" placeholder="密码（至少 6 位）" maxlength="128" autocomplete="new-password">
      <button id="usAdd" type="button">添加用户</button>
    </div>
    <div id="usList"><p class="empty">加载中…</p></div>
  </section>

  </div>
</main>
</div>

<div id="nameModal" class="modal-mask" hidden>
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="nameTitle">
    <h3 id="nameTitle">新建会话</h3>
    <p id="nameHint">输入会话名称</p>
    <input id="nameInput" type="text" maxlength="64" autocomplete="off" placeholder="例如 work / web终端">
    <p id="nameErr" class="modal-err" hidden></p>
    <div class="row">
      <button id="nameCancel" class="secondary" type="button">取消</button>
      <button id="nameConfirm" type="button">下一步</button>
    </div>
  </div>
</div>

<div id="pagesModal" class="modal-mask" hidden>
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="pagesTitle">
    <h3 id="pagesTitle">回看页数</h3>
    <p id="pagesHint">设置本会话滚轮回看的历史页数</p>
    <input id="pagesInput" type="text" inputmode="numeric" autocomplete="off" placeholder="留空 = 用全局默认">
    <div class="row">
      <button id="pagesCancel" class="secondary" type="button">取消</button>
      <button id="pagesReset" class="secondary" type="button">恢复默认</button>
      <button id="pagesConfirm" type="button">保存</button>
    </div>
  </div>
</div>

<script>
const termBase = '/term/';
let nameModalMode = null; // { type: 'create' } | { type: 'rename', oldName } | { type: 'clone', cwd, srcName }
let cache = { sessions: [], history: [], path_root: '', default_path: '' };
const rowSelected = new Set(); // name + \\u001e + cwd + \\u001e + live|dead
let lastRowChkIndex = null; // 多选：普通点击锚点，供 Shift 范围选取
let sortKey = 'status';
let sortDir = 1; // 1 asc, -1 desc

// 局域网直连：若当前不是经 Cloudflare 隧道（公网域名）访问，则没有 /term 路径路由，
// 需把票据返回的相对 /term/… 改写成 http://<本机>:<ttyd端口>/term/…，延迟更低。
function fmtBytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + ' B';
  const u = ['KB', 'MB', 'GB', 'TB'];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return (n < 10 ? n.toFixed(2) : n < 100 ? n.toFixed(1) : Math.round(n)) + ' ' + u[i];
}

function renderTrafficBar() {
  const el = document.getElementById('trafficBar');
  if (!el) return;
  const t = cache && cache.traffic;
  if (!t) { el.hidden = true; return; }
  el.innerHTML =
    '今日总流量 <b>' + esc(fmtBytes(t.today_total)) + '</b>' +
    '<span class="sep">·</span>下行 <b>' + esc(fmtBytes(t.today_rx)) + '</b>' +
    '<span class="sep">·</span>上行 <b>' + esc(fmtBytes(t.today_tx)) + '</b>' +
    '<span class="sep">·</span><span>终端 WebSocket 实际收发，按会话累计</span>';
  el.hidden = false;
}

function offTunnel() {
  const pub = cache && cache.public_host;
  return !!pub && location.hostname !== pub;
}

function resolveTermUrl(relUrl) {
  if (!offTunnel() || !cache.ttyd_port) return relUrl;
  try {
    // relUrl 形如 /term/?arg=...；保留其 path+query，仅换 host:port
    const u = new URL(relUrl, location.origin);
    return location.protocol + '//' + location.hostname + ':' + cache.ttyd_port + u.pathname + u.search;
  } catch (e) {
    return relUrl;
  }
}


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
    pages: Number(s.pages) || 0,
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
      pages: Number(h.pages) || 0,
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
  if (res.status === 401) {
    // 登录已过期或已登出：回登录页重新登录
    location.href = '/';
    throw new Error('登录已过期，请重新登录');
  }
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

async function fetchTicketUrl(item) {
  const body = { name: item.name, create: !!item.create };
  if (item.create && item.cwd) body.cwd = item.cwd;
  const data = await api('/api/ticket', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return data.url;
}

async function tryOpen(item) {
  try {
    const url = await fetchTicketUrl(item);
    openTermTab(resolveTermUrl(url));
    flash('已在新标签打开会话');
    await refresh();
  } catch (e) {
    flash(String(e.message || e), true);
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
    '<th class="pages-col" title="每会话可单独配置；未配置则用全局默认">回看</th>' +
    '<th class="sortable time-col" data-sort="time">时间' + sortArrow('time') + '</th>' +
    '<th class="act-col"></th></tr></thead><tbody>';
  for (const s of filtered) {
    const key = rowKey(s);
    const checked = rowSelected.has(key) ? ' checked' : '';
    const dot = s.live ? '<span class="dot green" title="可恢复"></span>' : '<span class="dot red" title="已断开"></span>';
    const defPages = Number(cache.default_pages) || 30;
    const inherited = !(s.pages > 0);
    const pagesLabel = inherited ? ('默认 ' + defPages) : (s.pages + ' 页');
    const pagesCls = inherited ? 'pages-edit is-default' : 'pages-edit';
    const pagesTitle = inherited
      ? '未单独配置，跟随全局默认 ' + defPages + ' 页；点击可单独设置'
      : '本会话单独配置 ' + s.pages + ' 页；点击可改回跟随全局';
    html += `<tr>
      <td class="chk"><input type="checkbox" class="row-chk" data-key="${esc(key)}"${checked}></td>
      <td class="status-cell">${dot}</td>
      <td class="name-cell"><button type="button" class="name-edit" data-rename="${esc(s.name)}" title="点击修改名称">${esc(s.name)}</button></td>
      <td class="path" title="${esc(s.path || '')}">${esc(s.path || '-')}</td>
      <td class="pages-cell"><button type="button" class="${pagesCls}" data-pages="${esc(key)}" title="${esc(pagesTitle)}">${esc(pagesLabel)}</button></td>
      <td class="time-cell">${esc(formatTime(s.time))}</td>
      <td class="actions">
        <button type="button" data-open-row="${esc(key)}">open</button>
        <button type="button" class="secondary" data-clone-name="${esc(s.name)}" data-clone-cwd="${esc(s.path || s.cwd || '')}" title="克隆：沿用此路径新建会话">克隆</button>
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
      tryOpen(item);
    };
  });
  wrap.querySelectorAll('[data-rename]').forEach(btn => {
    btn.onclick = () => openRenameModal(btn.getAttribute('data-rename') || '');
  });
  wrap.querySelectorAll('[data-clone-name]').forEach(btn => {
    btn.onclick = () => openCloneModal(btn.getAttribute('data-clone-name') || '', btn.getAttribute('data-clone-cwd') || '');
  });
  wrap.querySelectorAll('[data-pages]').forEach(btn => {
    btn.onclick = () => openPagesModal(parseRowKey(btn.getAttribute('data-pages')));
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
  const gbtn = document.getElementById('btnDefaultPages');
  if (gbtn) gbtn.textContent = '全局回看：' + defaultPages() + ' 页';
  renderList();
}

function renderLanBar() {
  const el = document.getElementById('lanBar');
  if (!el) return;
  const lan = cache && cache.lan_ip;
  // 端口必须用服务端真实监听端口：经隧道访问时 location.port 为空（HTTPS 默认 443），
  // 拿它拼局域网地址会得到 http://<lan>:443 这种打不开的 URL。
  const mport = (cache && cache.manage_port) || location.port || '7690';
  if (!lan) { el.hidden = true; return; }
  if (offTunnel()) {
    // 已在局域网/本机直连：终端会以 http://<本机>:<ttyd端口> 打开，延迟更低
    el.innerHTML = '局域网直连模式已启用 <span class="muted">· 打开终端将走本机地址，延迟更低</span>';
  } else {
    // 经公网隧道访问：提示同网设备可换用更快的局域网地址。
    // 地址由服务端后台每 20s 重新探测，DHCP 续租换 IP 后这里会自动更新。
    const list = (cache.lan_candidates || []).filter(ip => ip !== lan);
    let extra = '';
    if (list.length) {
      extra = '<div class="muted">备选地址：' +
        list.map(ip => '<code>http://' + esc(ip) + ':' + esc(mport) + '/</code>').join(' ') +
        '</div>';
    }
    el.innerHTML =
      '同一局域网内可改用更低延迟的地址访问本页：<code>http://' + esc(lan) + ':' + esc(mport) + '/</code>' +
      extra +
      '<div class="muted">连不上？多为访问设备开了代理/VPN（把私有网段也代理走了），' +
      '或路由器开启了 AP 隔离、两台设备不在同一网段。地址每 20s 自动重新探测。</div>';
  }
  el.hidden = false;
}

 async function refresh() {
   const data = await api('/api/sessions');
   cache = data;
   const who = document.getElementById('whoami');
   if (who) who.innerHTML = data.user ? ('当前用户 <b>' + esc(data.user) + '</b>') : '';
   isAdmin = !!data.is_admin;
   const nu = document.getElementById('navUsers');
   if (nu) nu.hidden = !isAdmin;
   // 普通用户停在 #users（书签/旧状态）时退回会话页
   if (!isAdmin && currentView === 'users') showView('sessions');
   // 刷新页面时直接停在 #users：此时才知道是否 admin，补一次加载
   if (isAdmin && currentView === 'users' && !usersLoaded) { usersLoaded = true; loadUsers(); }
   const alive = new Set(mergedRows().map(rowKey));
  for (const k of [...rowSelected]) {
    if (!alive.has(k)) rowSelected.delete(k);
  }
  renderLanBar();
  renderTrafficBar();
  paint();
}

function closeNameModal() {
  nameModalMode = null;
  document.getElementById('nameModal').hidden = true;
  document.getElementById('nameInput').value = '';
  setNameErr('');
}

// 弹窗内就地显示错误：全屏遮罩会挡住页面顶部的 flash，用户会以为按钮没反应
function setNameErr(msg) {
  const el = document.getElementById('nameErr');
  if (!el) return;
  el.textContent = msg || '';
  el.hidden = !msg;
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

function openCloneModal(srcName, cwd) {
  nameModalMode = { type: 'clone', cwd: cwd || '', srcName: srcName || '' };
  document.getElementById('nameTitle').textContent = '克隆会话';
  document.getElementById('nameHint').textContent = '沿用路径 ' + (cwd || '(默认)') + '，请输入新会话名';
  document.getElementById('nameConfirm').textContent = '创建';
  const modal = document.getElementById('nameModal');
  const input = document.getElementById('nameInput');
  modal.hidden = false;
  input.value = '';
  setTimeout(() => input.focus(), 50);
}

async function submitNameModal() {
  const name = (document.getElementById('nameInput').value || '').trim();
  if (!name) { setNameErr('请输入会话名'); return; }
  if (!isValidSessionName(name)) {
    setNameErr('会话名仅支持中英文、数字、_ 和 -（不含 + 、空格、点号等），最长 64');
    return;
  }
  setNameErr('');
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
      setNameErr(String(e.message || e));  // 服务端报错（如目标名已存在）也显示在弹窗内
    } finally {
      btn.disabled = false;
    }
    return;
  }
  closeNameModal();
  const cwd = (mode && mode.type === 'clone') ? (mode.cwd || cache.default_path || '') : (cache.default_path || '');
  tryOpen({ name, create: true, cwd });
}

// { scope:'session', name, cwd, live } | { scope:'global' }
let pagesModalTarget = null;

function defaultPages() {
  return Number(cache.default_pages) || 30;
}

function maxPages() {
  return Number(cache.max_pages) || 2000;
}

function closePagesModal() {
  pagesModalTarget = null;
  document.getElementById('pagesModal').hidden = true;
  document.getElementById('pagesInput').value = '';
}

function showPagesModal(input, cur) {
  input.value = cur > 0 ? String(cur) : '';
  document.getElementById('pagesModal').hidden = false;
  setTimeout(() => { input.focus(); input.select(); }, 50);
}

function openPagesModal(meta) {
  if (!meta || !meta.name) return;
  const row = mergedRows().find(r => r.name === meta.name && (r.cwd || '') === (meta.cwd || '') && r.live === meta.live);
  pagesModalTarget = { scope: 'session', name: meta.name, cwd: meta.cwd || '', live: meta.live };
  document.getElementById('pagesTitle').textContent = '回看页数 · ' + meta.name;
  document.getElementById('pagesHint').textContent =
    '本会话滚轮回看的历史页数。留空 = 不单独配置，跟随全局默认（当前 ' + defaultPages() + ' 页）。';
  document.getElementById('pagesInput').placeholder = '留空 = 跟随全局 ' + defaultPages();
  const rst = document.getElementById('pagesReset');
  rst.hidden = false;
  rst.textContent = '跟随全局';
  showPagesModal(document.getElementById('pagesInput'), row && row.pages > 0 ? row.pages : 0);
}

function openGlobalPagesModal() {
  pagesModalTarget = { scope: 'global' };
  const envDef = Number(cache.env_default_pages) || 30;
  document.getElementById('pagesTitle').textContent = '全局默认回看页数';
  document.getElementById('pagesHint').textContent =
    '所有「未单独配置」的会话都用这个值（1~' + maxPages() + ' 页）。改完立即生效，下次打开终端起作用。';
  document.getElementById('pagesInput').placeholder = '例如 ' + envDef;
  const rst = document.getElementById('pagesReset');
  rst.hidden = false;
  rst.textContent = '恢复 ' + envDef;
  showPagesModal(document.getElementById('pagesInput'), defaultPages());
}

async function savePages(pages) {
  if (!pagesModalTarget) return;
  const t = pagesModalTarget;
  const btn = document.getElementById('pagesConfirm');
  const rst = document.getElementById('pagesReset');
  btn.disabled = true; rst.disabled = true;
  try {
    const body = t.scope === 'global'
      ? { scope: 'global', pages }
      : { scope: 'session', name: t.name, cwd: t.cwd, live: t.live, pages };
    await api('/api/pages', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    closePagesModal();
    if (t.scope === 'global') {
      flash('全局默认已设为 ' + pages + ' 页，未单独配置的会话下次进入生效');
    } else if (pages === 0) {
      flash('「' + t.name + '」已改为跟随全局默认');
    } else {
      flash('已设置「' + t.name + '」回看 ' + pages + ' 页，下次进入生效');
    }
    await refresh();
  } catch (e) {
    flash(String(e.message || e), true);
  } finally {
    btn.disabled = false; rst.disabled = false;
  }
}

function resetPages() {
  if (!pagesModalTarget) return;
  // 会话级：0 = 清除配置跟随全局；全局级：回落 .env 里的初始默认
  if (pagesModalTarget.scope === 'global') savePages(Number(cache.env_default_pages) || 30);
  else savePages(0);
}

function submitPagesModal() {
  if (!pagesModalTarget) return;
  const raw = (document.getElementById('pagesInput').value || '').trim();
  const isGlobal = pagesModalTarget.scope === 'global';
  if (!raw) {
    if (isGlobal) { flash('全局默认必须填写页数', true); return; }
    savePages(0);   // 会话级留空 = 跟随全局
    return;
  }
  if (!/^\d+$/.test(raw)) { flash('页数必须是正整数', true); return; }
  const n = parseInt(raw, 10);
  if (n < 1) { flash('页数至少 1；如需跟随全局请留空', true); return; }
  if (n > maxPages()) { flash('页数最多 ' + maxPages(), true); return; }
  savePages(n);
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
document.getElementById('pagesCancel').onclick = closePagesModal;
document.getElementById('pagesReset').onclick = resetPages;
document.getElementById('btnDefaultPages').onclick = openGlobalPagesModal;
document.getElementById('pagesConfirm').onclick = submitPagesModal;
document.getElementById('pagesInput').addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter') submitPagesModal();
  if (ev.key === 'Escape') closePagesModal();
});
document.getElementById('pagesModal').addEventListener('click', (ev) => {
  if (ev.target.id === 'pagesModal') closePagesModal();
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
// ---- 用户管理（仅 admin） ----
function renderUsers(list, me) {
  const box = document.getElementById('usList');
  if (!list.length) { box.innerHTML = '<p class="empty">暂无用户</p>'; return; }
  box.innerHTML = '<table style="min-width:0"><thead><tr><th>用户名</th><th style="width:110px">角色</th><th style="width:110px">运行中会话</th><th style="width:90px"></th></tr></thead><tbody>' +
    list.map(u => '<tr><td>' + esc(u.username) + (u.username === me ? ' <span class="sub">(我)</span>' : '') + '</td>' +
      '<td>' + (u.primary ? '管理员' : '普通用户') + '</td>' +
      '<td>' + u.sessions + '</td>' +
      '<td>' + (u.primary || u.username === me ? '' :
        '<button class="danger us-del" type="button" data-u="' + esc(u.username) + '" data-n="' + u.sessions + '">删除</button>') + '</td></tr>'
    ).join('') + '</tbody></table>';
  box.querySelectorAll('.us-del').forEach(b => b.onclick = () => delUser(b.dataset.u, Number(b.dataset.n)));
}

async function loadUsers() {
  try {
    const d = await api('/api/users');
    renderUsers(d.users || [], d.me);
  } catch (e) {
    document.getElementById('usList').innerHTML = '<p class="empty">' + esc(e.message || e) + '</p>';
  }
}

async function addUser() {
  const nameEl = document.getElementById('usName');
  const passEl = document.getElementById('usPass');
  const btn = document.getElementById('usAdd');
  const username = nameEl.value.trim();
  const password = passEl.value;
  if (!username || !password) { flash('请输入用户名和密码', true); return; }
  btn.disabled = true;
  try {
    const d = await api('/api/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    nameEl.value = ''; passEl.value = '';
    renderUsers(d.users || [], cache.user);
    flash('已添加用户「' + username + '」');
  } catch (e) { flash(String(e.message || e), true); }
  finally { btn.disabled = false; }
}

async function delUser(username, n) {
  const extra = n ? ('\n该用户 ' + n + ' 个运行中的会话会被一并终止。') : '';
  if (!confirm('确定删除用户「' + username + '」吗？' + extra + '\n其上传的文件会保留。')) return;
  try {
    const d = await api('/api/users/' + encodeURIComponent(username), { method: 'DELETE' });
    renderUsers(d.users || [], cache.user);
    flash('已删除用户「' + username + '」');
    await refresh();
  } catch (e) { flash(String(e.message || e), true); }
}

document.getElementById('usAdd').onclick = addUser;
document.getElementById('usRefresh').onclick = loadUsers;
document.getElementById('usPass').addEventListener('keydown', ev => { if (ev.key === 'Enter') addUser(); });
document.getElementById('usName').addEventListener('keydown', ev => { if (ev.key === 'Enter') document.getElementById('usPass').focus(); });

// ---- LLM 适配 ----
async function loadLlmPrompt() {
  const ta = document.getElementById('llmText');
  try {
    const d = await api('/api/llm-prompt');
    ta.value = d.prompt || '';
  } catch (e) { ta.value = ''; flash(String(e.message || e), true); }
}
document.getElementById('llmReload').onclick = loadLlmPrompt;
document.getElementById('llmCopy').onclick = async () => {
  const ta = document.getElementById('llmText');
  if (!ta.value) await loadLlmPrompt();
  let ok = false;
  try { await navigator.clipboard.writeText(ta.value); ok = true; } catch (e) {}
  if (!ok) {   // http 非安全上下文没有 clipboard API，用选中 + execCommand 兜底
    ta.focus(); ta.select();
    try { ok = document.execCommand('copy'); } catch (e) {}
    ta.setSelectionRange(0, 0);
  }
  const b = document.getElementById('llmCopy');
  const old = b.textContent;
  b.textContent = ok ? '已复制 ✓' : '复制失败，请在文本框中全选后手动复制';
  setTimeout(() => { b.textContent = old; }, 2200);
};

// ---- 侧边栏视图切换 ----
const VIEWS = ['sessions', 'upload', 'files', 'llm', 'users'];
let isAdmin = false;
let usersLoaded = false;
let currentView = '';
const viewLoaded = {};

function showView(v, opts) {
  if (!VIEWS.includes(v)) v = 'sessions';
  if (v === 'users' && !isAdmin && cache && cache.user) v = 'sessions';
  currentView = v;
  document.querySelectorAll('.view').forEach(el => { el.hidden = el.dataset.view !== v; });
  document.querySelectorAll('#nav .navitem').forEach(a => a.classList.toggle('active', a.dataset.view === v));
  try { localStorage.setItem('wt_view', v); } catch (e) {}
  if (!(opts && opts.noHash) && location.hash !== '#' + v) history.replaceState(null, '', '#' + v);
  // 首次进入才加载，之后由各视图里的刷新按钮触发
  if (!viewLoaded[v]) {
    viewLoaded[v] = true;
    if (v === 'users' && isAdmin) { usersLoaded = true; loadUsers(); }
    if (v === 'llm') loadLlmPrompt();
  }
  if (v === 'sessions') window.dispatchEvent(new Event('wt-view-sessions'));
}

function initialView() {
  const h = (location.hash || '').replace('#', '');
  if (VIEWS.includes(h)) return h;
  try { const v = localStorage.getItem('wt_view'); if (VIEWS.includes(v)) return v; } catch (e) {}
  return 'sessions';
}

document.querySelectorAll('#nav .navitem').forEach(a => a.addEventListener('click', ev => {
  ev.preventDefault();
  showView(a.dataset.view);
}));
window.addEventListener('hashchange', () => showView(initialView(), { noHash: true }));
showView(initialView(), { noHash: true });

document.getElementById('btnLogout').onclick = () => {
  if (confirm('确定退出登录吗？已打开的终端标签页不受影响，重连需重新登录。')) location.href = '/api/logout';
};

refresh().catch(e => flash(String(e.message || e), true));
// ---- 文件上传 ----
// 文件夹/多选拖放统一收集为 {rel, file} 列表，逐个 PUT 原始字节流到 /api/upload。
// rel 保留目录结构（webkitRelativePath / DataTransferItem.webkitGetAsEntry）。
(function () {
  const drop = document.getElementById('upDrop');
  const prog = document.getElementById('upProgress');
  const fileInput = document.getElementById('upFileInput');
  const dirInput = document.getElementById('upDirInput');
  if (!drop) return;

  let queue = [];      // 待传 {rel, file}
  let done = 0, failed = 0, totalBytes = 0, sentBytes = 0;
  let active = false;

  // ---- 上传历史：最近 10 条，一键复制服务器全路径（分页展示，每页 5 条） ----
  const histEl = document.getElementById('upHistory');
  const HIST_PAGE_SIZE = 5;
  let histData = [];      // 倒序后的完整列表
  let histPage = 0;       // 当前页（0 起）
  let histHooked = false; // 容器级事件只挂一次

  function fmtSize(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    const u = ['KB', 'MB', 'GB', 'TB'];
    let i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
    return (n < 10 ? n.toFixed(2) : n < 100 ? n.toFixed(1) : Math.round(n)) + ' ' + u[i];
  }

  function histPageCount() {
    return Math.max(1, Math.ceil(histData.length / HIST_PAGE_SIZE));
  }

  function renderHistory(list) {
    if (!histEl) return;
    if (!list || !list.length) {
      histData = []; histPage = 0;
      histEl.hidden = true; histEl.innerHTML = '';
      return;
    }
    histData = list.slice().sort((a, b) => String(b.ts || '').localeCompare(String(a.ts || '')));
    if (histPage >= histPageCount()) histPage = 0; // 列表变短（如删除）时回到第 1 页
    // 容器级事件委托：翻页重渲染不用重复绑按钮
    if (!histHooked) {
      histHooked = true;
      histEl.addEventListener('click', onHistClick);
    }
    drawHistPage();
  }

  function drawHistPage() {
    const pages = histPageCount();
    if (histPage >= pages) histPage = pages - 1;
    const start = histPage * HIST_PAGE_SIZE;
    const slice = histData.slice(start, start + HIST_PAGE_SIZE);
    let html = '<h3>最近上传 · 点击文件名复制服务器路径</h3><ul>';
    for (const h of slice) {
      const rel = h.rel || (h.path || '').split('/').pop() || '';
      html += '<li data-rel="' + esc(rel) + '" data-path="' + esc(h.path || '') + '">' +
        '<span class="fname" title="' + esc(h.path) + '">' + esc(h.path) + '</span>' +
        '<span class="fmeta">' + esc(fmtSize(h.bytes)) + ' · ' + esc(h.ts || '') + '</span>' +
        '<button type="button" class="cp" data-path="' + esc(h.path) + '">复制路径</button>' +
        '<button type="button" class="op" data-act="download" title="下载到本机">下载</button>' +
        '<button type="button" class="op" data-act="overwrite" title="选择本机文件重新上传，覆盖服务器上的同名文件">重新上传</button>' +
        '<button type="button" class="op del" data-act="delete" title="删除服务器上的这个文件">删除</button>' +
        '</li>';
    }
    html += '</ul>';
    if (pages > 1) {
      html += '<div class="histpager">' +
        '<button type="button" class="pg" data-pg="prev"' + (histPage === 0 ? ' disabled' : '') + '>‹ 上一页</button>' +
        '<span class="pginfo">' + (histPage + 1) + ' / ' + pages + ' 页 · 共 ' + histData.length + ' 条</span>' +
        '<button type="button" class="pg" data-pg="next"' + (histPage >= pages - 1 ? ' disabled' : '') + '>下一页 ›</button>' +
        '</div>';
    }
    histEl.innerHTML = html;
    histEl.hidden = false;
  }

  async function onHistClick(ev) {
    const t = ev.target;
    if (!t || t.tagName !== 'BUTTON') return;
    // 翻页按钮
    if (t.classList.contains('pg')) {
      const pages = histPageCount();
      if (t.getAttribute('data-pg') === 'prev' && histPage > 0) histPage -= 1;
      else if (t.getAttribute('data-pg') === 'next' && histPage < pages - 1) histPage += 1;
      drawHistPage();
      return;
    }
    const li = t.closest('li');
    if (!li) return;
    const act = t.getAttribute('data-act');
    const rel = li.getAttribute('data-rel') || '';
    const path = li.getAttribute('data-path') || '';
    if (t.classList.contains('cp')) {
      let oked = false;
      try {
        await navigator.clipboard.writeText(path);
        oked = true;
      } catch (e) {
        // clipboard API 不可用（如非安全上下文）时用 execCommand 兜底
        try {
          const ta = document.createElement('textarea');
          ta.value = path;
          ta.style.cssText = 'position:fixed;opacity:0;';
          document.body.appendChild(ta);
          ta.select();
          oked = document.execCommand('copy');
          ta.remove();
        } catch (e2) { oked = false; }
      }
      t.textContent = oked ? '✓ 已复制' : '复制失败';
      t.classList.add('ok');
      setTimeout(() => { t.textContent = '复制路径'; t.classList.remove('ok'); }, 1600);
      return;
    }
    if (!rel || !path) return;
    if (act === 'download') {
      // 同源 fetch 带 Basic Auth（浏览器已缓存凭据），blob 触发保存
      t.disabled = true; t.textContent = '…';
      try {
        const res = await fetch('/api/upload-download?rel=' + encodeURIComponent(rel), { credentials: 'same-origin' });
        if (!res.ok) {
          let msg = 'HTTP ' + res.status;
          try { msg = (await res.json()).error || msg; } catch (e) {}
          throw new Error(msg);
        }
        const blob = await res.blob();
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = rel.split('/').pop() || 'download';
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 30000);
        flash('已开始下载 ' + rel.split('/').pop());
      } catch (e) {
        flash('下载失败: ' + (e.message || e), true);
      }
      t.disabled = false; t.textContent = '下载';
      return;
    }
    if (act === 'overwrite') {
      // 弹文件选择器 → 选中后直接覆盖写回服务器同一路径
      const input = document.createElement('input');
      input.type = 'file';
      input.addEventListener('change', async () => {
        const f = input.files && input.files[0];
        if (!f) return;
        if (!confirm('用「' + f.name + '」覆盖服务器上的 ' + rel + ' ？')) { input.value = ''; return; }
        t.disabled = true; t.textContent = '…';
        try {
          const d = await api('/api/upload?rel=' + encodeURIComponent(rel) + '&overwrite=1', {
            method: 'POST',
            headers: { 'Content-Type': 'application/octet-stream' },
            body: f,
          });
          flash('已覆盖 ' + rel + '（' + fmtSize(d.bytes || f.size) + '）');
          updateHistoryFrom(d);
        } catch (e) {
          flash('覆盖失败: ' + (e.message || e), true);
        }
        t.disabled = false; t.textContent = '重新上传';
      });
      input.click();
      return;
    }
    if (act === 'delete') {
      if (!confirm('删除服务器上的 ' + path + ' ？此操作不可恢复。')) return;
      t.disabled = true; t.textContent = '…';
      try {
        const d = await api('/api/upload-delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: path }),
        });
        flash(d.already_gone ? '文件已不存在，已清理历史记录' : '已删除 ' + rel);
        updateHistoryFrom(d);
      } catch (e) {
        flash('删除失败: ' + (e.message || e), true);
      }
      t.disabled = false; t.textContent = '删除';
      return;
    }
  }

  async function loadHistory() {
    try {
      const d = await api('/api/upload-history');
      renderHistory(d.history || []);
    } catch (e) { /* 历史加载失败不提示 */ }
  }

  function updateHistoryFrom(respData) {
    if (respData && Array.isArray(respData.history)) renderHistory(respData.history);
    else loadHistory();
  }

  function relOf(file) {
    // webkitdirectory 时有 webkitRelativePath（含所选文件夹名）
    return (file.webkitRelativePath || file.name || '').replace(/\\/g, '/');
  }

  function enqueue(files, label) {
    const list = [...files].filter(f => f && (f.size >= 0));
    if (!list.length) return;
    for (const f of list) queue.push({ rel: relOf(f), file: f });
    totalBytes += list.reduce((s, f) => s + f.size, 0);
    flash('');
    renderProgress(label || null);
    run();
  }

  function renderProgress(currentLabel, isError) {
    if (!queue.length && done + failed === 0) { prog.hidden = true; return; }
    const pct = totalBytes > 0 ? Math.min(100, Math.round(sentBytes / totalBytes * 100)) : 0;
    let html = '<b>' + (active ? '上传中 ' : '完成 ') + pct + '%</b>' +
      ' <span>' + done + ' 个成功' + (failed ? '，<span class="fail">' + failed + ' 个失败</span>' : '') + '</span>' +
      '<div class="bar"><i style="width:' + pct + '%"></i></div>';
    if (currentLabel) {
      html += '<div class="item' + (isError ? ' fail' : '') + '">' + esc(currentLabel) + '</div>';
    }
    prog.innerHTML = html;
    prog.hidden = false;
  }

  async function run() {
    if (active) return;
    active = true;
    // 每批上传先向服务端要一个独立子目录（服务端时钟命名），全部文件进同一目录，
    // 解压/整理互不影响 Downloads 其它内容
    let batch = '';
    try {
      const d = await api('/api/upload-batch', { method: 'POST' });
      batch = d.dir ? d.dir + '/' : '';
    } catch (e) {
      batch = ''; // 建目录失败则退回 Downloads 根，不阻断上传
    }
    if (batch) renderProgress('→ ' + batch);
    while (queue.length) {
      const item = queue.shift();
      const rel = batch + item.rel;
      const file = item.file;
      renderProgress(rel);
      try {
        const res = await fetch('/api/upload?rel=' + encodeURIComponent(rel), {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/octet-stream' },
          body: file,
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
        sentBytes += file.size;
        done++;
        renderProgress(null);
        updateHistoryFrom(data);  // 响应直接带最新历史，免一次额外请求
      } catch (e) {
        failed++;
        sentBytes += file.size;
        renderProgress(rel + ' — ' + (e.message || e), true);
      }
    }
    active = false;
    if (done + failed > 0) {
      flash(failed ? '上传完成：' + done + ' 成功，' + failed + ' 失败' : '已上传 ' + done + ' 个文件到 ' + (batch ? '~/Downloads/' + batch : '~/Downloads'), !!failed);
    }
    // 5 分钟后自动收起进度条
    setTimeout(() => { if (!active) { prog.hidden = true; done = failed = totalBytes = sentBytes = 0; } }, 5 * 60 * 1000);
  }

  // ---- 按钮 / input ----
  document.getElementById('btnUpFile').addEventListener('click', () => fileInput.click());
  document.getElementById('btnUpDir').addEventListener('click', () => dirInput.click());
  fileInput.addEventListener('change', () => { enqueue(fileInput.files); fileInput.value = ''; });
  dirInput.addEventListener('change', () => { enqueue(dirInput.files); dirInput.value = ''; });

  // ---- 拖放：支持文件、文件夹（webkitGetAsEntry 递归） ----
  ['dragenter', 'dragover'].forEach(t => drop.addEventListener(t, ev => {
    ev.preventDefault(); ev.stopPropagation();
    drop.classList.add('over');
  }));
  ['dragleave', 'drop'].forEach(t => drop.addEventListener(t, ev => {
    ev.preventDefault(); ev.stopPropagation();
    drop.classList.remove('over');
  }));

  function walkEntry(entry, base, out) {
    return new Promise(resolve => {
      if (!entry) return resolve();
      if (entry.isFile) {
        entry.file(f => {
          out.push({ rel: (base ? base + '/' : '') + f.name, file: f });
          resolve();
        }, () => resolve());
      } else if (entry.isDirectory) {
        const reader = entry.createReader();
        const readBatch = () => reader.readEntries(async entries => {
          if (!entries.length) return resolve();
          for (const e of entries) await walkEntry(e, (base ? base + '/' : '') + entry.name, out);
          readBatch(); // 目录条目超过 100 个时分批读
        }, () => resolve());
        readBatch();
      } else resolve();
    });
  }

  drop.addEventListener('drop', async ev => {
    ev.preventDefault(); ev.stopPropagation();
    const items = ev.dataTransfer && ev.dataTransfer.items;
    if (!items || !items.length) return;
    // Chrome/Edge/Safari 支持目录拖入；Firefox 走 files 兜底
    if (items[0].webkitGetAsEntry) {
      const out = [];
      const entries = [];
      for (const it of items) {
        const e = it.webkitGetAsEntry && it.webkitGetAsEntry();
        if (e) entries.push(e);
      }
      if (entries.length) {
        for (const e of entries) await walkEntry(e, '', out);
        if (out.length) {
          totalBytes += out.reduce((s, x) => s + x.file.size, 0);
          queue.push(...out);
          renderProgress(null);
          run();
        }
        return;
      }
    }
    if (ev.dataTransfer.files && ev.dataTransfer.files.length) enqueue(ev.dataTransfer.files);
  });

  loadHistory();  // 页面加载即显示最近上传
})();

// ---- 服务端文件浏览 / 预览 / 下载 ----
(function () {
  const crumbEl = document.getElementById('fsCrumb');
  const listEl = document.getElementById('fsList');
  if (!crumbEl || !listEl) return;
  let curRel = '';
  let curRoot = '';

  function fmtSize(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    const u = ['KB', 'MB', 'GB', 'TB'];
    let i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
    return (n < 10 ? n.toFixed(2) : n < 100 ? n.toFixed(1) : Math.round(n)) + ' ' + u[i];
  }

  function renderCrumb(rel) {
    const parts = rel ? rel.split('/') : [];
    let html = '<a data-rel="">/ (root)</a>';
    let acc = '';
    for (const p of parts) {
      acc = acc ? acc + '/' + p : p;
      html += ' / <a data-rel="' + esc(acc) + '">' + esc(p) + '</a>';
    }
    crumbEl.innerHTML = html;
    crumbEl.querySelectorAll('a').forEach(a => {
      a.addEventListener('click', () => load(a.getAttribute('data-rel') || ''));
    });
  }

  async function load(rel) {
    listEl.innerHTML = '<p class="empty">加载中…</p>';
    try {
      const d = await api('/api/fs-list?rel=' + encodeURIComponent(rel));
      curRel = d.rel || '';
      curRoot = d.root || '';
      renderCrumb(curRel);
      if (!d.items || !d.items.length) {
        listEl.innerHTML = '<p class="empty">空目录</p>';
        return;
      }
      let html = '<table><thead><tr><th style="width:46%">名称</th><th>大小</th><th>修改时间</th><th>操作</th></tr></thead><tbody>';
      for (const it of d.items) {
        const nameCls = it.dir ? 'dname' : 'dname file';
        const nav = it.dir ? ' data-nav="1"' : '';
        html += '<tr data-rel="' + esc(it.rel) + '" data-dir="' + (it.dir ? 1 : 0) + '" data-kind="' + esc(it.kind || '') + '" data-previewable="' + (it.previewable ? 1 : 0) + '">' +
          '<td><span class="' + nameCls + '"' + nav + '>' + (it.dir ? '📁 ' : (it.kind === 'image' ? '🖼 ' : '📄 ')) + esc(it.name) + '</span></td>' +
          '<td class="sz">' + (it.dir ? '-' : fmtSize(it.size)) + '</td>' +
          '<td class="sz">' + esc(it.mtime) + '</td>' +
          '<td class="ops">' +
          (it.dir ? '<button class="op2" data-act="zip">下载</button>' :
            (it.previewable ? '<button class="op2" data-act="preview">预览</button>' : '') +
            '<button class="op2" data-act="download">下载</button>') +
          '</td></tr>';
      }
      if (d.truncated) {
        html += '<tr><td colspan="4" class="sz">仅显示前 ' + d.items.length + ' 项（共 ' + d.total + ' 项）</td></tr>';
      }
      html += '</tbody></table>';
      listEl.innerHTML = html;
      // 目录点击进入；文件名点击=预览（可预览时）
      listEl.querySelectorAll('span.dname[data-nav]').forEach(el => {
        el.addEventListener('click', () => {
          const tr = el.closest('tr');
          load(tr.getAttribute('data-rel') || '');
        });
      });
      listEl.querySelectorAll('span.dname.file').forEach(el => {
        el.addEventListener('click', () => {
          const tr = el.closest('tr');
          if (tr.getAttribute('data-previewable') === '1') doPreview(tr);
        });
      });
      listEl.querySelectorAll('button.op2').forEach(btn => {
        btn.addEventListener('click', ev => {
          ev.stopPropagation();
          const tr = btn.closest('tr');
          const act = btn.getAttribute('data-act');
          if (act === 'download') doDownload(tr);
          else if (act === 'zip') doZip(tr, btn);
          else doPreview(tr);
        });
      });
    } catch (e) {
      listEl.innerHTML = '<p class="empty">加载失败: ' + esc(String(e.message || e)) + '</p>';
    }
  }

  function doDownload(tr) {
    const rel = tr.getAttribute('data-rel') || '';
    const a = document.createElement('a');
    // 同源 + Basic Auth（浏览器已缓存）；后端流式下发
    a.href = '/api/fs-download?rel=' + encodeURIComponent(rel);
    a.download = rel.split('/').pop() || 'download';
    document.body.appendChild(a);
    a.click();
    a.remove();
    flash('已开始下载 ' + (rel.split('/').pop() || ''));
  }

  // 文件夹下载：先统计大小，超过阈值（后端给出 confirm_over，默认 1GB）二次确认后再打包
  async function doZip(tr, btn) {
    const rel = tr.getAttribute('data-rel') || '';
    const name = rel.split('/').pop() || rel;
    const old = btn.textContent;
    btn.disabled = true; btn.textContent = '统计中…';
    try {
      const st = await api('/api/fs-size?rel=' + encodeURIComponent(rel));
      const limit = st.confirm_over || 1024 * 1024 * 1024;
      const approx = st.complete ? '' : '至少 ';
      const info = '「' + name + '」' + (st.complete ? '共 ' : '已统计到 ') + st.files + ' 个文件、' + st.dirs + ' 个子文件夹，' + approx + fmtSize(st.size);
      if (!st.files) {
        if (!confirm(info + '（空文件夹）。\n仍要下载吗？')) return;
      } else if (!st.complete || st.size > limit) {
        const why = st.complete ? '，超过 ' + fmtSize(limit) : '。统计超时，文件夹很大，实际体积未知';
        if (!confirm(info + why + '。\n将压缩为 zip 后下载，耗时较长且占用服务器资源，确定继续吗？')) return;
      }
      const a = document.createElement('a');
      a.href = '/api/fs-zip?rel=' + encodeURIComponent(rel);
      a.download = name + '.zip';
      document.body.appendChild(a);
      a.click();
      a.remove();
      flash('已开始下载 ' + name + '.zip（' + approx + fmtSize(st.size) + ' 压缩前）');
    } catch (e) {
      flash(String(e.message || e), true);
    } finally {
      btn.disabled = false; btn.textContent = old;
    }
  }

  function closeView() {
    const m = document.getElementById('fsviewMask');
    if (m) m.remove();
  }

  async function doPreview(tr) {
    const rel = tr.getAttribute('data-rel') || '';
    const kind = tr.getAttribute('data-kind') || '';
    const name = (rel.split('/').pop() || '');
    closeView();
    const mask = document.createElement('div');
    mask.id = 'fsviewMask';
    mask.className = 'fsview-mask';
    mask.innerHTML = '<div class="fsview" role="dialog" aria-modal="true">' +
      '<div class="vhead"><b>' + esc(name) + '</b>' +
      '<button class="secondary" id="fsviewDl" type="button">下载</button>' +
      '<button class="secondary" id="fsviewClose" type="button">关闭</button></div>' +
      '<div class="vbody" id="fsviewBody"><p class="empty">加载中…</p></div>' +
      '<div class="vfoot" id="fsviewFoot"></div></div>';
    document.body.appendChild(mask);
    mask.addEventListener('click', ev => { if (ev.target === mask) closeView(); });
    document.getElementById('fsviewClose').addEventListener('click', closeView);
    document.getElementById('fsviewDl').addEventListener('click', () => {
      const a = document.createElement('a');
      a.href = '/api/fs-download?rel=' + encodeURIComponent(rel);
      a.download = name;
      document.body.appendChild(a); a.click(); a.remove();
    });
    document.addEventListener('keydown', function esc(ev) {
      if (ev.key === 'Escape') { closeView(); document.removeEventListener('keydown', esc); }
    });
    const body = document.getElementById('fsviewBody');
    const foot = document.getElementById('fsviewFoot');
    try {
      if (kind === 'image') {
        // 同源直链；后端按后缀给 Content-Type
        body.innerHTML = '<img src="/api/fs-image?rel=' + encodeURIComponent(rel) + '" alt="' + esc(name) + '" ' +
          'onerror="this.parentNode.innerHTML=\'<p class=empty>图片加载失败</p>\'">';
        foot.textContent = name;
      } else {
        const d = await api('/api/fs-text?rel=' + encodeURIComponent(rel));
        body.innerHTML = '<pre>' + esc(d.text) + '</pre>';
        foot.textContent = name + ' · ' + fmtSize(d.size) + ' · ' + (d.encoding || '') + (d.truncated ? ' · 已截断（前 50 万字符）' : '');
      }
    } catch (e) {
      body.innerHTML = '<p class="empty">预览失败: ' + esc(String(e.message || e)) + '</p>';
    }
  }

  document.getElementById('fsRefresh').addEventListener('click', () => load(curRel));
  document.getElementById('fsUpDir').addEventListener('click', () => {
    if (!curRel) return;
    const parts = curRel.split('/');
    parts.pop();
    load(parts.join('/'));
  });
  document.getElementById('fsRoot').addEventListener('click', () => load(''));

  // ---- 路径跳转：支持绝对路径（根内）/ ~/xxx / 相对路径，回车或按钮触发 ----
  const pathInput = document.getElementById('fsPathInput');
  function goPath() {
    const v = (pathInput.value || '').trim();
    if (!v) return;
    // 后端负责归一化与校验；成功后面包屑/列表切到目标目录
    load(v).then(() => {
      try {
        // 同步输入框为服务端返回的规范相对路径，避免用户看到自己输入的原始形式
        pathInput.value = curRel ? (curRoot ? curRoot + '/' : '') + curRel : (curRoot || '/');
      } catch (e) {}
    });
  }
  document.getElementById('fsGo').addEventListener('click', goPath);
  pathInput.addEventListener('keydown', ev => {
    if (ev.key === 'Enter') { ev.preventDefault(); goPath(); }
  });
  // 目录加载成功后把当前绝对路径回填输入框（也方便复制）
  const _origLoad = load;
  load = async function (rel) {
    await _origLoad(rel);
    if (curRoot) pathInput.value = curRel ? curRoot + '/' + curRel : curRoot;
  };

  load('');  // 默认根目录
})();

// 省流量：标签页不可见时停止轮询；结果无变化时逐档放慢（5→10→20→30s），
// 列表一有变化立即回最快档；切回前台立即刷新一次，体验不变。
;(function () {
  var IDLE_STEPS = [5000, 10000, 20000, 30000];
  var idleIdx = 0;
  var lastJson = null;
  var timer = null;
  async function poll() {
    if (document.hidden || currentView !== 'sessions') { timer = setTimeout(poll, IDLE_STEPS[IDLE_STEPS.length - 1]); return; }
    try {
      await refresh();
      // 只对比影响渲染的关键字段：last_open 等时间戳每次都变，不能当“有变化”
      const sig = JSON.stringify((cache.sessions || []).map(s => [s.name, s.cwd_now, s.clients, s.pages, s.last_open]));
      if (sig !== lastJson) { lastJson = sig; idleIdx = 0; }
      else if (idleIdx < IDLE_STEPS.length - 1) idleIdx += 1;
    } catch (e) {}
    timer = setTimeout(poll, IDLE_STEPS[idleIdx]);
  }
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) { idleIdx = 0; clearTimeout(timer); poll(); }
  });
  window.addEventListener('wt-view-sessions', () => { idleIdx = 0; clearTimeout(timer); poll(); });
  poll();
})();
</script>
</body>
</html>
"""

MANAGE_HTML = MANAGE_HTML.replace("__FAVICON__", favicon_links(), 1)


# ---------- 独立登录页：替代 Basic 弹窗（浏览器可保存密码），Cookie 登录态 24h ----------
LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Web Terminal · 登录</title>
__FAVICON__
<style>
  :root {
    --bg: #0f1419; --panel: #1a222c; --line: #2a3542;
    --text: #e7ecf1; --muted: #8b9aab; --accent: #3d8bfd;
    --danger: #f31260;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    font: 14px/1.5 "IBM Plex Sans", "Source Han Sans SC", "PingFang SC", sans-serif;
    color: var(--text);
    background:
      radial-gradient(1200px 600px at 10% -10%, #1b3a5f 0%, transparent 55%),
      radial-gradient(900px 500px at 100% 0%, #14352c 0%, transparent 50%),
      var(--bg);
  }
  .card {
    width: 100%; max-width: 380px; padding: 28px 26px 24px;
    background: color-mix(in srgb, var(--panel) 92%, transparent);
    border: 1px solid var(--line); border-radius: 14px;
    backdrop-filter: blur(8px);
  }
  h1 { margin: 0 0 4px; font-size: 20px; letter-spacing: -0.02em; }
  .sub { color: var(--muted); margin-bottom: 20px; }
  label { display: block; margin: 14px 0 6px; color: var(--muted); font-size: 13px; }
  input {
    width: 100%; background: #0c1117; color: var(--text);
    border: 1px solid var(--line); border-radius: 8px;
    padding: 10px 12px; font: inherit;
  }
  input:focus { outline: none; border-color: var(--accent); }
  .err {
    display: none; margin: 14px 0 0; padding: 8px 12px;
    border-radius: 8px; background: rgba(243,18,96,.12); color: #ff7aa8;
  }
  .err.show { display: block; }
  button {
    width: 100%; margin-top: 20px; appearance: none; border: 0; cursor: pointer;
    border-radius: 8px; padding: 11px 14px; font: inherit; font-weight: 600; color: #fff;
    background: var(--accent);
  }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .hint { margin-top: 16px; color: var(--muted); font-size: 12px; text-align: center; }
</style>
</head>
<body>
<form class="card" method="POST" action="/api/login" autocomplete="on">
  <h1>Web Terminal</h1>
  <div class="sub">登录会话管理</div>
  <input type="hidden" name="next" value="__NEXT__">
  <label for="username">账号</label>
  <input id="username" name="username" type="text" autocomplete="username" autocapitalize="none" required>
  <label for="password">密码</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  <div id="err" class="err" role="alert">__ERROR__</div>
  <button id="btn" type="submit">登 录</button>
  <div class="hint">密码可由浏览器保存，24 小时内免再次输入</div>
</form>
<script>
  // 让带错误信息的重渲染也触发浏览器“保存密码”提示的正确行为：聚焦账号框
  (function () {
    var e = document.getElementById('err');
    if (e && e.textContent.trim()) e.classList.add('show');
    var u = document.getElementById('username');
    if (u && !u.value) u.focus();
  })();
</script>
</body>
</html>
"""


def render_login_html(next_url: str = "", error: str = "") -> str:
    import html as _html

    safe_next = next_url if _is_safe_next(next_url) else ""
    out = LOGIN_HTML.replace("__FAVICON__", favicon_links(), 1)
    out = out.replace("__NEXT__", _html.escape(safe_next, quote=True))
    return out.replace("__ERROR__", _html.escape(error, quote=True))


def _is_safe_next(url: str) -> bool:
    """next 只允许：本站路径（/ 开头且非 //）或指向本机 ttyd 端口的绝对 URL。
    防开放跳转；ttyd 与 manage 同主机不同端口，票据 URL 需要跨端口回跳。"""
    if not url:
        return False
    if url.startswith("/") and not url.startswith("//"):
        return True
    try:
        u = urlparse(url)
    except ValueError:
        return False
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    if u.hostname not in ("127.0.0.1", "localhost") and u.hostname != PUBLIC_HOST:
        return False
    if u.port is not None and u.port != TTYD_PORT:
        return False
    return True


def _parse_term_next(next_url: str) -> tuple[str, bool]:
    """从 next（/term/?arg=name&arg=create&arg=t1.…&pages=N）取会话名与 create 标记。
    返回 (name, create)；无效返回 ("", False)。"""
    if not _is_safe_next(next_url):
        return "", False
    try:
        q = parse_qs(urlparse(next_url).query)
    except ValueError:
        return "", False
    name = ""
    create = False
    for a in q.get("arg", []):
        if a.startswith("t1."):
            continue
        if a == "create":
            create = True
            continue
        if not name:
            name = a
    if name and NAME_RE.match(name):
        return name, create
    return "", False


def _strip_ticket_from_next(next_url: str) -> str:
    """去掉 next 里的旧票据 arg，保留其余参数（name/create/pages）。"""
    try:
        u = urlparse(next_url)
        q = parse_qs(u.query, keep_blank_values=True)
    except ValueError:
        return next_url
    args = [a for a in q.get("arg", []) if not a.startswith("t1.")]
    pairs: list[str] = []
    for a in args:
        pairs.append(("arg", a))
    for k, vs in q.items():
        if k == "arg":
            continue
        for v in vs:
            pairs.append((k, v))
    query = "&".join(f"{k}={quote(v, safe='')}" for k, v in pairs)
    return u._replace(query=query).geturl()


# ---------- 前台交互式工具识别 ----------
# tmux 的 #{pane_current_command} 不可靠：claude 会把进程名改成版本号（如 2.1.220），
# codex / cursor-agent 又都显示成 node。所以走 pane 的 tty，用 ps 找真正的前台进程，
# 再按完整 argv 判断是什么工具。
TOOL_RULES: list[tuple[str, str, str]] = [
    # (kind, 友好名, 在 argv 里匹配的正则)
    ("claude", "Claude", r"(^|/)claude(\s|$)"),
    ("codex", "Codex", r"(^|/)codex(\s|$|/)"),
    ("opencode", "OpenCode", r"(^|/)opencode(\s|$)"),
    ("cursor", "Cursor Agent", r"cursor-agent|(^|/)agent\s+--use-system-ca"),
    ("gemini", "Gemini CLI", r"(^|/)gemini(\s|$)"),
    ("aider", "Aider", r"(^|/)aider(\s|$)"),
    ("ipython", "IPython", r"(^|/)ipython[\d.]*(\s|$)"),
    ("python", "Python", r"(^|/)python[\d.]*(\s|$)"),
    ("node", "Node", r"(^|/)node(\s|$)"),
    ("deno", "Deno", r"(^|/)deno(\s|$)"),
    ("bun", "Bun", r"(^|/)bun(\s|$)"),
    ("scala", "Scala", r"(^|/)(scala|scala-cli|sbt|amm)[\d.]*(\s|$)"),
    ("ruby", "Ruby", r"(^|/)(irb|pry|ruby)(\s|$)"),
    ("php", "PHP", r"(^|/)php(\s|$)"),
    ("db", "psql", r"(^|/)psql(\s|$)"),
    ("db", "MySQL", r"(^|/)mysql(\s|$)"),
    ("db", "SQLite", r"(^|/)sqlite3?(\s|$)"),
    ("db", "Redis", r"(^|/)redis-cli(\s|$)"),
    ("db", "mongosh", r"(^|/)mongosh(\s|$)"),
    ("editor", "Neovim", r"(^|/)nvim(\s|$)"),
    ("editor", "Vim", r"(^|/)vim?(\s|$)"),
    ("editor", "Emacs", r"(^|/)emacs(\s|$)"),
    ("editor", "nano", r"(^|/)nano(\s|$)"),
    ("pager", "less", r"(^|/)(less|more)(\s|$)"),
    ("pager", "man", r"(^|/)man(\s|$)"),
    ("monitor", "top", r"(^|/)(top|htop|btop)(\s|$)"),
    ("k8s", "k9s", r"(^|/)k9s(\s|$)"),
    ("ssh", "SSH", r"(^|/)(ssh|mosh)(\s|$)"),
    ("tail", "tail -f", r"(^|/)tail\s+.*-f|(^|/)tail\s+-f"),
]
SHELL_NAMES = {"sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh", "login", "-zsh", "-bash"}


def label_process(args: str) -> dict[str, str]:
    """完整 argv → {kind, label, cmd}。shell 本身返回 kind=shell（前端不显示）。"""
    argv = (args or "").strip()
    if not argv:
        return {"kind": "", "label": "", "cmd": ""}
    argv0 = argv.split()[0]
    base = argv0.rsplit("/", 1)[-1].lstrip("-")
    if base in SHELL_NAMES or argv0.lstrip("-") in SHELL_NAMES:
        return {"kind": "shell", "label": "", "cmd": base}
    for kind, label, pattern in TOOL_RULES:
        if re.search(pattern, argv):
            # python3.14 / scala3 之类带版本的，把版本缀上去更直观
            m = re.match(r"^[a-zA-Z_-]+(\d+\.[\d.]+)$", base)
            if m and label.lower().rstrip("0123456789.") in base.lower():
                return {"kind": kind, "label": f"{label} {m.group(1)}", "cmd": base}
            return {"kind": kind, "label": label, "cmd": base}
    return {"kind": "other", "label": base, "cmd": base}


# ---------- Agent 友好的终端 HTTP 接口（基于 tmux，请求/响应式，无需 WebSocket） ----------
# exec：在会话 shell 里执行命令并等待结束，返回退出码与输出（cd/环境变量在会话内持久）
# send：向会话发送文本/特殊键（交互程序、Ctrl-C）；screen：读取屏幕/回滚文本
_IDLE_SHELLS = {"bash", "zsh", "sh", "dash", "-bash", "-zsh", "-sh"}
_exec_locks: dict[str, threading.Lock] = {}
_exec_locks_guard = threading.Lock()
EXEC_DIR = ROOT / "run" / "exec"
EXEC_MAX_TIMEOUT = 600.0
EXEC_MAX_OUTPUT = 200 * 1024     # 单次返回输出上限（保留尾部）
SEND_KEY_RE = re.compile(r"^(?:[CMS]-)*(?:[A-Za-z0-9]|Enter|Escape|Tab|BSpace|Space|Up|Down|Left|Right|Home|End|PageUp|PageDown|DC|IC|F(?:[1-9]|1[0-2]))$")


def _tmux(*args: str, timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args], capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
    )


def _term_sid(internal: str) -> str:
    return "wt-" + session_ticket.sanitize_name(internal)


def _pane_cmd(sid: str) -> str:
    cp = _tmux("display-message", "-p", "-t", f"{sid}:", "#{pane_current_command}")
    return (cp.stdout or "").strip() if cp.returncode == 0 else ""


def _capture(sid: str, scrollback: int = 0) -> str:
    """整屏（含 scrollback 行数）纯文本；-J 把终端自动折行的行接回去。"""
    args = ["capture-pane", "-p", "-J", "-t", f"{sid}:"]
    if scrollback > 0:
        args += ["-S", f"-{int(scrollback)}"]
    cp = _tmux(*args, timeout=10)
    if cp.returncode != 0:
        return ""
    return "\n".join(line.rstrip() for line in (cp.stdout or "").split("\n")).rstrip("\n")


def _term_target(user: str, name: str, create: bool) -> tuple[str, str] | tuple[None, str]:
    """校验并返回 (internal, sid)；失败返回 (None, 错误信息)。"""
    name = (name or "").strip()
    if not NAME_RE.match(name):
        return None, "无效会话名"
    internal = internal_name(user, name)
    if not owns_session(user, internal):
        return None, "无权访问该会话"
    live = {s["name"] for s in list_sessions()}
    if internal not in live:
        if not create:
            return None, "会话不存在或已停止"
        cp = run_ctl("create", internal)
        if cp.returncode != 0:
            return None, (cp.stderr or cp.stdout or "create failed").strip()
        drop_history_after_open(internal, "")
    return internal, _term_sid(internal)


def term_exec(sid: str, cmd: str, timeout: float) -> tuple[int, dict]:
    """在会话 shell 中执行 cmd。返回 (http_status, body)。"""
    fg = _pane_cmd(sid)
    if fg and fg not in _IDLE_SHELLS:
        return 409, {"error": "会话正忙（前台正在运行其它程序）", "foreground": fg,
                     "hint": "用 /api/term/screen 查看，/api/term/send 发 C-c 中断，或换一个会话名"}
    with _exec_locks_guard:
        lock = _exec_locks.setdefault(sid, threading.Lock())
    if not lock.acquire(blocking=False):
        return 409, {"error": "该会话正在执行另一条命令", "hint": "稍后重试或换会话名"}
    tag = uuid.uuid4().hex[:10]
    script = EXEC_DIR / f"{tag}.sh"
    try:
        EXEC_DIR.mkdir(parents=True, exist_ok=True)
        script.write_text(cmd if cmd.endswith("\n") else cmd + "\n", encoding="utf-8")
        # 回显出来的命令行里 __WT%s_%s 是被拆开的，不会被误认为真正的标记
        line = (f"printf '\\n__WT%s_%s\\n' B {tag}; . {script}; __wt_rc=$?; "
                f"printf '\\n__WT%s_%s_%d\\n' E {tag} $__wt_rc; rm -f {script}")
        cp = _tmux("send-keys", "-t", f"{sid}:", "-l", line)
        if cp.returncode != 0:
            return 500, {"error": (cp.stderr or "send-keys failed").strip()}
        _tmux("send-keys", "-t", f"{sid}:", "Enter")
        begin_re = re.compile(rf"^__WTB_{tag}$", re.M)
        end_re = re.compile(rf"^__WTE_{tag}_(\d+)$", re.M)
        deadline = time.monotonic() + timeout
        text = ""
        while True:
            text = _capture(sid, scrollback=20000)
            m = end_re.search(text)
            if m:
                break
            if _tmux("has-session", "-t", f"={sid}").returncode != 0:
                # 命令把 shell 本身结束了（如 exit）：tmux 会话随之消失
                return 200, {"timeout": False, "exit_code": None, "session_ended": True,
                             "output": "", "hint": "会话已结束（命令退出了 shell）；再次 exec 同名会话会自动新建"}
            if time.monotonic() > deadline:
                b = begin_re.search(text)
                part = text[b.end():] if b else text
                return 200, {"timeout": True, "exit_code": None,
                             "output": _trim_out(part.strip("\n")),
                             "hint": "命令仍在运行；用 /api/term/screen 继续查看，/api/term/send 发 C-c 中断"}
            time.sleep(0.15)
        b = begin_re.search(text)
        body = text[b.end():m.start()] if b else text[:m.start()]
        return 200, {"timeout": False, "exit_code": int(m.group(1)), "output": _trim_out(body.strip("\n"))}
    finally:
        try:
            script.unlink()
        except OSError:
            pass
        lock.release()


def _trim_out(out: str) -> str:
    raw = out.encode("utf-8")
    if len(raw) <= EXEC_MAX_OUTPUT:
        return out
    return "…(前面输出已截断，仅保留最后 %d KB)\n" % (EXEC_MAX_OUTPUT // 1024) + raw[-EXEC_MAX_OUTPUT:].decode("utf-8", "ignore")


def term_send(sid: str, text: str, keys: list[str], enter: bool) -> None:
    if text:
        cp = _tmux("send-keys", "-t", f"{sid}:", "-l", text)
        if cp.returncode != 0:
            raise RuntimeError((cp.stderr or "send-keys failed").strip())
    send_keys = list(keys) + (["Enter"] if enter else [])
    if send_keys:
        cp = _tmux("send-keys", "-t", f"{sid}:", *send_keys)
        if cp.returncode != 0:
            raise RuntimeError((cp.stderr or "send-keys failed").strip())


WT_CLI_FILE = ROOT / "docs" / "wt.py"

LLM_PROMPT_TMPL = r"""# 任务背景
我有一台远程服务器，上面跑着 Web Terminal（基于 tmux 的网页终端）。请你（本地 AI agent）通过下面的 HTTP 接口直接操作它的终端：执行命令、读输出、操作交互程序、传输文件。不需要浏览器，也不需要 WebSocket。

# 连接信息
- 管理/API 地址: __BASE__
- 终端页地址（仅浏览器/CDP 方式用）: __TERM__
- 账号 / 密码: **本提示词里没有，请你开始前先向我索取**（我会直接告诉你）。拿到后只放进环境变量 `WT_USER` / `WT_PASS`，不要写进文件、日志或你对我的回复。若我说“用 CDP 方式”，则不需要账号密码，见文末。
- 认证: 请求带 HTTP Basic（账号:密码）即可，无需先登录；wt 已内置这一步。

# 第一步：安装命令行工具 wt（已测试好，直接用，不要自己重写）
这是服务器自带的单文件 Python 3 工具（仅标准库，零依赖，内置了本站地址），免登录下载。账号密码只通过环境变量传入：

    mkdir -p ~/.local/bin && curl -fsS __BASE__/api/wt-cli -o ~/.local/bin/wt && chmod +x ~/.local/bin/wt
    export WT_USER='<我给你的账号>' WT_PASS='<我给你的密码>'
    python3 ~/.local/bin/wt doctor

（以上是 macOS/Linux 写法；Windows 请换成等价的 PowerShell 命令，如 `Invoke-WebRequest` 下载、`$env:WT_USER=...` 设变量、`python wt.py doctor` 运行，需要本机有 Python 3。）本站只提供 `http://`，不要改成 `https://`。

`doctor` 会一次性自检：连通、登录、执行命令与退出码、`cd` 状态保持、读屏、清理，输出 `"ok": true` 才算装好。失败时按它给出的信息告诉我，不要自己绕路。之后统一用 `python3 ~/.local/bin/wt <子命令>`（你可以在自己的环境里建别名 `wt`，但不要改写它的实现；需要新能力先告诉我）。

每个子命令输出一行 JSON；进程退出码 0=成功，3=远端命令非 0，2=超时（命令仍在运行），1=其它错误。

    wt exec <会话> "<命令>" [-t 秒]          执行并等待结束，返回 {exit_code, output}
    wt send <会话> ["文本"] [-k 键...] [--enter]   交互程序：发文本/按键，返回当前屏幕
    wt screen <会话> [--scrollback N]        读屏幕（可带 N 行回滚）
    wt ls | wt kill <会话>                    列出 / 结束我的会话
    wt put <本地文件> [--rel 子路径]          上传到服务器 ~/Downloads
    wt files [相对路径] | wt get <服务端相对路径> [-o 本地]   浏览 / 下载（目录自动打包 zip）
    wt doctor                                 自检

# 使用要点（均已实测）
0. **你自己的工具超时**：`wt exec -t N` 是服务器侧等待上限，要小于你自己 shell 工具的超时；预计超过一两分钟的任务，一律后台运行再轮询。
1. **会话**：会话名建议统一加前缀 `agent-`（如 `agent-build`），我在网页里能看到并旁观你的会话，也方便和我自己的会话区分。用名字标识（字母/数字/中文/下划线/短横线，≤64）。`exec`/`send` 对不存在的会话会自动创建；同名会话复用同一个 shell，所以 `cd`、`export`、虚拟环境在多次 `exec` 之间保持。建议为不同任务用不同会话名，用完 `kill` 自己建的会话，不要动不带 `agent-` 前缀的会话。
2. **exec 语义**：命令在会话 shell 里执行，返回真实退出码和纯文本输出（已去掉提示符和转义序列），可含多行、引号、中文。单次输出超过 200KB 只保留尾部。默认超时 60 秒，最大 600 秒。
3. **长任务/超时**：超时返回 `timeout:true`，命令仍在后台运行。这时用 `screen` 看进度，等它结束，或用 `send -k C-c` 中断。会话还在运行前台程序时再次 `exec` 会得到 HTTP 409（会话正忙），不会把命令塞进别人的输入里。长时间任务建议 `nohup cmd > log 2>&1 &` 后轮询日志。
4. **交互程序**（python REPL、vim、ssh、安装向导）：先用 `exec` 或 `send` 启动，再用 `send "文本" --enter` 输入，`send -k Up Enter Escape C-c C-d F5` 发按键，用返回的 `screen` 判断状态。键名为 tmux 键名。用 `C-d` 退出 REPL 后会话恢复可 `exec`。
5. **命令把 shell 退出了**（如 `exit`）：返回 `session_ended:true`，再次 `exec` 同名会话会自动新建。
6. **文件**：上传用 `put`；下载/浏览用 `files`/`get`。浏览范围是整台服务器磁盘；文件夹下载是 zip 流式打包，统计和打包大文件夹会比较慢。`wt put` 只传文件（目录先打包），同名会自动改名不覆盖；`wt get` 默认不覆盖本地已有文件。不要读取或回传凭据类文件。
7. **多用户**：每个账号只能看到并操作自己的会话。

# 原始接口（仅当 wt 不可用时才自己调用；wt 即对它们的封装）
- `POST __BASE__/api/term/exec`  JSON `{"name":"w1","cmd":"ls -la","timeout":60}` → `{"exit_code":0,"output":"...","timeout":false}`；会话忙返回 409
- `POST __BASE__/api/term/send`  JSON `{"name":"w1","text":"print(1)","keys":["C-c"],"enter":true,"wait":0.8}` → `{"foreground":"python3","screen":"..."}`
- `GET  __BASE__/api/term/screen?name=w1&scrollback=200` → `{"screen":"...","foreground":"bash"}`
- `GET  __BASE__/api/sessions` 列会话；`DELETE __BASE__/api/sessions/<名>` 结束会话
- `POST __BASE__/api/upload?rel=<子路径>` 请求体为文件原始字节
- `GET  __BASE__/api/fs-list?rel=<相对路径>`、`/api/fs-download?rel=`、`/api/fs-zip?rel=`（文件夹）、`/api/fs-size?rel=`

# 可选：不走 HTTP 接口，直接驱动浏览器里的终端（Chrome CDP）
适用：我已经在 Chrome 里登录了这个站点，你想操作我眼前那个标签页（或需要看到真实渲染的屏幕）。
1. 让我用 `--remote-debugging-port=9222` 启动 Chrome，你通过 CDP 连接（Playwright `connectOverCDP` 或原生 CDP 均可）。
2. 在管理页（__BASE__/ ，同源已带登录 Cookie）里执行 `fetch('/api/ticket',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'会话名',create:true})}).then(r=>r.json())`，拿到 `url`（形如 `/term/?arg=会话名&arg=t1.…`）。票据 12 小时有效，只对该会话名有效。
3. 在新标签页打开 `__TERM__` + 该 url（注意终端在另一个端口，管理页相对路径不能直接用）。等 `window.term` 出现。
4. 读屏：`window.term.buffer.active` 逐行 `getLine(i).translateToString(true)`，行数 = `buffer.active.length`。
5. 输入（实测）：`window.term.paste('命令')` 然后用 CDP 真实按键发送 `Enter`；单靠 `paste('…\r')` 或 `paste('…\n')` 不会执行。中文/引号可用 `Input.insertText` 或 `keyboard.insertText` 再按 `Enter`。`Ctrl+C` 用真实按键。`term.input` 在此版本不存在。
6. 页面是 xterm.js；终端与服务器之间走 WebSocket（子协议必须是 `tty`，首帧 JSON `{"AuthToken":"","columns":N,"rows":N}`，其后每帧首字节是类型：发送 `0`=输入 `1`=改尺寸，接收 `0`=输出 `1`=标题 `2`=偏好）。你通常不需要直接碰它，用上面的 HTTP 接口或 `window.term` 即可。仅当前两条都不可用时才自己实现这个协议（公网只提供 ws://，不提供 wss://）。

# 约束
- 优先级：wt > CDP > 原始接口/WebSocket。
- 每次操作前想清楚：`exec` 是同步等待的，命令可能阻塞；不确定会不会结束的命令请加 `-t` 或放后台。
- 删除/覆盖文件、重启服务、`kill` 进程等破坏性操作，先向我确认。
- 密码只通过环境变量 `WT_PASS` 使用，不要写进你生成的脚本、文件、日志或对我的回复里；不要把密码作为命令行参数传递。
"""


def render_llm_prompt(base: str, term: str) -> str:
    """提示词里不含任何账号密码：由用户自己告诉 agent。"""
    return LLM_PROMPT_TMPL.replace("__BASE__", base).replace("__TERM__", term)


def tmux_pane(sid: str) -> dict[str, str]:
    cp = subprocess.run(
        ["tmux", "display-message", "-p", "-t", f"{sid}:", "#{pane_tty}\t#{pane_pid}"],
        capture_output=True,
        text=True,
        timeout=3,
        env={**os.environ, "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
    )
    if cp.returncode != 0:
        return {}
    parts = (cp.stdout or "").strip().split("\t")
    if len(parts) < 2:
        return {}
    return {"tty": parts[0], "pid": parts[1]}


def foreground_process(sid: str) -> dict[str, str]:
    """pane 里真正的前台进程：tty 上带 '+'（前台进程组）且父进程是 pane 内 shell 的那个。"""
    pane = tmux_pane(sid)
    tty = pane.get("tty", "")
    pane_pid = pane.get("pid", "")
    if not tty or not pane_pid:
        return {"kind": "", "label": "", "cmd": ""}
    cp = subprocess.run(
        ["ps", "-t", tty.replace("/dev/", ""), "-o", "pid=,ppid=,stat=,args="],
        capture_output=True,
        text=True,
        timeout=3,
    )
    if cp.returncode != 0:
        return {"kind": "", "label": "", "cmd": ""}
    rows = []
    for line in (cp.stdout or "").splitlines():
        fields = line.strip().split(None, 3)
        if len(fields) < 4:
            continue
        rows.append({"pid": fields[0], "ppid": fields[1], "stat": fields[2], "args": fields[3]})
    fg = [r for r in rows if "+" in r["stat"]]
    # 直接挂在 pane shell 下的前台进程 = 用户手敲进入的那个（再深的是它自己拉起的子进程）
    direct = [r for r in fg if r["ppid"] == pane_pid]
    if direct:
        return label_process(direct[0]["args"])
    if any(r["pid"] == pane_pid for r in fg):
        return {"kind": "shell", "label": "", "cmd": "shell"}  # 就在 shell 提示符上
    if fg:
        return label_process(fg[0]["args"])
    return {"kind": "shell", "label": "", "cmd": "shell"}


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
        pages_raw = parts[7] if len(parts) > 7 else ""
        try:
            pages = int(pages_raw)
        except (TypeError, ValueError):
            pages = 0
        sessions.append(
            {
                "name": name,
                "sid": sid,
                "created": created,
                "last_open": last_open,
                "clients": int(clients or 0),
                "cwd": "" if cwd in ("", "-") else cwd,
                "cwd_now": "" if cwd_now in ("", "-") else cwd_now,
                "pages": pages,
            }
        )
    return sessions


NAME_SEP = "__"


def primary_user() -> str:
    """主用户名：users.json 首个，否则 .env 的 TTYD_USER。"""
    users = all_users()
    return next(iter(users), USER)


def is_primary(user: str) -> bool:
    return user == primary_user()


def internal_name(user: str, name: str) -> str:
    """用户视角名 → 内部会话名：主用户原名，其他用户 <user>__<name>。
    幂等：传入已是自己命名空间的内部名（带自己前缀）则原样返回——
    终端页 URL arg / 票据里存的就是内部名，manage 页传的是显示名。"""
    if is_primary(user):
        return name
    prefix = f"{user}{NAME_SEP}"
    if name.startswith(prefix):
        return name
    return f"{prefix}{name}"


def display_name(user: str, internal: str) -> str:
    """内部名 → 展示名：其他用户剥掉自己的前缀（别人的连别人的前缀都不剩）。"""
    if is_primary(user):
        return internal
    prefix = f"{user}{NAME_SEP}"
    if internal.startswith(prefix):
        return internal[len(prefix):]
    return internal


def owns_session(user: str, internal: str) -> bool:
    """internal 是否属于 user：主用户=无别人的前缀；其他用户=带自己前缀。"""
    p = primary_user()
    if user == p:
        return not any(internal.startswith(f"{u}{NAME_SEP}") for u in all_users() if u != p)
    return internal.startswith(f"{user}{NAME_SEP}")


def filter_sessions_for(user: str, sessions: list[dict]) -> list[dict]:
    out = []
    for s in sessions:
        internal = str(s.get("name") or "")
        if owns_session(user, internal):
            out.append({**s, "name": display_name(user, internal)})
    return out


def filter_history_for(user: str, items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        internal = str(it.get("name") or "")
        if owns_session(user, internal):
            out.append({**it, "name": display_name(user, internal)})
    return out


def filter_traffic_for(user: str, summary: dict) -> dict:
    """流量摘要按会话名前缀过滤。"""
    p = primary_user()
    out: dict = {}
    for k, v in summary.items():
        if k in ("today", "total"):
            out[k] = dict(v) if isinstance(v, dict) else v
            continue
        if user == p:
            if not any(str(k).startswith(f"{u}{NAME_SEP}") for u in all_users() if u != p):
                out[k] = v
        elif str(k).startswith(f"{user}{NAME_SEP}"):
            out[f"{user}{NAME_SEP}{display_name(user, str(k))}"] = v
    return out


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


def set_session_pages(name: str, pages: int) -> None:
    cp = run_ctl("set-pages", name, str(pages))
    if cp.returncode != 0:
        raise RuntimeError((cp.stderr or cp.stdout or "set-pages failed").strip())


def set_history_pages(name: str, cwd: str, pages: int) -> None:
    cp = run_ctl("history-set-pages", name, cwd or "", str(pages))
    if cp.returncode != 0:
        raise RuntimeError((cp.stderr or cp.stdout or "history-set-pages failed").strip())


def session_pages(name: str) -> int:
    """当前会话页数：优先运行中会话，其次历史记录；0 表示未设置(用全局默认)。"""
    for s in list_sessions():
        if s["name"] == name:
            return int(s.get("pages") or 0)
    for h in list_history(exclude_live=False):
        if h.get("name") == name:
            try:
                return int(h.get("pages") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def term_url(name: str, ticket: str) -> str:
    url = f"/term/?arg={quote(name, safe='')}&arg={quote(ticket, safe='')}"
    # 未单独配置的会话跟随全局默认；始终显式带上，改全局后下次打开即生效
    pages = session_pages(name) or default_pages()
    if pages > 0:
        url += f"&pages={pages}"
    return url


def parse_cookies(header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (header or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


# ---- 登录会话 Cookie（wt_auth）：替代 Basic 弹窗，表单登录后 24h 免输 ----
AUTH_COOKIE = "wt_auth"
LOGOUT_COOKIE = "wt_out"
AUTH_TTL_SEC = 24 * 3600
# 多用户：users.json 首个为主用户（无前缀，兼容旧会话/书签），其余用户会话名带 <user>__ 前缀


def all_users() -> dict[str, dict]:
    return session_ticket.load_users()


def find_user(username: str) -> dict | None:
    u = all_users().get(username)
    if u and u.get("password"):
        return u
    return None


# ---------- 登录失败限速（防暴力猜密码）----------
# 按“来源 IP”和“账号名”两个维度各自计数：窗口内失败达上限即锁定，锁定期内连正确密码也拒绝，
# 成功登录清零。IP 取 TCP 对端地址（本服务直连，不信任 X-Forwarded-For，防伪造绕过）。
LOGIN_FAIL_WINDOW = float(ENV.get("LOGIN_FAIL_WINDOW", "600"))     # 统计窗口（秒）
LOGIN_FAIL_MAX_IP = int(ENV.get("LOGIN_FAIL_MAX_IP", "10"))        # 单 IP 窗口内最多失败次数
LOGIN_FAIL_MAX_USER = int(ENV.get("LOGIN_FAIL_MAX_USER", "30"))    # 单账号窗口内最多失败次数（跨 IP 合计）
LOGIN_LOCK_SEC = float(ENV.get("LOGIN_LOCK_SEC", "900"))           # 锁定时长（秒）
_FAIL_LOCK = threading.Lock()
_FAILS: dict[str, list[float]] = {}     # key -> 失败时间戳
_LOCKED: dict[str, float] = {}          # key -> 解锁时间
_FAILS_MAX_KEYS = 20000                 # 防被灌爆内存


def _fail_keys(ip: str, username: str) -> tuple[str, str]:
    return "ip:" + ip, "u:" + username.lower()[:64]


def login_lock_remaining(ip: str, username: str) -> int:
    """被锁定则返回剩余秒数，否则 0。"""
    now = time.time()
    with _FAIL_LOCK:
        left = 0.0
        for k in _fail_keys(ip, username):
            until = _LOCKED.get(k, 0)
            if until > now:
                left = max(left, until - now)
            elif k in _LOCKED:
                del _LOCKED[k]
        return int(left) + 1 if left else 0


def login_record_failure(ip: str, username: str) -> None:
    now = time.time()
    k_ip, k_user = _fail_keys(ip, username)
    with _FAIL_LOCK:
        if len(_FAILS) > _FAILS_MAX_KEYS:   # 清掉过期的，仍超则丢弃最旧的
            for k in [k for k, v in _FAILS.items() if not v or now - v[-1] > LOGIN_FAIL_WINDOW]:
                _FAILS.pop(k, None)
            while len(_FAILS) > _FAILS_MAX_KEYS:
                _FAILS.pop(next(iter(_FAILS)), None)
        for k, limit in ((k_ip, LOGIN_FAIL_MAX_IP), (k_user, LOGIN_FAIL_MAX_USER)):
            lst = [t for t in _FAILS.get(k, []) if now - t <= LOGIN_FAIL_WINDOW]
            lst.append(now)
            _FAILS[k] = lst
            if len(lst) >= limit:
                _LOCKED[k] = now + LOGIN_LOCK_SEC
                _FAILS[k] = []
                sys.stderr.write(f"[auth] 锁定 {k} {int(LOGIN_LOCK_SEC)}s（窗口内失败 {len(lst)} 次）\n")


def login_record_success(ip: str, username: str) -> None:
    with _FAIL_LOCK:
        # 只清该 IP 和该账号的失败计数；不解除已生效的锁（锁定期内本来就不会走到这里）
        for k in _fail_keys(ip, username):
            _FAILS.pop(k, None)


def check_user_password(username: str, password: str) -> bool:
    if username == USER and password == PASSWORD:
        # Basic/旧配置的主用户（users.json 未配置时 .env 即主用户）
        return True
    u = find_user(username)
    return bool(u) and hmac.compare_digest(password, u["password"])


def issue_auth_token(user: str) -> tuple[str, int]:
    """返回 (token, max_age_seconds)，绑定用户（a3.<user>.<exp>.<sig>）。"""
    exp = int(time.time()) + AUTH_TTL_SEC
    msg = f"auth:{user}:{exp}".encode("utf-8")
    sig = hmac.new(session_ticket.user_secret(user, ENV), msg, hashlib.sha256).hexdigest()[:32]
    return f"a3.{user}.{exp}.{sig}", AUTH_TTL_SEC


def verify_auth_token(token: str) -> str | None:
    """返回用户名；无效/过期返回 None。"""
    parts = (token or "").split(".")
    if len(parts) != 4 or parts[0] != "a3":
        return None
    _, user, exp_s, sig = parts
    if user != USER and not find_user(user):
        return None
    try:
        exp = int(exp_s)
    except ValueError:
        return None
    if exp < int(time.time()):
        return None
    msg = f"auth:{user}:{exp_s}".encode("utf-8")
    expect = hmac.new(session_ticket.user_secret(user, ENV), msg, hashlib.sha256).hexdigest()[:32]
    return user if hmac.compare_digest(expect, sig) else None


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
    # 当前请求归属用户（登录 Cookie/Basic 解析出；默认主用户）
    wt_user: str = USER

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _unauthorized(self) -> None:
        left, self._basic_locked = getattr(self, "_basic_locked", 0), 0   # 用后即清，避免 keep-alive 连接残留
        if left:
            self._json(429, {"error": f"登录失败次数过多，已被临时锁定，请约 {(left + 59) // 60} 分钟后再试", "retry_after": left},
                       [("Retry-After", str(left))])
            return
        # 不再发 WWW-Authenticate：Basic 弹窗无法保存密码、且跨端口重复弹。
        # curl/脚本仍可用 -u（_basic_ok 静默接受）；未登录浏览器由 / 的登录页承接。
        self.send_response(401)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Unauthorized")

    def _basic_ok(self) -> bool:
        """Basic 认证（curl/脚本兼容）。凭据正确时记录用户名供后续过滤。"""
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        # 已点“退出登录”的浏览器带 wt_out 标记：忽略它缓存的 Basic 凭据，
        # 否则清掉登录 Cookie 后仍会被当作已登录。重新登录时清除标记。
        # curl/脚本不带该 Cookie，不受影响。
        if parse_cookies(self.headers.get("Cookie", "")).get(LOGOUT_COOKIE):
            return False
        try:
            raw = base64.b64decode(hdr[6:].encode("ascii")).decode("utf-8")
        except Exception:
            return False
        if ":" not in raw:
            return False
        u, p = raw.split(":", 1)
        self._basic_locked = 0
        ip = self.client_address[0]
        left = login_lock_remaining(ip, u)
        if left:
            self._basic_locked = left   # 锁定期内不校验（连正确密码也拒绝），由 _unauthorized 给出 429
            return False
        if check_user_password(u, p):
            login_record_success(ip, u)
            self.wt_user = u
            return True
        login_record_failure(ip, u)
        return False

    def _has_valid_auth(self) -> bool:
        """登录态：wt_auth Cookie（表单登录 24h）或 Basic（curl/脚本）。
        Cookie 优先：浏览器可能还缓存着别的账号的 Basic 凭据（旧弹框保存过），
        若 Basic 优先，换账号登录后仍会被识别成旧账号。只在无有效 Cookie 时才看 Basic。"""
        cookies = parse_cookies(self.headers.get("Cookie", ""))
        user = verify_auth_token(cookies.get(AUTH_COOKIE, ""))
        if user:
            self.wt_user = user
            return True
        return self._basic_ok()

    def _auth_cookie_header(self, user: str) -> str:
        token, max_age = issue_auth_token(user)
        # 不强制 Secure：本机 http://127.0.0.1 也可用；公网 HTTPS 下 SameSite=Lax 足够
        return (
            f"{AUTH_COOKIE}={token}; Path=/; Max-Age={max_age}; "
            f"HttpOnly; SameSite=Lax"
        )

    def _check_auth(self) -> bool:
        if self._has_valid_auth():
            return True
        self._unauthorized()
        return False

    def _send(self, code: int, body: bytes, content_type: str, extra_headers: list[tuple[str, str]] | None = None) -> None:
        headers = list(extra_headers or [])
        # 跨境裸 HTTP 很慢：文本类响应 >4KB 且客户端支持时 gzip（大页面省 70%+）
        if (
            code == 200
            and ("text/html" in content_type or "application/json" in content_type or "text/plain" in content_type)
            and len(body) > 4096
            and "gzip" in (self.headers.get("Accept-Encoding") or "")
        ):
            gz = gzip.compress(body, 6)
            if len(gz) < len(body):
                body = gz
                headers.append(("Content-Encoding", "gzip"))
                headers.append(("Vary", "Accept-Encoding"))
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict | list, extra_headers: list[tuple[str, str]] | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8", extra_headers)

    def _html(self, code: int, html: str, extra_headers: list[tuple[str, str]] | None = None) -> None:
        body = html.encode("utf-8")
        self._send(code, body, "text/html; charset=utf-8", extra_headers)

    def _binary(self, code: int, body: bytes, ctype: str) -> None:
        # 图标可长缓存：内容随发布变化时页面里的 data URI 才是主路径
        self._send(code, body, ctype, [("Cache-Control", "public, max-age=86400")])

    def _lan_cors_headers(self) -> list[tuple[str, str]]:
        """局域网直连时终端页在 ttyd 端口，粘贴图片是跨源请求。
        仅当 Origin 恰为本机同主机的 ttyd 端口时放行，并允许携带 Cookie。"""
        origin = self.headers.get("Origin", "")
        if not origin:
            return []
        try:
            u = urlparse(origin)
        except ValueError:
            return []
        if u.scheme not in ("http", "https") or not u.hostname:
            return []
        # loopback 也要放行：127.0.0.1:<ttyd> → 127.0.0.1:<manage> 同样是跨源（端口不同）
        want_port = str(u.port or "")
        if want_port != str(TTYD_PORT):
            return []
        return [
            ("Access-Control-Allow-Origin", origin),
            ("Access-Control-Allow-Credentials", "true"),
            ("Vary", "Origin"),
        ]

    def do_OPTIONS(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        cors = self._lan_cors_headers()
        # 局域网/跨端口直连时，终端页对 /api/* 的预检都要能过（带 Authorization 的
        # 请求会触发 preflight），放行范围仍只限同机 ttyd 源
        if path.startswith("/api/") and cors:
            self.send_response(204)
            for k, v in cors:
                self.send_header(k, v)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _redirect(self, location: str, cookies: list[str] | None = None) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        for c in cookies or []:
            self.send_header("Set-Cookie", c)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _enter_target(self, qs: dict[str, list[str]]) -> tuple[str, str, bool]:
        """/enter 的目标：next（完整终端 URL）或 name 参数。返回 (next_url, name, create)。"""
        next_url = (qs.get("next") or [""])[0]
        if next_url:
            name, create = _parse_term_next(next_url)
            if name:
                return next_url, name, create
            return "", "", False
        name = (qs.get("name") or [""])[0].strip()
        create = bool((qs.get("create") or [""])[0])
        if name and NAME_RE.match(name):
            return "", name, create
        return "", "", False

    def _issue_enter_redirect(self, next_url: str, name: str, create: bool, user: str = "") -> None:
        """已登录：签发票据并 302 回终端页（票据绑定该用户命名空间）。"""
        internal = internal_name(user, name)
        if create:
            live = {s["name"] for s in list_sessions()}
            if internal not in live:
                cp = run_ctl("create", internal)
                if cp.returncode != 0:
                    self._json(400, {"error": (cp.stderr or cp.stdout or "create failed").strip()})
                    return
                drop_history_after_open(internal, "")
        try:
            ticket = session_ticket.issue(internal, create=False, env=ENV)
        except ValueError as e:
            self._json(500, {"error": str(e)})
            return
        if next_url:
            base = _strip_ticket_from_next(next_url)
            sep = "&" if "?" in base else "?"
            target = f"{base}{sep}arg={quote(ticket, safe='')}"
        else:
            target = term_url(internal, ticket)
        self._redirect(target)

    def _handle_enter(self, qs: dict[str, list[str]]) -> None:
        """/enter：终端页无票据时的入口。已登录 → 直接发票据跳回；未登录 → 登录页。"""
        next_url, name, create = self._enter_target(qs)
        if not name:
            # 没有可进的会话名：当作普通登录页
            self._redirect("/")
            return
        if not self._has_valid_auth():
            self._html(200, render_login_html(next_url or term_url(name, "")))
            return
        self._issue_enter_redirect(next_url, name, create, self.wt_user)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        # 登录页与票据入口不走 _check_auth：未登录时由页面承接，而非 401
        if path in ("/", "/manage", "/login"):
            if not self._has_valid_auth():
                next_url = (qs.get("next") or [""])[0]
                self._html(200, render_login_html(next_url))
                return
            if path == "/login":
                next_url = (qs.get("next") or [""])[0]
                if _is_safe_next(next_url):
                    self.send_response(302)
                    self.send_header("Location", next_url)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._html(200, MANAGE_HTML)
            return

        if path == "/enter":
            self._handle_enter(qs)
            return

        if path == "/api/wt-cli":   # 命令行工具本身不含机密，免登录下发，地址按访问来源内置
            host = (self.headers.get("Host") or "").split(":")[0] or PUBLIC_HOST
            try:
                body = WT_CLI_FILE.read_text("utf-8").replace("__WT_DEFAULT_URL__", f"http://{host}:{MANAGE_PORT}")
                self._send(200, body.encode("utf-8"), "text/x-python; charset=utf-8",
                           [("Content-Disposition", 'attachment; filename="wt.py"'), ("Cache-Control", "no-store")])
            except OSError:
                self._json(404, {"error": "wt.py 不存在"})
            return

        if not self._check_auth():
            return

        if path == "/api/users":
            if not is_primary(self.wt_user):
                self._json(403, {"error": "仅管理员可管理用户"})
                return
            self._json(200, {"users": list_users_info(), "me": self.wt_user})
            return
        if path == "/api/sessions":
            user = self.wt_user
            self._json(
                200,
                {
                    "sessions": filter_sessions_for(user, list_sessions()),
                    "history": filter_history_for(user, list_history()),
                    "path_root": PATH_ROOT,
                    "default_path": DEFAULT_PATH,
                    "default_pages": default_pages(),
                    "env_default_pages": _env_default_pages(),
                    "max_pages": MAX_PAGES,
                    "lan_ip": detect_lan_ip(),
                    "lan_candidates": lan_candidates(),
                    "ttyd_port": TTYD_PORT,
                    "manage_port": MANAGE_PORT,
                    "term_base": TTYD_BASE_PATH + "/",
                    "public_host": PUBLIC_HOST,
                    "traffic": traffic_summary(),
                    "user": user,
                    "multi_user": bool(all_users()),
                    "is_admin": is_primary(user),
                },
            )
            return
        if path in ("/favicon.ico", "/icon.png"):
            try:
                self._binary(200, ICON_PNG.read_bytes(), "image/png")
            except OSError:
                self._json(404, {"error": "no icon"})
            return
        if path == "/icon.svg":
            try:
                self._binary(200, ICON_SVG.read_bytes(), "image/svg+xml")
            except OSError:
                self._json(404, {"error": "no icon"})
            return
        if path == "/api/llm-prompt":
            host = (self.headers.get("Host") or "").split(":")[0] or PUBLIC_HOST
            base = f"http://{host}:{MANAGE_PORT}"
            term = f"http://{host}:{TTYD_PORT}"
            self._json(200, {"prompt": render_llm_prompt(base, term)})
            return
        if path == "/api/term/screen":
            q = parse_qs(parsed.query)
            internal, sid = _term_target(self.wt_user, (q.get("name") or [""])[0], create=False)
            if internal is None:
                self._json(404 if "不存在" in sid else 400, {"error": sid})
                return
            try:
                lines = max(0, min(int((q.get("scrollback") or ["0"])[0]), 50000))
            except ValueError:
                lines = 0
            text = _capture(sid, scrollback=lines)
            self._json(200, {"name": (q.get("name") or [""])[0], "foreground": _pane_cmd(sid),
                             "screen": _trim_out(text)})
            return
        if path == "/api/foreground":
            cors = self._lan_cors_headers()
            name = (parse_qs(parsed.query).get("name") or [""])[0].strip()
            if not NAME_RE.match(name):
                self._json(400, {"error": "无效会话名"}, cors)
                return
            internal = internal_name(self.wt_user, name)
            if not owns_session(self.wt_user, internal):
                self._json(403, {"error": "无权访问该会话"}, cors)
                return
            sid = "wt-" + session_ticket.sanitize_name(internal)
            try:
                info = foreground_process(sid)
            except (OSError, subprocess.SubprocessError):
                info = {"kind": "", "label": "", "cmd": ""}
            self._json(200, {"ok": True, "name": name, **info}, cors)
            return
        if path == "/api/health":
            self._json(
                200,
                {
                    "ok": True,
                    "public": PUBLIC_HOST,
                    "default_pages": default_pages(),
                    "lan_ip": detect_lan_ip(),
                    "ttyd_port": TTYD_PORT,
                    "manage_port": MANAGE_PORT,
                },
            )
            return
        if path == "/api/traffic":
            qs = parse_qs(parsed.query)
            name = (qs.get("name") or [""])[0].strip()
            # 显示名/内部名统一映射后查询（wt_user 由登录态解析）
            internal = internal_name(self.wt_user, name)
            if name and not owns_session(self.wt_user, internal):
                self._json(403, {"error": "无权访问该会话"}, self._lan_cors_headers())
                return
            self._json(200, traffic_summary(internal), self._lan_cors_headers())
            return
        if path == "/api/upload-history":
            self._json(200, {"history": prune_upload_history(self.wt_user)})
            return
        if path == "/api/upload-download":
            qs = parse_qs(parsed.query)
            rel = unquote((qs.get("rel") or [""])[0]).strip()
            try:
                dest = _resolve_upload_rel(rel, self.wt_user)
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            if not dest.is_file():
                self._json(404, {"error": "文件不存在"})
                return
            try:
                size = dest.stat().st_size
            except OSError as e:
                self._json(500, {"error": str(e)})
                return
            # RFC 6266：文件名带非 ASCII 时用 filename*，中英文文件名都能正确落地
            quoted = quote(dest.name)
            body = b""  # 流式发送，不整读进内存（大文件）
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f"attachment; filename=\"{quoted}\"; filename*=UTF-8''{quoted}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                with dest.open("rb") as f:
                    while True:
                        chunk = f.read(UPLOAD_BLOCK)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # 客户端取消下载
            sys.stderr.write(f"[upload] download {rel} ({size} bytes)\n")
            return
        if path == "/api/fs-list":
            raw = unquote((parse_qs(parsed.query).get("rel") or [""])[0]).strip()
            try:
                # 支持三种输入：相对路径 / 绝对路径（根内）/ ~/ 开头；统一归一为根内相对路径
                rel = _fs_rel_from_abs(raw)
                self._json(200, fs_list(rel))
            except FileNotFoundError as e:
                self._json(404, {"error": str(e)})
            except (ValueError, PermissionError) as e:
                self._json(400, {"error": str(e)})
            except OSError as e:
                self._json(500, {"error": str(e)})
            return
        if path == "/api/fs-text":
            rel = unquote((parse_qs(parsed.query).get("rel") or [""])[0]).strip()
            try:
                p = _fs_resolve(rel)
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            if not p.is_file():
                self._json(404, {"error": "文件不存在"})
                return
            if p.stat().st_size > FS_TEXT_MAX:
                self._json(400, {"error": f"超过 {FS_TEXT_MAX // 1024 // 1024}MB，请下载后查看"})
                return
            if _fs_kind(p) not in ("text", "binary"):
                # 图片等其它类型走 /api/fs-image；但 svg 归文本
                pass
            try:
                raw = p.read_bytes()
                text = raw.decode("utf-8")  # 解不开就报错，不猜编码
            except UnicodeDecodeError:
                try:
                    text = raw.decode("utf-8", errors="replace")
                    self._json(200, {"rel": rel, "name": p.name, "size": len(raw), "truncated": False,
                                     "text": text[:200000], "encoding": "utf-8? (含二进制字符,已替换)"})
                except Exception as e:
                    self._json(500, {"error": str(e)})
                return
            except OSError as e:
                self._json(500, {"error": str(e)})
                return
            limit = 500000  # 前端渲染上限 50 万字符
            self._json(200, {"rel": rel, "name": p.name, "size": len(raw), "truncated": len(text) > limit,
                             "text": text[:limit], "encoding": "utf-8"})
            return
        if path == "/api/fs-image":
            rel = unquote((parse_qs(parsed.query).get("rel") or [""])[0]).strip()
            try:
                p = _fs_resolve(rel)
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            if not p.is_file():
                self._json(404, {"error": "文件不存在"})
                return
            size = p.stat().st_size
            if size > FS_IMAGE_MAX:
                self._json(400, {"error": f"图片超过 {FS_IMAGE_MAX // 1024 // 1024}MB，请下载后查看"})
                return
            ctype = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                     "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
                     "ico": "image/x-icon", "avif": "image/avif"}.get(p.suffix.lower().lstrip("."), "application/octet-stream")
            try:
                self._send(200, p.read_bytes(), ctype)
            except OSError as e:
                self._json(500, {"error": str(e)})
            return
        if path in ("/api/fs-size", "/api/fs-zip"):
            rel = unquote((parse_qs(parsed.query).get("rel") or [""])[0]).strip()
            try:
                top = _fs_resolve(rel)
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            if top == FS_ROOT:
                self._json(400, {"error": "不能打包整个根目录，请进入具体文件夹"})
                return
            if not top.is_dir():
                self._json(404, {"error": "文件夹不存在"})
                return
            if not os.access(top, os.R_OK | os.X_OK):
                self._json(403, {"error": "无权访问该文件夹"})
                return
            if path == "/api/fs-size":
                st = fs_dir_stats(top)
                self._json(200, {**st, "confirm_over": FS_ZIP_CONFIRM, "name": top.name})
                return
            if not _zip_slots.acquire(blocking=False):
                self._json(429, {"error": "当前打包任务较多，请稍后再试"})
                return
            try:
                quoted = quote(top.name + ".zip")
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f"attachment; filename=\"{quoted}\"; filename*=UTF-8''{quoted}")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")   # 无 Content-Length：以关闭连接结束
                self.end_headers()
                self.close_connection = True
                try:
                    n, sent = fs_stream_zip(top, self.wfile)
                    sys.stderr.write(f"[fs] zip {rel} ({n} files, {sent} bytes)\n")
                except (BrokenPipeError, ConnectionResetError):
                    sys.stderr.write(f"[fs] zip {rel} 客户端中断\n")
                except OSError as e:
                    sys.stderr.write(f"[fs] zip {rel} 失败: {e}\n")
            finally:
                _zip_slots.release()
            return
        if path == "/api/fs-download":
            rel = unquote((parse_qs(parsed.query).get("rel") or [""])[0]).strip()
            try:
                p = _fs_resolve(rel)
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            if not p.is_file():
                self._json(404, {"error": "文件不存在"})
                return
            try:
                size = p.stat().st_size
            except OSError as e:
                self._json(500, {"error": str(e)})
                return
            if size > FS_DOWNLOAD_MAX:
                self._json(400, {"error": "文件超过 2GB，暂不支持下载"})
                return
            quoted = quote(p.name)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f"attachment; filename=\"{quoted}\"; filename*=UTF-8''{quoted}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                with p.open("rb") as f:
                    while True:
                        chunk = f.read(UPLOAD_BLOCK)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            sys.stderr.write(f"[fs] download {rel} ({size} bytes)\n")
            return
        if path == "/api/logout":
            # 清登录 Cookie（顺带清旧版 PIN 解锁 Cookie）后回登录页
            kill = lambda name: f"{name}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
            out = f"{LOGOUT_COOKIE}=1; Path=/; Max-Age=604800; HttpOnly; SameSite=Lax"
            self._redirect("/", [kill(AUTH_COOKIE), kill("wt_pin_ok"), out])
            return
        self._json(404, {"error": "not found"})

    def _handle_upload(self) -> None:
        """POST /api/upload?rel=<相对路径>&overwrite=1  请求体=原始文件字节流。
        文件夹上传由前端拆成多个请求逐文件发送，rel 保留目录结构。
        默认同名自动改名不覆盖；overwrite=1 时覆盖历史记录指向的既有文件。"""
        if not self._check_auth():
            return
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        rel = unquote((qs.get("rel") or [""])[0]).strip()
        overwrite = (qs.get("overwrite") or [""])[0] in ("1", "true", "yes")
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            self._json(400, {"error": "无效的 Content-Length"})
            return
        try:
            if overwrite:
                dest, written = overwrite_upload_stream(rel, length, self.rfile, self.wt_user)
            else:
                dest, written = save_upload_stream(rel, length, self.rfile, self.wt_user)
        except ValueError as e:
            self._json(400, {"error": str(e)})
            return
        except ConnectionError as e:
            self._json(400, {"error": str(e)})
            return
        except RuntimeError as e:
            self._json(500, {"error": str(e)})
            return
        rel_out = str(dest.relative_to(UPLOAD_DIR))
        sys.stderr.write(f"[upload] {rel_out} ({written} bytes)\n")
        self._json(200, {"ok": True, "path": str(dest), "rel": rel_out, "bytes": written, "history": prune_upload_history(self.wt_user)})

    def _handle_batch(self) -> None:
        """POST /api/upload-batch：为一批上传创建独立子目录，返回相对目录名。"""
        try:
            d = new_upload_batch_dir(self.wt_user)
        except (OSError, RuntimeError) as e:
            self._json(500, {"error": str(e)})
            return
        sys.stderr.write(f"[upload] batch dir {d.name}\n")
        self._json(200, {"ok": True, "dir": d.name, "path": str(d)})

    def _read_body(self) -> dict[str, str]:
        """读取请求体为 dict：兼容表单（登录页原生 POST）与 JSON（脚本调用）。"""
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "")
        if "application/json" in ctype:
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                data = {}
            return {k: str(v) for k, v in data.items()}
        try:
            form = parse_qs(raw.decode("utf-8"))
        except UnicodeDecodeError:
            return {}
        return {k: (v[0] if v else "") for k, v in form.items()}

    def _handle_login(self) -> None:
        """POST /api/login：校验账号密码，种登录 Cookie；
        有 next（终端 URL）时直接签发票据跳回，否则回管理页。"""
        data = self._read_body()
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        next_url = data.get("next") or ""
        wants_json = "application/json" in (self.headers.get("Content-Type") or "")

        def fail(error: str):
            if wants_json:
                self._json(401, {"error": error})
            else:
                self._html(401, render_login_html(next_url, error))

        ip = self.client_address[0]
        left = login_lock_remaining(ip, username)
        if left:
            mins = (left + 59) // 60
            self._json(429, {"error": f"登录失败次数过多，请约 {mins} 分钟后再试", "retry_after": left}, [("Retry-After", str(left))]) \
                if wants_json else self._html(429, render_login_html(next_url, f"登录失败次数过多，请约 {mins} 分钟后再试"))
            return
        if not check_user_password(username, password):
            login_record_failure(ip, username)
            fail("账号或密码错误")
            return
        login_record_success(ip, username)

        cookies = [
            self._auth_cookie_header(username),
            f"{LOGOUT_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax",
        ]
        if next_url and _is_safe_next(next_url):
            name, create = _parse_term_next(next_url)
            if name:
                # 登录一步到位：签票据直接进终端（票据绑定该用户命名空间）
                internal = internal_name(username, name)
                if create:
                    live = {s["name"] for s in list_sessions()}
                    if internal not in live:
                        cp = run_ctl("create", internal)
                        if cp.returncode != 0:
                            fail((cp.stderr or cp.stdout or "create failed").strip())
                            return
                        drop_history_after_open(internal, "")
                try:
                    ticket = session_ticket.issue(internal, create=False, env=ENV)
                except ValueError as e:
                    fail(str(e))
                    return
                base = _strip_ticket_from_next(next_url)
                sep = "&" if "?" in base else "?"
                self._redirect(f"{base}{sep}arg={quote(ticket, safe='')}", cookies)
                return
            self._redirect(next_url, cookies)
            return
        self._redirect("/", cookies)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        # 登录：未认证即可调用，是获取登录态的唯一入口
        if path == "/api/login":
            self._handle_login()
            return
        # 文件上传：请求体是原始字节流，必须在此分流，不能走下面的 JSON 解析
        if path == "/api/upload":
            self._handle_upload()
            return
        if path == "/api/upload-batch":
            if not self._check_auth():
                return
            self._handle_batch()
            return
        if not self._check_auth():
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            data = {}

        if path == "/api/users":
            if not is_primary(self.wt_user):
                self._json(403, {"error": "仅管理员可管理用户"})
                return
            try:
                add_user(str(data.get("username") or ""), str(data.get("password") or ""))
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            except OSError as e:
                self._json(500, {"error": f"写入失败: {e}"})
                return
            self._json(200, {"ok": True, "users": list_users_info()})
            return

        if path == "/api/upload-delete":
            p = str(data.get("path") or "").strip()
            if not p:
                self._json(400, {"error": "缺少 path"})
                return
            try:
                result = remove_upload_file(p, self.wt_user)
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            except OSError as e:
                self._json(500, {"error": str(e)})
                return
            self._json(200, {"history": prune_upload_history(self.wt_user), **result})
            return

        if path == "/api/term/exec":
            cmd = str(data.get("cmd") or "")
            if not cmd.strip():
                self._json(400, {"error": "缺少 cmd"})
                return
            if len(cmd) > 200_000:
                self._json(400, {"error": "cmd 过长"})
                return
            try:
                timeout = min(max(float(data.get("timeout", 60)), 1.0), EXEC_MAX_TIMEOUT)
            except (TypeError, ValueError):
                timeout = 60.0
            internal, sid = _term_target(self.wt_user, str(data.get("name") or ""), create=data.get("create", True) is not False)
            if internal is None:
                self._json(404 if "不存在" in sid else (403 if "无权" in sid else 400), {"error": sid})
                return
            code, body = term_exec(sid, cmd, timeout)
            self._json(code, body)
            return
        if path == "/api/term/send":
            keys = data.get("keys") or []
            if isinstance(keys, str):
                keys = [keys]
            if not isinstance(keys, list) or not all(isinstance(k, str) and SEND_KEY_RE.match(k) for k in keys):
                self._json(400, {"error": "keys 只允许 tmux 键名，如 C-c、Enter、Up、Escape、F5"})
                return
            text = str(data.get("text") or "")
            if not text and not keys and not data.get("enter"):
                self._json(400, {"error": "需要 text 或 keys"})
                return
            internal, sid = _term_target(self.wt_user, str(data.get("name") or ""), create=bool(data.get("create")))
            if internal is None:
                self._json(404 if "不存在" in sid else (403 if "无权" in sid else 400), {"error": sid})
                return
            try:
                term_send(sid, text, keys, bool(data.get("enter")))
            except RuntimeError as e:
                self._json(500, {"error": str(e)})
                return
            try:
                wait = min(max(float(data.get("wait", 0.8)), 0.0), 10.0)
            except (TypeError, ValueError):
                wait = 0.8
            time.sleep(wait)
            self._json(200, {"ok": True, "foreground": _pane_cmd(sid), "screen": _trim_out(_capture(sid))})
            return

        if path == "/api/ticket":
            user = self.wt_user
            name = str(data.get("name") or "").strip()
            create = bool(data.get("create"))
            cwd = str(data.get("cwd") or "").strip()
            if not NAME_RE.match(name):
                self._json(400, {"error": "无效会话名"})
                return
            # 用户视角是显示名；实际会话/票据一律用内部名（其他用户带 <user>__ 前缀）
            internal = internal_name(user, name)
            if create:
                args = ["create", internal]
                if cwd:
                    args.append(cwd)
                cp = run_ctl(*args)
                if cp.returncode != 0:
                    self._json(400, {"error": (cp.stderr or cp.stdout or "create failed").strip()})
                    return
                drop_history_after_open(internal, cwd)
            else:
                live = {s["name"] for s in list_sessions()}
                if internal not in live:
                    self._json(404, {"error": "会话不存在或已停止"})
                    return
            try:
                ticket = session_ticket.issue(internal, create=False, env=ENV)
            except ValueError as e:
                self._json(500, {"error": str(e)})
                return
            self._json(200, {"ok": True, "ticket": ticket, "url": term_url(internal, ticket)})
            return

        if path == "/api/rename":
            old_name = str(data.get("old_name") or "").strip()
            new_name = str(data.get("new_name") or "").strip()
            if not NAME_RE.match(old_name) or not NAME_RE.match(new_name):
                self._json(400, {"error": "无效会话名"})
                return
            user = self.wt_user
            old_internal = internal_name(user, old_name)
            new_internal = internal_name(user, new_name)
            # 兜底：旧名必须属于自己（防止改别人的会话名）
            if not owns_session(user, old_internal):
                self._json(404, {"error": "会话不存在或已停止"})
                return
            try:
                result = rename_session(old_internal, new_internal)
            except RuntimeError as e:
                self._json(400, {"error": str(e)})
                return
            self._json(200, {"ok": True, "result": result})
            return

        if path == "/api/pages":
            scope = str(data.get("scope") or "session").strip()
            try:
                pages = int(data.get("pages"))
            except (TypeError, ValueError):
                self._json(400, {"error": "页数必须是整数"})
                return

            if scope == "global":
                # 全局默认：必须是实数页，不能为 0（0 只在会话级表示“不配置”）
                if pages < 1 or pages > MAX_PAGES:
                    self._json(400, {"error": f"全局默认需在 1~{MAX_PAGES} 之间"})
                    return
                try:
                    set_default_pages(pages)
                except OSError as e:
                    self._json(500, {"error": f"写入全局默认失败: {e}"})
                    return
                self._json(200, {"ok": True, "scope": "global", "default_pages": pages})
                return

            name = str(data.get("name") or "").strip()
            cwd = str(data.get("cwd") or "").strip()
            live = bool(data.get("live"))
            if not NAME_RE.match(name):
                self._json(400, {"error": "无效会话名"})
                return
            # 0 = 清除本会话配置，回落全局默认
            if pages < 0 or pages > MAX_PAGES:
                self._json(400, {"error": f"页数需在 0~{MAX_PAGES} 之间(0=用全局默认)"})
                return
            internal = internal_name(self.wt_user, name)
            try:
                if live:
                    set_session_pages(internal, pages)
                else:
                    set_history_pages(internal, cwd, pages)
            except RuntimeError as e:
                self._json(400, {"error": str(e)})
                return
            self._json(
                200,
                {"ok": True, "pages": pages, "inherited": pages == 0, "default_pages": default_pages()},
            )
            return

        if path == "/api/traffic":
            cors = self._lan_cors_headers()
            name = str(data.get("name") or "").strip()
            if not NAME_RE.match(name):
                self._json(400, {"error": "无效会话名"}, cors)
                return
            # 上报名映射到该用户命名空间后的内部名入账：显示名自动加前缀、
            # 已带自己前缀的原样保留；跨用户的名字永远落不到别人头上
            internal = internal_name(self.wt_user, name)
            if not owns_session(self.wt_user, internal):
                self._json(400, {"error": "无效会话名"}, cors)
                return
            try:
                rx = int(data.get("rx") or 0)
                tx = int(data.get("tx") or 0)
            except (TypeError, ValueError):
                self._json(400, {"error": "rx/tx 必须是整数"}, cors)
                return
            # 单次上报上限护栏：5s 内不可能超过 512MB，异常值直接拒绝防污染统计
            if rx < 0 or tx < 0 or rx > 512 * 1024 * 1024 or tx > 512 * 1024 * 1024:
                self._json(400, {"error": "流量增量超出合理范围"}, cors)
                return
            try:
                summary = add_traffic(internal, rx, tx)
            except ValueError as e:
                self._json(400, {"error": str(e)}, cors)
                return
            except RuntimeError as e:
                self._json(500, {"error": str(e)}, cors)
                return
            self._json(200, {"ok": True, **summary}, cors)
            return

        if path == "/api/paste-image":
            cors = self._lan_cors_headers()
            mime = str(data.get("mime") or "image/png")
            b64 = str(data.get("image_base64") or "")
            if not b64:
                self._json(400, {"error": "缺少 image_base64"}, cors)
                return
            try:
                img_raw = base64.b64decode(b64, validate=False)
            except Exception:
                self._json(400, {"error": "base64 无效"}, cors)
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
                self._json(400, {"error": str(e)}, cors)
                return
            except RuntimeError as e:
                self._json(500, {"error": str(e)}, cors)
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
                cors,
            )
            return

        self._json(404, {"error": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._check_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/users/"):
            if not is_primary(self.wt_user):
                self._json(403, {"error": "仅管理员可管理用户"})
                return
            target = unquote(path[len("/api/users/"):].strip("/"))
            if target == self.wt_user:
                self._json(400, {"error": "不能删除当前登录的账号"})
                return
            try:
                result = delete_user(target)
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            except OSError as e:
                self._json(500, {"error": f"写入失败: {e}"})
                return
            self._json(200, {"ok": True, **result, "users": list_users_info()})
            return
        if path == "/api/history" or path.startswith("/api/history"):
            qs = parse_qs(parsed.query)
            name = (qs.get("name") or [""])[0].strip()
            cwd = (qs.get("cwd") or [""])[0]
            if not name:
                self._json(400, {"error": "name required"})
                return
            internal = internal_name(self.wt_user, name)
            try:
                n = delete_history(internal, cwd)
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
            # 只能杀自己的会话（前端只展示自己的，这里兜底防直调）
            internal = internal_name(self.wt_user, name)
            live = {s["name"] for s in list_sessions()}
            if internal not in live or not owns_session(self.wt_user, internal):
                self._json(404, {"error": "会话不存在或已停止"})
                return
            qs = parse_qs(parsed.query)
            purge = (qs.get("purge") or [""])[0] in ("1", "true", "yes")
            args = ["kill", internal]
            if purge:
                args.append("--purge")
            cp = run_ctl(*args)
            if cp.returncode != 0:
                self._json(500, {"error": cp.stderr.strip() or "kill failed"})
                return
            self._json(200, {"ok": True, "result": cp.stdout.strip()})
            return
        self._json(404, {"error": "not found"})


_users_lock = threading.Lock()
USERNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,31}")


def _write_users(users: dict[str, dict]) -> None:
    """原子写 users.json（600），并让 mtime 缓存立即失效。调用方须持有 _users_lock。"""
    f = session_ticket.USERS_FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(users, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, f)
    session_ticket._users_cache = (0.0, {})


def _user_session_names(user: str) -> list[str]:
    prefix = f"{user}{NAME_SEP}"
    return [s["name"] for s in list_sessions() if str(s["name"]).startswith(prefix)]


def list_users_info() -> list[dict]:
    users = all_users()
    live = [str(s["name"]) for s in list_sessions()]
    out = []
    for name in users:
        prim = is_primary(name)
        if prim:
            cnt = sum(1 for n in live if owns_session(name, n))
        else:
            cnt = sum(1 for n in live if n.startswith(f"{name}{NAME_SEP}"))
        out.append({"username": name, "primary": prim, "sessions": cnt})
    return out


def add_user(username: str, password: str) -> None:
    """新增用户；校验失败抛 ValueError（消息可直接展示）。"""
    username = (username or "").strip()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("用户名只能含字母、数字、短横线，以字母或数字开头，最长 32 位")
    if len(password) < 6 or len(password) > 128 or any(ord(c) < 32 for c in password):
        raise ValueError("密码长度需在 6~128 位之间，且不能含控制字符")
    with _users_lock:
        users = {k: dict(v) for k, v in all_users().items()}
        if any(k.lower() == username.lower() for k in users) or username.lower() == USER.lower():
            raise ValueError("用户名已存在")
        # 主用户已有以 <username>__ 开头的会话名时，新用户会“继承”它们，拒绝
        prefix = f"{username}{NAME_SEP}"
        if any(str(s["name"]).startswith(prefix) for s in list_sessions()):
            raise ValueError("该用户名与现有会话名前缀冲突，请换一个")
        users[username] = {"password": password}
        _write_users(users)


def delete_user(username: str) -> dict:
    """删除用户：移除账号（登录 Cookie、终端票据随之失效），停掉并清理其会话与历史。
    保留其上传的文件（~/Downloads/<user>/），避免误删数据。"""
    with _users_lock:
        users = {k: dict(v) for k, v in all_users().items()}
        if username not in users:
            raise ValueError("用户不存在")
        if is_primary(username):
            raise ValueError("不能删除主管理员")
        del users[username]
        _write_users(users)
    killed = 0
    prefix = f"{username}{NAME_SEP}"
    for name in _user_session_names(username):
        if run_ctl("kill", name, "--purge").returncode == 0:
            killed += 1
    hist = 0
    try:
        for it in list_history(exclude_live=False):
            n = str(it.get("name") or "")
            if n.startswith(prefix):
                hist += delete_history(n, str(it.get("cwd") or ""))
    except RuntimeError:
        pass
    try:
        upload_hist_file_for(username).unlink()
    except OSError:
        pass
    return {"killed_sessions": killed, "deleted_history": hist}


def ensure_users_file() -> None:
    """首次启用多用户：users.json 不存在时用 .env 主账号生成。
    之后管理员可直接编辑该文件增删用户（服务端 mtime 缓存自动生效）。"""
    if session_ticket.USERS_FILE.exists():
        return
    users = {USER: {"password": PASSWORD}}
    session_ticket.USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    session_ticket.USERS_FILE.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        session_ticket.USERS_FILE.chmod(0o600)
    except OSError:
        pass
    print(f"[users] 初始化 {session_ticket.USERS_FILE}（主用户 {USER}）", flush=True)


def main() -> None:
    if not PASSWORD:
        raise SystemExit(".env 配置错误: TTYD_PASSWORD 未设置")
    ensure_users_file()
    users = all_users()
    if users:
        print(f"[users] {len(users)} 个用户: {', '.join(users.keys())}（首位为主用户）", flush=True)
    httpd = ThreadingHTTPServer((MANAGE_HOST, MANAGE_PORT), Handler)
    start_lan_watcher()  # 后台每 20s 刷新局域网地址，避免页面显示过期 IP
    print(f"manage listening on http://{MANAGE_HOST}:{MANAGE_PORT}", flush=True)
    print(f"path_root={PATH_ROOT} default_path={DEFAULT_PATH}", flush=True)
    print(f"lan_ip={detect_lan_ip() or '(未探测到)'}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
