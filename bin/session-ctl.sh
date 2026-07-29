#!/usr/bin/env bash
# 会话管理 CLI：list / create / kill / history
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=common.sh
source "${ROOT}/bin/common.sh"

TMUX_BIN="$(command -v tmux)"
TMUX_CONF="${ROOT}/config/tmux.web.conf"
SHELL_BIN="${LOGIN_SHELL:-/bin/zsh}"
META_DIR="${ROOT}/run/sessions"
HISTORY_FILE="${ROOT}/run/session-history.jsonl"
PATH_ROOT="${SESSION_PATH_ROOT:-${HOME}}"
DEFAULT_PATH="${SESSION_DEFAULT_PATH:-${HOME}}"

mkdir -p "${META_DIR}"

sanitize() {
  /opt/homebrew/bin/python3 "${ROOT}/bin/session-ticket.py" sanitize "${1:-}"
}

session_id() {
  echo "wt-$(sanitize "$1")"
}

meta_get() {
  local sid="$1" key="$2"
  local f="${META_DIR}/${sid}.${key}"
  if [[ -f "${f}" ]]; then
    cat "${f}"
  fi
}

meta_set() {
  printf '%s\n' "$3" >"${META_DIR}/${1}.${2}"
}

# 解析并校验工作目录：必须存在且位于 SESSION_PATH_ROOT 之下
resolve_cwd() {
  local raw="${1:-}"
  local root def resolved
  root="$(cd "${PATH_ROOT}" 2>/dev/null && pwd -P)" || {
    echo "SESSION_PATH_ROOT 无效: ${PATH_ROOT}" >&2
    return 1
  }
  def="${raw:-${DEFAULT_PATH}}"
  if [[ "${def}" != /* ]]; then
    def="${root}/${def}"
  fi
  if [[ ! -d "${def}" ]]; then
    echo "路径不存在或不是目录: ${def}" >&2
    return 1
  fi
  resolved="$(cd "${def}" && pwd -P)"
  case "${resolved}" in
    "${root}"|"${root}"/*) ;;
    *)
      echo "路径超出允许范围（须在 ${root} 下）: ${resolved}" >&2
      return 1
      ;;
  esac
  printf '%s\n' "${resolved}"
}

pane_cwd() {
  local sid="$1"
  "${TMUX_BIN}" display-message -p -t "${sid}:" '#{pane_current_path}' 2>/dev/null || true
}

cmd_list() {
  local line sid name created last_open clients cwd now
  while IFS= read -r line; do
    [[ -z "${line}" ]] && continue
    sid="${line}"
    name="${sid#wt-}"
    created="$(meta_get "${sid}" created)"
    last_open="$(meta_get "${sid}" last_open)"
    cwd="$(meta_get "${sid}" cwd)"
    now="$(pane_cwd "${sid}")"
    [[ -z "${cwd}" || "${cwd}" == "-" ]] && cwd="${now:--}"
    clients="$("${TMUX_BIN}" list-clients -t "${sid}" 2>/dev/null | wc -l | tr -d ' ')"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "${name}" "${sid}" "${created:--}" "${last_open:--}" "${clients}" "${cwd:--}" "${now:--}"
  done < <("${TMUX_BIN}" list-sessions -F '#{session_name}' 2>/dev/null | grep '^wt-' || true)
}

cmd_create() {
  local name sid cwd
  name="$(sanitize "${1:-}")"
  if [[ -z "${name}" ]]; then
    echo "需要会话名" >&2
    exit 1
  fi
  sid="$(session_id "${name}")"
  if "${TMUX_BIN}" has-session -t "${sid}" 2>/dev/null; then
    # 已存在：补写缺失的 cwd 元数据（不改正在跑的目录）
    if [[ -z "$(meta_get "${sid}" cwd)" ]]; then
      now="$(pane_cwd "${sid}")"
      [[ -n "${now}" ]] && meta_set "${sid}" cwd "${now}"
    fi
    echo "exists:${name}"
    return 0
  fi
  cwd="$(resolve_cwd "${2:-}")" || exit 1
  "${TMUX_BIN}" -f "${TMUX_CONF}" new-session -d -c "${cwd}" -s "${sid}" -x 220 -y 60 "${SHELL_BIN}" -l
  meta_set "${sid}" created "$(date '+%Y-%m-%dT%H:%M:%S%z')"
  meta_set "${sid}" title "${name}"
  meta_set "${sid}" cwd "${cwd}"
  echo "created:${name}:${cwd}"
}

cmd_kill() {
  local name sid created last_open cwd purge=0
  name="$(sanitize "${1:-}")"
  if [[ "${2:-}" == "--purge" || "${2:-}" == "purge" ]]; then
    purge=1
  fi
  if [[ -z "${name}" ]]; then
    echo "需要会话名" >&2
    exit 1
  fi
  sid="$(session_id "${name}")"
  if ! "${TMUX_BIN}" has-session -t "${sid}" 2>/dev/null; then
    echo "会话不存在或已停止: ${name}" >&2
    exit 1
  fi
  if [[ "${purge}" -eq 1 ]]; then
    # 彻底删除：停进程且不留历史记录
    "${TMUX_BIN}" kill-session -t "${sid}"
    rm -f "${META_DIR}/${sid}".*
    /opt/homebrew/bin/python3 - "${name}" "${HISTORY_FILE}" <<'PY'
import json, sys
from pathlib import Path
name, path = sys.argv[1:3]
p = Path(path)
if not p.exists():
    raise SystemExit(0)
items = []
for line in p.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        continue
    if obj.get("name") == name:
        continue
    items.append(obj)
p.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in items), encoding="utf-8")
PY
    echo "purged:${name}"
    return 0
  fi
  created="$(meta_get "${sid}" created)"
  last_open="$(meta_get "${sid}" last_open)"
  # 记住停止当下的实际工作目录，下次 open 从这里恢复
  cwd="$(pane_cwd "${sid}")"
  [[ -z "${cwd}" ]] && cwd="$(meta_get "${sid}" cwd)"
  "${TMUX_BIN}" kill-session -t "${sid}"
  /opt/homebrew/bin/python3 - "${name}" "${sid}" "${created}" "${last_open}" "${cwd}" "${HISTORY_FILE}" <<'PY'
import json, sys, datetime
from pathlib import Path
name, sid, created, last_open, cwd, path = sys.argv[1:7]
rec = {
    "name": name,
    "sid": sid,
    "created": created or "",
    "last_open": last_open or "",
    "cwd": cwd or "",
    "stopped": datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
}
p = Path(path)
items = []
if p.exists():
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        # 同一会话名只保留一条最新路径记忆
        if obj.get("name") == name:
            continue
        items.append(obj)
items.append(rec)
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in items), encoding="utf-8")
PY
  rm -f "${META_DIR}/${sid}".*
  echo "killed:${name}"
}

cmd_history_del() {
  local name cwd
  name="$(sanitize "${1:-}")"
  cwd="${2:-}"
  if [[ -z "${name}" ]]; then
    echo "需要会话名" >&2
    exit 1
  fi
  /opt/homebrew/bin/python3 - "${name}" "${cwd}" "${HISTORY_FILE}" <<'PY'
import json, sys
from pathlib import Path
name, cwd, path = sys.argv[1:4]
p = Path(path)
if not p.exists():
    print("missing")
    raise SystemExit(0)
items = []
removed = 0
for line in p.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        continue
    if obj.get("name") == name and (obj.get("cwd") or "") == (cwd or ""):
        removed += 1
        continue
    items.append(obj)
p.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in items), encoding="utf-8")
print(f"deleted:{removed}")
PY
}

cmd_history() {
  if [[ -f "${HISTORY_FILE}" ]]; then
    # 输出已按 name+cwd 去重后的最新记录
    /opt/homebrew/bin/python3 - "${HISTORY_FILE}" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
by = {}
for line in p.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        continue
    key = (obj.get("name") or "", obj.get("cwd") or "")
    by[key] = obj
for obj in by.values():
    print(json.dumps(obj, ensure_ascii=False))
PY
  fi
}

cmd_resolve() {
  resolve_cwd "${1:-}"
}

cmd_rename() {
  local old new old_sid new_sid live=0 hist_changed
  old="$(sanitize "${1:-}")"
  new="$(sanitize "${2:-}")"
  if [[ -z "${old}" || -z "${new}" ]]; then
    echo "需要旧名称和新名称" >&2
    exit 1
  fi
  if [[ "${old}" == "${new}" ]]; then
    echo "same:${old}"
    return 0
  fi
  old_sid="$(session_id "${old}")"
  new_sid="$(session_id "${new}")"

  if "${TMUX_BIN}" has-session -t "${new_sid}" 2>/dev/null; then
    echo "目标名称已存在（运行中）: ${new}" >&2
    exit 1
  fi
  if "${TMUX_BIN}" has-session -t "${old_sid}" 2>/dev/null; then
    live=1
  fi

  hist_changed="$(/opt/homebrew/bin/python3 - "${old}" "${new}" "${HISTORY_FILE}" <<'PY'
import json, sys
from pathlib import Path
old, new, path = sys.argv[1:4]
p = Path(path)
items = []
has_old = False
has_new = False
if p.exists():
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = obj.get("name") or ""
        if name == old:
            has_old = True
        if name == new:
            has_new = True
        items.append(obj)
if has_new:
    print("conflict")
    raise SystemExit(0)
if not has_old:
    print("0")
    raise SystemExit(0)
out = []
for obj in items:
    if obj.get("name") == old:
        obj = dict(obj)
        obj["name"] = new
        if obj.get("sid") == f"wt-{old}":
            obj["sid"] = f"wt-{new}"
    out.append(obj)
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in out), encoding="utf-8")
print("1")
PY
)"

  if [[ "${hist_changed}" == "conflict" ]]; then
    echo "目标名称已存在（历史记录）: ${new}" >&2
    exit 1
  fi
  if [[ "${live}" -eq 0 && "${hist_changed}" != "1" ]]; then
    echo "会话不存在: ${old}" >&2
    exit 1
  fi

  if [[ "${live}" -eq 1 ]]; then
    "${TMUX_BIN}" rename-session -t "${old_sid}" "${new_sid}"
    local f key
    for f in "${META_DIR}/${old_sid}".*; do
      [[ -e "${f}" ]] || continue
      key="${f##"${META_DIR}/${old_sid}".}"
      mv "${f}" "${META_DIR}/${new_sid}.${key}"
    done
    meta_set "${new_sid}" title "${new}"
  fi

  echo "renamed:${old}:${new}"
}

usage() {
  echo "用法: $0 list|create <name> [cwd]|kill <name> [--purge]|rename <old> <new>|history|history-del <name> [cwd]|resolve [cwd]" >&2
  exit 1
}

case "${1:-}" in
  list) cmd_list ;;
  create) cmd_create "${2:-}" "${3:-}" ;;
  kill) cmd_kill "${2:-}" "${3:-}" ;;
  rename) cmd_rename "${2:-}" "${3:-}" ;;
  history) cmd_history ;;
  history-del) cmd_history_del "${2:-}" "${3:-}" ;;
  resolve) cmd_resolve "${2:-}" ;;
  *) usage ;;
esac
