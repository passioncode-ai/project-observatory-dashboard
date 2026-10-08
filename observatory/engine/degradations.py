#!/usr/bin/env python3
"""What a collector said about its own coverage, read the same way everywhere.

Honest degradation: a source that could not be read appears in `degraded` with its
reason, and never as an empty result in place of a partial one. Every collector
writes that list; this is the one function that reads it.

It lives here rather than in `survey.py` because it now has two callers, and the
last time a rule about collector output existed in two places the two disagreed:
`survey` expected an object with a `degraded` key while `scan_github` wrote a
bare list, so the largest source in the estate could fail on every owner and no
survey, dashboard or finding would mention it. Both shapes are accepted below,
in one place, so a third caller cannot pick the wrong half.

Absent and empty are deliberately DIFFERENT claims. A missing file means "this
collector has not run", and a caller that reports it as "measured and fine" has
turned silence into a clean bill of health — the exact substitution the whole
degradation channel exists to prevent.
"""
from __future__ import annotations
import json

import paths


#: Which integration a receipt belongs to, DECLARED, for the receipts written by a
#: collector that an integration switch turns off. When the switch is off the
#: collector returns before it writes, so the receipt it wrote while the switch
#: was on stays in the scratch directory and nothing ever replaces it. Read as a
#: measurement, that leftover held a service degraded for days on the strength of
#: a run from before the operator turned the integration off. Each name must be
#: an integration `tools/tick_lease.py` gates a step on; a test holds that.
RECEIPT_INTEGRATIONS = {
    "bitbucket.json": "bitbucket", "domains_live.json": "domains", "heroku.json": "heroku",
    "openrouter.json": "openrouter", "cloudflare_zones.json": "cloudflare", "mcp.json": "mcp",
    "remote-env.json": "remote_env", "google.json": "google", "sessions.json": "sessions",
    "remotes.json": "git_remotes", "vault.json": "wiki",
    # `tools/record_lost_projects.py` reads the sessions collector's output (0.19.3).
    "lost-projects.json": "sessions",
}


def integration_off(name: str) -> str | None:
    """The integration a receipt belongs to when the workspace has it OFF, else None.

    The settings are read directly rather than through `configuration.enabled`:
    `OBSERVATORY_OFFLINE` makes every integration read as off for one run, and a
    run mode must not hide the operator's measurements. A configuration that
    cannot be read hides nothing either: the receipt is then read as before.
    """
    integration = RECEIPT_INTEGRATIONS.get(name)
    if integration is None:
        return None
    try:
        import configuration
        on = configuration.load().get("integrations", {}).get(integration) is True
    except Exception:  # noqa: BLE001 — an unreadable switch is not a switch that is off
        return None
    return None if on else integration


#: Which integration each of the merge's own coverage sources belongs to. Read by
#: the board (`tools/build_findings.py`, which raises no row for a switched-off
#: one) and by the merge's console summary, which lists those apart from the
#: degraded ones: an integration the user never turned on is not a failure.
MERGE_SOURCE_INTEGRATION = {
    "wiki": "wiki", "github": "github", "sessions.json": "sessions",
    "remotes.json": "git_remotes", "bitbucket.json": "bitbucket",
}


def merge_source_integration(source: str) -> str | None:
    """The integration a merge coverage source belongs to, or None if it is always read.

    `transfer:<owner/name>` rows are GitHub's: the transfer check asks the GitHub API,
    so with that integration off it cannot be asked at all — a consequence of the
    switch, not a failure (0.19.3; a tester's card counted two of them as degraded)."""
    if source.startswith("transfer:"):
        return "github"
    return MERGE_SOURCE_INTEGRATION.get(source)


def merge_source_off(source: str) -> str | None:
    """The integration a merge coverage source belongs to when the workspace has
    it OFF, else None. Settings are read directly, for the reason `integration_off`
    gives: `local` runs offline, and that run mode is not the user's switch."""
    integration = merge_source_integration(source)
    if integration is None:
        return None
    try:
        import configuration
        on = configuration.load().get("integrations", {}).get(integration) is True
    except Exception:  # noqa: BLE001 — an unreadable switch is not a switch that is off
        return None
    return None if on else integration


def _leftover(name: str, integration: str) -> dict:
    """The note that names a receipt an integration that is off left behind."""
    try:
        doc = json.loads((paths.SCRATCH / name).read_text(encoding="utf-8"))
        when = (doc.get("scanned_at") or doc.get("measured_at") or doc.get("scanned_on") or "")
    except (ValueError, OSError, AttributeError):
        when = ""
    return {"source": f"integration:{integration}",
            "reason": (f"the {integration} integration is off in this workspace, so "
                       f"store/raw/{name}" + (f" from {str(when)[:10]}" if when else "")
                       + " is a leftover of an earlier run and is not read as a measurement")}


def collector(name: str) -> list[dict]:
    """The `degraded` rows a collector wrote, or [] if it never ran.

    `name` is relative to the scratch directory — `"bitbucket.json"`,
    `"gh/_degraded.json"`, `"model.json"`. Resolved through `paths.SCRATCH` so a
    fixture can redirect it; a hardcoded `store/raw` was the third instance of
    that class in one sitting and `tools/check_paths.py` now refuses it.
    """
    f = paths.SCRATCH / name
    if not f.is_file() or integration_off(name):
        return []
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        # An unreadable file is a measurement in its own right, and the loudest
        # kind: it means a collector wrote something nothing can parse.
        return [{"source": name, "reason": "collector output is unreadable"}]
    if isinstance(doc, list):
        return list(doc)
    return list(doc.get("degraded") or [])

#: Receipts whose degradations already have a TAILORED reader, and must not be
#: reported twice. Declared rather than inferred: `model.json`'s own rule in
#: `tools/build_findings.py` names the remedy for a `gh` authentication failure
#: and deduplicates on (source, reason) after a 400-char cut swallowed one of
#: three sources. A generic row beside it would say the same thing
#: worse.
OWN_READER = {
    "model.json": "tools/build_findings.py builds `model.degraded` with the "
                  "remedy for a `gh` failure and its own deduplication",
}


def every_collector() -> dict[str, list[dict]]:
    """Every receipt in the scratch that carries a `degraded` list, DERIVED.

    **Measured 2026-09-08: two of six had a reader, and they were in different
    surfaces.** `model.json` reached the operator's board, `bitbucket.json` and
    `domains_live.json` reached the wire, and `sessions.json` and `local.json`
    reached nobody — while `sessions.json` held a live degradation at that
    moment. So a collector could report honestly that claude-mem's store was
    unreadable or that its shape had moved, and the estate would lose its
    session half in silence: exactly the state `collectors/scan_sessions.py`
    exists to prevent, reached by the back door.

    Derived from the directory rather than listed, so a collector added tomorrow
    is surfaced by default and skipping one takes a sentence in `OWN_READER`.
    """
    out: dict[str, list[dict]] = {}
    if not paths.SCRATCH.is_dir():
        return out
    for f in sorted(paths.SCRATCH.glob("*.json")):
        if f.name in OWN_READER or integration_off(f.name):
            continue
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            out[f.name] = [{"source": f.name,
                            "reason": "collector output is unreadable"}]
            continue
        # A `degraded` KEY is the contract. A receipt without one is not a
        # collector report — `tick.json`, `integrity.json` and the rest — and
        # inventing an empty list for them would put every receipt in a rule
        # about collectors.
        if isinstance(doc, dict) and isinstance(doc.get("degraded"), list) and doc["degraded"]:
            out[f.name] = list(doc["degraded"])
    return out


def every_not_applicable() -> dict[str, list[dict]]:
    """Every receipt's `not_applicable` rows: sources that do not apply HERE.

    The second list beside `degraded`, and deliberately a different claim. A row
    on it was measured and found not to apply to this machine or this estate — a
    companion tool that is not installed, a registry that runs no RDAP service
    for a domain DNS shows is held, a credential set up for another surface that
    a second credential already reads. Nothing failed and no person can act, so it
    must not hold a service degraded; but it is still a fact about coverage, so it
    is kept, read here and shown, never dropped. A receipt an integration that is
    off left behind is reported on this list too, in place of its stale rows.
    """
    out: dict[str, list[dict]] = {}
    if not paths.SCRATCH.is_dir():
        return out
    for f in sorted(paths.SCRATCH.glob("*.json")):
        off = integration_off(f.name)
        if off:
            out[f.name] = [_leftover(f.name, off)]
            continue
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue  # an unreadable receipt is reported by `every_collector`
        rows = doc.get("not_applicable") if isinstance(doc, dict) else None
        if isinstance(rows, list) and rows:
            out[f.name] = [r for r in rows if isinstance(r, dict)]
    return out
