#!/usr/bin/env bash
# 跑全部防回归测试
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"
python3 tests/test_wheel_regression.py
python3 tests/test_paste_image.py
python3 tests/test_auto_update.py
python3 tests/test_public_drive.py
