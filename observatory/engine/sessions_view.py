#!/usr/bin/env python3
"""One session count, in three facets (OBS-550; docs/design/CONTROL-PLANE.md CP-03, CP-04, D1).

Three sources say three different things, and each is kept as what it is:

* PROCESSES say what runs: the account manager's process scan when it answers, else the
  machine survey's coding-agent origins (`store/raw/machine.json`, `agent:<name>` groups);
* the ACCOUNT MANAGER (Switchboard) says which of them it manages, on which account group,
  read through its CLI (`switchboard sessions --machine --json`) with a timeout. D1: its MCP
  tool answered 0 while the CLI listed 39 sessions in the same minute, so the CLI is the reader;
* the LEDGER says what did work: sessions with a recorded turn in the last hour
  (`agents_view.recent_sessions`, the rows the Stop hook writes).

The page used to show only the third, under the name of the first. A source that could not
be read is `None` with its reason in `degraded`, never 0: "the manager manages nothing" and
"the manager was not asked" must not look alike. An answer that lists fewer sessions than
the process survey counted is reported, not hidden.

`summary()` asks the manager now (one call); the tick writes its answer to
`store/raw/coding-sessions.json` (the name `sessions.json` belongs to `scan_sessions`), and the
pages and the finding read that file through `load()`. Both pages read the numbers through
`facets()`, one function, so Home and Agents can never disagree.

A session row names its folder by its last component and its project by id; a full path is
never written, because a folder path names a person's disk layout.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from typing import Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import paths                                                                       # noqa: E402

#: Seconds the manager's CLI may take before it reads as `timeout`.
MANAGER_TIMEOUT = 10
#: The manager's executable, looked up on PATH, then in the user's own bin folder.
MANAGER = "switchboard"
#: What the tick writes and the pages read.
FILE = "coding-sessions.json"
SCHEMA = "coding-sessions/1"
#: The machine survey counts for the cross-check only while it is at most this old.
SURVEY_FRESH_SECONDS = 1800
#: Bounds on what one summary carries.
MAX_SESSIONS = 500

_UNSET = object()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch_iso(value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return _iso(datetime.fromtimestamp(value, timezone.utc))
    except (OverflowError, OSError, ValueError):
        return None


def _parse_iso(value) -> datetime | None:
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _gap(source: str, code: str, reason: str) -> dict:
    return {"source": source, "code": code, "reason": reason}


def find_manager(which: Callable[[str], str | None] = shutil.which,
                 home: pathlib.Path | None = None) -> str | None:
    """The account manager's CLI: on PATH, else `~/.local/bin/switchboard`; None when absent.
    A launchd job's PATH rarely holds the user's bin folder, hence the second place."""
    found = which(MANAGER)
    if found:
        return found
    local = pathlib.Path(home or pathlib.Path.home()) / ".local" / "bin" / MANAGER
    return str(local) if local.is_file() and os.access(local, os.X_OK) else None


def _run(argv: list[str], timeout: float) -> tuple[int, str]:
    p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, stdin=subprocess.DEVNULL)
    return p.returncode, p.stdout


def _folder_name(folder) -> str | None:
    if not isinstance(folder, str) or not folder.strip():
        return None
    name = folder.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return name or None


def default_attribute() -> Callable[[str], str | None]:
    """A folder's project by the Fabric contract's path rules (folder_attribution), over the
    registry's folders, without Git: one read of the registry for every session, no command
    per folder. A worktree outside every project folder reads no project; the contract's Git
    rules would place it, at three Git reads a session (D1: "where cheap")."""
    import fabric_attribution
    import folder_attribution
    projects = [{"id": pid, "folders": fl, "sources": []}
                for pid, fl in sorted(fabric_attribution.project_folders().items())]
    home, containers = fabric_attribution._real(pathlib.Path.home()), fabric_attribution.containers()

    def attribute(folder: str) -> str | None:
        answer = folder_attribution.attribute_folder({
            "folder": fabric_attribution._real(folder), "home": home, "tempRoots": [], "git": None,
            "projects": projects, "containers": containers})
        return answer.get("project") if answer.get("outcome") == "attached" else None
    return attribute


def _manager(binary: str | None, runner: Callable, degraded: list[dict]) -> tuple[dict | None, dict]:
    """(the manager's `data`, its source line). `data` is None whenever it was not read."""
    if not binary:
        degraded.append(_gap("manager", "not-installed",
                             "the account manager (Switchboard) is not installed: which sessions it manages "
                             "is not known"))
        return None, {"at": None, "state": "not-installed"}
    try:
        code, out = runner([binary, "sessions", "--machine", "--json"], MANAGER_TIMEOUT)
    except subprocess.TimeoutExpired:
        degraded.append(_gap("manager", "timeout", f"the account manager did not answer in {MANAGER_TIMEOUT} s"))
        return None, {"at": None, "state": "timeout"}
    except FileNotFoundError:
        degraded.append(_gap("manager", "not-installed", "the account manager's command is gone"))
        return None, {"at": None, "state": "not-installed"}
    except OSError as exc:
        degraded.append(_gap("manager", "failed", f"the account manager could not be run: {type(exc).__name__}"))
        return None, {"at": None, "state": "failed"}
    if code != 0:
        degraded.append(_gap("manager", "failed", f"the account manager answered exit {code}"))
        return None, {"at": None, "state": "failed"}
    try:
        doc = json.loads(out)
    except ValueError:
        doc = None
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
        degraded.append(_gap("manager", "malformed", "the account manager's answer has no session list"))
        return None, {"at": None, "state": "malformed"}
    return data, {"at": _epoch_iso(data.get("observed_at")), "state": "read"}


def _survey(scratch: pathlib.Path, degraded: list[dict]) -> tuple[dict | None, str | None]:
    """Coding agents per the machine survey: {agent: sessions}, and when it was measured."""
    try:
        doc = json.loads((scratch / "machine.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        degraded.append(_gap("processes", "not-surveyed", "the machine survey has not run yet"))
        return None, None
    except (OSError, ValueError) as exc:
        degraded.append(_gap("processes", "unreadable", f"the machine survey is unreadable: {type(exc).__name__}"))
        return None, None
    groups = ((doc.get("processes") or {}).get("groups") if isinstance(doc, dict) else None) or []
    counts: dict[str, int] = {}
    for g in groups:
        origin = g.get("origin") if isinstance(g, dict) else None
        n = g.get("sessions") if isinstance(g, dict) else None
        if isinstance(origin, str) and origin.startswith("agent:") and isinstance(n, int) and not isinstance(n, bool):
            counts[origin.split(":", 1)[1]] = counts.get(origin.split(":", 1)[1], 0) + n
    return counts, doc.get("measured_at") if isinstance(doc, dict) else None


def _ledger(db: pathlib.Path, now: datetime, degraded: list[dict]) -> int | None:
    if not db.is_file():
        degraded.append(_gap("ledger", "no-store", "the store does not exist yet"))
        return None
    import agents_view
    try:
        conn = sqlite3.connect(paths.sqlite_uri(db), uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            return sum(1 for s in agents_view.recent_sessions(conn, now) if s["active"])
        finally:
            conn.close()
    except sqlite3.Error as exc:
        degraded.append(_gap("ledger", "unreadable", f"the ledger is unreadable: {type(exc).__name__}"))
        return None


def _row(raw, attribute, degraded_attribution: list[str]) -> dict | None:
    if not isinstance(raw, dict):
        return None
    pid, agent = raw.get("pid"), raw.get("agent")
    if isinstance(pid, bool) or not isinstance(pid, int) or not isinstance(agent, str) or not agent:
        return None
    project = None
    folder = raw.get("folder")
    if isinstance(folder, str) and folder and attribute is not None:
        try:
            project = attribute(folder)
        except Exception as exc:  # noqa: BLE001: a folder unattributed, never a lost session
            degraded_attribution.append(type(exc).__name__)
    group = raw.get("pool") if isinstance(raw.get("pool"), str) else raw.get("account_group")
    quiet = raw.get("quiet") is True
    return {"agent": agent, "project": project if isinstance(project, str) else None,
            "folder_name": _folder_name(folder), "pid": pid, "managed": raw.get("managed") is True,
            "launch_id": raw.get("launch_id") if isinstance(raw.get("launch_id"), str) else None,
            "account_group": group if isinstance(group, str) else None,
            "last_turn_at": _epoch_iso(raw.get("last_write_at")), "quiet": quiet,
            "state": "quiet" if quiet else "working"}


def summary(home: pathlib.Path | None = None, *, now: datetime | None = None,
            runner: Callable[[list[str], float], tuple[int, str]] | None = None,
            manager=_UNSET, attribute=_UNSET) -> dict:
    """The contract `sessions_view.summary()` (CONTROL-PLANE.md): processes per agent, of them
    managed, of them with a turn in the last hour, the sessions, the sources and `degraded`.

    `home` is a workspace (its `store/`); None reads this engine's own. `runner`, `manager`
    (the CLI's path, None for not installed) and `attribute` (folder -> project id) are
    injected by tests; by default the CLI is found, run once, and folders are attributed by
    the contract's path rules."""
    now = now or _now()
    scratch = pathlib.Path(home) / "store" / "raw" if home else paths.SCRATCH
    db = pathlib.Path(home) / "store" / "observatory.db" if home else paths.DB
    degraded: list[dict] = []
    binary = find_manager() if manager is _UNSET else manager
    data, manager_source = _manager(binary, runner or _run, degraded)
    survey, survey_at = _survey(scratch, degraded)

    sessions: list[dict] = []
    if data is not None:
        if attribute is _UNSET:
            try:
                attribute = default_attribute()
            except Exception as exc:  # noqa: BLE001: sessions without projects, never no sessions
                degraded.append(_gap("attribution", "unavailable",
                                     f"projects could not be read for attribution: {type(exc).__name__}"))
                attribute = None
        failed: list[str] = []
        raw = data["sessions"]
        for item in raw[:MAX_SESSIONS]:
            r = _row(item, attribute, failed)
            if r is not None:
                sessions.append(r)
        skipped = min(len(raw), MAX_SESSIONS) - len(sessions)
        if skipped:
            degraded.append(_gap("manager", "rows-skipped",
                                 f"{skipped} row(s) of the account manager's answer were not sessions"))
        if len(raw) > MAX_SESSIONS:
            degraded.append(_gap("manager", "truncated", f"only the first {MAX_SESSIONS} sessions are kept"))
        if failed:
            degraded.append(_gap("attribution", "failed",
                                 f"{len(failed)} folder(s) could not be attributed: {sorted(set(failed))[0]}"))

    if data is not None:
        processes: dict[str, int] = {}
        for s in sessions:
            processes[s["agent"]] = processes.get(s["agent"], 0) + 1
        processes_source = {"at": manager_source["at"], "from": "manager"}
        fresh = _parse_iso(survey_at)
        if survey and fresh and abs((now - fresh).total_seconds()) <= SURVEY_FRESH_SECONDS:
            fewer = [f"{agent} {processes.get(agent, 0)} of {n}" for agent, n in sorted(survey.items())
                     if processes.get(agent, 0) < n]
            if fewer:
                degraded.append(_gap("manager", "fewer-than-processes",
                                     "the account manager listed fewer sessions than the process survey counted: "
                                     + ", ".join(fewer)))
    else:
        processes = dict(sorted((survey or {}).items()))
        processes_source = {"at": survey_at, "from": "machine-survey" if survey is not None else None}

    active = _ledger(db, now, degraded)
    return {
        "schema": SCHEMA, "measuredAt": _iso(now),
        "processes": processes,
        "managed": sum(1 for s in sessions if s["managed"]) if data is not None else None,
        "quiet": sum(1 for s in sessions if s["quiet"]) if data is not None else None,
        "active_hour": active,
        "sessions": sessions,
        "sources": {"processes": processes_source, "manager": manager_source,
                    "ledger": {"at": _iso(now) if active is not None else None}},
        "degraded": degraded,
    }


def write(doc: dict, out: pathlib.Path | None = None) -> pathlib.Path:
    import atomic
    out = pathlib.Path(out or paths.SCRATCH / FILE)
    out.parent.mkdir(parents=True, exist_ok=True)
    return atomic.write_json(out, doc)


def _empty(code: str, reason: str) -> dict:
    return {"schema": SCHEMA, "measuredAt": None, "processes": {}, "managed": None, "quiet": None,
            "active_hour": None, "sessions": [],
            "sources": {"processes": {"at": None, "from": None}, "manager": {"at": None, "state": None},
                        "ledger": {"at": None}},
            "degraded": [_gap("sessions", code, reason)]}


def load(home: pathlib.Path | None = None) -> dict:
    """What the tick last wrote; a missing or unreadable file is a summary that says so."""
    path = (pathlib.Path(home) / "store" / "raw" if home else paths.SCRATCH) / FILE
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty("not-collected", "the coding sessions have not been read yet: the next tick reads them")
    except (OSError, ValueError) as exc:
        return _empty("unreadable", f"the coding sessions file is unreadable: {type(exc).__name__}")
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return _empty("unreadable", "the coding sessions file is not coding-sessions/1")
    return doc


def facets(doc: dict | None) -> dict:
    """The three numbers and their sources, as Home's tile and the Agents page show them."""
    doc = doc if isinstance(doc, dict) else {}
    sources = doc.get("sources") or {}
    processes = doc.get("processes") if isinstance(doc.get("processes"), dict) else {}
    processes_from = (sources.get("processes") or {}).get("from")

    def count(value):
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    return {"running": sum(processes.values()) if processes_from else None,
            "byAgent": dict(sorted(processes.items())),
            "managed": count(doc.get("managed")),
            "activeHour": count(doc.get("active_hour")),
            "processesFrom": processes_from,
            "processesAt": (sources.get("processes") or {}).get("at"),
            "managerState": (sources.get("manager") or {}).get("state"),
            "managerAt": (sources.get("manager") or {}).get("at"),
            "ledgerAt": (sources.get("ledger") or {}).get("at"),
            "measuredAt": doc.get("measuredAt")}


def main(argv: list[str]) -> int:
    """`sessions_view.py [OUT]`: read the three sources now and write the summary (the tick's step)."""
    out = pathlib.Path(argv[1]) if len(argv) > 1 else paths.SCRATCH / FILE
    doc = summary()
    write(doc, out)
    f = facets(doc)
    print(f"coding-sessions: {f['running'] if f['running'] is not None else 'unknown'} running, "
          f"{f['managed'] if f['managed'] is not None else 'unknown'} managed, "
          f"{f['activeHour'] if f['activeHour'] is not None else 'unknown'} active in the last hour"
          + (f"; degraded: {', '.join(sorted({d['code'] for d in doc['degraded']}))}" if doc["degraded"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
