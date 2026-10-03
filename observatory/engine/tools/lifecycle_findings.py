#!/usr/bin/env python3
"""What the lifecycle watch owes the operator, one finding per product and kind.

Reads `store/raw/lifecycle.json` (collectors/scan_lifecycle.py). Each kind of
observation becomes one finding per product, naming up to LISTED examples and the
rule of the organisation's lifecycle contract it breaks (fabric-workspace
knowledge/lifecycle.md). The board never acts: it names the process or the file
and the command that explains it; the product's own tests are where the fix lands.

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

LISTED = 5

#: kind -> finding type and the contract rule it breaks. Titles are literal
#: `titled` calls in `findings`, so the catalog checks (tests/test_i18n.py) read every id.
KINDS = {
    "orphan": {"type": "lifecycle.orphans", "rule": "LC-02"},
    "stale-code": {"type": "lifecycle.stale_servers", "rule": "LC-10"},
    "overrun": {"type": "lifecycle.overrun", "rule": "LC-03"},
    "log-over-cap": {"type": "lifecycle.logs_over_cap", "rule": "LC-12"},
    "log-readable": {"type": "lifecycle.log_readable", "rule": "LC-12"},
}
SEVERITY = {"orphan": "warning", "stale-code": "warning", "overrun": "warning",
            "log-over-cap": "info", "log-readable": "warning"}
TYPES = tuple(k["type"] for k in KINDS.values())
TITLES = ("{n} processes of {product} outlived their parent",
          "{n} session servers of {product} run replaced code",
          "a job of {product} runs longer than its interval",
          "{n} logs of {product} are past their cap",
          "{n} logs of {product} are readable by other accounts")


ACTIONS = {
    "orphan": "`project-observatory full machine --explain PID` names what started it; stop it if nothing "
              "uses it, and give the product a test that plants a child and asserts none survives (LC-02)",
    "stale-code": "restart the sessions that own these servers (or reconnect the server in each); the "
                  "product's server should compare its version with the installed one and answer stale (LC-10)",
    "overrun": "read the job's own log for the step that hangs; its watchdog must be shorter than the "
               "interval (LC-03)",
    "log-over-cap": "the product must rotate by size, 5 × 5 MB by default (LC-12); for this engine "
                    "`python \"$(project-observatory full-path)/tools/rotate_logs.py\"`",
    "log-readable": "`chmod 600` the files; the product must create its logs owner-only (LC-12)",
}


def _example(kind: str, o: dict) -> str:
    if kind in ("orphan", "stale-code"):
        return f"{o.get('process') or '?'} (pid {o.get('pid')})"
    if kind == "overrun":
        return f"{o.get('label')} (pid {o.get('pid')}, {o.get('elapsed_s')} s of {o.get('interval_s')} s)"
    if kind == "log-over-cap":
        return f"{o.get('file')} ({int(o.get('bytes') or 0) // 1024} KB of {int(o.get('cap') or 0) // 1024} KB)"
    return f"{o.get('file')} (mode {o.get('mode')})"


def findings(doc: dict | None) -> list[dict]:
    if not isinstance(doc, dict):
        return []
    groups: dict[tuple[str, str], list[dict]] = {}
    names: dict[str, str] = {}
    for o in doc.get("observations") or []:
        if isinstance(o, dict) and o.get("kind") in KINDS and o.get("slug"):
            groups.setdefault((o["slug"], o["kind"]), []).append(o)
            names[o["slug"]] = str(o.get("product") or o["slug"])
    out = []
    for (product_slug, kind), rows in sorted(groups.items()):
        ftype, rule = KINDS[kind]["type"], KINDS[kind]["rule"]
        product = names[product_slug]
        out.append({
            "type": ftype, "subject": f"product:{product_slug}", "severity": SEVERITY[kind],
            **(titled("{n} processes of {product} outlived their parent", n=len(rows), product=product)
               if kind == "orphan" else
               titled("{n} session servers of {product} run replaced code", n=len(rows), product=product)
               if kind == "stale-code" else
               titled("a job of {product} runs longer than its interval", product=product)
               if kind == "overrun" else
               titled("{n} logs of {product} are past their cap", n=len(rows), product=product)
               if kind == "log-over-cap" else
               titled("{n} logs of {product} are readable by other accounts", n=len(rows), product=product)),
            "detail": (f"Lifecycle contract {rule}, measured {doc.get('measured_at') or 'at the last tick'}: "
                       + "; ".join(_example(kind, o) for o in rows[:LISTED])
                       + (f"; and {len(rows) - LISTED} more" if len(rows) > LISTED else "") + "."),
            "action": ACTIONS[kind],
            "evidence": ["store/raw/lifecycle.json"]})
    return out


def from_file(path: pathlib.Path) -> list[dict]:
    try:
        return findings(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []
