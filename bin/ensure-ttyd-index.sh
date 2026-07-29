#!/usr/bin/env bash
# 确保 web/ttyd-index.html 存在：必要时临时拉起 ttyd 抓取默认页再注入
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STOCK="${ROOT}/web/ttyd-stock.html"
INDEX="${ROOT}/web/ttyd-index.html"

mkdir -p "${ROOT}/web"

if [[ -f "${INDEX}" ]] && grep -q 'id="wt-reconnect"' "${INDEX}" && [[ -f "${STOCK}" ]]; then
  # stock 在时始终按最新注入脚本重生成 index
  python3 "${ROOT}/bin/patch-ttyd-index.py"
  exit $?
fi

if [[ -f "${INDEX}" ]] && grep -q 'id="wt-reconnect"' "${INDEX}"; then
  exit 0
fi

if [[ ! -f "${STOCK}" ]]; then
  PORT=17991
  TMP_LOG="${ROOT}/run/ttyd-index-bootstrap.log"
  # 临时 ttyd：无认证，只为导出 HTML
  /opt/homebrew/bin/ttyd --interface 127.0.0.1 --port "${PORT}" --writable /bin/true \
    >"${TMP_LOG}" 2>&1 &
  pid=$!
  cleanup() { kill "${pid}" 2>/dev/null || true; }
  trap cleanup EXIT
  for _ in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${PORT}/" -o "${STOCK}"; then
      break
    fi
    sleep 0.2
  done
  cleanup
  trap - EXIT
  if [[ ! -s "${STOCK}" ]]; then
    echo "无法抓取 ttyd 默认 HTML" >&2
    exit 1
  fi
fi

python3 "${ROOT}/bin/patch-ttyd-index.py"
