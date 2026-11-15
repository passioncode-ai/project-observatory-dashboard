#!/usr/bin/env python3
"""A small daily free-space series for the Disk page (OBS-572).

The guard knows the free space of one pass; the page wants the days before it. Every guard run, due or not,
records what the disk reported into one row per UTC day (the lowest and the last reading, how many readings)
in `store/disk-free-series.json`, and keeps the newest `KEEP_DAYS` rows, so the file stays a few kilobytes
however long the machine runs. It holds numbers only: no path, no name.

A missing, unreadable or malformed file reads as an empty series (the page says no history), never as zero
free space; a write that fails raises `OSError` and the guard names it in its `degraded` and goes on, because it
runs exactly when the disk may be full.
"""
from __future__ import annotations

import json
import os
import pathlib
from datetime import datetime, timezone

import atomic
import paths

#: How many days the series keeps; the page draws all of them and tabulates the newest few.
KEEP_DAYS = 30


def series_file() -> pathlib.Path:
    return paths.STATE / "disk-free-series.json"


def _day(value) -> str | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _number(value) -> float | None:
    return round(float(value), 1) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and value == value and value >= 0 else None


def read(path: pathlib.Path | None = None) -> list[dict]:
    """The rows, oldest first: `{day, min_gb, last_gb, samples}`. What cannot be read is an empty series."""
    try:
        doc = json.loads((path or series_file()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = {}
    for row in (doc.get("days") if isinstance(doc, dict) else None) or []:
        if not isinstance(row, dict):
            continue
        day, low, last = _day(row.get("day")), _number(row.get("min_gb")), _number(row.get("last_gb"))
        samples = row.get("samples")
        if day and low is not None and last is not None:
            rows[day] = {"day": day, "min_gb": low, "last_gb": last,
                         "samples": samples if isinstance(samples, int) and samples > 0 else 1}
    return [rows[d] for d in sorted(rows)][-KEEP_DAYS:]


def record(free_gb: float, at: datetime | None = None, path: pathlib.Path | None = None) -> list[dict]:
    """Fold one reading into its UTC day and write the series; returns it. Raises OSError when it cannot be written."""
    reading = _number(free_gb)
    if reading is None:
        return read(path)
    day = (at or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%d")
    rows = read(path)
    if rows and rows[-1]["day"] == day:
        last = rows[-1]
        rows[-1] = {"day": day, "min_gb": min(last["min_gb"], reading), "last_gb": reading,
                    "samples": last["samples"] + 1}
    else:
        rows.append({"day": day, "min_gb": reading, "last_gb": reading, "samples": 1})
    rows = sorted(rows, key=lambda r: r["day"])[-KEEP_DAYS:]
    target = path or series_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json(target, {"schema_version": 1, "days": rows})
    os.chmod(target, 0o600)
    return rows
