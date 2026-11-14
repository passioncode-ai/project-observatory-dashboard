#!/usr/bin/env python3
"""Coding sessions outside a lifecycle owner (OBS-550; CONTROL-PLANE.md CP-04, D9).

Reads `store/raw/coding-sessions.json` (sessions_view.py, written by the tick). A coding
session should run through a lifecycle owner, the account manager (Switchboard) or an
application's own embedded agent, so its start, limits, hand-over, end and logs are
observed. One `session.unmanaged` row (info) says how many do not, never one row per
session: the count is the fact, and a page of identical rows hides the rest of the board.

Two cases raise it: the manager answered and lists unmanaged sessions, or the manager is
not installed (then the row says what it adds and how to set it up). A manager that was
installed but failed, timed out or answered nonsense raises nothing here: that is a gap in
the reading (`degraded`), not a fact about the sessions.

NO PATH IN A FINDING: sessions are named by agent and project id only.
"""
from __future__ import annotations
import pathlib as _pathlib
import sys as _sys
_DASHBOARD = str(_pathlib.Path(__file__).resolve().parents[1] / "dashboard")
if _DASHBOARD not in _sys.path:
    _sys.path.append(_DASHBOARD)  # `finding_types.titled`: a title is a message id
from finding_types import titled  # noqa: E402

import json
import pathlib

TYPE = "session.unmanaged"
LISTED = 6
SETUP = "npx @passioncode-ai/passioncode doctor"
ADDS = ("starts each session on an account group's account, moves it to the next account before a "
        "limit stops it, hands its work over when one does, ends it, and keeps its logs")


def _by_agent(rows: list[dict]) -> str:
    counts: dict[str, int] = {}
    for r in rows:
        counts[str(r.get("agent") or "unknown")] = counts.get(str(r.get("agent") or "unknown"), 0) + 1
    return ", ".join(f"{agent} {n}" for agent, n in sorted(counts.items(), key=lambda x: (-x[1], x[0])))


def _projects(rows: list[dict]) -> str:
    counts: dict[str, int] = {}
    for r in rows:
        if r.get("project"):
            counts[r["project"]] = counts.get(r["project"], 0) + 1
    top = sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:LISTED]
    return ", ".join(f"{p} ({n})" for p, n in top)


def findings(doc: dict | None) -> list[dict]:
    if not isinstance(doc, dict):
        return []
    state = ((doc.get("sources") or {}).get("manager") or {}).get("state")
    if state == "read":
        sessions = [s for s in doc.get("sessions") or [] if isinstance(s, dict)]
        unmanaged = [s for s in sessions if not s.get("managed")]
        if not unmanaged:
            return []
        quiet = sum(1 for s in unmanaged if s.get("quiet"))
        where = _projects(unmanaged)
        return [{
            "type": TYPE, "subject": "machine:sessions", "severity": "info",
            **titled("{n} coding sessions run outside a lifecycle owner", n=len(unmanaged)),
            "detail": (f"The account manager reads {len(sessions)} coding session(s) on this machine and manages "
                       f"{len(sessions) - len(unmanaged)}; the others started outside it and outside any "
                       f"application's embedded agent, so their limits, hand-over and end are not observed. "
                       f"By agent: {_by_agent(unmanaged)}"
                       + (f"; quiet for 30 minutes or more: {quiet}" if quiet else "")
                       + (f". By project: {where}." if where else ".")),
            "action": ("Start coding sessions through the account manager, from its window or with "
                       "`switchboard launch --provider claude --working-directory FOLDER` (codex likewise), or "
                       "from an application's embedded agent; a running session keeps its process until it ends. "
                       "`observatory_sessions` lists which ones are unmanaged."),
            "evidence": ["store/raw/coding-sessions.json"]}]
    if state == "not-installed":
        processes = doc.get("processes") if isinstance(doc.get("processes"), dict) else {}
        running = sum(n for n in processes.values() if isinstance(n, int) and not isinstance(n, bool))
        if not running:
            return []
        return [{
            "type": TYPE, "subject": "machine:sessions", "severity": "info",
            **titled("{n} coding sessions run without an account manager", n=running),
            "detail": (f"The process survey counts {running} coding session(s) here "
                       f"({', '.join(f'{a} {n}' for a, n in sorted(processes.items()))}), and no account manager is "
                       f"installed to manage them. The account manager (Fabric Switchboard) {ADDS}."),
            "action": (f"Set it up with `{SETUP}`, which names the install step for this machine; then start "
                       "coding sessions from it, or from an application's embedded agent, so their limits and "
                       "hand-over are managed."),
            "evidence": ["store/raw/coding-sessions.json", "store/raw/machine.json"]}]
    return []


def from_file(path: pathlib.Path) -> list[dict]:
    try:
        return findings(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []
