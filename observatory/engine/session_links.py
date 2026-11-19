#!/usr/bin/env python3
"""Links that hand a coding session to Fabric (fabric-session-links/1), and the runner session ids they need.

The person asked for a new session to start from Observatory, and for a running one to open from it: open it,
open its terminal, move it into Fabric. Observatory starts nothing itself. Fabric owns the one launch path, so
every surface here hands Fabric a `fabric://` link (fabric-agent-contract DEC-0040, extended by Fabric's
`fabric-session-links/1`):

* `fabric://session/new?project=<id|slug>&from=project-observatory`: Fabric opens the launch for the project;
  the person presses Start there;
* `fabric://session/open?session=<id>&from=project-observatory`: Fabric shows the session (its own console, or
  the session's last messages when it runs outside Fabric);
* `fabric://session/continue?session=<runner session id>&from=project-observatory`: Fabric asks once, then
  stops the session where it runs and resumes it in its own console.

A link carries ids only, never words, a path or a secret, and it opens a view: the act is the person's press in
Fabric. `project` is Fabric's own id for the project (`fabric:<uuid>`, the alias Observatory pulled from Fabric,
OBS-160) when the registry holds one, else the project's slug, which Fabric reads as its slug.

THE RUNNER SESSION ID. The account manager's machine list (`switchboard sessions --machine --json`) names a
process, never its session; its `--rows` list does, but took ten seconds on a real machine, past the tick's
budget. Claude Code itself writes `sessions/<pid>.json` (pid, `sessionId`, `startedAt` in ms) under its config
home: `~/.claude`, or a home the account manager made (`runtimes/*` and `homes/*` of its data folder). A file
is believed only when its pid matches and it started within `START_SLACK_SECONDS` of the process the manager
listed, so a file left by an ended process whose pid was reused is never read as this session. A session with
no believable file has no id, and its row offers no open or continue: never a guess.

The terminal opens on this machine through the key server (`session-terminal`), which finds the folder in the
same file by the session id: the folder never reaches a page.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
from urllib.parse import quote

#: The `from` every link carries: how Fabric's launch journal names this app.
FROM = "project-observatory"
SCHEME = "fabric://session"
#: A runner session id: Claude Code's are UUIDs; the grammar is kept to what a link may carry.
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}")
#: What `project=` may carry: a `fabric:<uuid>` alias's uuid or a project slug.
PROJECT_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}")
#: How far a session file's start may sit from the process's own (the manager's `started_at` is in seconds).
START_SLACK_SECONDS = 120
#: The account manager's data folder, per platform, under the user's home.
MANAGER_DATA = {"darwin": ("Library/Application Support/ai.passioncode.fabric-switchboard",),
                "linux": (".local/share/ai.passioncode.fabric-switchboard",)}
#: Agents whose session files this module reads.
RUNNERS = ("claude-code",)


def project_ref(project_id: str | None, fabric_alias: str | None = None) -> str | None:
    """What `project=` carries for an Observatory project: Fabric's uuid when the alias is known, else the slug."""
    if isinstance(fabric_alias, str) and fabric_alias.startswith("fabric:"):
        ref = fabric_alias.split(":", 1)[1]
        if PROJECT_REF.fullmatch(ref):
            return ref
    if isinstance(project_id, str) and project_id.startswith("project:"):
        ref = project_id.split(":", 1)[1]
        if PROJECT_REF.fullmatch(ref):
            return ref
    return None


def new_session(project_id: str | None, fabric_alias: str | None = None) -> str | None:
    """`fabric://session/new` for a project, or None when it has no id a link may carry."""
    ref = project_ref(project_id, fabric_alias)
    return f"{SCHEME}/new?project={quote(ref, safe=':')}&from={FROM}" if ref else None


def _session_link(verb: str, session_id) -> str | None:
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        return None
    return f"{SCHEME}/{verb}?session={session_id}&from={FROM}"


def open_session(session_id) -> str | None:
    return _session_link("open", session_id)


def continue_session(session_id) -> str | None:
    return _session_link("continue", session_id)


def links(session_id) -> dict | None:
    """The two links of one session, or None when it has no id (a row then offers neither)."""
    opened, moved = open_session(session_id), continue_session(session_id)
    return {"open": opened, "continue": moved} if opened and moved else None


def session_dirs(home: pathlib.Path | None = None, platform: str | None = None) -> list[pathlib.Path]:
    """Every folder Claude Code writes its per-process session files to: the default home's, then each home the
    account manager made. Folders that do not exist are left out; reading them is the caller's."""
    home = pathlib.Path(home) if home else pathlib.Path.home()
    out = [home / ".claude" / "sessions"]
    for rel in MANAGER_DATA.get(platform or sys.platform, ()):
        base = home / rel
        for kind in ("runtimes", "homes"):
            try:
                out += sorted(p / "sessions" for p in (base / kind).iterdir() if p.is_dir())
            except OSError:
                continue
    return [d for d in out if d.is_dir()]


def _read(path: pathlib.Path) -> dict | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def runner_session(pid, started_at, dirs: list[pathlib.Path]) -> dict | None:
    """The session file of one running process, when it is believable: {"sessionId", "cwd"} or None."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    for d in dirs:
        doc = _read(d / f"{pid}.json")
        if doc is None or doc.get("pid") != pid:
            continue
        sid, began = doc.get("sessionId"), doc.get("startedAt")
        if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
            continue
        if isinstance(started_at, (int, float)) and not isinstance(started_at, bool):
            if not isinstance(began, (int, float)) or abs(began / 1000 - started_at) > START_SLACK_SECONDS:
                continue
        return {"sessionId": sid, "cwd": doc.get("cwd") if isinstance(doc.get("cwd"), str) else None}
    return None


def resolver(dirs: list[pathlib.Path] | None = None):
    """`(pid, started_at, agent) -> session id or None`, over the folders read once."""
    found = session_dirs() if dirs is None else dirs

    def resolve(pid, started_at, agent) -> str | None:
        if agent not in RUNNERS:
            return None
        hit = runner_session(pid, started_at, found)
        return hit["sessionId"] if hit else None
    return resolve


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def folder_of(session_id, dirs: list[pathlib.Path] | None = None) -> str | None:
    """The folder a RUNNING session works in, found by its id in its own session file: what the key server's
    terminal opens. None when no live process holds that session, so an ended or unknown one opens nothing."""
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        return None
    for d in session_dirs() if dirs is None else dirs:
        try:
            files = sorted(d.glob("*.json"))
        except OSError:
            continue
        for f in files:
            doc = _read(f)
            if not doc or doc.get("sessionId") != session_id:
                continue
            pid, cwd = doc.get("pid"), doc.get("cwd")
            if (isinstance(pid, int) and not isinstance(pid, bool) and f.stem == str(pid) and _alive(pid)
                    and isinstance(cwd, str) and os.path.isabs(cwd) and os.path.isdir(cwd)):
                return cwd
    return None
