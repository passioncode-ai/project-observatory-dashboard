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
    "digitalocean.json": "digitalocean", "cloudflare_account.json": "cloudflare",
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


#: Hours a collector's same degradation may last before it is a finding of its own
#: (`collector.degraded_persistent`, tools/build_findings.py). 2026-10-21: the Fabric attribution
#: collector wrote `degraded: access-refused` on every run for over a day and produced nothing, and
#: the board carried it as one more info row. A day is past any outage a retry rides out.
PERSIST_HOURS = 24
#: Where each current degradation's first sighting is kept, under the store (not the scratch, so a
#: receipt reader never mistakes it for a collector). Reasons only, never a detail.
PERSISTENCE_FILE = "degraded-since.json"


#: DEGRADATIONS THAT ARE THE STATE OF THINGS, not a collector doing nothing (review of #328, M2).
#: `collector.degraded` is info on purpose: most of its rows are structural, explained and not the
#: operator's to fix, and a day of them is no news. Per receipt: (source prefix, a pattern its reason
#: or code must match, why). Both must match, so a real failure from the same source still fires: an
#: OpenRouter listing with no provisioning key, or a spend key whose `GET /activity` failed, is no
#: listing budget and no retired key (confirming review of #328). A collector can also mark one row
#: `"structural": true` where it writes it. Anything else, a new collector's included, is reported once
#: it lasts a day: that is how the Fabric attribution collector's `access-refused` reaches a person.
#: What one checkout's own state reads like in the git hygiene receipts (collectors/scan_git_hygiene.py).
CHECKOUT_STATE = (r"^(registered checkout could not be read|no default branch could be resolved|"
                  r"worktree registration unavailable|(worktree|branch) classification unavailable|"
                  r"current branch unavailable)")
STRUCTURAL: dict[str, tuple[tuple[str, str, str], ...]] = {
    "leak-scan.json": (("", "", "the leak scan's coverage; reported as `secret.leak_scan_partial`, which is "
                                "kept off the push channel (ENG-001)"),),
    "disk-guard.json": (("holders", r"^what processes hold could not be read",
                         "no removal is admitted until non-use can be proven: a policy (OBS-173)"),),
    "spend.json": (("spend:openrouter:key:", r"^(a retired key|retired with|rotated on or before|"
                                             r"a provider hash held by two|no provider hash to read by)",
                    "a retired or rotated key's history: nothing left to measure"),),
    "git-hygiene.json": tuple((prefix, CHECKOUT_STATE, "one checkout's own state") for prefix in ("local:", "repository:"))
    + (("worktree-registration", r"^registration budget exhausted", "the registration budget; the rest is read next pass"),
       ("git-hygiene", r"^the step's time ran out after \d+ of \d+ checkouts",
        "the step's share of the tick; the checkouts not reached carry their last measurement and go first next pass")),
    "worktree-registration.json": tuple((prefix, CHECKOUT_STATE, "one checkout's own state") for prefix in ("local:", "repository:"))
    + (("worktree-registration", r"^registration budget exhausted", "the registration budget; the rest is read next pass"),),
    "local.json": (("folder:", r"^(remote|remotes_all|dirty): |^mtime unreadable",
                    "one folder's own state (no remote, unreadable)"),),
    "openrouter.json": (("openrouter:listing", r"^stopped after \d+ pages",
                         "the account's key listing past its page budget"),
                        ("openrouter:", r"^its key is not among the newest \d+ keys",
                         "a key past the listing's page budget, asked about itself")),
}
#: Finding types kept off the push channel (tools/notify_findings.NOT_PUSHED), and the receipt each is
#: built from: such a receipt must be STRUCTURAL whole, or a day of it would be pushed after all.
NOT_PUSHED_RECEIPTS = {"secret.leak_scan_partial": "leak-scan.json"}


def structural(receipt: str, row: dict) -> bool:
    """True when `row` of `receipt` is a structural degradation: its source starts with a STRUCTURAL
    prefix and its reason (or code) matches that entry's pattern, or it is marked at its source."""
    import re
    if row.get("structural") is True:
        return True
    source = str(row.get("source", ""))
    text = str(row.get("reason") or row.get("code") or "")
    return any(source.startswith(prefix) and re.search(pattern, text)
               for prefix, pattern, _ in STRUCTURAL.get(receipt, ()))


def _persist_key(receipt: str, row: dict) -> tuple[str, str]:
    """(record key, the pair a person reads). The `code` when the row has one, else its reason:
    a reason that carries a count changes with the count and starts its clock again, which errs
    towards saying nothing rather than towards a false 'for a day'."""
    source = str(row.get("source", "?"))
    reason = str(row.get("code") or row.get("reason") or "").strip()
    return f"{receipt}\t{source}\t{reason}", f"{source} — {reason}"


def persistence(current: dict[str, list[dict]], record, now: float
                ) -> tuple[dict, dict[str, list[tuple[str, float]]]]:
    """(the new record, per receipt the degradations seen for more than PERSIST_HOURS with their
    first sighting). Structural rows (`structural`) are neither kept nor reported. `record` is the previous one, `{"first_seen": {key: epoch}}`; anything else is
    read as empty. A degradation absent from `current` is forgotten, so one that comes back starts
    again."""
    import math
    old = record.get("first_seen") if isinstance(record, dict) else None
    old = old if isinstance(old, dict) else {}
    first_seen: dict[str, float] = {}
    persistent: dict[str, list[tuple[str, float]]] = {}
    for receipt, rows in sorted(current.items()):
        for row in rows:
            if not isinstance(row, dict) or structural(receipt, row):
                continue
            key, pair = _persist_key(receipt, row)
            seen = old.get(key)
            seen = float(seen) if (isinstance(seen, (int, float)) and not isinstance(seen, bool)
                                   and math.isfinite(seen) and seen <= now) else now
            first_seen[key] = seen
            if now - seen > PERSIST_HOURS * 3600 and pair not in [p for p, _ in persistent.get(receipt, [])]:
                persistent.setdefault(receipt, []).append((pair, seen))
    return {"schema": 1, "first_seen": first_seen}, persistent


def persistent(now: float | None = None) -> dict[str, list[tuple[str, float]]]:
    """`persistence` over every collector's receipt now, the record kept in the store between runs.
    An unreadable record starts every clock again; a record that cannot be written is said on
    stderr and costs only the next run's memory."""
    import sys
    import time
    now = time.time() if now is None else now
    path = paths.STATE / PERSISTENCE_FILE
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = {}
    record, out = persistence(every_collector(), record, now)
    try:
        import atomic
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic.write_json(path, record)
    except Exception as exc:  # noqa: BLE001 — the findings still build
        print(f"  {PERSISTENCE_FILE} not written: {type(exc).__name__}", file=sys.stderr)
    return out
