#!/usr/bin/env bash
# LaunchDaemon 安装/卸载公共逻辑（system 域，开机无需图形登录）
# shellcheck disable=SC2034

# label 前缀由本地渲染的真实 plist 文件名决定（bin/render-plists.sh 生成；
# 本机值记录在 LOCAL.md，不入库）。缺省回退到通用名。
_plist_prefix() {
  local first
  first="$(ls "${ROOT}/config/"*.web-terminal.ttyd.plist 2>/dev/null | head -1)"
  if [[ -n "${first}" ]]; then
    basename "${first}" .plist | sed 's/\.ttyd$//'
  else
    echo "local.web-terminal"
  fi
}
LAUNCH_PREFIX="$(_plist_prefix)"
LABEL_TTYD="${LAUNCH_PREFIX}.ttyd"
LABEL_MANAGE="${LAUNCH_PREFIX}.manage"
LABEL_CLOUDFLARED="${LAUNCH_PREFIX}.cloudflared"
LABEL_HEALTHCHECK="${LAUNCH_PREFIX}.healthcheck"
LAUNCH_LABELS=("${LABEL_TTYD}" "${LABEL_MANAGE}" "${LABEL_CLOUDFLARED}" "${LABEL_HEALTHCHECK}")

DAEMON_DIR="/Library/LaunchDaemons"
AGENT_DIR="${HOME}/Library/LaunchAgents"
SYSTEM_DOMAIN="system"

remove_old_agents() {
  local name uid_num
  uid_num="$(id -u)"
  for name in "${LAUNCH_LABELS[@]}"; do
    if launchctl print "gui/${uid_num}/${name}" &>/dev/null; then
      launchctl bootout "gui/${uid_num}/${name}" 2>/dev/null || true
      echo "已移除旧 LaunchAgent: ${name}"
    fi
    rm -f "${AGENT_DIR}/${name}.plist"
  done
}

# 只清 ttyd/manage/healthcheck 的旧 Agent，默认不动 cloudflared（可能共用隧道）
remove_old_agents_app_only() {
  local name uid_num
  uid_num="$(id -u)"
  for name in \
    ${LABEL_TTYD} \
    ${LABEL_MANAGE} \
    ${LABEL_HEALTHCHECK}
  do
    if launchctl print "gui/${uid_num}/${name}" &>/dev/null; then
      launchctl bootout "gui/${uid_num}/${name}" 2>/dev/null || true
      echo "已移除旧 LaunchAgent: ${name}"
    fi
    rm -f "${AGENT_DIR}/${name}.plist"
  done
}

install_daemon() {
  local name="$1"
  local src="${ROOT}/config/${name}.plist"
  local dst="${DAEMON_DIR}/${name}.plist"

  sudo cp "${src}" "${dst}"
  sudo chown root:wheel "${dst}"
  sudo chmod 644 "${dst}"

  if sudo launchctl print "${SYSTEM_DOMAIN}/${name}" &>/dev/null; then
    sudo launchctl bootout "${SYSTEM_DOMAIN}/${name}" 2>/dev/null || true
    sleep 1
  fi

  if ! sudo launchctl bootstrap "${SYSTEM_DOMAIN}" "${dst}" 2>/dev/null; then
    sleep 1
    sudo launchctl bootstrap "${SYSTEM_DOMAIN}" "${dst}"
  fi
  sudo launchctl enable "${SYSTEM_DOMAIN}/${name}" 2>/dev/null || true
  sudo launchctl kickstart -k "${SYSTEM_DOMAIN}/${name}" 2>/dev/null || true
  echo "已启动 LaunchDaemon: ${name}"
}

unload_daemon() {
  local name="$1"
  local dst="${DAEMON_DIR}/${name}.plist"
  if sudo launchctl print "${SYSTEM_DOMAIN}/${name}" &>/dev/null; then
    sudo launchctl bootout "${SYSTEM_DOMAIN}/${name}" 2>/dev/null || true
    echo "已停止: ${name}"
  else
    echo "未加载: ${name}"
  fi
  sudo rm -f "${dst}"
  rm -f "${AGENT_DIR}/${name}.plist"
}
