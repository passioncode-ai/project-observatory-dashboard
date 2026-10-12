#!/usr/bin/env python3
"""What the machine survey owes the operator: space, memory, leftovers.

Reads what collectors/scan_machine.py, collectors/scan_git_hygiene.py and
tools/cleanup.py wrote, and turns the parts that need a person into findings.
Each finding names the biggest contributors and the command that recovers the
space — the board does not run it: this page reads, the operator acts.

NO TIMESTAMP INSIDE A FINDING beyond the documents' own.
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
from datetime import datetime, timedelta, timezone

#: Interpreters whose detached instances are usually a session's server left behind.
SERVERS = ("npm:", "node", "python", "Python", "bun", "deno", "uv", "uvx")
LISTED = 5


def _load(path: pathlib.Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _age_hours(stamp: str | None, now: datetime) -> float | None:
    try:
        return (now - datetime.strptime(stamp or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds() / 3600
    except ValueError:
        return None


def _auto_cleanup() -> bool:
    try:
        import configuration
        return configuration.enabled("auto_cleanup", "features")
    except Exception:  # noqa: BLE001 — a finding never fails over a settings read
        return False


def findings(scratch: pathlib.Path, logs: pathlib.Path, config: dict, now: datetime | None = None,
             auto_cleanup: bool | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    out: list[dict] = []
    machine = _load(scratch / "machine.json")
    hygiene = _load(scratch / "git-hygiene.json")
    guard = _load(scratch / "disk-guard.json")
    auto = _auto_cleanup() if auto_cleanup is None else auto_cleanup

    # THE GUARD ITSELF. On 2026-10-11 the self-cleaning had not run for a day and nothing
    # said so: its steps were "not started" in a tick too long to reach them. A guard that
    # should act at least hourly and has not reported for three hours is a finding of its own.
    if auto:
        age = _age_hours((guard or {}).get("at"), now)
        if age is None or age > config.get("guard_silent_hours", 3):
            out.append({"type": "machine.guard_idle", "subject": "machine", "severity": "warning",
                        **(titled("the disk guard has not run for {hours} hours", hours=f"{age:.0f}")
                           if age is not None else titled("the disk guard has never run")),
                        "detail": ("features.auto_cleanup is on, and the maintenance job (every 15 minutes) runs the guard "
                                   "first in every pass; without it nothing frees space as the disk fills."),
                        "action": "`project-observatory full maintain status`; `full disk-guard --apply` runs it now"})

    if machine:
        age = _age_hours(machine.get("measured_at"), now)
        if age is not None and age > 2:
            out.append({"type": "machine.stale", "subject": "machine", "severity": "warning",
                        **titled("the machine survey is {hours} hours old", hours=f"{age:.0f}"),
                        "detail": "Processes and memory change by the minute; this page is reading an old picture.",
                        "action": "check the tick log for the `machine` step"})

        vol = (machine.get("disk") or {}).get("volume") or {}
        pct = vol.get("free_percent")
        warn = config.get("free_space_warning_percent", 15)
        crit = config.get("free_space_critical_percent", 5)
        if pct is not None and pct < warn:
            locs = (machine.get("disk") or {}).get("locations") or []
            # A place full of APFS clones gives back far less than `du` says (OBS-66).
            named = "; ".join(f"{l['label']} {l['gb']} GB"
                              + (f", of which removal frees at least {l['frees_at_least_gb']} GB (APFS clones)"
                                 if l.get("clones_matter") else "")
                              + (f" (`{l['command']}`)" if l.get("command") else "")
                              for l in locs[:LISTED])
            swap = next((l for l in locs if l.get("kind") == "swap"), None)
            if swap and swap["gb"] >= 2:
                named += (f". {swap['gb']} GB of it is swap — memory the machine wrote to this same disk; "
                          f"closing what holds the most memory gives that space back")
            out.append({"type": "machine.disk_low", "subject": "machine",
                        "severity": "critical" if pct < crit else "warning",
                        **titled("{gb} GB free on the disk ({pct}%)", gb=vol.get("free_gb"), pct=pct),
                        "detail": (f"Below the {crit if pct < crit else warn}% line in config/machine.json. "
                                   f"Largest measured places outside the projects: {named or 'none measured yet'}."
                                   + _guard_detail(guard)),
                        "action": "review the Machine page's disk table; each row names how its space comes back"})

        mem = machine.get("memory") or {}
        groups = (machine.get("processes") or {}).get("groups") or []
        swap = mem.get("swap_used_mb") or 0
        if swap > 4096:
            heavy = ", ".join(f"{g['origin']} {g['rss_mb'] / 1024:.1f} GB" for g in groups[:LISTED])
            out.append({"type": "machine.memory_pressure", "subject": "machine", "severity": "warning",
                        **titled("{gb} GB of memory is swapped to disk", gb=f"{swap / 1024:.1f}"),
                        "detail": f"Largest by origin: {heavy}.",
                        "action": "close what the Machine page shows as largest and idle"})
        limit = config.get("memory_group_warning_mb", 4096)
        for g in groups:
            if g["rss_mb"] >= limit and not g["origin"].startswith(("system", "agent:")):
                out.append({"type": "machine.heavy_origin", "subject": f"machine:{g['origin']}", "severity": "info",
                            **titled("{origin} holds {gb} GB in {n} processes", origin=g["origin"], gb=f"{g['rss_mb'] / 1024:.1f}", n=g["processes"]),
                            "detail": "Above memory_group_warning_mb in config/machine.json.",
                            "action": "quit it if it is not in use"})

        detached = [g for g in groups if g["origin"].startswith("detached:")
                    and g["origin"].split(":", 1)[1].startswith(SERVERS)]
        if detached:
            total = sum(g["rss_mb"] for g in detached)
            out.append({"type": "machine.detached_servers", "subject": "machine", "severity": "info",
                        **titled("{n} detached server processes hold {mb} MB", n=sum(g["processes"] for g in detached), mb=f"{total:.0f}"),
                        "detail": ("Interpreters whose parent is launchd and which no job, app or agent session explains — "
                                   "usually an MCP or dev server that outlived the session that started it: "
                                   + ", ".join(g["origin"].split(":", 1)[1] for g in detached[:LISTED]) + "."),
                        "action": "`project-observatory full machine --explain PID` says what started one; stop it if nothing uses it"})

    if hygiene:
        dirty = [(c["repository"], w) for c in hygiene.get("checkouts") or [] for w in c.get("worktrees") or []
                 if w.get("state") == "dirty" and not w.get("busy") and (w.get("idle_days") or 0) >= 7]
        if dirty:
            out.append({"type": "git.idle_dirty_worktrees", "subject": "estate:git", "severity": "warning",
                        **titled("{n} idle worktrees hold uncommitted work", n=len(dirty)),
                        "detail": ("Nobody works in them and their edits exist nowhere else: "
                                   + ", ".join(w["path"].rsplit("/", 1)[-1] for _, w in dirty[:LISTED]) + "."),
                        "action": "commit what matters, or `full cleanup --apply --include manual` archives and removes them"})
        unique = [(c["repository"], b) for c in hygiene.get("checkouts") or [] for b in c.get("branches") or []
                  if b.get("class") == "unique" and (b.get("idle_days") or 0) >= 14]
        if unique:
            out.append({"type": "git.idle_unique_branches", "subject": "estate:git", "severity": "info",
                        **titled("{n} idle branches hold the only copy of their commits", n=len(unique)),
                        "detail": ", ".join(f"{r.split('/', 1)[-1]}:{b['name']}" for r, b in unique[:LISTED]) + ".",
                        "action": "push or merge what matters; `full cleanup --apply --include manual` bundles the rest before deleting"})

    since = now - timedelta(hours=24)
    removed, freed, measured = 0, 0, 0
    try:
        for line in (logs / "disk-guard.jsonl").read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("kind") != "summary":
                continue
            at = datetime.strptime(row.get("at", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if at >= since:
                removed += int(row.get("removed") or 0)
                measured += max(0, row.get("freed_mb") or 0) * 1024
    except (OSError, ValueError):
        pass
    try:
        for line in (logs / "cleanup.jsonl").read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            at = datetime.strptime(row.get("at", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if at >= since and row.get("result") in ("removed", "pruned") and row.get("mode") != "guard":
                removed += 1
                # What the disk said (`freed_mb`, the guard's statvfs delta) outranks the
                # estimate: `du` counts an APFS clone in full and frees nothing removing it.
                if "freed_mb" in row:
                    measured += max(0, row["freed_mb"]) * 1024
                else:
                    freed += row.get("kb") or 0
    except (OSError, ValueError):
        pass
    freed += measured
    if removed:
        out.append({"type": "cleanup.done", "subject": "machine", "severity": "info",
                    **(titled("cleanup removed {n} items in 24 hours and freed {gb} GB", n=removed, gb=f"{freed / 1048576:.1f}")
                       if freed else titled("cleanup removed {n} items in 24 hours", n=removed)),
                    "detail": "Only what loses nothing: merged or pushed branches, clean idle worktrees, "
                              "build output of idle checkouts, old temporary files, the cache tools' own cleanup. "
                              "The git tier's undo record is store/logs/cleanup.jsonl; the guard's actions "
                              "and run summaries are store/logs/disk-guard.jsonl.",
                    "action": "nothing to do; the journal names every item and how to bring it back"})
    return out


def _guard_detail(guard: dict | None) -> str:
    """What the guard did last, who holds the memory that swap writes here, and the
    largest files — the three things a person needs when the disk is low."""
    if not guard:
        return ""
    parts = [f" The disk guard last ran at pressure {guard.get('level')}: "
             f"{(guard.get('results') or {}).get('removed', 0)} item(s) removed, "
             f"free {guard.get('free_gb_before')} → {guard.get('free_gb_after')} GB."]
    mem = guard.get("memory") or {}
    if (mem.get("swap_gb") or 0) >= 2:
        holders = ", ".join(f"{h['origin']} {(h.get('rss_mb') or 0) / 1024:.1f} GB" for h in (mem.get("holders") or [])[:3])
        parts.append(f" Swap holds {mem['swap_gb']} GB of this disk; memory is held by {holders}.")
        sims = mem.get("simulators") or []
        if sims:
            parts.append(" Running simulators: " + "; ".join(f"`{x['command']}`" for x in sims[:LISTED]) + ".")
    files = ((guard.get("large_files") or {}).get("files") or [])[:3]
    if files:
        parts.append(" Largest files: " + ", ".join(f"{f['path']} {f['gb']} GB" for f in files) + ".")
    return "".join(parts)
