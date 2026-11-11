#!/usr/bin/env bash
# 把 config/systemd/ 下的 drop-in 安装到 /etc/systemd/system/ 并 daemon-reload。
# 幂等；不重启服务（KillMode/OOMPolicy 在下次 stop/OOM 时才被读取，无需重启）。
#   ./bin/install-systemd-dropins.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${ROOT}/config/systemd"
DST="/etc/systemd/system"

if [[ $EUID -ne 0 ]]; then
  echo "需要 root（写 ${DST}）" >&2
  exit 1
fi
if [[ ! -d "${SRC}" ]]; then
  echo "缺少 ${SRC}" >&2
  exit 1
fi

changed=0
while IFS= read -r -d '' f; do
  rel="${f#"${SRC}/"}"          # web-terminal-manage.service.d/10-detach-tmux.conf
  target="${DST}/${rel}"
  if [[ -f "${target}" ]] && cmp -s "${f}" "${target}"; then
    echo "未变: ${rel}"
    continue
  fi
  mkdir -p "$(dirname "${target}")"
  install -m 0644 "${f}" "${target}"
  echo "已安装: ${rel}"
  changed=1
done < <(find "${SRC}" -type f -name '*.conf' -print0 | sort -z)

if [[ "${changed}" -eq 1 ]]; then
  systemctl daemon-reload
  echo "daemon-reload 完成"
else
  echo "无变化，跳过 daemon-reload"
fi
