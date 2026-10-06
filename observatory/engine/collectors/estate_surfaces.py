#!/usr/bin/env python3
"""Two projections about what the estate OWNS and how it GROUPS: zones and products.

`registry/cloudflare-zones.json`: every Cloudflare zone the estate holds a
token for, joined to the domain registry (registered where?), to the projects
(whose surface?) and to the operator's boundary file (ruled outside? pending?).
It is the answer to "which domains do we have and what is their status" that a
hand-transcribed `domains.json` could only give for the domains it was told about.

`registry/products.json`: several projects, folders and domains that are ONE
THING to a user: a site, its admin, its app, its wiki. Curated rows come from
`collectors/products.json` and are facts; SUGGESTED rows are derived here from
projects that share a registrable domain and are labelled so, because an
inference that looks like a decision gets acted on as one.

Both are read by the dashboard and mirrored into the wiki like every other
projection; neither holds a value of any kind.
"""
from __future__ import annotations
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins"))
import paths                                                                    

CURATED_PRODUCTS = paths.config_file('products.json')
BOUNDARY = paths.config_file('host_boundary.json')
SRC = ["SRC-0016"]

#: How strongly each kind of site evidence speaks, lowest number wins. The
#: operator's word beats anything measured; among measurements, a config file in
#: a LOCAL checkout beats a GitHub homepage field, which beats Bitbucket, which
#: beats a mention in a wiki note — the operator's priority order (2026-09-13):
#: "first what is on this machine, then GitHub, then Bitbucket".
EVIDENCE_RANK = (
    ("operator-claim", 0),
    ("repo-config:", 1),
    # A zone's DNS pointing at a Heroku app the registry has already tied to a
    # project: two measurements chained, neither of them a name match. Ranked
    # with GitHub — measured outside this machine, stronger than a wiki note.
    ("dns:", 2),
    ("github:", 2),
    ("bitbucket:", 3),
    ("vault-overview:", 4),
)


def evidence_rank(ev: list[str]) -> int:
    best = 9
    for e in ev or []:
        for prefix, rank in EVIDENCE_RANK:
            if e.startswith(prefix):
                best = min(best, rank)
    return best


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "unnamed"


def registrable(host: str, owned: set[str]) -> str | None:
    parts = host.split(".")
    for i in range(len(parts) - 1):
        cand = ".".join(parts[i:])
        if cand in owned:
            return cand
    return None


# ─────────────────────────── host -> project, ranked ─────────────────────────

def host_table(projects: list[dict]) -> dict[str, tuple[str, int]]:
    """host -> (project id, rank of the evidence that placed it there).

    Where two projects claim one host the LOWER rank wins; a tie keeps the
    first in project order and the zone row says both.
    """
    table: dict[str, tuple[str, int]] = {}
    for p in projects:
        for s in p.get("sites") or []:
            h = (s.get("host") or "").lower()
            if not h:
                continue
            r = evidence_rank(s.get("evidence") or [])
            if h not in table or r < table[h][1]:
                table[h] = (p["id"], r)
    return table


def resolve(host: str, table: dict[str, tuple[str, int]]) -> str | None:
    h = (host or "").lower().strip(".")
    if h in table:
        return table[h][0]
    if h.startswith("www.") and h[4:] in table:
        return table[h[4:]][0]
    parts = h.split(".")
    if len(parts) > 2:
        apex = ".".join(parts[-2:])
        if apex in table:
            return table[apex][0]
    return None


#: Where a DNS target points, by suffix. The handle is what a later join can
#: use: a Heroku hostname, a Pages project, a Vercel alias.
TARGET_SUFFIXES = (
    (".herokudns.com", "heroku"), (".herokuapp.com", "heroku"),
    (".pages.dev", "cloudflare-pages"), (".workers.dev", "cloudflare-workers"),
    (".vercel.app", "vercel"), (".vercel-dns.com", "vercel"), ("cname.vercel-dns.com", "vercel"),
    (".netlify.app", "netlify"), (".netlify.com", "netlify"),
    (".github.io", "github-pages"), (".onrender.com", "render"), (".fly.dev", "fly"),
    (".amazonaws.com", "aws"), (".cloudfront.net", "aws"), (".azurewebsites.net", "azure"),
    (".ghost.io", "ghost"), (".webflow.io", "webflow"), (".framer.app", "framer"),
    (".myshopify.com", "shopify"), (".tilda.ws", "tilda"), (".wixdns.net", "wix"),
)


def classify_target(record: dict) -> tuple[str, str] | None:
    """(provider, handle) for an A/AAAA/CNAME record, or None for an IP/unknown."""
    if record.get("type") == "CNAME":
        c = (record.get("content") or "").lower().rstrip(".")
        for suf, prov in TARGET_SUFFIXES:
            if c.endswith(suf) or c == suf.lstrip("."):
                return prov, c
        return ("cname", c) if c else None
    if record.get("type") in ("A", "AAAA"):
        return ("ip", record.get("content") or "")
    return None


def zone_targets(z: dict) -> list[dict]:
    """The apex and www records, classified — the reader's question is
    'what serves this domain', not the full record set."""
    out = []
    for r in z.get("records") or []:
        if "error" in r:
            continue
        name = (r.get("name") or "").lower()
        if name not in (z["name"], "www." + z["name"]):
            continue
        c = classify_target(r)
        if c:
            out.append({"name": name, "type": r["type"], "provider": c[0], "handle": c[1],
                        "proxied": r.get("proxied", False)})
    return out


def heroku_site_hints(scan_apps: list[dict], linked_apps: list[dict]) -> dict[str, dict[str, str]]:
    """project id -> {host: evidence} from Heroku's own domain lists.

    A zone's CNAME names a Heroku DNS target and never the app's name; Heroku's
    list of the hostnames it accepts for an app is the other end of that CNAME,
    and `heroku-apps.json` already ties the app to a project by a named rule.
    Chained, no name is ever compared to a name.
    """
    proj_of = {a["name"]: a["project"] for a in linked_apps if a.get("project")}
    out: dict[str, dict[str, str]] = {}
    for a in scan_apps:
        pid = proj_of.get(a.get("name"))
        if not pid:
            continue
        for h in a.get("domains") or []:
            out.setdefault(pid, {})[h.lower()] = f"dns:heroku:{a['name']}"
    return out


def _boundary() -> dict:
    try:
        return json.loads(BOUNDARY.read_text(encoding="utf-8")).get("hosts", {})
    except (OSError, ValueError):
        return {}


# ─────────────────────────── zones ────────────────────────────────────────────

def zone_rows(scan: dict, domains: list[dict], projects: list[dict],
              products: list[dict] | None = None) -> list[dict]:
    owned = {d["name"] for d in domains}
    by_name = {d["name"]: d for d in domains}
    table = host_table(projects)
    boundary = _boundary()
    # A CURATED product's domains: one product can claim example.* as one thing
    # while no single repository is its site — the domain has an owner at the
    # product level, and saying "unclassified" there would be false.
    prod_of: dict[str, str] = {}
    prod_name: dict[str, str] = {}
    for pr in products or []:
        if pr.get("kind") != "curated":
            continue
        for dom in pr.get("domains") or []:
            prod_of[dom.lower()] = pr["id"]; prod_name[pr["id"]] = pr["name"]
    out = []
    # ONE ZONE NAME CAN LIVE IN TWO ACCOUNTS (a zone moved, or a duplicate added
    # by mistake). The id was the name alone, so the second row collided and the
    # validator rejected the registry. A name held by one account keeps
    # `zone:<name>`; a name held by several is qualified by its account.
    _accounts_of: dict[str, set] = {}
    for z in scan.get("zones", []):
        _accounts_of.setdefault(z["name"], set()).add(z.get("account_id") or z.get("account_label"))
    for z in scan.get("zones", []):
        name = z["name"]
        reg = by_name.get(name)
        pid = resolve(name, table)
        b = boundary.get(name) or {}
        # ONE WORD PER ZONE, in this order: a project claims it (linked); the
        # operator ruled on it (outside / pending); nobody has said anything
        # (unclassified). The dashboard sorts on it.
        targets = zone_targets(z)
        readable = not any("error" in r for r in (z.get("records") or []))
        if pid:
            standing = "linked"
        elif b.get("status") == "outside":
            standing = "outside"
        elif b.get("status") == "pending":
            standing = "pending"
        elif name in prod_of or any(name.endswith("." + dm) for dm in prod_of):
            standing = "product"
        elif readable and z.get("records") is not None and not targets:
            # DORMANT: the DNS was read and neither the apex nor www points
            # anywhere. 56 of the 70 "unclassified" zones were this on
            # 2026-09-14 — parked names, not projects nobody spoke about. A
            # word the operator owes is a different debt from a domain that
            # serves nothing, so the two are not one list.
            standing = "dormant"
        else:
            standing = "unclassified"
        out.append({
            "id": (f"zone:{name}" if len(_accounts_of.get(name) or ()) < 2
                   else f"zone:{name}@{z.get('account_label') or z.get('account_id')}"),
            "name": name, "account_id": z.get("account_id"),
            "account": z.get("account_name"), "account_label": z.get("account_label"),
            "status": z.get("status"), "paused": z.get("paused"), "plan": z.get("plan"),
            "registered_domain": f"domain:{name}" if reg else None,
            "registrar": (reg or {}).get("registrar") or z.get("original_registrar"),
            "in_domain_registry": bool(reg),
            "project": pid, "standing": standing,
            "product": (prod_of.get(name)
                        or next((prod_of[dm] for dm in prod_of if name.endswith("." + dm)), None)),
            "boundary_why": b.get("why"), "candidates": b.get("candidates") or [],
            "created_on": z.get("created_on"), "modified_on": z.get("modified_on"),
            "targets": targets,
            "hosted_on": sorted({x["provider"] for x in targets if x["provider"] not in ("ip", "cname")}),
            "dns_readable": readable,
            "source_refs": SRC,
        })
    return out


def zones_document(scan: dict, rows: list[dict], obs_date: str) -> dict:
    by = {}
    for r in rows:
        by[r["standing"]] = by.get(r["standing"], 0) + 1
    return {
        "schema_version": 1, "updated_on": obs_date,
        # A DATE, not a clock: the committed registry may not change every run
        # by construction (test_tick_repo's rule), and a timestamp with seconds
        # is exactly that. The scan receipt keeps the clock.
        "scanned_on": (scan.get("scanned_at") or "")[:10] or None,
        "accounts": scan.get("accounts", []),
        "note": ("Every Cloudflare zone the estate holds a token for, joined to the "
                 "domain registry, the projects and the operator's boundary file. "
                 "`standing` is the one word the dashboard sorts on: linked (a "
                 "project's surface), outside (ruled beyond the estate's edge), "
                 "pending (the operator owes a word), product (a curated product claims "
                 "it while no single project does), dormant (DNS read, apex and www "
                 "point nowhere — a parked name), unclassified (nobody has spoken)."),
        "degraded": scan.get("degraded", []),
        "zones": rows,
        "totals": {"zones": len(rows), "by_standing": by,
                   "in_domain_registry": sum(1 for r in rows if r["in_domain_registry"]),
                   "linked": by.get("linked", 0)},
        "source_refs": SRC,
    }


# ─────────────────────────── products ─────────────────────────────────────────

def load_curated() -> tuple[dict, list[str]]:
    try:
        doc = json.loads(CURATED_PRODUCTS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, []
    return doc.get("products", {}), doc.get("roles", [])


def curated_errors(products: dict, roles: list[str], project_ids: set[str]
                   ) -> tuple[list[str], list[str]]:
    """(hard errors, missing members).

    The SHAPE of the file (a bad id, no name, no why, a role outside the list)
    is the operator's mistake and the emitter refuses it whole. A member the
    registry does not hold TODAY is a different thing: a project gets renamed,
    dissolved or measured out of a sandbox, and an emit that stops for that
    stops the tick for the whole estate. A test fixture whose registry lacked
    some curated members made the first version refuse its emit. Missing
    members are DROPPED and RECORDED as degraded.
    """
    bad, missing = [], []
    for pid, p in products.items():
        if not pid.startswith("product:"):
            bad.append(f"{pid}: id is not namespaced `product:`")
        if not p.get("name"):
            bad.append(f"{pid}: no name")
        if not p.get("why"):
            bad.append(f"{pid}: no why — a grouping nobody can judge later")
        for m, role in (p.get("members") or {}).items():
            if m not in project_ids:
                missing.append(f"{pid}: member {m} is not a project in this registry")
            if role not in roles:
                bad.append(f"{pid}: role {role!r} for {m} is not one of {roles}")
    return bad, missing


def suggested_products(projects: list[dict], domains: list[dict],
                       curated: dict) -> list[dict]:
    """Projects that share a registrable domain are probably one product.

    Derived, labelled `suggested`, never a fact: two repositories under one
    domain may be one product or two teams' work. The row exists so the
    operator can promote it into `collectors/products.json` with a word, not
    so anyone acts on it.
    """
    owned = {d["name"] for d in domains}
    already = {m for p in curated.values() for m in (p.get("members") or {})}
    by_apex: dict[str, dict[str, set[str]]] = {}
    for p in projects:
        if p["id"] in already:
            continue
        for s in p.get("sites") or []:
            h = (s.get("host") or "").lower()
            apex = registrable(h, owned) or (".".join(h.split(".")[-2:]) if h.count(".") >= 1 else None)
            if not apex:
                continue
            by_apex.setdefault(apex, {}).setdefault(p["id"], set()).add(h)
    out = []
    for apex, members in sorted(by_apex.items()):
        if len(members) < 2:
            continue
        out.append({
            "id": f"product:suggested-{slug(apex)}", "name": apex, "kind": "suggested",
            "members": [{"project": m, "role": "other"} for m in sorted(members)],
            "domains": sorted({h for hs in members.values() for h in hs}),
            "why": (f"{len(members)} projects claim hosts under {apex}; a shared "
                    f"registrable domain is the cheapest sign of one product — promote "
                    f"or dismiss in {paths.config_label('products.json')}"),
            "source_refs": SRC,
        })
    return out


def products_document(projects: list[dict], domains: list[dict], obs_date: str
                      ) -> tuple[dict, list[dict], list[str]]:
    """(document, part_of edges, curated errors)."""
    curated, roles = load_curated()
    ids = {p["id"] for p in projects}
    errors, missing = curated_errors(curated, roles, ids)
    rows, edges = [], []
    for pid, p in sorted(curated.items()):
        members = [{"project": m, "role": r} for m, r in sorted((p.get("members") or {}).items())
                   if m in ids]
        rows.append({"id": pid, "name": p["name"], "kind": "curated",
                     "members": members, "domains": sorted(p.get("domains") or []),
                     "why": p["why"], "said_on": p.get("said_on"), "source_refs": SRC})
        for m in members:
            if m["project"] in ids:
                edges.append({"id": f"relation:{m['project'].split(':', 1)[1]}:part-of:"
                                    f"{pid.split(':', 1)[1]}",
                              "type": "part_of", "from": m["project"], "to": pid,
                              "role": m["role"], "source_refs": SRC})
    rows += suggested_products(projects, domains, curated)
    doc = {
        "schema_version": 1, "updated_on": obs_date,
        "note": ("Several projects, folders and domains that are one thing to a user. "
                 "`kind: curated` rows are the operator's decisions from "
                 f"{paths.config_label('products.json')} and carry `part_of` edges; `kind: suggested` "
                 "rows are derived from a shared registrable domain and carry none — "
                 "an inference labelled as one."),
        "roles": roles, "products": rows,
        # A curated member this registry does not hold: dropped from the row,
        # named here — absent is not zero, and a silent drop is a member nobody
        # notices vanishing.
        "degraded": [{"source": paths.config_label("products.json"), "reason": m} for m in missing],
        "totals": {"products": len(rows),
                   "curated": sum(1 for r in rows if r["kind"] == "curated"),
                   "suggested": sum(1 for r in rows if r["kind"] == "suggested"),
                   "projects_grouped": len({m["project"] for r in rows for m in r["members"]})},
        "source_refs": SRC,
    }
    return doc, edges, errors


# ─────────────────────────── MCP servers ──────────────────────────────────────

LIVENESS = ("connected", "failed", "needs-auth", "not-listed", "not-probed")


def mcp_rows(scan: dict) -> list[dict]:
    """One row per declaration, id'd by agent, scope and name. No values,
    no query strings — the scan already dropped them; this only shapes."""
    out = []
    for r in scan.get("servers") or []:
        if not r.get("name"):
            continue
        scope = r.get("scope") or "user"
        out.append({
            "id": f"mcp:{r['agent']}/{slug(scope)}/{slug(r['name'])}",
            "name": r["name"], "agent": r["agent"], "scope": scope,
            "declared_in": r.get("declared_in"),
            "transport": r.get("transport"), "target": r.get("target"),
            "command": r.get("command"), "command_present": r.get("command_present"),
            "key_in_url": bool(r.get("key_in_url")),
            "key_in_header": bool(r.get("key_in_header")),
            "key_in_env": bool(r.get("key_in_env")),
            "liveness": r.get("liveness") if r.get("liveness") in LIVENESS else "not-probed",
            "liveness_detail": r.get("liveness_detail"),
            "source_refs": ["SRC-0017"],
        })
    return out


def mcp_document(scan: dict, rows: list[dict], obs_date: str) -> dict:
    by: dict[str, int] = {}
    for r in rows:
        by[r["liveness"]] = by.get(r["liveness"], 0) + 1
    names = {}
    for r in rows:
        names.setdefault(r["name"], set()).add(r["agent"])
    return {
        "schema_version": 1, "updated_on": obs_date,
        "scanned_on": (scan.get("scanned_at") or "")[:10] or None,
        "note": ("Every MCP server an agent on this machine is told to reach — Claude "
                 "Code (user and project scopes, plugins, claude.ai connectors), "
                 "Cursor, opencode — with its transport, its target minus any query "
                 "string, where its key sits (URL, header, env: booleans only), and "
                 "Claude's own liveness verdict where Claude was the agent asked."),
        "own_server": scan.get("own_server"), "own_declared": bool(scan.get("own_declared")),
        # WHEN LIVENESS WAS LAST ASKED, so a page of "not probed" can say how to probe
        # and how old the last answer is (audit A36). The verdicts are on the rows.
        "probe": {k: (scan.get("probe") or {}).get(k) for k in ("state", "probed_at", "complete")},
        "degraded": scan.get("degraded", []),
        "servers": rows,
        "totals": {"declarations": len(rows), "distinct_servers": len(names),
                   "by_liveness": by,
                   "in_one_agent_only": sum(1 for a in names.values() if len(a) == 1),
                   "key_in_url": sum(1 for r in rows if r["key_in_url"])},
        "source_refs": ["SRC-0017"],
    }
