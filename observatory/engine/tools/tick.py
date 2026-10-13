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
what happened to each kept in `store/tick-rotation.json` (schema 2, `Rotation`): when it last
started — stamped BEFORE the step —, when it finished, how it ended (`ran`, `failed`, `stopped`,
`not_started`), how many ticks in a row it was not started and when it last ran to the end. A step
that was `not started` keeps its old start and goes first next tick; one that started goes to the
back. A collector that reads another's output in the same tick still runs after it (`READS_FROM`,
kept equal to the artefact graph and acyclic by `tests/test_pipeline.py`); a cycle there is named in
the log and the planned order runs, rather than the tick spinning on it.

**How a step ended is a class, not an exit code** (`outcome_of`): the watchdog's 124 means both a
step it did not start and one it stopped at its limit. Every `failed_steps` row in `tick.json`
carries `outcome` beside `exit`.

**The three early stops** — merge, emit, validate — go through `bail`, which writes the report
and then stops, so the most consequential outcomes leave a record. The one quiet exit is the
lease stand-down: another run holds the registry and the next tick is 30 minutes away.
"""
from __future__ import annotations

import math
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
#: Where the rotation keeps what happened to each collector (`Rotation`, schema 2).
ROTATION_FILE = "tick-rotation.json"
ROTATION_SCHEMA = 2
#: How a step ended (`outcome_of`). `ran` is the only one that produced the step's output.
OUTCOMES = ("ran", "failed", "stopped", "not_started")
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


def outcome_of(rc: int, first: str | None) -> str:
    """How a step ended, from the watchdog's exit and the first line the step printed.

    `not_started`: 124 and the watchdog's own `not started` line — no time was left for it;
    `stopped`: 124 otherwise (the watchdog stopped it at its limit), or a signal (128 + n, or the
    negative code of a process killed by one); `failed`: any other non-zero exit; `ran`: 0."""
    if rc == 0:
        return "ran"
    if rc == TIMED_OUT:
        return "not_started" if (first or "").startswith(NOT_STARTED) else "stopped"
    if rc < 0 or rc > 128:
        return "stopped"
    return "failed"


def reads_cycle(reads: dict[str, tuple[str, ...]], names) -> list[str]:
    """The steps of one cycle in `reads` among `names`, in order; [] when there is none.

    A cycle has no order in which every writer runs before its readers, so `rotation_order` could
    only spin on it (ENG-007). `tests/test_pipeline.py` asserts READS_FROM has none."""
    present = set(names)
    edges = {reader: [w for w in writers if w in present]
             for reader, writers in reads.items() if reader in present}
    state: dict[str, int] = {}                  # 1 on the current path, 2 finished
    path: list[str] = []

    def visit(node: str) -> list[str]:
        state[node] = 1
        path.append(node)
        for nxt in edges.get(node, ()):
            if state.get(nxt) == 1:
                return path[path.index(nxt):]
            if nxt not in state:
                found = visit(nxt)
                if found:
                    return found
        state[node] = 2
        path.pop()
        return []

    for node in sorted(edges):
        if node not in state:
            found = visit(node)
            if found:
                return list(found)
    return []


def rotation_order(planned: list[str], started: dict[str, float],
                   reads: dict[str, tuple[str, ...]] = READS_FROM) -> list[str]:
    """`planned` least recently started first; a step never started goes first, and ties keep the
    planned order. Then a writer that would run after a step reading its output is moved to just
    before that reader, so the starved reader stays early and still reads this tick's output. A
    start of None (never started) sorts as the oldest; only this ordering reads it as 0.

    Bounded: a cycle in `reads` among the planned steps has no such order, and the loop that moved
    writers forward used to spin on it for ever (ENG-007). A cycle is named in the log and the
    planned order runs. The len(planned)² cap on passes is a second guard that acyclic reads never
    reach — each pass that moves a writer settles one edge for good — kept so no later change to the
    loop can make it spin again."""
    cycle = reads_cycle(reads, planned)
    if cycle:
        log(f"rotation: READS_FROM has a cycle through {', '.join(cycle)} — the planned order this tick")
        return list(planned)
    order = sorted(planned, key=lambda name: started.get(name) or 0.0)
    for _ in range(max(len(order), 1) ** 2):
        moved = False
        for reader, writers in reads.items():
            for writer in writers:
                if reader in order and writer in order and order.index(writer) > order.index(reader):
                    order.remove(writer)
                    order.insert(order.index(reader), writer)
                    moved = True
        if not moved:
            return order
    log("rotation: the writers-before-readers order did not settle — the planned order this tick")
    return list(planned)


def _ago(seconds: float | None) -> str:
    if seconds is None:
        return "never started"
    hours = seconds / 3600
    return f"last started {hours:.1f} h ago" if hours < 48 else f"last started {hours / 24:.1f} days ago"


def _epoch(value) -> float | None:
    """A time from the record, or None: not a number, a bool, or not finite (JSON's Infinity, NaN)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


class Rotation:
    """The collectors of one tick: planned by the same `step(...)` calls, run by `rotation_order`.

    The record (`store/tick-rotation.json`), schema 2 — per collector:

        {"schema": 2, "collectors": {name: {"started_at": epoch, "finished_at": epoch,
            "outcome": "ran|failed|stopped|not_started", "not_started_streak": int,
            "last_ran_at": epoch|null}}}

    `started_at` is stamped BEFORE the step (schema 1 kept the time after it, so a slow collector
    looked recently started by its own duration) and saved, provisionally, before the step is
    called, so a tick killed inside a step still moves it back. A step not started keeps its
    previous `started_at` — null for one never started — and grows its `not_started_streak`, so it
    goes first next tick and a reader can tell starvation from rotation. `last_ran_at` is the start
    of the last attempt that ran to the end (`ran`): a failed or stopped attempt moves the collector
    back without producing its output. `outcome` null with `finished_at` null means the step was
    started and the tick was killed before it returned. Schema 1 (`{"started": {name: epoch}}`) is
    still read, its times seeding `started_at` and `last_ran_at` — schema 1 kept no outcome, so a
    seeded `last_ran_at` may belong to a run that failed —; the first save writes schema 2. A bad
    row is coerced or skipped, never the whole record; a field that is not a finite number reads as
    null. A schema that is an int past 2 (a newer engine's) is logged and left untouched: the
    planned order runs and nothing is written over it. Any other unreadable shape is logged and
    rewritten as schema 2 by the first save."""

    def __init__(self, t: "Tick", record: Path):
        self.t, self.record = t, record
        self.planned: list[tuple[str, tuple]] = []
        #: True when the record holds a schema this engine does not know (a newer engine's): it is
        #: then not rewritten this tick, so running an older engine never erases it.
        self.foreign = False

    def step(self, name: str, *argv) -> None:
        self.planned.append((name, argv))

    def _row(self, name: str, row) -> dict | None:
        """One schema-2 row, its fields coerced; None (skipped, and said) when it is not a row."""
        if not isinstance(row, dict):
            log(f"rotation: {self.record.name} row {name!r} is not a record — skipped")
            return None
        try:
            streak = max(int(row.get("not_started_streak") or 0), 0)
        except (TypeError, ValueError, OverflowError):       # "x", Infinity, NaN
            streak = 0
        return {"started_at": _epoch(row.get("started_at")),
                "finished_at": _epoch(row.get("finished_at")),
                "outcome": row.get("outcome") if row.get("outcome") in OUTCOMES else None,
                "not_started_streak": streak,
                "last_ran_at": _epoch(row.get("last_ran_at"))}

    def load(self) -> dict[str, dict]:
        try:
            import json
            doc = json.loads(self.record.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                raise TypeError("not an object")
            if doc.get("schema") == ROTATION_SCHEMA:
                collectors = doc.get("collectors") or {}
                if not isinstance(collectors, dict):
                    raise TypeError("collectors is not an object")
                rows = {str(name): self._row(str(name), row) for name, row in collectors.items()}
                return {name: row for name, row in rows.items() if row is not None}
            if "schema" not in doc and isinstance(doc.get("started"), dict):
                # Schema 1: the time it kept was taken when a step RETURNED, and only for a step that
                # was started — the nearest thing it holds to a start and to the last run, so it seeds
                # both and an upgrade does not read as every collector starved.
                rows = {}
                for k, v in doc["started"].items():
                    at = _epoch(v)
                    rows[str(k)] = {"started_at": at, "finished_at": None, "outcome": None,
                                    "not_started_streak": 0, "last_ran_at": at}
                return rows
            schema = doc.get("schema")
            if isinstance(schema, int) and not isinstance(schema, bool) and schema > ROTATION_SCHEMA:
                # A NEWER engine's record: kept as it is, so running an older engine never erases it.
                log(f"rotation: {self.record.name} unreadable (schema {schema}, newer than {ROTATION_SCHEMA}) "
                    f"— the planned order this tick, and the record is not rewritten")
                self.foreign = True
                return {}
            # Any other shape ({}, a string schema, schema 1 without `started`) is damage, not a newer
            # engine: pinning it would run the planned order every tick and starve the tail again.
            log(f"rotation: {self.record.name} unreadable (schema {schema!r}) — the planned order this tick; "
                f"it is rewritten as schema {ROTATION_SCHEMA}")
            return {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            log(f"rotation: {self.record.name} unreadable ({type(exc).__name__}) — the planned order this tick")
            return {}

    def save(self, rows: dict[str, dict]) -> None:
        if self.foreign:
            return                      # a newer engine's record: kept as it is (said once, in load)
        try:
            import atomic
            atomic.write_json(self.record, {"schema": ROTATION_SCHEMA, "collectors": rows})
        except Exception as exc:  # noqa: BLE001 — losing the record costs fairness, never the tick
            log(f"rotation: could not keep {self.record.name} ({type(exc).__name__})")

    def run_in_turn(self) -> None:
        rows = self.load()
        started = {name: row["started_at"] for name, row in rows.items()}
        argv_of = dict(self.planned)
        planned = [name for name, _ in self.planned]
        for name in planned:
            streak = (rows.get(name) or {}).get("not_started_streak") or 0
            if streak:
                log(f"rotation: {name} not started for {streak} tick{'s' if streak != 1 else ''}")
        order = rotation_order(planned, started)
        ahead = [name for name in order if order.index(name) < planned.index(name)]
        if ahead:
            now = time.time()
            log("rotation: " + ", ".join(
                f"{name} ({_ago(now - started[name] if started.get(name) is not None else None)})"
                for name in ahead) + " first — the collectors started least recently run first")
        self.t.order = order
        for name in order:
            row = rows.setdefault(name, {"started_at": None, "finished_at": None, "outcome": None,
                                         "not_started_streak": 0, "last_ran_at": None})
            previous = dict(row)
            before = time.time()
            # A PROVISIONAL row first: a tick killed inside this step still moves it to the back
            # next time, instead of running the same heavy step first for ever.
            row.update(started_at=before, finished_at=None, outcome=None)
            self.save(rows)
            outcome = self.t.step(name, *argv_of[name])
            after = time.time()
            row["outcome"] = outcome
            if outcome == "not_started":
                # It keeps the start it had — null for a collector never started — so it goes first
                # next tick, and its streak says how long it has waited.
                row.update(started_at=previous["started_at"], finished_at=previous["finished_at"],
                           not_started_streak=int(previous.get("not_started_streak") or 0) + 1)
                self.t.not_started.append(name)
            else:
                row.update(finished_at=after, not_started_streak=0)
                if outcome == "ran":
                    row["last_ran_at"] = before
            self.save(rows)


class Tick:
    """One run's record: which steps failed, and where the report goes."""

    def __init__(self, scratch: Path):
        self.scratch = scratch
        #: (step, exit) of every step that did not end `ran`, and how each ended (`outcome_of`).
        self.failed: list[tuple[str, int]] = []
        self.outcomes: dict[str, str] = {}
        self.order: list[str] = []
        self.not_started: list[str] = []

    def step(self, name: str, *argv) -> str:
        """Run a step under the watchdog; log every line it prints under its name; record a
        non-zero exit as a failed step and go on. Returns how it ended (`outcome_of`)."""
        proc = subprocess.Popen([PY, LEASE, "step", name, "--", *_text(argv)], cwd=ROOT,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace")
        first = None
        for line in proc.stdout:
            first = line if first is None else first
            log(f"{name}: {line.rstrip()}")
        rc = proc.wait()
        outcome = outcome_of(rc, first)
        if rc != 0:
            log(f"{name}: EXIT {rc} — recorded as a failed step ({outcome.replace('_', ' ')})")
            self.failed.append((name, rc))
            self.outcomes[name] = outcome
        return outcome

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
                # `exit` for the readers that know only it; `outcome` tells a step not started from
                # one stopped at its limit, which share the watchdog's 124.
                "failed_steps": [{"step": s, "exit": rc, "outcome": self.outcomes.get(s) or outcome_of(rc, None)}
                                 for s, rc in self.failed],
                "collector_order": self.order,
                "not_started": self.not_started,
            })
        except Exception:  # noqa: BLE001 — the report is best effort; the log says it is missing
            log("tick: could not record the step report")

    def bail(self, name: str, rc: int, message: str, first: str | None = None) -> None:
        """Stop the tick at `name`, and leave the report that says so. Exits 0: a scheduler must
        not retry a state that has been reported. `first` is what the step printed (its first line
        decides `not_started` from `stopped` under the watchdog's 124)."""
        log(message)
        self.failed.append((name, rc))
        self.outcomes[name] = outcome_of(rc, (first or "").splitlines()[0] if first else None)
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
    # Each bails with its OWN exit and first line, so a stopped or never-started merge is not
    # reported as a failure (verification round 2: a literal 1 read every bail as `failed`).
    rc, out = bounded("merge", PY, "collectors/merge.py", SCRATCH)
    if rc != 0:
        t.bail("merge", rc, "merge failed — stopping, the registry is not rewritten", first=out)
    rc, out = bounded("emit", PY, "collectors/emit_registry.py", SCRATCH)
    if rc != 0:
        t.bail("emit", rc, "emit failed or REFUSED a wholesale change — the registry keeps the last good version",
               first=out)
    rc, out = bounded("validate", PY, "tools/validate_registry.py")
    if rc != 0:
        t.bail("validate", rc, "VALIDATOR RED — the registry is not projected and the agent is not called", first=out)
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
