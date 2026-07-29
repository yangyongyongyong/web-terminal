#!/usr/bin/env bash
# 跑全部防回归测试
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"
python3 tests/test_wheel_regression.py
