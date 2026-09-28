#!/usr/bin/env python3
"""Identical `clone.stale` rows, and some of them describe the normal state of a copy.

`clone.stale` fires per repository. An ownership policy that would have
suppressed some of them was declined: `stale` says plainly that nothing is at
risk and keeps a mild action for every ownership, so suppressing it would cost
a true row to save an `info` line.

For an `external` clone — a read-only copy of somebody else's repository —
"the remote has moved and nothing here is at risk" is not news, it is the
resting state: third-party projects fall behind upstream continuously and
always will.

**And the rows are worth keeping** not because an ownership policy is
unappealing, but because they are the ONLY carrier of this state on the wire:
`survey._repo_view` maps a fixed set of fields and `sync` is not among them,
while `project.detail`'s findings section DOES admit a repository subject. So
dropping them would take "this clone is behind" off the per-project surface
entirely. Putting `sync` where it belongs — the repository view — is blocked by
`additionalProperties: false` in `fabric/schemas/capability-output.schema.json`,
which makes it a contract revision the operator publishes.

What is free is legibility: the row can say whether staleness is EXPECTED, so
uniform lines become "a copy of somebody else's work, behind upstream as copies
are" for third-party clones and the plain reading for the operator's own. No row
is suppressed and no severity moves — the distinction declined was a policy,
and this is a sentence.
"""
from __future__ import annotations
import importlib, json, os, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir                                                # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def findings_for(sync: str, ownership: str | None) -> list[dict]:
    """One repository in one project, with the ownership under test."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-stale-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    projects = [] if ownership is None else [
        {"id": "project:p", "name": "p", "anchor": "vault-folder",
         "ownership": ownership, "lifecycle": "active", "local_folders": [],
         "membership_rules": [], "sites": [], "stack": []}]
    relations = [] if ownership is None else [
        {"id": "rel:1", "type": "implemented_by", "from": "project:p",
         "to": "repository:o/r", "source_refs": ["SRC-0007"]}]
    (d / "registry/projects.json").write_text(json.dumps({"projects": projects}))
    (d / "registry/relations.json").write_text(json.dumps({"relations": relations}))
    (d / "registry/repositories.json").write_text(json.dumps({"repositories": [
        {"id": "repository:o/r", "name_with_owner": "o/r", "local": {
            "folder": "r", "path": "/x/r", "checked_out_branch": "main",
            "sync": sync, "remote_checked_on": "2026-09-07"}}]}))
    os.environ.update(OBSERVATORY_REGISTRY=str(d / "registry"),
                      OBSERVATORY_SCRATCH=str(d / "scratch"),
                      OBSERVATORY_DB=str(d / "absent.db"))
    import paths
    importlib.reload(paths)
    import build_findings as B
    importlib.reload(B)
    try:
        return [f for f in B.collect() if f["type"].startswith("clone.")]
    finally:
        for k in ("OBSERVATORY_REGISTRY", "OBSERVATORY_SCRATCH", "OBSERVATORY_DB"):
            os.environ.pop(k, None)
        importlib.reload(paths)


def one(got: list[dict]) -> dict | None:
    return got[0] if len(got) == 1 else None


# ─────────── the sentence, not a policy ────────────────────────────────

def test_a_third_party_copy_says_staleness_is_expected() -> None:
    f = one(findings_for("stale", "external"))
    check("the row is still there", f is not None,
          "no row is suppressed — the distinction is a sentence, not a policy")
    if f is None:
        return
    check("and it is still info", f["severity"] == "info", f["severity"])
    check("it says this is a copy of somebody else's work",
          "copy" in f["detail"].lower(), f["detail"])
    check("and that being behind is the resting state",
          "expected" in f["detail"].lower() or "as copies" in f["detail"].lower(),
          f["detail"])
    check("the action does not ask for work on somebody else's repository",
          "pull" in f["action"], f["action"])


def test_the_operators_own_clone_keeps_the_plain_reading() -> None:
    for own in ("owned", "work-bitbucket"):
        f = one(findings_for("stale", own))
        check(f"{own}: the row fires", f is not None, str(own))
        if f is None:
            continue
        check(f"{own}: it does NOT call it a copy of somebody else's work",
              "copy" not in f["detail"].lower(), f["detail"])
        check(f"{own}: and still says nothing is at risk",
              "nothing here is at risk" in f["detail"], f["detail"])


def test_a_repository_attached_to_no_project_is_not_guessed() -> None:
    """Ownership comes from the project that implements the repository. With no
    relation there is no ownership, and inventing one is how a third-party
    clone's history once became the estate's own."""
    f = one(findings_for("stale", None))
    check("the row fires", f is not None, "")
    if f:
        check("and says nothing about whose copy it is",
              "copy" not in f["detail"].lower(), f["detail"])


def test_no_other_clone_state_gains_the_sentence() -> None:
    """`stale` is the only state where third-party staleness is the resting
    state. A branch that exists on no remote is work at risk whoever owns the
    upstream, and it must not be softened here."""
    f = one(findings_for("local-only-branch", "external"))
    check("a local-only branch still fires", f is not None, "")
    if f:
        check("as a warning", f["severity"] == "warning", f["severity"])
        check("and is not excused as somebody else's copy",
              "copy" not in f["detail"].lower(),
              "the operator's own commits sit in it")
    f = one(findings_for("ahead", "external"))
    if f:
        check("nor is `ahead`", "copy" not in f["detail"].lower(), f["detail"])


def test_the_count_of_stale_clones_survives_elsewhere() -> None:
    """Before trusting one row to carry less, check the fact is on another
    surface: the dashboard chips each stale clone and counts the non-current
    ones. Asserted so a later author cannot remove both halves."""
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    check("the dashboard chips a stale clone", '"stale":' in src, "")
    check("and counts every non-current one",
          'x["sync"] != "current"' in src, "")


def test_the_wire_still_carries_it_only_as_a_finding() -> None:
    """Why the rows stay. `survey._repo_view` maps a fixed field set with no
    `sync`, and the output schema closes the object — so the finding is the only
    carrier, and moving it is a contract revision."""
    view = (ROOT / "survey.py").read_text(encoding="utf-8")
    block = view.split("def _repo_view(")[1].split("\ndef ")[0]
    check("the repository view carries no sync field", '"sync"' not in block,
          "if this changes, the finding may be dropped: the view would carry it")
    schema = json.loads((ROOT / "fabric/schemas/capability-output.schema.json")
                        .read_text(encoding="utf-8"))
    check("and the wire's repository object is closed",
          "additionalProperties" in json.dumps(schema),
          "adding a field is a contract revision the operator publishes")


if __name__ == "__main__":
    print("stale clones — a copy behind its upstream is at rest\n")
    for fn in (test_a_third_party_copy_says_staleness_is_expected,
               test_the_operators_own_clone_keeps_the_plain_reading,
               test_a_repository_attached_to_no_project_is_not_guessed,
               test_no_other_clone_state_gains_the_sentence,
               test_the_count_of_stale_clones_survives_elsewhere,
               test_the_wire_still_carries_it_only_as_a_finding):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma copy behind its upstream reads as what it is\033[0m")
