#!/usr/bin/env python3
"""Four things about Heroku that belong on the operator's queue, not on a tab.

A TAB IS NOT A WATCHER. A dashboard tab answers a question somebody asked; a
finding reaches somebody who did not ask. That difference is the whole reason these
rules exist: an app whose web dyno has crashed can go on costing money for months
while the one screen that would show it stays unopened.

FOUR RULES, AND WHAT EACH ONE IS NOT.

`heroku.app_down`          — one row per application, because each is a decision
                             about a different thing. Not aggregated: an
                             operator restarts or deletes them one at a time.
`heroku.paying_for_nothing`— resources billed with no dyno to use them. The
                             severity is `warning` rather than `critical`
                             because nothing is BROKEN; money is leaving, which
                             is a decision, not an incident.
`heroku.orphan_app`        — running, and no project in this registry claims it.
                             ONE aggregate row: nineteen of them today, and
                             nineteen slots of a forty-slot board would be the
                             `clone.stale` defect again. The count is
                             the information; which ones is the tab's job.
`heroku.no_local_clone`    — an application whose source is on no disk here. Also
                             aggregated, and `info`, because it is a fact about
                             this machine rather than about the estate: a laptop
                             is not a backup and never was.

NO TIMESTAMP INSIDE A FINDING: every date these rows quote is a fact
the registry already holds — the day of a deploy — never the moment this ran.
"""
from __future__ import annotations
from datetime import date
import pathlib as _pathlib
import sys as _sys
_DASHBOARD = str(_pathlib.Path(__file__).resolve().parents[1] / "dashboard")
if _DASHBOARD not in _sys.path:
    _sys.path.append(_DASHBOARD)  # `finding_types.titled`: a title is a message id
from finding_types import titled  # noqa: E402


def _config_label(name: str) -> str:
    """The workspace-relative name of a configuration file.

    Imported late: these rule modules are loaded by `tools/build_findings.py`,
    which has already resolved the workspace, and importing `paths` at module
    load would resolve it again for every test that imports a rule alone."""
    import paths
    return paths.config_label(name)

#: How old a snapshot may be before the estate rows stop being built from it.
#: `tools/tick.sh` refreshes the scan when it is older than 24h, so anything past
#: two days means the tick has not run or the scan refused — and rows built from
#: it would describe an estate that was, not one that is. Measured cost of not
#: having this: `emit` rewrites the document on EVERY tick from a raw file only a
#: manual run refreshes, so a fixed application would have kept raising a
#: critical until somebody re-scanned, and one that broke would have raised
#: nothing (audit 2026-09-09).
STALE_AFTER_DAYS = 2

#: Listed by name up to this many; beyond it the row says how many more. Same
#: number as the other aggregates on the board, so a reader learns one rule.
LISTED = 6


def _listed(names: list[str]) -> str:
    shown = sorted(names)[:LISTED]
    rest = len(names) - len(shown)
    return ", ".join(shown) + (f" and {rest} more" if rest > 0 else "")


def app_down(apps: list[dict]) -> list[dict]:
    """One row per application that is scaled and not serving."""
    out = []
    for a in apps:
        if a["state"] not in ("down", "suspended") and not a.get("crashed"):
            continue
        suspended = a["state"] == "suspended"
        what = ("Heroku has SUSPENDED this application"
                if suspended else
                f"its dyno(s) {', '.join(a['crashed'])} are crashed"
                if a.get("crashed") else
                "it is scaled and nothing came up")
        # The last deploy is the operator's first question — a crash the day
        # after a deploy is a bad release, a crash two years after one is a
        # dependency or a platform change underneath it.
        since = (f"; the last code deploy was {a['last_deploy_on']}"
                 if a.get("last_deploy_on") else
                 "; no code was ever deployed to it")
        out.append({
            "type": "heroku.app_down",
            "subject": a["id"],
            "severity": "critical" if suspended else "warning",
            **titled("{app} is paid for and not serving", app=a["name"]),
            "detail": (f"{what}{since}. It bills ${a['monthly_cost']:.0f} a month "
                       f"and belongs to "
                       + (f"{a['project'].split(':', 1)[1]}" if a.get("project")
                          else "no project this registry holds") + "."),
            "action": ("ask Heroku support why it was suspended, or delete the "
                       "application and its add-ons"
                       if suspended else
                       f"heroku logs -a {a['name']} --tail, then restart it or "
                       f"scale it to zero and stop paying"),
        })
    return out


def paying_for_nothing(apps: list[dict]) -> list[dict]:
    """Add-ons billing with no dyno to use them — one row each, they are separable."""
    out = []
    for a in apps:
        if a["state"] != "resources-only":
            continue
        plans = ", ".join(x["plan"] for x in a.get("addons", []))
        never = a.get("never_deployed")
        out.append({
            "type": "heroku.paying_for_nothing",
            "subject": a["id"],
            "severity": "warning",
            **titled("{app} pays for resources it cannot use", app=a["name"]),
            "detail": (f"No dyno is scaled, and {plans} still bills "
                       f"${a['monthly_cost']:.0f} a month."
                       + (" Code was never deployed to this application at all — "
                          "its whole history is the release that provisioned the "
                          "database." if never else "")),
            "action": ("delete the add-on, or scale a dyno if this was meant to be "
                       "running"),
        })
    return out


def orphan_app(apps: list[dict]) -> list[dict]:
    """Running, and nothing in the registry claims it. ONE row, because the count is the point."""
    orphans = [a for a in apps
               if not a.get("project") and a["state"] in ("running", "down", "suspended")]
    if not orphans:
        return []
    cost = sum(a["monthly_cost"] for a in orphans)
    # BUILT, THEN RETURNED AS ONE. The aggregate is the whole point of this rule
    # — nineteen rows would be the `clone.stale` defect — and a
    # single `return [row]` is what lets the mutation instrument express "one
    # row per orphan" as one line, so the guard can be watched catching it.
    row = {
        "type": "heroku.orphan_app",
        "subject": "estate:heroku-orphans",
        "severity": "warning",
        **titled("{n} running applications belong to no project here", n=len(orphans)),
        "detail": (f"Heroku is running them and this registry cannot say what they "
                   f"are for: {_listed([a['name'] for a in orphans])}. Together "
                   f"${cost:.0f} a month. A link is only made where something was "
                   f"measured — Heroku's own Deploy tab, or a checkout carrying the "
                   f"application's git remote — so an orphan means neither exists, "
                   f"not that nobody looked."),
        "action": ("open the Heroku page and filter to “no project”; where you know "
                   f"the answer, add it to {_config_label('heroku_links.json')} with the "
                   "evidence that proves it"),
    }
    return [row]


def no_local_clone(apps: list[dict]) -> list[dict]:
    """Applications whose source is on no disk here. One row: this is a fact about the machine."""
    absent = [a for a in apps if not a.get("local_folders")]
    if not absent:
        return []
    teams: dict[str, int] = {}
    for a in absent:
        teams[a["team"]] = teams.get(a["team"], 0) + 1
    whole = sorted(t for t, n in teams.items()
                   if n == sum(1 for a in apps if a["team"] == t))
    return [{
        "type": "heroku.no_local_clone",
        "subject": "estate:heroku-unclonned",
        "severity": "info",
        **titled("{n} of {total} Heroku applications have no checkout here", n=len(absent), total=len(apps)),
        "detail": ("Nothing on this machine can read or rebuild them; the source is "
                   "wherever it was last pushed from."
                   + (f" Entire team(s) absent: {', '.join(whole)}." if whole else "")
                   + " A laptop is not a backup, so this is a fact about reach rather "
                     "than about risk to the code."),
        "action": ("clone what you expect to work on; for the rest this row is the "
                   "record that they live elsewhere"),
    }]


def snapshot_stale(doc: dict, age: int) -> list[dict]:
    """One row about the EVIDENCE, in place of every row about the estate.

    Silence would be worse than a wrong row: the estate rows disappearing reads
    as "nothing is wrong on Heroku", which is a claim this run cannot make. So
    the board says what it does not know and how to fix it, and says nothing
    about applications until it can.
    """
    return [{
        "type": "heroku.snapshot_stale",
        "subject": "estate:heroku-snapshot",
        "severity": "warning",
        **titled("the Heroku snapshot is {n} days old, so nothing is being said about it", n=age),
        "detail": (f"`registry/heroku-apps.json` was measured on {doc.get('scanned_on')} "
                   f"and every rule about applications is withheld past "
                   f"{STALE_AFTER_DAYS} days. A crashed dyno reported from a stale "
                   f"scan may have been restarted, and one that crashed since is "
                   f"invisible — both are worse than an admitted gap."),
        "action": "./observatory.py heroku, then ./observatory.py emit findings",
    }]


def findings(doc: dict | None, today: date | None = None) -> list[dict]:
    """Every Heroku rule, over the registry document. Empty when it was never scanned.

    ABSENT IS NOT CLEAN, and this is the one place that distinction is silent on
    purpose: a fresh clone has no `heroku-apps.json`, and inventing a finding
    that says so would put a row about the OBSERVATORY on a board about the
    ESTATE. The dashboard's own tab says "never scanned" where a reader is
    already asking about Heroku.
    """
    if not doc:
        return []
    apps = doc.get("apps") or []
    if not apps:
        return []
    # THE AGE OF THE EVIDENCE DECIDES WHETHER THERE IS ANYTHING TO SAY.
    scanned = doc.get("scanned_on")
    if scanned:
        try:
            age = (today or date.today()) - date.fromisoformat(scanned)
        except ValueError:
            age = None
        if age is not None and age.days > STALE_AFTER_DAYS:
            return snapshot_stale(doc, age.days)
    rows = app_down(apps) + paying_for_nothing(apps) + orphan_app(apps) + no_local_clone(apps)
    # EVERY ROW NAMES THE DAY IT WAS MEASURED. Without it a reader cannot tell a
    # crash that happened an hour ago from one the scan saw two days ago, and the
    # remedy for the two is not the same.
    if scanned:
        for row in rows:
            row["detail"] = row["detail"].rstrip() + f" Measured {scanned}."
    return rows
