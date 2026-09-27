#!/usr/bin/env bash








set -uo pipefail
cd "$(dirname "$0")/.." || exit 0
# The interpreter that installed the engine (set by install_launchd.py), else a
# source checkout's venv, else python3. An installed package has no .venv, and a
# bare python3 lacks the [full] dependencies (sqlite-vec, mcp, jsonschema).
PY="${OBSERVATORY_PYTHON:-./.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)" || exit 0


if [ "${OBSERVATORY_TICK_SUPERVISOR_PID:-}" != "$PPID" ]; then
  exec "$PY" tools/tick_lease.py run -- /bin/bash "$0"
fi
umask 077
SCRATCH="$("$PY" -c 'import paths; print(paths.SCRATCH)')" || exit 1
STATE="$("$PY" -c 'import paths; print(paths.STATE)')" || exit 1
DB_DIR="$("$PY" -c 'import paths; print(paths.DB.parent)')" || exit 1
DASHBOARD="$("$PY" -c 'import paths; print(paths.DASHBOARD_HTML)')" || exit 1
mkdir -p "$SCRATCH" "$STATE/logs"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
















FAILED_STEPS=""
step() {
  local name="$1"; shift
  if ! "$PY" tools/tick_lease.py allowed "$name"; then
    log "$name: disabled in workspace settings"
    return 0
  fi
  "$@" 2>&1 | while IFS= read -r l; do log "$name: $l"; done
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    log "$name: EXIT $rc — recorded as a failed step"
    FAILED_STEPS="$FAILED_STEPS $name=$rc"
  fi
  return 0
}



write_report() {
  "$PY" - "$FAILED_STEPS" <<'PYEOF' || log "tick: could not record the step report"
import json, pathlib, sys
from datetime import datetime, timezone
sys.path.insert(0, ".")
import paths, atomic
failed = [s for s in sys.argv[1].split() if "=" in s]
atomic.write_json(paths.SCRATCH / "tick.json", {
    "finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "failed_steps": [{"step": s.split("=")[0], "exit": int(s.split("=")[1])} for s in failed],
})
PYEOF
}








bail() {
  local name="$1" rc="$2" msg="$3"
  log "$msg"
  FAILED_STEPS="$FAILED_STEPS $name=$rc"
  write_report
  log "tick stopped at $name"
  exit 0
}

log "tick start"















export AGENT_SYNC_RUN_ID="ti$$-observatory-tick"
if ! LEASE_OUT="$("$PY" tools/tick_lease.py acquire 2>&1)"; then
  log "lease: $(printf '%s' "$LEASE_OUT" | head -2 | tr '\n' ' ')"
  log "tick skipped — the registry belongs to another run right now, and the next tick is 30 minutes away"
  exit 0
fi
log "lease: $LEASE_OUT"







trap '"$PY" tools/tick_lease.py release 2>&1 | while IFS= read -r l; do log "lease: $l"; done' EXIT










step "integrity" "$PY" tools/check_store.py --print

step "scan-fs"       "$PY" collectors/scan_filesystem.py "$SCRATCH/local.json"
step "scan-gh"       "$PY" collectors/scan_github.py    "$SCRATCH/gh"
step "scan-vault"    "$PY" collectors/scan_vault.py     "$SCRATCH/vault.json"



step "scan-sessions" "$PY" collectors/scan_sessions.py "$SCRATCH/sessions.json"





STALE_HOURS=6
needs_refresh() {  # $1 = file; true when missing or older than STALE_HOURS
  [ -f "$1" ] || return 0
  [ -z "$(find "$1" -mmin +$((STALE_HOURS * 60)) -print -maxdepth 0 2>/dev/null)" ] && return 1
  return 0
}
if needs_refresh "$SCRATCH/remotes.json"; then
  log "remotes: refreshing (older than ${STALE_HOURS}h)"
  step "remotes"   "$PY" collectors/scan_remotes.py  "$SCRATCH/remotes.json"
  step "bitbucket" "$PY" collectors/scan_bitbucket.py "$SCRATCH/bitbucket.json"
fi


if [ -z "$(find "$SCRATCH/domains_live.json" -mmin -1440 -print -maxdepth 0 2>/dev/null)" ]; then
  log "domains: refreshing (older than 24h)"





  step "domains" "$PY" collectors/scan_domains.py "$SCRATCH/domains_live.json"
fi






# The daily copy may live in the backups root (encrypted, outside this disk) or
# beside the database (no passphrase yet); only Python knows where the root is,
# so the due check is the script's, not a `find` over one of the two places.
if "$PY" tools/backup_store.py --due >/dev/null 2>&1; then
  log "backup: taking a daily copy of the store"
  step "backup" "$PY" tools/backup_store.py
fi
if [ -z "$(find "$SCRATCH/heroku.json" -mmin -1440 -print -maxdepth 0 2>/dev/null)" ]; then
  log "heroku: refreshing (older than 24h or absent)"
  step "heroku" "$PY" collectors/scan_heroku.py "$SCRATCH/heroku.json"
fi




if [ -z "$(find "$SCRATCH/openrouter.json" -mmin -1440 -print -maxdepth 0 2>/dev/null)" ]; then
  log "openrouter: refreshing the account listing (older than 24h or absent)"
  step "openrouter" "$PY" collectors/scan_openrouter.py "$SCRATCH/openrouter.json"
fi
if [ -z "$(find "$SCRATCH/env.json" -mmin -1440 -print -maxdepth 0 2>/dev/null)" ]; then
  log "env: re-reading every .env under the estate (older than 24h or absent)"
  step "env" "$PY" collectors/scan_env.py "$SCRATCH/env.json"
fi







step "scan-cloudflare" "$PY" collectors/scan_cloudflare.py "$SCRATCH/cloudflare_zones.json"
step "scan-mcp" "$PY" collectors/scan_mcp.py "$SCRATCH/mcp.json"
step "remote-env" "$PY" collectors/scan_remote_env.py "$SCRATCH/remote-env.json"
step "google" "$PY" collectors/scan_google.py "$SCRATCH/google.json"
step "leaks" "$PY" tools/scan_leaks.py




step "scrub-companion" "$PY" tools/scrub_companion.py
# THE MACHINE: what runs, where memory and disk went, which worktrees and branches
# outlived their work — then the cleanup's auto tier, which removes only what
# loses nothing (tools/cleanup.py) and only with features.auto_cleanup on.
step "machine" "$PY" collectors/scan_machine.py "$SCRATCH/machine.json"
step "git-hygiene" "$PY" collectors/scan_git_hygiene.py "$SCRATCH/git-hygiene.json"
step "cleanup" "$PY" tools/cleanup.py --auto
"$PY" collectors/merge.py          "$SCRATCH"             >/dev/null 2>&1 || bail merge 1 "merge failed — stopping, the registry is not rewritten"
"$PY" collectors/emit_registry.py  "$SCRATCH"             >/dev/null 2>&1 || bail emit 1 "emit failed or REFUSED a wholesale change — the registry keeps the last good version"

if ! "$PY" tools/validate_registry.py >/dev/null 2>&1; then
  bail validate 1 "VALIDATOR RED — the registry is not projected and the agent is not called"
fi

"$PY" collectors/scan_events.py   >/dev/null 2>&1 || log "events degraded"





step "plugins" "$PY" collectors/run_plugins.py
step "rollup" "$PY" store/rollup.py refresh
"$PY" collectors/compute_deltas.py snapshot >/dev/null 2>&1 || log "snapshot degraded"
DIFF="$("$PY" collectors/compute_deltas.py diff 2>&1)"
log "$(printf '%s' "$DIFF" | head -1)"



if printf '%s' "$DIFF" | grep -qE 'nothing moved|nothing to compare'; then
  log "quiet tick — no model called, 0 tokens"
else
  step "agent" "$PY" agent/observe.py
fi




step "index" "$PY" store/indexer.py index



step "retention" "$PY" store/retention.py apply






step "sweep" "$PY" tools/sweep_fixtures.py







step "corroborate" "$PY" tools/corroborate.py

step "ledger" "$PY" tools/export_ledger.py




step "lost" "$PY" tools/record_lost_projects.py
step "findings" "$PY" tools/build_findings.py











step "dashboard" "$PY" dashboard/build_dashboard.py
























if command -v node >/dev/null; then
  SMOKE_BEFORE="$FAILED_STEPS"
  step "smoke" node dashboard/smoke.js "$DASHBOARD"
  if [ "$FAILED_STEPS" != "$SMOKE_BEFORE" ]; then
    log "dashboard SMOKE FAILED — the page would render blank"
    step "findings-recheck" "$PY" tools/build_findings.py
  fi
else
  log "smoke: node is absent, so whether the page renders was not measured"
fi
step "notify" "$PY" tools/notify_findings.py





step "registry" "$PY" tools/commit_registry.py

step "project-into-vault" "$PY" tools/project_into_vault.py


step "projection" "$PY" tools/commit_projection.py





step "links" "$PY" tools/audit_vault_links.py --quiet







write_report

log "tick done"







rotate_log() {
  local f="$STATE/logs/tick.log" limit=$((2 * 1024 * 1024))
  [ -f "$f" ] || return 0
  local size; size=$(stat -f %z "$f" 2>/dev/null || stat -c %s "$f" 2>/dev/null || echo 0)
  [ "$size" -gt "$limit" ] || return 0
  [ -f "$f.2" ] && mv -f "$f.2" "$f.3"
  [ -f "$f.1" ] && mv -f "$f.1" "$f.2"
  mv -f "$f" "$f.1"
  log "log: rotated tick.log at ${size} bytes — three generations kept"
}
rotate_log
