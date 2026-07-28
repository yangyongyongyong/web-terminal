#!/usr/bin/env python3
"""会话进入票据：用 SESSION_PIN 签发/校验，防止仅 Basic Auth 直连 /term。"""
from __future__ import annotations

import hashlib
import hmac
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PIN_RE = re.compile(r"^.{1,128}$")
# 终端 URL 票据：覆盖断线重连
TTL_SEC = 12 * 3600
# PIN 解锁 Cookie：输入一次后 24h 内免再输
UNLOCK_TTL_SEC = 24 * 3600
UNLOCK_COOKIE = "wt_pin_ok"


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


def get_pin(env: dict[str, str] | None = None) -> str:
    env = env or load_env()
    pin = (env.get("SESSION_PIN") or "").strip()
    if not PIN_RE.match(pin):
        raise ValueError("SESSION_PIN 未配置或过长")
    return pin


def _key(pin: str, password: str) -> bytes:
    return hashlib.sha256(f"wt-ticket:{pin}:{password}".encode("utf-8")).digest()


def sanitize_name(name: str) -> str:
    """允许中英文、数字、_ -；禁止空白与 . : / \\ 等（ticket 用 . 分隔）。"""
    cleaned = "".join(ch for ch in (name or "").strip() if ch.isalnum() or ch in "_-")
    return cleaned[:64] or "main"


def is_valid_name(name: str) -> bool:
    s = (name or "").strip()
    return bool(s) and s == sanitize_name(s) and len(s) <= 64


def issue(name: str, create: bool = False, env: dict[str, str] | None = None) -> str:
    env = env or load_env()
    pin = get_pin(env)
    password = env.get("TTYD_PASSWORD", "")
    name = sanitize_name(name)
    exp = int(time.time()) + TTL_SEC
    flag = "c" if create else "a"
    msg = f"{exp}:{name}:{flag}".encode("utf-8")
    sig = hmac.new(_key(pin, password), msg, hashlib.sha256).hexdigest()[:32]
    return f"t1.{exp}.{name}.{flag}.{sig}"


def verify(name: str, ticket: str, need_create: bool = False, env: dict[str, str] | None = None) -> bool:
    env = env or load_env()
    try:
        pin = get_pin(env)
    except ValueError:
        return False
    password = env.get("TTYD_PASSWORD", "")
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
    msg = f"{exp}:{name}:{flag}".encode("utf-8")
    expect = hmac.new(_key(pin, password), msg, hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(expect, sig)


def check_pin(pin: str, env: dict[str, str] | None = None) -> bool:
    env = env or load_env()
    try:
        expect = get_pin(env)
    except ValueError:
        return False
    if not pin or len(pin) > 128:
        return False
    return hmac.compare_digest(pin, expect)


def issue_unlock(env: dict[str, str] | None = None) -> tuple[str, int]:
    """返回 (token, max_age_seconds)。"""
    env = env or load_env()
    pin = get_pin(env)
    password = env.get("TTYD_PASSWORD", "")
    exp = int(time.time()) + UNLOCK_TTL_SEC
    msg = f"unlock:{exp}".encode("utf-8")
    sig = hmac.new(_key(pin, password), msg, hashlib.sha256).hexdigest()[:32]
    return f"u1.{exp}.{sig}", UNLOCK_TTL_SEC


def verify_unlock(token: str, env: dict[str, str] | None = None) -> bool:
    env = env or load_env()
    try:
        pin = get_pin(env)
    except ValueError:
        return False
    password = env.get("TTYD_PASSWORD", "")
    parts = (token or "").split(".")
    if len(parts) != 3 or parts[0] != "u1":
        return False
    _, exp_s, sig = parts
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    msg = f"unlock:{exp}".encode("utf-8")
    expect = hmac.new(_key(pin, password), msg, hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(expect, sig)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "usage: session-ticket.py issue <name> [create]|verify <name> <ticket> [create]|check-pin <pin>|sanitize <name>",
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
        if cmd == "check-pin":
            ok = check_pin(argv[2] if len(argv) > 2 else "")
            print("ok" if ok else "fail")
            return 0 if ok else 1
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1
    print("unknown command", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
