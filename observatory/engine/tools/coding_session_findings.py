#!/usr/bin/env python3
"""Coding sessions outside a lifecycle owner (OBS-550; CONTROL-PLANE.md CP-04, D9), and the sessions watch (OBS-630).

Reads `store/raw/coding-sessions.json` (sessions_view.py, written by the tick). A coding
session should run through a lifecycle owner, the account manager (Switchboard) or an
application's own embedded agent, so its start, limits, hand-over, end and logs are
observed. One `session.unmanaged` row says how many do not, never one row per
session: the count is the fact, and a page of identical rows hides the rest of the board.
Since OBS-630 a session in a group folder the manager routes counts as managed, and the row
is a warning when the manager answered, so `tools/notify_findings.py` sends it to the
operator once; with no manager installed it stays info (a setup to make, not an incident).

THE WATCH (OBS-630, `monitor_findings`): over the summary's host sample, groups and session
rows (sessions_monitor.py), a critical host (`host.overloaded`), a group with no account with
room (`pool.no_room`), a session served by another group than its folder's
(`session.wrong_group`) and a session busy with no turn (`session.stalled`). Each row names
its `owner` (the operator, or the account manager's settings) and its fix (`action`, naming a
tool, never a shell command). A value that was not read raises nothing.

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
_ENGINE = str(_pathlib.Path(__file__).resolve().parents[1])
if _ENGINE not in _sys.path:
    _sys.path.append(_ENGINE)  # sessions_monitor: the settings and a session's key

import json
import pathlib

import sessions_monitor  # noqa: E402

TYPE = "session.unmanaged"
#: Who fixes a finding: the operator, or the account manager's own settings.
OWNER_OPERATOR, OWNER_MANAGER = "operator", "account-manager"
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


def findings(doc: dict | None, config: dict | None = None) -> list[dict]:
    """`session.unmanaged` and the watch's rows (`monitor_findings`)."""
    return unmanaged(doc) + monitor_findings(doc, config)


def unmanaged(doc: dict | None) -> list[dict]:
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
        routed = any("route" in s for s in sessions)  # the watch read the folder routes
        return [{
            "type": TYPE, "subject": "machine:sessions", "severity": "warning", "owner": OWNER_OPERATOR,
            **titled("{n} coding sessions run outside a lifecycle owner", n=len(unmanaged)),
            "detail": (f"The account manager reads {len(sessions)} coding session(s) on this machine and manages "
                       f"{len(sessions) - len(unmanaged)}; the others started outside it and outside any "
                       f"application's embedded agent, so their limits, hand-over and end are not observed. "
                       f"By agent: {_by_agent(unmanaged)}"
                       + (f"; quiet for 30 minutes or more: {quiet}" if quiet else "")
                       + (f". By project: {where}." if where else ".")),
            "action": (("Work in a folder of an account group: the account manager routes a Claude Code "
                        "session started there (switchboard_project_edit adds a folder to a group). " if routed
                        else "") +
                       "Start coding sessions through the account manager, from its window or with "
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
            "type": TYPE, "subject": "machine:sessions", "severity": "info", "owner": OWNER_OPERATOR,
            **titled("{n} coding sessions run without an account manager", n=running),
            "detail": (f"The process survey counts {running} coding session(s) here "
                       f"({', '.join(f'{a} {n}' for a, n in sorted(processes.items()))}), and no account manager is "
                       f"installed to manage them. The account manager (Fabric Switchboard) {ADDS}."),
            "action": (f"Set it up with `{SETUP}`, which names the install step for this machine; then start "
                       "coding sessions from it, or from an application's embedded agent, so their limits and "
                       "hand-over are managed."),
            "evidence": ["store/raw/coding-sessions.json", "store/raw/machine.json"]}]
    return []


def monitor_findings(doc: dict | None, config: dict | None = None) -> list[dict]:
    """The watch over the summary (OBS-630): a critical host, a group with no account with room, a session served by
    another group, a stalled session. `config` holds the settings (config/machine.json by default)."""
    if not isinstance(doc, dict):
        return []
    s = sessions_monitor.settings(config)
    out: list[dict] = []
    host = doc.get("host") if isinstance(doc.get("host"), dict) else {}
    load = sessions_monitor._num(host.get("load_per_core"))
    pressure = host.get("memory_pressure")
    over_load = load is not None and load >= s["sessions_load_per_core_critical"]
    over_pressure = sessions_monitor.PRESSURE_LEVEL.get(pressure, 0) >= s["sessions_memory_pressure_critical"]

    def known(value, unit=""):
        return f"{value}{unit}" if value is not None else "not read"
    if over_load or over_pressure:
        out.append({
            "type": "host.overloaded", "subject": "host:sessions",
            "severity": "critical" if over_load and over_pressure else "warning", "owner": OWNER_OPERATOR,
            **(titled("Host overloaded: load per core and memory pressure over their lines")
               if over_load and over_pressure else titled("Host overloaded: load per core over its line")
               if over_load else titled("Host overloaded: memory pressure over its line")),
            "detail": (f"Load per core {known(load)} (line {s['sessions_load_per_core_critical']}), memory pressure "
                       f"{known(pressure)}, swap {known(host.get('swap_gb'), ' GB')}; "
                       f"{known(host.get('sessions_used'))} live sessions of a cap of {known(host.get('sessions_cap'))}."),
            "action": ("End the sessions that wait or are quiet (the account manager's Sessions page), lower its cap "
                       "on live sessions (switchboard_capacity), then quit what holds the most memory (the Machine "
                       "page). The lines are the settings machine.sessions_load_per_core_critical and "
                       "machine.sessions_memory_pressure_critical."),
            "evidence": ["store/raw/coding-sessions.json#host"]})
    groups = doc.get("groups") if isinstance(doc.get("groups"), dict) else {}
    for group, g in sorted(groups.items()):
        if not isinstance(g, dict) or not g.get("accounts") or g.get("with_room") or g.get("unknown"):
            continue
        reset = g.get("next_reset_at")
        out.append({
            "type": "pool.no_room", "subject": f"pool:{group}", "severity": "warning", "owner": OWNER_MANAGER,
            **titled("Group {group} has no account with room", group=group),
            "detail": (f"All {g['accounts']} account(s) of the group are at their limit"
                       + (f"; the nearest reset is {reset}." if reset else ".")
                       + " Its sessions stop at their next request until an account has room."),
            "action": ("Add an account with room to the group, or let the account manager lend one from another "
                       "group (its balance setting, switchboard_balance); otherwise its sessions wait for the reset."),
            "evidence": ["store/raw/coding-sessions.json#groups"]})
    stall = s["sessions_stall_minutes"]
    for r in doc.get("sessions") or []:
        if not isinstance(r, dict):
            continue
        key = sessions_monitor.session_key(r)
        where = str(r.get("project") or r.get("folder_name") or "a folder")
        own, served = r.get("folder_group"), r.get("account_group")
        if key and own and served and own != served:
            out.append({
                "type": "session.wrong_group", "subject": f"session:{key}", "severity": "warning",
                "owner": OWNER_MANAGER,
                **titled("A session in group {own} is served by group {served}", own=own, served=served),
                "detail": (f"A {r.get('agent')} session in {where} runs in a folder of group {own} and is served by "
                           f"an account of group {served}, so its use counts against the other group."),
                "action": ("Move the session to its own group's account (switchboard_switch), or move the folder into "
                           "the group that serves it (switchboard_project_edit)."),
                "evidence": ["store/raw/coding-sessions.json#sessions"]})
        quiet = sessions_monitor._int(r.get("quiet_minutes"))
        if key and r.get("busy") is True and quiet is not None and quiet >= stall:
            out.append({
                "type": "session.stalled", "subject": f"session:{key}", "severity": "warning",
                "owner": OWNER_OPERATOR,
                **titled("A {agent} session in {where} is busy with no turn", agent=str(r.get("agent")), where=where),
                "detail": (f"The account manager counts it as working, and it has written nothing for {quiet} "
                           f"minutes (the line is {stall}, the setting machine.sessions_stall_minutes)."),
                "action": ("Open the session and answer what it waits on, or end it and continue its work in a new "
                           "session (switchboard_session_end, then switchboard_session_continue); the account "
                           "manager's duty officer does the same for a managed one."),
                "evidence": ["store/raw/coding-sessions.json#sessions"]})
    return out


def from_file(path: pathlib.Path) -> list[dict]:
    try:
        return findings(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []
