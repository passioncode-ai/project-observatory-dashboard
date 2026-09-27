#!/usr/bin/env python3
"""What the machine survey owes the operator: space, memory, leftovers.

Reads what collectors/scan_machine.py, collectors/scan_git_hygiene.py and
tools/cleanup.py wrote, and turns the parts that need a person into findings.
Each finding names the biggest contributors and the command that recovers the
space — the board does not run it: this page reads, the operator acts.

NO TIMESTAMP INSIDE A FINDING beyond the documents' own.
"""
from __future__ import annotations

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


def findings(scratch: pathlib.Path, logs: pathlib.Path, config: dict, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    out: list[dict] = []
    machine = _load(scratch / "machine.json")
    hygiene = _load(scratch / "git-hygiene.json")

    if machine:
        age = _age_hours(machine.get("measured_at"), now)
        if age is not None and age > 2:
            out.append({"type": "machine.stale", "subject": "machine", "severity": "warning",
                        "title": f"the machine survey is {age:.0f} hours old",
                        "detail": "Processes and memory change by the minute; this page is reading an old picture.",
                        "action": "check the tick log for the `machine` step"})

        vol = (machine.get("disk") or {}).get("volume") or {}
        pct = vol.get("free_percent")
        warn = config.get("free_space_warning_percent", 15)
        crit = config.get("free_space_critical_percent", 5)
        if pct is not None and pct < warn:
            locs = (machine.get("disk") or {}).get("locations") or []
            named = "; ".join(f"{l['label']} {l['gb']} GB" + (f" (`{l['command']}`)" if l.get("command") else "")
                              for l in locs[:LISTED])
            out.append({"type": "machine.disk_low", "subject": "machine",
                        "severity": "critical" if pct < crit else "warning",
                        "title": f"{vol.get('free_gb')} GB free on the disk ({pct}%)",
                        "detail": (f"Below the {crit if pct < crit else warn}% line in config/machine.json. "
                                   f"Largest measured places outside the projects: {named or 'none measured yet'}."),
                        "action": "review the Machine page's disk table; each row names how its space comes back"})

        mem = machine.get("memory") or {}
        groups = (machine.get("processes") or {}).get("groups") or []
        swap = mem.get("swap_used_mb") or 0
        if swap > 4096:
            heavy = ", ".join(f"{g['origin']} {g['rss_mb'] / 1024:.1f} GB" for g in groups[:LISTED])
            out.append({"type": "machine.memory_pressure", "subject": "machine", "severity": "warning",
                        "title": f"{swap / 1024:.1f} GB of memory is swapped to disk",
                        "detail": f"Largest by origin: {heavy}.",
                        "action": "close what the Machine page shows as largest and idle"})
        limit = config.get("memory_group_warning_mb", 4096)
        for g in groups:
            if g["rss_mb"] >= limit and not g["origin"].startswith(("system", "agent:")):
                out.append({"type": "machine.heavy_origin", "subject": f"machine:{g['origin']}", "severity": "info",
                            "title": f"{g['origin']} holds {g['rss_mb'] / 1024:.1f} GB in {g['processes']} process(es)",
                            "detail": "Above memory_group_warning_mb in config/machine.json.",
                            "action": "quit it if it is not in use"})

        detached = [g for g in groups if g["origin"].startswith("detached:")
                    and g["origin"].split(":", 1)[1].startswith(SERVERS)]
        if detached:
            total = sum(g["rss_mb"] for g in detached)
            out.append({"type": "machine.detached_servers", "subject": "machine", "severity": "info",
                        "title": f"{sum(g['processes'] for g in detached)} detached server process(es) hold {total:.0f} MB",
                        "detail": ("Interpreters whose parent is launchd and which no job, app or agent session explains — "
                                   "usually an MCP or dev server that outlived the session that started it: "
                                   + ", ".join(g["origin"].split(":", 1)[1] for g in detached[:LISTED]) + "."),
                        "action": "`project-observatory full machine --explain PID` says what started one; stop it if nothing uses it"})

    if hygiene:
        dirty = [(c["repository"], w) for c in hygiene.get("checkouts") or [] for w in c.get("worktrees") or []
                 if w.get("state") == "dirty" and not w.get("busy") and (w.get("idle_days") or 0) >= 7]
        if dirty:
            out.append({"type": "git.idle_dirty_worktrees", "subject": "estate:git", "severity": "warning",
                        "title": f"{len(dirty)} idle worktree(s) hold uncommitted work",
                        "detail": ("Nobody works in them and their edits exist nowhere else: "
                                   + ", ".join(w["path"].rsplit("/", 1)[-1] for _, w in dirty[:LISTED]) + "."),
                        "action": "commit what matters, or `full cleanup --apply --include manual` archives and removes them"})
        unique = [(c["repository"], b) for c in hygiene.get("checkouts") or [] for b in c.get("branches") or []
                  if b.get("class") == "unique" and (b.get("idle_days") or 0) >= 14]
        if unique:
            out.append({"type": "git.idle_unique_branches", "subject": "estate:git", "severity": "info",
                        "title": f"{len(unique)} idle branch(es) hold the only copy of their commits",
                        "detail": ", ".join(f"{r.split('/', 1)[-1]}:{b['name']}" for r, b in unique[:LISTED]) + ".",
                        "action": "push or merge what matters; `full cleanup --apply --include manual` bundles the rest before deleting"})

    since = now - timedelta(hours=24)
    removed, freed = 0, 0
    try:
        for line in (logs / "cleanup.jsonl").read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            at = datetime.strptime(row.get("at", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if at >= since and row.get("result") in ("removed", "pruned"):
                removed += 1
                freed += row.get("kb") or 0
    except (OSError, ValueError):
        pass
    if removed:
        out.append({"type": "cleanup.done", "subject": "machine", "severity": "info",
                    "title": f"cleanup removed {removed} item(s) in 24 hours"
                             + (f", {freed / 1048576:.1f} GB of build output" if freed else ""),
                    "detail": "Only what loses nothing: merged or pushed branches, clean idle worktrees, "
                              "build output of idle projects. Each is a line in store/logs/cleanup.jsonl.",
                    "action": "nothing to do; the journal names every item and how to bring it back"})
    return out
