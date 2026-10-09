#!/usr/bin/env python3
"""wt — Web Terminal 命令行客户端（给本地 agent 用，仅 Python 标准库，无需安装依赖）。

环境变量: WT_USER=账号  WT_PASS=密码  （WT_URL 可选：从服务器下载时已内置默认地址）
所有子命令输出一行 JSON；进程退出码: 0 成功 / 3 远端命令非 0 / 2 超时(命令仍在运行) / 1 其它错误

  wt exec <会话> "<命令>" [-t 秒]    在会话 shell 里执行并等待结束；cd/环境变量在同一会话内持久
  wt send <会话> [文本] [-k 键 ...] [--enter] [--wait 秒]   交互程序：发文本/按键，返回屏幕
  wt screen <会话> [--scrollback N]  读取屏幕（可带 N 行回滚）
  wt ls                              列出我的会话
  wt kill <会话>                     结束会话
  wt put <本地路径> [--rel 远端相对路径]   上传文件到服务器 ~/Downloads（目录请先打包）
  wt files [相对路径]                列服务端目录
  wt get <服务端相对路径> [-o 本地]  下载文件；目录自动打包 zip
  wt doctor                          自检：连通性/账号/执行/读屏/清理，一次确认全部可用
"""
import argparse, base64, json, os, sys, urllib.error, urllib.parse, urllib.request

VERSION = "1"
_DEFAULT_URL = "__WT_DEFAULT_URL__"   # 服务器下发时替换为本站地址
URL = (os.environ.get("WT_URL") or ("" if _DEFAULT_URL.startswith("__") else _DEFAULT_URL)).rstrip("/")
USER, PASS = os.environ.get("WT_USER", ""), os.environ.get("WT_PASS", "")


def out(obj, code=0):
    txt = json.dumps(obj, ensure_ascii=False)
    try:
        sys.stdout.write(txt + "\n")
    except UnicodeEncodeError:   # 终端不是 UTF-8（如 ASCII/GBK 环境）：退回转义，避免崩溃
        sys.stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")
    sys.stdout.flush(); sys.exit(code)


def call(method, path, body=None, raw=False, data=None, timeout=900, size=None):
    if not (URL and USER and PASS):
        out({"error": "请设置环境变量 WT_USER / WT_PASS（以及 WT_URL，若脚本未内置地址）"}, 1)
    req = urllib.request.Request(URL + path, method=method, data=data)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{USER}:{PASS}".encode()).decode())
    if body is not None:
        req.data = json.dumps(body).encode(); req.add_header("Content-Type", "application/json")
    if size is not None:
        req.add_header("Content-Length", str(size))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read() if raw else json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try: j = json.loads(e.read().decode())
        except Exception: j = {"error": e.reason}
        j["http_status"] = e.code
        out(j, 1)
    except urllib.error.URLError as e:
        out({"error": f"连接失败: {e.reason}"}, 1)


def main():
    p = argparse.ArgumentParser(prog="wt", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="c", required=True)
    s = sp.add_parser("exec"); s.add_argument("name"); s.add_argument("cmd"); s.add_argument("-t", "--timeout", type=float, default=60)
    s = sp.add_parser("send"); s.add_argument("name"); s.add_argument("text", nargs="?", default="")
    s.add_argument("-k", "--keys", nargs="*", default=[]); s.add_argument("--enter", action="store_true"); s.add_argument("--wait", type=float, default=0.8)
    s = sp.add_parser("screen"); s.add_argument("name"); s.add_argument("--scrollback", type=int, default=0)
    sp.add_parser("ls")
    s = sp.add_parser("kill"); s.add_argument("name")
    s = sp.add_parser("put"); s.add_argument("path"); s.add_argument("--rel")
    s = sp.add_parser("files"); s.add_argument("rel", nargs="?", default="")
    s = sp.add_parser("get"); s.add_argument("rel"); s.add_argument("-o"); s.add_argument("--force", action="store_true")
    sp.add_parser("doctor")
    a = p.parse_args()
    q = urllib.parse.quote
    if a.c == "exec":
        r = call("POST", "/api/term/exec", {"name": a.name, "cmd": a.cmd, "timeout": a.timeout}, timeout=a.timeout + 30)
        out(r, 2 if r.get("timeout") else (0 if r.get("exit_code") in (0, None) else 3))
    elif a.c == "send":
        out(call("POST", "/api/term/send", {"name": a.name, "text": a.text, "keys": a.keys, "enter": a.enter, "wait": a.wait, "create": True}))
    elif a.c == "screen":
        out(call("GET", f"/api/term/screen?name={q(a.name)}&scrollback={a.scrollback}"))
    elif a.c == "ls":
        d = call("GET", "/api/sessions")
        out({"user": d.get("user"), "sessions": [{"name": s["name"], "cwd": s.get("cwd_now") or s.get("cwd")} for s in d.get("sessions", [])]})
    elif a.c == "kill":
        out(call("DELETE", "/api/sessions/" + q(a.name)))
    elif a.c == "put":
        rel = a.rel or os.path.basename(a.path)
        if not os.path.isfile(a.path):
            out({"error": "只能上传文件；目录请先 zip/tar 打包"}, 1)
        with open(a.path, "rb") as f:   # 文件对象流式发送，不整体读入内存
            out(call("POST", "/api/upload?rel=" + q(rel), data=f, size=os.path.getsize(a.path)))
    elif a.c == "files":
        d = call("GET", "/api/fs-list?rel=" + q(a.rel))
        out({"rel": d.get("rel"), "items": [{"name": i["name"], "dir": i["dir"], "size": i["size"], "rel": i["rel"]} for i in d.get("items", [])]})
    elif a.c == "doctor":
        n = "wt-doctor"; steps = []
        who = call("GET", "/api/sessions"); steps.append({"step": "登录/连通", "ok": True, "detail": f"user={who.get('user')} url={URL}"})
        r = call("POST", "/api/term/exec", {"name": n, "cmd": "cd /tmp && echo doctor-$((6*7)); echo err >&2; false", "timeout": 30}, timeout=60)
        steps.append({"step": "exec(输出/退出码)", "ok": r.get("exit_code") == 1 and "doctor-42" in r.get("output", ""), "detail": r})
        r = call("POST", "/api/term/exec", {"name": n, "cmd": "pwd", "timeout": 30}, timeout=60)
        steps.append({"step": "会话状态保持(cd)", "ok": r.get("output", "").strip() == "/tmp", "detail": r.get("output")})
        r = call("POST", "/api/term/send", {"name": n, "text": "echo screen-ok", "enter": True, "wait": 0.8})
        steps.append({"step": "send+读屏", "ok": "screen-ok" in r.get("screen", ""), "detail": r.get("foreground")})
        call("DELETE", "/api/sessions/" + q(n)); steps.append({"step": "清理测试会话", "ok": True})
        ok = all(x["ok"] for x in steps)
        out({"version": VERSION, "ok": ok, "steps": steps}, 0 if ok else 1)
    elif a.c == "get":
        rel = a.rel.strip("/")
        try:   # 先静默探测是否目录（目录有 fs-size，文件会 404）
            urllib.request.urlopen(urllib.request.Request(URL + "/api/fs-size?rel=" + q(rel), headers={
                "Authorization": "Basic " + base64.b64encode(f"{USER}:{PASS}".encode()).decode()}), timeout=60).read()
            is_dir = True
        except urllib.error.HTTPError:
            is_dir = False
        blob = call("GET", ("/api/fs-zip?rel=" if is_dir else "/api/fs-download?rel=") + q(rel), raw=True)
        dest = a.o or (os.path.basename(rel) + (".zip" if is_dir else ""))
        if os.path.exists(dest) and not a.force:
            out({"error": f"本地已存在 {dest}，换个 -o 路径，或加 --force 覆盖"}, 1)
        with open(dest, "wb") as f: f.write(blob)
        out({"saved": dest, "bytes": len(blob), "zip": is_dir})


if __name__ == "__main__":
    main()
