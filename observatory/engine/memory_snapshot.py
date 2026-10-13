"""Cheap RAM/swap and RSS observations, independent of disk walks and process argv.

Receipts are observations, never lifecycle or cleanup authority. RSS adds each
process's resident pages and can count shared pages repeatedly.
"""
from __future__ import annotations
import contextlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import stat
import uuid
import time
from datetime import datetime, timezone

import atomic
import paths
import oslocks
import osprivacy

MAX_AGE_SECONDS = 1800
MEMORY_FIELDS = ("total_mb", "free_mb", "active_mb", "inactive_mb", "wired_mb", "compressed_mb", "swap_used_mb")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _stamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _run(args, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None, "budget-expired"
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=min(3, remaining))
        return (result.stdout, None) if result.returncode == 0 else (None, "command-failed")
    except subprocess.TimeoutExpired:
        return None, "command-timeout"
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None, "command-unavailable"


PRESSURE_LEVELS = {1: "normal", 2: "warning", 4: "critical"}


def pressure_summary(value, at, *, allow_current=True):
    """Pressure has its own age; swap/RSS never supplies a missing level."""
    value = value if isinstance(value, dict) else {}
    raw = value.get("raw_level")
    supported = type(raw) is int and raw in PRESSURE_LEVELS
    measured, attempted = _stamp(value.get("measured_at")), _stamp(value.get("attempted_at"))
    age = (at - measured).total_seconds() if measured else None
    valid = (allow_current and supported and value.get("source") == "macos-sysctl"
             and value.get("level") == PRESSURE_LEVELS[raw] and value.get("reason") is None
             and attempted is not None and measured is not None and attempted <= measured <= at
             and 0 <= age <= MAX_AGE_SECONDS)
    reason = value.get("reason")
    reasons = {"unsupported-platform", "command-failed", "command-timeout", "command-unavailable",
               "budget-expired", "invalid-output"}
    return {"level": PRESSURE_LEVELS[raw] if valid else "unknown",
            "recorded_level": PRESSURE_LEVELS[raw] if supported and value.get("level") == PRESSURE_LEVELS[raw] else "unknown",
            "raw_level": raw if supported else None,
            "source": "macos-sysctl" if value.get("source") == "macos-sysctl" else None,
            "attempted_at": attempted.isoformat() if attempted else None,
            "measured_at": measured.isoformat() if measured else None,
            "age_seconds": age, "stale": age > MAX_AGE_SECONDS if age is not None and age >= 0 else None,
            "reason": None if valid else reason if isinstance(reason, str) and reason in reasons else "missing-stale-or-invalid-pressure",
            "actionable": False}


def collect(seconds=8):
    """Only sysctl/vm_stat (or proc meminfo) and one ps RSS/comm query; no argv."""
    started = now()
    deadline = time.monotonic() + min(15, max(0, seconds))
    try:
        outer = float(os.environ.get("OBSERVATORY_STEP_DEADLINE", "nan"))
        if math.isfinite(outer):
            deadline = min(deadline, time.monotonic() + max(0, outer - time.time()))
    except ValueError:
        pass
    memory = {k: None for k in MEMORY_FIELDS}
    memory.update(attempted_at=started, measured_at=None, degraded=[], sources={})
    def source(name, valid, reason=None):
        memory["sources"][name] = {"measured_at": now() if valid else None,
                                   "reason": None if valid else reason or "invalid-output"}
        if not valid:
            memory["degraded"].append({"source": name, "reason": reason or "invalid-output"})
    if sys.platform == "darwin":
        total, reason = _run(["/usr/sbin/sysctl", "-n", "hw.memsize"], deadline)
        valid = isinstance(total, str) and len(total.strip()) <= 20 and total.strip().isdigit() and int(total) > 0
        if valid:
            memory["total_mb"] = round(int(total) / 1048576, 1)
        source("ram", valid, reason)
        vm, reason = _run(["vm_stat"], deadline)
        page = re.search(r"page size of (\d{1,10}) bytes", vm or "")
        valid = page is not None and int(page.group(1)) > 0
        if valid:
            values = dict(re.findall(r"^([^:]+):\s+(\d{1,20})\.?$", vm, re.M))
            for key, label in (("free_mb", "Pages free"), ("active_mb", "Pages active"),
                               ("inactive_mb", "Pages inactive"), ("wired_mb", "Pages wired down"),
                               ("compressed_mb", "Pages occupied by compressor")):
                if label in values:
                    memory[key] = round(int(values[label]) * int(page.group(1)) / 1048576, 1)
            valid = memory["free_mb"] is not None
        source("vm", valid, reason)
        swap, reason = _run(["/usr/sbin/sysctl", "-n", "vm.swapusage"], deadline)
        match = re.search(r"\bused\s*=\s*([\d.]+)M\b", swap or "")
        try:
            amount = float(match.group(1)) if match else None
        except ValueError:
            amount = None
        valid = _number(amount)
        if valid:
            memory["swap_used_mb"] = amount
        source("swap", valid, reason)
    elif sys.platform.startswith("linux"):
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError
            with open("/proc/meminfo", encoding="utf-8") as stream:
                text = stream.read(65536)
            values = {k: int(v) for k, v in re.findall(r"^(\w+):\s+(\d+) kB$", text, re.M)}
            memory["total_mb"] = values.get("MemTotal", 0) / 1024 if values.get("MemTotal", 0) > 0 else None
            memory["free_mb"] = values["MemAvailable"] / 1024 if "MemAvailable" in values else None
            if "SwapTotal" in values and "SwapFree" in values and values["SwapTotal"] >= values["SwapFree"]:
                memory["swap_used_mb"] = (values["SwapTotal"] - values["SwapFree"]) / 1024
            source("ram", memory["total_mb"] is not None and memory["free_mb"] is not None)
            source("swap", memory["swap_used_mb"] is not None)
        except (OSError, ValueError, TimeoutError):
            source("ram", False, "memory-source-unavailable")
            source("swap", False, "memory-source-unavailable")
    else:
        source("memory", False, "unsupported-platform")
    pressure = {"level": "unknown", "raw_level": None, "attempted_at": now(),
                "measured_at": None, "source": None, "reason": "unsupported-platform", "actionable": False}
    if sys.platform == "darwin":
        output, reason = _run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"], deadline)
        pressure["source"] = "macos-sysctl"
        value = output.strip() if isinstance(output, str) else None
        if reason is None and value in ("1", "2", "4"):
            pressure.update(raw_level=int(value), level=PRESSURE_LEVELS[int(value)], measured_at=now(), reason=None)
        else:
            pressure["reason"] = reason or "invalid-output"
    memory["pressure"] = pressure
    stamps = [r["measured_at"] for r in memory["sources"].values() if r["measured_at"]]
    memory["measured_at"] = max(stamps) if stamps else None
    attempted = now()
    output, reason = _run(["ps", "-axo", "pid=,rss=,comm="], deadline)
    processes = {"attempted_at": attempted, "measured_at": None, "count": None,
                 "attribution": "executable-only", "projects_measured": False,
                 "rss_semantics": "sum-of-process-rss-not-unique-physical-memory",
                 "groups": [], "top": [], "projects": [], "degraded": []}
    rows, groups, invalid = [], {}, 0
    for line in (output or "").splitlines():
        parts = line.split(None, 2)
        if (len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit()
                or len(parts[0]) > 20 or len(parts[1]) > 20):
            invalid += 1
            continue
        name = Path(parts[2]).name
        if (not re.fullmatch(r"[\w .+()-]{1,120}", name)
                or re.search(r"token|password|secret|bearer|cookie|api.key", name, re.I)):
            name = "redacted-executable"
        label = "executable:" + name
        rss = int(parts[1]) / 1024
        group = groups.setdefault(label, {"origin": label, "processes": 0, "sessions": None,
                                          "rss_mb": 0, "cpu": None})
        group["processes"] += 1
        group["rss_mb"] += rss
        rows.append({"pid": int(parts[0]), "name": name, "origin": label, "rss_mb": round(rss, 1)})
    if rows:
        processes.update(count=len(rows) if not invalid else None, observed_count=len(rows), measured_at=now(),
                         groups=sorted(({**g, "rss_mb": round(g["rss_mb"], 1)} for g in groups.values()),
                                       key=lambda g: -g["rss_mb"]),
                         top=sorted(rows, key=lambda row: -row["rss_mb"])[:40])
    if not rows or invalid:
        processes["degraded"].append({"source": "ps", "reason": reason or "invalid-output",
                                      "invalid_rows": invalid})
    return {"schema_version": 1, "kind": "machine-memory", "attempted_at": started,
            "memory": memory, "processes": processes,
            "partial": bool(memory["degraded"] or processes["degraded"])}


@contextlib.contextmanager
def _receipt_lock(out):
    """Only serialize receipt replacement; collection never holds this lock."""
    out = Path(out)
    if any(p.is_symlink() for p in (out, *out.parents)):
        raise ValueError("Output path must not contain symbolic links")
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = out.with_name(out.name + ".lock")
    fd = osprivacy.open(lock, os.O_RDWR | os.O_CREAT | osprivacy.NOFOLLOW | osprivacy.NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Receipt lock must be a regular file")
        end = time.monotonic() + 0.25
        while True:
            try:
                oslocks.flock(fd, oslocks.LOCK_EX | oslocks.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise TimeoutError("memory-receipt-lock-busy")
                time.sleep(0.005)
        try:
            yield
        finally:
            oslocks.flock(fd, oslocks.LOCK_UN)
    finally:
        os.close(fd)


def failed_attempt(reason="measurement-incomplete"):
    stamp = now()
    error = [{"source": "memory-refresh", "reason": reason}]
    return {"schema_version": 1, "kind": "machine-memory", "attempted_at": stamp, "partial": True,
            "memory": {**{key: None for key in MEMORY_FIELDS}, "attempted_at": stamp,
                       "measured_at": None, "degraded": error},
            "processes": {"count": None, "attempted_at": stamp, "measured_at": None,
                          "groups": [], "top": [], "projects": [], "degraded": error}}


def begin_attempt(out):
    with _receipt_lock(out):
        doc = failed_attempt()
        doc["attempt_id"] = uuid.uuid4().hex
        atomic.write_json(out, doc)
        return doc["attempt_id"]


def finish_attempt(out, attempt_id, doc):
    """Compare and replace under one lock; an older finish cannot hide a newer failure."""
    with _receipt_lock(out):
        try:
            prior = json.loads(Path(out).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(prior, dict) or prior.get("attempt_id") != attempt_id:
            return False
        doc = {**doc, "attempt_id": attempt_id, "attempted_at": prior["attempted_at"]}
        for section in ("memory", "processes"):
            doc[section] = {**doc[section], "attempted_at": prior["attempted_at"]}
        atomic.write_json(out, doc)
        return True


def write(out, seconds=8, attempt_id=None):
    attempt_id = attempt_id or begin_attempt(out)
    doc = collect(seconds)
    doc["publication"] = "published" if finish_attempt(out, attempt_id, doc) else "superseded"
    return doc


def select_sources(machine, light, *, at=None):
    """Select each newest attempt, retaining failures and independent source ages."""
    at = at or datetime.now(timezone.utc)
    machine = machine if isinstance(machine, dict) else {}
    valid_light = (isinstance(light, dict) and type(light.get("schema_version")) is int
                   and light.get("schema_version") == 1 and light.get("kind") == "machine-memory")
    result = {}
    for section in ("memory", "processes"):
        candidates, receipt_errors = [], []
        for name, doc in (("machine.json", machine), ("machine-memory.json", light if valid_light else {})):
            part = doc.get(section)
            if not isinstance(part, dict):
                continue
            stamp = part.get("attempted_at", part.get("measured_at", doc.get("measured_at")))
            rank = _stamp(stamp)
            if rank is None:
                receipt_errors.append({"source": name, "reason": "invalid-attempted-at"})
            # An undated or future light receipt is visible as unknown, never fresh.
            candidates.append((rank or datetime.min.replace(tzinfo=timezone.utc), name,
                               {**part, "attempted_at": stamp,
                                "measured_at": part.get("measured_at", doc.get("measured_at"))}))
        if candidates:
            _, name, part = max(candidates, key=lambda row: (row[0], row[1] == "machine-memory.json"))
        else:
            name, part = None, {"attempted_at": None, "measured_at": None}
        errors = part.get("degraded")
        errors = ([x for x in errors if isinstance(x, dict)] if isinstance(errors, list) else []) + receipt_errors
        stamp = _stamp(part.get("measured_at"))
        age = (at - stamp).total_seconds() if stamp else None
        stale = age > MAX_AGE_SECONDS if age is not None and age >= -60 else None
        attempt = _stamp(part.get("attempted_at"))
        if attempt is None or (attempt - at).total_seconds() > 60:
            stale = None
            errors.append({"source": section, "reason": "invalid-attempted-at"})
        if stale is None or stale:
            errors.append({"source": section, "reason": "stale-measurement" if stale else "unknown-measurement-time"})
        if section == "memory":
            part["pressure"] = pressure_summary(part.get("pressure"), at,
                                                allow_current=attempt is not None and attempt <= at)
            for key in MEMORY_FIELDS:
                value = part.get(key)
                if value is not None and not _number(value):
                    errors.append({"source": key, "reason": "invalid-measurement"})
                part[key] = value if _number(value) else None
                if part[key] is None and key in ("total_mb", "free_mb", "swap_used_mb"):
                    errors.append({"source": key, "reason": "missing-measurement"})
        else:
            count = part.get("count")
            part["count"] = count if type(count) is int and count >= 0 else None
            for key in ("groups", "top", "projects"):
                rows = part.get(key, [])
                valid = []
                for row in rows if isinstance(rows, list) else []:
                    required = {"groups": ("origin", "processes", "rss_mb"),
                                "top": ("pid", "name", "origin", "rss_mb"),
                                "projects": ("project", "processes", "rss_mb")}[key]
                    text_keys = {"groups": ("origin",), "top": ("name", "origin"), "projects": ("project",)}[key]
                    count_key = "pid" if key == "top" else "processes"
                    if (isinstance(row, dict) and all(k in row for k in required) and _number(row.get("rss_mb"))
                            and all(isinstance(row.get(k), str) and row[k] for k in text_keys)
                            and type(row.get(count_key)) is int and row[count_key] >= 0):
                        row = dict(row)
                        for metric in ("sessions", "cpu"):
                            if metric in row and not _number(row[metric]):
                                row[metric] = None
                        valid.append(row)
                    else:
                        errors.append({"source": "processes", "reason": "invalid-row"})
                if not isinstance(rows, list):
                    errors.append({"source": "processes", "reason": "invalid-collection"})
                part[key] = valid
            if part["count"] is None:
                errors.append({"source": "processes", "reason": "missing-process-count"})
        if light is not None and (not valid_light or not isinstance(light.get(section), dict)):
            errors.append({"source": "machine-memory.json", "reason": "invalid-receipt"})
        result[section] = {**part, "source": name, "stale": stale, "degraded": errors}
    return result


def read_sources(machine=None, *, scratch=None, at=None):
    scratch = paths.SCRATCH if scratch is None else scratch
    errors = []
    def read(name):
        try:
            return json.loads((scratch / name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            errors.append({"source": name, "reason": "unreadable-receipt"})
            return None
    result = select_sources(read("machine.json") if machine is None else machine, read("machine-memory.json"), at=at)
    for part in result.values():
        part["degraded"] += errors
    return result
