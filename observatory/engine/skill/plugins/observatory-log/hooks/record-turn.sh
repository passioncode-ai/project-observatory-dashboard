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

# ONE PARSE of the payload, not one interpreter start per field: four starts cost
# 0.1-0.4 s of every turn's Stop before the recorder ran. A value holding a line
# break is dropped rather than split across the fields that follow it.
read_fields() {                    # stop_hook_active, cwd, session_id, transcript_path
  [ -n "$payload" ] || return 0
  printf '%s' "$payload" | "$py" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
if not isinstance(d, dict):
    d = {}
for k in ("stop_hook_active", "cwd", "session_id", "transcript_path"):
    v = d.get(k)
    v = "" if v is None or v is False else str(v)
    print("" if ("\n" in v or "\r" in v) else v)
' 2>/dev/null
}
fields="$(read_fields)"
active=""; cwd=""; session=""; transcript=""
{ IFS= read -r active; IFS= read -r cwd; IFS= read -r session; IFS= read -r transcript; } <<EOF_FIELDS || true
$fields
EOF_FIELDS

[ "$active" = "True" ] && exit 0
[ "$active" = "true" ] && exit 0

[ -n "$cwd" ] || cwd="${CLAUDE_PROJECT_DIR:-$PWD}"
# `transcript` is the session's own transcript, so a register another session is
# editing in the same checkout does not silence this session's ask. Passed only to a
# recorder that reads it: an older engine's argument parser would refuse the flag
# and record nothing.
extra=()
if [ -n "$transcript" ] && [ -f "$transcript" ] && grep -q -- '--transcript' "$root/tools/record_turn.py" 2>/dev/null; then
  extra=(--transcript "$transcript")
fi

result="$("$py" "$root/tools/record_turn.py" --cwd "$cwd" --session-id "$session" ${extra[@]+"${extra[@]}"} 2>/dev/null)"
[ -n "$result" ] || exit 0

printf '%s' "$result" | "$py" "${CLAUDE_PLUGIN_ROOT:-$(dirname "$0")/..}/hooks/ask-why.py" 2>/dev/null
exit 0
