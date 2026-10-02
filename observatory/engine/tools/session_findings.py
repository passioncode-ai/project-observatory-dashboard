#!/usr/bin/env python3
"""Folders agents actually opened that the registry never joined.

THE GAP THIS CLOSES. The registry knows a project by its folder under the
projects root or by a repository it has scanned; an agent knows a project by
where it was told to work. When those disagree — a checkout outside the
projects root, a folder the tick has not reached, an organisation that exists
only as a forge owner with no local checkout — the board showed nothing and
the operator had to remember what the board could not.
The SessionStart hook writes every such sighting to
`store/raw/sessions-seen.jsonl` (cwd, remote as owner/name, session, when);
this rule reads it back as ONE row, so the disagreement is a measured fact
with a decision attached rather than a memory.

WHAT IT IS NOT. Not a defect in any project: a folder outside the projects root
is the operator's choice, and the remedy is theirs — move it under the root, or
say it is not to be watched. One row, the folders named, worst-first by how
many sessions opened them, over the last `DAYS` days.
"""
from __future__ import annotations
import collections
import datetime
import json
import pathlib
import pathlib as _pathlib
import sys as _sys
_DASHBOARD = str(_pathlib.Path(__file__).resolve().parents[1] / "dashboard")
if _DASHBOARD not in _sys.path:
    _sys.path.append(_DASHBOARD)  # `finding_types.titled`: a title is a message id
from finding_types import titled  # noqa: E402

DAYS = 14
LISTED = 6


def rows_of(path: pathlib.Path, now: datetime.datetime | None = None) -> list[dict]:
    if not path.is_file():
        return []
    now = now or datetime.datetime.now(datetime.timezone.utc)
    since = (now - datetime.timedelta(days=DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if (r.get("at") or "") >= since and r.get("cwd"):
                out.append(r)
    except OSError:
        return []
    return out


def findings(path: pathlib.Path, known_folders: set[str], data_root: pathlib.Path,
             now: datetime.datetime | None = None) -> list[dict]:
    """`known_folders` are the estate folders the registry already joins; a
    sighting inside the estate whose folder is now known has been picked up by
    a tick since, and is not reported."""
    seen = rows_of(path, now)
    if not seen:
        return []
    by_cwd: dict[str, list[dict]] = collections.defaultdict(list)
    for r in seen:
        by_cwd[r["cwd"]].append(r)
    still: list[tuple[str, int, str | None]] = []
    for cwd, rs in by_cwd.items():
        p = pathlib.Path(cwd)
        try:
            folder = p.resolve().relative_to(data_root.resolve()).parts[0]
            if folder in known_folders:
                continue                                                    
        except (ValueError, IndexError, OSError):
            pass
        remote = next((r.get("remote") for r in rs if r.get("remote")), None)
        still.append((cwd, len({r.get("session_id") for r in rs}), remote))
    if not still:
        return []
    still.sort(key=lambda t: (-t[1], t[0]))
    total_sessions = sum(n for _c, n, _r in still)
    named = ", ".join(f"{pathlib.Path(c).name} ({n} sess.{', ' + r if r else ''})" for c, n, r in still[:LISTED])
    more = len(still) - min(len(still), LISTED)
    return [{
        "type": "project.seen_unobserved",
        "subject": "estate:sessions-seen",
        "severity": "info",
        **titled("{n} folders agents worked in over {days} days are not in the registry", n=len(still), days=DAYS),
        "detail": (f"{named}{f' and {more} more' if more > 0 else ''} — {total_sessions} session(s) in all. "
                   f"Each was where an agent was told to work and the board could show nothing "
                   f"about it: no keys by name, no findings, no history. A folder under the configured project directory "
                   f"joins on the next tick; one elsewhere joins only if it moves."),
        "action": ("move the folder under the configured project directory, or update "
                   "sources.projects; otherwise this row records a project outside the scan scope"),
    }]
