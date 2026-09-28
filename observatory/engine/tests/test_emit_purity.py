#!/usr/bin/env python3
"""The emit is a function of (measurement, curated overrides) — and of nothing else.

It used to read its own previous output. `lifecycle`, `description`,
`canonical_page` and `source_refs` each fell back to `prev.get(...)`, which is
trap T4 — *"`or prev.get(...)` made a field impossible to clear; a wrong value
written once was carried forward by every later run"* — repeated four times in
the file that documents it. Archive every repository of a project on GitHub and
its `lifecycle` stayed `active` for ever, because the emit read the answer it
gave last time. It also meant the emit converged rather than computed: after the
underlying data moved it needed two passes, which is how T4 went red.

The check that has to be run BEFORE removing such a fallback is T11's: rebuild
from measurement alone and see what disappears. What disappears is a value
transcribed by hand that no listing ever reported; such values belong in the
workspace's `config/repo_overrides.json`.

Every case below runs the real emitter against a synthetic registry, through
`OBSERVATORY_REGISTRY`, with its own private workspace. A test that rebuilt a
real one would be the fixture depending on ambient state that trap T16 forbids.
"""
from __future__ import annotations
import json, os, pathlib, shutil, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402
import paths                                                        # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def sandbox() -> pathlib.Path:
    """A seeded emitter model plus its own initialized workspace.

    The engine reads curated overrides from the selected workspace's `config/`
    (never from a directory beside the source), so each sandbox carries a
    private workspace and its curation files live there.
    """
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-emit-"))
    from emitter_fixture import seed
    import workspace
    seed(d)
    workspace.initialize(d / "home", identities=False)
    return d / "registry"


def curation(reg: pathlib.Path) -> pathlib.Path:
    return reg.parent / "home" / "config"


def env_for(reg: pathlib.Path) -> dict[str, str]:
    from emitter_fixture import environment
    return {**environment(reg.parent), "OBSERVATORY_HOME": str(reg.parent / "home")}


def emit(reg: pathlib.Path) -> str:
    p = subprocess.run([PY, "collectors/emit_registry.py", str(reg.parent / "raw")], cwd=ROOT,
                       capture_output=True, text=True, timeout=300,
                       env=env_for(reg))
    assert p.returncode == 0, f"emitter exited {p.returncode}: {p.stderr[-300:]}"
    return p.stdout + p.stderr


def load(reg: pathlib.Path, name: str) -> str:
    return (reg / name).read_text(encoding="utf-8")


#: The ONE field of a registry document that is derived from the previous output
#: on purpose. `updated_on` answers "when did the content last
#: change", which can only be known by comparing against what was there — so
#: `atomic.write_json_carrying` reads the old file, keeps the stamp when the rest
#: is byte-equal, and stamps the run's date when it is not.
#:
#: The purity property below therefore compares CONTENT and asserts separately
#: that the stamp is the only difference. That is stronger than the byte
#: equality it replaces, not weaker: a leak in any other field now names itself
#: instead of being one line inside a diff of two 5000-line files.
DERIVED_STAMP = "updated_on"


def content(reg: pathlib.Path, name: str) -> dict:
    doc = json.loads(load(reg, name))
    doc.pop(DERIVED_STAMP, None)
    return doc


def same_but_the_stamp(reg: pathlib.Path, name: str, before: str) -> tuple[bool, str]:
    """(content is identical, what differs). Reports the leaking keys by name."""
    old, new = json.loads(before), json.loads(load(reg, name))
    old.pop(DERIVED_STAMP, None)
    new.pop(DERIVED_STAMP, None)
    if old == new:
        return True, ""
    if not (isinstance(old.get("projects", old.get("repositories")), list)):
        return False, "the document shape changed"
    key = "projects" if "projects" in old else "repositories"
    a = {r["id"]: r for r in old[key]}
    b = {r["id"]: r for r in new[key]}
    leaks = sorted({f"{i}.{f}" for i in set(a) & set(b)
                    for f in set(a[i]) | set(b[i]) if a[i].get(f) != b[i].get(f)})
    gone, added = sorted(set(a) - set(b)), sorted(set(b) - set(a))
    return False, f"leaked: {leaks[:6]}; gone: {gone[:3]}; added: {added[:3]}"


def test_the_previous_output_cannot_change_the_next_one() -> None:
    """The property in one sentence: tamper with the artefact, get it back."""
    reg = sandbox()
    emit(reg)
    clean = load(reg, "projects.json")

    doc = json.loads(clean)
    for p in doc["projects"][:20]:
        p["lifecycle"] = "TAMPERED"
        p["description"] = "TAMPERED"
        p["canonical_page"] = "vault:TAMPERED.md"
    (reg / "projects.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False),
                                       encoding="utf-8")
    emit(reg)
    ok, why = same_but_the_stamp(reg, "projects.json", clean)
    check("tampered fixture records do not survive the next emit", ok,
          f"the emit is reading its own previous output again — {why}")
    # AND the stamp DID move, which is the other half: the content on disk
    # changed, so the field that says when it changed must say today. A carry
    # rule that carried through a tamper would be a stamp that cannot be
    # trusted either.
    # TODAY'S DATE, not "different from the clean run". The first version
    # compared the two stamps, and they are equal whenever the clean run ALSO
    # stamped today — which is the normal case for a freshly seeded registry. An assertion that holds only while a date is stale is
    # not an assertion about the rule.
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    check("while the stamp records that the file's content moved today",
          json.loads(load(reg, "projects.json"))[DERIVED_STAMP] == today,
          "the tamper-and-restore IS a change to what is on disk")


def test_a_rebuild_from_nothing_loses_nothing() -> None:
    """T11's own procedure, run as a test rather than remembered as a lesson."""
    reg = sandbox()
    emit(reg)
    before_p, before_r = load(reg, "projects.json"), load(reg, "repositories.json")

    for name, keyname in (("projects.json", "projects"), ("repositories.json", "repositories")):
        (reg / name).write_text(json.dumps(
            {"schema_version": 2, "updated_on": "1970-01-01", keyname: []}), encoding="utf-8")
    out = emit(reg)
    for name, before in (("projects.json", before_p), ("repositories.json", before_r)):
        ok, why = same_but_the_stamp(reg, name, before)
        check(f"{name} rebuilt from measurement alone is identical", ok,
              f"{why} | {out[-160:]}")


def test_a_curated_repository_field_survives_the_rebuild() -> None:
    reg = sandbox()
    for name, keyname in (("repositories.json", "repositories"),):
        (reg / name).write_text(json.dumps(
            {"schema_version": 2, "updated_on": "1970-01-01", keyname: []}), encoding="utf-8")
    curated = {"fixture/service": {"description": "Synthetic curated description"}}
    (curation(reg) / "repo_overrides.json").write_text(json.dumps({"repositories": curated}))
    emit(reg)
    got = {r["name_with_owner"]: r for r in json.loads(load(reg, "repositories.json"))["repositories"]}
    for name, fields in curated.items():
        row = got.get(name)
        check(f"{name} keeps its curated description", bool(row) and
              row.get("description") == fields["description"], str(row and row.get("description")))
        check(f"{name} is marked as curated", bool(row) and
              "description" in (row.get("curated_fields") or []), str(row and row.get("curated_fields")))


def test_selected_curation_never_falls_back_to_the_author() -> None:
    """A workspace missing a required curation file is refused, never silently
    completed from the defaults shipped beside the source."""
    reg = sandbox()
    before = (reg / 'projects.json').read_bytes()
    (curation(reg) / 'project_overrides.json').unlink()
    p = subprocess.run([PY, 'collectors/emit_registry.py', str(reg.parent / 'raw')],
                       cwd=ROOT, env=env_for(reg), capture_output=True, text=True)
    check('missing selected required curation is refused', p.returncode != 0)
    check('refusal happens before registry replacement', (reg / 'projects.json').read_bytes() == before)


def test_the_one_remaining_fallback_states_its_reason() -> None:
    """`default_branch` keeps a fallback, and the difference must be written down."""
    src = (ROOT / "collectors/emit_registry.py").read_text(encoding="utf-8")
    check("default_branch still falls back to the previous emit",
          'prev.get("default_branch"' in src)
    check("and the reason is stated where it is done",
          "age-gates" in src or "DID NOT MEASURE" in src,
          "an unexplained fallback is indistinguishable from the bug it resembles")
    for field in ('prev.get("lifecycle"', 'prev.get("canonical_page"'):
        check(f"{field}...) is gone", field not in src)
    check('the project description no longer falls back',
          'else prev.get("description","")' not in src)


def test_a_clearing_is_announced() -> None:
    """T11 cost a curated value whose only sign was a count falling by one."""
    src = (ROOT / "collectors/emit_registry.py").read_text(encoding="utf-8")
    check("the emitter reports fields measurement has cleared", "CLEARED by measurement" in src)
    check("and names the file a curated one belongs in",
          "project_overrides.json" in src and "repo_overrides.json" in src)


if __name__ == "__main__":
    print("emit purity — measurement and curation, nothing else\n")
    for fn in (test_the_previous_output_cannot_change_the_next_one,
               test_a_rebuild_from_nothing_loses_nothing,
               test_a_curated_repository_field_survives_the_rebuild,
               test_selected_curation_never_falls_back_to_the_author,
               test_the_one_remaining_fallback_states_its_reason,
               test_a_clearing_is_announced):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe emit computes; it no longer converges\033[0m")
