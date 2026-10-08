"""The time a tick step has left, read from the deadline its runner handed it.

WHY. A step that does not know its watchdog's moment can only be killed by it,
and a killed collector writes nothing: measured 2026-10-04..07, `scan-fs` reached
its 900 s limit on 14 ticks while `integrity_check` over a 70 MB store took up to
266 s — the disk was starved, not the collector broken — and each time the whole
scan was lost. `tools/tick_lease.bounded` now sets `OBSERVATORY_STEP_DEADLINE`
(epoch seconds) in every step's environment; a step that reads it here can stop
in time and write a partial, honestly labelled answer (OBS-40).

Outside a tick the variable is absent and the budget is unlimited, so a step run
by hand behaves exactly as before.
"""
from __future__ import annotations

import math
import os
import time

DEADLINE_ENV = "OBSERVATORY_STEP_DEADLINE"


class OutOfTime(Exception):
    """The step's budget does not cover the next unit of work."""


def deadline() -> float | None:
    raw = os.environ.get(DEADLINE_ENV, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def remaining() -> float:
    """Seconds until the step's watchdog fires; infinity with no deadline."""
    end = deadline()
    return math.inf if end is None else end - time.time()


def clip(timeouts: tuple[float, ...], reserve: float, backoff: float = 0.0) -> tuple[float, ...]:
    """The attempts of a retried command that fit in the budget, less `reserve`.

    Each attempt is cut to what is left; attempts that would start with nothing
    left are dropped. `backoff` is `slow_command`'s pause rule (`backoff * 2**n`
    before attempt n + 1), so the pauses are counted too. Raises OutOfTime when
    not even one second remains, so the caller stops BEFORE starting work it
    cannot finish."""
    left = remaining() - reserve
    if left < 1:
        raise OutOfTime(f"{max(left + reserve, 0):.0f} s left before the step's limit")
    fitted: list[float] = []
    for n, limit in enumerate(timeouts):
        if n:
            left -= backoff * (2 ** (n - 1))
        if left < 1:
            break
        fitted.append(min(limit, left))
        left -= limit
    return tuple(fitted)
