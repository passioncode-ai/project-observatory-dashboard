#!/usr/bin/env python3
"""`identifiers.py` — one project under many names, resolved without a guess.

Agents name the same thing differently: a store console calls an app by a
numeric id, a finance agent by a slug, a hosting platform by an app name, a
bot by a knowledge namespace. The operator curates `config/identifiers.json`
(alias -> canonical id, with the alias's owner), the emitter publishes it as
`known_as` relations, and `resolve` answers ANY of those names with the
canonical project or product. Everything runs on planted rows.
"""
from __future__ import annotations
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                # noqa: E402
import identifiers as ids                                           # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


PROJECTS = [{"id": "project:alpha-web", "name": "alpha-web"},
            {"id": "project:alpha-app", "name": "alpha-app"},
            {"id": "project:beta-api", "name": "beta-api"}]
PRODUCTS = [{"id": "product:alpha", "name": "Alpha", "kind": "curated",
             "members": [{"project": "project:alpha-web", "role": "site"},
                         {"project": "project:alpha-app", "role": "app"}],
             "domains": ["alpha.example"], "why": "one thing to a user"},
            {"id": "product:suggested-beta-example", "name": "beta.example", "kind": "suggested",
             "members": [{"project": "project:beta-api", "role": "other"}],
             "domains": ["beta.example"], "why": "shared domain"}]
RELATIONS = [
    {"id": "r1", "type": "implemented_by", "from": "project:alpha-web", "to": "repository:example-org/alpha-web"},
    {"id": "r2", "type": "implemented_by", "from": "project:alpha-app", "to": "repository:example-org/alpha-ios"},
    {"id": "r3", "type": "implemented_by", "from": "project:alpha-app", "to": "repository:example-org/alpha-android"},
    {"id": "r4", "type": "deployed_to", "from": "project:beta-api", "to": "heroku:beta-api-prod"},
    {"id": "r5", "type": "public_domain_of", "from": "domain:alpha.example", "to": "project:alpha-web"},
    {"id": "r6", "type": "part_of", "from": "project:alpha-web", "to": "product:alpha", "role": "site"},
    {"id": "r7", "type": "part_of", "from": "project:alpha-app", "to": "product:alpha", "role": "app"},
    # One repository claimed by two projects: a name that cannot be resolved alone.
    {"id": "r8", "type": "implemented_by", "from": "project:beta-api", "to": "repository:example-org/shared-lib"},
    {"id": "r9", "type": "implemented_by", "from": "project:alpha-web", "to": "repository:example-org/shared-lib"},
]


def curated(doc: dict | str) -> pathlib.Path:
    path = pathlib.Path(tmpdir.mkdtemp()) / "identifiers.json"
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return path


def row(target: str, owner: str = "store-agent", **extra) -> dict:
    return {"resolves_to": target, "owner": owner, "source": "planted row", "since": "2026-10-11", **extra}


def test_curated_aliases_become_known_as_edges_and_resolve_to_one_id() -> None:
    path = curated({"schema_version": 1, "aliases": {
        "appstore:app:1000000001": row("project:alpha-app"),
        "urn:finance:project:alpha": row("product:alpha", owner="finance-agent"),
        "bot:knowledge_project:alpha": row("product:alpha", owner="community-bot"),
    }})
    doc, edges, errors = ids.document(PROJECTS, PRODUCTS, RELATIONS, "2026-10-11", path=path)
    check("a well-formed file has no errors", not errors, str(errors))
    check("every alias is published", doc["totals"]["aliases"] == 3, str(doc["totals"]))
    check("each alias becomes one known_as edge from its canonical id, carrying the owner",
          {(e["from"], e["to"], e["owner"]) for e in edges} == {
              ("project:alpha-app", "appstore:app:1000000001", "store-agent"),
              ("product:alpha", "urn:finance:project:alpha", "finance-agent"),
              ("product:alpha", "bot:knowledge_project:alpha", "community-bot")}, str(edges))
    check("edge ids are unique", len({e["id"] for e in edges}) == len(edges))
    reg = ids.Registry(PROJECTS, PRODUCTS, RELATIONS, doc)
    got = ids.resolve("appstore:app:1000000001", reg)
    check("an alias resolves to its project", got.get("canonical") == "project:alpha-app"
          and got["kind"] == "project" and got["matched"] == {"query": "appstore:app:1000000001", "via": "alias"},
          json.dumps(got)[:300])
    check("the project answer names the products it is part of, with its role",
          got.get("products") == [{"id": "product:alpha", "role": "app"}], str(got.get("products")))
    check("and both of its repositories",
          got["members"][0]["repositories"] == ["repository:example-org/alpha-android",
                                                "repository:example-org/alpha-ios"], str(got["members"]))
    got = ids.resolve("urn:finance:project:alpha", reg)
    check("a urn-prefixed alias is an opaque name like any other",
          got.get("canonical") == "product:alpha" and got["kind"] == "product", json.dumps(got)[:300])
    members = {m["project"]: m for m in got["members"]}
    check("a product answers every member with its role, repositories, deployments and domains",
          members["project:alpha-web"]["role"] == "site"
          and members["project:alpha-web"]["domains"] == ["alpha.example"]
          and members["project:alpha-app"]["repositories"] == ["repository:example-org/alpha-android",
                                                               "repository:example-org/alpha-ios"],
          json.dumps(got["members"])[:400])
    check("and every alias that names it, with its owner",
          {(a["id"], a["owner"]) for a in got["aliases"]} == {
              ("urn:finance:project:alpha", "finance-agent"), ("bot:knowledge_project:alpha", "community-bot")},
          str(got["aliases"]))
    check("a full answer degrades nothing", got["degraded"] == [], str(got["degraded"]))


def test_derived_names_resolve_without_curation_and_ambiguity_is_never_guessed() -> None:
    reg = ids.Registry(PROJECTS, PRODUCTS, RELATIONS, ids.empty_document("2026-10-11"))
    for query, canonical, via in (("project:beta-api", "project:beta-api", "canonical"),
                                  ("product:alpha", "product:alpha", "canonical"),
                                  ("repository:example-org/alpha-ios", "project:alpha-app", "repository"),
                                  ("heroku:beta-api-prod", "project:beta-api", "deployment"),
                                  ("domain:alpha.example", "project:alpha-web", "domain")):
        got = ids.resolve(query, reg)
        check(f"{query} resolves through {via}", got.get("canonical") == canonical
              and got["matched"]["via"] == via, json.dumps(got)[:300])
    got = ids.resolve("repository:example-org/shared-lib", reg)
    check("a repository two projects claim is AMBIGUOUS, with both candidates, not the first",
          got.get("error") == "ambiguous"
          and sorted(got["candidates"]) == ["project:alpha-web", "project:beta-api"], json.dumps(got))
    for miss in ("project:alpha", "alpha-web", "appstore:app:1000000002", "Repository:example-org/alpha-ios"):
        got = ids.resolve(miss, reg)
        check(f"{miss!r} is not found — no prefix, case or near match", got.get("error") == "not-found",
              json.dumps(got)[:200])
    got = ids.resolve("product:suggested-beta-example", reg)
    check("a SUGGESTED product resolves, and says it is an inference",
          got.get("canonical") == "product:suggested-beta-example"
          and any("suggested" in d["reason"] for d in got["degraded"]), json.dumps(got)[:300])
    got = ids.resolve("sk-" + "a" * 40, reg)
    check("a credential-shaped query is not echoed back",
          got.get("error") == "not-found" and "a" * 40 not in json.dumps(got), json.dumps(got)[:200])


def test_a_conflict_is_a_finding_and_never_a_silent_overwrite() -> None:
    text = ('{"schema_version": 1, "aliases": {'
            '"store:app:7": {"resolves_to": "project:alpha-app", "owner": "a", "source": "s", "since": "2026-10-11"},'
            '"store:app:7": {"resolves_to": "project:beta-api", "owner": "b", "source": "s", "since": "2026-10-11"},'
            '"Ops:app:9": {"resolves_to": "project:alpha-app", "owner": "a", "source": "s", "since": "2026-10-11"},'
            '"ops:app:9": {"resolves_to": "project:beta-api", "owner": "b", "source": "s", "since": "2026-10-11"},'
            '"heroku:beta-api-prod": {"resolves_to": "project:alpha-web", "owner": "c", "source": "s", "since": "2026-10-11"},'
            '"repository:example-org/alpha-ios": {"resolves_to": "product:alpha", "owner": "c", "source": "s", "since": "2026-10-11"},'
            '"same:twice:1": {"resolves_to": "project:alpha-web", "owner": "a", "source": "s", "since": "2026-10-11"},'
            '"same:twice:1": {"resolves_to": "project:alpha-web", "owner": "a", "source": "s", "since": "2026-10-11"}'
            '}}')
    doc, edges, errors = ids.document(PROJECTS, PRODUCTS, RELATIONS, "2026-10-11", path=curated(text))
    check("conflicts are not shape errors: the emit goes on", not errors, str(errors))
    conflicted = {c["id"] for c in doc["conflicts"]}
    check("one name written twice with two targets is a conflict, and NEITHER row is published",
          "store:app:7" in conflicted and not any(a["id"] == "store:app:7" for a in doc["aliases"]),
          json.dumps(doc["conflicts"])[:300])
    check("two spellings that differ only in case are one name, so two targets conflict",
          "ops:app:9" in {c["id"].casefold() for c in doc["conflicts"]}
          and not any(a["id"].casefold() == "ops:app:9" for a in doc["aliases"]),
          json.dumps(doc["conflicts"])[:300])
    check("a curated alias that contradicts a measured deployment is a conflict",
          "heroku:beta-api-prod" in conflicted, json.dumps(doc["conflicts"])[:400])
    check("but naming the PRODUCT a measured project belongs to does not contradict it",
          "repository:example-org/alpha-ios" not in conflicted
          and any(a["id"] == "repository:example-org/alpha-ios" for a in doc["aliases"]), str(doc["aliases"]))
    check("the same row written twice is published once and named as a duplicate",
          sum(a["id"] == "same:twice:1" for a in doc["aliases"]) == 1
          and any("same:twice:1" in d["reason"] for d in doc["degraded"]), str(doc["degraded"]))
    check("no edge is drawn for a conflicted alias",
          not any(e["to"] in ("store:app:7", "heroku:beta-api-prod") for e in edges), str(edges))
    reg = ids.Registry(PROJECTS, PRODUCTS, RELATIONS, doc)
    got = ids.resolve("store:app:7", reg)
    check("resolving a conflicted name says so instead of picking one",
          got.get("error") == "conflict" and sorted(got["candidates"]) == ["project:alpha-app", "project:beta-api"],
          json.dumps(got)[:300])
    got = ids.resolve("heroku:beta-api-prod", reg)
    check("a measured name with a contradicting curated claim is a conflict too, not the measurement silently",
          got.get("error") == "conflict", json.dumps(got)[:300])


def test_a_malformed_file_is_refused_whole_and_an_unknown_target_is_dropped_and_named() -> None:
    doc, edges, errors = ids.document(PROJECTS, PRODUCTS, RELATIONS, "2026-10-11", path=curated({
        "schema_version": 1, "aliases": {
            "project:alpha-web": row("project:alpha-app"),
            "noprefix": row("project:alpha-app"),
            "has space:x:1": row("project:alpha-app"),
            "store:app:1": row("repository:example-org/x"),
            "store:app:2": {"resolves_to": "project:alpha-app"},
            "store:app:3": row("project:alpha-app", since="yesterday"),
            "store:app:4": row("project:alpha-app", colour="blue"),
        }}))
    # A name with whitespace is not echoable, so its refusal names it by length.
    for needle in ("project:alpha-web", "noprefix", "<13 characters, not shown>", "store:app:1", "store:app:2",
                   "store:app:3", "store:app:4"):
        check(f"a shape error names {needle}", any(needle in e for e in errors), str(errors))
    check("a refused file publishes nothing", not edges and not doc["aliases"], str(doc["aliases"]))
    doc, edges, errors = ids.document(PROJECTS, PRODUCTS, RELATIONS, "2026-10-11", path=curated({
        "schema_version": 1, "aliases": {"store:app:5": row("project:gone")}}))
    check("an alias for a project this registry does not hold is dropped, named, and does not stop the emit",
          not errors and not doc["aliases"] and any("project:gone" in d["reason"] for d in doc["degraded"]),
          json.dumps(doc)[:300])
    doc, edges, errors = ids.document(PROJECTS, PRODUCTS, RELATIONS, "2026-10-11",
                                      path=pathlib.Path(tmpdir.mkdtemp()) / "absent.json")
    check("no curated file is an empty, valid document", not errors and doc["aliases"] == [] and not edges,
          json.dumps(doc)[:200])
    doc, edges, errors = ids.document(PROJECTS, PRODUCTS, RELATIONS, "2026-10-11", path=curated("{not json"))
    check("an unreadable file is refused, not read as empty", errors and "read" in errors[0], str(errors))
    tok = "ghp_" + "b" * 36
    doc, edges, errors = ids.document(PROJECTS, PRODUCTS, RELATIONS, "2026-10-11", path=curated({
        "schema_version": 1, "aliases": {f"store:token:{tok}": row("project:alpha-app")}}))
    check("a credential-shaped alias is refused and not quoted", errors and tok not in " ".join(errors),
          str(errors))


def test_an_alias_proposal_has_one_shape() -> None:
    good = {"id": "store:app:42", "owner": "store-agent", "source": "store console"}
    check("a well-formed alias row has no problem", ids.alias_problem(good) == "", ids.alias_problem(good))
    for bad, needle in (({"id": "store:app:42"}, "owner"),
                        ({**good, "id": "project:alpha-web"}, "canonical"),
                        ({**good, "extra": 1}, "extra"),
                        ({**good, "id": "x"}, "system"),
                        ("store:app:42", "object")):
        problem = ids.alias_problem(bad)
        check(f"{str(bad)[:40]} is refused naming {needle}", needle in problem, problem)
    merged = ids.merge_accepted({"schema_version": 1, "aliases": {"store:app:1": row("project:alpha-web")}},
                                [good], target="project:alpha-app", on="2026-10-11",
                                provenance={"why": "the console says so", "proposed_by": "p-1", "evidence": []})
    check("an accepted alias lands as a curated row with its target and provenance",
          merged["aliases"]["store:app:42"]["resolves_to"] == "project:alpha-app"
          and merged["aliases"]["store:app:42"]["owner"] == "store-agent"
          and merged["aliases"]["store:app:42"]["why"] == "the console says so"
          and "store:app:1" in merged["aliases"], json.dumps(merged)[:300])
    try:
        ids.merge_accepted(merged, [{**good, "owner": "other"}], target="project:beta-api", on="2026-10-11",
                           provenance={"why": "w", "proposed_by": "p-2", "evidence": []})
        raised = False
    except ids.AliasConflict:
        raised = True
    check("accepting an alias that already names something else is refused, never overwritten", raised)


def test_the_emit_publishes_aliases_as_edges() -> None:
    """The real emitter over a synthetic estate, with its own private workspace. The
    validator's alias rules are planted one by one in test_validator_rules."""
    import subprocess
    import workspace
    from emitter_fixture import environment, seed
    root = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-identifiers-"))
    seed(root)
    workspace.initialize(root / "home", identities=False)
    config, reg = root / "home" / "config", root / "registry"
    env = {**environment(root), "OBSERVATORY_HOME": str(root / "home")}
    py = sys.executable

    def emit() -> subprocess.CompletedProcess:
        return subprocess.run([py, "collectors/emit_registry.py", str(root / "raw")], cwd=ROOT,
                              capture_output=True, text=True, timeout=300, env=env)

    (config / "products.json").write_text(json.dumps({"roles": ["site", "api"], "products": {
        "product:fixture": {"name": "Fixture", "why": "planted", "members": {"project:fixture-a": "site"}}}}),
        encoding="utf-8")
    (config / "identifiers.json").write_text(json.dumps({"schema_version": 1, "aliases": {
        "store:app:1": row("project:fixture-a"),
        "urn:finance:project:fixture": row("product:fixture", owner="finance-agent"),
        "repository:fixture/service": row("project:fixture-b", owner="confused-agent"),
    }}), encoding="utf-8")
    p = emit()
    check("the emit accepts a curated identifiers file", p.returncode == 0, p.stderr[-400:])
    rels = json.loads((reg / "relations.json").read_text(encoding="utf-8"))
    known = {(r["from"], r["to"], r.get("owner")) for r in rels["relations"] if r["type"] == "known_as"}
    check("each published alias is a known_as edge with its owner",
          known == {("project:fixture-a", "store:app:1", "store-agent"),
                    ("product:fixture", "urn:finance:project:fixture", "finance-agent")}, str(known))
    check("the relation type explains itself", "known_as" in rels["relation_types"])
    doc = json.loads((reg / "identifiers.json").read_text(encoding="utf-8"))
    check("an alias contradicting a measured repository is a published conflict, not an edge",
          [c["id"] for c in doc["conflicts"]] == ["repository:fixture/service"], json.dumps(doc["conflicts"]))

    before = (reg / "relations.json").read_bytes()
    (config / "identifiers.json").write_text(json.dumps({"aliases": {"project:x": row("project:fixture-a")}}),
                                             encoding="utf-8")
    p = emit()
    check("a malformed identifiers file stops the emit, naming the file",
          p.returncode != 0 and "identifiers.json" in p.stderr, p.stderr[-300:])
    check("before anything it would have replaced",
          (reg / "relations.json").read_bytes() == before)

    (config / "identifiers.json").unlink()
    p = emit()
    known = [r for r in json.loads((reg / "relations.json").read_text(encoding="utf-8"))["relations"]
             if r["type"] == "known_as"]
    check("an alias removed from the file loses its edge on the next emit — known_as is rebuilt, not kept",
          p.returncode == 0 and not known, str(known)[:200])


def test_the_review_of_pr_203_each_rule_on_its_own_row() -> None:
    """One check per finding of the #203 review (project-observatory-3d, 2026-10-11)."""
    # 1. A write reads the table with the duplicate-key hook and refuses duplicates.
    text = ('{"schema_version": 1, "aliases": {'
            '"store:app:7": {"resolves_to": "project:alpha-app", "owner": "a", "source": "s", "since": "2026-10-11"},'
            '"store:app:7": {"resolves_to": "project:beta-api", "owner": "b", "source": "s", "since": "2026-10-11"}}}')
    try:
        ids.load_table(curated(text))
        refused = False
    except ids.AliasConflict as exc:
        refused = "store:app:7" in str(exc)
    check("1: a table with a name written twice is refused for a write, not collapsed to the last row", refused)
    table = ids.load_table(curated({"schema_version": 1, "aliases": {"store:app:1": row("project:alpha-web")}}))
    check("1: a clean table loads without the hook's bookkeeping",
          "__pairs__" not in json.dumps(table) and table["aliases"]["store:app:1"]["owner"] == "store-agent",
          json.dumps(table)[:200])
    check("1: an absent table loads as empty", ids.load_table(pathlib.Path(tmpdir.mkdtemp()) / "none.json") == {})

    # 2. Accepting the same name with the same target never rewrites the curated row.
    curated_row = row("project:alpha-app", owner="operator-curated", why="the operator wrote it")
    merged = ids.merge_accepted({"schema_version": 1, "aliases": {"store:app:42": curated_row}},
                                [{"id": "store:app:42", "owner": "agent-x", "source": "a proposal"}],
                                target="project:alpha-app", on="2026-12-01",
                                provenance={"why": "agent says", "proposed_by": "p-9", "evidence": []})
    check("2: an operator-curated row survives an accepted proposal byte for byte",
          merged["aliases"]["store:app:42"] == curated_row, json.dumps(merged["aliases"])[:300])
    merged = ids.merge_accepted({"schema_version": 1, "aliases": {"Store:App:42": curated_row}},
                                [{"id": "store:app:42", "owner": "agent-x", "source": "a proposal"}],
                                target="project:alpha-app", on="2026-12-01",
                                provenance={"why": "w", "proposed_by": "p-9", "evidence": []})
    check("2: and so does its other spelling — no second row appears",
          list(merged["aliases"]) == ["Store:App:42"], str(list(merged["aliases"])))

    # 3. The canonical namespaces are reserved whatever the case, and never case-matched.
    check("3: `Project:foo` is refused as an alias", "canonical" in ids.id_problem("Project:foo"),
          ids.id_problem("Project:foo"))
    check("3: so is `PRODUCT:foo`", "canonical" in ids.id_problem("PRODUCT:foo"))
    planted = {"schema_version": 1, "aliases": [{"id": "Project:alpha", "resolves_to": "project:alpha-web",
                                                 "owner": "x", "source": "s", "since": "2026-10-11"}],
               "conflicts": []}
    reg = ids.Registry(PROJECTS, PRODUCTS, RELATIONS, planted)
    check("3: even a planted canonical-looking alias is not reached through case folding",
          ids.resolve("project:alpha", reg).get("error") == "not-found", json.dumps(ids.resolve("project:alpha", reg)))

    # 4. A curated alias is compared with measured names without case.
    doc, _edges, errors = ids.document(PROJECTS, PRODUCTS, RELATIONS, "2026-10-11", path=curated({
        "schema_version": 1, "aliases": {"domain:ALPHA.example": row("project:beta-api")}}))
    check("4: a measured domain in another case still makes a contradicting alias a conflict",
          not errors and [c["id"] for c in doc["conflicts"]] == ["domain:ALPHA.example"]
          and not doc["aliases"], json.dumps(doc)[:300])

    # 5. A trailing newline is not a name, and not a date.
    check("5: `store:x` followed by a newline is refused", ids.id_problem("store:x\n") != "")
    doc, _e, errors = ids.document(PROJECTS, PRODUCTS, RELATIONS, "2026-10-11", path=curated({
        "schema_version": 1, "aliases": {"store:app:1": row("project:alpha-app", since="2026-10-11\n")}}))
    check("5: a date followed by a newline is refused", any("since" in e for e in errors), str(errors))

    # 6. Two aliases whose slugs agree still get two edges.
    doc, edges, errors = ids.document(PROJECTS, PRODUCTS, RELATIONS, "2026-10-11", path=curated({
        "schema_version": 1, "aliases": {"store:a_b": row("project:alpha-app"),
                                         "store:a-b": row("project:alpha-app")}}))
    check("6: `a_b` and `a-b` are two edges with two ids",
          not errors and len(edges) == 2 and len({e["id"] for e in edges}) == 2, str([e["id"] for e in edges]))

    # 8. The tool agrees with the published input schema (`^\\S+$`): whitespace is refused.
    reg = ids.Registry(PROJECTS, PRODUCTS, RELATIONS, ids.empty_document("2026-10-11"))
    for spaced in (" project:beta-api", "project:beta-api ", "project:beta-api\n"):
        check(f"8: {spaced!r} is refused rather than trimmed", ids.resolve(spaced, reg).get("error") == "not-found")


if __name__ == "__main__":
    print("identifiers — many names, one canonical id, never a guess\n")
    for fn in (test_curated_aliases_become_known_as_edges_and_resolve_to_one_id,
               test_derived_names_resolve_without_curation_and_ambiguity_is_never_guessed,
               test_a_conflict_is_a_finding_and_never_a_silent_overwrite,
               test_a_malformed_file_is_refused_whole_and_an_unknown_target_is_dropped_and_named,
               test_an_alias_proposal_has_one_shape,
               test_the_emit_publishes_aliases_as_edges,
               test_the_review_of_pr_203_each_rule_on_its_own_row):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    print("\033[32mall passed\033[0m")
