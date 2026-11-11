"""Local credential input and exact known-value scanning. No provider APIs."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

from .core import (MIN_SECRET_BYTES, ObservatoryError, config, known_slot_values, now, private_dir,
                   private_file, redact, valid_name, write_json, write_private)

MAX_TARGET_BYTES = 16 * 1024 * 1024
MAX_SQLITE_CELLS = 100_000


def put_secret(state: Path, name: str, value: str) -> dict:
    config(state)
    valid_name(name)
    value = value.rstrip("\r\n")
    if len(value.encode()) < MIN_SECRET_BYTES or "\x00" in value or len(value.encode()) > 64 * 1024:
        raise ObservatoryError("Secret must contain 8 to 65536 bytes and no NUL.")
    directory = state / "secrets"
    private_dir(directory)
    write_private(directory / name, value)
    return redact({"name": name, "stored": True, "value_printed": False}, [value])


def secret_names(state: Path) -> list[str]:
    config(state)
    directory = state / "secrets"
    if directory.is_symlink():
        raise ObservatoryError("Secret storage must not be a symlink.")
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if p.is_file() and not p.is_symlink())


def read_secret(state: Path, name: str) -> str:
    valid_name(name)
    file = state / "secrets" / name
    if file.parent.is_symlink():
        raise ObservatoryError("Secret storage must not be a symlink.")
    private_file(file)
    if file.stat().st_size > 64 * 1024:
        raise ObservatoryError("Secret slot exceeds 64 KB.")
    return file.read_text()


def run_secret(state: Path, name: str, variable: str, command: list[str]) -> dict:
    config(state)
    if not variable.replace("_", "a").isalnum() or not variable or variable[0].isdigit():
        raise ObservatoryError("Use a valid environment variable name.")
    if not command:
        raise ObservatoryError("Supply an explicit command after --.")
    value = read_secret(state, name)
    try:
        # Child output could contain the value. Suppress both streams by default.
        result = subprocess.run(command, env={**os.environ, variable: value},
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        raise ObservatoryError("The child command could not be started; no credential was printed.") from None
    return redact({"name": name, "exit_code": result.returncode, "child_output": "suppressed"}, [value])


def _sqlite_counts(path: Path, values: dict[str, bytes]) -> tuple[dict, list[str]]:
    counts = {n: 0 for n in values}
    degraded = []
    quote = lambda s: '"' + s.replace('"', '""') + '"'
    db = None
    try:
        # URI quoting protects ? and # in filenames; read-only SQLite mode prevents mutation.
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        db.execute("PRAGMA query_only = ON")
        db.execute("PRAGMA trusted_schema = OFF")
        # Bound SQLite itself before materializing hostile/generated content.
        if hasattr(db, "setlimit"):
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_TARGET_BYTES)
            db.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 256)
        deadline = time.monotonic() + 5
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        if sqlite3.sqlite_version_info < (3, 37, 0):
            return counts, ["SQLite 3.37+ is required for safe table classification."]
        tables = [(r[1], r[2]) for r in db.execute("PRAGMA table_list") if not r[1].startswith("sqlite_")]
        if len(tables) > 256:
            return counts, ["SQLite table limit reached; result is partial."]
        inspected = 0
        inspected_bytes = 0
        for table, kind in tables:
            if kind != "table":
                degraded.append("Virtual SQLite table skipped; result is partial.")
                continue
            columns = list(db.execute(f"PRAGMA table_xinfo({quote(table)})"))
            stored = [r[1] for r in columns if r[6] == 0]
            if len(stored) != len(columns):
                degraded.append("Generated SQLite columns skipped; result is partial.")
            if not stored or len(stored) > 256:
                continue
            projection = ",".join(quote(c) for c in stored)
            for row in db.execute(f"SELECT {projection} FROM {quote(table)}"):
                for cell in row:
                    inspected += 1
                    if inspected > MAX_SQLITE_CELLS:
                        return counts, ["SQLite cell limit reached; result is partial."]
                    if not isinstance(cell, (bytes, str)):
                        continue
                    body = cell.encode("utf-8") if isinstance(cell, str) else cell
                    inspected_bytes += len(body)
                    if inspected_bytes > MAX_TARGET_BYTES:
                        return counts, ["SQLite byte limit reached; result is partial."]
                    for name, value in values.items():
                        counts[name] += body.count(value)
    except (sqlite3.Error, UnicodeError):
        degraded.append("SQLite target could not be fully read; result is partial.")
    finally:
        if db is not None:
            db.close()
    return counts, degraded


def scan_leaks(state: Path, targets: list[str], sqlite: bool = False) -> dict:
    config(state)
    if not targets:
        raise ObservatoryError("Choose at least one explicit --file target; no transcript directory is scanned automatically.")
    if len(targets) > 32:
        raise ObservatoryError("Choose at most 32 explicit targets per scan.")
    known_slot_values(state)  # Enforce the shared value-count and byte budget.
    values = {}
    skipped = 0
    for name in secret_names(state):
        raw = read_secret(state, name).encode()
        if len(raw) < MIN_SECRET_BYTES:
            skipped += 1
        else:
            values[name] = raw
    report = {"schema_version": 1, "scanned_at": now(), "mode": "known-values-only",
              "known_slots": len(values), "short_slots_skipped": skipped,
              "targets": [], "findings": [], "degraded": []}
    if not values:
        report["degraded"].append("No eligible known values; this scan cannot establish absence of secrets.")
    seen = set()
    for number, source in enumerate(targets, 1):
        path = Path(source).expanduser().absolute()
        label = f"target-{number}"
        # Never serialize absolute targets, table contents or snippets.
        item = {"target": label, "format": "sqlite" if sqlite else "file", "occurrences": 0, "degraded": []}
        report["targets"].append(item)
        if path.is_symlink() or any(p.is_symlink() for p in path.parents) or not path.is_file():
            item["degraded"].append("Target is not a regular file or uses a symlink.")
            continue
        canonical = path.resolve()
        if canonical in seen:
            item["degraded"].append("Duplicate target skipped.")
            continue
        seen.add(canonical)
        if canonical == state.resolve() or state.resolve() in canonical.parents:
            item["degraded"].append("Private Observatory state is not an exposure target.")
            continue
        try:
            if path.stat().st_size > MAX_TARGET_BYTES:
                item["degraded"].append("Target exceeds the 16 MB scan limit.")
                continue
            if sqlite:
                counts, errors = _sqlite_counts(path, values)
                item["degraded"].extend(errors)
            else:
                body = path.read_bytes()
                counts = {name: body.count(value) for name, value in values.items()}
            for name, count in counts.items():
                if count:
                    report["findings"].append({"kind": "credential.local-exposure", "secret": name,
                        "target": label, "occurrences": count,
                        "remedy": "Review the local artifact, rotate at its issuer if appropriate, and remove exposed copies after preserving necessary evidence."})
                    item["occurrences"] += count
        except OSError:
            item["degraded"].append("Target could not be read.")
    report["degraded"].extend(f"{t['target']}: {d}" for t in report["targets"] for d in t["degraded"])
    report = redact(report, [v.decode("utf-8") for v in values.values()])
    write_json(state / "leaks.json", report)
    return report
