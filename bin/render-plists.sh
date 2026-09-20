#!/usr/bin/env bash
# 从 config/plist-templates/*.plist.example 渲染真实 LaunchDaemon plist。
# 模板用 __HOME__ / __USER__ 占位；label 用本机已有的 uk.lucadesign.* 原值
# （运行中的 LaunchDaemon 依赖这些 label，不要改）。
# 真实 plist 不入库（gitignore），模板入库。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TPL_DIR="${ROOT}/config/plist-templates"
OUT_DIR="${ROOT}/config"

USER_NAME="$(id -un)"
HOME_DIR="$(eval echo ~"${USER_NAME}")"

# 本机实际使用的 label 前缀：已有真实 plist 时沿用其前缀（保持 LaunchDaemon 兼容）；
# 全新安装时用 local.web-terminal（避免把隐私域名写进仓库）
LABEL_PREFIX="$(ls "${OUT_DIR}/"*.web-terminal.ttyd.plist 2>/dev/null | head -1 | xargs -I{} basename {} .plist 2>/dev/null | sed 's/\.ttyd$//')"
LABEL_PREFIX="${LABEL_PREFIX:-local.web-terminal}"

render() {
  local svc="$1"
  local tpl="${TPL_DIR}/web-terminal.${svc}.plist.example"
  local out="${OUT_DIR}/${LABEL_PREFIX}.${svc}.plist"
  [[ -f "${tpl}" ]] || { echo "缺少模板 ${tpl}" >&2; exit 1; }
  sed -e "s|__HOME__|${HOME_DIR}|g" -e "s|__USER__|${USER_NAME}|g" \
      -e "s|<string>web-terminal\.${svc}</string>|<string>${LABEL_PREFIX}.${svc}</string>|" \
      "${tpl}" > "${out}"
  echo "渲染 ${out}"
}

for svc in ttyd manage cloudflared healthcheck; do
  render "${svc}"
done

echo "完成。安装到系统：./bin/start.sh（或 safe-restart.sh）"
