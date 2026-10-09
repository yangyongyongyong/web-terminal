#!/usr/bin/env python3
"""会话进入票据：按用户签发/校验，防止没登录直接用终端 URL 越权进入。

门槛只有一道：管理服务只会给"已登录用户"签发票据，attach-session.sh 校验票据签名。
多用户：config/users.json（首个为主用户）。会话内部名带 "<user>__" 前缀的用该用户
的密码派生密钥；无前缀（主用户/历史遗留）用主用户密钥。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USERS_FILE = ROOT / "config" / "users.json"
# 终端 URL 票据：覆盖断线重连
TTL_SEC = 12 * 3600
NAME_SEP = "__"


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def _key(user: str, password: str) -> bytes:
    return hashlib.sha256(f"wt-ticket-v2:{user}:{password}".encode("utf-8")).digest()


# ---- 多用户：config/users.json（无该文件=单用户，回落 .env 的 TTYD_USER/TTYD_PASSWORD）----
_users_cache: tuple[float, dict[str, dict]] = (0.0, {})


def load_users() -> dict[str, dict]:
    """读 users.json（mtime 缓存）。返回 {用户名: {"password":…}}；
    文件不存在/损坏时返回 {}，此时一律按主用户（.env）处理。
    用户名限 [A-Za-z0-9-]（会话名前缀 <user>__ 的解析前提）。"""
    global _users_cache
    try:
        mtime = USERS_FILE.stat().st_mtime
    except OSError:
        return {}
    if _users_cache[0] == mtime:
        return _users_cache[1]
    try:
        raw = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        users = {
            str(k): {"password": str(v.get("password", ""))}
            for k, v in raw.items()
            if isinstance(v, dict) and v.get("password")
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,31}", str(k))
        }
    except (OSError, json.JSONDecodeError, AttributeError):
        users = {}
    _users_cache = (mtime, users)
    return users


def primary_creds(env: dict[str, str]) -> tuple[str, str]:
    """主用户 (用户名, 密码)：users.json 首个，回落 .env。"""
    users = load_users()
    if users:
        name, u = next(iter(users.items()))
        return name, u["password"]
    return env.get("TTYD_USER", "admin"), env.get("TTYD_PASSWORD", "")


def user_secret(user: str, env: dict[str, str] | None = None) -> bytes:
    """指定用户的签名密钥；未知用户回落主用户。"""
    env = env or load_env()
    u = load_users().get(user)
    if u:
        return _key(user, u["password"])
    pname, ppw = primary_creds(env)
    return _key(pname, ppw)


def owner_of_session(name: str, env: dict[str, str] | None = None) -> str:
    """内部会话名 → 归属用户名。<user>__<原名> 归 user（非主用户），其余归主用户。"""
    env = env or load_env()
    pname, _ = primary_creds(env)
    if NAME_SEP in name:
        head = name.split(NAME_SEP, 1)[0]
        if head in load_users() and head != pname:
            return head
    return pname


def sanitize_name(name: str) -> str:
    """允许中英文、数字、_ -；禁止空白与 . : / \\ 等（ticket 用 . 分隔）。"""
    cleaned = "".join(ch for ch in (name or "").strip() if ch.isalnum() or ch in "_-")
    return cleaned[:64] or "main"


def is_valid_name(name: str) -> bool:
    s = (name or "").strip()
    return bool(s) and s == sanitize_name(s) and len(s) <= 64


def issue(name: str, create: bool = False, env: dict[str, str] | None = None) -> str:
    env = env or load_env()
    name = sanitize_name(name)
    key = user_secret(owner_of_session(name, env), env)
    exp = int(time.time()) + TTL_SEC
    flag = "c" if create else "a"
    msg = f"{exp}:{name}:{flag}".encode("utf-8")
    sig = hmac.new(key, msg, hashlib.sha256).hexdigest()[:32]
    return f"t1.{exp}.{name}.{flag}.{sig}"


def verify(name: str, ticket: str, need_create: bool = False, env: dict[str, str] | None = None) -> bool:
    env = env or load_env()
    name = sanitize_name(name)
    parts = (ticket or "").split(".")
    if len(parts) != 5 or parts[0] != "t1":
        return False
    _, exp_s, t_name, flag, sig = parts
    if t_name != name:
        return False
    if flag not in ("a", "c"):
        return False
    if need_create and flag != "c":
        return False
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    key = user_secret(owner_of_session(name, env), env)
    msg = f"{exp}:{name}:{flag}".encode("utf-8")
    expect = hmac.new(key, msg, hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(expect, sig)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "usage: session-ticket.py issue <name> [create]|verify <name> <ticket> [create]|sanitize <name>",
            file=sys.stderr,
        )
        return 2
    cmd = argv[1]
    try:
        if cmd == "sanitize":
            print(sanitize_name(argv[2] if len(argv) > 2 else ""))
            return 0
        if cmd == "issue":
            name = argv[2] if len(argv) > 2 else "main"
            create = len(argv) > 3 and argv[3] == "create"
            print(issue(name, create=create))
            return 0
        if cmd == "verify":
            name = argv[2]
            ticket = argv[3]
            need_create = len(argv) > 4 and argv[4] == "create"
            ok = verify(name, ticket, need_create=need_create)
            print("ok" if ok else "fail")
            return 0 if ok else 1
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1
    print("unknown command", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
