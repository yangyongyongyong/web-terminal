#!/usr/bin/env bash
# LaunchAgent：加载 .env 后 exec ttyd（tmux 会话模式 + 自定义重连页）
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export HOME="${HOME:-$(eval echo ~$(id -un))}"
export USER="${USER:-$(id -un)}"
export LANG="${LANG:-zh_CN.UTF-8}"
export LC_ALL="${LC_ALL:-zh_CN.UTF-8}"
export TERM_PROGRAM="${TERM_PROGRAM:-ttyd}"
export COLORTERM="${COLORTERM:-truecolor}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=common.sh
source "${ROOT}/bin/common.sh"

mkdir -p "${ROOT}/logs" "${ROOT}/run" "${ROOT}/web"

# 生成带重连注入的 index
"${ROOT}/bin/ensure-ttyd-index.sh"

TTYD_BIN="$(command -v ttyd)"
INDEX_HTML="${ROOT}/web/ttyd-index.html"
ATTACH="${ROOT}/bin/attach-session.sh"
BASE_PATH="${TTYD_BASE_PATH:-/term}"
# 约 SCROLLBACK_PAGES 页（按 50 行/页）；xterm 本地 scrollback，滚轮不走公网
SCROLLBACK_LINES=$(( ${SCROLLBACK_PAGES:-30} * 50 ))
if [[ "${SCROLLBACK_LINES}" -lt 200 ]]; then SCROLLBACK_LINES=200; fi

# 端口：对外 7681 由 Caddy 反代（压缩 HTML，跨境省 74% 流量）；本体只听内部
TTYD_ARGS=(
  --interface "${TTYD_HOST}"
  --port "${TTYD_LISTEN_PORT:-17781}"
  # 不再使用 --credential（Basic 弹窗）：页面本身无敏感内容，shell 由
  # attach-session.sh 的登录票据把关；登录改走管理端口 7690 的登录页。
  --writable
  --url-arg
  --base-path "${BASE_PATH}"
  --terminal-type xterm-256color
  --ping-interval 5
  --signal 1
  --index "${INDEX_HTML}"
  --client-option "rendererType=canvas"
  --client-option "disableResizeOverlay=true"
  --client-option "disableLeaveAlert=true"
  --client-option "cursorBlink=true"
  --client-option "fontSize=15"
  --client-option "fontFamily=Menlo, Monaco, 'Courier New', monospace"
  --client-option "scrollback=${SCROLLBACK_LINES}"
)

# 每个浏览器连接：attach-session.sh <name> [create]
# URL: /term/?arg=main 或 /term/?arg=work&arg=create
exec "${TTYD_BIN}" "${TTYD_ARGS[@]}" "${ATTACH}"
