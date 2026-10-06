#!/usr/bin/env bash







set -uo pipefail
if [ -t 0 ]; then payload=""; else payload=$(cat 2>/dev/null || true); fi
root="${OBSERVATORY_ROOT:-}"
if [ -z "$root" ] && [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
  c="${CLAUDE_PLUGIN_ROOT}"
  for _ in 1 2 3 4 5 6; do
    [ -f "$c/observatory.py" ] && { root="$c"; break; }
    n="$(cd "$c/.." 2>/dev/null && pwd)" || break
    [ "$n" = "$c" ] && break
    c="$n"
  done
fi
if [ -z "$root" ] && command -v project-observatory >/dev/null 2>&1; then
  # Installed by the PassionCode launcher with no `full agent install`: ask the engine.
  root="$(project-observatory full-path 2>/dev/null || true)"
fi
[ -n "$root" ] || exit 0
[ -d "$root" ] || exit 0

# The engine's own virtual environment (the ancestor holding pyvenv.cfg), so the jobs
# below run the installed package and never whichever python3 is first on PATH.
venv=""; c="$root"
for _ in 1 2 3 4 5 6 7 8; do
  [ -f "$c/pyvenv.cfg" ] && { venv="$c"; break; }
  n="$(dirname "$c")"; [ "$n" = "$c" ] && break; c="$n"
done
py="${OBSERVATORY_PYTHON:-}"                              # set by `full agent install`
[ -n "$py" ] && [ -x "$py" ] || py=""
[ -z "$py" ] && [ -n "$venv" ] && [ -x "$venv/bin/python" ] && py="$venv/bin/python"
[ -z "$py" ] && [ -x "$root/.venv/bin/python" ] && py="$root/.venv/bin/python"
[ -z "$py" ] && py="$(command -v python3 2>/dev/null)"
[ -n "$py" ] || exit 0

if [ -f "$root/tools/session_start.py" ]; then
  printf '%s' "$payload" | "$py" "$root/tools/session_start.py" 2>/dev/null || true
fi

# Updates that arrive by themselves (docs/runs/2026-10-05-auto-update, D8). Detached and
# rate-limited, so a session never waits for it and never fails because of it.
stamps="${XDG_STATE_HOME:-$HOME/.local/state}/project-observatory"
mkdir -p "$stamps" 2>/dev/null && chmod 700 "$stamps" 2>/dev/null
# claim NAME SECONDS: true for exactly one caller per window. The check and the stamp happen
# under one exclusive lock, so twenty sessions starting at once start one job, not twenty —
# also when the previous window has just expired (audit A20).
claim() {
  "$py" - "$stamps/$1.at" "$2" <<'PYCLAIM' 2>/dev/null
import fcntl, os, sys, time
path, window = sys.argv[1], int(sys.argv[2])
fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(1)
try:
    last = os.stat(path).st_mtime
except OSError:
    last = 0
if time.time() - last < window:
    sys.exit(1)
with open(path, "a"):
    pass
os.utime(path, None)
PYCLAIM
}
# detach LOG CMD...: a new session of its own, so ending this session or its process group
# cannot stop an install half-way (review F8).
detach() {
  local log="$1"; shift
  "$py" -c 'import subprocess, sys
log = open(sys.argv[1], "a")
subprocess.Popen(sys.argv[2:], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)' \
    "$log" "$@" 2>/dev/null || true
}
if [ -f "$root/tools/maintain.py" ]; then
  # 0.17.0 and later: schedule the hourly job where it is missing, or run a pass where
  # this machine has no scheduler. At most every six hours.
  if claim maintain-hook 21600; then
    OBSERVATORY_SYSTEM_SETUP=1 detach "$stamps/maintain-hook.log" "$py" "$root/tools/maintain.py" hook
  fi
else
  # The bridge for engines from before 0.17.0, which carry no updater of their own:
  # once a day, check; when a newer stable release exists and the person has not turned
  # automatic updates off (the `auto-update` file says `off`, or `updates.auto` is false),
  # install it with `full update --apply`.
  po=""
  [ -n "$venv" ] && [ -x "$venv/bin/project-observatory" ] && po="$venv/bin/project-observatory"
  [ -n "$po" ] || po="$(command -v project-observatory 2>/dev/null)"
  if [ -n "$po" ] && claim update-bridge 86400; then
    detach "$stamps/update-bridge.log" /bin/bash -c '
      po="$1"; py="$2"
      home="${OBSERVATORY_FULL_HOME:-${OBSERVATORY_HOME:-$HOME/.local/share/project-observatory-full}}"
      # The switch (LC-16): the file `auto-update` in the home, where only `off` is off;
      # without one, `updates.auto: false` in settings.json, as those releases wrote it.
      off="$("$py" -c "import json,os,sys
f=os.path.join(sys.argv[1], \"auto-update\")
if os.path.isfile(f) and not os.path.islink(f):
    print(1 if open(f, errors=\"replace\").read(64).strip().lower() == \"off\" else 0); sys.exit()
try: d=json.load(open(os.path.join(sys.argv[1], \"config\", \"settings.json\")))
except Exception: d={}
print(1 if (d.get(\"updates\") or {}).get(\"auto\") is False else 0)" "$home" 2>/dev/null)"
      [ "$off" = "1" ] && { echo "$(date -u +%FT%TZ) automatic updates are off"; exit 0; }
      "$po" full update --check >/dev/null 2>&1; rc=$?
      echo "$(date -u +%FT%TZ) check exit $rc"
      [ "$rc" = "10" ] || exit 0
      # No launchd (Linux): nothing for the update to stop, and saying so lets it run (A02).
      if command -v launchctl >/dev/null 2>&1; then "$po" full update --apply
      else "$po" full update --apply --writers-stopped; fi
      echo "$(date -u +%FT%TZ) apply exit $?"
    ' _ "$po" "$py"
  fi
fi
exit 0
