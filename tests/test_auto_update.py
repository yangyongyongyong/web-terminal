#!/usr/bin/env python3
"""自动更新/回滚逻辑的单元测试：用临时 git 仓库模拟远端，健康检查用假实现。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from importlib import util

_spec = util.spec_from_file_location(
    "auto_update", Path(__file__).resolve().parent.parent / "bin" / "auto-update.py"
)
assert _spec and _spec.loader
auto_update = util.module_from_spec(_spec)
_spec.loader.exec_module(auto_update)
Updater = auto_update.Updater

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, env=GIT_ENV
    ).stdout.strip()


class FakeUpdater(Updater):
    """健康检查按 HEAD 是否在 healthy 集合里决定；重启只计数。"""

    def __init__(self, root: Path, healthy: set[str], cfg: dict[str, str] | None = None):
        env = {
            "AUTO_UPDATE_ENABLED": "1",
            "AUTO_UPDATE_REMOTE": "origin",
            "AUTO_UPDATE_BRANCH": "main",
            "AUTO_UPDATE_COOLDOWN_MIN": "0",
            "AUTO_UPDATE_MAX_ROLLBACK": "2",
            "AUTO_UPDATE_HEALTH_TIMEOUT": "1",
            "MANAGE_PORT": "1",
            "TTYD_LISTEN_PORT": "1",
        }
        env.update(cfg or {})
        super().__init__(root, env)
        self.healthy = set(healthy)
        self.restart_heads: list[str] = []

    def post_checkout(self) -> None:
        pass

    def restart_services(self) -> bool:
        self.restarts += 1
        self.restart_heads.append(self.rev("HEAD"))
        return True

    def health_ok(self) -> bool:
        return self.rev("HEAD") in self.healthy

    def wait_health(self, timeout=None) -> bool:
        return self.health_ok()


class AutoUpdateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wt-autoupdate-"))
        self.remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.remote)],
                       check=True, capture_output=True, env=GIT_ENV)
        self.seed = self.tmp / "seed"
        self.seed.mkdir()
        git(self.seed, "init", "-b", "main")
        git(self.seed, "remote", "add", "origin", str(self.remote))
        self.v1 = self.commit("v1")
        self.local = self.tmp / "local"
        subprocess.run(["git", "clone", str(self.remote), str(self.local)],
                       check=True, capture_output=True, env=GIT_ENV)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def commit(self, msg: str) -> str:
        (self.seed / "app.txt").write_text(msg + "\n", encoding="utf-8")
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-m", msg)
        git(self.seed, "push", "-q", "origin", "main")
        return git(self.seed, "rev-parse", "HEAD")

    def head(self) -> str:
        return git(self.local, "rev-parse", "HEAD")

    def state(self, up: FakeUpdater) -> dict:
        return json.loads(up.state_file.read_text(encoding="utf-8"))

    def test_no_update(self) -> None:
        up = FakeUpdater(self.local, {self.v1})
        self.assertEqual(up.run(), 0)
        self.assertEqual(up.restarts, 0)
        self.assertEqual(self.head(), self.v1)

    def test_update_success(self) -> None:
        v2 = self.commit("v2")
        up = FakeUpdater(self.local, {self.v1, v2})
        self.assertEqual(up.run(), 0)
        self.assertEqual(self.head(), v2)
        st = self.state(up)
        self.assertEqual(st["last_good"], v2)
        self.assertEqual(st["last_action"], "update")

    def test_failed_update_rolls_back_and_blocks(self) -> None:
        v2 = self.commit("v2")
        up = FakeUpdater(self.local, {self.v1, v2})
        up.run()
        self.assertEqual(self.head(), v2)

        v3 = self.commit("v3")          # 这个版本"坏"了
        up2 = FakeUpdater(self.local, {self.v1, v2})   # v3 不健康
        self.assertEqual(up2.run(), 0)
        self.assertEqual(self.head(), v2, "应回滚到上一个健康版本")
        st = self.state(up2)
        self.assertIn(v3, st["blocked"])
        self.assertFalse(st["paused"])
        self.assertEqual(st["last_action"], "rollback")

        # 远程仍是 v3：第二次运行不能再次升级（防止 1↔2 反复升降级）
        up3 = FakeUpdater(self.local, {self.v1, v2})
        before = up3.restarts
        self.assertEqual(up3.run(), 0)
        self.assertEqual(self.head(), v2)
        self.assertEqual(up3.restarts, before, "黑名单版本不应触发重启/升级")

    def test_rollback_walks_back_until_healthy(self) -> None:
        """最近的候选不健康时，应继续往前回滚，直到找到健康版本。"""
        v2 = self.commit("v2")
        git(self.local, "pull", "-q", "--ff-only")     # 本地先到 v2
        v3 = self.commit("v3")
        v4 = self.commit("v4")
        # 当前版本 v2 和 v3 都不健康，只有更早的 v1 健康 —— 应一路回退到 v1
        up = FakeUpdater(self.local, {self.v1}, {"AUTO_UPDATE_MAX_ROLLBACK": "4"})
        self.assertEqual(up.run(), 0)
        self.assertEqual(self.head(), self.v1, "应回滚到 v1")
        st = self.state(up)
        self.assertEqual(st["last_action"], "rollback")
        self.assertEqual(st["last_good"], self.v1)
        self.assertFalse(st["paused"])
        # 1 次更新尝试 + 3 次回滚尝试
        self.assertEqual(up.restarts, 4)

    def test_all_rollbacks_fail_pauses(self) -> None:
        v2 = self.commit("v2")
        v3 = self.commit("v3")
        up = FakeUpdater(self.local, set())   # 没有任何版本健康
        self.assertEqual(up.run(), 1)
        st = self.state(up)
        self.assertTrue(st["paused"])
        self.assertIn(v3, st["blocked"])
        # 再跑一次应直接跳过
        up2 = FakeUpdater(self.local, set())
        self.assertEqual(up2.run(), 0)
        self.assertEqual(up2.restarts, 0)

    def test_resume_clears_pause_and_blocked(self) -> None:
        v2 = self.commit("v2")
        up = FakeUpdater(self.local, set())
        up.run()
        self.assertTrue(self.state(up)["paused"])
        up2 = FakeUpdater(self.local, set())
        up2.run(resume=True)
        st = self.state(up2)
        self.assertFalse(st["paused"])
        self.assertEqual(st["blocked"], {})

    def test_cooldown_blocks_next_check(self) -> None:
        v2 = self.commit("v2")
        up = FakeUpdater(self.local, {self.v1, v2}, {"AUTO_UPDATE_COOLDOWN_MIN": "30"})
        self.assertEqual(up.run(), 0)
        self.assertEqual(self.head(), v2)
        v3 = self.commit("v3")
        up2 = FakeUpdater(self.local, {self.v1, v2, v3}, {"AUTO_UPDATE_COOLDOWN_MIN": "30"})
        self.assertEqual(up2.run(), 0)
        self.assertEqual(self.head(), v2, "静默期内不应升级")
        self.assertEqual(up2.restarts, 0)

    def test_dirty_worktree_skips(self) -> None:
        self.commit("v2")
        (self.local / "app.txt").write_text("local edit\n", encoding="utf-8")
        up = FakeUpdater(self.local, {self.v1})
        self.assertEqual(up.run(), 0)
        self.assertEqual(self.head(), self.v1)
        self.assertEqual(up.restarts, 0)

    def test_local_ahead_skips(self) -> None:
        (self.local / "local.txt").write_text("local\n", encoding="utf-8")
        git(self.local, "add", "-A")
        git(self.local, "commit", "-m", "local ahead")
        ahead = self.head()
        self.commit("v2")
        up = FakeUpdater(self.local, {ahead})
        self.assertEqual(up.run(), 0)
        self.assertEqual(self.head(), ahead, "本地领先时不能动")

    def test_check_only_reports_without_changing(self) -> None:
        v2 = self.commit("v2")
        up = FakeUpdater(self.local, {self.v1, v2})
        self.assertEqual(up.run(check_only=True), 0)
        self.assertEqual(self.head(), self.v1)
        self.assertEqual(up.restarts, 0)
        self.assertEqual(self.state(up)["last_action"], "check")


if __name__ == "__main__":
    unittest.main(verbosity=2)
