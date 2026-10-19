#!/usr/bin/env bash







set -uo pipefail

emit() { printf '%s\n' "$1"; exit 0; }


if [ -t 0 ]; then payload=""; else payload=$(cat 2>/dev/null || true); fi























root="${OBSERVATORY_ROOT:-}"
if [ -z "$root" ] && [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
  c="${CLAUDE_PLUGIN_ROOT}"
  for _ in 1 2 3 4 5 6; do
    [ -f "$c/observatory.py" ] && { root="$c"; break; }
    n="$(cd "$c/.." 2>/dev/null && pwd)" || break
    [ "$n" = "$c" ] && break                 # reached the filesystem root
    c="$n"
  done
fi
[ -n "$root" ] || exit 0                       # no checkout: silent, not an error
[ -f "$root/tools/record_turn.py" ] || exit 0  # older checkout: silent

py="${OBSERVATORY_PYTHON:-$root/.venv/bin/python}"   # set by `full agent install`
[ -x "$py" ] || py="$(command -v python3 2>/dev/null)"
[ -n "$py" ] || exit 0                         # no interpreter: silent

read_field() {                     # read_field <key>
  [ -n "$payload" ] || return 0
  printf '%s' "$payload" | "$py" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
print(d.get(sys.argv[1]) or "")
' "$1" 2>/dev/null
}




[ "$(read_field stop_hook_active)" = "True" ] && exit 0
[ "$(read_field stop_hook_active)" = "true" ] && exit 0

cwd="$(read_field cwd)"; [ -n "$cwd" ] || cwd="${CLAUDE_PROJECT_DIR:-$PWD}"
session="$(read_field session_id)"
# The session's own transcript, so a register another session is editing in the same
# checkout does not silence this session's ask. Passed only to a recorder that reads
# it: an older engine's argument parser would refuse the flag and record nothing.
transcript="$(read_field transcript_path)"
extra=()
if [ -n "$transcript" ] && [ -f "$transcript" ] && grep -q -- '--transcript' "$root/tools/record_turn.py" 2>/dev/null; then
  extra=(--transcript "$transcript")
fi

result="$("$py" "$root/tools/record_turn.py" --cwd "$cwd" --session-id "$session" ${extra[@]+"${extra[@]}"} 2>/dev/null)"
[ -n "$result" ] || exit 0

printf '%s' "$result" | "$py" "${CLAUDE_PLUGIN_ROOT:-$(dirname "$0")/..}/hooks/ask-why.py" 2>/dev/null
exit 0
