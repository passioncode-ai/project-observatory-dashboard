#!/usr/bin/env python3
"""The agents installed on this machine, one row per Fabric service, generated.

    scan_agents.py store/raw/agents.json        # the tick's collector: descriptors + launchd

WHY. The estate map says the engine generates the list of agents on a machine, and until
OBS-83 nothing did: the wiki's `inventory/` had no `agents.json` and no code named one. The
list a person or an agent wants — which agents exist, where each answers, which contract it
pins, who supervises it, whether it is running, which MCP declarations reach it — was spread
over three places this engine already reads for other reasons: the Fabric service descriptors
(`fabric_service.services_dir()`), the MCP inventory (`store/raw/mcp.json`, written by
`collectors/scan_mcp.py`), and launchd's own map.

TWO HALVES, because the tick runs its collectors in rotation (`tools/tick.py`, `Rotation`) and
so cannot promise this one runs after `scan-mcp`:

* `collect()` — this collector, run by the tick — reads the descriptors, each one's Fabric
  manifest for the contract pins, and launchd, and writes `store/raw/agents.json`;
* `document()` — called by `collectors/emit_registry.py`, after every collector — joins the MCP
  inventory, carries first-seen and last-running dates from the previous registry document and
  writes `registry/agents.json`. `tools/project_into_vault.py` mirrors every `registry/*.json`,
  so the wiki gets `inventory/agents.json` with no code of its own.

A ROW IS NEVER INFERRED FROM A NAME. Rows come from descriptors only. The MCP inventory and
launchd are joined to a row by a key the descriptor states — the origin's host and port, the
launchd label — so an MCP server or a launchd job no descriptor claims stays unattributed and is
counted, never guessed onto an agent whose name looks similar.

WHAT IS NEVER WRITTEN. Nothing of `auth`: not the token, not where the token file lives — the
location is not a fact about the agent and the file is a credential. Commands are listed by name
(`doctor`, `update`), never their argument vectors. A path under the home directory is written
`~/…`. No process id and no instant: the document changes when the estate changes, not on every
tick, so the registry is not committed every thirty minutes for nothing.

EVERY SOURCE IS ACCOUNTED FOR (`sources`, `degraded`):

* the services directory `read`, `absent` (no Fabric here: an empty machine, not a partial
  scan) or `unreadable` (degraded; the previous rows are kept, each marked `carried`, because an
  empty list in place of a partial one would be a false claim);
* a descriptor that will not parse, or is not an object, is named in `degraded` by file and the
  kind of error, never its message; one that parses but breaks the contract is LISTED with
  `valid: false` and its problems (`fabric_service.validate_descriptor`), since an agent with a
  broken descriptor still exists;
* a manifest that cannot be read: `manifest.state: unreadable` and degraded; none declared:
  `absent`, not degraded;
* launchd `read`, `failed` (degraded: `launchctl list` did not answer on macOS),
  `not-applicable` (another platform), `skipped` (`OBSERVATORY_AGENTS_LAUNCHD=skip`) or
  `not-needed` (no descriptor names launchd). systemd and Task Scheduler are not read yet: their
  rows say `not-measured` and why;
* no MCP inventory: `mcp_declarations: null` per row (unmeasured, not "none") and degraded.

It starts nothing and signals nothing: `launchctl list` is the only command, run once. The tick
runs it under `features.machine_watch`, the switch of the lifecycle watch that reads the same
descriptors; with it off the step is skipped and `registry/agents.json` keeps its last version.
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
import re
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCHEMA_VERSION = 1
#: The source this document is evidence of (`registry/sources.json`, declared by the emit).
SOURCE_ID = "SRC-0020"
#: The MCP inventory's own source, cited when its declarations were joined.
MCP_SOURCE_ID = "SRC-0017"
LAUNCHD_ENV = "OBSERVATORY_AGENTS_LAUNCHD"
SUPERVISOR_STATES = ("running", "loaded", "not-loaded", "not-measured", "platform", "unsupervised")
MANIFEST_STATES = ("pinned", "absent", "unreadable")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_LOCAL_ORIGIN = re.compile(r"^http://127\.0\.0\.1:([0-9]{1,5})$")
_REMOTE_ORIGIN = re.compile(r"^https://([a-z0-9.-]+)(?::([0-9]{1,5}))?$")


# --- small helpers -------------------------------------------------------------

def _home() -> str:
    return str(pathlib.Path.home()).rstrip("/")


def tilde(value):
    """A string with the home directory written `~`; anything else unchanged."""
    if not isinstance(value, str):
        return value
    home = _home()
    if value == home:
        return "~"
    return value.replace(home + "/", "~/")


def _expand(value: str) -> pathlib.Path:
    return pathlib.Path(os.path.expanduser(value))


def _date(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _reason(path: pathlib.Path, exc: BaseException | str) -> str:
    kind = exc if isinstance(exc, str) else type(exc).__name__
    return f"{path.name}: {kind}"


# --- launchd -------------------------------------------------------------------

def parse_launchctl(out: str) -> dict[str, dict]:
    """`launchctl list` (PID, Status, Label, tab-separated) as {label: {pid, status}}."""
    jobs: dict[str, dict] = {}
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) != 3 or cols[0] == "PID":
            continue
        pid, status, label = cols
        try:
            jobs[label] = {"pid": int(pid) if pid.strip().isdigit() else None,
                           "status": int(status) if status.strip().lstrip("-").isdigit() else None}
        except ValueError:
            continue
    return jobs


def read_launchd() -> dict[str, dict] | None:
    """launchd's map, or None when `launchctl list` did not answer."""
    try:
        p = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=15,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_launchctl(p.stdout) if p.returncode == 0 else None


# --- one descriptor --------------------------------------------------------------

def _origin(origin) -> tuple[str | None, int | None]:
    """(host, port) of an origin; a remote origin without a port has port None (443 implied)."""
    text = str(origin or "")
    m = _LOCAL_ORIGIN.match(text)
    if m:
        return "127.0.0.1", int(m.group(1))
    m = _REMOTE_ORIGIN.match(text)
    if m:
        return m.group(1), int(m.group(2)) if m.group(2) else None
    return None, None


def _manifest(declared, degraded: list, agent_id: str) -> dict:
    if not declared:
        return {"state": "absent"}
    shown = tilde(str(declared))
    path = _expand(str(declared))
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as exc:
        degraded.append({"source": "fabric-manifest", "agent": agent_id,
                         "reason": f"{shown}: {type(exc).__name__} — the contract pins of "
                                   f"{agent_id} were not read"})
        return {"state": "unreadable", "path": shown}
    provider = doc.get("provider") if isinstance(doc.get("provider"), dict) else {}
    pins = {"state": "pinned", "path": shown,
            "contract_version": doc.get("contractVersion"),
            "provider_id": provider.get("id"),
            "provider_revision": provider.get("revision"),
            "content_hash": provider.get("contentHash"),
            "supported_contract_versions": provider.get("supportedContractVersions")}
    return {k: v for k, v in pins.items() if v is not None}


def _supervisor(life, placement: str, launchd: dict | None, launchd_state: str) -> dict:
    life = life if isinstance(life, dict) else {}
    manager = life.get("manager")
    sup: dict = {"manager": manager}
    for key in ("label", "unit", "task"):
        if isinstance(life.get(key), str):
            sup[key] = life[key]
    if isinstance(life.get("plist"), str):
        sup["plist"] = tilde(life["plist"])
        sup["plist_present"] = _expand(life["plist"]).is_file()
    if manager == "launchd":
        if launchd is None:
            sup.update(state="not-measured", why=f"launchd {launchd_state}")
        elif sup.get("label") in launchd:
            job = launchd[sup["label"]]
            sup.update(state="running" if job.get("pid") else "loaded")
            if job.get("status") is not None:
                sup["last_exit_status"] = job["status"]
        else:
            sup.update(state="not-loaded")
    elif manager == "none":
        sup["state"] = "platform" if placement == "remote" else "unsupervised"
    else:
        sup.update(state="not-measured",
                   why=f"{manager or 'an unknown manager'} is not read by this scan; launchd is")
    return sup


def _row(path: pathlib.Path, doc: dict, launchd: dict | None, launchd_state: str,
         degraded: list) -> dict:
    import fabric_service
    service_id = doc.get("id") if isinstance(doc.get("id"), str) else None
    instance = doc.get("instance") if isinstance(doc.get("instance"), str) else None
    if service_id and instance:
        agent_id = f"agent:{service_id}.{instance}"
    else:
        agent_id = f"agent:{path.stem}"
    try:
        problems = [tilde(p) for p in fabric_service.validate_descriptor(doc)]
    except Exception as exc:                                        # noqa: BLE001 — any shape
        problems = [f"the descriptor could not be checked: {type(exc).__name__}"]
    placement = "remote" if doc.get("placement") == "remote" else "local"
    host, port = _origin(doc.get("origin"))
    source = doc.get("source") if isinstance(doc.get("source"), dict) else {}
    commands = doc.get("commands") if isinstance(doc.get("commands"), dict) else {}
    row = {"id": agent_id, "service_id": service_id, "instance": instance,
           "name": doc.get("name") if isinstance(doc.get("name"), str) else None,
           "summary": doc.get("summary") if isinstance(doc.get("summary"), str) else None,
           "placement": placement,
           "origin": doc.get("origin") if isinstance(doc.get("origin"), str) else None,
           "host": host, "port": port,
           "protocol": doc.get("protocol") if isinstance(doc.get("protocol"), str) else None,
           "descriptor": path.name,
           "installed_at": doc.get("installedAt") if isinstance(doc.get("installedAt"), str) else None,
           "source_repository": source.get("repository") if isinstance(source.get("repository"), str) else None,
           "commands": sorted(k for k in commands if k in ("doctor", "update")),
           "supervisor": _supervisor(doc.get("lifecycle"), placement, launchd, launchd_state),
           "manifest": _manifest(doc.get("fabricManifest"), degraded, agent_id),
           "valid": not problems, "problems": problems}
    return row


# --- the collector ----------------------------------------------------------------

def collect(directory: pathlib.Path, *, launchd: dict | None = None, platform: str | None = None,
            launchd_failed: bool = False, now: datetime | None = None,
            read_launchd_now: bool = False) -> dict:
    """The scan: every descriptor in `directory`, its manifest, and launchd's view of it.

    `launchd` is the label map when the caller already has it (tests inject one); with
    `read_launchd_now` the collector asks `launchctl` itself, once, and only when a descriptor
    names launchd."""
    now = now or datetime.now(timezone.utc)
    platform = platform or sys.platform
    degraded: list[dict] = []
    files: list[pathlib.Path] = []
    if not directory.exists():
        dir_state = "absent"
    else:
        try:
            files = sorted(p for p in directory.iterdir() if p.suffix == ".json")
            dir_state = "read"
        except OSError as exc:
            dir_state = "unreadable"
            degraded.append({"source": "fabric-services",
                             "reason": f"the services directory could not be listed: "
                                       f"{type(exc).__name__}; the previous rows are kept"})
    docs: list[tuple[pathlib.Path, dict]] = []
    for path in files:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            degraded.append({"source": "fabric-services", "reason": _reason(path, exc)
                             + " — this descriptor's agent is not listed"})
            continue
        if not isinstance(doc, dict):
            degraded.append({"source": "fabric-services", "reason": _reason(path, "not a JSON object")
                             + " — this descriptor's agent is not listed"})
            continue
        docs.append((path, doc))

    wants_launchd = any(isinstance(d.get("lifecycle"), dict) and d["lifecycle"].get("manager") == "launchd"
                        for _p, d in docs)
    if platform != "darwin":
        launchd_state, launchd = "not-applicable", None
    elif os.environ.get(LAUNCHD_ENV) == "skip":
        launchd_state, launchd = "skipped", None
    elif not wants_launchd:
        launchd_state, launchd = "not-needed", None
    else:
        if launchd is None and read_launchd_now and not launchd_failed:
            launchd = read_launchd()
            launchd_failed = launchd is None
        if launchd is None and launchd_failed:
            launchd_state = "failed"
            degraded.append({"source": "launchd",
                             "reason": "`launchctl list` did not answer; whether each launchd-supervised "
                                       "agent is loaded or running was not measured"})
        elif launchd is None:
            launchd_state = "not-measured"
        else:
            launchd_state = "read"

    rows: list[dict] = []
    seen: set[str] = set()
    for path, doc in docs:
        row = _row(path, doc, launchd, launchd_state, degraded)
        if row["id"] in seen:
            degraded.append({"source": "fabric-services",
                             "reason": f"{path.name}: claims {row['id']}, which an earlier descriptor "
                                       f"already claims — not listed twice"})
            continue
        seen.add(row["id"])
        rows.append(row)
    return {"schema_version": SCHEMA_VERSION,
            "scanned_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sources": {"fabric_services": {"state": dir_state, "directory": tilde(str(directory)),
                                            "descriptors": len(files)},
                        "launchd": {"state": launchd_state}},
            "services": rows, "degraded": degraded}


# --- the registry document (called by the emit) -----------------------------------------

def _mcp_key(target: str) -> tuple[str, int] | None:
    try:
        parts = urlsplit(target)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not host or parts.scheme not in ("http", "https"):
        return None
    port = port or (443 if parts.scheme == "https" else 80)
    return ("loopback" if host in LOOPBACK else host), port


def _agent_key(row: dict) -> tuple[str, int] | None:
    if not row.get("host"):
        return None
    if row.get("placement") == "remote":
        return row["host"], row.get("port") or 443
    return "loopback", row["port"]


def _join_mcp(rows: list[dict], mcp_scan: dict | None) -> dict | None:
    """Each row's MCP declarations, by the origin both name. None when there is no inventory."""
    if mcp_scan is None:
        for row in rows:
            row["mcp_declarations"] = None
        return None
    import estate_surfaces
    by_key = {_agent_key(r): r for r in rows if _agent_key(r)}
    for row in rows:
        row["mcp_declarations"] = []
    attributed = unattributed = stdio = 0
    for decl in estate_surfaces.mcp_rows(mcp_scan):
        if not decl.get("target"):
            stdio += 1
            continue
        row = by_key.get(_mcp_key(decl["target"]))
        if row is None:
            unattributed += 1
            continue
        row["mcp_declarations"].append(decl["id"])
        attributed += 1
    for row in rows:
        row["mcp_declarations"].sort()
    return {"attributed": attributed, "unattributed_http": unattributed, "stdio": stdio}


def document(scan: dict, mcp_scan: dict | None, previous: dict | None, obs_date: str) -> dict:
    """`registry/agents.json` from the collector's scan, the MCP inventory and the last document."""
    degraded = [dict(d) for d in scan.get("degraded") or []]
    previous_rows = {r.get("id"): r for r in (previous or {}).get("agents") or [] if isinstance(r, dict)}
    dir_state = ((scan.get("sources") or {}).get("fabric_services") or {}).get("state")
    if dir_state == "unreadable":
        rows = [dict(copy.deepcopy(r), carried=True) for r in previous_rows.values()]
        removed: list[str] = []
    else:
        rows = []
        for r in scan.get("services") or []:
            row = copy.deepcopy(r)
            row["carried"] = False
            before = previous_rows.get(row["id"]) or {}
            row["first_seen_on"] = before.get("first_seen_on") or obs_date
            running = (row.get("supervisor") or {}).get("state") == "running"
            row["last_seen_running_on"] = obs_date if running else before.get("last_seen_running_on")
            row["source_refs"] = [SOURCE_ID]
            rows.append(row)
        removed = sorted(set(previous_rows) - {r["id"] for r in rows})
    rows.sort(key=lambda r: r["id"])
    mcp_totals = _join_mcp(rows, mcp_scan)
    if mcp_scan is None:
        degraded.append({"source": "mcp-inventory",
                         "reason": "store/raw/mcp.json is absent, so which MCP declarations reach each "
                                   "agent was not measured; `full scan-mcp` writes it"})
    else:
        for row in rows:
            if row["mcp_declarations"]:
                row["source_refs"] = sorted(set(row.get("source_refs") or [SOURCE_ID]) | {MCP_SOURCE_ID})
    sources = copy.deepcopy(scan.get("sources") or {})
    sources["mcp_inventory"] = ({"state": "absent"} if mcp_scan is None else
                                {"state": "read", "scanned_on": (mcp_scan.get("scanned_at") or "")[:10] or None})
    by_state: dict[str, int] = {}
    for row in rows:
        state = (row.get("supervisor") or {}).get("state") or "not-measured"
        by_state[state] = by_state.get(state, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION, "updated_on": obs_date,
        "scanned_on": (scan.get("scanned_at") or "")[:10] or None,
        "note": ("Every agent installed on this machine as a Fabric service (fabric-service/0.1): "
                 "where it answers, the contract its manifest pins, who supervises it and whether "
                 "launchd has it running, and which MCP declarations reach it by its origin. "
                 "Generated by collectors/scan_agents.py; never edited by hand."),
        "sources": sources,
        "agents": rows,
        "removed": removed,
        "totals": {"agents": len(rows),
                   "local": sum(1 for r in rows if r.get("placement") == "local"),
                   "remote": sum(1 for r in rows if r.get("placement") == "remote"),
                   "valid": sum(1 for r in rows if r.get("valid")),
                   "invalid": sum(1 for r in rows if not r.get("valid")),
                   "manifests_pinned": sum(1 for r in rows if (r.get("manifest") or {}).get("state") == "pinned"),
                   "by_supervisor_state": dict(sorted(by_state.items())),
                   "mcp": mcp_totals},
        "degraded": degraded,
        "source_refs": [SOURCE_ID] + ([MCP_SOURCE_ID] if mcp_scan is not None else []),
    }


# --- the validator's rules ------------------------------------------------------------

def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def problems(doc) -> list[str]:
    """What `tools/validate_registry.py` refuses in `registry/agents.json`."""
    if not isinstance(doc, dict) or not isinstance(doc.get("agents"), list):
        return ["agents.json carries no `agents` list"]
    out: list[str] = []
    ids = [r.get("id") for r in doc["agents"] if isinstance(r, dict)]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        out.append(f"duplicate agent ids: {dup}")
    for row in doc["agents"]:
        if not isinstance(row, dict):
            out.append("an agent row is not an object")
            continue
        rid = row.get("id")
        if not isinstance(rid, str) or not rid.startswith("agent:"):
            out.append(f"agent id is not namespaced: {rid}")
        if "auth" in row:
            out.append(f"{rid} carries auth: the token file is never part of the registry")
        if (row.get("supervisor") or {}).get("state") not in SUPERVISOR_STATES:
            out.append(f"{rid} has an unknown supervisor state: {(row.get('supervisor') or {}).get('state')}")
        if (row.get("manifest") or {}).get("state") not in MANIFEST_STATES:
            out.append(f"{rid} has an unknown manifest state: {(row.get('manifest') or {}).get('state')}")
    home = _home() + "/"
    if any(s.startswith(home) for s in _strings(doc)):
        out.append("agents.json holds an absolute home path; paths under home are written ~/")
    if (doc.get("totals") or {}).get("agents") != len(doc["agents"]):
        out.append("agents.json totals disagree with the list")
    if SOURCE_ID not in (doc.get("source_refs") or []):
        out.append(f"agents.json does not cite {SOURCE_ID}, the source that measures it")
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    import atomic
    import fabric_service
    scan = collect(fabric_service.services_dir(), read_launchd_now=True)
    out = pathlib.Path(argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_json(out, scan)
    rows = scan["services"]
    print(f"agents: {len(rows)} agent(s) from {scan['sources']['fabric_services']['state']} descriptors, "
          f"{sum(1 for r in rows if not r['valid'])} invalid; launchd {scan['sources']['launchd']['state']}")
    for d in scan["degraded"]:
        print(f"  degraded {d['source']}: {d['reason'][:140]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
