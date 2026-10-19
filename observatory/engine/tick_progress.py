"""Bounded metadata in the existing tick receipts, including silent tail calls.

A start is the watchdog invocation, not proof that it started the collector. Only
its terminal outcome distinguishes not_started from stopped. The supervisor may
observe a start without an end: that stage stays unknown, even on a global timeout.
No command, output, path or exception text belongs to this projection.
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

RUN_ENV = "OBSERVATORY_TICK_RUN_ID"
MAX_BYTES = 262144
MAX_STAGES = 128
# A finite vocabulary prevents arbitrary argv/exception values becoming metadata.
STAGES = frozenset("""integrity scan-fs scan-gh scan-vault scan-sessions remotes bitbucket
    domains backup-due backup heroku openrouter env scan-cloudflare scan-mcp scan-harness
    scan-spend remote-env google leaks scrub-companion machine scan-agents git-hygiene lifecycle cleanup
    merge emit validate events snapshot diff index retention sweep corroborate ledger lost plugins
    rollup agent findings dashboard smoke findings-recheck notify registry project-into-vault
    projection links logs unknown""".split())
OUTCOMES = frozenset(("running", "ran", "failed", "stopped", "not_started", "disabled", "unknown"))


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def valid_id(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def read(path: Path) -> dict:
    # A damaged/foreign receipt must not cause an unbounded allocation.
    import osprivacy
    if any(p.is_symlink() for p in path.parents):
        raise ValueError("receipt parent link")
    fd = osprivacy.open(path, os.O_RDONLY | osprivacy.NOFOLLOW | osprivacy.NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("receipt source")
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("receipt size")
    doc = json.loads(data)
    if not isinstance(doc, dict):
        raise ValueError("receipt shape")
    return doc


class Progress:
    def __init__(self, scratch: Path, logger):
        self.path = scratch / "tick.json"
        candidate = os.environ.get(RUN_ENV)
        self.run_id = candidate if valid_id(candidate) else uuid.uuid4().hex
        self.stages: list[dict] = []
        self.write_failures = 0
        self.logger = logger
        self.began = 0.0

    def document(self) -> dict:
        return {"schema": 1, "run_id": self.run_id, "stages": self.stages,
                "write_failures": self.write_failures}

    def warn(self, message: str) -> None:
        try:
            self.logger(message)
        except Exception:  # noqa: BLE001 — a broken diagnostic sink cannot stop the command
            pass

    def save(self) -> None:
        try:
            try:
                doc = read(self.path)
            except FileNotFoundError:
                doc = {}
            # Preserve the prior completed report until this run actually writes its report.
            doc["progress"] = self.document()
            import atomic
            atomic.write_json(self.path, doc)
        except Exception:  # noqa: BLE001 — instrumentation cannot change the step's exit
            self.write_failures += 1
            self.warn("tick: stage receipt unavailable")

    def start(self, name: str) -> None:
        self.began = time.monotonic()
        if len(self.stages) >= MAX_STAGES:
            self.write_failures += 1
            self.warn("tick: stage receipt limit reached")
            self.save()
            return
        self.stages.append({"step": name if name in STAGES else "unknown",
                            "started_at": stamp(), "ended_at": None, "duration_seconds": None,
                            "exit": None, "outcome": "running"})
        self.save()

    def finish(self, code: int | None, outcome: str) -> None:
        if not self.stages or self.stages[-1]["outcome"] != "running":
            return
        self.stages[-1].update(ended_at=stamp(), duration_seconds=round(time.monotonic() - self.began, 6),
                               exit=code, outcome=outcome)
        self.save()


def _time(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        return True
    except ValueError:
        return False


def snapshot(scratch: Path, run_id: str | None) -> dict:
    """Strict metadata projection after the child exits; stale/missing is unknown.

    A start whose end was never saved has no inferred exit, duration or end time.
    Extra fields are discarded, and any malformed row makes coverage unknown.
    """
    unknown = {"schema": 1, "coverage": "unknown", "reason": "stage_receipt_unavailable",
               "stages": []}
    if not valid_id(run_id):
        return unknown
    try:
        doc = read(scratch / "tick.json")["progress"]
        if not isinstance(doc, dict) or (type(doc.get("schema")) is not int or doc.get("schema") != 1) or doc.get("run_id") != run_id:
            return unknown
        rows, failures = doc["stages"], doc["write_failures"]
        if (not isinstance(rows, list) or len(rows) > MAX_STAGES
                or type(failures) is not int or failures < 0):
            return unknown
        clean = []
        for row in rows:
            if not isinstance(row, dict) or row.get("step") not in STAGES or row.get("outcome") not in OUTCOMES:
                return unknown
            outcome, code = row["outcome"], row.get("exit")
            duration, ended = row.get("duration_seconds"), row.get("ended_at")
            if not _time(row.get("started_at")):
                return unknown
            if outcome == "running":
                if any(v is not None for v in (code, duration, ended)):
                    return unknown
                outcome = "unknown"
            else:
                if not _time(ended) or type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
                    return unknown
                if outcome == "unknown":
                    if code is not None:
                        return unknown
                elif type(code) is not int:
                    return unknown
                elif ((outcome in ("ran", "disabled") and code != 0)
                      or (outcome == "not_started" and code != 124)
                      or (outcome == "stopped" and not (code == 124 or code < 0 or code > 128))
                      or (outcome == "failed" and not (0 < code <= 128 and code != 124))):
                    return unknown
            clean.append({"step": row["step"], "started_at": row["started_at"], "ended_at": ended,
                          "duration_seconds": duration, "exit": code, "outcome": outcome})
        partial = failures > 0 or any(r["outcome"] == "unknown" or r["step"] == "unknown" for r in clean)
        return {"schema": 1, "run_id": run_id, "coverage": "partial" if partial else "observed",
                "write_failures": failures, "stages": clean}
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return unknown
