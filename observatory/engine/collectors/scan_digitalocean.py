#!/usr/bin/env python3
"""What every DigitalOcean team the estate holds a token for runs, and what it costs.

    scan_digitalocean.py store/raw/digitalocean.json

WHY. A Droplet powered off is billed at its full size until it is destroyed (DigitalOcean's
pricing page, "To end billing, destroy the Droplet"), an App Platform app with no active
deployment serves nothing, and neither reaches a finding rule unless a collector records it.
Per team: the balance and month-to-date usage, Droplets with their monthly price, App Platform
apps with the repositories they deploy from, managed databases, volumes and domains.

WHAT IT DOES NOT DO. It never decides which project a resource belongs to. It records what
DigitalOcean says (an app's repository, a resource's name) and `tools/digitalocean_findings.py`
ties an app to a project only through a repository the registry ALREADY owns, the way
`collectors/heroku_registry.py` does. A name is never evidence.

CREDENTIALS. Tokens come from the DigitalOcean door (`tools/digitalocean.py`): per stashed team,
the team's `observatory-read` token where one is installed, else the team's stashed token, and
the scan says which (`token_kind`). DigitalOcean's public API cannot mint a narrow token, so the
narrow one exists only once somebody minted it in the control panel and installed it; until then
the stash is the only key there is, and the scan says so rather than reading nothing. A value
lives in this process's memory for its requests and is never written: not to the raw file, not
to a log. A database object carries its users' passwords; only named fields are copied out of it.

DEGRADATION. Each failure is NAMED in `degraded` and the rest still lands: no team stashed, a
token DigitalOcean refuses (401: the whole team), a scope the token lacks (403: that resource),
a rate limit (429: that resource, with the wait the provider asked for).
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import atomic  # noqa: E402
import step_budget  # noqa: E402
import digitalocean as door  # noqa: E402

#: Seconds kept for writing the scan when the tick's deadline nears: a team not started by then
#: is named in `degraded` and what was read is written (OBS-40's partial answer).
WRITE_RESERVE = 15
#: (field in the raw file, listing path, the key DigitalOcean answers with).
LISTINGS = (("droplets", "/v2/droplets", "droplets"), ("apps", "/v2/apps", "apps"),
            ("databases", "/v2/databases", "databases"), ("volumes", "/v2/volumes", "volumes"),
            ("domains", "/v2/domains", "domains"))
GITHUB = re.compile(r"github\.com[:/]+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(obj) -> str | None:
    return obj.get("slug") if isinstance(obj, dict) else obj if isinstance(obj, str) else None


def droplet_row(d: dict) -> dict:
    size = d.get("size") if isinstance(d.get("size"), dict) else {}
    return {"id": d.get("id"), "name": d.get("name"), "status": d.get("status"),
            "size_slug": d.get("size_slug") or size.get("slug"), "price_monthly": size.get("price_monthly"),
            "region": _slug(d.get("region")), "created_at": d.get("created_at"),
            "tags": d.get("tags") or [], "volume_ids": d.get("volume_ids") or []}


def repositories(spec: dict) -> list[str]:
    """`owner/name` of every GitHub repository an app's components deploy from."""
    out: list[str] = []
    for kind in ("services", "workers", "jobs", "static_sites", "functions"):
        for comp in spec.get(kind) or []:
            if not isinstance(comp, dict):
                continue
            gh = (comp.get("github") or {}).get("repo")
            url = (comp.get("git") or {}).get("repo_clone_url") or ""
            m = GITHUB.search(url)
            for repo in (gh, m.group(1) if m else None):
                if repo and repo.lower() not in out:
                    out.append(repo.lower())
    return out


def app_row(a: dict) -> dict:
    spec = a.get("spec") or {}
    active = a.get("active_deployment") or {}
    last = a.get("in_progress_deployment") or a.get("pending_deployment") or active
    live = a.get("live_url") or ""
    return {"id": a.get("id"), "name": spec.get("name"), "region": _slug(a.get("region")),
            "serving": bool(active.get("id")), "last_phase": last.get("phase"),
            "live_host": re.sub(r"^https?://", "", live).split("/")[0] or None,
            "repositories": repositories(spec),
            "created_at": a.get("created_at"), "updated_at": a.get("updated_at")}


def database_row(c: dict) -> dict:
    # NAMED FIELDS ONLY: the cluster object carries `connection`, `private_connection` and
    # `users`, each with a password. Nothing else is copied.
    return {"id": c.get("id"), "name": c.get("name"), "engine": c.get("engine"), "version": c.get("version"),
            "size": c.get("size"), "num_nodes": c.get("num_nodes"), "region": c.get("region"),
            "status": c.get("status"), "created_at": c.get("created_at")}


def volume_row(v: dict) -> dict:
    return {"id": v.get("id"), "name": v.get("name"), "size_gigabytes": v.get("size_gigabytes"),
            "region": _slug(v.get("region")), "droplet_ids": v.get("droplet_ids") or [],
            "created_at": v.get("created_at")}


def domain_row(d: dict) -> dict:
    return {"name": d.get("name"), "ttl": d.get("ttl")}


ROWS = {"droplets": droplet_row, "apps": app_row, "databases": database_row,
        "volumes": volume_row, "domains": domain_row}


def read_team(label: str, meta: dict, value: str, kind: str, degraded: list[dict]) -> dict | None:
    """One team's resources, or None when DigitalOcean refuses the token outright."""
    try:
        acct = door.account_of(value)
    except door.ApiError as e:
        degraded.append({"source": f"digitalocean:{label}", "reason": str(e)})
        return None
    except door.DoorError as e:
        degraded.append({"source": f"digitalocean:{label}", "reason": str(e)[:200]})
        return None
    team = {"label": label, "team_uuid": acct.get("team_uuid"), "team_name": acct.get("team_name"),
            "token_kind": kind}
    if meta.get("team_uuid") and acct.get("team_uuid") != meta.get("team_uuid"):
        degraded.append({"source": f"digitalocean:{label}", "reason":
                         "the token answers for another team than the one it was stashed for; not read"})
        return None
    try:
        b = door._api("/v2/customers/my/balance", value)
        team["balance"] = {k: b.get(k) for k in ("month_to_date_usage", "month_to_date_balance",
                                                 "account_balance", "generated_at")}
    except door.DoorError as e:
        team["balance"] = None
        degraded.append({"source": f"digitalocean:{label}/balance", "reason": str(e)[:200]})
    for field, path, key in LISTINGS:
        try:
            rows, bad = [], 0
            for r in door._pages(path, value, key):
                # ONE MALFORMED ROW is that row's problem: it is counted, the others land.
                try:
                    rows.append(ROWS[field](r))
                except Exception:  # noqa: BLE001
                    bad += 1
            team[field] = rows
            if bad:
                degraded.append({"source": f"digitalocean:{label}/{field}",
                                 "reason": f"{bad} row(s) not in the shape this collector reads; skipped"})
        except door.ApiError as e:
            team[field] = None
            degraded.append({"source": f"digitalocean:{label}/{field}", "reason": str(e)})
            if e.status == 401:
                break
        except door.DoorError as e:
            team[field] = None
            degraded.append({"source": f"digitalocean:{label}/{field}", "reason": str(e)[:200]})
    return team


def main(argv: list[str]) -> int:
    import configuration
    if not configuration.enabled("digitalocean"):
        print("digitalocean: not configured (integration disabled)")
        return 0
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    degraded: list[dict] = []
    teams: list[dict] = []
    try:
        door.api_base()
        tokens = door.team_tokens()
    except (door.DoorError, RuntimeError) as e:
        tokens = []
        degraded.append({"source": "digitalocean", "reason": f"the door's stash could not be read: {str(e)[:160]}"})
    for i, (label, meta, value, kind) in enumerate(tokens):
        if step_budget.remaining() < WRITE_RESERVE:
            left = [t[0] for t in tokens[i:]]
            degraded.append({"source": "digitalocean", "reason":
                             f"the step's deadline came first; not read: {', '.join(left)}"})
            break
        try:
            row = read_team(label, meta, value, kind, degraded)
        except Exception as e:  # noqa: BLE001  one team's surprise never costs the others their scan
            row = None
            degraded.append({"source": f"digitalocean:{label}",
                             "reason": f"the team could not be read ({type(e).__name__}); details withheld"})
        if row:
            teams.append(row)
    if not tokens and not degraded:
        degraded.append({"source": "digitalocean", "reason":
                         "no team stashed: `python \"$(project-observatory full-path)/tools/digitalocean.py\" "
                         "stash --from-doctl all`"})
    stamp = now()
    atomic.write_json(argv[1], {"scanned_at": stamp, "scanned_on": stamp[:10], "teams": teams,
                                "degraded": degraded})
    print(f"digitalocean: {len(teams)} team(s): " + ", ".join(
        f"{t['label']} {len(t.get('droplets') or [])} droplet(s) {len(t.get('apps') or [])} app(s)"
        f" usage ${(t.get('balance') or {}).get('month_to_date_usage') or '?'}" for t in teams))
    for d in degraded:
        print(f"  degraded {d['source']}: {d['reason'][:110]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
