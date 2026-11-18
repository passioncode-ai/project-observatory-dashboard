#!/usr/bin/env python3
"""What the integration health step owes the operator (OBS-557, `tools/integration_health.py`).

- `integration.key_invalid`: a held key the provider refuses (401 or 403). Names the slot, never the value.
- `integration.drift`: a field a collector relies on is missing from every row of the stored provider answer.
- `integration.untracked` (info): a project uses a provider (a package or a variable name) that no door or
  collector here covers. Carries the project, so the issue filer can reach its repository.
- `integration.scope_missing`: a held key lacks a scope the collectors or its door's preset need (OBS-605).
- `integration.scope_excess`: a held key holds a scope beyond that need, which widens what a leak of it reaches.

Every finding carries a `code`: `key-invalid`, `field-drift`, `untracked-key` (a variable, a key, points at a provider
with no door) or `untracked-package`, `scope-missing`, `scope-excess`.

Silent until the step has run once: no answer is not a clean bill (`observatory_integrations` says so in `degraded`).
NO TIMESTAMP INSIDE A FINDING beyond the day the step checked.
"""
from __future__ import annotations
import json
import pathlib
import pathlib as _pathlib
import sys as _sys
_DASHBOARD = str(_pathlib.Path(__file__).resolve().parents[1] / "dashboard")
if _DASHBOARD not in _sys.path:
    _sys.path.append(_DASHBOARD)  # `finding_types.titled`: a title is a message id
from finding_types import titled  # noqa: E402

TYPES = ("integration.key_invalid", "integration.drift", "integration.untracked", "integration.scope_missing",
         "integration.scope_excess")
EVIDENCE = ["store/raw/integration-health.json"]


def findings(doc: dict | None) -> list[dict]:
    if not isinstance(doc, dict):
        return []
    day = doc.get("checked_on")
    out: list[dict] = []
    for k in doc.get("keys") or []:
        out += _scope_findings(k, day)
        if k.get("state") != "invalid":
            continue
        out.append({
            "type": "integration.key_invalid", "subject": f"integration:key/{k['slot']}", "severity": "warning",
            "code": "key-invalid",
            **titled("The key {slot} is refused by {provider}", slot=k["slot"], provider=k["provider"]),
            "detail": f"{k['provider']} answered {k.get('status')} to the key held as {k['slot']} on {day}"
                      + (f", and has since {k['since']}" if k.get("since") and k["since"] != day else "")
                      + ". Anything that reads through it fails.",
            "action": f"issue or stash a replacement for {k['slot']} through the {k['provider']} door, then "
                      "let the next daily check see it",
            "evidence": EVIDENCE, "provider": k["provider"], "slot": k["slot"], "status": k.get("status")})
    for d in doc.get("drift") or []:
        seen = d.get("last_seen") or "never"
        out.append({
            "type": "integration.drift", "subject": f"integration:drift/{d['collector']}/{d['field']}",
            "severity": "warning", "code": "field-drift",
            **titled("The {collector} answer no longer carries {field}", collector=d["collector"], field=d["field"]),
            "detail": f"No row of the {d['collector']} collector's answer ({d.get('rows')} row(s), checked {day}) "
                      f"carries `{d['field']}`; it was last seen {seen}. The provider may have renamed or removed it.",
            "action": f"read the provider's changelog for {d['collector']} and update the collector's reader",
            "evidence": EVIDENCE, "collector": d["collector"], "field": d["field"], "last_seen": d.get("last_seen")})
    for u in doc.get("untracked") or []:
        names = ", ".join(e["name"] for e in u.get("evidence") or [])
        more = u.get("evidence_total", 0) - len(u.get("evidence") or [])
        out.append({
            "type": "integration.untracked", "subject": f"integration:untracked/{u['project']}/{u['provider']}",
            "severity": "info", "code": u.get("code") or "untracked-package",
            **titled("{project} uses {provider}, which nothing here watches", project=u["project"].split(":", 1)[-1],
                     provider=u["provider"]),
            "detail": f"{u['project']} uses {u['provider']} ({u.get('category')}): {names}"
                      + (f" and {more} more" if more > 0 else "") + ". No door or collector covers it.",
            "action": f"add a door for {u['provider']}, or accept this finding if its keys are not the estate's to watch",
            "evidence": EVIDENCE, "project": u["project"], "provider": u["provider"], "category": u.get("category")})
    return out


def _scope_findings(k: dict, day) -> list[dict]:
    """One finding for the scopes a key lacks and one for those it holds beyond the need: names, never a value."""
    scope = k.get("scope") if isinstance(k.get("scope"), dict) else {}
    out = []
    needed = ", ".join(scope.get("needed") or []) or "nothing named"
    granted = ", ".join(scope.get("granted") or []) or "no scope"
    if scope.get("missing"):
        missing = ", ".join(scope["missing"])
        out.append({
            "type": "integration.scope_missing", "subject": f"integration:scope/{k['slot']}", "severity": "warning",
            "code": "scope-missing",
            **titled("The key {slot} lacks a scope its readers need", slot=k["slot"]),
            "detail": f"{k['provider']} grants {granted} to the key held as {k['slot']} (checked {day}); what reads "
                      f"through it needs {needed}, so {missing} is missing and those reads fail.",
            "action": f"issue a replacement for {k['slot']} with the scopes it needs through the {k['provider']} door",
            "evidence": EVIDENCE, "provider": k["provider"], "slot": k["slot"], "missing": scope["missing"]})
    if scope.get("excess"):
        excess = ", ".join(scope["excess"])
        out.append({
            "type": "integration.scope_excess", "subject": f"integration:scope-excess/{k['slot']}",
            "severity": "warning", "code": "scope-excess",
            **titled("The key {slot} holds more scope than its readers need", slot=k["slot"]),
            "detail": f"{k['provider']} grants {granted} to the key held as {k['slot']} (checked {day}); what reads "
                      f"through it needs {needed}, so {excess} is more than it needs and widens what a leak reaches.",
            "action": f"issue a narrower replacement for {k['slot']} through the {k['provider']} door, then revoke "
                      "this one",
            "evidence": EVIDENCE, "provider": k["provider"], "slot": k["slot"], "excess": scope["excess"]})
    return out


def from_file(path: pathlib.Path) -> list[dict]:
    try:
        return findings(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []
