#!/usr/bin/env bash
# LaunchDaemon 安装/卸载公共逻辑（system 域，开机无需图形登录）
# shellcheck disable=SC2034

LAUNCH_LABELS=(
  uk.lucadesign.web-terminal.ttyd
  uk.lucadesign.web-terminal.manage
  uk.lucadesign.web-terminal.cloudflared
  uk.lucadesign.web-terminal.healthcheck
)

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
