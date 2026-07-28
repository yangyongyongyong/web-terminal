#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=common.sh
source "${ROOT}/bin/common.sh"

UID_NUM="$(id -u)"

check_one() {
  local name="$1"
  local state="" domain=""
  if state="$(launchctl print "system/${name}" 2>/dev/null)"; then
    domain="system"
  elif state="$(launchctl print "gui/${UID_NUM}/${name}" 2>/dev/null)"; then
    domain="gui"
  else
    echo "${name}: 未加载"
    return
  fi
  local pid
  pid="$(printf '%s\n' "${state}" | awk '/[[:space:]]pid = /{print $3; exit}')"
  if [[ -n "${pid}" && "${pid}" != "0" ]]; then
    echo "${name}: 运行中 (pid ${pid}, ${domain})"
  else
    echo "${name}: 已加载但无进程 (${domain})"
  fi
}

check_one "uk.lucadesign.web-terminal.ttyd"
check_one "uk.lucadesign.web-terminal.manage"
check_one "uk.lucadesign.web-terminal.cloudflared"
check_one "uk.lucadesign.web-terminal.healthcheck"

echo "--- 会话 ---"
if out="$("${ROOT}/bin/session-ctl.sh" list)" && [[ -n "${out}" ]]; then
  printf '%s\n' "${out}" | while IFS=$'\t' read -r name sid created last_open clients cwd cwd_now; do
    echo "  ${name} (${sid}) clients=${clients:-0} cwd=${cwd:-} now=${cwd_now:-}"
  done
else
  echo "  (无 wt-* 会话)"
fi

if lsof -nP -iTCP:"${TTYD_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "端口 ${TTYD_PORT} (ttyd): 监听中"
else
  echo "端口 ${TTYD_PORT} (ttyd): 未监听"
fi

if lsof -nP -iTCP:"${MANAGE_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "端口 ${MANAGE_PORT} (manage): 监听中"
else
  echo "端口 ${MANAGE_PORT} (manage): 未监听"
fi

echo "公网管理: https://${PUBLIC_HOST}/"
echo "公网终端: https://${PUBLIC_HOST}${TTYD_BASE_PATH}/?arg=main"
echo "本机管理: http://${MANAGE_HOST}:${MANAGE_PORT}/"
echo "本机终端: http://${TTYD_HOST}:${TTYD_PORT}${TTYD_BASE_PATH}/"
echo "自启方式: LaunchDaemon（开机无需图形登录；FileVault 仍需解锁磁盘）"
