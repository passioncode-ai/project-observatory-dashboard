"""Is the scheduled tick alive? Read from the tick's own receipts, never by running it.

Findings are built INSIDE the tick, so a tick that died (the machine rebooted
mid-run) or ticks that stopped altogether can't report themselves: the last
report stays on disk and reads as current. This module answers from outside,
for the places that are read while no tick runs: `/health` (tools/serverd.py),
`full doctor` (workspace.py) and every MCP survey's `degraded` (survey.py).
PB-132.

Inputs, all written by the tick itself:

- `store/raw/tick-lease.json`: `last_acquired_at`, when the last tick started
  (tools/tick_lease.py);
- `store/raw/tick.json`: `finished_at`, when the last tick reached its end or
  stopped at a step (tools/tick.sh `write_report`);
- `store/tick.lock`: held with an exclusive flock for as long as a tick runs
  (tools/tick_lease.py `supervised`).

The lock is probed with a non-blocking shared flock and released at once. The
probe never creates the file. If a tick starts in that instant it skips one run
and says so in its log; nothing else is affected.
"""
from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: Longer than any tick measured (about 2.5 hours on a loaded machine before
#: the incremental store scans) and short enough that a dead scheduler shows
#: the same day.
STALE_AFTER = timedelta(hours=6)

VERDICTS = ("ok", "running", "running-long", "interrupted", "stale", "never", "disabled")


def _stamp(path: Path, *keys: str) -> datetime | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for key in keys:
        value = doc.get(key) if isinstance(doc, dict) else None
        if value:
            try:
                return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                return None
    return None


def lock_held(lock: Path) -> bool:
    """True while a tick holds the workspace lock."""
    try:
        fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def health(state: Path, scratch: Path, *, scheduler_enabled: bool = True,
           now: datetime | None = None) -> dict:
    """{"verdict", "why", "last_started", "last_finished"}; see VERDICTS."""
    now = now or datetime.now(timezone.utc)
    started = _stamp(scratch / "tick-lease.json", "last_acquired_at")
    finished = _stamp(scratch / "tick.json", "finished_at")
    out = {"last_started": started and started.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "last_finished": finished and finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "recent": recent(scratch)}
    if not scheduler_enabled:
        return {**out, "verdict": "disabled", "why": "features.scheduler is off in this workspace"}
    if lock_held(state / "tick.lock"):
        if started and now - started > STALE_AFTER:
            return {**out, "verdict": "running-long",
                    "why": f"a tick has held the workspace lock since {out['last_started']}"}
        return {**out, "verdict": "running", "why": "a tick holds the workspace lock"}
    if not started and not finished:
        return {**out, "verdict": "never", "why": "no tick has run in this workspace"}
    if started and (not finished or finished < started):
        return {**out, "verdict": "interrupted",
                "why": f"the tick that started at {out['last_started']} never finished and holds no lock: "
                       "it was killed (a restart, a sleep, a signal); its report is from the tick before"}
    if finished and now - finished > STALE_AFTER:
        return {**out, "verdict": "stale",
                "why": f"no tick has finished since {out['last_finished']}; is the scheduler installed and loaded?"}
    return {**out, "verdict": "ok", "why": "the last tick finished and none is overdue"}


#: One line per tick run (`tools/tick_lease.record_run`), the newest last. The last run
#: alone hid a pattern: 42 ticks stopped at their ceiling on one machine while Health read
#: "ok" after every one that happened to finish (OBS-40).
RUNS_FILE = "tick-runs.jsonl"
KEEP_RUNS = 50
#: How many recent runs Health summarises, and how many of them not finishing is a gap.
RECENT_RUNS = 10
INCOMPLETE_ALERT = 3


def record(scratch: Path, run: dict) -> None:
    """Append one run's outcome, keeping the newest KEEP_RUNS. Never raises: a run that
    cannot be recorded is a missing line, not a failed tick."""
    try:
        f = scratch / RUNS_FILE
        lines = f.read_text(encoding="utf-8").splitlines() if f.is_file() else []
        lines = (lines + [json.dumps(run, sort_keys=True)])[-KEEP_RUNS:]
        import atomic
        atomic.write_text(f, "\n".join(lines) + "\n")
    except Exception:  # noqa: BLE001
        pass


def recent(scratch: Path, last: int = RECENT_RUNS) -> dict:
    """{"runs", "incomplete", "outcomes"} over the newest `last` recorded runs."""
    outcomes: dict[str, int] = {}
    try:
        lines = (scratch / RUNS_FILE).read_text(encoding="utf-8").splitlines()[-last:]
    except OSError:
        lines = []
    for line in lines:
        try:
            outcome = str(json.loads(line).get("outcome") or "unknown")
        except (ValueError, AttributeError):
            outcome = "unreadable"
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    runs = sum(outcomes.values())
    return {"runs": runs, "incomplete": runs - outcomes.get("finished", 0), "outcomes": outcomes}


def degraded(state: Path, scratch: Path, *, scheduler_enabled: bool = True) -> list[dict]:
    """A `degraded` entry when the data may be older than it looks, else []."""
    h = health(state, scratch, scheduler_enabled=scheduler_enabled)
    out = []
    if h["verdict"] in ("interrupted", "stale", "running-long"):
        out.append({"source": "tick", "reason": f"{h['verdict']}: {h['why']}"})
    r = h.get("recent") or {}
    if scheduler_enabled and r.get("incomplete", 0) >= INCOMPLETE_ALERT:
        how = ", ".join(f"{k}: {v}" for k, v in sorted(r["outcomes"].items()) if k != "finished")
        out.append({"source": "tick-history", "reason": (
            f"{r['incomplete']} of the last {r['runs']} ticks did not finish ({how}); the registry, "
            f"findings and dashboard may lag behind the estate — `store/logs/tick.log` names the slow steps")})
    return out
