"""The update events every product logs the same way (lifecycle LC-16, "Log events").

`store/logs/update.jsonl` is this workspace's own journal of an update, step by step, with
versions and reasons. This module writes the second, shared record the organisation reads
across products: one JSON line per event, **codes only** — the event, its code, a UTC time,
which part of the product it is about (`engine` or `app`) and the workspace's instance id
(the same sha16 of its path the launchd labels carry, so two workspaces on one account can be
told apart without naming a path). Never a version, a path, a message or a value.

Where it goes (`directory()`):

- `OBSERVATORY_PRODUCT_LOG_DIR`, when set — the test suites set it, so no test writes the
  real log folder;
- macOS: `~/Library/Logs/Project Observatory/` (LC-12);
- elsewhere: `$XDG_STATE_HOME/project-observatory/logs/` (`~/.local/state/…` by default).

The file is `updates.jsonl`, owner-only, rotated under the engine's one log policy
(log_policy.py). Writing never decides an outcome: a folder that cannot be written loses
the line, never the update.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path
import sys

#: Every event and the codes it may carry (LC-16). An unknown pair is a programming error.
EVENTS = {
    "update_check": frozenset({"current", "ready", "check_failed", "download_failed", "signature_failed",
                               "install_failed", "needs_migration"}),
    "update_download": frozenset({"started", "done"}),
    "update_install": frozenset({"started", "installed", "failed", "timeout"}),
    "update_restart": frozenset({"requested", "refused"}),
    "auto_update": frozenset({"on", "off"}),
}
SUBJECTS = frozenset({"engine", "app"})
DIR_ENV = "OBSERVATORY_PRODUCT_LOG_DIR"
PRODUCT = "Project Observatory"
FILE = "updates.jsonl"


def directory() -> Path:
    override = os.environ.get(DIR_ENV)
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / PRODUCT
    state = os.environ.get("XDG_STATE_HOME")
    root = Path(state) if state and Path(state).is_absolute() else Path.home() / ".local" / "state"
    return root / "project-observatory" / "logs"


def instance(base: Path | None) -> str | None:
    if base is None:
        return None
    try:
        return hashlib.sha256(str(Path(base).resolve()).encode()).hexdigest()[:16]
    except OSError:
        return None


def emit(event: str, code: str, *, subject: str = "engine", base: Path | None = None) -> None:
    """One line: {"at", "event", "code", "subject", "instance"}. Never raises for I/O."""
    if code not in EVENTS.get(event, ()) or subject not in SUBJECTS:
        raise ValueError(f"unknown update event {event}/{code} for {subject}")
    row = {"at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "event": event, "code": code, "subject": subject}
    ident = instance(base)
    if ident:
        row["instance"] = ident
    try:
        folder = directory()
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        file = folder / FILE
        if file.is_symlink():
            return
        import log_policy
        log_policy.rotate(file)
        fd = os.open(file, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as out:
            out.write(json.dumps(row, sort_keys=True) + "\n")
    except (OSError, ImportError):
        pass


def read(limit: int = 200) -> list[dict]:
    """The newest `limit` lines, oldest first — for tests and for a person's `tail`."""
    try:
        lines = (directory() / FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out
