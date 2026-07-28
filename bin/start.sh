#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=common.sh
source "${ROOT}/bin/common.sh"
# shellcheck source=launchd-lib.sh
source "${ROOT}/bin/launchd-lib.sh"

mkdir -p "${ROOT}/run" "${ROOT}/logs" "${ROOT}/run/sessions" "${ROOT}/web"

# 预生成 ttyd 自定义页
"${ROOT}/bin/ensure-ttyd-index.sh"

echo "将安装到 /Library/LaunchDaemons（开机无需登录）"
echo "请在本机「终端.app」执行本脚本以便输入管理员密码；Cursor 内无法交互 sudo。"
if ! sudo -n true 2>/dev/null; then
  if [[ ! -t 0 ]]; then
    echo "错误: 当前环境无法弹出 sudo 密码框。" >&2
    echo "请打开「终端」运行:  cd ~/web-terminal && ./bin/start.sh" >&2
    exit 1
  fi
fi

remove_old_agents

install_daemon "uk.lucadesign.web-terminal.ttyd"
install_daemon "uk.lucadesign.web-terminal.manage"
install_daemon "uk.lucadesign.web-terminal.cloudflared"
install_daemon "uk.lucadesign.web-terminal.healthcheck"

echo
echo "管理页:    https://${PUBLIC_HOST}/"
echo "终端:      https://${PUBLIC_HOST}/term/?arg=main"
echo "本机管理:  http://${MANAGE_HOST}:${MANAGE_PORT}/"
echo "本机终端:  http://${TTYD_HOST}:${TTYD_PORT}${TTYD_BASE_PATH}/"
echo "账号:      ${TTYD_USER}"
echo "密码:      见 ${ROOT}/.env"
echo
echo "已改为 LaunchDaemon：开机 / 通电后无需手动登录也会启动（以用户 ${USER} 运行）"
echo "若开启了 FileVault，仍需有人完成一次磁盘解锁后系统才能起来"
echo "断线后会话保留在 tmux（wt-*）；整机重启后 tmux 会话会丢"
