#!/usr/bin/env python3
"""What the Agents page shows: running agents, their workflows, and where work moved.

One reader, two callers: the dashboard build renders it into `agents.html` on every
tick, and the local server renders it again on request (`/agents`) so a page opened
through the server stays current between ticks. Both read the store READ-ONLY and
change nothing — the page shows and hands over commands; it never acts.

The shape, top to bottom, is the order a person asks in:

* `counters` — sessions working now, open workflows, handoffs waiting, stalled
  workflows, steps kept after a lost lease;
* `needsYou` — what someone has to look at, each with its reason and a command;
* `workflows` — open ones, and those closed in the last week, each with its
  executor lanes (one segment per lease, a dot per checkpoint, the handoff and its
  reason between segments), constraints, next actions and credentials by name;
* `sessions` — agent sessions with a recorded turn in the last day.

Nothing here carries a lease token or a credential value: the token is read by no
query, and credentials are reported by name and state only.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import paths                                                                       # noqa: E402
from store import workflow as W                                                    # noqa: E402

#: A session counts as working now when its last recorded turn is this recent.
ACTIVE_SECONDS = 3600
#: Closed workflows stay on the page this long, so a finished handoff can be read.
CLOSED_DAYS = 7
#: Bounds on what one page carries.
MAX_WORKFLOWS = 100
MAX_CHECKPOINTS = 40
MAX_SESSIONS = 60


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _open(db: pathlib.Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _lanes(conn: sqlite3.Connection, wid: str) -> list[dict]:
    """One segment per lease, in order, each with the checkpoints written while it
    held the workflow, and the handoff that ended it.

    A checkpoint belongs to the lease that wrote it (its provenance names the
    lease); the revision number orders checkpoints written in the same second."""
    leases = conn.execute(
        "SELECT * FROM workflow_leases WHERE workflow_id = ? ORDER BY granted_at, rowid",
        (wid,)).fetchall()
    ckpts = []
    for c in conn.execute(
            "SELECT revision, step_id, created_at, owner, provenance_json FROM ledger"
            " WHERE memory_id = ? ORDER BY revision", (f"ckpt:{wid}",)):
        prov = next((p for p in json.loads(c["provenance_json"] or "[]")
                     if p.get("source") == "checkpoint"), {})
        ckpts.append({"revision": c["revision"], "stepId": c["step_id"], "at": c["created_at"],
                      "by": c["owner"], "status": prov.get("status"), "lease": prov.get("lease")})
    reasons = {}
    for r in conn.execute("SELECT memory_id, body_json FROM ledger WHERE kind = 'handoff'"
                          " AND workflow_id = ?", (wid,)):
        try:
            body = json.loads(r["body_json"] or "{}")
        except ValueError:
            body = {}
        reasons[r["memory_id"]] = {"reason": body.get("reason"),
                                   "authority": body.get("authority")}
    segments = []
    held = [x for x in leases if x["accepted_at"]]
    for i, lease in enumerate(held):
        start = lease["accepted_at"]
        end = lease["ended_at"] if lease["state"] == "ended" else None
        nxt = held[i + 1]["accepted_at"] if i + 1 < len(held) else None
        stop = end or nxt
        # BY THE LEASE THAT WROTE IT, recorded on every checkpoint; by time
        # only for a checkpoint written before that was recorded.
        dots = [{k: c[k] for k in ("revision", "stepId", "status", "at", "by")}
                for c in ckpts
                if c["lease"] == lease["lease_ref"]
                or (c["lease"] is None and c["at"] >= start and not (stop and c["at"] > stop))]
        came_by = reasons.get(lease["handoff_id"]) if lease["handoff_id"] else None
        segments.append({"executor": json.loads(lease["executor_json"] or "{}"),
                         "holder": lease["holder"], "from": start, "until": end,
                         "state": lease["state"], "endedReason": lease["ended_reason"],
                         "arrivedBy": came_by, "checkpoints": dots[-MAX_CHECKPOINTS:]})
    offers = [x for x in leases if x["handoff_id"] and not x["accepted_at"]]
    pending = []
    for o in offers[-3:]:
        why = reasons.get(o["handoff_id"]) or {}
        state = "offered" if o["state"] == "offered" else (o["ended_reason"] or "ended")
        pending.append({"handoffId": o["handoff_id"], "to": json.loads(o["executor_json"] or "{}"),
                        "at": o["granted_at"], "expiresAt": o["expires_at"], "state": state,
                        "reason": why.get("reason")})
    return [{"segments": segments, "offers": pending}]


def summary(db: pathlib.Path | None = None,
            credential_reader: Callable[[str], dict] | None = None,
            now: datetime | None = None) -> dict:
    """The page's data. Degrades by section, never by raising."""
    now = now or _now()
    db = pathlib.Path(db or paths.DB)
    out: dict = {"measuredAt": _iso(now), "counters": {}, "needsYou": [], "workflows": [],
                 "sessions": [], "degraded": []}
    if not db.is_file():
        out["degraded"].append({"source": "store", "code": "no-store",
                                "reason": "the store does not exist yet"})
        return out
    try:
        conn = _open(db)
    except sqlite3.Error as exc:
        out["degraded"].append({"source": "store", "code": "unreadable",
                                "reason": f"{type(exc).__name__}"})
        return out
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"workflows", "workflow_leases"} <= tables:
            out["degraded"].append({"source": "store", "code": "before-agent-memory",
                                    "reason": "the store predates agent memory; the next "
                                              "engine start migrates it"})
            return out
        listed = W.workflow_list(conn, status="open", limit=MAX_WORKFLOWS)
        closed = [w for w in W.workflow_list(conn, status="closed", limit=50)["workflows"]
                  if w["closedAt"] and w["closedAt"] >= _iso(now - timedelta(days=CLOSED_DAYS))]
        workflows = []
        for w in [*listed["workflows"], *closed]:
            detail = W.checkpoint_latest(conn, w["workflowId"], credential_reader=credential_reader)
            body = ((detail.get("checkpoint") or {}).get("body")) or {}
            workflows.append({
                **w,
                "constraints": body.get("constraints", []),
                "next": [o for o in body.get("open", [])][:5],
                "artifacts": [a for a in body.get("artifacts", []) if a.get("kind") == "git"][:5],
                "credentials": detail.get("credentials", []),
                "credentialsMissing": detail.get("credentialsMissing", False),
                "lapsedHandoff": detail.get("lapsedHandoff"),
                **_lanes(conn, w["workflowId"])[0]})
            out["degraded"] += [dict(d, source=f"{w['workflowId']}:{d['source']}")
                                for d in detail.get("degraded", [])]
        out["workflows"] = workflows
        kept_total = conn.execute(
            "SELECT count(*) FROM ledger l JOIN (SELECT memory_id, MAX(revision) r FROM ledger"
            " GROUP BY memory_id) m ON m.memory_id = l.memory_id AND m.r = l.revision"
            " LEFT JOIN tombstones t ON t.memory_id = l.memory_id WHERE t.memory_id IS NULL"
            " AND l.kind = 'step_result' AND l.state = 'proposed'").fetchone()[0]
        since = _iso(now - timedelta(days=1))
        rows = conn.execute(
            "SELECT session_id, project_id, max(created_at) AS last, count(*) AS turns,"
            "       max(workflow_id) AS workflow_id, max(agent_id) AS agent"
            " FROM ledger WHERE kind = 'session' AND session_id IS NOT NULL AND created_at >= ?"
            " GROUP BY session_id ORDER BY last DESC LIMIT ?", (since, MAX_SESSIONS)).fetchall()
        active_since = _iso(now - timedelta(seconds=ACTIVE_SECONDS))
        out["sessions"] = [{"sessionId": r["session_id"], "projectId": r["project_id"],
                            "lastTurnAt": r["last"], "turns": r["turns"],
                            "workflowId": r["workflow_id"], "active": r["last"] >= active_since}
                           for r in rows]
    except sqlite3.Error as exc:
        out["degraded"].append({"source": "store", "code": "unreadable",
                                "reason": f"{type(exc).__name__}"})
        return out
    finally:
        conn.close()

    open_ = [w for w in workflows if w["status"] == "open"]
    out["counters"] = {
        "sessionsActive": sum(1 for s in out["sessions"] if s["active"]),
        "workflowsOpen": len(open_),
        "handoffsWaiting": sum(1 for w in open_ if w["pendingHandoff"]),
        "stalled": sum(1 for w in open_ if w["stalled"]),
        "keptSteps": kept_total,
    }
    needs = []
    for w in open_:
        wid = w["workflowId"]
        if w["stalled"]:
            needs.append({"kind": "stalled", "workflowId": wid, "projectId": w["projectId"],
                          "seconds": w["silentSeconds"],
                          "command": f"project-observatory full workflow show {wid}"})
        if w.get("lapsedHandoff"):
            needs.append({"kind": "handoff-lapsed", "workflowId": wid,
                          "projectId": w["projectId"],
                          "command": f"project-observatory full workflow handoff {wid} --to-provider PROVIDER --reason limit"})
        if w["keptSteps"]:
            needs.append({"kind": "kept-steps", "workflowId": wid, "projectId": w["projectId"],
                          "count": w["keptSteps"], "command": "project-observatory full review"})
        for c in w["credentials"]:
            if c["state"] in ("missing", "env-only"):
                needs.append({"kind": f"credential-{c['state']}", "workflowId": wid,
                              "projectId": w["projectId"], "name": c["name"], "env": c["env"],
                              "project": c["project"], "command": c.get("put", "")})
    out["needsYou"] = needs
    return out
