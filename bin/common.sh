#!/usr/bin/env bash
# 加载项目 .env 并导出
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT}/.env"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "缺少 ${ENV_FILE}，请先: cp .env.example .env 并修改密码" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

: "${TTYD_USER:?}"
: "${TTYD_PASSWORD:?}"
: "${TTYD_HOST:=127.0.0.1}"
: "${TTYD_PORT:=7681}"
: "${TMUX_SESSION:=web-term}"
: "${PUBLIC_HOST:=term.lucadesign.uk}"
: "${USE_TMUX:=1}"
: "${LOGIN_SHELL:=/bin/zsh}"
: "${MANAGE_HOST:=127.0.0.1}"
: "${MANAGE_PORT:=7690}"
: "${TTYD_BASE_PATH:=/term}"
: "${SESSION_PIN:?请在 .env 配置 SESSION_PIN}"
: "${SESSION_PATH_ROOT:=${HOME}}"
: "${SESSION_DEFAULT_PATH:=${HOME}}"
# 浏览器端保留约 N 页滚动历史（滚轮本地回看）；按 50 行/页估算
: "${SCROLLBACK_PAGES:=30}"

# 注意：不要 export 名为 TUNNEL_* 的变量，会干扰 cloudflared
export ROOT ENV_FILE TTYD_USER TTYD_PASSWORD TTYD_HOST TTYD_PORT TMUX_SESSION PUBLIC_HOST USE_TMUX LOGIN_SHELL
export MANAGE_HOST MANAGE_PORT TTYD_BASE_PATH SESSION_PIN SESSION_PATH_ROOT SESSION_DEFAULT_PATH SCROLLBACK_PAGES
TTYD_PID_FILE="${ROOT}/run/ttyd.pid"
TUNNEL_PID_FILE="${ROOT}/run/cloudflared.pid"
TTYD_LOG="${ROOT}/logs/ttyd.log"
CLOUDFLARED_LOG="${ROOT}/logs/cloudflared.log"
CLOUDFLARED_CONFIG="${ROOT}/config/cloudflared.yml"
export TTYD_PID_FILE TUNNEL_PID_FILE TTYD_LOG CLOUDFLARED_LOG CLOUDFLARED_CONFIG
