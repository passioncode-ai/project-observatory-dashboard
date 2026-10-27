#!/usr/bin/env python3
"""DigitalOcean and Cloudflare as registry records, in the shape the Heroku page set (OBS-235, OBS-271).

A transformer, not a collector: it takes a raw scan and the registry and returns one
document per provider, `registry/digitalocean-account.json` and
`registry/cloudflare-account.json`. Nothing runs at import.

ONE SHAPE FOR THREE PROVIDERS (PERSON-SURFACES.md, Infrastructure; IS-18). Every row
carries the columns the pages show for Heroku: the resource (`kind`, `name`), its
`project`, its `account`, its `state`, its `monthly_cost` and the `subjects` a finding
names it by. `state` is one word of a closed set (`STATES`), and the provider's own word
stays beside it in `provider_state`, so the page can sort and a reader can still see what
the provider said.

COST IS WHAT THE PROVIDER STATES, OR NOTHING. A Droplet's listing carries its monthly
price and a zone's plan carries its price; nothing else these APIs list does (an App
Platform app, a managed database, a volume, a Worker, a bucket). Those rows say
`monthly_cost: null` with `cost_note`, never a zero and never an estimate. A team's
month-to-date usage, which DigitalOcean's balance endpoint does state, sits on the team.

A LINK IS MEASURED OR ABSENT (rule 2: never infer from a name). An App Platform app or a
Pages project is tied to a project only through a repository it deploys from that the
registry ALREADY ties to one; a zone carries the project `registry/cloudflare-zones.json`
measured for it. Every link names its rule; a row no rule reaches says why.
"""
from __future__ import annotations

#: The one vocabulary of states every infrastructure page shows.
#:   running   serving or up, as the provider reports it
#:   off       stopped by its owner and still billed (a powered-off Droplet)
#:   down      should serve and does not (an app with no active deployment, a failed deploy)
#:   pending   on its way (a deployment building, a zone awaiting its name servers)
#:   idle      exists and does nothing (a detached volume, a paused zone)
#:   listed    exists; the API states no health for it (a Worker, a bucket, a D1 database)
STATES = ("running", "off", "down", "pending", "idle", "listed")
#: The rules a link may carry, per document.
RULES = {"digitalocean": {"digitalocean-app-repo"},
         "cloudflare": {"pages-source-repo", "zone-site"}}
SOURCES = {"digitalocean": "SRC-0021", "cloudflare": "SRC-0022"}
#: App Platform's deployment phases still on their way.
IN_PROGRESS = {"PENDING", "PENDING_BUILD", "BUILDING", "PENDING_DEPLOY", "DEPLOYING"}
NO_PRICE = "the provider's listing states no price for this resource"
UNLINKED = {
    "no-repo": "the provider names no repository it deploys from",
    "external-repo": "it deploys from a repository this registry does not tie to a project",
    "not-linkable": "the provider records nothing that ties it to a repository or a site",
    "zone-unlinked": "no project's site is on this zone",
}


#: The hosts a registry repository is on GitHub by. Both providers name a GitHub repository by
#: `owner/name` alone, and the same pair on another host is another repository.
GITHUB_HOSTS = {"github", "github.com"}


def repo_index(repos: list[dict], relations: list[dict]) -> dict[str, str]:
    """`owner/name` (lowercased) -> project id, through the registry's `implemented_by` edges,
    for the registry's GITHUB repositories only (DigitalOcean's and Pages' sources are GitHub's)."""
    by_id = {r.get("id"): r for r in repos if str(r.get("host") or "").lower() in GITHUB_HOSTS}
    out: dict[str, str] = {}
    for rel in relations:
        if rel.get("type") != "implemented_by":
            continue
        repo = by_id.get(rel.get("to"))
        if repo and repo.get("name_with_owner"):
            out.setdefault(repo["name_with_owner"].lower(), rel["from"])
    return out


def _row(rid: str, kind: str, name, account: str, state: str, provider_state, cost=None,
         cost_note: str | None = NO_PRICE, subjects: list[str] | None = None, **extra) -> dict:
    row = {"id": rid, "kind": kind, "name": name if name is not None else rid.rsplit(":", 1)[-1],
           "account": account, "state": state, "provider_state": provider_state,
           "monthly_cost": cost, "cost_note": None if cost is not None else cost_note,
           "subjects": subjects or [rid], "project": None, "link_rule": None}
    row.update(extra)
    return row


def _link(row: dict, repositories: list[str] | None, index: dict[str, str], rule: str) -> None:
    hit = next((index[r] for r in repositories or [] if r in index), None)
    if hit:
        row["project"], row["link_rule"] = hit, rule
    elif repositories:
        row["unlinked_kind"] = "external-repo"
    else:
        row["unlinked_kind"] = "no-repo"
    if not row["project"]:
        row["unlinked_reason"] = UNLINKED[row["unlinked_kind"]]


def _unlinkable(row: dict, kind: str = "not-linkable") -> dict:
    row["unlinked_kind"], row["unlinked_reason"] = kind, UNLINKED[kind]
    return row


# ─────────────────────────── DigitalOcean ────────────────────────────────────

def droplet_state(status) -> str:
    return {"active": "running", "off": "off", "new": "pending", "archive": "idle"}.get(status, "listed")


def app_state(app: dict) -> str:
    if app.get("serving"):
        return "running"
    return "pending" if app.get("last_phase") in IN_PROGRESS else "down"


def digitalocean_rows(scan: dict, index: dict[str, str]) -> tuple[list[dict], list[dict]]:
    rows, teams = [], []
    for t in scan.get("teams") or []:
        account = t.get("team_name") or t.get("label")
        bal = t.get("balance") or {}
        teams.append({"id": f"digitalocean:team:{t.get('team_uuid') or t.get('label')}",
                      "label": t.get("label"), "name": account, "token_kind": t.get("token_kind"),
                      "month_to_date_usage": _number(bal.get("month_to_date_usage")),
                      "unread": [k for k in ("droplets", "apps", "databases", "volumes") if t.get(k) is None]})
        for d in t.get("droplets") or []:
            price = d.get("price_monthly")
            rows.append(_unlinkable(_row(
                f"digitalocean:droplet:{d.get('id')}", "droplet", d.get("name"), account,
                droplet_state(d.get("status")), d.get("status"),
                cost=float(price) if isinstance(price, (int, float)) else None,
                region=d.get("region"), size=d.get("size_slug"), created_on=(d.get("created_at") or "")[:10] or None)))
        for a in t.get("apps") or []:
            row = _row(f"digitalocean:app:{a.get('id')}", "app", a.get("name"), account, app_state(a),
                       a.get("last_phase"), region=a.get("region"), host=a.get("live_host"),
                       repositories=a.get("repositories") or [],
                       updated_on=(a.get("updated_at") or "")[:10] or None)
            _link(row, a.get("repositories"), index, "digitalocean-app-repo")
            rows.append(row)
        for c in t.get("databases") or []:
            rows.append(_unlinkable(_row(
                f"digitalocean:database:{c.get('id')}", "database", c.get("name"), account,
                "running" if c.get("status") == "online" else "pending" if c.get("status") in ("creating", "resizing", "migrating") else "down",
                c.get("status"), region=c.get("region"), size=c.get("size"),
                engine=" ".join(x for x in (c.get("engine"), c.get("version")) if x) or None)))
        for v in t.get("volumes") or []:
            rows.append(_unlinkable(_row(
                f"digitalocean:volume:{v.get('id')}", "volume", v.get("name"), account,
                "running" if v.get("droplet_ids") else "idle",
                "attached" if v.get("droplet_ids") else "detached", region=v.get("region"),
                size_gb=v.get("size_gigabytes"))))
    return rows, teams


def _number(value):
    try:
        return round(float(value), 2) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ─────────────────────────── Cloudflare ──────────────────────────────────────

def pages_state(p: dict) -> str:
    status = p.get("last_deploy_status")
    if status == "success":
        return "running"
    if status in ("failure", "canceled"):
        return "down"
    return "pending" if status in ("active", "idle", "queued") else "listed"


def zone_state(z: dict) -> str:
    if z.get("paused"):
        return "idle"
    status = z.get("status")
    if status == "active":
        return "running"
    return "pending" if status in ("pending", "initializing") else "down"


def cloudflare_rows(scan: dict, index: dict[str, str],
                    zone_projects: dict[tuple, str]) -> tuple[list[dict], list[dict]]:
    rows, accounts = [], []
    for a in scan.get("accounts") or []:
        aid, account = a.get("account_id"), a.get("account_name") or a.get("label")
        accounts.append({"id": f"cloudflare:account:{aid}", "label": a.get("label"), "name": account,
                         "token_expires_at": a.get("token_expires_at"),
                         "unread": [k for k in ("workers", "pages", "r2", "d1", "zones") if a.get(k) is None]})
        for w in a.get("workers") or []:
            rows.append(_unlinkable(_row(f"cloudflare:worker:{aid}/{w.get('name')}", "worker", w.get("name"),
                                         account, "listed", None, modified_on=w.get("modified_on"))))
        for p in a.get("pages") or []:
            row = _row(f"cloudflare:pages:{aid}/{p.get('name')}", "pages", p.get("name"), account,
                       pages_state(p), p.get("last_deploy_status"), host=p.get("subdomain"),
                       domains=p.get("domains") or [], last_deploy_on=p.get("last_deploy_on"))
            # Only a GitHub source names a GitHub repository; a GitLab `owner/name` is another one.
            github = p.get("repository") if p.get("source_type") == "github" else None
            _link(row, [github] if github else [], index, "pages-source-repo")
            rows.append(row)
        for b in a.get("r2") or []:
            rows.append(_unlinkable(_row(f"cloudflare:r2:{aid}/{b.get('name')}", "r2", b.get("name"), account,
                                         "listed", None, location=b.get("location"), created_on=b.get("created_on"))))
        for d in a.get("d1") or []:
            rows.append(_unlinkable(_row(f"cloudflare:d1:{d.get('uuid')}", "d1", d.get("name"), account,
                                         "listed", None, size_bytes=d.get("file_size"), tables=d.get("num_tables"))))
        for z in a.get("zones") or []:
            monthly = z.get("plan_price") if z.get("plan_frequency") == "monthly" and z.get("plan_currency") == "USD" else None
            free = z.get("plan_price") == 0
            row = _row(f"cloudflare:zone:{z.get('zone_id')}", "zone", z.get("name"), account, zone_state(z),
                       "paused" if z.get("paused") else z.get("status"),
                       cost=float(monthly) if isinstance(monthly, (int, float)) else 0.0 if free else None,
                       cost_note="the plan states no monthly USD price",
                       subjects=[f"cloudflare:zone:{z.get('zone_id')}", f"domain:{z.get('name')}", f"zone:{z.get('name')}"],
                       plan=z.get("plan"), dns_records=z.get("dns_records"),
                       certificate_expires_on=z.get("certificate_expires_on"),
                       certificate_status=z.get("certificate_status"))
            pid = zone_projects.get((aid, z.get("name"))) or zone_projects.get((None, z.get("name")))
            if pid:
                row["project"], row["link_rule"] = pid, "zone-site"
            else:
                _unlinkable(row, "zone-unlinked")
            rows.append(row)
    return rows, accounts


def zone_projects(zones: list[dict]) -> dict[tuple, str]:
    """(account id, zone name) -> project, from `registry/cloudflare-zones.json`'s rows;
    also keyed by name alone when only one account holds the name."""
    out: dict[tuple, str] = {}
    names: dict[str, int] = {}
    for z in zones:
        names[z.get("name")] = names.get(z.get("name"), 0) + 1
    for z in zones:
        if z.get("project"):
            out[(z.get("account_id"), z.get("name"))] = z["project"]
            if names[z.get("name")] == 1:
                out[(None, z.get("name"))] = z["project"]
    return out


# ─────────────────────────── the documents ───────────────────────────────────

NOTES = {
    "digitalocean": ("What every DigitalOcean team the estate holds a token for runs, measured by "
                     "collectors/scan_digitalocean.py. `monthly_cost` is the price DigitalOcean's listing "
                     "states (a Droplet's size), or null with `cost_note`; a team's `month_to_date_usage` is "
                     "its balance endpoint's figure. An app's `project` is measured through a repository the "
                     "registry already holds, with its `link_rule`."),
    "cloudflare": ("What every Cloudflare account the estate holds an inventory reader for runs, measured by "
                   "collectors/scan_cloudflare_account.py: Workers, Pages projects, R2 buckets, D1 databases "
                   "and zones with their plan, DNS record count and certificate expiry. `monthly_cost` is a "
                   "zone plan's stated monthly price, or null with `cost_note`. A Pages project's `project` "
                   "is measured through its source repository; a zone's comes from cloudflare-zones.json."),
}


def document(provider: str, scan: dict, rows: list[dict], accounts: list[dict], obs_date: str) -> dict:
    known = [r["monthly_cost"] for r in rows if isinstance(r.get("monthly_cost"), (int, float))]
    return {
        "schema_version": 1,
        "updated_on": obs_date,
        "note": NOTES[provider],
        "source_refs": [SOURCES[provider]],
        "scanned_on": scan.get("scanned_on") or (scan.get("scanned_at") or "")[:10] or None,
        "accounts": accounts,
        "totals": {
            "resources": len(rows),
            "linked_to_a_project": sum(1 for r in rows if r.get("project")),
            "by_kind": {k: sum(1 for r in rows if r["kind"] == k) for k in sorted({r["kind"] for r in rows})},
            "by_state": {s: sum(1 for r in rows if r["state"] == s) for s in STATES},
            # Null when nothing states a price: a sum of nothing is not "$0". When some rows
            # state none, the sum is a floor, and `monthly_cost_partial` says so.
            "monthly_cost": round(sum(known), 2) if known else None,
            "monthly_cost_partial": bool(known) and any(r.get("monthly_cost") is None for r in rows),
            "cost_unknown": sum(1 for r in rows if r.get("monthly_cost") is None),
            "month_to_date_usage": (round(sum(a["month_to_date_usage"] for a in accounts
                                              if isinstance(a.get("month_to_date_usage"), (int, float))), 2)
                                    if any(isinstance(a.get("month_to_date_usage"), (int, float)) for a in accounts)
                                    else None),
        },
        "resources": sorted(rows, key=lambda r: (r["account"] or "", r["kind"], str(r["name"]).lower())),
        "degraded": scan.get("degraded") or [],
    }


def digitalocean_document(scan: dict, repos: list[dict], relations: list[dict], obs_date: str) -> dict:
    rows, teams = digitalocean_rows(scan, repo_index(repos, relations))
    return document("digitalocean", scan, rows, teams, obs_date)


def cloudflare_document(scan: dict, zones: list[dict], repos: list[dict], relations: list[dict],
                        obs_date: str) -> dict:
    rows, accounts = cloudflare_rows(scan, repo_index(repos, relations), zone_projects(zones))
    return document("cloudflare", scan, rows, accounts, obs_date)


def problems(provider: str, doc: dict, project_ids: set[str], source_ids: set[str]) -> list[str]:
    """What the validator refuses in one of these documents, as printable strings."""
    out: list[str] = []
    name = f"{provider}-account.json"
    if set(doc.get("source_refs") or []) - source_ids:
        out.append(f"{name} cites an unresolved source")
    rows = doc.get("resources") or []
    seen: set[str] = set()
    for r in rows:
        rid = r.get("id") or ""
        if rid in seen:
            out.append(f"{name}: duplicate id {rid}")
        seen.add(rid)
        if not rid.startswith(provider + ":"):
            out.append(f"{name}: id is not namespaced: {rid}")
        if r.get("state") not in STATES:
            out.append(f"{name}: unknown state: {rid} {r.get('state')}")
        if bool(r.get("project")) != bool(r.get("link_rule")):
            out.append(f"{name}: link without its rule, or rule without a link: {rid}")
        if r.get("link_rule") and r["link_rule"] not in RULES[provider]:
            out.append(f"{name}: unknown link rule: {rid} {r['link_rule']}")
        if r.get("project") and r["project"] not in project_ids:
            out.append(f"{name}: names a project that does not exist: {rid} -> {r['project']}")
        if not r.get("project") and not r.get("unlinked_reason"):
            out.append(f"{name}: unlinked resource says nothing about why: {rid}")
        if r.get("monthly_cost") is None and not r.get("cost_note"):
            out.append(f"{name}: a cost nobody stated carries no note: {rid}")
    t = doc.get("totals") or {}
    if t.get("resources") != len(rows):
        out.append(f"{name}: totals.resources disagrees with the list it summarises")
    if t.get("linked_to_a_project") != sum(1 for r in rows if r.get("project")):
        out.append(f"{name}: totals.linked_to_a_project disagrees with the rows")
    return out
