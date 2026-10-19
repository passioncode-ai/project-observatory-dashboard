#!/usr/bin/env python3
"""What DigitalOcean puts on the operator's queue (OBS-155): money for nothing, and an app down.

Read from `store/raw/digitalocean.json`, which `collectors/scan_digitalocean.py` writes.

`digitalocean.droplet_off_billed`: one row per Droplet whose status is `off`. DigitalOcean
                                    bills a powered-off Droplet at its full size until it is
                                    destroyed (its pricing page: "To end billing, destroy the
                                    Droplet"), so `off` is a cost decision, not a saving.
                                    `warning`: nothing is broken; money is leaving.
`digitalocean.app_down`:           one row per App Platform app with no active deployment:
                                    it serves nothing. Its project is named only when the
                                    registry already ties a repository the app deploys from to
                                    one; a name is never evidence.
`digitalocean.snapshot_stale`:     the scan is older than two days: one row about the
                                    evidence in place of every row about the estate, because a
                                    Droplet reported off from a stale scan may be gone.

ABSENT IS NOT CLEAN, and the board knows it: no scan means no rows rather than a row saying
nothing is wrong. A team the scan could not read is in the scan's own `degraded`, which the
collector-degraded rule already carries.
"""
from __future__ import annotations

from datetime import date
import pathlib as _pathlib
import sys as _sys

_DASHBOARD = str(_pathlib.Path(__file__).resolve().parents[1] / "dashboard")
if _DASHBOARD not in _sys.path:
    _sys.path.append(_DASHBOARD)  # `finding_types.titled`: a title is a message id
from finding_types import titled  # noqa: E402

#: Deployment phases of a deployment still on its way (App Platform's phase enum).
IN_PROGRESS = frozenset({"PENDING", "PENDING_BUILD", "BUILDING", "PENDING_DEPLOY", "DEPLOYING"})

#: Days past which the scan is stale; the tick refreshes it daily.
STALE_AFTER_DAYS = 2


def _repo_index(repos: list[dict], relations: list[dict]) -> dict[str, str]:
    """`owner/name` (lowercased) -> project id, through the registry's own `implemented_by` edges."""
    by_id = {r.get("id"): r for r in repos}
    out: dict[str, str] = {}
    for rel in relations:
        if rel.get("type") != "implemented_by":
            continue
        repo = by_id.get(rel.get("to"))
        if repo and repo.get("name_with_owner"):
            out.setdefault(repo["name_with_owner"].lower(), rel["from"])
    return out


def droplet_off_billed(teams: list[dict]) -> list[dict]:
    out = []
    for t in teams:
        for d in t.get("droplets") or []:
            if d.get("status") != "off":
                continue
            price = d.get("price_monthly")
            cost = f"${price:.0f} a month" if isinstance(price, (int, float)) else "its size's full price"
            out.append({
                "type": "digitalocean.droplet_off_billed",
                "subject": f"digitalocean:droplet:{d.get('id')}",
                "severity": "warning",
                **titled("{droplet} is powered off and still billed", droplet=d.get("name") or d.get("id")),
                "detail": (f"Droplet {d.get('name')} ({d.get('size_slug')}) in team {t.get('team_name') or t.get('label')} "
                           f"is off, and DigitalOcean bills a powered-off Droplet at {cost} until it is destroyed."),
                "action": ("snapshot it if its disk matters, then destroy it; or power it on if it was meant to run"),
            })
    return out


def app_down(teams: list[dict], index: dict[str, str]) -> list[dict]:
    out = []
    for t in teams:
        for a in t.get("apps") or []:
            # A first deployment still on its way is not an app down: it has never served yet.
            if a.get("serving") or a.get("last_phase") in IN_PROGRESS:
                continue
            project = next((index[r] for r in a.get("repositories") or [] if r in index), None)
            phase = a.get("last_phase")
            out.append({
                "type": "digitalocean.app_down",
                "subject": f"digitalocean:app:{a.get('id')}",
                "severity": "warning",
                **titled("{app} has no active deployment", app=a.get("name") or a.get("id")),
                "detail": (f"App Platform app {a.get('name')} in team {t.get('team_name') or t.get('label')} serves nothing"
                           + (f"; its latest deployment is {phase}" if phase else "")
                           + ". It belongs to "
                           + (project.split(":", 1)[-1] if project else "no project this registry holds")
                           + "."),
                "action": f"doctl apps list-deployments {a.get('id')}, then redeploy it or delete the app",
            })
    return out


def snapshot_stale(doc: dict, age: int) -> list[dict]:
    return [{
        "type": "digitalocean.snapshot_stale",
        "subject": "estate:digitalocean-snapshot",
        "severity": "warning",
        **titled("the DigitalOcean snapshot is {n} days old, so nothing is being said about it", n=age),
        "detail": (f"`store/raw/digitalocean.json` was measured on {doc.get('scanned_on')}, and every rule "
                   f"about Droplets and apps is withheld past {STALE_AFTER_DAYS} days."),
        "action": "`project-observatory full scan-digitalocean`, then `project-observatory full findings`",
    }]


def findings(doc: dict | None, repos: list[dict], relations: list[dict],
             today: date | None = None) -> list[dict]:
    """Every DigitalOcean rule over the raw scan. Empty when it was never scanned."""
    if not doc:
        return []
    teams = doc.get("teams") or []
    scanned = doc.get("scanned_on")
    if scanned:
        try:
            age = ((today or date.today()) - date.fromisoformat(scanned)).days
        except ValueError:
            age = None
        if age is not None and age > STALE_AFTER_DAYS:
            return snapshot_stale(doc, age)
    rows = droplet_off_billed(teams) + app_down(teams, _repo_index(repos, relations))
    if scanned:
        for row in rows:
            row["detail"] = row["detail"].rstrip() + f" Measured {scanned}."
    return rows
