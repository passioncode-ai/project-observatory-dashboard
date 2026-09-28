#!/usr/bin/env python3
"""Did the thing under test write that, or did the scheduled tick?

Four instances of one class were found, one at a time:

* `observatory.py`'s purity verdict, hardened first with `culprit_of()` and
  `FOREIGN_WRITES_IGNORED`, because a gate that reports the wrong cause is a gate
  people argue with;
* `tests/test_traps.py` T4, which accused `emit` of non-determinism when the tick
  rewrote the scratch reports between two readings;
* `tools/run_probes.py`, which could publish a side-effect FAIL against a
  read-only capability, in the receipt a Fabric host reads;
* `tests/test_gate_purity.py`, which hashes the scratch directory around a suite
  run with a long timeout and names the SUITE when anything moved. The tick
  writes that directory every thirty minutes, so this one sends a reader looking
  for a redirect bug in a suite that has none.

This module exists because the fourth proved the class is not incidental, and a
fifth reader should not have to rediscover the discriminator.

**The discriminator is evidence, not a heuristic.** The tick's log grows
monotonically and every line carries a UTC stamp, so its size before and after a
window says whether a tick RAN in that window. That is a positive fact about the
actual writer, unlike the third-reading trick `run_probes` uses, which is all
that is available there, since a probe cannot know whether the tick will write
again.

Where the log cannot be read, `tick_ran()` returns None and the caller must
report that it could not tell. "The tick did not run" and "nobody could ask" are
different facts, and collapsing them is how a check starts passing for the wrong
reason.
"""
from __future__ import annotations
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import paths                                                          # noqa: E402

#: The tick's own log. `tools/tick.sh` appends to `$STATE/logs/tick.log`, one UTC
#: stamp per line, from every step, so its LENGTH is a monotone clock for "a tick
#: did something". It follows the selected state directory, which is the store
#: unless OBSERVATORY_STATE redirects it.
TICK_LOG = paths.STATE / "logs" / "tick.log"


def tick_mark() -> int | None:
    """A comparable mark for the tick's progress, or None if it cannot be read."""
    try:
        return TICK_LOG.stat().st_size
    except OSError:
        return None


def tick_ran(before: int | None, after: int | None) -> bool | None:
    """True if a tick wrote during the window, False if not, None if unknowable.

    THREE OUTCOMES. A missing or unreadable log means the question was not
    answered, and a caller that treats that as "no tick ran" would attribute the
    tick's writes to whatever it was testing, the exact defect this module
    exists for, one level up.
    """
    if before is None or after is None:
        return None
    return after > before


def attribute(moved: list[str], ran: bool | None, subject: str) -> tuple[bool, str]:
    """(is it the subject's fault, why) for a set of paths that changed.

    `moved` is what changed, `ran` is `tick_ran()`'s answer, `subject` is what
    was under test. Returns False with a reason whenever the change cannot be
    pinned on the subject, which is not the same as the subject being clean, and
    the reason says so.
    """
    # THE DAEMON'S OWN HEARTBEAT IS NOT A TEST'S WRITE. `tools/serverd.py` runs
    # as a background service and rewrites `serverd.json` every thirty seconds,
    # and no pipeline step writes it. A suite that happens to take longer than
    # one beat was being reported as dirtying the scratch directory: the same
    # misattribution class as the tick above, from a writer that never stops
    # rather than one that runs on a schedule.
    moved = [m for m in moved if pathlib.Path(m).name != "serverd.json"]
    if not moved:
        return False, "nothing moved that a test could have written"
    if ran is True:
        return False, (f"the scheduled tick wrote during this window — "
                       f"{TICK_LOG.name} grew — so {', '.join(moved[:4])} "
                       f"{'and more ' if len(moved) > 4 else ''}is not shown to be "
                       f"{subject}'s. Re-run when no tick is due to test {subject} "
                       f"itself.")
    if ran is None:
        return False, (f"{TICK_LOG.name} could not be read, so it is unknown "
                       f"whether the tick wrote during this window: "
                       f"{', '.join(moved[:4])} is unattributed, not excused.")
    return True, (f"{subject} changed {', '.join(moved[:6])}"
                  + (f" and {len(moved) - 6} more" if len(moved) > 6 else "")
                  + " while no tick ran")
