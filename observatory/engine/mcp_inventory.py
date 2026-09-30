#!/usr/bin/env python3
"""`machine.mcp.inventory`: every MCP server this machine's agents declare, by name.

WHY THIS SHAPE. A host (Fabric's MCP servers tab) needs one inventory of the MCP
servers on a machine, kept apart from the agents, and it must never scan agent
configs itself as a second inventory. The observatory already scans them on
every tick (`collectors/scan_mcp.py` → `store/raw/mcp.json`); this module turns
that scan into the locked shape of the Fabric agent-registry contract, C6:

    {servers: [{name, declaredIn: [{agent, file}], transport, answers, checkedAt}],
     inventoryAt}

plus the observatory's own `degraded` list, a per-server `status` and, per
declaration, its `scope`, `status` and `disabled` flag. The additions are named in
`fabric/FABRIC-CONFORMANCE.md` as the observatory's reading of C6.

WHAT NEVER LEAVES. Only names, home-relative file locations, transports and
verdicts. The scan already holds no values (its reader drops URL queries and
user-info, and turns headers and environment blocks into booleans); this builder
additionally copies an ALLOW-LIST of fields, so a field the scan grows later
cannot reach a host by being passed through.

COVERAGE IS NAMED. An agent config that is absent or unreadable is a `degraded`
row, never a silent gap, because an inventory that lacks an agent reads as that
agent declaring nothing. An inventory older than the tick's staleness bound
(`tick_health.STALE_AFTER`) is still returned — the last known answer, with its
time — and called stale. A machine never scanned answers an empty list and says
so, rather than looking like a machine with no servers.

READ-ONLY. `effect: none`: nothing here writes. `machine.mcp.refresh` is the job
that re-takes the scan.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import configuration
import paths
import tick_health

#: The scan's agent spelling → the runner-catalogue kind (contract C2) a host
#: joins on. The scan keeps its own spelling because registry ids are built from it.
AGENT_KINDS = {
    "claude": "claude-code",
    "cursor": "cursor-agent",
    "opencode": "opencode",
    "codex": "codex",
    "gemini": "gemini-cli",
    "kiro": "kiro",
}
#: Where each agent keeps its declarations, for scans older than `sources`.
AGENT_FILES = {
    "claude": "~/.claude.json",
    "cursor": "~/.cursor/mcp.json",
    "opencode": "~/.config/opencode/opencode.json",
    "codex": "~/.codex/config.toml",
    "gemini": "~/.gemini/settings.json",
    "kiro": "~/.kiro/settings/mcp.json",
}
#: The declaration statuses, strongest verdict first: a server answers if any of
#: its declarations was heard answering.
STATUS_ORDER = ("connected", "needs-auth", "failed", "not-listed", "not-probed", "disabled")
ANSWERS = {"connected": True, "needs-auth": True, "failed": False}
#: The scan's old two-value transport, for scans taken before `wire` existed.
OLD_TRANSPORT = {"http": "streamable-http", "stdio": "stdio"}
SOURCE = "mcp inventory"


def scan_file() -> Path:
    return paths.SCRATCH / "mcp.json"


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _kind(agent: Any) -> str:
    return AGENT_KINDS.get(str(agent), str(agent) if agent else "unknown")


def _file(row: dict) -> str | None:
    """The config file a declaration lives in; None for plugin and connector servers."""
    shown = row.get("declared_in")
    if not isinstance(shown, str) or not shown.startswith("~"):
        return None
    return shown.split("#", 1)[0]


def _status(row: dict) -> str:
    if row.get("disabled") is True:
        return "disabled"
    live = row.get("liveness")
    return live if live in STATUS_ORDER else "not-probed"


def _transport(row: dict) -> str | None:
    wire = row.get("wire")
    if wire in ("stdio", "streamable-http", "sse"):
        return wire
    return OLD_TRANSPORT.get(str(row.get("transport")))


def _sources(scan: dict) -> list[dict]:
    """What became of each config file. Older scans are reconstructed from their rows."""
    listed = scan.get("sources")
    if isinstance(listed, list):
        return [s for s in listed if isinstance(s, dict) and s.get("agent")]
    broken = {r.get("agent") for r in scan.get("servers") or [] if isinstance(r, dict) and not r.get("name")}
    return [{"agent": a, "file": AGENT_FILES.get(str(a), "?"), "state": "unreadable",
             "reason": "unreadable (reported by an older scan)"} for a in sorted(broken, key=str)]


def _coverage_rows(sources: list[dict]) -> list[dict]:
    rows = []
    for s in sources:
        where = f"{_kind(s['agent'])}:{s.get('file') or AGENT_FILES.get(str(s['agent']), '?')}"
        if s.get("state") == "absent":
            rows.append({"source": where, "reason": "absent: no config at this path, so this agent declares "
                                                    "no MCP server here (or keeps them where this scan does "
                                                    "not look)"})
        elif s.get("state") == "unreadable":
            rows.append({"source": where, "reason": str(s.get("reason") or "unreadable")})
    return rows


def build(scan: dict, now: datetime | None = None) -> dict:
    """The C6 answer for one scan document."""
    now = now or datetime.now(timezone.utc)
    degraded: list[dict] = []
    at = _parse_time(scan.get("scanned_at"))
    if at is None:
        degraded.append({"source": SOURCE, "reason": "the scan carries no readable time, so it cannot be "
                                                     "called current"})
    elif now - at > tick_health.STALE_AFTER:
        hours = (now - at).total_seconds() / 3600
        degraded.append({"source": SOURCE, "reason": f"stale: last scanned {_iso(at)}, {hours:.1f} h ago — "
                                                     f"older than the {tick_health.STALE_AFTER} the tick "
                                                     f"allows; this is the last inventory, not the current one"})
    sources = _sources(scan)
    degraded += _coverage_rows(sources)
    # The scan's own degraded rows, except the ones that restate an unreadable
    # config (the scan names those as `<agent>:<file>`), already listed above.
    own = {f"{s['agent']}:{s.get('file')}" for s in sources}
    for d in scan.get("degraded") or []:
        if isinstance(d, dict) and d.get("source") and d.get("reason") and d["source"] not in own:
            degraded.append({"source": str(d["source"]), "reason": str(d["reason"])})

    groups: dict[tuple[str, str | None], list[dict]] = {}
    for row in scan.get("servers") or []:
        if isinstance(row, dict) and isinstance(row.get("name"), str) and row["name"]:
            groups.setdefault((row["name"], _transport(row)), []).append(row)
    inventory_at = _iso(at) if at else None
    servers = []
    for (name, transport), rows in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        declared = []
        for r in rows:
            declared.append({"agent": _kind(r.get("agent")), "file": _file(r),
                             "scope": str(r.get("scope") or "user"), "status": _status(r),
                             "disabled": r.get("disabled") is True})
        declared.sort(key=lambda d: (d["agent"], d["file"] or "", d["scope"]))
        status = min((d["status"] for d in declared), key=STATUS_ORDER.index)
        answers = ANSWERS.get(status)
        servers.append({"name": name, "declaredIn": declared, "transport": transport,
                        "answers": answers, "status": status,
                        "checkedAt": inventory_at if answers is not None else None})
    return {"servers": servers, "inventoryAt": inventory_at,
            "sources": [{"agent": _kind(s["agent"]),
                         "file": str(s.get("file") or AGENT_FILES.get(str(s["agent"]), "?")),
                         "state": s.get("state") if s.get("state") in ("read", "absent", "unreadable")
                         else "unreadable"} for s in sources],
            "degraded": degraded}


def inventory(now: datetime | None = None) -> dict:
    """The answer from the last scan on this machine, or an empty one that says why."""
    disabled = [] if configuration.enabled("mcp") else [{
        "source": "mcp", "reason": "integration disabled: set integrations.mcp and sources.mcp_config_root "
                                   "in config/settings.json to inventory this machine's MCP servers"}]
    file = scan_file()
    if not file.is_file():
        return {"servers": [], "inventoryAt": None, "sources": [],
                "degraded": disabled or [{"source": SOURCE, "reason": "never scanned: no MCP scan exists in "
                                                                      "this workspace yet; the next tick, or "
                                                                      "machine.mcp.refresh, takes one"}]}
    try:
        scan = json.loads(file.read_text(encoding="utf-8"))
        if not isinstance(scan, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as exc:
        return {"servers": [], "inventoryAt": None, "sources": [],
                "degraded": disabled + [{"source": SOURCE, "reason": f"unreadable: the last scan "
                                                                     f"({type(exc).__name__}) — nothing it "
                                                                     f"held can be shown"}]}
    answer = build(scan, now)
    answer["degraded"] = disabled + answer["degraded"]
    return answer
