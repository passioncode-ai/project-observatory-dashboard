#!/usr/bin/env python3
"""An external command that times out once is asked again before it is reported.

WHY. A collector that runs a CLI under a fixed timeout reports the first timeout
as the source's state. On a machine whose load average stood near 200, `heroku
auth:token` took 31 s on one tick and 8-13 s on the next three tries, and `git
status` in a small checkout missed 25 s once and then answered in 0.3 s. Each of
those single misses held the whole service degraded, with a reason that read as a
broken source and asked a person to act on a spike that had already passed.

WHAT IT DOES. One call, several attempts: each attempt gets its own timeout from
`timeouts`, a pause of `backoff * 2**n` seconds separates them, and only a command
that misses every attempt is reported. The report names every duration it was
given and the load average at the end, because those two facts are what tell a
slow machine from a hung tool. A command that RUNS and fails is returned at once:
retrying a refusal would only repeat it.

    proc, why = slow_command.run(["git", "status"], timeouts=(25, 60), capture_output=True)
"""
from __future__ import annotations

import os
import subprocess
import time
from typing import Callable, Sequence


def _seconds(value: float) -> str:
    return f"{value:g}s"


def load_average() -> str:
    try:
        one, five, _ = os.getloadavg()
    except (OSError, AttributeError):
        return "load average unknown"
    return f"load average {one:.1f} (5-minute {five:.1f})"


def describe(args: Sequence[str], tried: Sequence[float], backoffs: Sequence[float]) -> str:
    """The reason a command never answered, with every attempt it was given."""
    shown = " ".join(str(a) for a in list(args)[:3])
    head = f"`{shown}` did not finish in {_seconds(tried[0])}"
    for limit, pause in zip(tried[1:], backoffs):
        head += f", nor in {_seconds(limit)} when asked again {_seconds(pause)} later"
    return f"{head}; {load_average()}"


def run(args: Sequence[str], *, timeouts: Sequence[float] = (25, 60), backoff: float = 3.0,
        sleep: Callable[[float], None] = time.sleep,
        **kwargs) -> tuple[subprocess.CompletedProcess | None, str]:
    """(the completed process, "") or (None, why it never answered).

    `OSError` (the tool is missing or cannot start) propagates to the caller,
    which already names that case in its own words.
    """
    if not timeouts:
        raise ValueError("at least one timeout is required")
    tried: list[float] = []
    pauses: list[float] = []
    for attempt, limit in enumerate(timeouts):
        try:
            return subprocess.run(list(args), timeout=limit, **kwargs), ""
        except subprocess.TimeoutExpired:
            tried.append(limit)
            if attempt + 1 < len(timeouts):
                pause = backoff * (2 ** attempt)
                pauses.append(pause)
                sleep(pause)
    return None, describe(args, tried, pauses)
