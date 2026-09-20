#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=common.sh
source "${ROOT}/bin/common.sh"
# shellcheck source=launchd-lib.sh
source "${ROOT}/bin/launchd-lib.sh"

echo "停止 LaunchDaemon（需要管理员密码）"
unload_daemon "${LABEL_HEALTHCHECK}"
unload_daemon "${LABEL_CLOUDFLARED}"
unload_daemon "${LABEL_MANAGE}"
unload_daemon "${LABEL_TTYD}"
remove_old_agents

# 兜底杀掉残留进程（不杀 tmux 会话）
pkill -f "${ROOT}/bin/run-ttyd.sh" 2>/dev/null || true
pkill -f "${ROOT}/bin/run-manage.sh" 2>/dev/null || true
pkill -f "${ROOT}/bin/manage-server.py" 2>/dev/null || true
pkill -f 'cloudflared tunnel --config '"${ROOT}"'/config/cloudflared.yml' 2>/dev/null || true
rm -f "${TTYD_PID_FILE}" "${TUNNEL_PID_FILE}"

echo "tmux 会话 wt-* 已保留（浏览器可下次继续）"
echo "查看: ${ROOT}/bin/session-ctl.sh list"
echo "彻底清会话: ${ROOT}/bin/session-ctl.sh kill <name>"
