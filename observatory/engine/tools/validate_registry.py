#!/usr/bin/env python3
"""Validate the typed inventory using only the Python standard library."""
from __future__ import annotations
import csv, hashlib, json, re, sys
from collections import Counter
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import activity
import paths
# `paths.REGISTRY`, not `ROOT/"registry"`. The hardcoded path made the validator
# the one tool `OBSERVATORY_REGISTRY` could not redirect, so a test that pointed
# it at a sandboxed copy silently validated the LIVE registry instead — and
# passed, for a reason that had nothing to do with what it was asserting.
INV=paths.REGISTRY
def resolve_ref(ref):
    """A reference names the store it lives in. A bare relative path is rejected:
    that is what silently broke when the registry moved out of the wiki."""
    if ref.startswith("registry:"): return (INV/ref.split(":",1)[1]), None
    if ref.startswith("vault:"):    return (paths.VAULT/ref.split(":",1)[1]), None
    p=Path(ref)
    if p.is_absolute(): return p, None
    return None, f"reference must start with 'registry:' or 'vault:': {ref}"
# Optional evidence belongs to the user, never a dated source-tree snapshot.
RAW = paths.NAMECHEAP_EXPORT
SNAPSHOT = paths.source_path("cloudflare_snapshot", paths.HOME / "unconfigured/cloudflare-snapshot.json")
RX=re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
def load(n):
    with (INV/n).open(encoding="utf-8") as h: return json.load(h)
def uniq(rs,k,label,errors):
    vals=[r[k] for r in rs]
    dup=sorted(v for v,c in Counter(vals).items() if c>1)
    if dup: errors.append(f"{label} duplicate {k}: {dup}")
    return set(vals)
def main():
    errors=[]
    degraded=[]
    domains=load("domains.json")["domains"]; exclusions=load("domain-exclusions.json")["domains"]
    projects=load("projects.json")["projects"]; repos=load("repositories.json")["repositories"]
    rd=load("relations.json"); relations=rd["relations"]; sources=load("sources.json")["sources"]
    snap=None
    if SNAPSHOT.is_file():
        snap=json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    else:
        degraded.append("Cloudflare snapshot unavailable; snapshot parity not verified")
    di=uniq(domains,"id","domains",errors); dn=uniq(domains,"name","domains",errors)
    xi=uniq(exclusions,"name","exclusions",errors); pi=uniq(projects,"id","projects",errors)
    ri=uniq(repos,"id","repositories",errors); uniq(relations,"id","relations",errors)
    si=uniq(sources,"id","sources",errors)
    for d in domains:
        n=d["name"]
        if n!=n.lower() or "." not in n or not RX.fullmatch(n): errors.append(f"invalid domain: {n}")
        if d["id"]!=f"domain:{n}": errors.append(f"domain id mismatch: {d['id']}")
        if d.get("ownership")!="owned": errors.append(f"canonical domain is not owned: {n}")
        if not isinstance(d.get("registrar"), str) or not d["registrar"].strip(): errors.append(f"missing registrar: {n}")
        dns=d.get("dns")
        if not isinstance(dns,dict) or not isinstance(dns.get("provider"),str) or not dns["provider"].strip(): errors.append(f"bad DNS record: {n}")
        elif "zone_present" in dns and type(dns["zone_present"]) is not bool: errors.append(f"invalid DNS zone presence: {n}")
        if set(d.get("source_refs",[]))-si: errors.append(f"unresolved source: {n}")
    if dn&xi: errors.append(f"owned/excluded overlap: {sorted(dn&xi)}")
    snapshot_zones = snap["zones"] if snap is not None else []
    if snap is not None:
        zi=uniq(snapshot_zones,"domain_id","snapshot",errors)
        expected_zones={d["id"] for d in domains if (d.get("dns") or {}).get("provider")=="cloudflare"}
        if zi!=expected_zones: errors.append(f"snapshot mismatch missing={sorted(expected_zones-zi)} extra={sorted(zi-expected_zones)}")
        for zone in snapshot_zones:
            if not isinstance(zone.get("status"),str) or not zone["status"]: errors.append("snapshot zone has no status")
            if not isinstance(zone.get("plan"),str) or not zone["plan"]: errors.append("snapshot zone has no plan")
    raw_rows=[]
    if RAW.is_file():
        with RAW.open(newline="",encoding="utf-8") as h: raw_rows=list(csv.DictReader(h))
    else:
        degraded.append("Registrar export unavailable; export parity not verified")
    raw_names=[r["Domain Name"].lower() for r in raw_rows]; raw_unique=set(raw_names)
    if RAW.is_file():
        namecheap_by_name={}
        for row in raw_rows:
            namecheap_by_name[row["Domain Name"].lower()]={
                "status": row["Domain status at NC"].lower(),
                "privacy": row["Domain privacy protection status"]=="ON",
                "auto_renew": row["Domain auto-renew status"]=="ON",
                "expires_on": datetime.strptime(row["Domain expiration date"], "%b %d %Y").date().isoformat(),
            }
        expected={d["name"] for d in domains if d["registrar"]=="namecheap"}|xi
        if raw_unique!=expected: errors.append(f"Namecheap mismatch unclassified={sorted(raw_unique-expected)} not_in_export={sorted(expected-raw_unique)}")
        for d in domains:
            if d["registrar"]=="namecheap" and d.get("namecheap")!=namecheap_by_name.get(d["name"]): errors.append(f"Namecheap fields differ from export: {d['name']}")
    for d in domains:
        if d["registrar"]!="namecheap" and "namecheap" in d: errors.append(f"non-Namecheap domain carries Namecheap data: {d['name']}")
    # ---- Heroku ------------------------------------------------------------
    # A hosting record is a claim about money and about whether something is up,
    # so it is gated like every other typed fact rather than trusted because a
    # collector wrote it.
    heroku=load("heroku-apps.json")["apps"] if (INV/"heroku-apps.json").is_file() else []
    hi=uniq(heroku,"id","heroku-apps",errors) if heroku else set()
    RULES={"heroku-github-link","heroku-remote","heroku-remote-nested","verified"}
    STATES={"running","down","suspended","resources-only","idle"}
    for a in heroku:
        if a["id"]!="heroku:"+a["name"]: errors.append(f"heroku id mismatch: {a['id']}")
        if a["state"] not in STATES: errors.append(f"unknown heroku state: {a['id']} {a['state']}")
        # A LINK AND ITS RULE ARE ONE FACT. Either both are present or neither
        # is: a project with no rule is the guess the never-infer-from-a-name rule forbids, and
        # a rule with no project is a rule that fired on nothing.
        if bool(a.get("project")) != bool(a.get("link_rule")):
            errors.append(f"heroku link without its rule, or rule without a link: {a['id']}")
        if a.get("link_rule") and a["link_rule"] not in RULES:
            errors.append(f"unknown heroku link rule: {a['id']} {a['link_rule']}")
        if a.get("project") and a["project"] not in pi:
            errors.append(f"heroku app names a project that does not exist: {a['id']} -> {a['project']}")
        # Silence about why is how an unresolved link becomes a permanent shrug.
        if not a.get("project") and not a.get("unlinked_reason"):
            errors.append(f"unlinked heroku app says nothing about why: {a['id']}")
    # A CURATED LINK IS A FACT WITH A HALF-LIFE. `collectors/heroku_links.json`
    # names project ids, and an id is DERIVED from a folder or a repository name
    # — rename either and the row stops matching, the application quietly becomes
    # unlinked, and the human's evidence is lost with no signal. Trap T11's shape,
    # found by the 2026-09-09 audit.
    links_path=paths.config_file("heroku_links.json")
    if links_path.is_file():
        sys.path.insert(0,str(ROOT/"collectors"))
        import heroku_registry
        errors.extend(heroku_registry.curated_link_errors(
            json.loads(links_path.read_text(encoding="utf-8")), pi,
            {a["name"] for a in heroku} if heroku else None))
    if heroku:
        hdoc=load("heroku-apps.json")
        if set(hdoc.get("source_refs",[]))-si: errors.append("heroku-apps.json cites an unresolved source")
        t=hdoc.get("totals") or {}
        if t.get("apps")!=len(heroku): errors.append("heroku totals.apps disagrees with the list it summarises")
        if t.get("linked_to_a_project")!=sum(1 for a in heroku if a.get("project")):
            errors.append("heroku totals.linked_to_a_project disagrees with the rows")

    # ---- credentials --------------------------------------------------------
    # A document about secrets is gated harder than the rest, and the first rule
    # is that it contains none: `tools/check_secrets.py` reads it on every gate
    # run, and this adds the shape check beside it.
    creds=load("credentials.json")["credentials"] if (INV/"credentials.json").is_file() else []
    ci=uniq(creds,"id","credentials",errors) if creds else set()
    # FIVE KINDS SINCE 2026-09-14: `project-secret-file` is a key a
    # project keeps in its OWN `secrets/` folder — ten of them on this estate,
    # and no inventory had seen one. A closed set is the point: a sixth kind
    # arrives here deliberately or not at all.
    KINDS={"llm-api-key","project-secret","leaked-untracked","machine-secret",
           "project-secret-file"}
    for c in creds:
        if not c["id"].startswith("credential:"): errors.append(f"credential id is not namespaced: {c['id']}")
        if c.get("kind") not in KINDS: errors.append(f"unknown credential kind: {c['id']} {c.get('kind')}")
        for pid in c.get("used_by") or []:
            if pid not in pi: errors.append(f"credential names a project that does not exist: {c['id']} -> {pid}")
        # Silence about why nothing claims it is how a shared account becomes
        # permanently unattributable.
        if not (c.get("used_by") or []) and not c.get("unclaimed_reason"):
            errors.append(f"unclaimed credential says nothing about why: {c['id']}")
        # THE ONE RULE THIS DOCUMENT EXISTS TO KEEP. A label is a truncated key
        # with an ellipsis in the middle; anything longer that starts the same way
        # is a key, and a key here would be a credential committed to git.
        for k,v in c.items():
            if isinstance(v,str) and v.startswith("sk-") and "..." not in v and len(v)>24:
                errors.append(f"credential record carries something key-shaped: {c['id']}.{k}")
    if creds:
        cdoc=load("credentials.json")
        if set(cdoc.get("source_refs",[]))-si: errors.append("credentials.json cites an unresolved source")
        t=cdoc.get("totals") or {}
        if t.get("credentials")!=len(creds): errors.append("credentials totals disagree with the list")
        if t.get("leaked_unrotated")!=sum(1 for c in creds if c.get("leaked")):
            errors.append("credentials totals.leaked_unrotated disagrees with the rows")
    owners_path=paths.config_file("credential_owners.json")
    if owners_path.is_file():
        odoc=json.loads(owners_path.read_text(encoding="utf-8"))
        cids={c["id"] for c in creds}
        pnames={p["name"] for p in projects}
        for row in odoc.get("owners",[]):
            if not row.get("evidence"):
                errors.append(f"credential_owners row for {row.get('credential')} carries no evidence")
            if row.get("credential") and cids and row["credential"] not in cids:
                errors.append(f"credential_owners names {row['credential']}, which no scan holds — the curated row is stale")
            for n in row.get("projects") or []:
                if n not in pnames and not n.startswith("project:"):
                    errors.append(f"credential_owners names project {n!r}, which the registry does not hold")

    # ---- products and zones -------------------------------------
    prods=load("products.json")["products"] if (INV/"products.json").is_file() else []
    pri=uniq(prods,"id","products",errors) if prods else set()
    if prods:
        pdoc=load("products.json"); roles=set(pdoc.get("roles") or [])
        for pr in prods:
            if not pr["id"].startswith("product:"): errors.append(f"product id is not namespaced: {pr['id']}")
            if pr.get("kind") not in ("curated","suggested"): errors.append(f"product kind unknown: {pr['id']} {pr.get('kind')}")
            if not pr.get("why"): errors.append(f"product says nothing about why: {pr['id']}")
            for m in pr.get("members") or []:
                if m.get("project") not in pi: errors.append(f"product member is not a project: {pr['id']} -> {m.get('project')}")
                if m.get("role") not in roles: errors.append(f"product member role unknown: {pr['id']} {m.get('role')}")
        t=pdoc.get("totals") or {}
        if t.get("products")!=len(prods): errors.append("products totals disagree with the list")
    zones=load("cloudflare-zones.json")["zones"] if (INV/"cloudflare-zones.json").is_file() else []
    if zones:
        uniq(zones,"id","cloudflare-zones",errors)
        STANDING={"linked","outside","pending","product","dormant","unclassified"}
        for z in zones:
            if not z["id"].startswith("zone:"): errors.append(f"zone id is not namespaced: {z['id']}")
            if z.get("standing") not in STANDING: errors.append(f"zone standing unknown: {z['id']} {z.get('standing')}")
            if z.get("registered_domain") and z["registered_domain"] not in di: errors.append(f"zone names a domain the registry does not hold: {z['id']}")
            if z.get("project") and z["project"] not in pi: errors.append(f"zone names a project that does not exist: {z['id']} -> {z['project']}")
            if (z.get("standing")=="linked")!=bool(z.get("project")): errors.append(f"zone standing disagrees with its project link: {z['id']}")
            if (z.get("standing")=="product")!=(bool(z.get("product")) and not z.get("project")): errors.append(f"zone standing disagrees with its product claim: {z['id']}")
            if z.get("product") and z["product"] not in pri: errors.append(f"zone names a product that does not exist: {z['id']} -> {z['product']}")
        zt=load("cloudflare-zones.json").get("totals") or {}
        if zt.get("zones")!=len(zones): errors.append("zone totals disagree with the list")
    mcps=load("mcp-servers.json")["servers"] if (INV/"mcp-servers.json").is_file() else []
    if mcps:
        uniq(mcps,"id","mcp-servers",errors)
        LIVE={"connected","failed","needs-auth","not-listed","not-probed"}
        for s in mcps:
            if not s["id"].startswith("mcp:"): errors.append(f"mcp id is not namespaced: {s['id']}")
            if s.get("liveness") not in LIVE: errors.append(f"mcp liveness unknown: {s['id']} {s.get('liveness')}")
            if s.get("target") and "?" in s["target"]: errors.append(f"mcp target carries a query string: {s['id']}")
        mt=load("mcp-servers.json").get("totals") or {}
        if mt.get("declarations")!=len(mcps): errors.append("mcp totals disagree with the list")
    zi={z["id"] for z in zones}
    accs=load("accounts.json")["accounts"] if (INV/"accounts.json").is_file() else []
    uniq(accs,"id","accounts",errors)
    for a in accs:
        if not a["id"].startswith("account:") or "@" in a["id"]: errors.append(f"account id is not a provider id: {a['id']}")
    ai={a["id"] for a in accs}
    envs=load("environments.json")["environments"] if (INV/"environments.json").is_file() else []
    uniq(envs,"id","environments",errors)
    for e in envs:
        if e["id"]!=f"environment:{e['project']}/{e['name']}": errors.append(f"environment id is not <project>/<name>: {e['id']}")
        if e["project"] not in pi: errors.append(f"environment names a project that does not exist: {e['id']}")
    ei={e["id"] for e in envs}
    endpoints=di|pi|ri|hi|ci|pri|zi|ai|ei; types=set(rd["relation_types"])
    for r in relations:
        if r["type"] not in types: errors.append(f"undefined relation type: {r['type']}")
        if r.get("environment") and r["environment"] not in ei: errors.append(f"relation names an environment that does not exist: {r['id']}")
        if r.get("deployment") and r["deployment"] not in hi: errors.append(f"relation names a deployment that does not exist: {r['id']}")
        for side in ("from","to"):
            if r[side] not in endpoints: errors.append(f"unresolved relation endpoint: {r['id']} {side}={r[side]}")
    for p in projects:
        if not p.get("source_refs") or set(p["source_refs"])-si: errors.append(f"project has missing or unresolved sources: {p['id']}")
        cp=p.get("canonical_page")
        if cp:
            target,bad=resolve_ref(cp)
            if bad: errors.append(bad)
            elif not target.exists(): errors.append(f"missing canonical page: {cp}")
    # The activity tier is DERIVED, so it is re-derived here rather than trusted.
    # A tier that can be typed is a tier that can disagree with the date beside
    # it, and a reader would have no way to tell which of the two was wrong.
    #
    # A one-day tolerance, because the tier depends on today: a project sitting
    # exactly on a boundary legitimately changes tier between an emit at 23:59
    # and a validation at 00:01. Failing on that would be the gate going red for
    # a non-defect.
    import datetime as _dt
    valid_tiers = set(activity.tier_ids())
    today = _dt.date.today()
    for p in projects:
        tier = p.get("activity_tier")
        if tier is None:
            errors.append(f"project has no activity_tier: {p['id']}")
            continue
        if tier not in valid_tiers:
            errors.append(f"project has an unknown activity_tier {tier!r}: {p['id']}")
            continue
        allowed = {activity.tier_of(p.get("last_activity_on"), today=today),
                   activity.tier_of(p.get("last_activity_on"), today=today - _dt.timedelta(days=1))}
        if tier not in allowed:
            errors.append(f"{p['id']} says activity_tier={tier} but "
                          f"last_activity_on={p.get('last_activity_on')} derives {sorted(allowed)}")

    for repo in repos:
        if not repo.get("source_refs") or set(repo["source_refs"])-si: errors.append(f"repository has missing or unresolved sources: {repo['id']}")

    # PROVENANCE IS CHECKED FOR TRUTH, NOT ONLY FOR RESOLUTION. Every rule above
    # asks whether a reference points at a source that exists. None asked whether
    # it points at the source that did the measuring — so every repository with
    # `local.sync` and `local.remote_head`, produced by `git ls-remote` over the
    # network, cited SRC-0007, the filesystem scan of the projects directory.
    #
    # A source now declares `evidence_for`, and this is the converse: a record
    # carrying one of those fields must cite that source. It is a NECESSARY
    # condition, not a sufficient one — nothing here can prove a citation true —
    # but it catches the whole class where a field appears with no witness.
    evidence_of = {s["id"]: s["evidence_for"] for s in sources if s.get("evidence_for")}
    def field_present(record, dotted):
        node = record
        for part in dotted.split("."):
            if not isinstance(node, dict): return False
            node = node.get(part)
        return bool(node)
    for sid, fields in sorted(evidence_of.items()):
        for field in fields:
            if field.startswith("registry:"):
                continue                     # a whole file, checked below
            for repo in repos:
                if field_present(repo, field) and sid not in repo.get("source_refs", []):
                    errors.append(f"{repo['id']} carries {field}, which only {sid} measures, "
                                  f"but cites {repo.get('source_refs')}")
    # The liveness file was never loaded by this validator at all, so neither its
    # claims nor their provenance were checked by anything.
    live_path = INV/"domain-liveness.json"
    if live_path.exists():
        live=json.loads(live_path.read_text(encoding="utf-8"))
        refs=set(live.get("source_refs") or [])
        has_measurement=bool(live.get("hosts") or live.get("scanned_on"))
        if not refs and has_measurement: errors.append("domain-liveness.json carries no source_refs")
        elif not refs: degraded.append("Domain liveness not measured")
        elif refs-si: errors.append(f"domain-liveness.json cites unresolved sources: {sorted(refs-si)}")
        for sid, fields in evidence_of.items():
            if has_measurement and "registry:domain-liveness.json" in fields and sid not in refs:
                errors.append(f"domain-liveness.json is measured by {sid} but does not cite it")
    # THE PHANTOM INVARIANT, guarded where it can stop the tick. `stale-remotes`
    # records a transferred address; the whole point of following the transfer is
    # that the OLD name never becomes a repository. So the new name must be in
    # the registry and the old one must not — if that inverts, the merge has
    # admitted a repository that belongs to nobody, and this validator is the
    # only step that can stop it being committed.
    stale_path = INV/"stale-remotes.json"
    if stale_path.exists():
        stale=json.loads(stale_path.read_text(encoding="utf-8"))
        refs=set(stale.get("source_refs") or [])
        if not refs: errors.append("stale-remotes.json carries no source_refs")
        elif refs-si: errors.append(f"stale-remotes.json cites unresolved sources: {sorted(refs-si)}")
        for c in stale.get("clones", []):
            if not c.get("was") or not c.get("now"):
                errors.append(f"stale-remotes.json row names no address pair: {c}")
                continue
            if f"repository:{c['was']}" in ri:
                errors.append(f"phantom repository in the registry: {c['was']} was "
                              f"transferred to {c['now']} and must not be a repository")
            if f"repository:{c['now']}" not in ri:
                errors.append(f"stale-remotes.json names {c['now']} as the current "
                              f"address, which is not a repository in this registry")
    for relation in relations:
        if not relation.get("source_refs") or set(relation["source_refs"])-si: errors.append(f"relation has missing or unresolved sources: {relation['id']}")
        # Derived edges must say why they exist; an authored edge may explain itself in its own fields.
        if relation["type"]=="credential_used_by":
            b=relation.get("binding")
            if b not in ("run","build","local","unknown"): errors.append(f"credential edge has no binding: {relation['id']}")
            if b=="run" and not relation.get("deployment"): errors.append(f"run binding names no deployment: {relation['id']}")
        if relation["type"] in ("implemented_by", "deployed_to", "credential_used_by", "in_account", "serves") and not relation.get("rule"):
            errors.append(f"derived relation carries no rule: {relation['id']}")
    for source in sources:
        artifact=source.get("artifact")
        if not artifact: continue
        path,bad=resolve_ref(artifact)
        if bad: errors.append(f"{source['id']}: {bad}")
        elif not path.exists(): errors.append(f"source artifact does not resolve: {source['id']} {artifact}")
        elif source.get("sha256") and hashlib.sha256(path.read_bytes()).hexdigest()!=source["sha256"]: errors.append(f"source artifact hash mismatch: {source['id']}")
    if errors:
        print("Inventory validation FAILED",file=sys.stderr)
        for error in errors: print(f"- {error}",file=sys.stderr)
        return 1
    status=Counter(x.get("status") for x in snapshot_zones)
    duplicates=sum(c-1 for c in Counter(raw_names).values() if c>1)
    print("Inventory validation OK" + (" (degraded evidence)" if degraded else ""))
    print(f"degraded_sources={len(degraded)}")
    for reason in degraded: print(f"DEGRADED: {reason}")
    # The domain counters belong to an estate that has domains: printed as nine
    # `key=0` lines to a new user with none, they read as maintainer debugging.
    if dn or raw_rows or snapshot_zones:
        print(f"domains: {len(dn)} owned (Namecheap {sum(d['registrar']=='namecheap' for d in domains)}, "
              f"Cloudflare {sum(d['registrar']=='cloudflare' for d in domains)}), {len(xi)} excluded as not owned; "
              f"Cloudflare zones active {status['active']}, invalid nameservers {status['invalid_nameservers']}; "
              f"registrar export {len(raw_rows)} row(s), {len(raw_unique)} unique, {duplicates} duplicate")
    print(f"projects={len(projects)} repositories={len(repos)} relations={len(relations)}")
    print(f"  {sum(1 for p in projects if p.get('canonical_page'))} with a canonical page, "
          f"{sum(1 for p in projects if not p.get('has_vault_note'))} without a wiki note; "
          f"{sum(1 for r in repos if r.get('local'))} repositories with a local checkout")
    return 0
if __name__=="__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"Inventory validation FAILED: malformed or unreadable input ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(1)
