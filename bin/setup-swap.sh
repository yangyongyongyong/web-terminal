#!/usr/bin/env bash
# 配置 swap 与内核内存调优（幂等，可重复执行）
#
#   机型 3.8G 内存 + 512M swap 时，会话里一个 go build 就能把整机打满，
#   进而触发全局 OOM。这里扩到 4.6G swap，并把 swappiness 调低，
#   让 swap 只作保命用，不让热数据频繁换入换出。
#
#   ./bin/setup-swap.sh [大小G，默认 4]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIZE_G="${1:-4}"
NEW="/swap2.img"

if [[ $EUID -ne 0 ]]; then
  echo "需要 root" >&2
  exit 1
fi

# 1) swap 文件
if [[ -f "${NEW}" ]]; then
  echo "已存在: ${NEW}（跳过创建）"
else
  echo "创建 ${SIZE_G}G swap: ${NEW}"
  fallocate -l "${SIZE_G}G" "${NEW}"
  chmod 600 "${NEW}"
  mkswap "${NEW}" >/dev/null
fi

# 已在用则不重复 swapon
if swapon --show=NAME --noheadings | grep -qx "${NEW}"; then
  echo "已启用: ${NEW}"
else
  swapon "${NEW}"
  echo "已启用: ${NEW}"
fi

# 2) 开机自动挂载
if grep -q "^${NEW}[[:space:]]" /etc/fstab; then
  echo "fstab 已配置"
else
  cp /etc/fstab "/etc/fstab.bak.$(date +%Y%m%d-%H%M%S)"
  printf '%s\tnone\tswap\tsw\t0\t0\n' "${NEW}" >> /etc/fstab
  echo "已写入 fstab"
fi

# 3) 内核调优
install -m 0644 "${ROOT}/config/sysctl/99-web-terminal-swap.conf" /etc/sysctl.d/99-web-terminal-swap.conf
sysctl --system >/dev/null 2>&1 || true
echo "已应用 /etc/sysctl.d/99-web-terminal-swap.conf"

echo
echo "=== 结果 ==="
swapon --show
free -h | sed -n '1,3p'
echo "swappiness=$(cat /proc/sys/vm/swappiness) vfs_cache_pressure=$(cat /proc/sys/vm/vfs_cache_pressure)"
