"""The machine survey as one answer, for `observatory_machine` and the Machine page.

Reads what the tick's `machine`, `git-hygiene` and `cleanup` steps wrote; runs
nothing heavy itself. `explain` is the one live call: why a single process runs.
"""
from __future__ import annotations

import json
import re
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


def carried(hygiene: dict) -> dict | None:
    """The checkouts a cut-short git-hygiene pass carried from an earlier one (`stale: true`), and
    the oldest of their measurements; None when every row is fresh (ENG-009). The one reader of
    `stale`: `tools/cleanup.py` plans and logs from it, this summary and the page show it."""
    stale = [c for c in hygiene.get("checkouts") or [] if c.get("stale")]
    if not stale:
        return None
    stamps = [c["measured_at"] for c in stale if c.get("measured_at")]
    return {"checkouts": len(stale), "measured_at": min(stamps) if stamps else None}


#: The page of an answer that has no skills to page.
EMPTY_PAGE = {"limit": None, "cursor": "0", "nextCursor": None, "total": 0,
              "order": "findings, then each space's plugins, then its skills"}
#: Past this age the skills section says its scan is stale.
SKILLS_STALE_HOURS = 24
#: What the skills section always answers, so a reader can tell an empty inventory from an unread one.
SKILLS_OPEN = [{"finding": "organization-plugin-in-personal-space", "waitsOn": "OBS-359",
                "reason": "which space is a person's own needs the space's context binding (OBS-359); until then an "
                          "organization's plugin enabled here is not reported"}]


def skills_section(limit: int | None = None, cursor: str | None = None) -> dict:
    """The `skills` section (OBS-477): every skill and plugin each agent of this space loads or has installed, with
    its version, scope and agent, and the findings the records support (a plugin behind the newest version its
    marketplace's local copy publishes). Read from the harness scan's `inventory` (collectors/scan_harness.py), which
    the tick refreshes when `integrations.harness` is on and `full scan-harness` refreshes on request; nothing is
    scanned here. A scan that is missing, unreadable or older than the inventory is named in `degraded` with the
    command that fills it, and the lists are then empty, never a claim that nothing is installed.

    `limit` pages every row (a machine can hold over a thousand skills and a plugin row per project install; the MCP
    tool answers 100 rows at a time): one window over the findings, then each space's plugins, then its skills;
    `cursor` is the offset `page.nextCursor` gave; `counts` are always whole. A cursor that is not one this function
    gave is answered with `degraded` code `invalid-cursor` and the first page."""
    doc, problem = _load("harness.json", "scan-harness")
    if problem:
        if problem["code"] == "not-surveyed":
            problem = {**problem, "reason": "not surveyed yet: turn on the harness scan "
                                            "(`project-observatory full configure integrations harness true` and "
                                            "`project-observatory full configure sources harness_root ~`), then run "
                                            "`project-observatory full scan-harness`"}
        return {"measuredAt": None, "spaces": [], "page": EMPTY_PAGE,
                "counts": {"skills": 0, "plugins": 0, "findings": 0},
                "findings": [], "open": SKILLS_OPEN, "degraded": [problem]}
    inventory = doc.get("inventory") if isinstance(doc, dict) else None
    if not isinstance(inventory, dict):
        return {"measuredAt": doc.get("generated_at") if isinstance(doc, dict) else None, "spaces": [],
                "page": EMPTY_PAGE,
                "counts": {"skills": 0, "plugins": 0, "findings": 0}, "findings": [], "open": SKILLS_OPEN,
                "degraded": [{"source": "harness.json", "code": "not-surveyed",
                              "reason": "the last harness scan predates the skills inventory; run "
                                        "`project-observatory full scan-harness`"}]}
    spaces = [{**s, "skills": [r for r in s.get("skills") or [] if isinstance(r, dict)]
                  if isinstance(s.get("skills"), list) else [],
               "plugins": [r for r in s.get("plugins") or [] if isinstance(r, dict)]
                  if isinstance(s.get("plugins"), list) else []}
              for s in (inventory.get("spaces") if isinstance(inventory.get("spaces"), list) else [])
              if isinstance(s, dict)]
    findings = [f for f in inventory.get("findings") or [] if isinstance(f, dict)]
    degraded = [d for d in inventory.get("degraded") or [] if isinstance(d, dict)]
    try:
        measured = datetime.strptime(str(doc.get("generated_at")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        measured = None
    # The tick refreshes the scan every half hour while the harness integration is on; a day without one means it
    # stopped (the integration turned off, the tick not running), and the lists describe that day, not today.
    if measured is None or datetime.now(timezone.utc) - measured > timedelta(hours=SKILLS_STALE_HOURS):
        degraded.append({"source": "harness.json", "code": "stale",
                         "reason": f"the harness scan is older than {SKILLS_STALE_HOURS} hours or carries no time; "
                                   f"run `project-observatory full scan-harness`"})
    counts = {"skills": sum(len(sp["skills"]) for sp in spaces),
              "plugins": sum(len(sp["plugins"]) for sp in spaces), "findings": len(findings)}
    # One window over every row, in one order: the findings, then each space's plugins, then its skills. A page of
    # `limit` rows is bounded whatever a machine holds (a project install is a plugin row per project).
    rows: list[tuple[str, int, dict]] = [("finding", -1, f) for f in findings]
    for index, space in enumerate(spaces):
        rows += [("plugin", index, r) for r in space["plugins"]] + [("skill", index, r) for r in space["skills"]]
    total = len(rows)
    start = 0
    if cursor is not None:
        if re.fullmatch(r"[0-9]{1,9}", str(cursor)) and int(cursor) <= total:
            start = int(cursor)
        elif re.fullmatch(r"[0-9]{1,9}", str(cursor)):
            # Past the end (the inventory shrank between two scans): an empty last page, never the first page
            # again, or a reader following `nextCursor` would read from the start without end.
            start = total
            degraded.append({"source": "cursor", "code": "invalid-cursor",
                             "reason": "the cursor is past the end of the current inventory; nothing follows it"})
        else:
            degraded.append({"source": "cursor", "code": "invalid-cursor",
                             "reason": "the cursor is not one this answer gave; the first page is shown"})
    end = total if limit is None else min(total, start + max(1, int(limit)))
    window = rows[start:end]
    findings = [r for kind, _, r in window if kind == "finding"]
    spaces = [{**space, "plugins": [r for kind, i, r in window if kind == "plugin" and i == index],
               "skills": [r for kind, i, r in window if kind == "skill" and i == index]}
              for index, space in enumerate(spaces)]
    page = {"limit": limit, "cursor": str(start), "nextCursor": str(end) if end < total else None, "total": total,
            "order": "findings, then each space's plugins, then its skills"}
    return {"measuredAt": doc.get("generated_at"), "spaces": spaces, "page": page, "counts": counts,
            "findings": findings, "open": inventory.get("open") or SKILLS_OPEN, "degraded": degraded}


def summary(explain_pid: int | None = None) -> dict:
    degraded = []
    machine, d1 = _load("machine.json", "machine")
    hygiene, d2 = _load("git-hygiene.json", "cleanup")
    plan, d3 = _load("cleanup-plan.json", "cleanup")
    guard, d4 = _load("disk-guard.json", "disk-guard")
    degraded += [d for d in (d1, d2, d3, d4) if d]
    import coverage_view
    out: dict = {"degraded": degraded, "coverage": coverage_view.summary()}
    if machine:
        out["measuredAt"] = machine.get("measured_at")
        out["memory"] = machine.get("memory")
        out["processes"] = {k: (machine.get("processes") or {}).get(k) for k in ("count", "groups", "projects", "top")}
        out["disk"] = machine.get("disk")
        out["witr"] = machine.get("witr", False)
        degraded += (machine.get("processes") or {}).get("degraded") or []
        degraded += (machine.get("memory") or {}).get("degraded") or []
        degraded += (machine.get("disk") or {}).get("degraded") or []
    import memory_snapshot
    fresh = memory_snapshot.read_sources(machine)
    for section in ("memory", "processes"):
        out[section] = fresh[section]
        degraded += fresh[section]["degraded"]
    if hygiene:
        rows = hygiene.get("checkouts") or []
        out["git"] = {
            "measuredAt": hygiene.get("measured_at"), "totals": hygiene.get("totals"),
            "worktrees": [{"repository": c["repository"], **w} for c in rows for w in c.get("worktrees") or []],
            "branches": {cls: sum(1 for c in rows for b in c.get("branches") or [] if b["class"] == cls)
                         for cls in ("merged", "patch-merged", "pushed", "unique")},
            "uniqueBranches": [{"repository": c["repository"], **b} for c in rows
                               for b in c.get("branches") or [] if b["class"] == "unique"],
            # A pass the deadline cut short (ENG-009): how many rows are carried, and from when.
            # Their auto actions wait for a fresh pass (tools/cleanup.py `carried`).
            "partial": bool(hygiene.get("partial")),
            "carried": carried(hygiene),
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
                        "actions": guard.get("actions"), "kept": guard.get("kept") or [],
                        "largeFiles": guard.get("large_files"),
                        "memory": guard.get("memory")}
        degraded += guard.get("degraded") or []
    if explain_pid is not None:
        sys.path.insert(0, str(paths.ROOT / "collectors"))
        import scan_machine
        out["explain"] = scan_machine.explain(int(explain_pid))
    # The coverage block carries its own unmeasured sources; the overview's `degraded` names
    # them too, once each, or an empty list would claim full coverage beside them.
    seen = {(d.get("source"), d.get("code")) for d in degraded if isinstance(d, dict)}
    for d in (out.get("coverage") or {}).get("degraded") or []:
        if (d.get("source"), d.get("code")) not in seen:
            seen.add((d.get("source"), d.get("code")))
            degraded.append(d)
    out["degraded"] = degraded
    return out
