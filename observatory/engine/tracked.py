#!/usr/bin/env python3
"""Exact local Git folders declared for the ordinary filesystem collector.

`config/tracked.json` is a narrow declaration, never a second registry. A row
contains an absolute checkout path and its provenance; the next filesystem scan
measures that path through `scan_folder`, and merge/emit remain the only writers
of `registry/projects.json`. `sources.projects` is not broadened.

The store follows the declared-tracking design: one locked read-modify-write,
atomic JSON replacement, unknown-field preservation, a journal beside the raw
store, and no credentials or organization assignment. A removal also leaves a
`dismissed_local_folders` marker. SessionStart is not allowed to restore such a
path; an explicit `full track local-folder PATH` is the opt-in that can.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
from pathlib import Path
import time
from typing import Any

import atomic
import configuration
import oslocks
import osprivacy
import paths

SCHEMA_VERSION = 1
FILE = "tracked.json"
JOURNAL = "tracked-journal.jsonl"


class TrackError(configuration.ConfigurationError):
    """A declaration cannot be read or written without risking existing rows."""


def file_path() -> Path:
    return paths.config_file(FILE)


def journal_path() -> Path:
    return paths.SCRATCH / JOURNAL


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def empty() -> dict:
    return {"schema_version": SCHEMA_VERSION, "local_folders": [], "dismissed_local_folders": []}


def validate(doc: Any) -> list[str]:
    if not isinstance(doc, dict):
        return ["tracked.json must be an object"]
    version = doc.get("schema_version", SCHEMA_VERSION)
    if type(version) is not int or version < 1:
        return ["schema_version must be a positive integer"]
    if version > SCHEMA_VERSION:
        return [f"tracked.json schema {version} is newer than this release reads ({SCHEMA_VERSION})"]
    out = []
    for field in ("local_folders", "dismissed_local_folders"):
        rows = doc.get(field, [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            out.append(f"{field} must be a list of objects")
    return out


def load(path: Path | None = None) -> dict:
    """Read declarations; an unreadable file is never treated as an empty one."""
    path = path or file_path()
    if path.is_symlink():
        raise TrackError(f"{path.name} must not be a symbolic link")
    if not path.exists():
        return empty()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TrackError(f"{path.name} cannot be read: {type(exc).__name__}; nothing was changed") from None
    problems = validate(doc)
    if problems:
        raise TrackError("; ".join(problems) + "; nothing was changed")
    doc.setdefault("schema_version", SCHEMA_VERSION)
    doc.setdefault("local_folders", [])
    doc.setdefault("dismissed_local_folders", [])
    return doc


def canonical(value: str | os.PathLike) -> Path:
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise TrackError("a local-folder declaration needs an absolute path")
        return path.resolve()
    except (OSError, RuntimeError, TypeError) as exc:
        raise TrackError(f"the local-folder path cannot be resolved: {type(exc).__name__}") from None


def _valid_rows(doc: dict, field: str) -> tuple[list[dict], list[dict]]:
    rows, degraded = [], []
    for index, row in enumerate(doc.get(field) or []):
        try:
            path = canonical(row.get("path", ""))
        except TrackError as exc:
            degraded.append({"source": "tracked", "reason": f"local-folder declaration {index}: {exc}"})
            continue
        rows.append({**row, "path": str(path)})
    return rows, degraded


def rows(doc: dict | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    """(active, dismissed, degraded), with one bad row not hiding valid peers."""
    doc = doc if doc is not None else load()
    active, first = _valid_rows(doc, "local_folders")
    dismissed, second = _valid_rows(doc, "dismissed_local_folders")
    return active, dismissed, first + second


def is_excluded(path: Path, prefixes: tuple[str, ...], names) -> bool:
    """Apply folder exclusions to a root child or an absolute declared path."""
    text = str(path)
    names = set(names)
    return (text.startswith(prefixes) or path.name.startswith(prefixes)
            or text in names or path.name in names)


def exclusion_rules() -> tuple[tuple[str, ...], dict[str, str]]:
    try:
        doc = json.loads(paths.config_file("folder_exclusions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return (), {}
    return (tuple(x["prefix"] for x in doc.get("prefixes") or []),
            {x["name"]: x.get("why", "") for x in doc.get("names") or []})


def eligibility(value: str | os.PathLike) -> tuple[Path, str]:
    """The exact checkout may be declared; broad and internal folders may not."""
    path = canonical(value)
    data, home = paths.DATA.resolve(), paths.HOME.resolve()
    if path == data or data in path.parents:
        return path, "inside-projects"
    if path in data.parents:
        return path, "a parent of the configured projects folder is too broad to declare"
    protected = (paths.ROOT.resolve(), home, paths.REGISTRY.resolve(), paths.STORE.resolve())
    if any(path == root or root in path.parents for root in protected):
        return path, "the Observatory engine and its workspace are not projects"
    user_home = Path.home().resolve()
    if path == Path(path.anchor) or path == user_home or path in user_home.parents:
        return path, "a home, filesystem root or broad parent is not a project checkout"
    prefixes, names = exclusion_rules()
    if is_excluded(path, prefixes, names):
        why = names.get(path.name) or names.get(str(path)) or "excluded by folder rule"
        return path, f"excluded: {why}"
    if not path.is_dir() or not (path / ".git").exists():
        return path, "not a Git repository or worktree"
    return path, ""


@contextlib.contextmanager
def _locked(timeout: float = 10.0):
    """Serialize the read-modify-write across SessionStart and the CLI."""
    lock = file_path().parent / f".{FILE}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = osprivacy.open(lock, os.O_CREAT | os.O_RDWR | osprivacy.NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                oslocks.flock(fd, oslocks.LOCK_EX | oslocks.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TrackError("another local-folder declaration is being written; retry shortly") from None
                time.sleep(0.05)
        yield
    finally:
        with contextlib.suppress(OSError):
            oslocks.flock(fd, oslocks.LOCK_UN)
        os.close(fd)


def _save(doc: dict) -> None:
    atomic.write_json(file_path(), doc, indent=2)


def _journal(event: dict, out: dict) -> None:
    """A saved declaration reports a journal failure instead of lying about it."""
    try:
        journal = journal_path()
        journal.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = osprivacy.open(journal, os.O_CREAT | os.O_WRONLY | os.O_APPEND | osprivacy.NOFOLLOW, 0o600)
        try:
            os.write(fd, (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as exc:
        out.setdefault("degraded", []).append(
            {"source": "tracked-journal", "reason": f"the change was saved but not journalled: {exc}"})


def _write_problems(doc: dict) -> list[str]:
    problems = []
    for field in ("local_folders", "dismissed_local_folders"):
        for index, row in enumerate(doc.get(field) or []):
            try:
                canonical(row.get("path", ""))
            except TrackError as exc:
                problems.append(f"{field} declaration {index}: {exc}")
    return problems


def declare(value: str | os.PathLike, actor: str, *, session_id: str = "", automatic: bool = True) -> dict:
    """Declare one checkout, or explain why automation must leave it alone."""
    path, why = eligibility(value)
    out: dict[str, Any] = {"path": str(path), "degraded": []}
    if why == "inside-projects":
        return {**out, "status": "inside-projects"}
    if why:
        return {**out, "status": "refused", "reason": why}
    with _locked():
        doc = load()
        problems = _write_problems(doc)
        if problems:
            raise TrackError("; ".join(problems) + "; nothing was changed")
        active, dismissed, _ = rows(doc)
        existing = next((row for row in active if row["path"] == str(path)), None)
        if existing is not None:
            return {**out, "status": "pending" if automatic else "unchanged", "entry": existing}
        removed = next((row for row in dismissed if row["path"] == str(path)), None)
        if removed is not None and automatic:
            return {**out, "status": "refused",
                    "reason": f"removed by {removed.get('removed_by') or 'the operator'}; automatic declaration will not restore it"}
        entry = {"path": str(path), "added_by": actor, "added_at": _now()}
        if session_id:
            entry["session_id"] = session_id
        doc["local_folders"].append(entry)
        if removed is not None:
            doc["dismissed_local_folders"] = [row for row in doc["dismissed_local_folders"]
                                                if canonical(row.get("path", "")) != path]
        _save(doc)
    out.update(status="declared", entry=entry)
    _journal({"at": entry["added_at"], "actor": actor, "action": "declare",
              "kind": "local-folder", "path": str(path), "session_id": session_id or None}, out)
    return out


def remove(value: str | os.PathLike, actor: str = "operator") -> dict:
    """Untrack a declaration and prevent SessionStart from silently restoring it."""
    path = canonical(value)
    out: dict[str, Any] = {"path": str(path), "degraded": []}
    with _locked():
        doc = load()
        problems = _write_problems(doc)
        if problems:
            raise TrackError("; ".join(problems) + "; nothing was changed")
        active, dismissed, _ = rows(doc)
        entry = next((row for row in active if row["path"] == str(path)), None)
        if entry is None:
            if any(row["path"] == str(path) for row in dismissed):
                return {**out, "status": "already-removed"}
            raise TrackError(f"no declared local folder at {path}; `project-observatory full tracked` lists declarations")
        doc["local_folders"] = [row for row in doc["local_folders"]
                                  if canonical(row.get("path", "")) != path]
        doc["dismissed_local_folders"] = [row for row in doc["dismissed_local_folders"]
                                            if canonical(row.get("path", "")) != path]
        removed = {"path": str(path), "removed_by": actor, "removed_at": _now(),
                   "previous_owner": entry.get("added_by")}
        doc["dismissed_local_folders"].append(removed)
        _save(doc)
    out.update(status="removed", entry=entry, dismissal=removed)
    _journal({"at": removed["removed_at"], "actor": actor, "action": "remove",
              "kind": "local-folder", "path": str(path), "previous_owner": entry.get("added_by")}, out)
    return out


def listing() -> dict:
    active, dismissed, degraded = rows()
    return {"schema_version": SCHEMA_VERSION, "file": str(file_path()),
            "local_folders": active, "dismissed_local_folders": dismissed, "degraded": degraded}


def health() -> dict:
    """Doctor's bounded view; malformed declarations are degraded, not healthy zeroes."""
    try:
        doc = listing()
    except TrackError as exc:
        return {"active": 0, "dismissed": 0, "degraded": [str(exc)]}
    return {"active": len(doc["local_folders"]), "dismissed": len(doc["dismissed_local_folders"]),
            "degraded": [row["reason"] for row in doc["degraded"]]}
