#!/usr/bin/env bash
# 安装/卸载自动更新定时器（systemd）。
#   ./bin/install-autoupdate.sh            安装并启用 timer（每 AUTO_UPDATE_INTERVAL_MIN 分钟检查一次）
#   ./bin/install-autoupdate.sh --disable  停止并卸载 timer
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=common.sh
source "${ROOT}/bin/common.sh"

SRC="${ROOT}/config/systemd"
DST="/etc/systemd/system"
SVC="web-terminal-autoupdate.service"
TIMER="web-terminal-autoupdate.timer"
TMPL_SVC="${SRC}/${SVC}.template"
TMPL_TIMER="${SRC}/${TIMER}.template"

DISABLE=0
for a in "$@"; do
  case "$a" in
    --disable) DISABLE=1 ;;
    -h|--help)
      sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "未知参数: $a" >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "需要 root（写 ${DST}）" >&2
  exit 1
fi
if ! command -v systemctl >/dev/null 2>&1 || [[ ! -d /run/systemd/system ]]; then
  echo "当前不是 systemd 环境；macOS 请用 launchd（见 README）" >&2
  exit 1
fi

if [[ "${DISABLE}" -eq 1 ]]; then
  systemctl disable --now "${TIMER}" 2>/dev/null || true
  rm -f "${DST}/${SVC}" "${DST}/${TIMER}"
  systemctl daemon-reload
  echo "已卸载自动更新定时器"
  exit 0
fi

if [[ ! -f "${TMPL_SVC}" || ! -f "${TMPL_TIMER}" ]]; then
  echo "缺少模板: ${TMPL_SVC} / ${TMPL_TIMER}" >&2
  exit 1
fi

PYTHON_BIN="$(command -v python3)"
INTERVAL="${AUTO_UPDATE_INTERVAL_MIN:-15}"
if ! [[ "${INTERVAL}" =~ ^[0-9]+$ ]] || [[ "${INTERVAL}" -lt 1 ]]; then
  echo "AUTO_UPDATE_INTERVAL_MIN 必须是 >=1 的整数: ${INTERVAL}" >&2
  exit 1
fi

render() {
  sed -e "s|__ROOT__|${ROOT}|g" \
      -e "s|__PYTHON__|${PYTHON_BIN}|g" \
      -e "s|__INTERVAL__|${INTERVAL}|g" "$1"
}

render "${TMPL_SVC}" >"${DST}/${SVC}"
render "${TMPL_TIMER}" >"${DST}/${TIMER}"
chmod 0644 "${DST}/${SVC}" "${DST}/${TIMER}"

systemctl daemon-reload
systemctl enable --now "${TIMER}"

echo "已安装并启用："
echo "  ${SVC}"
echo "  ${TIMER}（每 ${INTERVAL} 分钟检查一次）"
echo
systemctl list-timers "${TIMER}" --no-pager || true
echo
echo "手动执行一次：  ${ROOT}/bin/auto-update.py --check"
echo "查看状态：      cat ${ROOT}/run/auto-update-state.json"
echo "查看日志：      tail -f ${ROOT}/logs/auto-update.log"
echo "卸载：          ${ROOT}/bin/install-autoupdate.sh --disable"
