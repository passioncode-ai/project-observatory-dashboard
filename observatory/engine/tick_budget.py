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

`ALERT_FRACTION`, `ALERT_WINDOW` and `ALERT_MIN` say when one step's runs are reported
(`tick.step_over_share`, tools/tick_findings.py), and `STARVED_TICKS` is also where `tick.collector_starved` begins. The
ceiling itself is read here too (`ceiling_seconds`), so the watchdog and the receipt that the
findings judge name the same number. Nothing here reads the clock.
"""
from __future__ import annotations

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
#: takes 30 % (450 s of 1500 s) and finishes its backlog over several ticks.
STEP_SHARES: dict[str, float] = {"leaks": 0.3}
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
RESUMABLE = frozenset({"leaks"})
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
