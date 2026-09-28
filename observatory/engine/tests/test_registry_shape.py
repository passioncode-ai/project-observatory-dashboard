#!/usr/bin/env python3
"""The canonical fact base has no published shape, and readers guess.

The registry calls itself "typed facts", and nothing published those types: the
store's tables and the MCP output shapes are documented, and
`tools/validate_registry.py` checks invariants — ids unique, domains lowercase —
not the set of fields a record carries. So the shape lived only in the emitting
code and in the data, and every reader inferred it. Five such inferences were
wrong in two iterations (`c["name"]` for `nwo`, `rel["repository_id"]` where
relations carry `from` and `to`, and three more of the same kind).

**Generated, never hand-written.** A hand-listed field table is the next thing to
drift. The shape is derived FROM the data, so it cannot disagree with it, and a
rule fails when the published copy is stale.

**Optional keys must say how optional.** A field present on a handful of records
listed beside `id` and `host` would teach a reader it is always there. Every
field carries the count of records holding it.

Driven against the synthetic workspace registry, which is emitted by the engine
itself. That registry exposed a real defect: `repositories.json` there carries
more `degraded` notices than repositories, and the shape tool — which took "the
longest list" as the records — described the notices. The published shape
document lives in the original documentation archive, which this distribution
does not ship, so its render and stamp are driven here instead of its copy.
"""
from __future__ import annotations
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def shapes():
    import registry_shape
    fn = getattr(registry_shape, "shapes", None)
    return None if fn is None else fn()


# ─────────── the reader ────────────────────────────────────────────────

def test_every_registry_document_is_described() -> None:
    got = shapes()
    if got is None:
        check("tools/registry_shape.shapes exists", False,
              "the canonical fact base publishes no shape and readers guess")
        return
    import paths
    files = {p.name for p in paths.REGISTRY.glob("*.json")}
    described = set(got)
    missing = sorted(files - described)
    check("no registry document is left undescribed", not missing, str(missing[:5]))
    check("and nothing is described that is not there",
          not sorted(described - files), str(sorted(described - files))[:120])


def test_a_field_carries_its_type_and_how_many_records_have_it() -> None:
    got = shapes()
    if got is None:
        return
    repos = got.get("repositories.json")
    check("repositories.json is described", bool(repos), str(sorted(got))[:160])
    if not repos:
        return
    fields = repos.get("fields") or {}
    check("`name_with_owner` is listed — the field I read as `name`",
          "name_with_owner" in fields, str(sorted(fields))[:200])
    f = fields.get("name_with_owner") or {}
    check("with its type", f.get("type") == "str", str(f))
    check("and the count of records carrying it",
          isinstance(f.get("present"), int) and f["present"] > 0, str(f))
    check("and the total, so `present` can be read as a fraction",
          isinstance(repos.get("records"), int) and repos["records"] > 0, str(repos)[:160])


def test_an_optional_field_is_visibly_optional() -> None:
    """A field on a few of many. A list that showed it beside `id` would teach
    a reader it is always there. Planted: one repository of several carries an
    `unpushed` block, the others do not."""
    import paths
    import registry_shape
    doc = {"schema_version": 2, "repositories": [
        {"id": f"repository:example/r{i}", "name_with_owner": f"example/r{i}",
         "local": {"sync": "clean", "folder": f"r{i}", "path": f"/tmp/r{i}",
                   **({"unpushed": 2} if i == 0 else {})}} for i in range(4)],
        "degraded": [{"source": "x", "reason": "y"}] * 9}
    saved = paths.REGISTRY
    import tmp as tmpdir
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-shape-"))
    (d / "repositories.json").write_text(json.dumps(doc), encoding="utf-8")
    registry_shape.paths.REGISTRY = d
    try:
        got = registry_shape.shapes()["repositories.json"]
    finally:
        registry_shape.paths.REGISTRY = saved
    check("the records are the repositories, not the longer notice list",
          got.get("collection") == "repositories" and got.get("records") == 4, str(got)[:160])
    up = ((got.get("nested") or {}).get("local") or {}).get("unpushed")
    check("an optional field's count is far below the total",
          bool(up) and up["present"] < got["records"], f"{up} of {got.get('records')}")


def test_the_nested_local_block_is_described() -> None:
    """The block every one of my wrong guesses lived in."""
    got = shapes()
    if got is None:
        return
    nested = (got.get("repositories.json") or {}).get("nested") or {}
    check("the `local` block is described", "local" in nested, str(sorted(nested))[:160])
    keys = nested.get("local") or {}
    for want in ("sync", "folder", "path"):
        check(f"`local.{want}` is listed", want in keys, str(sorted(keys))[:200])


# ─────────── the published copy cannot drift ───────────────────────────

def test_the_document_is_generated_and_stamped() -> None:
    """What `--write` would publish, rendered in memory: nothing in the program
    tree is written by a test."""
    import registry_shape
    data = registry_shape.shapes()
    text = registry_shape.render(data)
    check("it says it is generated", "generated" in text.lower(),
          "a reader must not edit a file the next run overwrites")
    check("and carries a content stamp so staleness is decidable",
          f"data={registry_shape.stamp(data)}" in text.splitlines()[0],
          text.splitlines()[0][:120] if text else "")
    moved = json.loads(json.dumps(data))
    moved["repositories.json"]["fields"]["planted_field"] = {"type": "str", "present": 1,
                                                             "example": ""}
    check("a change of SHAPE changes the stamp",
          registry_shape.stamp(moved) != registry_shape.stamp(data))
    counted = json.loads(json.dumps(data))
    counted["repositories.json"]["fields"]["name_with_owner"]["present"] += 5
    check("while a change of COUNT does not, so the check is not a chore",
          registry_shape.stamp(counted) == registry_shape.stamp(data))


def test_a_stale_document_is_refused() -> None:
    import check_docs
    fn = getattr(check_docs, "shape_doc_failures", None)
    check("check_docs.shape_doc_failures exists", fn is not None,
          "a generated document with no check is a document that drifts")


def test_the_cli_prints_it_without_writing() -> None:
    p = subprocess.run([PY, "tools/registry_shape.py"], cwd=ROOT,
                       capture_output=True, text=True, timeout=300)
    check("the CLI runs", p.returncode == 0, (p.stdout + p.stderr)[-200:])
    check("and names a field I once guessed wrong",
          "name_with_owner" in p.stdout, p.stdout[:200])
    check("reading it changes nothing on disk",
          "--write" in (ROOT / "tools/registry_shape.py").read_text(encoding="utf-8"),
          "the write must be explicit, so a reader cannot dirty the tree")


if __name__ == "__main__":
    print("registry shape — the typed facts had no published types\n")
    for fn in (test_every_registry_document_is_described,
               test_a_field_carries_its_type_and_how_many_records_have_it,
               test_an_optional_field_is_visibly_optional,
               test_the_nested_local_block_is_described,
               test_the_document_is_generated_and_stamped,
               test_a_stale_document_is_refused,
               test_the_cli_prints_it_without_writing):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe shape is published, generated, and checked\033[0m")
