#!/usr/bin/env python3
"""The guard's short history of what grows on the host disk (OBS-620): each VM disk file and the swap.

On 2026-11-17 a container log inside a Colima VM grew to 10.7 GB and filled the host disk twice, while swap held
about 15 GB of the same APFS container. Neither showed as growth: the free-space series knows one number a day, and
the machine survey sizes its places every few weeks. Every guard pass records here the space each VM disk file takes
(its allocated size) and the swap in use, one sample a key, and keeps `KEEP_HOURS` of them, so a pass can say how
much each grew over the last day.

The file holds numbers and keys only (`colima/default/datadisk`, `swap`), never a path. A missing, unreadable or
malformed file reads as no history (the growth is then unknown, never zero); a write that fails raises `OSError` and
the guard names it in its `degraded`.
"""
from __future__ import annotations

import json
import math
import os
import pathlib
import re

import atomic
import paths

#: Hours of samples kept for each key; a key not read for this long is dropped.
KEEP_HOURS = 48
#: Samples kept for each key (a pass every 15 minutes makes 192 in 48 hours).
KEEP_SAMPLES = 240
#: Keys kept at most: a VM made and deleted every day leaves no unbounded file.
KEEP_KEYS = 64
#: The window growth is measured over, and the shortest window a rate is claimed from.
WINDOW_HOURS = 24
MIN_WINDOW_HOURS = 1.0
#: A sample this much older than the window still counts as the day's start (passes are not exactly on time).
SLACK_HOURS = 1.0
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")


def series_file() -> pathlib.Path:
    return paths.STATE / "disk-growth-series.json"


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def read(path: pathlib.Path | None = None) -> dict[str, list[list[float]]]:
    """Each key's samples `[at, gb]`, oldest first. What cannot be read is an empty history."""
    try:
        doc = json.loads((path or series_file()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    keys = doc.get("keys") if isinstance(doc, dict) else None
    out: dict[str, list[list[float]]] = {}
    for key, samples in (keys.items() if isinstance(keys, dict) else ()):
        if not isinstance(key, str) or not KEY.fullmatch(key) or not isinstance(samples, list):
            continue
        rows = []
        for s in samples:
            if isinstance(s, list) and len(s) == 2 and _number(s[0]) is not None and _number(s[1]) is not None:
                rows.append([float(s[0]), float(s[1])])
        if rows:
            out[key] = sorted(rows)[-KEEP_SAMPLES:]
    return out


def record(readings: dict, at: float, path: pathlib.Path | None = None) -> dict[str, list[list[float]]]:
    """Add one reading a key at `at` (epoch seconds), drop what is older than `KEEP_HOURS`, write; returns the series.
    A reading that is not a number adds nothing. Raises OSError when the file cannot be written."""
    series = read(path)
    for key, gb in (readings or {}).items():
        value = _number(gb)
        if value is None or not isinstance(key, str) or not KEY.fullmatch(key):
            continue
        series.setdefault(key, []).append([float(at), round(value, 3)])
    floor = at - KEEP_HOURS * 3600
    kept = {k: [s for s in v if s[0] >= floor][-KEEP_SAMPLES:] for k, v in series.items()}
    kept = {k: v for k, v in kept.items() if v}
    if len(kept) > KEEP_KEYS:
        newest = sorted(kept, key=lambda k: -kept[k][-1][0])[:KEEP_KEYS]
        kept = {k: kept[k] for k in newest}
    target = path or series_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json(target, {"schema_version": 1, "keys": kept})
    os.chmod(target, 0o600)
    return kept


def growth(samples, at: float) -> dict | None:
    """How much a key grew from the oldest sample of the last day to the newest: `{gb, window_hours, gb_per_day}`,
    or None when the samples span less than `MIN_WINDOW_HOURS` (the first passes claim no rate). `gb` is what was
    measured over `window_hours`; `gb_per_day` scales it to a day."""
    rows = [s for s in samples or [] if isinstance(s, list) and len(s) == 2 and s[0] <= at]
    if len(rows) < 2:
        return None
    floor = at - (WINDOW_HOURS + SLACK_HOURS) * 3600
    inside = [s for s in rows if s[0] >= floor]
    if len(inside) < 2:
        return None
    first, last = inside[0], inside[-1]
    hours = (last[0] - first[0]) / 3600
    if hours < MIN_WINDOW_HOURS:
        return None
    delta = last[1] - first[1]
    return {"gb": round(delta, 2), "window_hours": round(hours, 1), "gb_per_day": round(delta * 24 / hours, 2)}
