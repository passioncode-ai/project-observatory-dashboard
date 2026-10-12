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

**The collectors take turns.** Under load the collectors in front used the whole window, and the
ones at the back of a fixed order were `not started` tick after tick: on one machine the leak scan
and the lifecycle watch had not run for two days while the folder scan and the store check took
the first thirteen minutes of every tick (2026-10-12, load average 26-30). So the collectors after
`integrity` are planned first and then run least recently started first (`rotation_order`), with
the start times kept in `store/tick-rotation.json`. A step that was `not started` keeps its old
time and goes first next tick; one that ran goes to the back. A collector that reads another's
output in the same tick still runs after it (`READS_FROM`, kept equal to the artefact graph by
`tests/test_pipeline.py`).

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
#: The watchdog's exit for a step it stopped or did not start (tick_lease.TIMED_OUT).
TIMED_OUT = 124
LEASE = "tools/tick_lease.py"
#: Hours before the remotes and Bitbucket listings are read again.
STALE_HOURS = 6
#: Where the rotation keeps the last time each collector actually started (epoch seconds).
ROTATION_FILE = "tick-rotation.json"
#: What `tick_lease.py step` prints, first, for a step it did not start for want of time.
NOT_STARTED = "not started"
#: Collectors that read another collector's output in the same tick: the reader runs after the
#: steps it names whenever both run this tick. Equal to the artefact graph (tests/test_pipeline.py).
READS_FROM: dict[str, tuple[str, ...]] = {
    "remotes": ("scan-fs",),
    "bitbucket": ("scan-fs",),
    "remote-env": ("heroku",),
    "leaks": ("env",),
    "scrub-companion": ("env", "leaks"),
    "cleanup": ("machine", "git-hygiene"),
}


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


def rotation_order(planned: list[str], started: dict[str, float],
                   reads: dict[str, tuple[str, ...]] = READS_FROM) -> list[str]:
    """`planned` least recently started first; a step never started goes first, and ties keep the
    planned order. Then a writer that would run after a step reading its output is moved to just
    before that reader, so the starved reader stays early and still reads this tick's output."""
    order = sorted(planned, key=lambda name: started.get(name, 0.0))
    moved = True
    while moved:
        moved = False
        for reader, writers in reads.items():
            for writer in writers:
                if reader in order and writer in order and order.index(writer) > order.index(reader):
                    order.remove(writer)
                    order.insert(order.index(reader), writer)
                    moved = True
    return order


def _ago(seconds: float | None) -> str:
    if seconds is None:
        return "never started"
    hours = seconds / 3600
    return f"last started {hours:.1f} h ago" if hours < 48 else f"last started {hours / 24:.1f} days ago"


class Rotation:
    """The collectors of one tick: planned by the same `step(...)` calls, run by `rotation_order`."""

    def __init__(self, t: "Tick", record: Path):
        self.t, self.record = t, record
        self.planned: list[tuple[str, tuple]] = []

    def step(self, name: str, *argv) -> None:
        self.planned.append((name, argv))

    def load(self) -> dict[str, float]:
        try:
            import json
            doc = json.loads(self.record.read_text(encoding="utf-8"))
            return {str(k): float(v) for k, v in (doc.get("started") or {}).items()}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            log(f"rotation: {self.record.name} unreadable ({type(exc).__name__}) — the planned order this tick")
            return {}

    def save(self, started: dict[str, float]) -> None:
        try:
            import atomic
            atomic.write_json(self.record, {"started": started})
        except Exception as exc:  # noqa: BLE001 — losing the record costs fairness, never the tick
            log(f"rotation: could not keep {self.record.name} ({type(exc).__name__})")

    def run_in_turn(self) -> None:
        started = self.load()
        argv_of = dict(self.planned)
        planned = [name for name, _ in self.planned]
        order = rotation_order(planned, started)
        ahead = [name for name in order if order.index(name) < planned.index(name)]
        if ahead:
            now = time.time()
            log("rotation: " + ", ".join(
                f"{name} ({_ago(now - started[name] if name in started else None)})" for name in ahead)
                + " first — the collectors started least recently run first")
        self.t.order = order
        for name in order:
            if self.t.step(name, *argv_of[name]):
                started[name] = time.time()
                self.save(started)
            else:
                self.t.not_started.append(name)


class Tick:
    """One run's record: which steps failed, and where the report goes."""

    def __init__(self, scratch: Path):
        self.scratch = scratch
        self.failed: list[tuple[str, int]] = []
        self.order: list[str] = []
        self.not_started: list[str] = []

    def step(self, name: str, *argv) -> bool:
        """Run a step under the watchdog; log every line it prints under its name; record a
        non-zero exit as a failed step and go on. False when the watchdog did not start it."""
        proc = subprocess.Popen([PY, LEASE, "step", name, "--", *_text(argv)], cwd=ROOT,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace")
        first = None
        for line in proc.stdout:
            first = line if first is None else first
            log(f"{name}: {line.rstrip()}")
        rc = proc.wait()
        if rc != 0:
            log(f"{name}: EXIT {rc} — recorded as a failed step")
            self.failed.append((name, rc))
        return not (rc == TIMED_OUT and (first or "").startswith(NOT_STARTED))

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
                "collector_order": self.order,
                "not_started": self.not_started,
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

    # THE COLLECTORS take turns: each `step(...)` below plans; `rotation.run_in_turn()` runs them.
    import paths
    rotation = Rotation(t, paths.STATE / ROTATION_FILE)
    step = rotation.step

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
    # THE HARNESS: what every agent session loads, what it costs, which skills match a security rule
    # (docs/design/HARNESS.md). Files only; nothing runs.
    step("scan-harness", PY, "collectors/scan_harness.py", SCRATCH / "harness.json")
    step("scan-spend", PY, "collectors/scan_spend.py", SCRATCH / "spend.json")
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
    rotation.run_in_turn()
    step = t.step

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
