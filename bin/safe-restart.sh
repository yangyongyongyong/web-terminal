#!/usr/bin/env bash
# 安全滚动重启：不杀 tmux；LaunchDaemon 需 sudo
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=common.sh
source "${ROOT}/bin/common.sh"
# shellcheck source=launchd-lib.sh
source "${ROOT}/bin/launchd-lib.sh"

LOG="${ROOT}/logs/safe-restart.log"
DELAY="${1:-8}"

mkdir -p "${ROOT}/logs" "${ROOT}/run"

{
  echo "===== $(date '+%Y-%m-%dT%H:%M:%S%z') safe-restart begin (delay=${DELAY}s) ====="
  sleep "${DELAY}"

  "${ROOT}/bin/ensure-ttyd-index.sh"

  remove_old_agents
  install_daemon "uk.lucadesign.web-terminal.ttyd"
  sleep 1
  install_daemon "uk.lucadesign.web-terminal.manage"
  sleep 1
  install_daemon "uk.lucadesign.web-terminal.cloudflared"
  sleep 1
  install_daemon "uk.lucadesign.web-terminal.healthcheck"

  sleep 2
  "${ROOT}/bin/status.sh" || true
  echo "===== $(date '+%Y-%m-%dT%H:%M:%S%z') safe-restart done ====="
} >>"${LOG}" 2>&1
