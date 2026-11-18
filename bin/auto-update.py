#!/usr/bin/env python3
"""自动更新 + 异常回滚（配合 web-terminal-autoupdate.timer 定期运行）。

流程：
  fetch 远程分支 → 有新提交？
    → 检查静默期 / 黑名单 / 暂停标记 / 工作区是否干净
    → git merge --ff-only 拉取 → 重启服务 → 健康检查
    → 健康则记录 last_good；不健康则回滚到上一个版本（最多 N 个），每次回滚后重新健康检查

防「版本 1↔2 反复升降级」：
  - 健康检查失败的提交写入 blocked，之后不再尝试，直到远程出现更新的提交或人工 --resume
  - 每次更新/回滚后进入静默期（AUTO_UPDATE_COOLDOWN_MIN），期间不再动作
  - 回滚候选全部失败时置 paused，自动更新停止，等待人工处理

状态：run/auto-update-state.json（忽略文件，不落库）
日志：logs/auto-update.log
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env(root: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    f = root / ".env"
    if not f.exists():
        return env
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def _truthy(v: str) -> bool:
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


def _now_iso() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


class Updater:
    """自动更新器。restart_services/health_ok/wait_health 可在测试中替换。"""

    def __init__(self, root: Path = ROOT, env: dict[str, str] | None = None):
        self.root = Path(root).resolve()
        self.env = dict(env if env is not None else _load_env(self.root))
        self.log_file = self.root / "logs" / "auto-update.log"
        self.state_file = self.root / "run" / "auto-update-state.json"
        self.lock_file = self.root / "run" / "auto-update.lock"

        self.remote = self.cfg("AUTO_UPDATE_REMOTE", "origin")
        self.branch = self.cfg("AUTO_UPDATE_BRANCH", "main")
        self.cooldown_sec = self._int_cfg("AUTO_UPDATE_COOLDOWN_MIN", 30) * 60
        self.max_rollback = max(1, self._int_cfg("AUTO_UPDATE_MAX_ROLLBACK", 5))
        self.health_timeout = max(5, self._int_cfg("AUTO_UPDATE_HEALTH_TIMEOUT", 90))
        self.enabled = _truthy(self.cfg("AUTO_UPDATE_ENABLED", "1"))
        self.manage_port = self.cfg("MANAGE_PORT", "7690")
        self.ttyd_port = self.cfg("TTYD_LISTEN_PORT") or self.cfg("TTYD_PORT", "7681")
        self.ttyd_base = (self.cfg("TTYD_BASE_PATH", "/term") or "/term").rstrip("/")

        # 测试可替换：restart_services / health_ok / wait_health / post_checkout
        self.restarts = 0

    # ---- 配置 / 日志 / 状态 ----
    def cfg(self, key: str, default: str = "") -> str:
        return str(os.environ.get(key) or self.env.get(key) or default)

    def _int_cfg(self, key: str, default: int) -> int:
        try:
            return int(self.cfg(key, str(default)))
        except ValueError:
            return default

    def log(self, msg: str) -> None:
        line = f"{_now_iso()} {msg}"
        print(line, flush=True)
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def load_state(self) -> dict:
        try:
            st = json.loads(self.state_file.read_text(encoding="utf-8"))
            if not isinstance(st, dict):
                raise ValueError
        except (OSError, json.JSONDecodeError, ValueError):
            st = {}
        st.setdefault("version", 1)
        st.setdefault("last_check", "")
        st.setdefault("current", "")
        st.setdefault("last_good", "")
        st.setdefault("cooldown_until", 0)
        st.setdefault("paused", False)
        st.setdefault("blocked", {})
        st.setdefault("history", [])
        return st

    def save_state(self, st: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        hist = st.get("history") or []
        st["history"] = hist[-50:]
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, self.state_file)

    def record(self, st: dict, action: str, frm: str = "", to: str = "", note: str = "") -> None:
        st["last_action"] = action
        st["last_action_at"] = _now_iso()
        st.setdefault("history", []).append(
            {"at": _now_iso(), "action": action, "from": frm, "to": to, "note": note}
        )

    # ---- git ----
    def git(self, *args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=str(self.root), capture_output=True, text=True, timeout=timeout
        )

    def rev(self, ref: str) -> str:
        cp = self.git("rev-parse", ref)
        return cp.stdout.strip() if cp.returncode == 0 else ""

    def is_ancestor(self, a: str, b: str) -> bool:
        if not a or not b:
            return False
        return self.git("merge-base", "--is-ancestor", a, b).returncode == 0

    def is_dirty(self) -> bool:
        cp = self.git("status", "--porcelain", "--untracked-files=no")
        return bool(cp.stdout.strip())

    # ---- 服务 ----
    def has_systemd(self) -> bool:
        return shutil.which("systemctl") is not None and Path("/run/systemd/system").is_dir()

    def restart_services(self) -> bool:
        self.restarts += 1
        if self.has_systemd():
            cp = subprocess.run(
                ["systemctl", "restart", "web-terminal-ttyd.service", "web-terminal-manage.service"],
                capture_output=True, text=True, timeout=180,
            )
            if cp.returncode != 0:
                self.log(f"重启失败: {(cp.stderr or cp.stdout).strip()}")
                return False
            return True
        safe = self.root / "bin" / "safe-restart.sh"
        if safe.exists():
            cp = subprocess.run(["bash", str(safe), "0"], capture_output=True, text=True, timeout=180)
            return cp.returncode == 0
        self.log("没有 systemd 也没有 bin/safe-restart.sh，无法重启")
        return False

    def health_urls(self) -> list[str]:
        return [
            f"http://127.0.0.1:{self.manage_port}/",
            f"http://127.0.0.1:{self.ttyd_port}{self.ttyd_base}/",
        ]

    def health_ok(self) -> bool:
        if self.has_systemd():
            for unit in ("web-terminal-manage.service", "web-terminal-ttyd.service"):
                cp = subprocess.run(["systemctl", "is-active", "--quiet", unit], timeout=15)
                if cp.returncode != 0:
                    return False
        for url in self.health_urls():
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    if not (200 <= resp.status < 400):
                        return False
            except Exception:
                return False
        return True

    def wait_health(self, timeout: int | None = None) -> bool:
        deadline = time.monotonic() + (timeout if timeout is not None else self.health_timeout)
        while time.monotonic() < deadline:
            if self.health_ok():
                return True
            time.sleep(2)
        return False

    def post_checkout(self) -> None:
        """拉取/回滚后同步仓库内的 systemd drop-in（幂等）。"""
        if not self.has_systemd() or os.geteuid() != 0:
            return
        inst = self.root / "bin" / "install-systemd-dropins.sh"
        if not inst.exists():
            return
        try:
            cp = subprocess.run(["bash", str(inst)], capture_output=True, text=True, timeout=120)
            if cp.returncode != 0:
                self.log(f"同步 systemd drop-in 失败: {(cp.stderr or cp.stdout).strip()}")
        except (OSError, subprocess.TimeoutExpired) as e:
            self.log(f"同步 systemd drop-in 异常: {e}")

    # ---- 主流程 ----
    def run(self, force: bool = False, check_only: bool = False, resume: bool = False) -> int:
        st = self.load_state()
        st["last_check"] = _now_iso()
        st["current"] = self.rev("HEAD")

        if resume:
            st["paused"] = False
            st["blocked"] = {}
            st["cooldown_until"] = 0
            self.log("已人工恢复：清除暂停、黑名单和静默期；下次检查会重新尝试")
            self.record(st, "resume", st["current"], st["current"])
            # 本次只做恢复，不立刻重试：避免人工刚恢复就再次失败并回到 paused
            self.save_state(st)
            return 0
        if not self.enabled and not force:
            self.log("AUTO_UPDATE_ENABLED=0，跳过（可用 --force 手动执行）")
            self.save_state(st)
            return 0
        if st.get("paused") and not force:
            self.log("自动更新处于暂停状态（上次回滚全部失败）；人工处理后再 --resume")
            self.save_state(st)
            return 0
        now = int(time.time())
        if not force and now < int(st.get("cooldown_until") or 0):
            left = int(st["cooldown_until"]) - now
            self.log(f"静默期内（剩余 {left // 60} 分 {left % 60} 秒），跳过")
            self.save_state(st)
            return 0
        if self.is_dirty():
            self.log("工作区有未提交改动，跳过（避免把本地改动冲掉）")
            self.save_state(st)
            return 0

        cp = self.git("fetch", "--prune", self.remote, self.branch, timeout=180)
        if cp.returncode != 0:
            self.log(f"git fetch 失败: {(cp.stderr or cp.stdout).strip()}")
            self.save_state(st)
            return 0
        remote_ref = f"{self.remote}/{self.branch}"
        remote_sha = self.rev(remote_ref)
        head = self.rev("HEAD")
        if not remote_sha:
            self.log(f"远端分支不存在: {remote_ref}")
            self.save_state(st)
            return 0

        if head == remote_sha:
            self.log(f"已是最新 {head[:9]}")
            self.save_state(st)
            return 0
        if remote_sha in (st.get("blocked") or {}):
            self.log(f"远端 {remote_sha[:9]} 在失败黑名单中，跳过（等待更新提交或 --resume）")
            self.save_state(st)
            return 0
        if not self.is_ancestor(head, remote_sha):
            self.log(f"本地 {head[:9]} 不是远端 {remote_sha[:9]} 的祖先（有未推送提交或已分叉），跳过")
            self.save_state(st)
            return 0

        self.log(f"发现更新: {head[:9]} → {remote_sha[:9]}")
        if check_only:
            self.record(st, "check", head, remote_sha, "有可用更新")
            self.save_state(st)
            return 0

        # 拉取并验证
        cp = self.git("merge", "--ff-only", remote_ref)
        if cp.returncode != 0:
            self.log(f"快进合并失败: {(cp.stderr or cp.stdout).strip()}")
            self.save_state(st)
            return 1
        self.post_checkout()
        self.log("已拉取，重启服务…")
        ok = self.restart_services() and self.wait_health()
        if ok:
            st["last_good"] = remote_sha
            st["current"] = remote_sha
            st["cooldown_until"] = int(time.time()) + self.cooldown_sec
            self.record(st, "update", head, remote_sha, "更新成功")
            self.log(f"更新成功 {remote_sha[:9]}，静默 {self.cooldown_sec // 60} 分钟")
            self.save_state(st)
            return 0

        self.log(f"更新到 {remote_sha[:9]} 后健康检查失败，开始回滚")
        st.setdefault("blocked", {})[remote_sha] = {"at": _now_iso(), "reason": "健康检查失败"}
        self.record(st, "block", head, remote_sha, "健康检查失败")
        rolled = self._rollback(st, remote_sha)
        self.save_state(st)
        if rolled:
            return 0
        st["paused"] = True
        self.record(st, "pause", remote_sha, st.get("current", ""), "所有回滚候选均失败")
        self.save_state(st)
        self.log("回滚候选全部失败，自动更新已暂停，请人工介入（修复后 --resume）")
        return 1

    def _rollback(self, st: dict, bad_sha: str) -> bool:
        """按 last_good → bad~1 → bad~2 … 顺序回滚，最多 max_rollback 次。"""
        candidates: list[str] = []
        last_good = str(st.get("last_good") or "")
        if last_good and self.is_ancestor(last_good, bad_sha):
            candidates.append(last_good)
        cp = self.git("rev-list", "--first-parent", f"--max-count={self.max_rollback}", f"{bad_sha}^")
        if cp.returncode == 0:
            for sha in cp.stdout.split():
                if sha not in candidates and sha != bad_sha:
                    candidates.append(sha)
        candidates = candidates[: self.max_rollback]
        if not candidates:
            self.log("没有可回滚的历史版本")
            return False

        for idx, sha in enumerate(candidates, 1):
            self.log(f"回滚尝试 {idx}/{len(candidates)}: → {sha[:9]}")
            if self.is_dirty():
                self.log("工作区不干净，放弃回滚")
                return False
            cp = self.git("reset", "--hard", sha)
            if cp.returncode != 0:
                self.log(f"git reset 失败: {cp.stderr.strip()}")
                continue
            self.post_checkout()
            if not self.restart_services():
                continue
            if not self.wait_health():
                self.log(f"回滚到 {sha[:9]} 后仍不健康，继续向前找")
                continue
            st["current"] = sha
            st["last_good"] = sha
            st["cooldown_until"] = int(time.time()) + self.cooldown_sec
            self.record(st, "rollback", bad_sha, sha, f"回滚成功（第 {idx} 个候选）")
            self.log(f"回滚成功，当前 {sha[:9]}，静默 {self.cooldown_sec // 60} 分钟")
            return True
        return False


def main(argv: list[str]) -> int:
    force = check_only = resume = False
    for a in argv[1:]:
        if a == "--force":
            force = True
        elif a == "--check":
            check_only = True
        elif a == "--resume":
            resume = True
        elif a in ("-h", "--help"):
            print(__doc__.strip())
            print("\n参数: --check 只检查并报告；--force 忽略暂停/静默期（仍遵守黑名单）；--resume 清除暂停、黑名单与静默期")
            return 0
        else:
            print(f"未知参数: {a}", file=sys.stderr)
            return 2

    # 同一时间只允许一个实例；用文件锁避免 timer 与手动执行叠加
    lock = (ROOT / "run" / "auto-update.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = lock.open("w")
    try:
        import fcntl
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, BlockingIOError, OSError):
        print("另一个自动更新进程正在运行，跳过")
        lock_fh.close()
        return 0
    try:
        return Updater().run(force=force, check_only=check_only, resume=resume)
    finally:
        try:
            import fcntl
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        lock_fh.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
