#!/usr/bin/env bash
# LaunchAgent：会话管理 HTTP 服务
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export HOME="${HOME:-/Users/thomas990p}"
export USER="${USER:-thomas990p}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "${ROOT}/logs" "${ROOT}/run"

exec /opt/homebrew/bin/python3 "${ROOT}/bin/manage-server.py"
