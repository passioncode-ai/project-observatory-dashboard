#!/usr/bin/env python3
"""`collectors/estate_surfaces.py` — zones, products, and the evidence order.

Three questions the operator asked on 2026-09-13, each with a rule here:
which domains do we HAVE and where is the project link (`zone_rows`); which
projects are one PRODUCT (`products_document`, curated and suggested, never
confused); and when two projects claim one host, who wins — the operator's word,
then what is on this machine, then GitHub, then Bitbucket, then a wiki mention
(`EVIDENCE_RANK`). Everything runs on planted rows.
"""
from __future__ import annotations
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def load():
    spec = importlib.util.spec_from_file_location("estate_surfaces",
                                                  ROOT / "collectors/estate_surfaces.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


PROJECTS = [
    {"id": "project:local-one", "sites": [{"host": "shared.example",
                                           "evidence": ["repo-config:local-one/CNAME"]}]},
    {"id": "project:gh-one", "sites": [{"host": "shared.example",
                                        "evidence": ["github:o/r:homepageUrl"]}]},
    {"id": "project:wiki-one", "sites": [{"host": "shared.example",
                                          "evidence": ["vault-overview:x.md"]},
                                         {"host": "solo.example",
                                          "evidence": ["vault-overview:x.md"]}]},
    {"id": "project:claimed", "sites": [{"host": "shared.example",
                                         "evidence": ["operator-claim:the operator said so"]}]},
]
DOMAINS = [{"name": "shared.example", "registrar": "namecheap"},
           {"name": "solo.example", "registrar": "namecheap"}]


def test_the_evidence_order_is_the_operators() -> None:
    m = load()
    check("the operator's claim ranks above everything",
          m.evidence_rank(["operator-claim:x"]) < m.evidence_rank(["repo-config:a/b"]))
    check("a local checkout's config beats a GitHub homepage field",
          m.evidence_rank(["repo-config:a/b"]) < m.evidence_rank(["github:o/r:homepageUrl"]))
    check("GitHub beats Bitbucket",
          m.evidence_rank(["github:o/r"]) < m.evidence_rank(["bitbucket:o/r"]))
    check("Bitbucket beats a wiki mention",
          m.evidence_rank(["bitbucket:o/r"]) < m.evidence_rank(["vault-overview:x.md"]))
    check("unknown evidence ranks last, never first",
          m.evidence_rank(["something-new"]) > m.evidence_rank(["vault-overview:x"]))
    table = m.host_table(PROJECTS)
    check("with four projects on one host, the operator's claim wins",
          table["shared.example"][0] == "project:claimed", str(table))
    table2 = m.host_table([p for p in PROJECTS if p["id"] != "project:claimed"])
    check("without a claim, the LOCAL checkout wins over GitHub and the wiki",
          table2["shared.example"][0] == "project:local-one", str(table2))
    check("a subdomain resolves to its claimed apex",
          m.resolve("api.solo.example", table) == "project:wiki-one")


def test_every_zone_gets_one_standing() -> None:
    m = load()
    m.BOUNDARY = pathlib.Path(tmpdir.mkdtemp()) / "host_boundary.json"
    m.BOUNDARY.write_text(json.dumps({"hosts": {
        "ruled.example": {"status": "outside", "why": "a backend host"},
        "later.example": {"status": "pending", "candidates": ["project:local-one"]}}}),
        encoding="utf-8")
    scan = {"scanned_at": "2026-09-13T00:00:00Z", "accounts": ["a"], "zones": [
        {"name": "shared.example", "zone_id": "z1", "account_name": "Acct", "account_label": "a",
         "status": "active", "paused": False, "plan": "free", "original_registrar": "cloudflare"},
        {"name": "ruled.example", "zone_id": "z2", "account_name": "Acct", "account_label": "a",
         "status": "active", "paused": False, "plan": "free", "original_registrar": None},
        {"name": "later.example", "zone_id": "z3", "account_name": "Acct", "account_label": "a",
         "status": "pending", "paused": False, "plan": "free", "original_registrar": None},
        {"name": "silent.example", "zone_id": "z4", "account_name": "Acct", "account_label": "a",
         "status": "active", "paused": True, "plan": "pro", "original_registrar": "godaddy",
         "records": [{"name": "silent.example", "type": "A", "content": "1.2.3.4", "proxied": True}]},
        {"name": "parked.example", "zone_id": "z5", "account_name": "Acct", "account_label": "a",
         "status": "active", "paused": False, "plan": "free", "original_registrar": None,
         "records": [{"name": "mail.parked.example", "type": "CNAME", "content": "ghs.google.com", "proxied": False}]},
        {"name": "heroku.example", "zone_id": "z6", "account_name": "Acct", "account_label": "a",
         "status": "active", "paused": False, "plan": "free", "original_registrar": None,
         "records": [{"name": "heroku.example", "type": "CNAME", "content": "shiny-cat-123.herokudns.com", "proxied": True}]}]}
    scan["zones"].append({"name": "numbers.brand.example", "zone_id": "z7", "account_name": "Acct",
                          "account_label": "a", "status": "active", "paused": False, "plan": "free",
                          "original_registrar": None, "records": []})
    products = [{"id": "product:brand", "name": "Brand", "kind": "curated", "domains": ["brand.example"]},
                {"id": "product:suggested-x", "name": "x", "kind": "suggested", "domains": ["silent.example"]}]
    rows = {r["name"]: r for r in m.zone_rows(scan, DOMAINS, PROJECTS, products)}
    check("a zone under a CURATED product's domain stands as `product`, naming it",
          rows["numbers.brand.example"]["standing"] == "product"
          and rows["numbers.brand.example"]["product"] == "product:brand", str(rows["numbers.brand.example"]))
    check("a suggested product claims nothing — an inference is not an owner",
          rows["silent.example"]["standing"] != "product" and rows["silent.example"]["product"] is None, "")
    check("a zone a project claims is `linked` and names the project",
          rows["shared.example"]["standing"] == "linked"
          and rows["shared.example"]["project"] == "project:claimed", str(rows["shared.example"]))
    check("and it knows it is in the domain registry, with that registrar",
          rows["shared.example"]["in_domain_registry"]
          and rows["shared.example"]["registrar"] == "namecheap", "")
    check("a zone the operator ruled out is `outside` with the reason",
          rows["ruled.example"]["standing"] == "outside"
          and rows["ruled.example"]["boundary_why"] == "a backend host", "")
    check("a pending zone carries its candidates",
          rows["later.example"]["standing"] == "pending"
          and rows["later.example"]["candidates"] == ["project:local-one"], "")
    check("a zone nobody spoke about is `unclassified`, registrar from Cloudflare's own field",
          rows["silent.example"]["standing"] == "unclassified"
          and rows["silent.example"]["registrar"] == "godaddy"
          and rows["silent.example"]["in_domain_registry"] is False, str(rows["silent.example"]))
    check("a zone whose DNS was read and whose apex and www point nowhere is DORMANT",
          rows["parked.example"]["standing"] == "dormant"
          and rows["parked.example"]["targets"] == [], str(rows["parked.example"]))
    check("a zone with an apex A record is merely unclassified, hosted on an IP",
          rows["silent.example"]["standing"] == "unclassified"
          and rows["silent.example"]["targets"][0]["provider"] == "ip", str(rows["silent.example"]["targets"]))
    check("a CNAME to herokudns is read as hosted on Heroku, with the handle kept",
          rows["heroku.example"]["hosted_on"] == ["heroku"]
          and rows["heroku.example"]["targets"][0]["handle"] == "shiny-cat-123.herokudns.com",
          str(rows["heroku.example"]["targets"]))
    doc = m.zones_document(scan, list(rows.values()), "2026-09-13")
    check("the document's totals are computed from the rows",
          doc["totals"]["zones"] == 7 and doc["totals"]["by_standing"] == {
              "linked": 1, "outside": 1, "pending": 1, "unclassified": 2, "dormant": 1, "product": 1}, str(doc["totals"]))
    check("classify_target knows the providers a target can name",
          m.classify_target({"type": "CNAME", "content": "x.pages.dev"}) == ("cloudflare-pages", "x.pages.dev")
          and m.classify_target({"type": "CNAME", "content": "cname.vercel-dns.com"})[0] == "vercel"
          and m.classify_target({"type": "TXT", "content": "v=spf1"}) is None, "")


def test_products_are_curated_or_suggested_and_never_confused() -> None:
    m = load()
    m.CURATED_PRODUCTS = pathlib.Path(tmpdir.mkdtemp()) / "products.json"
    m.CURATED_PRODUCTS.write_text(json.dumps({
        "roles": ["site", "api", "other"],
        "products": {"product:shared": {
            "name": "Shared", "why": "the operator said these are one thing",
            "members": {"project:local-one": "site", "project:gh-one": "api"},
            "domains": ["shared.example"]}}}), encoding="utf-8")
    doc, edges, errors = m.products_document(PROJECTS, DOMAINS, "2026-09-13")
    check("a well-formed curated file has no errors", not errors, str(errors))
    kinds = {p["id"]: p["kind"] for p in doc["products"]}
    check("the curated product is a fact", kinds.get("product:shared") == "curated", str(kinds))
    check("and only curated members become part_of edges, each with its role",
          {(e["from"], e["role"]) for e in edges} ==
          {("project:local-one", "site"), ("project:gh-one", "api")}, str(edges))
    sugg = [p for p in doc["products"] if p["kind"] == "suggested"]
    check("projects sharing a domain and not yet curated are SUGGESTED",
          any(p["name"] == "shared.example" for p in sugg), str([p["name"] for p in sugg]))
    s = next(p for p in sugg if p["name"] == "shared.example")
    check("a suggestion leaves out members the curated product already holds",
          {x["project"] for x in s["members"]} == {"project:wiki-one", "project:claimed"},
          str(s["members"]))
    check("and a suggestion carries no edge", not any(e["to"] == s["id"] for e in edges))
    check("a host with ONE project suggests nothing — one is not a group",
          not any(p["name"] == "solo.example" for p in sugg), str([p["name"] for p in sugg]))

    m.CURATED_PRODUCTS.write_text(json.dumps({
        "roles": ["site"],
        "products": {"nonamespace": {"name": "X", "why": "y",
                                     "members": {"project:ghost": "admin"}}}}), encoding="utf-8")
    doc2, _edges, errors = m.products_document(PROJECTS, DOMAINS, "2026-09-13")
    check("a curated file with a bad id and an unknown role is refused, both named",
          len(errors) == 2 and any("namespaced" in e for e in errors)
          and any("role" in e for e in errors), str(errors))
    check("but a member this registry does not hold is DROPPED and RECORDED, not refused — "
          "a renamed project must not stop the tick",
          # (the ROLE error still names the ghost member — that is the shape
          # error, and it stays; what must be gone is the "not a project" refusal)
          not any("not a project" in e for e in errors)
          and any("ghost" in d["reason"] for d in doc2["degraded"])
          and not any(x["project"] == "project:ghost" for r in doc2["products"] for x in r["members"]),
          str(doc2.get("degraded")))


def test_the_heroku_chain_never_compares_a_name_to_a_name() -> None:
    m = load()
    scan_apps = [{"name": "shiny-web", "domains": ["shop.example", "www.shop.example"]},
                 {"name": "orphan", "domains": ["lost.example"]},
                 {"name": "no-domains", "domains": []}]
    linked = [{"name": "shiny-web", "project": "project:shop"},
              {"name": "no-domains", "project": "project:x"}]
    hints = m.heroku_site_hints(scan_apps, linked)
    check("a linked app's custom domains become that project's hosts",
          hints == {"project:shop": {"shop.example": "dns:heroku:shiny-web",
                                     "www.shop.example": "dns:heroku:shiny-web"}}, str(hints))
    check("an app no rule linked contributes nothing — a domain is not a guess",
          "lost.example" not in json.dumps(hints))
    check("and the evidence is ranked with GitHub, above a wiki mention",
          m.evidence_rank(["dns:heroku:x"]) == m.evidence_rank(["github:o/r"]) < m.evidence_rank(["vault-overview:x"]))


if __name__ == "__main__":
    print("estate surfaces — what we hold, what is one product, whose word wins\n")
    for fn in (test_the_evidence_order_is_the_operators,
               test_every_zone_gets_one_standing,
               test_products_are_curated_or_suggested_and_never_confused,
               test_the_heroku_chain_never_compares_a_name_to_a_name):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe estate knows what it holds, and a suggestion never passes for a decision\033[0m")
