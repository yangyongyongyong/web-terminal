#!/usr/bin/env bash
# 隧道/源站健康检查：ttyd、manage、cloudflared ready
# LaunchDaemon 下用 pkill + KeepAlive 拉起（无需 sudo kickstart）
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export HOME="${HOME:-/Users/thomas990p}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=common.sh
source "${ROOT}/bin/common.sh" 2>/dev/null || true

LOG="${ROOT}/logs/healthcheck.log"
METRICS_READY="http://127.0.0.1:20242/ready"
ORIGIN="http://127.0.0.1:${TTYD_PORT:-7681}${TTYD_BASE_PATH:-/term}/"
MANAGE="http://127.0.0.1:${MANAGE_PORT:-7690}/api/health"
FAIL_FILE="${ROOT}/run/tunnel-fail-count"

mkdir -p "${ROOT}/logs" "${ROOT}/run"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >>"${LOG}"
}

fail_count() {
  if [[ -f "${FAIL_FILE}" ]]; then
    cat "${FAIL_FILE}"
  else
    echo 0
  fi
}

bump_fail() {
  local n
  n="$(fail_count)"
  echo $((n + 1)) >"${FAIL_FILE}"
}

reset_fail() {
  echo 0 >"${FAIL_FILE}"
}

# KeepAlive 会把进程拉回；优先 system kickstart，失败则 pkill
restart_service() {
  local name="$1"
  local pattern="$2"
  log "restart ${name}"
  if launchctl kickstart -k "system/${name}" 2>>"${LOG}"; then
    return 0
  fi
  pkill -f "${pattern}" 2>>"${LOG}" || true
}

# manage 挂了就拉起（不影响已有 tmux）
if ! curl -s -o /dev/null --connect-timeout 2 --max-time 3 -u "${TTYD_USER}:${TTYD_PASSWORD}" "${MANAGE}"; then
  log "manage down → restart manage"
  restart_service "uk.lucadesign.web-terminal.manage" '/Users/thomas990p/web-terminal/bin/manage-server.py'
  sleep 1
fi

if ! curl -s -o /dev/null --connect-timeout 2 --max-time 3 "${ORIGIN}"; then
  code="$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 2 --max-time 3 "${ORIGIN}" || echo 000)"
  if [[ "${code}" == "000" ]]; then
    log "origin ttyd down → restart ttyd"
    restart_service "uk.lucadesign.web-terminal.ttyd" '/Users/thomas990p/web-terminal/bin/run-ttyd.sh'
    sleep 2
  fi
fi

ready_json="$(curl -s --connect-timeout 2 --max-time 3 "${METRICS_READY}" 2>/dev/null || true)"
ready_n="$(printf '%s' "${ready_json}" | sed -n 's/.*"readyConnections":\([0-9][0-9]*\).*/\1/p')"
ready_n="${ready_n:-0}"

if [[ "${ready_n}" -ge 1 ]]; then
  reset_fail
  exit 0
fi

bump_fail
n="$(fail_count)"
log "tunnel not ready (readyConnections=${ready_n}, fail=${n}) body=${ready_json}"

if [[ "${n}" -ge 1 ]]; then
  log "restarting cloudflared after failure"
  restart_service "uk.lucadesign.web-terminal.cloudflared" 'cloudflared tunnel --config /Users/thomas990p/web-terminal/config/cloudflared.yml'
  reset_fail
fi
