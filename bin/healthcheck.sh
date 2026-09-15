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

# 只处理"自上次检查点之后"新增的日志行，避免历史遗留的错误行反复触发重启。
new_log_lines_since_checkpoint() {
  local logfile="$1"
  local checkpoint_file="$2"
  local total_lines last_pos
  total_lines="$(wc -l <"${logfile}" 2>/dev/null | tr -dc '0-9')"
  total_lines="${total_lines:-0}"
  last_pos="0"
  if [[ -f "${checkpoint_file}" ]]; then
    last_pos="$(cat "${checkpoint_file}" 2>/dev/null | tr -dc '0-9')"
    last_pos="${last_pos:-0}"
  fi
  if [[ "${total_lines}" -lt "${last_pos}" ]]; then
    # 日志被截断/轮转过，从头算
    last_pos=0
  fi
  echo "${total_lines}" >"${checkpoint_file}"
  if [[ "${total_lines}" -gt "${last_pos}" ]]; then
    tail -n "$((total_lines - last_pos))" "${logfile}" 2>/dev/null
  fi
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

# ttyd fd 泄漏检测：pty_spawn 失败（fd 耗尽）会导致前端一直重连刷新。
# 只看"自上次检查点之后新增"的 "Too many open files" 次数（避免历史行反复触发），
# 再叠加当前进程打开的 ptmx fd 数作为信号，达到阈值就主动重启 ttyd
# （不影响已保留的 tmux 会话，会话数据在独立 tmux server 里）。
TTYD_LOG_FILE="${ROOT}/logs/ttyd.log"
TTYD_LOG_CHECKPOINT="${ROOT}/run/ttyd-log-checkpoint"
TTYD_PID="$(pgrep -f '^/opt/homebrew/bin/ttyd .*run-ttyd|bin/ttyd --interface' 2>/dev/null | head -1)"
if [[ -z "${TTYD_PID}" ]]; then
  TTYD_PID="$(pgrep -x ttyd 2>/dev/null | head -1)"
fi

new_fd_errors=0
if [[ -f "${TTYD_LOG_FILE}" ]]; then
  new_fd_errors="$(new_log_lines_since_checkpoint "${TTYD_LOG_FILE}" "${TTYD_LOG_CHECKPOINT}" | grep -c 'Too many open files' || true)"
fi

open_ptmx=0
if [[ -n "${TTYD_PID}" ]]; then
  open_ptmx="$(lsof -p "${TTYD_PID}" 2>/dev/null | grep -c '/dev/ptmx' || true)"
fi

FD_ERROR_THRESHOLD=1
PTMX_THRESHOLD=180

if [[ "${new_fd_errors}" -ge "${FD_ERROR_THRESHOLD}" || "${open_ptmx}" -ge "${PTMX_THRESHOLD}" ]]; then
  log "ttyd fd leak suspected (new_fd_errors=${new_fd_errors}, open_ptmx=${open_ptmx}, pid=${TTYD_PID}) → restart ttyd"
  restart_service "uk.lucadesign.web-terminal.ttyd" '/Users/thomas990p/web-terminal/bin/run-ttyd.sh'
  sleep 2
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
