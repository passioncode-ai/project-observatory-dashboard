"""The machine survey as one answer, for `observatory_machine` and the Machine page.

Reads what the tick's `machine`, `git-hygiene` and `cleanup` steps wrote; runs
nothing heavy itself. `explain` is the one live call: why a single process runs.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

import paths


def _load(name: str, command: str) -> tuple[dict | None, dict | None]:
    try:
        return json.loads((paths.SCRATCH / name).read_text(encoding="utf-8")), None
    except FileNotFoundError:
        # Each survey names the command that runs it: the git-hygiene and
        # cleanup files are refreshed by `full cleanup`, not `full machine`
        # (audit A42 — one hint used to name `full machine` for all three).
        return None, {"source": name, "code": "not-surveyed",
                      "reason": f"not surveyed yet — enable features.machine_watch, "
                                f"or run `project-observatory full {command}`"}
    except (OSError, ValueError) as exc:
        return None, {"source": name, "code": "unreadable", "error": type(exc).__name__,
                      "reason": f"unreadable: {type(exc).__name__}"}


def journal(days: int = 7, limit: int = 200) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = []
    try:
        lines = (paths.STATE / "logs" / "cleanup.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in reversed(lines):
        try:
            row = json.loads(line)
            if datetime.strptime(row["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) < since:
                break
            rows.append(row)
        except (ValueError, KeyError):
            continue
        if len(rows) >= limit:
            break
    return rows


def guard_summaries(days: int = 7) -> list[dict]:
    """The disk guard's one-row-per-run summaries (store/logs/disk-guard.jsonl), newest first.

    The guard writes thousands of action rows a run; its summaries are what a view counts.
    Reading the cleanup journal's newest 200 rows instead undercounted the week and pushed
    the git tier's undo rows out of sight within one pass (review 2026-10-12)."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        lines = (paths.STATE / "logs" / "disk-guard.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("kind") != "summary":
            continue
        try:
            if datetime.strptime(row["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) < since:
                break
        except (ValueError, KeyError):
            continue
        out.append(row)
    return out


def summary(explain_pid: int | None = None) -> dict:
    degraded = []
    machine, d1 = _load("machine.json", "machine")
    hygiene, d2 = _load("git-hygiene.json", "cleanup")
    plan, d3 = _load("cleanup-plan.json", "cleanup")
    guard, d4 = _load("disk-guard.json", "disk-guard")
    degraded += [d for d in (d1, d2, d3, d4) if d]
    out: dict = {"degraded": degraded}
    if machine:
        out["measuredAt"] = machine.get("measured_at")
        out["memory"] = machine.get("memory")
        out["processes"] = {k: (machine.get("processes") or {}).get(k) for k in ("count", "groups", "projects", "top")}
        out["disk"] = machine.get("disk")
        out["witr"] = machine.get("witr", False)
        degraded += (machine.get("processes") or {}).get("degraded") or []
        degraded += (machine.get("memory") or {}).get("degraded") or []
        degraded += (machine.get("disk") or {}).get("degraded") or []
    if hygiene:
        rows = hygiene.get("checkouts") or []
        out["git"] = {
            "measuredAt": hygiene.get("measured_at"), "totals": hygiene.get("totals"),
            "worktrees": [{"repository": c["repository"], **w} for c in rows for w in c.get("worktrees") or []],
            "branches": {cls: sum(1 for c in rows for b in c.get("branches") or [] if b["class"] == cls)
                         for cls in ("merged", "patch-merged", "pushed", "unique")},
            "uniqueBranches": [{"repository": c["repository"], **b} for c in rows
                               for b in c.get("branches") or [] if b["class"] == "unique"],
        }
        degraded += hygiene.get("degraded") or []
    if plan:
        out["cleanup"] = {"plannedAt": plan.get("planned_at"), "autoEnabled": plan.get("auto_enabled"),
                          "counts": plan.get("counts"), "actions": plan.get("actions"), "journal": journal(),
                          "level": plan.get("level", "normal")}
    runs = guard_summaries()
    if runs:
        out.setdefault("cleanup", {"journal": journal()})
        out["cleanup"]["guardRuns"] = runs[:50]
        out["cleanup"]["guardTotals"] = {"runs": len(runs), "removed": sum(r.get("removed") or 0 for r in runs),
                                         "freedMb": sum(max(0, r.get("freed_mb") or 0) for r in runs)}
    if guard:
        out["guard"] = {"at": guard.get("at"), "level": guard.get("level"), "acting": guard.get("acting"),
                        "autoEnabled": guard.get("auto_enabled"), "freeGbBefore": guard.get("free_gb_before"),
                        "freeGbAfter": guard.get("free_gb_after"), "freedGb": guard.get("freed_gb"),
                        "planned": guard.get("planned"), "plannedGb": guard.get("planned_gb"),
                        "results": guard.get("results"), "seconds": guard.get("seconds"),
                        "actions": guard.get("actions"), "largeFiles": guard.get("large_files"),
                        "memory": guard.get("memory")}
        degraded += guard.get("degraded") or []
    if explain_pid is not None:
        sys.path.insert(0, str(paths.ROOT / "collectors"))
        import scan_machine
        out["explain"] = scan_machine.explain(int(explain_pid))
    out["degraded"] = degraded
    return out
