#!/usr/bin/env python3
"""What the machine survey owes the operator: space, memory, leftovers.

Reads what collectors/scan_machine.py, collectors/scan_git_hygiene.py and
tools/cleanup.py wrote, and turns the parts that need a person into findings.
Each finding names the biggest contributors and the command that recovers the
space — the board does not run it: this page reads, the operator acts.

Memory findings retain the selected source measurement time.
"""
from __future__ import annotations
import pathlib as _pathlib
import sys as _sys
_DASHBOARD = str(_pathlib.Path(__file__).resolve().parents[1] / "dashboard")
if _DASHBOARD not in _sys.path:
    _sys.path.append(_DASHBOARD)  # `finding_types.titled`: a title is a message id
from finding_types import titled  # noqa: E402

import json
from collections import deque
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


def _guard_receipt(path: pathlib.Path) -> dict | None:
    """Malformed guard evidence degrades coverage without hiding peer findings."""
    unknown = {"source": "receipt", "reason": "guard receipt has unreadable or invalid coverage data; measurement coverage is unknown"}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"degraded": [unknown]}
    if not isinstance(doc, dict):
        return {"degraded": [unknown]}
    notes = doc.get("degraded", [])
    if not isinstance(notes, list):
        return {**doc, "degraded": [unknown]}
    valid = [note for note in notes if isinstance(note, dict)
             and isinstance(note.get("source"), str) and note["source"]
             and isinstance(note.get("reason"), str) and note["reason"]]
    return {**doc, "degraded": valid + ([unknown] if len(valid) != len(notes) else [])}


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
    guard = _guard_receipt(scratch / "disk-guard.json")
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

    import memory_snapshot
    selected = memory_snapshot.read_sources(machine, scratch=scratch, at=now)
    machine = {**(machine if isinstance(machine, dict) else {}), **selected}
    if machine:
        age = _age_hours(machine.get("measured_at"), now)
        if age is not None and age > 2:
            out.append({"type": "machine.stale", "subject": "machine", "severity": "warning",
                        **titled("the machine survey is {hours} hours old", hours=f"{age:.0f}"),
                        "detail": "The full machine survey is old; RAM and processes may have separate newer measurements.",
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
        swap = mem.get("swap_used_mb")
        if swap is not None and swap > 4096:
            heavy = ", ".join(f"{g['origin']} {g['rss_mb'] / 1024:.1f} GB" for g in groups[:LISTED])
            out.append({"type": "machine.memory_pressure", "subject": "machine", "severity": "warning",
                        **titled("{gb} GB of memory is swapped to disk", gb=f"{swap / 1024:.1f}"),
                        "detail": (f"Largest by origin: {heavy}. Memory measured: {mem.get('measured_at') or 'unknown'}; "
                                   f"processes measured: {machine['processes'].get('measured_at') or 'unknown'}. "
                                   "RSS sums can count shared pages more than once."),
                        "measured_at": mem.get("measured_at"), "stale": mem.get("stale"),
                        "action": "review current memory use and process ownership on the Machine page before stopping anything"})
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
    summaries = deque(maxlen=3)
    guard_notes = list((guard or {}).get("degraded") or [])
    journal_bad = False
    try:
        # Stream the journal: only three summary rows need to stay in memory.
        with (logs / "disk-guard.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("object required")
                    if row.get("kind") != "summary":
                        continue
                    at = datetime.strptime(row.get("at", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    summaries.append(row)
                    if at >= since:
                        removed += int(row.get("removed") or 0)
                        measured += max(0, row.get("freed_mb") or 0) * 1024
                except (ValueError, TypeError, OverflowError):
                    journal_bad = True
                    summaries.append(None)  # A missing row cannot prove a consecutive streak.
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError):
        journal_bad = True
    if journal_bad:
        guard_notes.append({"source": "journal", "reason": "guard journal coverage is incomplete; some run outcomes are unknown"})

    def stopped(row):
        git = row.get("git") if isinstance(row, dict) else None
        if not isinstance(git, dict):
            return 0
        count = git.get("stopped")
        return (count if isinstance(count, int) and not isinstance(count, bool) and count > 0
                else 1 if git.get("result") == "stopped" else 0)

    if len(summaries) == 3 and all(stopped(row) for row in summaries):
        last = summaries[-1]
        out.append({"type": "machine.guard_stopping", "subject": "guard:git", "severity": "warning",
                    **titled("Git cleanup stopped in three consecutive disk guard runs"),
                    "detail": f"Last run stopped {stopped(last)} Git cleanup action(s) at pressure {last.get('level', 'unknown')}. Every one of the last three summary rows reports a stop.",
                    "action": "inspect store/logs/disk-guard.jsonl and cleanup.jsonl for the limiting phase; review the time budget and preserved work before a new plan",
                    "evidence": ["store:logs/disk-guard.jsonl"]})
    notes_by_source = {}
    for note in guard_notes:
        if isinstance(note, dict):
            notes_by_source.setdefault(str(note.get("source") or "unknown"), []).append(str(note.get("reason") or "measurement unavailable"))
    for source, reasons in notes_by_source.items():
        fda = source == "trash" and any("Full Disk Access" in reason for reason in reasons)
        out.append({"type": "machine.guard_degraded", "subject": "guard:" + source, "severity": "info",
                    **titled("the disk guard could not measure {source}", source=source),
                    "detail": "; ".join(reasons[:8]) + (f"; {len(reasons) - 8} further reasons in the receipt" if len(reasons) > 8 else ""),
                    "action": ("a person can grant the maintenance job's interpreter Full Disk Access in System Settings → Privacy & Security → Full Disk Access, then rerun the guard plan; declining keeps this source unmeasured"
                               if fda else "inspect the named source and its reason in store/raw/disk-guard.json or the guard journal; resolve that access or time-budget limit and rerun `project-observatory full disk-guard`"),
                    "evidence": ["store:raw/disk-guard.json", "store:logs/disk-guard.jsonl"]})
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


def backup_findings(state_file: pathlib.Path) -> list[dict]:
    """What the newest machine bundle left behind, one finding per item (machine_backup.py).

    WHY. The bundle exists so the whole machine can move to another one. On 2026-10-20 it left
    19 items behind, ten of them repositories whose unpushed commits were over the bundle cap,
    and the only trace was a count in a receipt and a line in `doctor`. Each item is now a row
    on the board with what is lost and the fix the backup recorded for it (`remedy`): push, or
    the exact `limits` value. The only copies of work (commits, uncommitted changes) are
    warnings; the rest is information. Silent when no bundle was taken or nothing was left."""
    doc = _load(state_file)
    success = doc.get("success") if isinstance(doc, dict) else None
    if not isinstance(success, dict):
        return []
    items = [r for r in success.get("not_carried") or [] if isinstance(r, dict) and r.get("source")]
    try:
        count = int(success.get("not_carried_count") or 0)
    except (TypeError, ValueError):
        count = len(items)
    import machine_backup
    status = "`project-observatory full machine-backup status` lists every item with its fix"
    # ONE ROW PER SOURCE AND PART. A checkout can leave its commits, its uncommitted changes and
    # its untracked files behind at once, and a home folder can break two caps: keyed on the
    # source alone they shared one id, and the board's duplicate-id guard then published
    # nothing at all (review of #325; the operator's receipt held such a pair).
    parts = {0: "commits", 1: "changes", 2: "untracked", 3: "checkout"}
    groups: dict[tuple[str, str], list[dict]] = {}
    for record in items:
        part = parts.get(machine_backup.loss_rank(record), "files")
        groups.setdefault((str(record["source"]), part), []).append(record)
    out = []
    for (source, part), records in groups.items():
        rank = min(machine_backup.loss_rank(r) for r in records)
        reasons = []
        for record in records:
            size = record.get("size")
            reason = str(record.get("reason") or "not carried")
            reasons.append(reason + (f" ({size / 1048576:.1f} MB)" if isinstance(size, int) and size else ""))
        detail = "; ".join(reasons)
        detail += (". It is the only copy of this work: were this machine lost, it would be lost with it"
                   if rank <= 1 else ". A new machine restored from the bundle would not have it")
        detail += f" (bundle {success['file']})." if success.get("file") else "."
        fixes = list(dict.fromkeys(str(r.get("fix") or "").strip() for r in records if str(r.get("fix") or "").strip()))
        out.append({"type": "machine.backup_not_carried", "subject": f"machine-backup:{source}:{part}",
                    "severity": "warning" if rank <= 1 else "info",
                    **titled("the machine backup did not carry {source}", source=source),
                    "detail": detail,
                    "action": "; ".join([*fixes, status]),
                    "evidence": ["store:machine-backup.json"]})
    if count > len(items):
        out.append({"type": "machine.backup_not_carried", "subject": "machine-backup:more", "severity": "info",
                    **titled("items the machine backup did not carry beyond those listed: {count}", count=count - len(items)),
                    "detail": "The receipt lists the first items, the only copies of work first; the bundle's "
                              "MANIFEST.json names every one.",
                    "action": status, "evidence": ["store:machine-backup.json"]})
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
    if mem and mem.get("swap_gb") is None:
        parts.append(" Swap measurement is unavailable.")
    if (mem.get("swap_gb") or 0) >= 2:
        holders = ", ".join(f"{h['origin']} {(h.get('rss_mb') or 0) / 1024:.1f} GB" for h in (mem.get("holders") or [])[:3])
        parts.append(f" Swap holds {mem['swap_gb']} GB of this disk; memory is held by {holders}.")
        sims = mem.get("simulators") or []
        if sims:
            parts.append(" Running simulators: " + "; ".join(f"`{x['command']}`" for x in sims[:LISTED]) + ".")
    coverage = guard.get("large_files") or {}
    if coverage and coverage.get("complete") is not True:
        parts.append(" The largest-file survey is incomplete.")
    files = (coverage.get("files") or [])[:3]
    if files:
        parts.append(" Largest files: " + ", ".join(f"{f['path']} {f['gb']} GB" for f in files) + ".")
    return "".join(parts)
