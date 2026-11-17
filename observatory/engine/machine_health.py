"""machine_health: one `agent-health/1` reading of this engine (OBS-577, DEC-0179).

The report is `{schema, product, version, at, state, checks:[{id, area, state, count, fix}]}`, counts and codes only
(DEC-0157, `FAC-SEM-181`): no path, name, id or text from a finding reaches it. Every check is derived from the
findings the engine already builds (`registry/findings.json`), so the reading costs one file read.

HL-2/HL-5: a source not read is `unknown`, never `ok`. No findings file makes every check `unknown`; a check whose
scan could not measure (`host.disk_unknown`, a blind leak scan, a stale vulnerability watch, a degraded harness scan)
is `unknown` too. The report's state is the worst of its checks in the order ok < unknown < warn < fail.

The connection area's check reads one more file: the daily integration health report (OBS-557,
`store/raw/integration-health.json`). Without it the check is `unknown`, because its findings are only written from
that report. Not read here: an engine version behind the published one (the engine records no such check); that
check is omitted, not reported ok.

`worst_state`, `read_health`, `episode_key` and `report_rules` port `src/agent-health.ts` of the contract at the
commit named in `fabric/agent-health/README.md`; `tests/test_machine_health.py` runs the contract's vectors and
fixtures through them.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import configuration as config
import ops_digest

PRODUCT = "project-observatory"
SCHEMA = "agent-health/1"
STATE_ORDER = ("ok", "unknown", "warn", "fail")
AREAS = ("lifecycle", "sessions", "accounts", "crash", "errors", "update", "environment", "security", "connection")
VENDORED = Path(__file__).resolve().parent / "fabric" / "agent-health"

CHECK_ID = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")
SLUG = re.compile(r"\A[a-z][a-z0-9-]{1,62}\Z")


def _rank(state: Any) -> int:
    return STATE_ORDER.index(state) if isinstance(state, str) and state in STATE_ORDER else -1


def worst_state(states: list) -> str:
    """HL-2: the worst of `states` in the order ok < unknown < warn < fail; unknown for none or for a non-state."""
    if not states:
        return "unknown"
    worst = 0
    for state in states:
        rank = _rank(state)
        if rank < 0:
            return "unknown"
        worst = max(worst, rank)
    return STATE_ORDER[worst]


def _hidden(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if ops_digest.secret_value(value):
        return "is shaped like a credential"
    if ops_digest.has_home_path(value):
        return "holds a home folder"
    return None


def report_rules(value: dict) -> list[dict]:
    """`FAC-SEM-181` over one report: the state is the worst of the checks, each id once, no name that hides a value."""
    findings: list[dict] = []

    def add(path: str, message: str) -> None:
        findings.append({"code": "FAC-SEM-181", "instancePath": path, "message": message})

    why = _hidden(value.get("product"))
    if why:
        add("/product", f"the product id {why}; a health report carries codes, never a value (DEC-0157)")
    checks = value.get("checks") if isinstance(value.get("checks"), list) else []
    seen: set[str] = set()
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            continue
        name = check.get("id")
        if isinstance(name, str):
            if name in seen:
                add(f"/checks/{index}/id", "a check id is named once in a report")
            seen.add(name)
            why = _hidden(name)
            if why:
                add(f"/checks/{index}/id", f"a check id {why}")
        fix = check.get("fix")
        if isinstance(fix, dict):
            for key in ("tool", "capability"):
                why = _hidden(fix.get(key))
                if why:
                    add(f"/checks/{index}/fix/{key}", f"a fix's {key} {why}")
    worst = worst_state([c.get("state") if isinstance(c, dict) else None for c in checks])
    if value.get("state") != worst:
        add("/state", "a report with no checks is unknown (HL-2)" if not checks
            else f"state is the worst of the checks in the order ok < unknown < warn < fail, here {worst} (HL-2)")
    return findings


def service_rules(value: dict) -> list[dict]:
    """HL-7: a well-known answer that lists a degraded source is never read beside an ok health report."""
    health, well_known = value.get("health"), value.get("wellKnown")
    if not isinstance(health, dict):
        return []
    found = [{**f, "instancePath": "/health" + f["instancePath"]} for f in report_rules(health)]
    if isinstance(well_known, dict):
        degraded = well_known.get("degraded") if isinstance(well_known.get("degraded"), list) else []
        if (degraded or well_known.get("status") == "degraded") and health.get("state") == "ok":
            found.append({"code": "FAC-SEM-181", "instancePath": "/health/state",
                          "message": "the well-known answer lists a degraded source, so the health report is not ok (HL-7)"})
    return found


_SHAPE: Any = None


def _schema_errors(value: Any) -> list:
    global _SHAPE
    if _SHAPE is None:
        import jsonschema
        from referencing import Registry, Resource
        docs = [json.loads((VENDORED / n).read_text(encoding="utf-8"))
                for n in ("agent-health.schema.json", "common.schema.json")]
        registry = Registry().with_resources([(d["$id"], Resource.from_contents(d)) for d in docs])
        _SHAPE = jsonschema.Draft202012Validator(docs[0], registry=registry)
    return list(_SHAPE.iter_errors(value))


def read_health(answer: Any) -> dict:
    """HL-5: the state a reader counts from one answer; a report it cannot trust is `unknown` with the reason."""
    if answer is None:
        return {"state": "unknown", "reason": "no-answer"}
    if _schema_errors(answer):
        return {"state": "unknown", "reason": "schema"}
    if report_rules(answer):
        return {"state": "unknown", "reason": "FAC-SEM-181"}
    return {"state": answer["state"]}


def episode_key(product: str, check_id: str, at: int) -> str:
    """HL-6: `<product>:<check id>@<UTC day of at>`."""
    return f"{product}:{check_id}@{datetime.fromtimestamp(at, timezone.utc).strftime('%Y-%m-%d')}"


# ── what the engine measures ─────────────────────────────────────────────────────────────────────────────────

Match = Callable[[dict], bool]


def _types(*kinds: str) -> Match:
    return lambda f: f.get("type") in kinds


#: One row per check: id, area, the findings that count, the findings that say the scan could not measure, the
#: severity-to-state rule, and the MCP tool that shows or clears it. `fixed` is a state, `by_severity` reads each
#: finding's own severity (critical is fail, anything else warn).
CHECKS: tuple[dict, ...] = (
    {"id": "disk_low", "area": "environment", "counts": _types("machine.disk_low", "host.disk_low"),
     "blind": _types("host.disk_unknown"), "tool": "observatory_machine"},
    {"id": "memory_pressure", "area": "environment", "counts": _types("machine.memory_pressure"),
     "tool": "observatory_machine", "state": "warn"},
    {"id": "vm_disk_errors", "area": "environment", "counts": _types("vm.disk_errors"),
     "tool": "observatory_machine", "state": "fail"},
    {"id": "plugins_behind", "area": "update", "counts": _types("harness.plugin_behind"),
     "blind": _types("harness.scan_degraded"), "tool": "observatory_machine", "state": "warn"},
    {"id": "keys_leaked_unrotated", "area": "security", "counts": _types("secret.leaked_unrotated"),
     "blind": _types("secret.register_unreadable", "secret.leak_scan_unmeasured", "secret.leak_scan_blind"),
     "tool": "observatory_credentials"},
    {"id": "keys_expiring", "area": "security", "counts": _types("credential.expiring", "credential.expired"),
     "tool": "observatory_credentials"},
    {"id": "dependencies_exploited", "area": "security",
     "counts": lambda f: f.get("type") == "dep.vulnerable" and f.get("exploited") is True,
     "blind": _types("dep.watch_stale"), "tool": "observatory_dependencies", "state": "warn"},
    {"id": "keys_in_repositories", "area": "security", "counts": _types("secret.in_repository"),
     "blind": _types("secret.leak_scan_unmeasured", "secret.leak_scan_blind"), "tool": "observatory_findings"},
    {"id": "mcp_risky", "area": "security", "counts": _types("mcp.key_in_url", "mcp.risky_command"),
     "blind": _types("harness.scan_degraded"), "tool": "machine_mcp_inventory", "state": "warn"},
    {"id": "integration_keys_invalid", "area": "connection", "counts": _types("integration.key_invalid"),
     "needs": "integration", "tool": "observatory_integrations", "state": "warn"},
    {"id": "ssh_keys", "area": "security",
     "counts": lambda f: str(f.get("type") or "").startswith("ssh."), "tool": "observatory_ssh_keys",
     "state": "warn"},
)


def _check(row: dict, findings: list[dict] | None, read: dict | None = None) -> dict:
    out: dict[str, Any] = {"id": row["id"], "area": row["area"]}
    if findings is None or (row.get("needs") and not (read or {}).get(row["needs"])):
        out.update(state="unknown", count=0)
    else:
        hits = [f for f in findings if row["counts"](f)]
        blind = row.get("blind")
        if hits:
            state = row.get("state") or ("fail" if any(f.get("severity") == "critical" for f in hits) else "warn")
            out.update(state=state, count=len(hits))
        elif blind and any(blind(f) for f in findings):
            out.update(state="unknown", count=0)
        else:
            out.update(state="ok", count=0)
    out["fix"] = {"tool": row["tool"]} if row.get("tool") else None
    return out


def read_sources(registry: Path | None = None, scratch: Path | None = None) -> dict:
    """The findings the engine last built: a list, or None when the file is absent or unreadable (never an empty list),
    and `integration`: whether the daily integration health report exists and parses (its findings come from it)."""
    import paths
    if registry is None:
        registry = paths.REGISTRY
    try:
        doc = json.loads(Path(scratch or paths.SCRATCH, "integration-health.json").read_text(encoding="utf-8"))
        integration = isinstance(doc, dict) and bool(doc.get("checked_on"))
    except (OSError, ValueError):
        integration = False
    try:
        doc = json.loads((Path(registry) / "findings.json").read_text(encoding="utf-8"))
        rows = doc["findings"]
        if not isinstance(rows, list):
            raise ValueError("findings is not a list")
    except (OSError, ValueError, KeyError, TypeError):
        return {"findings": None, "integration": integration}
    return {"findings": [f for f in rows if isinstance(f, dict) and not f.get("acked")], "integration": integration}


def report(sources: dict | None = None, now: float | None = None, version: str | None = None) -> dict:
    """One `agent-health/1` object for this engine. `sources` is injectable (`{"findings": [...] | None}`)."""
    read = read_sources() if sources is None else sources
    findings = read.get("findings")
    if findings is not None:
        findings = [f for f in findings if isinstance(f, dict) and not f.get("acked")]
    checks = [_check(row, findings, read) for row in CHECKS]
    return {"schema": SCHEMA, "product": PRODUCT, "version": version or config.VERSION,
            "at": int(time.time() if now is None else now),
            "state": worst_state([c["state"] for c in checks]), "checks": checks}


def main(argv: list[str]) -> int:
    """`project-observatory full health [--json]`: the same document the MCP tool `observatory_health` answers."""
    import argparse
    parser = parser_()
    parser.parse_args(argv)
    print(json.dumps(report(), indent=2))
    return 0


def parser_() -> "argparse.ArgumentParser":
    import argparse
    ap = argparse.ArgumentParser(prog="project-observatory full health",
                                 description="One agent-health/1 reading of this engine: counts and codes only.")
    ap.add_argument("--json", action="store_true", help="the report as JSON (the only format)")
    return ap


def parser():
    return parser_()
