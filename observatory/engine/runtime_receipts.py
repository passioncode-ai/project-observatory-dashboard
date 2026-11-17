#!/usr/bin/env python3
"""Every file a live writer changes while an update stages or a snapshot copies (OBS-610).

An update stops the tick and the local server, snapshots the workspace and stages the new
release; then it requires everything but the databases to have held still, and refuses (and
rolls back) when anything moved. Some writers are never stopped: the session hooks of every
coding session, the MCP servers those sessions run, the five-minute update-check job, and a
server whose launchd job something loads again meanwhile. Until 0.41 the files they write were
a hand-kept list in `workspace_upgrade.py`, so each newly found live writer rolled an update
back: the update-check job (2026-11-10), the analytics queue (2026-11-11), the server's receipt
(2026-11-17).

Now each such file is DECLARED here, beside its owner, and its owner imports its path from
here (`SERVERD.at(paths.SCRATCH)`), so the path a writer writes and the path the stillness
check excuses are one value. The stillness checks of `workspace_upgrade` read `live()` and
`foreign()` and nothing else. A file nobody declared still has to hold still, and its change
still refuses the update with its name: config, registry, vault, secrets and the store's
other files keep the guarantee they had.

A declaration may say how to tell a file this workspace's own writer wrote (`belongs`): the
server's receipt names the workspace it serves, so a receipt naming another home, or no JSON
object at all, is something else and still refuses.

Standard library only, and never `paths`: the session hooks import this, and `paths` reads
the workspace's settings at import.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

#: Who is never stopped by an update, and so may write while it stages.
SESSION_HOOK = "session-hook"      # the Stop and SessionStart hooks of every coding session
SESSION_MCP = "session-mcp"        # an MCP server a coding session runs (`project-observatory full mcp`)
CHECK_JOB = "check-job"            # the five-minute update-check job (`maintenance.check_pass`)
SERVER = "server"                  # a local server whose launchd job is loaded again meanwhile
ANY_PROCESS = "any-process"        # every engine process (the append-only logs)
WRITERS = (SESSION_HOOK, SESSION_MCP, CHECK_JOB, SERVER, ANY_PROCESS)


@dataclass(frozen=True)
class Receipt:
    """One file a live writer changes. `path` is workspace-relative; `owners` are the engine
    modules (engine-relative) that write it; `why` is the one line the next reader needs."""
    path: str
    owners: tuple[str, ...]
    writer: str
    why: str
    #: (workspace, file) -> whether this workspace's own writer wrote it. None: any content.
    belongs: Optional[Callable[[Path, Path], bool]] = None

    @property
    def name(self) -> str:
        return PurePosixPath(self.path).name

    @property
    def folder(self) -> str:
        return PurePosixPath(self.path).parent.as_posix()

    def at(self, folder: Path) -> Path:
        """The file inside `folder`, the redirectable directory its owner writes in
        (`paths.SCRATCH` for `store/raw`, the workspace's `store` for `store`)."""
        return Path(folder) / self.name

    def of(self, base: Path) -> Path:
        """The file in the workspace `base` (a reader handed another workspace's home)."""
        return Path(base) / self.path

    def matches(self, name: str) -> bool:
        """The file itself, or its writer's temporary file beside it (`.<name>.<anything>`:
        `atomic.write_json`, `usage_analytics._private_write` and `maintenance.write_state`
        all write the new content there and rename it over the file)."""
        if name == self.path:
            return True
        path = PurePosixPath(name)
        return path.parent.as_posix() == self.folder and path.name.startswith(f".{self.name}.")


def _serves_this_workspace(base: Path, file: Path) -> bool:
    """A server of this workspace writes `workspace` as its own home, whatever port it listens
    on (one workspace has one server, behind its instance lock)."""
    try:
        doc = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    home = doc.get("workspace") if isinstance(doc, dict) else None
    return isinstance(home, str) and bool(home) and os.path.realpath(home) == os.path.realpath(base)


def _hook(path: str, owner: str, why: str) -> Receipt:
    return Receipt(path, (owner,), SESSION_HOOK, why)


# --- the session hooks: written on every agent turn, whether or not anything is stopped -----------

RECORD_TURN = _hook("store/raw/record-turn.json", "tools/record_turn.py", "the Stop hook's receipt of the last turn")
SESSIONS_SEEN = _hook("store/raw/sessions-seen.jsonl", "tools/session_start.py",
                      "the folders a session opened in, appended by the SessionStart hook")
COMPANION_FAULTS = _hook("store/raw/companion-faults.jsonl", "companion_faults.py", "a hook's fault, appended")
WHY_ASKS = _hook("store/raw/why-asks.jsonl", "why_asks.py", "the journal of requests for a why, appended")
WHY_ASKS_PREVIOUS = _hook("store/raw/why-asks.jsonl.1", "why_asks.py", "that journal's previous generation")
WHY_ASKS_LOCK = _hook("store/raw/why-asks.jsonl.lock", "why_asks.py", "the lock its rotation takes")

# --- the update-check job: every five minutes, never stopped for a snapshot (2026-11-10) -----------

UPDATE_CHECK = Receipt("store/update-check.json", ("maintenance.py",), CHECK_JOB,
                       "the check job's own record; the daily snapshot does not stop the job")
UPDATE_CHECK_LOCK = Receipt("store/update-check.lock", ("maintenance.py",), CHECK_JOB, "the check job's lock")

# --- usage analytics: every process records, a session's MCP server included (2026-11-11) ----------

_ANALYTICS = ("usage_analytics.py",)
ANALYTICS_QUEUE = Receipt("store/analytics/queue.jsonl", _ANALYTICS, SESSION_MCP, "the unsent events, appended")
ANALYTICS_COUNTERS = Receipt("store/analytics/counters.json", _ANALYTICS, SESSION_MCP, "the counters")
ANALYTICS_COUNTERS_LOCK = Receipt("store/analytics/.counters.lock", _ANALYTICS, SESSION_MCP, "the counters' lock")
ANALYTICS_NOTICE = Receipt("store/analytics/notice.json", _ANALYTICS, SESSION_MCP, "where the notice was shown")
ANALYTICS_INSTALL_ID = Receipt("store/analytics/install-id", _ANALYTICS, SESSION_MCP,
                               "made by the first event a workspace records, which may be a session's")
ANALYTICS_PROBLEM = Receipt("store/analytics/counters-problem.json", _ANALYTICS, SESSION_MCP,
                            "why the last counter update failed, cleared by the next one")

# --- the local server: its job can be loaded again while an update stages (2026-11-17) --------------

SERVERD = Receipt("store/raw/serverd.json", ("tools/serverd.py",), SERVER,
                  "the server's pid, port and heartbeat; only this workspace's own server's",
                  belongs=_serves_this_workspace)

DECLARED: tuple[Receipt, ...] = (
    RECORD_TURN, SESSIONS_SEEN, COMPANION_FAULTS, WHY_ASKS, WHY_ASKS_PREVIOUS, WHY_ASKS_LOCK,
    UPDATE_CHECK, UPDATE_CHECK_LOCK,
    ANALYTICS_QUEUE, ANALYTICS_COUNTERS, ANALYTICS_COUNTERS_LOCK, ANALYTICS_NOTICE, ANALYTICS_INSTALL_ID,
    ANALYTICS_PROBLEM,
    SERVERD,
)

#: The append-only logs every process writes, with their rotated generations (`x.log.3`):
#: copied mid-line they lose at most that line (audit A11). A family, not a file each.
LOG_FOLDER = "store/logs"
LOG_SUFFIXES = (".log", ".err", ".out", ".jsonl")

#: Files a live writer's module names that still hold still while an update stages: files it
#: only reads, or writes only from a part that the update stops. The declaration test
#: (`test_runtime_receipts`) needs to tell them from an undeclared write.
HOLDS_STILL = {
    "store/raw/vault.json": "tools/record_turn.py reads the tick's vault scan",
    "store/maintenance.json": "the maintenance pass's record; the check job only reads it",
    "store/maintenance.lock": "the maintenance pass's lock, held (never rewritten) while it runs the update",
    "store/maintenance-reload-pending": "written by the maintenance pass after an update, never while one stages",
    "store/raw/hung-commands.json": "the maintenance pass's guard, which never runs beside an update",
    "store/raw/skill-sessions.json": "tools/serverd.py reads the tick's skill scan",
    "store/raw/tick-lease.json": "tools/serverd.py reads the tick's lease receipt",
    "store/raw/tick.json": "tools/serverd.py reads the tick's receipt",
}


def _log(name: str) -> bool:
    path = PurePosixPath(name)
    if path.parts[:2] != tuple(LOG_FOLDER.split("/")):
        return False
    stem, _, tail = path.name.rpartition(".")
    return path.suffix in LOG_SUFFIXES or (tail.isdigit() and PurePosixPath(stem).suffix in LOG_SUFFIXES)


def declaration(name: str) -> Optional[Receipt]:
    """The declaration that covers this workspace-relative path, or None."""
    for receipt in DECLARED:
        if receipt.matches(name):
            return receipt
    return None


def live(name: str) -> bool:
    """Whether a live writer may change this workspace-relative path while an update stages."""
    return declaration(name) is not None or _log(name)


def foreign(base: Path) -> set[str]:
    """The declared files present in BASE that its own writer did not write (a declaration's
    `belongs` says no): those keep the stillness check. Read once per check, after staging."""
    found = set()
    for receipt in DECLARED:
        if receipt.belongs is None:
            continue
        file = Path(base) / receipt.path
        if file.is_file() and not receipt.belongs(Path(base), file):
            found.add(receipt.path)
    return found


def by_writer(writer: str) -> frozenset[str]:
    return frozenset(r.path for r in DECLARED if r.writer == writer)
