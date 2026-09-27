"""The machine survey as one answer, for `observatory_machine` and the Machine page.

Reads what the tick's `machine`, `git-hygiene` and `cleanup` steps wrote; runs
nothing heavy itself. `explain` is the one live call: why a single process runs.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

import paths


def _load(name: str) -> tuple[dict | None, dict | None]:
    try:
        return json.loads((paths.SCRATCH / name).read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, {"source": name, "reason": "not surveyed yet — enable features.machine_watch, "
                                                "or run `project-observatory full machine`"}
    except (OSError, ValueError) as exc:
        return None, {"source": name, "reason": f"unreadable: {type(exc).__name__}"}


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


def summary(explain_pid: int | None = None) -> dict:
    degraded = []
    machine, d1 = _load("machine.json")
    hygiene, d2 = _load("git-hygiene.json")
    plan, d3 = _load("cleanup-plan.json")
    degraded += [d for d in (d1, d2, d3) if d]
    out: dict = {"degraded": degraded}
    if machine:
        out["measuredAt"] = machine.get("measured_at")
        out["memory"] = machine.get("memory")
        out["processes"] = {k: (machine.get("processes") or {}).get(k) for k in ("count", "groups", "projects", "top")}
        out["disk"] = machine.get("disk")
        out["witr"] = machine.get("witr", False)
        degraded += (machine.get("processes") or {}).get("degraded") or []
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
                          "counts": plan.get("counts"), "actions": plan.get("actions"), "journal": journal()}
    if explain_pid is not None:
        sys.path.insert(0, str(paths.ROOT / "collectors"))
        import scan_machine
        out["explain"] = scan_machine.explain(int(explain_pid))
    out["degraded"] = degraded
    return out
