#!/usr/bin/env python3
"""Inputs the schema permits and the estate has never produced.

`fabric/schemas/capability-input.schema.json` is the list of what a conforming
host is ALLOWED to send. The fabric probes drive the shapes the estate happens to
produce; nothing drove the rest. A sweep of the remaining surface found that most
of it already behaves:

* An unknown project id answers `counts.projects: 0` while a REAL but empty
  project answers `1` with zero repositories: the two are distinguishable, and
  registries do contain projects in the second state.
* `scope: {kind: "project"}` with no `value` is permitted by the schema (its
  `required` lists only `kind`, and the prose says value is needed for two of
  the three kinds). `mcp/server.py` answers a typed `error: missing value` with
  a hint rather than raising, because raising "tells a caller that the server
  broke rather than that the argument was wrong".
* `limit` at 1 and at the schema's maximum of 200 both page correctly, and a
  cursor past the end returns an empty page with no `nextCursor`.

**One input was read as something it did not say.** The cursor filter is
`p["id"] > cursor`, so a string sorting before every id (anything not starting
with `project:`) passes the whole list and the walk silently restarts.
`cursor="!!! not an id !!!"` with `limit=5` returned page one again, so a host
holding a stale or corrupted cursor would receive projects it had already walked
with nothing to distinguish that from progress.

Reported rather than refused, because the answer is still useful and this file
reports everywhere else. And the check is the SHAPE, not membership: a
well-formed cursor whose project was deleted between two pages is legitimate and
the walk must continue after it.
"""
   
from __future__ import annotations
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup
portable_setup()
sys.path.insert(0, str(ROOT / "mcp"))

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def schema() -> dict:
    return json.loads(
        (ROOT / "fabric/schemas/capability-input.schema.json").read_text(encoding="utf-8"))


def cursor_note(answer: dict) -> list[str]:
    return [d["reason"] for d in answer.get("degraded") or [] if d.get("source") == "cursor"]


# ─────────── the cursor that names no position ─────────────────────────

def test_a_cursor_that_is_not_an_id_is_reported() -> None:
    import survey
    got = survey.survey({"kind": "estate"}, limit=3, cursor="!!! not an id !!!")
    check("the answer still comes", len(got.get("projects") or []) > 0,
          "a degradation, not a refusal — the page is still useful")
    notes = cursor_note(got)
    check("and the cursor is named as naming nothing", bool(notes), str(got.get("degraded"))[:200])
    if notes:
        check("with the warning that a walk will repeat itself",
              "repeat" in notes[0], notes[0][:160])


def test_a_well_formed_cursor_is_not_second_guessed() -> None:
    """A project deleted between two pages leaves a cursor that is a perfectly
    good position and matches nothing. The walk must continue after it in
    silence — reporting that would cry wolf on the commonest legitimate case."""
    import survey
    real = survey.survey({"kind": "estate"}, limit=3, cursor=__import__("test_portable_mcp").PROJECT_ID)
    gone = survey.survey({"kind": "estate"}, limit=3,
                         cursor="project:deleted-between-pages")
    check("an existing id is not reported", not cursor_note(real), str(cursor_note(real)))
    check("and neither is a well-formed id nobody has",
          not cursor_note(gone), str(cursor_note(gone)))


def test_a_cursor_past_the_end_is_an_empty_page_not_an_error() -> None:
    import survey
    got = survey.survey({"kind": "estate"}, limit=5, cursor="zzzzzzzz")
    check("the page is empty", len(got.get("projects") or []) == 0,
          str(len(got.get("projects") or [])))
    check("there is no next cursor", got.get("nextCursor") is None, str(got.get("nextCursor")))
    check("and the total still describes the whole scope",
          got["counts"]["projects"] > 0, str(got["counts"]))


# ─────────── the scopes the schema allows ──────────────────────────────

def test_an_unknown_subject_is_distinguishable_from_an_empty_one() -> None:
    """The conflation this repository removes everywhere: it is NOT here.

    A project that exists and holds nothing answers with itself; an id nobody
    has answers with nothing. Registries do hold projects with no folder and no
    repository, so the first state is a real one."""
    import survey
    import paths
    projects = json.loads(
        (paths.REGISTRY / "projects.json").read_text(encoding="utf-8"))["projects"]
    empty = next((p["id"] for p in projects
                  if not (p.get("local_folders") or [])), None)
    absent = survey.survey({"kind": "project", "value": "project:no-such-thing-at-all"})
    check("an id nobody has answers with no project",
          absent["counts"]["projects"] == 0, str(absent["counts"]))
    if empty is None:
        print("  NOTE  every project on this estate has a folder, so the "
              "empty-but-real state is not available here "
              "[covered: the absent case above is the half that could conflate]")
        return
    real = survey.survey({"kind": "project", "value": empty})
    check(f"a real project with nothing in it answers with itself ({empty})",
          real["counts"]["projects"] == 1, str(real["counts"]))
    check("so the two are not the same answer",
          real["counts"] != absent["counts"],
          "zero for both would make 'no such project' unreadable")


def test_the_wire_refuses_a_scope_the_schema_permits_but_cannot_mean() -> None:
    """`required` lists only `kind`, so `{kind: "project"}` is schema-valid and
    means nothing. The server answers a typed error rather than raising."""
    sch = schema()["properties"]["scope"]
    check("the schema really does permit it",
          sch.get("required") == ["kind"],
          f"required={sch.get('required')} — if `value` became required this "
          f"case is stale and the server's guard is belt without braces")
    import importlib.util
    spec = importlib.util.spec_from_file_location("srv_inputs", ROOT / "mcp/server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["srv_inputs"] = mod
    spec.loader.exec_module(mod)
    for kind in ("project", "owner"):
        got = mod._scope_error(kind, None)
        check(f"kind={kind} with no value is a typed error",
              isinstance(got, dict) and got.get("error") == "missing value", str(got))
        check(f"and it tells the caller what {kind} takes",
              kind in (got or {}).get("hint", ""), str(got))
    check("while estate needs no value", mod._scope_error("estate", None) is None,
          str(mod._scope_error("estate", None)))


def test_a_one_of_answer_is_cut_by_its_own_branch() -> None:
    """`project.resolve` publishes a `oneOf` (answer | refusal), each closed. The shaper
    once cut only `type: object` with `properties`, so a oneOf passed through uncut."""
    import server
    import jsonschema
    out_schema = json.loads((ROOT / "fabric/schemas/project-resolve-output.schema.json").read_text(encoding="utf-8"))
    answer = {"canonical": "project:alpha", "kind": "project", "name": "alpha", "matched": {"query": "x:1", "via": "alias"},
              "members": [{"project": "project:alpha", "role": None, "repositories": [], "deployments": [],
                           "domains": [], "planted": 1}],
              "aliases": [], "domains": [], "degraded": [], "products": [], "planted": True}
    dropped: set[str] = set()
    cut = server._published_shape(out_schema, answer, dropped)
    check("an extra key on the answer is dropped and named", "planted" not in cut and "planted" in dropped,
          str(sorted(dropped)))
    check("and inside a $ref'd member too", "planted" not in cut["members"][0] and "members.planted" in dropped,
          str(sorted(dropped)))
    errors = [e.message for e in jsonschema.Draft202012Validator(out_schema).iter_errors(cut)]
    check("the cut answer validates against the published schema", not errors, str(errors)[:200])
    refusal = {"error": "not-found", "query": "x:1", "hint": "h", "degraded": [], "planted": True}
    dropped = set()
    cut = server._published_shape(out_schema, refusal, dropped)
    check("a refusal is cut by the refusal branch", "planted" not in cut and cut["error"] == "not-found",
          str(cut))


if __name__ == "__main__":
    print("wire inputs — what a host may send and the estate never has\n")
    for fn in (test_a_cursor_that_is_not_an_id_is_reported,
               test_a_well_formed_cursor_is_not_second_guessed,
               test_a_cursor_past_the_end_is_an_empty_page_not_an_error,
               test_an_unknown_subject_is_distinguishable_from_an_empty_one,
               test_the_wire_refuses_a_scope_the_schema_permits_but_cannot_mean,
               test_a_one_of_answer_is_cut_by_its_own_branch):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma permitted input gets an answer or a reason, never a silent guess\033[0m")
