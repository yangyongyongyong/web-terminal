#!/usr/bin/env python3
"""公共文件夹（临时公共网盘）单元测试：文件名校验、上传/配额、开关 token。"""
from __future__ import annotations

import importlib.util
import io
import shutil
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("manage_server", ROOT / "bin" / "manage-server.py")
assert _spec and _spec.loader
ms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ms)


class PublicDriveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wt-public-"))
        self._dir = ms.PUBLIC_DIR
        self._state = ms.PUBLIC_STATE_FILE
        self._quota = ms.PUBLIC_QUOTA_BYTES
        self._max = ms.PUBLIC_MAX_BYTES
        self._limit_default = ms.PUBLIC_LIMIT_DEFAULT
        ms.PUBLIC_DIR = self.tmp / "drive"
        ms.PUBLIC_STATE_FILE = self.tmp / "state.json"
        ms.PUBLIC_QUOTA_BYTES = 100
        ms.PUBLIC_MAX_BYTES = 60
        ms.PUBLIC_LIMIT_DEFAULT = 0

    def tearDown(self) -> None:
        ms.PUBLIC_DIR = self._dir
        ms.PUBLIC_STATE_FILE = self._state
        ms.PUBLIC_QUOTA_BYTES = self._quota
        ms.PUBLIC_MAX_BYTES = self._max
        ms.PUBLIC_LIMIT_DEFAULT = self._limit_default
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_name_validation(self) -> None:
        for good in ("a.txt", "报告 2026.pdf", "x"):
            self.assertTrue(ms.public_name_ok(good), good)
        for bad in ("", ".", "..", "../etc/passwd", "a/b.txt", ".hidden", "a\\b", "a\x00b"):
            self.assertFalse(ms.public_name_ok(bad), repr(bad))

    def test_upload_list_download_size(self) -> None:
        dest, written = ms.public_save_stream("a.txt", 5, io.BytesIO(b"hello"))
        self.assertEqual(written, 5)
        self.assertEqual(dest.read_bytes(), b"hello")
        files = ms.public_files()
        self.assertEqual([f["name"] for f in files], ["a.txt"])
        self.assertEqual(ms.public_usage(), 5)

    def test_same_name_gets_suffix(self) -> None:
        ms.public_save_stream("a.txt", 1, io.BytesIO(b"1"))
        dest, _ = ms.public_save_stream("a.txt", 1, io.BytesIO(b"2"))
        self.assertEqual(dest.name, "a (1).txt")
        self.assertEqual(sorted(f["name"] for f in ms.public_files()), ["a (1).txt", "a.txt"])

    def test_max_file_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ms.public_save_stream("big.bin", 61, io.BytesIO(b"x" * 61))

    def test_quota_rejected_and_temp_cleaned(self) -> None:
        ms.public_save_stream("a.bin", 60, io.BytesIO(b"a" * 60))
        with self.assertRaises(PermissionError):
            ms.public_save_stream("b.bin", 60, io.BytesIO(b"b" * 60))
        self.assertEqual([f["name"] for f in ms.public_files()], ["a.bin"])
        self.assertEqual(list((self.tmp / "drive").glob(".upload-*")), [], "临时文件应清理")

    def test_delete(self) -> None:
        ms.public_save_stream("a.txt", 1, io.BytesIO(b"x"))
        self.assertTrue(ms.public_delete("a.txt"))
        self.assertFalse(ms.public_delete("a.txt"))
        with self.assertRaises(ValueError):
            ms.public_delete("../x")

    def test_toggle_token_lifecycle(self) -> None:
        ms.public_state_save({"enabled": True, "token": "a" * 32, "updated": "", "by": "u"})
        self.assertEqual(ms.public_open_token(), "a" * 32)
        ms.public_state_save({"enabled": False, "token": "", "updated": "", "by": "u"})
        self.assertEqual(ms.public_open_token(), "")
        # 非法 token 不生效
        ms.public_state_save({"enabled": True, "token": "bad token!", "updated": "", "by": "u"})
        self.assertEqual(ms.public_open_token(), "")

    def test_traffic_count_and_limit(self) -> None:
        ms.public_traffic_add(up=100, down=50)
        t = ms.public_traffic()
        self.assertEqual((t["up"], t["down"], t["total"]), (100, 50, 150))
        ms.public_set_limit(200)
        self.assertFalse(ms.public_traffic_limited())
        ms.public_traffic_add(down=100)
        self.assertTrue(ms.public_traffic_limited(), "总流量达到上限后应暂停公网访问")
        ms.public_set_limit(0)   # 0 = 不限制
        self.assertFalse(ms.public_traffic_limited())

    def test_traffic_month_rollover(self) -> None:
        st = ms.public_state_load()
        st.update({"month": "2000-01", "up_bytes": 10, "down_bytes": 20})
        ms.public_state_save(st)
        t = ms.public_traffic()
        self.assertEqual(t["total"], 0, "跨月应清零")
        self.assertEqual(t["month"], time.strftime("%Y-%m"))

    def test_limit_validation(self) -> None:
        with self.assertRaises(ValueError):
            ms.public_set_limit(-1)
        with self.assertRaises(ValueError):
            ms.public_set_limit(1024 ** 4 + 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
