#!/usr/bin/env python3
"""Every rule the validator has, seen failing.

`tools/validate_registry.py` is what stops the tick before it rewrites the
canonical fact base: `tick.sh` runs it between `merge` and `emit`, and a non-zero
exit means the registry is not touched. When this suite was first written only
two of its error conditions had ever been seen fail; the rest were green because
nothing had ever made them red, in the highest-stakes gate in the repository.

Each rule below is planted in a synthetic complete workspace, one minimal
mutation at a time (`validator_fixture.py`). Its own matching registrar export,
zone snapshot and canonical wiki page let the healthy baseline pass before any
violation is planted.

**A defect found while building this** is the reason the file starts with this
paragraph: the registrar export was once resolved from the source tree rather
than from `paths`, four lines below a comment explaining that exact bug about
the registry and calling it fixed. So a sandboxed registry validated its domains
against the LIVE export: the one input a redirect could not move, in the file
whose own comment says it was redirected.
"""
from __future__ import annotations
import json, os, pathlib, re, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402
import validator_fixture  # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def sandbox() -> pathlib.Path:
    """Complete synthetic workspace: registry, export, snapshot, wiki, curation."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-validator-"))
    return validator_fixture.seed(d)


def run(reg: pathlib.Path) -> tuple[int, str]:
    p = subprocess.run([PY, "tools/validate_registry.py"], cwd=ROOT,
                       env=validator_fixture.environment(reg),
                       capture_output=True, text=True, timeout=300)
    return p.returncode, p.stdout + p.stderr


def edit(reg: pathlib.Path, name: str, fn) -> None:
    """Apply `fn` to one registry document and write it back."""
    f = reg / name
    doc = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    fn(doc)
    f.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ─────────────── the control: an untouched copy must pass ───────────────

def test_the_untouched_copy_passes() -> None:
    """Without this, every 'caught it' below could be a fixture artefact."""
    reg = sandbox()
    code, out = run(reg)
    check("the complete synthetic registry validates", code == 0, out[-400:])
    check('validation summary counts its snapshot, not the wider zone inventory',
          'Cloudflare zones active 1, invalid nameservers 1;' in out,
          out[-500:])
    reg = sandbox()
    edit(reg, "domain-liveness.json", lambda d: d.update(source_refs=[]))
    code, out = run(reg)
    check("liveness that was never measured is reported degraded, not refused",
          code == 0 and "DEGRADED: Domain liveness not measured" in out, out[-400:])


# ─────────────── one planted violation per rule ─────────────────────────

def first_domain(doc: dict) -> dict:
    return doc["domains"][0]


def first_app(doc: dict) -> dict:
    return doc["apps"][0]


def linked_app(doc: dict) -> dict:
    """An application the registry ties to a project — several rules need one."""
    return next(a for a in doc["apps"] if a.get("project"))


def first_cred(doc: dict) -> dict:
    return doc["credentials"][0]


def first_product(doc: dict) -> dict:
    return doc["products"][0]


def first_zone(doc: dict) -> dict:
    return doc["zones"][0]


def linked_zone(doc: dict) -> dict:
    return next(z for z in doc["zones"] if z.get("project"))


def credential_edge() -> dict:
    """A derived credential edge, complete except for what a plant removes."""
    return {"id": "relation:credential-fixture", "from": "credential:fixture",
            "to": validator_fixture.PROJECT, "type": "credential_used_by",
            "rule": "synthetic", "source_refs": ["SRC-FIXTURE"]}


#: (label, document, mutation, the fingerprint the message must carry).
#: A table rather than twenty-five functions: the interesting part is the
#: mutation and the sentence it must produce, and a table makes a missing rule
#: visible as a missing row.
PLANTS = [
    ("an upper-case domain name", "domains.json",
     lambda d: first_domain(d).update(name=first_domain(d)["name"].upper()),
     "invalid domain"),
    ("an id that does not match the name", "domains.json",
     lambda d: first_domain(d).update(id="domain:not-the-name.test"),
     "domain id mismatch"),
    ("a canonical domain that is not owned", "domains.json",
     lambda d: first_domain(d).update(ownership="external"),
     "canonical domain is not owned"),
    # The engine accepts any registrar and DNS provider it is told about; what
    # it refuses is a record that names none, or a zone flag that is not a bool.
    ("a domain with no registrar", "domains.json",
     lambda d: first_domain(d).update(registrar=""),
     "missing registrar"),
    ("a DNS record that names no provider", "domains.json",
     lambda d: first_domain(d).update(dns={"provider": "", "zone_present": True}),
     "bad DNS record"),
    ("a DNS zone flag that is not a boolean", "domains.json",
     lambda d: first_domain(d).update(dns={"provider": "cloudflare", "zone_present": "yes"}),
     "invalid DNS zone presence"),
    ("a domain at another registrar carrying export data", "domains.json",
     lambda d: d["domains"][1].update(namecheap={"status": "active"}),
     "non-Namecheap domain carries Namecheap data"),
    ("a source reference that resolves to nothing", "domains.json",
     lambda d: first_domain(d).update(source_refs=["SRC-9999"]),
     "unresolved source"),
    # The domain registered elsewhere on purpose: it is the only one not in the
    # registrar export, so excluding it does not disturb that comparison. Planting
    # a Namecheap domain here failed with `Namecheap mismatch … not_in_export`
    # instead — an EARLIER rule catching the same edit, which proves the edit
    # broke something but not that the overlap rule works.
    ("a domain that is both owned and excluded", "domain-exclusions.json",
     lambda d: d["domains"].append({"name": validator_fixture.ELSEWHERE,
                                    "classification": "not_owned",
                                    "observed_in": ["SRC-FIXTURE"],
                                    "classified_by": "SRC-FIXTURE"}),
     "owned/excluded overlap"),
    ("a relation type nothing defines", "relations.json",
     lambda d: d["relations"][0].update(type="haunted_by"),
     "undefined relation type"),
    ("a relation pointing at nothing", "relations.json",
     lambda d: d["relations"][0].update(to="repository:nobody/nothing"),
     "unresolved relation endpoint"),
    ("a derived relation with no rule", "relations.json",
     lambda d: d["relations"][0].pop("rule"),
     "derived relation carries no rule"),
    ("a relation naming an environment that does not exist", "relations.json",
     lambda d: d["relations"][0].update(environment="environment:project:ghost/prod"),
     "relation names an environment that does not exist"),
    ("a relation naming a deployment that does not exist", "relations.json",
     lambda d: d["relations"][0].update(deployment="heroku:ghost"),
     "relation names a deployment that does not exist"),
    ("a credential edge with no binding", "relations.json",
     lambda d: (d["relation_types"].update(credential_used_by={}),
                d["relations"].append(credential_edge())),
     "credential edge has no binding"),
    ("a run binding that names no deployment", "relations.json",
     lambda d: (d["relation_types"].update(credential_used_by={}),
                d["relations"].append({**credential_edge(), "binding": "run"})),
     "run binding names no deployment"),
    ("a relation with no source", "relations.json",
     lambda d: d["relations"][0].update(source_refs=[]),
     "relation has missing or unresolved source"),
    ("a project with no source", "projects.json",
     lambda d: d["projects"][0].update(source_refs=[]),
     "project has missing or unresolved source"),
    ("a project whose canonical page is gone", "projects.json",
     lambda d: next(p for p in d["projects"] if p.get("canonical_page")).update(
         canonical_page="vault:projects/nowhere/nothing.md"),
     "missing canonical page"),
    ("a project with no activity tier", "projects.json",
     lambda d: d["projects"][0].pop("activity_tier", None),
     "project has no activity_tier"),
    ("a project with an invented tier", "projects.json",
     lambda d: d["projects"][0].update(activity_tier="lukewarm"),
     "unknown activity_tier"),
    ("a tier that disagrees with its own date", "projects.json",
     lambda d: next(p for p in d["projects"]
                    if p.get("activity_tier") == "active").update(activity_tier="cold"),
     "says activity_tier=cold"),
    ("a repository with no source", "repositories.json",
     lambda d: d["repositories"][0].update(source_refs=[]),
     "repository has missing or unresolved source"),
    ("stale remotes with no source at all", "stale-remotes.json",
     lambda d: d.update(source_refs=[]),
     "stale-remotes.json carries no source_refs"),
    ("stale remotes citing a source that does not exist", "stale-remotes.json",
     lambda d: d.update(source_refs=["SRC-9999"]),
     "stale-remotes.json cites unresolved sources"),
    ("a source artifact that resolves to nothing", "sources.json",
     lambda d: d["sources"][0].update(artifact="registry:nothing-here.json"),
     "source artifact does not resolve"),
    ("a source artifact whose hash moved", "sources.json",
     lambda d: d["sources"][0].update(artifact="vault:fixture.md", sha256="0" * 64),
     "source artifact hash mismatch"),
    ("an account id that is an address, not a provider id", "accounts.json",
     lambda d: d.update(accounts=[{"id": "account:someone@example.com"}]),
     "account id is not a provider id"),
    ("an environment id that is not <project>/<name>", "environments.json",
     lambda d: d.update(environments=[{"id": "environment:prod",
                                       "project": validator_fixture.PROJECT, "name": "prod"}]),
     "environment id is not <project>/<name>"),
    ("an environment of a project that does not exist", "environments.json",
     lambda d: d.update(environments=[{"id": "environment:project:ghost/prod",
                                       "project": "project:ghost", "name": "prod"}]),
     "environment names a project that does not exist"),
    # A MEASURED liveness file with no source. An unmeasured one with no source
    # is not an error but a degraded input — see the control test above.
    ("measured liveness with no source at all", "domain-liveness.json",
     lambda d: d.update(source_refs=[], scanned_on="2026-01-01"),
     "carries no source_refs"),
    ("liveness citing a source that does not exist", "domain-liveness.json",
     lambda d: d.update(source_refs=["SRC-9999"]),
     "cites unresolved source"),
    # ── Heroku. Every one of these rules first shipped unwatched: this suite's
    # own "a rule nobody has watched fail" check is what found that, one gate
    # run after the subsystem landed.
    ("a heroku id that does not match the app name", "heroku-apps.json",
     lambda d: first_app(d).update(id="heroku:not-the-name"),
     "heroku id mismatch"),
    ("a heroku state outside the five", "heroku-apps.json",
     lambda d: first_app(d).update(state="floating"),
     "unknown heroku state"),
    ("a link with no rule behind it", "heroku-apps.json",
     lambda d: linked_app(d).pop("link_rule"),
     "heroku link without its rule"),
    ("a link rule nothing defines", "heroku-apps.json",
     lambda d: linked_app(d).update(link_rule="because-it-looked-right"),
     "unknown heroku link rule"),
    ("a link to a project that does not exist", "heroku-apps.json",
     lambda d: linked_app(d).update(project="project:no-such-thing"),
     "heroku app names a project that does not exist"),
    ("an unlinked app that says nothing about why", "heroku-apps.json",
     lambda d: linked_app(d).update(project=None, link_rule=None,
                                    unlinked_reason=None, unlinked_kind=None),
     "unlinked heroku app says nothing about why"),
    ("a hosting document citing a source nothing holds", "heroku-apps.json",
     lambda d: d.update(source_refs=["SRC-9999"]),
     "heroku-apps.json cites an unresolved source"),
    ("a total that disagrees with the rows it summarises", "heroku-apps.json",
     lambda d: d["totals"].update(apps=d["totals"]["apps"] + 1),
     "heroku totals.apps disagrees"),
    ("a linked count that disagrees with the rows", "heroku-apps.json",
     lambda d: d["totals"].update(linked_to_a_project=d["totals"]["linked_to_a_project"] + 1),
     "heroku totals.linked_to_a_project disagrees"),
    # THE CREDENTIALS PROJECTION, whose rules arrived with none of them watched.
    # Eight live in `credentials.json` and plant here; the three
    # `credential_owners` rules read a CURATED file outside the registry, so they
    # are driven by the test below instead.
    ("a credential id without its namespace", "credentials.json",
     lambda d: first_cred(d).update(id="openrouter/whatever"),
     "credential id is not namespaced"),
    ("a credential of an unknown kind", "credentials.json",
     lambda d: first_cred(d).update(kind="mystery"),
     "unknown credential kind"),
    ("a credential used by a project that does not exist", "credentials.json",
     lambda d: first_cred(d).update(used_by=["project:no-such-thing"]),
     "credential names a project that does not exist"),
    ("a credential nothing claims and no reason why", "credentials.json",
     lambda d: first_cred(d).update(used_by=[], unclaimed_reason=None),
     "unclaimed credential says nothing about why"),
    # The one rule the document exists for: a real key in a file that is in git.
    ("a credential record carrying a key-shaped value", "credentials.json",
     lambda d: first_cred(d).update(label="sk-or-v1-" + "a" * 40),
     "credential record carries something key-shaped"),
    ("a credentials document citing a source nothing holds", "credentials.json",
     lambda d: d.update(source_refs=["source:invented"]),
     "credentials.json cites an unresolved source"),
    ("credential totals that disagree with the rows", "credentials.json",
     lambda d: d["totals"].update(credentials=999),
     "credentials totals disagree with the list"),
    ("a leak count that disagrees with the rows", "credentials.json",
     lambda d: d["totals"].update(leaked_unrotated=999),
     "credentials totals.leaked_unrotated disagrees with the rows"),
    # PRODUCTS AND ZONES — ten rules, planted the day they arrived.
    ("a product id without its namespace", "products.json",
     lambda d: first_product(d).update(id="nonamespace"), "product id is not namespaced"),
    ("a product of an unknown kind", "products.json",
     lambda d: first_product(d).update(kind="guessed"), "product kind unknown"),
    ("a product with no why", "products.json",
     lambda d: first_product(d).update(why=""), "product says nothing about why"),
    ("a product member that is not a project", "products.json",
     lambda d: first_product(d)["members"].append({"project": "project:ghost", "role": "site"}),
     "product member is not a project"),
    ("a product member with an unknown role", "products.json",
     lambda d: first_product(d)["members"].append({"project": first_product(d)["members"][0]["project"], "role": "boss"}),
     "product member role unknown"),
    ("product totals that disagree", "products.json",
     lambda d: d["totals"].update(products=999), "products totals disagree with the list"),
    ("a zone id without its namespace", "cloudflare-zones.json",
     lambda d: first_zone(d).update(id="nonamespace"), "zone id is not namespaced"),
    ("a zone with an unknown standing", "cloudflare-zones.json",
     lambda d: first_zone(d).update(standing="maybe"), "zone standing unknown"),
    ("a zone naming a domain the registry lacks", "cloudflare-zones.json",
     lambda d: first_zone(d).update(registered_domain="domain:invented.test"),
     "zone names a domain the registry does not hold"),
    ("a zone naming a project that does not exist", "cloudflare-zones.json",
     lambda d: linked_zone(d).update(project="project:ghost"),
     "zone names a project that does not exist"),
    ("a linked zone with no project", "cloudflare-zones.json",
     lambda d: linked_zone(d).update(project=None),
     "zone standing disagrees with its project link"),
    ("a zone that says `product` with no product named", "cloudflare-zones.json",
     lambda d: next(z for z in d["zones"] if not z.get("project")).update(standing="product", product=None),
     "zone standing disagrees with its product claim"),
    ("a zone naming a product that does not exist", "cloudflare-zones.json",
     lambda d: next(z for z in d["zones"] if z.get("standing") == "product").update(product="product:ghost"),
     "zone names a product that does not exist"),
    # MCP INVENTORY
    ("an mcp id without its namespace", "mcp-servers.json",
     lambda d: d["servers"][0].update(id="nonamespace"), "mcp id is not namespaced"),
    ("an mcp liveness nobody defined", "mcp-servers.json",
     lambda d: d["servers"][0].update(liveness="maybe"), "mcp liveness unknown"),
    ("an mcp target carrying a query string", "mcp-servers.json",
     lambda d: d["servers"][0].update(target="https://mcp.example.com/mcp?token=nope"),
     "mcp target carries a query string"),
    ("mcp totals that disagree", "mcp-servers.json",
     lambda d: d["totals"].update(declarations=999), "mcp totals disagree with the list"),
    ("zone totals that disagree", "cloudflare-zones.json",
     lambda d: d["totals"].update(zones=999), "zone totals disagree with the list"),
]


def test_every_planted_violation_is_caught_and_named() -> None:
    for label, doc, mutate, fingerprint in PLANTS:
        reg = sandbox()
        try:
            edit(reg, doc, mutate)
        except (StopIteration, KeyError, IndexError) as exc:
            check(f"the fixture for {label} could be built", False,
                  f"{type(exc).__name__}: {exc}")
            continue
        code, out = run(reg)
        check(f"{label} is refused", code != 0, out[-200:])
        check(f"…and the message says {fingerprint!r}", fingerprint in out,
              out[-300:])


def test_the_curated_credential_owners_are_checked_too() -> None:
    """Curated claims obey the selected input directory and are validated."""
    reg = sandbox()
    creds = json.loads((reg / 'credentials.json').read_text())
    real_cred = creds['credentials'][0]['id']
    owners = reg.parent / 'config/credential_owners.json'

    def drive(rows: list[dict], fingerprint: str, label: str) -> None:
        owners.write_text(json.dumps({"owners": rows}, ensure_ascii=False),
                          encoding="utf-8")
        p = subprocess.run([PY, str(ROOT / "tools/validate_registry.py")], cwd=ROOT,
                           env=validator_fixture.environment(reg),
                           capture_output=True, text=True, timeout=300)
        out = p.stdout + p.stderr
        check(f"{label} is refused", p.returncode != 0, out[-200:])
        check(f"\u2026and the message says {fingerprint!r}", fingerprint in out,
              out[-300:])

    drive([{"credential": real_cred, "projects": [], "evidence": ""}],
          "carries no evidence", "a curated owner row with no evidence")
    drive([{"credential": "credential:invented/nothing", "projects": [],
            "evidence": "planted"}],
          "which no scan holds", "a curated row for a credential no scan holds")
    drive([{"credential": real_cred, "projects": ["not-a-project"],
            "evidence": "planted"}],
          "which the registry does not hold", "a curated row naming no project")


def test_selected_heroku_links_are_checked() -> None:
    reg = sandbox()
    path = reg.parent / 'config/heroku_links.json'
    valid = {'app':'fixture','project':validator_fixture.PROJECT,'evidence':'Synthetic verified link'}
    path.write_text(json.dumps({'links':[valid]}))
    rc, out = run(reg)
    check('selected valid Heroku claim passes', rc == 0, out[-300:])
    path.write_text(json.dumps({'links':[{**valid, 'project':'project:missing'}]}))
    rc, out = run(reg)
    check('selected stale Heroku claim is refused', rc != 0 and 'heroku_links names project' in out, out[-300:])


# ─────────────── the rules that need more than one edit ─────────────────

def test_a_snapshot_that_disagrees_with_the_registry_is_refused() -> None:
    """The Cloudflare zone snapshot and `domains.json` must name the same set."""
    reg = sandbox()
    # The fixture's own name for the file, which is what `config/settings.json`
    # selects as the `cloudflare_snapshot` source. A test that guessed the path
    # once never drove this rule at all, and the skip read as absent data.
    f = validator_fixture.snapshot_path(reg.parent)
    if not f.is_file():
        print("  SKIP  no zone snapshot in this workspace "
              "[covered: nothing else drives the snapshot rule — if this fires, "
              "the fixture no longer plants the snapshot]")
        return
    doc = json.loads(f.read_text(encoding="utf-8"))
    key = next((k for k in ("zones", "domains") if k in doc), None)
    check("the snapshot names its collection", bool(key), str(sorted(doc)))
    if not key:
        return
    # TWO SIDES, one rule. The rule is `snapshot mismatch missing=… extra=…`,
    # and this drove only `missing` — by popping a row — so it dropped every
    # assertion on a registry whose snapshot happens to be empty. `extra` needs
    # nothing to already be there: a planted zone with no matching domain fires
    # the same rule, carries the status and plan the loop below reads, and so
    # cannot fail for a neighbouring reason.
    if doc[key]:
        doc[key].pop()
        side = "a zone missing from the snapshot"
    else:
        doc[key].append({"domain_id": "domain:planted.invalid",
                         "status": "active", "plan": "free"})
        side = "a snapshot zone with no domain behind it"
    f.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    code, out = run(reg)
    check(f"{side} is refused", code != 0, out[-200:])
    # ATTRIBUTED, not merely red. A synthetic row could trip a neighbouring rule
    # and this assertion would have read that as success — the false green this
    # file's own `test_the_rule_count_is_watched` exists to prevent.
    check("…and the refusal names the snapshot rule",
          "snapshot mismatch" in out, out[-300:])
    for field, fingerprint in (("status", "snapshot zone has no status"),
                               ("plan", "snapshot zone has no plan")):
        reg = sandbox()
        f = validator_fixture.snapshot_path(reg.parent)
        doc = json.loads(f.read_text(encoding="utf-8"))
        doc["zones"][0][field] = ""
        f.write_text(json.dumps(doc), encoding="utf-8")
        code, out = run(reg)
        check(f"a snapshot zone with no {field} is refused", code != 0, out[-200:])
        check(f"…and named: {fingerprint!r}", fingerprint in out, out[-300:])


def test_a_registrar_export_that_disagrees_is_refused() -> None:
    """The rule that could not be tested at all until `RAW` was redirected."""
    reg = sandbox()
    raw = validator_fixture.export_path(reg.parent)
    check("the sandbox carries the registrar export", raw.is_file(),
          "without it the comparison cannot run, and the copy would be narrower "
          "than the repository")
    if not raw.is_file():
        return
    lines = raw.read_text(encoding="utf-8").splitlines()
    raw.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    code, out = run(reg)
    check("a domain missing from the export is refused", code != 0, out[-200:])
    check("and the message names the registrar comparison",
          "Namecheap mismatch" in out or "not_in_export" in out, out[-400:])


def test_the_validator_reads_the_sandbox_and_not_the_live_registry() -> None:
    """The defect this suite found: the export was resolved from the source tree.

    Driven rather than asserted about the source: break the sandbox's export and
    require the failure. If the validator were still reading the live CSV, the
    sandbox's broken one would go unnoticed and this would pass green — which is
    exactly how the bug survived.
    """
    src = (ROOT / "tools/validate_registry.py").read_text(encoding="utf-8")
    # The engine names the export by what it is — a user-selected source,
    # never a dated snapshot inside the source tree.
    check("the export resolves through paths", "RAW = paths.NAMECHEAP_EXPORT" in src,
          "not from the source tree")
    check("and so does the zone snapshot",
          'SNAPSHOT = paths.source_path("cloudflare_snapshot"' in src, "not from the source tree")
    reg = sandbox()
    raw = validator_fixture.export_path(reg.parent)
    if not raw.is_file():
        print("  SKIP  no export to corrupt "
              "[covered: the `paths.NAMECHEAP_EXPORT` assertion above IS the invariant — "
              "the bug was the validator reading the LIVE export while a "
              "fixture corrupted a sandbox copy — and this block demonstrates it "
              "end to end where the export exists]")
        return
    raw.write_text("this is not a csv the validator can read\n", encoding="utf-8")
    code, out = run(reg)
    check("corrupting the SANDBOX's export changes the verdict", code != 0,
          "the validator is still reading the live file")


def test_the_phantom_invariant_is_refused_both_ways() -> None:
    """`stale-remotes.json` records an address that MOVED, so the old name must
    not be a repository and the new one must be. If that inverts, the merge has
    admitted a repository belonging to nobody and this validator is the only step
    that can stop it reaching a commit.

    Not in the PLANTS table because each half needs a real id read out of the
    sandbox rather than a literal, and a fixture that hardcodes one is a fixture
    that breaks the day the repository is renamed.
    """
    for label, pick, fingerprint in (
            ("the old address still being a repository",
             lambda a, b: {"was": a, "now": b}, "phantom repository in the registry"),
            ("the new address not being one",
             lambda a, b: {"was": "gone/gone", "now": "nobody/nothing"},
             "which is not a repository in this registry"),
            ("a row naming no address pair at all",
             lambda a, b: {"was": "", "now": ""},
             "row names no address pair")):
        reg = sandbox()
        f = reg / "stale-remotes.json"
        if not f.is_file():
            # CREATED, not skipped. Every row this case asserts about is planted
            # three lines below, so a pre-existing file was never the fixture —
            # it was an accident of which machine ran the suite, and it dropped
            # three refusal messages on any registry without one.
            f.write_text('{"source_refs": ["SRC-FIXTURE"], "clones": []}\n', encoding="utf-8")
        repos = json.loads((reg / "repositories.json").read_text(
            encoding="utf-8"))["repositories"]
        row = pick(repos[0]["name_with_owner"], repos[1]["name_with_owner"])
        row.update(folder="x", path="/x")
        doc = json.loads(f.read_text(encoding="utf-8"))
        doc["clones"].append(row)
        f.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        code, out = run(reg)
        check(f"{label} is refused", code != 0, out[-200:])
        check(f"…and named: {fingerprint!r}", fingerprint in out, out[-300:])


def test_the_rule_count_is_watched() -> None:
    """A rule added without a plant should be visible, not silent."""
    import re
    src = (ROOT / "tools/validate_registry.py").read_text(encoding="utf-8")
    # A SIXTY-character window, not forty. At forty the harvest cut
    # `domain-liveness.json carries no source_r` mid-word, so neither
    # containment direction matched the fingerprint this file plants — the check
    # reported two rules as unwatched that it watches by name. The window has to
    # be wider than the longest fingerprint, or the coverage check measures its
    # own truncation.
    msgs = {m.strip() for m in re.findall(r'errors\.append\(f?"([^"{]{6,60})', src)}
    planted = {p[3] for p in PLANTS} | {"snapshot mismatch", "Namecheap mismatch",
                                        "snapshot zone has no status",
                                        "snapshot zone has no plan",
                                        "Namecheap fields differ",
                                        # Provenance of a measured file: driven by
                                        # tests/test_provenance.py, not here.
                                        "is measured by", "duplicate",
                                        # Planted in
                                        # `test_the_phantom_invariant_is_refused_both_ways`
                                        # rather than in the table: each needs a
                                        # real repository id read out of the
                                        # sandbox, and a hardcoded one breaks the
                                        # day that repository is renamed.
                                        "phantom repository in the registry",
                                        "stale-remotes.json names",
                                        "stale-remotes.json row names no address pair",
                                        # The three `credential_owners` rules,
                                        # driven in
                                        # `test_the_curated_credential_owners_are_checked_too`
                                        # rather than in the table: the file they
                                        # read sits in the workspace's config,
                                        # outside the registry.
                                        "credential_owners row for",
                                        "credential_owners names"}
    # Either direction: the harvested message is truncated at forty characters
    # while a fingerprint may be longer, so `p in m` alone reported four rules
    # as unplanted that this file plants by name.
    unplanted = sorted(m for m in msgs
                       if not any(p in m or m in p for p in planted))
    check("every error message is either planted here or listed as knowingly not",
          not unplanted, f"{unplanted} — a rule nobody has watched fail")
    check("and the count is what it was measured to be", len(msgs) >= 27,
          f"{len(msgs)} messages; 27 were counted when this suite was written")


if __name__ == "__main__":
    print("the validator — every rule, seen failing\n")
    for fn in (test_the_untouched_copy_passes,
               test_every_planted_violation_is_caught_and_named,
               test_a_snapshot_that_disagrees_with_the_registry_is_refused,
               test_a_registrar_export_that_disagrees_is_refused,
               test_the_validator_reads_the_sandbox_and_not_the_live_registry,
               test_the_phantom_invariant_is_refused_both_ways,
               test_the_rule_count_is_watched,
               test_the_curated_credential_owners_are_checked_too,
               test_selected_heroku_links_are_checked):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe gate that stops the tick has been watched refusing\033[0m")
