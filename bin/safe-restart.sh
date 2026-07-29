#!/usr/bin/env bash
# 安全滚动重启：不杀 tmux；LaunchDaemon 需 sudo
#
# 默认只重启 ttyd + manage（+ healthcheck），绝不碰 cloudflared。
# 该隧道可能与其它服务共用；重启/pkill 隧道会导致全部公网入口中断。
# 仅当明确需要重载隧道配置时：
#   ./bin/safe-restart.sh 8 --with-tunnel
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=common.sh
source "${ROOT}/bin/common.sh"
# shellcheck source=launchd-lib.sh
source "${ROOT}/bin/launchd-lib.sh"

LOG="${ROOT}/logs/safe-restart.log"
DELAY=8
WITH_TUNNEL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-tunnel) WITH_TUNNEL=1 ;;
    -h|--help)
      echo "用法: $0 [delay秒] [--with-tunnel]" >&2
      echo "默认不重启 cloudflared（共用隧道，危险）。" >&2
      exit 0
      ;;
    *)
      if [[ "$1" =~ ^[0-9]+$ ]]; then
        DELAY="$1"
      else
        echo "未知参数: $1" >&2
        echo "用法: $0 [delay秒] [--with-tunnel]" >&2
        exit 1
      fi
      ;;
  esac
  shift
done

mkdir -p "${ROOT}/logs" "${ROOT}/run"

{
  echo "===== $(date '+%Y-%m-%dT%H:%M:%S%z') safe-restart begin (delay=${DELAY}s with_tunnel=${WITH_TUNNEL}) ====="
  sleep "${DELAY}"

  "${ROOT}/bin/ensure-ttyd-index.sh"

  # 默认只清应用相关旧 Agent，绝不 bootout cloudflared
  if [[ "${WITH_TUNNEL}" -eq 1 ]]; then
    remove_old_agents
  else
    remove_old_agents_app_only
  fi

  install_daemon "uk.lucadesign.web-terminal.ttyd"
  sleep 1
  install_daemon "uk.lucadesign.web-terminal.manage"
  sleep 1
  if [[ "${WITH_TUNNEL}" -eq 1 ]]; then
    echo "WARNING: 正在重启 cloudflared（--with-tunnel）；共用该隧道的其它服务会短暂中断"
    install_daemon "uk.lucadesign.web-terminal.cloudflared"
    sleep 1
  else
    echo "跳过 cloudflared（避免中断共用隧道；需要时加 --with-tunnel）"
  fi
  install_daemon "uk.lucadesign.web-terminal.healthcheck"

  sleep 2
  "${ROOT}/bin/status.sh" || true
  echo "===== $(date '+%Y-%m-%dT%H:%M:%S%z') safe-restart done ====="
} >>"${LOG}" 2>&1
