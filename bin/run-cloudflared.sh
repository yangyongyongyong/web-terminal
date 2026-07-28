#!/usr/bin/env bash
# LaunchAgent / 手动启动用：exec cloudflared
# 周期 502/530：多为小火箭 TUN 弄断 QUIC；靠重试 + healthcheck 自动拉起
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export HOME="${HOME:-/Users/thomas990p}"
export USER="${USER:-thomas990p}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${ROOT}/config/cloudflared.yml"
METRICS_ADDR="127.0.0.1:20242"

unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy \
  FTP_PROXY ftp_proxy SOCKS_PROXY SOCKS5_PROXY socks_proxy socks5_proxy \
  TUNNEL_LOG TUNNEL_PID_FILE TUNNEL_TOKEN 2>/dev/null || true
export NO_PROXY='*'
export no_proxy='*'

exec /opt/homebrew/bin/cloudflared tunnel \
  --config "${CONFIG}" \
  --no-autoupdate \
  --no-prechecks \
  --edge-ip-version 4 \
  --retries 15 \
  --grace-period 10s \
  --metrics "${METRICS_ADDR}" \
  run \
  --dns-resolver-addrs 1.1.1.1:53 \
  --dns-resolver-addrs 1.0.0.1:53 \
  --dns-resolver-addrs 8.8.8.8:53
