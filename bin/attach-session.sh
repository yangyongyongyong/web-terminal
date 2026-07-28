#!/usr/bin/env bash
# ttyd --url-arg 入口：attach-session.sh <name> [create] <ticket>
# 必须带有效 PIN 票据；断线 SIGHUP 仅 detach，会话保留
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export HOME="${HOME:-/Users/thomas990p}"
export USER="${USER:-thomas990p}"
export LANG="${LANG:-zh_CN.UTF-8}"
export LC_ALL="${LC_ALL:-zh_CN.UTF-8}"
export TERM_PROGRAM="${TERM_PROGRAM:-ttyd}"
export COLORTERM="${COLORTERM:-truecolor}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=common.sh
source "${ROOT}/bin/common.sh"

TMUX_BIN="$(command -v tmux)"
TMUX_CONF="${ROOT}/config/tmux.web.conf"
SHELL_BIN="${LOGIN_SHELL:-/bin/zsh}"
META_DIR="${ROOT}/run/sessions"
TICKET_PY="${ROOT}/bin/session-ticket.py"

mkdir -p "${META_DIR}" "${ROOT}/run" "${ROOT}/logs"

raw_name="${1:-}"
shift || true

mode=""
ticket=""
for a in "$@"; do
  if [[ "${a}" == "create" ]]; then
    mode="create"
  elif [[ "${a}" == t1.* ]]; then
    ticket="${a}"
  fi
done

deny() {
  printf '\n[web-terminal] %s\n' "$1"
  printf '请打开管理页并输入 PIN 后再进入：https://%s/\n\n' "${PUBLIC_HOST}"
  printf '按 Ctrl-C 关闭此页…\n'
  sleep 3600
  exit 1
}

if [[ -z "${raw_name}" || "${raw_name}" == t1.* ]]; then
  deny "缺少会话名或 PIN 票据无效"
fi

name="$(/opt/homebrew/bin/python3 "${TICKET_PY}" sanitize "${raw_name}")"
if [[ -z "${name}" ]]; then
  name="main"
fi
session="wt-${name}"

if [[ -z "${ticket}" ]]; then
  deny "需要 PIN 票据才能进入会话（Chrome 保存的登录密码不够）"
fi

verify_args=("${TICKET_PY}" verify "${name}" "${ticket}")
if [[ "${mode}" == "create" ]]; then
  verify_args+=("create")
fi
if ! /opt/homebrew/bin/python3 "${verify_args[@]}" >/dev/null; then
  deny "PIN 票据无效或已过期"
fi

touch_meta() {
  local key="$1"
  local val="$2"
  printf '%s\n' "${val}" >"${META_DIR}/${session}.${key}"
}

if ! "${TMUX_BIN}" has-session -t "${session}" 2>/dev/null; then
  if [[ "${mode}" == "create" ]]; then
    "${TMUX_BIN}" -f "${TMUX_CONF}" new-session -d -s "${session}" -x 220 -y 60 "${SHELL_BIN}" -l
    touch_meta created "$(date '+%Y-%m-%dT%H:%M:%S%z')"
    touch_meta title "${name}"
  else
    deny "会话「${name}」不存在或已停止"
  fi
fi

touch_meta last_open "$(date '+%Y-%m-%dT%H:%M:%S%z')"
exec "${TMUX_BIN}" -f "${TMUX_CONF}" attach-session -t "${session}"
