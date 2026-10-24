"""How the tick's time is shared out, in one place the tick, its watchdog and its findings read.

WHY (OBS-52, 2026-10-21). One installed tick timed out on 28 of its last 50 runs. The leak scan
took a median 1125 s of the 1500 s ceiling and was stopped on most runs, and eight collectors
(the Cloudflare and spend scans, remote env, Google, the machine survey, git hygiene, the cleanup
and the GitHub scan) were `not started` 28 times. Two causes, both closed here:

* **No step had a share.** A collector's limit was its own wall clock (900 s, 1200 s for the leak
  scan) or the time left before the tail's reserve, whichever came first, so one slow collector
  could take everything the collectors had. Now a collector's limit is also at most its share of
  the ceiling (`share_limit`): `COLLECTOR_SHARE` for any collector, `STEP_SHARES` for the ones
  named there. The leak scan resumes where it stopped (`tools/scan_leaks.py`): a file from its
  offset, and a complete pass over the databases from how far it read each table, the databases it
  has not finished first. So a bounded share costs it time; coverage it reaches over several ticks.
* **A disabled reader pulled its heavy writer to the front.** A step whose feature is off keeps
  its old start (so it never looks freshly measured), which made it sort as the most starved
  collector of all; the rotation then moved the step it reads from before it. The disabled
  companion scrub dragged the leak scan, and the disabled Bitbucket listing dragged the folder
  scan, to the head of every tick. `tools/tick.py` now orders by `priority`: a collector not
  started for `STARVED_TICKS` ticks first, a disabled one last.

WHY THE TAIL'S TIME IS MEASURED (OBS-220, 2026-10-24). On one installed 0.29.0, 10 of its last
40 ticks reached the ceiling under a load average above 70. The collectors kept their flat 300 s
reserve, but the tail spent it: on the tick that started 2026-10-24T00:42:45Z `events` took 211 s
of it, the optional steps (`index`, `ledger`, `rollup` among them) were `not started`, `findings`
was stopped at the 17 s that were left and the dashboard did not run. The 300 s was measured on a
quiet machine (106 s, 2026-10-07); the core steps alone measure more at their 90th percentile under
load. So the tail's time is now what the tail has measured (`needs`, from `store/raw/tick-runs.jsonl`):

* **The collectors leave the measured tail** (`collector_reserve`): the 90th percentile of each of
  the `RESERVED` steps' recent runs, times `NEED_MARGIN`, never less than the old flat reserve and
  never more than `RESERVE_CAP` of the ceiling.
* **Every tail step leaves the steps that must finish after it their measured time**
  (`reserve_after`, `tail_limit`): `events` stops early rather than starve `findings`; a step in
  `MUST_FINISH` may still use its own measured time when less is left, so `findings` is never
  stopped at a fixed few seconds when it has needed more. When `merge` or `emit` is stopped,
  `tools/tick.py` runs `findings` and the dashboard on the previous registry instead of stopping.

`ALERT_FRACTION`, `ALERT_WINDOW` and `ALERT_MIN` say when one step's runs are reported
(`tick.step_over_share`, tools/tick_findings.py), and `STARVED_TICKS` is also where `tick.collector_starved` begins. The
ceiling itself is read here too (`ceiling_seconds`), so the watchdog and the receipt that the
findings judge name the same number. Nothing here reads the clock.
"""
from __future__ import annotations

import math
import os

#: The whole tick's ceiling, below the 30-minute interval (lifecycle LC-03: a watchdog shorter
#: than the interval). `install_launchd.build` lowers it for a shorter interval and writes it into
#: the plist as CEILING_ENV.
DEFAULT_CEILING_SECONDS = 1500
CEILING_ENV = "OBSERVATORY_TICK_CEILING_SECONDS"


def ceiling_seconds() -> float:
    """The tick's ceiling in seconds: CEILING_ENV when it is a number from 1 to 3600, else the default."""
    raw = os.environ.get(CEILING_ENV, "").strip()
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_CEILING_SECONDS)
    return value if 1 <= value <= 3600 else float(DEFAULT_CEILING_SECONDS)


#: The most of the tick's ceiling any one collector may take. With the 1500 s ceiling that is
#: 600 s: the slowest collectors measured besides the leak scan (the folder scan 453 s, git
#: hygiene 418 s) fit, and two of them cannot fill the collectors' whole window.
COLLECTOR_SHARE = 0.4
#: Collectors with a smaller share of their own. The leak scan resumes where it stopped, so it
#: takes 30 % (450 s of 1500 s) and finishes its backlog over several ticks. The `.env` inventory
#: (OBS-220) walked the whole estate root in one pass, 38 s on a quiet machine and 599 s under a
#: load average above 70, so it took the 40 % every collector may; it now resumes its walk and its
#: git reads where it stopped (collectors/scan_env.py) and takes 20 % (300 s of 1500 s).
STEP_SHARES: dict[str, float] = {"leaks": 0.3, "env": 0.2}
#: A collector not started for this many ticks in a row goes first, and is reported as starved.
STARVED_TICKS = 3
#: A step is reported (`tick.step_over_share`) when it used this fraction of its own share of the
#: ceiling (`share`: a collector's, so one that keeps inside its share never fires; a tail step is
#: judged by the collector share) in ALERT_MIN of the last ALERT_WINDOW ticks, so one slow tick does
#: not make the finding come and go (review of #328, L2).
#: Collectors that stop themselves before their share runs out and resume where they stopped: they
#: use their whole share by design whenever there is a backlog, so `tick.step_over_share` does not
#: judge them (confirming review of #328: the leak scan stops its databases at about 435 s of its
#: 450 s, above 95 % of its share). Their backlog is reported by their own findings.
RESUMABLE = frozenset({"leaks", "env", "remote-env"})
ALERT_FRACTION = 0.95
ALERT_WINDOW = 5
ALERT_MIN = 3


def share(name: str) -> float:
    """The share of the ceiling collector `name` may take."""
    return STEP_SHARES.get(name, COLLECTOR_SHARE)


def share_limit(name: str, ceiling: float) -> float:
    """Seconds collector `name` may run in a tick whose ceiling is `ceiling` seconds."""
    return share(name) * float(ceiling)


def priority(row: dict | None) -> int:
    """Where a collector's rotation row puts it: 0 starved, 1 in turn, 2 disabled last tick.

    A disabled step costs nothing and measures nothing, so it never needs to go early, and it
    must not pull the steps it reads from forward. One whose feature is switched back on runs
    last once, then by its streak and start like every other."""
    row = row or {}
    if row.get("outcome") == "disabled":
        return 2
    try:
        streak = int(row.get("not_started_streak") or 0)
    except (TypeError, ValueError, OverflowError):
        streak = 0
    return 0 if streak >= STARVED_TICKS else 1


# region tail: the time the steps after the collectors need, measured (OBS-220)

#: THE TAIL, in the order `tools/tick.py` runs it. `tests/test_tick_budget.py` reads the tick's own
#: step calls and fails when the two disagree.
TAIL_ORDER: tuple[str, ...] = (
    "merge", "emit", "validate", "events", "snapshot", "diff", "fabric-attribution", "index",
    "retention", "sweep", "corroborate", "ledger", "lost", "plugins", "rollup", "agent", "findings",
    "dashboard", "smoke", "findings-recheck", "notify", "registry", "project-into-vault", "projection",
    "links", "logs",
)
#: The steps that finish every tick: what turns the collectors' facts into the registry, the
#: findings, the page and the notice, and the indexes a reader of them asks (`index`, `ledger`,
#: `rollup`). Every step before one of them leaves it its measured time (`reserve_after`).
MUST_FINISH = frozenset({
    "merge", "emit", "validate", "index", "ledger", "rollup", "findings", "dashboard", "smoke",
    "notify", "registry", "project-into-vault", "projection", "links", "logs",
})
#: What the collectors leave the tail: the steps that must finish, and the change of the tick
#: (`events`, `snapshot`, `diff`), which yields to them inside the tail but is reserved for here.
RESERVED = MUST_FINISH | {"events", "snapshot", "diff"}
#: A step's time before it has a history: a quiet machine's, rounded up. Their sum times
#: NEED_MARGIN stays under the old flat 300 s, so a new workspace keeps the reserve it had.
DEFAULT_NEED: dict[str, float] = {
    "merge": 20, "emit": 15, "validate": 3, "events": 40, "snapshot": 5, "diff": 3, "index": 3,
    "ledger": 3, "rollup": 3, "findings": 20, "dashboard": 15, "smoke": 5, "notify": 3,
    "registry": 3, "project-into-vault": 3, "projection": 3, "links": 5, "logs": 3,
}
#: How many recent runs a step's need is read from, how many of its durations it takes before the
#: history replaces DEFAULT_NEED, and which quantile of them is its need.
NEED_RUNS = 20
NEED_MIN_SAMPLES = 3
NEED_QUANTILE = 0.9
#: Headroom over a measured need: the 90th percentile is not the worst run.
NEED_MARGIN = 1.25
#: The least a step that must finish is given, whatever its history says: a step that takes a second
#: on a quiet machine takes several under load, process start included, and a few seconds short of
#: that stops it (review of #352: `validate` measured 3 to 4 s under load and was handed under 2 s
#: after a 270 s emit). A step not named here gets MUST_FINISH_MIN_DEFAULT.
MUST_FINISH_MIN: dict[str, float] = {"validate": 10.0, "findings": 30.0, "dashboard": 20.0}
MUST_FINISH_MIN_DEFAULT = 5.0
#: The most of the ceiling the collectors leave the tail, so a slow history cannot starve them.
RESERVE_CAP = 0.4


def _seconds(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def needs(runs, *, defaults: bool = True) -> dict[str, float]:
    """Seconds each RESERVED step needs: the NEED_QUANTILE of its durations over `runs` (stage receipts,
    `stage_progress`, oldest first; the newest NEED_RUNS are read), a run it `ran` or was `stopped` in
    (a stopped run needed at least that long). DEFAULT_NEED for a step with fewer than NEED_MIN_SAMPLES,
    or no entry at all with `defaults=False`. Rows that are not stage rows are skipped; nothing raises."""
    seen: dict[str, list[float]] = {}
    for run in list(runs or ())[-NEED_RUNS:]:
        stages = run.get("stages") if isinstance(run, dict) else None
        for row in stages if isinstance(stages, list) else ():
            if not isinstance(row, dict) or row.get("outcome") not in ("ran", "stopped"):
                continue
            name, took = row.get("step"), _seconds(row.get("duration_seconds"))
            if name in RESERVED and took is not None:
                seen.setdefault(name, []).append(took)
    out = {}
    for name in RESERVED:
        values = sorted(seen.get(name, ()))
        if len(values) < NEED_MIN_SAMPLES:
            if defaults:
                out[name] = float(DEFAULT_NEED.get(name, 5))
        else:
            rank = max(1, math.ceil(round(NEED_QUANTILE * len(values), 9)))   # the nearest rank
            out[name] = values[min(rank, len(values)) - 1]
    return out


def given(name: str, need: dict[str, float]) -> float:
    """Seconds step `name` is reserved and, if it must finish, always given: its need times
    NEED_MARGIN, and for a MUST_FINISH step at least its minimum (MUST_FINISH_MIN)."""
    seconds = NEED_MARGIN * need.get(name, DEFAULT_NEED.get(name, 5))
    if name in MUST_FINISH:
        seconds = max(seconds, MUST_FINISH_MIN.get(name, MUST_FINISH_MIN_DEFAULT))
    return seconds


def collector_reserve(measured_need: dict[str, float], ceiling: float, floor: float) -> float:
    """Seconds before the ceiling the collectors leave the tail: what the RESERVED steps that have a
    history (`needs(runs, defaults=False)`) are given (`given`), at least `floor` (the old flat reserve,
    which covers a step not measured yet) and at most RESERVE_CAP of the ceiling."""
    measured = sum(given(name, measured_need) for name in RESERVED if name in measured_need)
    return min(max(float(floor), measured), max(float(floor), RESERVE_CAP * float(ceiling)))


def reserve_after(name: str, need: dict[str, float]) -> float:
    """Seconds tail step `name` leaves the MUST_FINISH steps that run after it (`given`)."""
    try:
        at = TAIL_ORDER.index(name)
    except ValueError:
        return 0.0
    return sum(given(later, need) for later in TAIL_ORDER[at + 1:] if later in MUST_FINISH)


def tail_limit(name: str, left: float, need: dict[str, float], floor: float = 0.0) -> float:
    """Seconds tail step `name` may run with `left` seconds before the ceiling.

    Any tail step leaves the MUST_FINISH steps after it their measured time, and at least `floor`
    (the optional steps' old flat reserve for the core). A MUST_FINISH step may still take its own
    measured time out of what is left, so it is never stopped at a few seconds it has never been
    enough: the reserve before it was meant for it, and it is never less than the step's minimum
    (MUST_FINISH_MIN) while that much is left."""
    limit = left - max(reserve_after(name, need), float(floor))
    if name in MUST_FINISH:
        limit = max(limit, min(left, given(name, need)))
    return limit
# endregion tail
