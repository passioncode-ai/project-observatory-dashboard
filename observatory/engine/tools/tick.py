#!/usr/bin/env python3
"""The tick: every collector, then the tail that turns their output into the registry, the
findings and the page — one unattended pass, on every OS (docs/design/WINDOWS-LINUX.md, W3).

This was `tools/tick.sh`; it is Python so that Windows, which has no bash a scheduler can rely
on, runs the same steps in the same order with the same log lines. `tools/tick.sh` stays as a
wrapper that picks the interpreter and hands over, so every scheduler entry that names it keeps
working.

    python tools/tick.py          # under the supervisor, or it re-enters through it

**The supervisor.** A tick runs under `tools/tick_lease.py run`, which holds the workspace's
tick lock, sets the tick's ceiling and stops the whole tree on it. A tick started directly
(launchd, Task Scheduler, a person) re-enters through it; it knows it is supervised when
`OBSERVATORY_TICK_SUPERVISOR_PID` names its parent.

**Every step** runs under the watchdog in `tools/tick_lease.py step`: its own process group,
its own wall-clock limit, never past the tick's ceiling, the whole group stopped on the limit
(lifecycle LC-02, LC-03); the feature gate is checked in the same process. A failed step is
recorded — 124 for one the watchdog stopped — and the tick goes on: a degradation is a state to
report in `tick.json`, where `tick.step_failed` names it, not a reason for a scheduler to retry.

**The three early stops** — merge, emit, validate — go through `bail`, which writes the report
and then stops, so the most consequential outcomes leave a record. The one quiet exit is the
lease stand-down: another run holds the registry and the next tick is 30 minutes away.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: The interpreter running the tick is the one every step runs with: the wrapper and the
#: schedulers choose the one that installed the engine, which carries the [full] dependencies.
PY = sys.executable
LEASE = "tools/tick_lease.py"
#: Hours before the remotes and Bitbucket listings are read again.
STALE_HOURS = 6


def log(message: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {message}", flush=True)


def stale(path: Path, hours: float) -> bool:
    """True when `path` is missing or older than `hours`. The gate is the FILE's age, so a scan
    that refuses — and writes nothing — is tried again on the next tick."""
    try:
        return time.time() - path.stat().st_mtime > hours * 3600
    except OSError:
        return True


def _text(argv) -> list[str]:
    return [str(a) for a in argv]


def lease(verb: str) -> tuple[int, str]:
    """`tools/tick_lease.py acquire|release`, with what it printed."""
    p = subprocess.run([PY, LEASE, verb], cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout + p.stderr).strip()


class Tick:
    """One run's record: which steps failed, and where the report goes."""

    def __init__(self, scratch: Path):
        self.scratch = scratch
        self.failed: list[tuple[str, int]] = []

    def step(self, name: str, *argv) -> None:
        """Run a step under the watchdog; log every line it prints under its name; record a
        non-zero exit as a failed step and go on."""
        proc = subprocess.Popen([PY, LEASE, "step", name, "--", *_text(argv)], cwd=ROOT,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace")
        for line in proc.stdout:
            log(f"{name}: {line.rstrip()}")
        rc = proc.wait()
        if rc != 0:
            log(f"{name}: EXIT {rc} — recorded as a failed step")
            self.failed.append((name, rc))

    def bounded(self, name: str, *argv) -> tuple[int, str]:
        """The same watchdog, ungated, for a command whose exit and output the tick reads itself."""
        p = subprocess.run([PY, LEASE, "step", "--ungated", name, "--", *_text(argv)], cwd=ROOT,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        return p.returncode, p.stdout + p.stderr

    def write_report(self) -> None:
        try:
            import atomic
            atomic.write_json(self.scratch / "tick.json", {
                "finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "failed_steps": [{"step": s, "exit": rc} for s, rc in self.failed],
            })
        except Exception:  # noqa: BLE001 — the report is best effort; the log says it is missing
            log("tick: could not record the step report")

    def bail(self, name: str, rc: int, message: str) -> None:
        """Stop the tick at `name`, and leave the report that says so. Exits 0: a scheduler must
        not retry a state that has been reported."""
        log(message)
        self.failed.append((name, rc))
        self.write_report()
        log(f"tick stopped at {name}")
        raise SystemExit(0)


def run(t: Tick, scratch: Path, dashboard: Path) -> int:
    log("tick start")
    # The tick's own agent-sync identity: without it the tick IS every shell command in this
    # checkout, and a lease under a shared identity separates nothing.
    os.environ["AGENT_SYNC_RUN_ID"] = f"ti{os.getpid()}-observatory-tick"
    code, out = lease("acquire")
    if code != 0:
        log("lease: " + " ".join(out.splitlines()[:2]))
        log("tick skipped — the registry belongs to another run right now, and the next tick is 30 minutes away")
        return 0
    log(f"lease: {out}")
    try:
        steps(t, scratch, dashboard)
        t.write_report()
        log("tick done")
    finally:
        # Released on every path, a bail and a crash included: a held lease would stop the
        # next tick until its TTL.
        _, out = lease("release")
        for line in out.splitlines():
            log(f"lease: {line}")
    return 0


def steps(t: Tick, SCRATCH: Path, DASHBOARD: Path) -> None:
    """Every step of one tick, in order."""
    step, bounded = t.step, t.bounded

    step("integrity", PY, "tools/check_store.py", "--print")

    step("scan-fs", PY, "collectors/scan_filesystem.py", SCRATCH / "local.json")
    step("scan-gh", PY, "collectors/scan_github.py", SCRATCH / "gh")
    step("scan-vault", PY, "collectors/scan_vault.py", SCRATCH / "vault.json")
    step("scan-sessions", PY, "collectors/scan_sessions.py", SCRATCH / "sessions.json")

    if stale(SCRATCH / "remotes.json", STALE_HOURS):
        log(f"remotes: refreshing (older than {STALE_HOURS}h)")
        step("remotes", PY, "collectors/scan_remotes.py", SCRATCH / "remotes.json")
        step("bitbucket", PY, "collectors/scan_bitbucket.py", SCRATCH / "bitbucket.json")

    if stale(SCRATCH / "domains_live.json", 24):
        log("domains: refreshing (older than 24h)")
        step("domains", PY, "collectors/scan_domains.py", SCRATCH / "domains_live.json")

    # The daily copy may live in the backups root (encrypted, outside this disk) or beside the
    # database (no passphrase yet); only the script knows where the root is, so the due check
    # is the script's own.
    if bounded("backup-due", PY, "tools/backup_store.py", "--due")[0] == 0:
        log("backup: taking a daily copy of the store")
        step("backup", PY, "tools/backup_store.py")
    if stale(SCRATCH / "heroku.json", 24):
        log("heroku: refreshing (older than 24h or absent)")
        step("heroku", PY, "collectors/scan_heroku.py", SCRATCH / "heroku.json")
    if stale(SCRATCH / "openrouter.json", 24):
        log("openrouter: refreshing the account listing (older than 24h or absent)")
        step("openrouter", PY, "collectors/scan_openrouter.py", SCRATCH / "openrouter.json")
    if stale(SCRATCH / "env.json", 24):
        log("env: re-reading every .env under the estate (older than 24h or absent)")
        step("env", PY, "collectors/scan_env.py", SCRATCH / "env.json")

    step("scan-cloudflare", PY, "collectors/scan_cloudflare.py", SCRATCH / "cloudflare_zones.json")
    step("scan-mcp", PY, "collectors/scan_mcp.py", SCRATCH / "mcp.json", "--declarations-only")
    step("remote-env", PY, "collectors/scan_remote_env.py", SCRATCH / "remote-env.json")
    step("google", PY, "collectors/scan_google.py", SCRATCH / "google.json")
    step("leaks", PY, "tools/scan_leaks.py")
    step("scrub-companion", PY, "tools/scrub_companion.py")
    # THE MACHINE: what runs, where memory and disk went, which worktrees and branches outlived
    # their work — then the cleanup's auto tier, which removes only what loses nothing
    # (tools/cleanup.py) and only with features.auto_cleanup on.
    step("machine", PY, "collectors/scan_machine.py", SCRATCH / "machine.json")
    step("git-hygiene", PY, "collectors/scan_git_hygiene.py", SCRATCH / "git-hygiene.json")
    # The lifecycle watch: orphaned product processes, per-session servers on replaced code,
    # jobs past their interval, logs past their cap. Reads the process table; starts nothing.
    step("lifecycle", PY, "collectors/scan_lifecycle.py", SCRATCH / "lifecycle.json")
    step("cleanup", PY, "tools/cleanup.py", "--auto")

    # THE TAIL. Every step from here on is in tick_lease.TAIL_STEPS, so it keeps its reserve.
    if bounded("merge", PY, "collectors/merge.py", SCRATCH)[0] != 0:
        t.bail("merge", 1, "merge failed — stopping, the registry is not rewritten")
    if bounded("emit", PY, "collectors/emit_registry.py", SCRATCH)[0] != 0:
        t.bail("emit", 1, "emit failed or REFUSED a wholesale change — the registry keeps the last good version")
    if bounded("validate", PY, "tools/validate_registry.py")[0] != 0:
        t.bail("validate", 1, "VALIDATOR RED — the registry is not projected and the agent is not called")
    if bounded("events", PY, "collectors/scan_events.py")[0] != 0:
        log("events degraded")
    if bounded("snapshot", PY, "collectors/compute_deltas.py", "snapshot")[0] != 0:
        log("snapshot degraded")
    _, diff = bounded("diff", PY, "collectors/compute_deltas.py", "diff")
    log(diff.splitlines()[0] if diff.strip() else "")

    # THE OPTIONAL STEPS, cheap ones first. Measured 2026-10-09/10: `plugins` (92–497 s)
    # and `agent` (135–244 s) ran first and used the window every tick, so the steps that
    # take seconds — the index, the erasure, the review machinery — were `not started`
    # tick after tick. Now they run first and the heavy ones take what is left; what the
    # agent proposes this tick is indexed and exported on the next.
    step("index", PY, "store/indexer.py", "index")
    step("retention", PY, "store/retention.py", "apply")
    step("sweep", PY, "tools/sweep_fixtures.py")
    step("corroborate", PY, "tools/corroborate.py")
    step("ledger", PY, "tools/export_ledger.py")
    step("lost", PY, "tools/record_lost_projects.py")
    step("plugins", PY, "collectors/run_plugins.py")
    step("rollup", PY, "store/rollup.py", "refresh")
    if re.search(r"nothing moved|nothing to compare", diff):
        log("quiet tick — no model called, 0 tokens")
    else:
        step("agent", PY, "agent/observe.py")

    step("findings", PY, "tools/build_findings.py")
    step("dashboard", PY, "dashboard/build_dashboard.py")

    # Smoke renders the page the way a browser would; a blank page is recorded, and findings
    # are built again so the notifier carries `dashboard.blank` THIS tick, not the next.
    if shutil.which("node"):
        before = list(t.failed)
        step("smoke", "node", "dashboard/smoke.js", DASHBOARD)
        if t.failed != before:
            log("dashboard SMOKE FAILED — the page would render blank")
            step("findings-recheck", PY, "tools/build_findings.py")
    else:
        log("smoke: node is absent, so whether the page renders was not measured")
    step("notify", PY, "tools/notify_findings.py")

    step("registry", PY, "tools/commit_registry.py")
    step("project-into-vault", PY, "tools/project_into_vault.py")
    step("projection", PY, "tools/commit_projection.py")
    step("links", PY, "tools/audit_vault_links.py", "--quiet")
    # One rotation policy for every log this engine writes (log_policy.py, LC-12).
    step("logs", PY, "tools/rotate_logs.py")


def supervise() -> int:
    """Re-enter through the supervisor (`tick_lease.py run`)."""
    command = [PY, LEASE, "run", "--", PY, str(Path(__file__).resolve())]
    if os.name == "nt":  # no exec on Windows: wait, and hand back its exit
        return subprocess.call(command, cwd=ROOT)
    os.execv(PY, command)
    return 0  # pragma: no cover - execv does not return


def main() -> int:
    os.chdir(ROOT)
    if os.environ.get("OBSERVATORY_TICK_SUPERVISOR_PID") != str(os.getppid()):
        return supervise()
    os.umask(0o077)
    import paths
    paths.SCRATCH.mkdir(parents=True, exist_ok=True)
    (paths.STATE / "logs").mkdir(parents=True, exist_ok=True)
    return run(Tick(paths.SCRATCH), paths.SCRATCH, paths.DASHBOARD_HTML)


if __name__ == "__main__":
    raise SystemExit(main())
