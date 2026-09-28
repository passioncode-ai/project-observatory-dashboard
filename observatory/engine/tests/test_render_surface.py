#!/usr/bin/env python3
"""What a fabric host reads to render one project, and what it costs.

The original ask for this project ends with rendering its dashboard, or the data
of one particular project, inside a fabric host. Two things stood in the way,
and only one of them was obvious.

* **Volume.** An unpaged estate survey grows with the estate — on the
  installation it was measured on, about 120 KB (some 30,000 tokens) for 150
  projects, against under 3 KB for one project. A host that wanted a single
  project had to swallow the estate first just to learn the ids.

* **Addressability.** There was no way to name one project. The pinned contract
  defines no rendering capability, no resource concept and no pagination, so an
  `estate.render` capability would have been invented contract surface — a host
  compiling the manifest would find a capability the contract cannot describe.
  MCP's resource templates already give one URI per subject with a declared
  media type, which is exactly what a renderer needs.

The rule this file also holds: **no silent cap.** `limit` has no default, and a
paged answer keeps `counts` describing the whole scope, so a caller can always
tell a page from an estate.

The synthetic estate holds eight projects, so the size assertions are stated as
proportions of that estate rather than as the byte counts measured on a large
one: a page grows with its limit, and one project costs a fraction of all eight.
"""
from __future__ import annotations
import asyncio, importlib.util, json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def load_server():
    """Registered in `sys.modules` on purpose.

    pydantic resolves a function's annotations against
    `sys.modules[fn.__module__].__dict__`, and a module created by
    `spec_from_file_location` is not there — so `dict[str, Any]` on a resource
    function raised `NameError: name 'Any' is not defined` at import. The server
    is fine; the loader was lying about the environment.
    """
    spec = importlib.util.spec_from_file_location("srv_render", ROOT / "mcp/server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["srv_render"] = mod
    spec.loader.exec_module(mod)
    return mod


def body(out) -> str:
    first = out[0]
    content = first.content if hasattr(first, "content") else first
    return content if isinstance(content, str) else getattr(content, "text", str(content))


# ─────────────────────────── paging ─────────────────────────────────────

def test_the_volume_problem_is_real_and_the_page_solves_it() -> None:
    import survey
    whole = survey.survey({"kind": "estate"})
    one = survey.survey({"kind": "estate"}, limit=1)
    page = survey.survey({"kind": "estate"}, limit=5)
    big, small, least = len(json.dumps(whole)), len(json.dumps(page)), len(json.dumps(one))
    n = whole["counts"]["projects"]
    check("the fixture has enough projects to page", n >= 6, str(n))
    # PORTED-DIVERGED: proportions of an eight-project estate instead of the
    # 100 KB threshold measured on a large one.
    check("an unpaged estate is the expensive answer", big > least * 3,
          f"{big:,} bytes for {n} projects vs {least:,} for one")
    check("and a page costs less than the estate it is cut from", small < big,
          f"{small:,} vs {big:,}")
    check("the page returns exactly what was asked for", len(page["projects"]) == 5,
          str(len(page["projects"])))


def test_counts_describe_the_scope_not_the_page() -> None:
    """A count that shrank with the page would be a lie about the estate."""
    import survey
    whole = survey.survey({"kind": "estate"})
    page = survey.survey({"kind": "estate"}, limit=3)
    check("counts.projects is the total, not the page size",
          page["counts"]["projects"] == whole["counts"]["projects"] > 3,
          f'{page["counts"]["projects"]} vs {whole["counts"]["projects"]}')
    check("counts.repositories does not shrink either",
          page["counts"]["repositories"] == whole["counts"]["repositories"],
          f'{page["counts"]["repositories"]} vs {whole["counts"]["repositories"]}')
    check("and the page declares that more remain", bool(page.get("nextCursor")),
          "a truncated answer with no cursor is a silent cap")


def test_the_walk_is_complete_and_never_repeats() -> None:
    """The property that matters: paging must not lose or duplicate a project."""
    import survey
    # THE PIN IS PASSED AND DOES NOTHING, which is known rather than assumed:
    # `as_of_scan_id` filters nothing, so this walk is
    # stable because the registry does not change during the test — not because
    # of the pin. Kept because it is what a host compiling the input schema
    # would send, so the walk is exercised the way a caller will drive it.
    pinned = survey.survey({"kind": "estate"}).get("scanId")
    seen: list[str] = []
    cursor, rounds = None, 0
    while rounds < 100:
        rounds += 1
        p = survey.survey({"kind": "estate"}, limit=3, cursor=cursor,
                          as_of_scan_id=pinned)
        seen.extend(x["id"] for x in p["projects"])
        cursor = p.get("nextCursor")
        if not cursor:
            break
    whole = [x["id"] for x in survey.survey({"kind": "estate"})["projects"]]
    check("the walk terminates", cursor is None, f"{rounds} rounds and still going")
    check("it returns every project exactly once", seen == sorted(whole),
          f"{len(seen)} walked vs {len(whole)} present; "
          f"duplicates={len(seen) - len(set(seen))}")


def test_no_default_page_size() -> None:
    """A default would truncate every existing caller silently."""
    import inspect
    import survey
    sig = inspect.signature(survey.survey)
    check("limit defaults to None", sig.parameters["limit"].default is None)
    check("cursor defaults to None", sig.parameters["cursor"].default is None)
    whole = survey.survey({"kind": "estate"})
    check("so an unpaged call still returns everything",
          len(whole["projects"]) == whole["counts"]["projects"],
          f'{len(whole["projects"])} of {whole["counts"]["projects"]}')
    check("and carries no cursor", "nextCursor" not in whole, str(whole.get("nextCursor")))
    try:
        survey.survey({"kind": "estate"}, limit=0)
        check("a limit below 1 is refused rather than answered empty", False, "accepted")
    except ValueError as exc:
        check("a limit below 1 is refused rather than answered empty", True, str(exc))


def test_both_schemas_declare_the_paging_surface() -> None:
    """`additionalProperties: false` on both sides makes this mandatory."""
    inp = json.loads((ROOT / "fabric/schemas/capability-input.schema.json")
                     .read_text(encoding="utf-8"))
    out = json.loads((ROOT / "fabric/schemas/capability-output.schema.json")
                     .read_text(encoding="utf-8"))
    check("the input schema is closed, so an undeclared parameter is unreachable",
          inp.get("additionalProperties") is False)
    for prop in ("limit", "cursor"):
        check(f"input declares {prop}", prop in inp["properties"])
    check("the output schema is closed too", out.get("additionalProperties") is False)
    check("output declares nextCursor", "nextCursor" in out["properties"])

    # Driven: a paged answer must validate against the schema it claims to obey.
    import survey
    try:
        import jsonschema
    except ImportError:
        print("  SKIP  jsonschema is not installed here")
        return
    paged = survey.survey({"kind": "estate"}, limit=2)
    try:
        jsonschema.validate(paged, out)
        check("and a paged answer validates against it", True)
    except Exception as exc:
        check("and a paged answer validates against it", False, str(exc)[:200])


# ─────────────────────────── resources ──────────────────────────────────

def test_one_uri_per_project() -> None:
    m = load_server()

    async def go():
        statics = {str(r.uri): r.mime_type for r in await m.server.list_resources()}
        templates = {r.uri_template: r.mime_type
                     for r in await m.server.list_resource_templates()}
        return statics, templates

    statics, templates = asyncio.run(go())
    check("the estate has an address", "observatory://estate" in statics, str(statics))
    check("served as JSON", statics.get("observatory://estate") == "application/json")
    check("the dashboard has one too", "observatory://dashboard" in statics, str(statics))
    check("served as HTML so a host can RENDER it rather than read it",
          statics.get("observatory://dashboard") == "text/html")
    check("and a project is a template, one URI per project",
          "observatory://project/{project_id}" in templates, str(templates))


def test_reading_one_project_costs_a_fraction_of_the_estate() -> None:
    m = load_server()
    import survey
    pid = survey.survey({"kind": "estate"}, limit=1)["projects"][0]["id"]

    async def go():
        one = body(await m.server.read_resource(f"observatory://project/{pid}"))
        estate = body(await m.server.read_resource("observatory://estate"))
        missing = body(await m.server.read_resource("observatory://project/project:nope"))
        return one, estate, missing

    one, estate, missing = asyncio.run(go())
    doc = json.loads(one)
    check("the project resource answers with that project",
          doc.get("project", {}).get("id") == pid, str(doc)[:160])
    check("it carries a degraded list, as every answer here does",
          isinstance(doc.get("degraded"), list), str(sorted(doc)))
    check("and its evidence", bool(doc.get("evidence")), str(doc.get("evidence"))[:80])
    # PORTED-DIVERGED: a fraction of an eight-project estate, where the large
    # estate this was measured on made it a fiftieth.
    check("one project is far cheaper than the estate", len(one) * 2 < len(estate),
          f"{len(one):,} vs {len(estate):,}")
    check("an unknown project is a typed answer, not a blank one",
          json.loads(missing).get("error") == "unknown project", missing[:160])


def test_the_dashboard_resource_does_not_pretend() -> None:
    """An empty page would read as 'the estate has nothing to show'."""
    m = load_server()

    async def go():
        return body(await m.server.read_resource("observatory://dashboard"))

    html = asyncio.run(go())
    check("it returns a document", html.lstrip().startswith("<!doctype html"), html[:60])
    src = (ROOT / "mcp/server.py").read_text(encoding="utf-8")
    check("and says so when the page was never built",
          "The dashboard has not been built on this machine" in src,
          "a blank page is a false claim about the estate")


def test_the_resources_do_not_claim_contract_coverage() -> None:
    """The honest half: they are protocol surface, and the manifest says nothing."""
    manifest = json.loads((ROOT / "fabric-agent.json").read_text(encoding="utf-8"))
    check("the manifest declares no resource field",
          not any("resource" in k.lower() for k in manifest), str(sorted(manifest)))
    check("nor a render capability",
          not any("render" in c["id"] for c in manifest["capabilities"]),
          str([c["id"] for c in manifest["capabilities"]]))
    conf = (ROOT / "fabric/FABRIC-CONFORMANCE.md").read_text(encoding="utf-8")
    check("and the conformance report states they are outside the pinned contract",
          "protocol surface rather than contract" in conf)
    check("the revision was bumped with the surface",
          f"revision {manifest['provider']['revision']}" in conf,
          f"manifest says {manifest['provider']['revision']}")


if __name__ == "__main__":
    print("the render surface — one URI per project, and what a page costs\n")
    for fn in (test_the_volume_problem_is_real_and_the_page_solves_it,
               test_counts_describe_the_scope_not_the_page,
               test_the_walk_is_complete_and_never_repeats,
               test_no_default_page_size,
               test_both_schemas_declare_the_paging_surface,
               test_one_uri_per_project,
               test_reading_one_project_costs_a_fraction_of_the_estate,
               test_the_dashboard_resource_does_not_pretend,
               test_the_resources_do_not_claim_contract_coverage):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma host can render one project without reading the estate\033[0m")
