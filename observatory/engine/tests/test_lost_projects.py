#!/usr/bin/env python3
"""A project this machine HAD, and the finding could not say so.

`work.unattributed` reported "N session(s) of work on 'some-name', which this
registry does not list" and offered two readings: a project the registry has
never seen, or a name that was never a project. A real estate held a third that
is neither.

The session companion's store recorded dozens of sessions under a name, and its
`files_read` named a folder under the estate root. That folder was absent — from
the disk, from the archives, and from the registry's repositories. **The work
happened, the project existed here, and the only trace left is a store this
system reads read-only and does not own.**

The registry cannot hold it: it is derived from what EXISTS, and an entry with no
anchor is wiped by the next emit. So the honest act is to say precisely what is
known — which folder, and that it is gone — and leave the decision with the
operator. Three verdicts now, each naming its own remedy:

    estate-folder-gone       the work's own folder was under the estate root and is not there
    folder-exists-unmatched  it still exists; the matcher did not connect the name
    no-path-recorded         no path evidence at all — a gap, not a verdict

**And a rule that was built, driven, and removed inside the hour.** "A name
matching no project whose work touched exactly one registry folder belongs to
that project" looks like a measurement rather than a guess about a name. It is
not — contact is not ownership — and live data said so on the first run: a lost
project's sessions were credited to a notes vault because the work read a
reference note in it, and a skill's sessions to the repository it sat beside.

Every fixture here is synthetic: folder names, session rows and project ids.
"""
from __future__ import annotations
import importlib, json, os, pathlib, sqlite3, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
from test_portable_mcp import setup as portable_setup               # noqa: E402
portable_setup()
import tmp as tmpdir                                                # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def sessions_module(data_root: pathlib.Path):
    os.environ["OBSERVATORY_DATA"] = str(data_root)
    import paths
    importlib.reload(paths)
    sys.path.insert(0, str(ROOT / "collectors"))
    import scan_sessions as S
    return importlib.reload(S)


def restore() -> None:
    os.environ.pop("OBSERVATORY_DATA", None)
    import paths
    importlib.reload(paths)


# ─────────── the evidence, read where it lives ─────────────────────────

def test_paths_are_read_from_the_blobs_the_store_actually_writes() -> None:
    """The companion stores `files_read` as a JSON array, and `group_concat`
    joins several rows' arrays with `|`. The first version of this read
    `session_summaries.files_read` — NULL in every row for the lost project,
    while `observations.files_read` named a path in several of them."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-lost-"))
    (d / "alive").mkdir()
    S = sessions_module(d)
    try:
        got = S.estate_paths(json.dumps([str(d / "alive/x.py"), str(d / "gone/y.py")]))
        check("a JSON array yields the estate-relative folders",
              got == {"alive", "gone"}, str(got))
        got = S.estate_paths(json.dumps([str(d / "alive/a")]) + "|"
                             + json.dumps([str(d / "gone/b")]))
        check("several rows joined by `|` are read as one",
              got == {"alive", "gone"}, str(got))
        check("a path outside the estate root is ignored",
              S.estate_paths(json.dumps(["/etc/passwd"])) == set(), "")
        check("a bare string that is not JSON is still considered",
              S.estate_paths(str(d / "alive/z")) == {"alive"}, "")
        check("a malformed blob yields nothing rather than a guess",
              S.estate_paths("{not json") == set(), "")
        check("and None is empty", S.estate_paths(None) == set(), "")
    finally:
        restore()


def test_the_three_verdicts_are_distinguished() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-verdict-"))
    (d / "still-here").mkdir()
    S = sessions_module(d)
    try:
        v, folders = S.verdict_for({"still-here", "long-deleted"})
        check("a folder that is gone decides the verdict",
              v == "estate-folder-gone", f"{v} {folders}")
        check("and it names which one", folders == ["long-deleted"], str(folders))
        v, folders = S.verdict_for({"still-here"})
        check("an existing folder is a matcher problem, not a loss",
              v == "folder-exists-unmatched", f"{v} {folders}")
        check("named too", folders == ["still-here"], str(folders))
        v, folders = S.verdict_for(set())
        check("no evidence is a gap rather than a verdict",
              v == "no-path-recorded" and folders == [], f"{v} {folders}")
    finally:
        restore()


# ─────────── contact is not ownership ──────────────────────────────────

def test_no_attribution_is_inferred_from_a_touched_folder() -> None:
    """The rule that was built, driven and removed. Its absence is asserted
    because the wrong version passed every test that existed at the time — what
    caught it was running it against the live estate."""
    import source_reader
    src = source_reader.code_keeping_strings(
        (ROOT / "collectors/scan_sessions.py").read_text(encoding="utf-8"))
    check("there is no path-based attribution function",
          "def attribute_by_path" not in src,
          "contact is not ownership")
    check("and nothing assigns a project from the touched folders",
          "resolved_by_path" not in src, "")
    # Comment text, with the `#:` markers and line wrapping taken out, since
    # the reason is a wrapped paragraph rather than one line.
    raw = (ROOT / "collectors/scan_sessions.py").read_text(encoding="utf-8")
    prose = " ".join(line.strip().lstrip("#:").strip() for line in raw.splitlines())
    prose = " ".join(prose.split())
    check("the reason it does not exist is written down where it would go",
          "Contact is not ownership" in prose,
          "a removed inference with no record invites the next author to add it")
    # The public source states the wrong answers generically, without the
    # private project names the original cited.
    check("with the wrong answers it produced",
          "notes vault" in prose and "family repository" in prose,
          "the evidence is what makes the rule refusable")


def test_the_name_rules_still_attribute_what_they_should() -> None:
    """Removing the path rule must not have removed the ones that work: a name
    matching a folder, an id slug, or the first segment of a path-like name."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-attr-"))
    S = sessions_module(d)
    try:
        index = {"alpha-web": ("project:alpha-web", "folder")}
        pid, rule = S.attribute("alpha-web", index)
        check("an exact name matches", pid == "project:alpha-web", f"{pid} {rule}")
        pid, rule = S.attribute("alpha-web/docs", index)
        check("and the first path segment does",
              pid == "project:alpha-web" and "first path segment" in rule,
              f"{pid} {rule}")
        pid, rule = S.attribute("nothing-like-it", index)
        check("an unknown name matches nothing and says why",
              pid is None and "matches no project" in rule, f"{pid} {rule}")
    finally:
        restore()


# ─────────── the finding says which of the three it is ─────────────────

def findings_for(rows: list[dict]) -> list[dict]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-lostf-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "registry/projects.json").write_text('{"projects": []}')
    (d / "scratch/sessions.json").write_text(json.dumps({
        "scanned_on": "2026-09-07", "counts": {"sessions": 0, "projects": 0,
                                               "unmatched_names": len(rows)},
        "unattributed": rows, "sessions": [], "degraded": []}))
    os.environ.update(OBSERVATORY_REGISTRY=str(d / "registry"),
                      OBSERVATORY_SCRATCH=str(d / "scratch"),
                      OBSERVATORY_DB=str(d / "absent.db"))
    import paths
    importlib.reload(paths)
    import build_findings as B
    importlib.reload(B)
    try:
        return [f for f in B.collect() if f["type"] == "work.unattributed"]
    finally:
        for k in ("OBSERVATORY_REGISTRY", "OBSERVATORY_SCRATCH", "OBSERVATORY_DB"):
            os.environ.pop(k, None)
        importlib.reload(paths)


def test_a_lost_project_is_a_warning_whatever_its_session_count() -> None:
    got = findings_for([{"name": "gone-thing", "sessions": 2,
                         "verdict": "estate-folder-gone", "folders": ["gone-thing"]}])
    check("it fires", len(got) == 1, str(got)[:200])
    if not got:
        return
    f = got[0]
    check("a lost project is a warning on two sessions",
          f["severity"] == "warning",
          "the other two verdicts are naming problems; this one is lost work")
    check("the title says the folder is gone", "is gone" in f["title"], f["title"])
    check("the detail says the registry cannot hold it",
          "derived from what exists" in f["detail"], f["detail"][:200])
    check("and the action offers the three real choices",
          all(w in f["action"] for w in ("ledger", "renamed", "deliberately")),
          f["action"])


def test_an_existing_folder_reads_as_a_matcher_problem() -> None:
    got = findings_for([{"name": "some-alias", "sessions": 2,
                         "verdict": "folder-exists-unmatched",
                         "folders": ["alpha-web"]}])
    check("it fires as info", got and got[0]["severity"] == "info", str(got)[:160])
    if got:
        # The engine names the folder under the configured estate root rather
        # than a fixed home-directory path.
        import paths
        check("and names the folder the work touched",
              str(paths.DATA / "alpha-web") in got[0]["detail"]
              and "still exists" in got[0]["detail"], got[0]["detail"][:160])
        check("while refusing to attribute it",
              "contact is not ownership" in got[0]["detail"], got[0]["detail"][:200])


def test_no_evidence_is_reported_as_a_gap() -> None:
    got = findings_for([{"name": "mystery", "sessions": 3,
                         "verdict": "no-path-recorded", "folders": []}])
    check("it fires", len(got) == 1, str(got)[:160])
    if got:
        check("and says the evidence is missing, not the verdict",
              "gap in the evidence" in got[0]["detail"], got[0]["detail"][-160:])


def test_ambiguous_known_projects_are_visible_after_one_session() -> None:
    got=findings_for([{'name':'shared-service','sessions':1,'verdict':'ambiguous-project',
                      'candidates':['project:alpha','project:beta'],'reason':'ambiguous repository name','folders':[]}])
    check('one ambiguous session raises a finding', len(got)==1)
    if got:
        f=got[0]
        check('ambiguity names both candidate IDs', all(x in f['detail'] for x in ('project:alpha','project:beta')))
        check('ambiguity asks for a precise mapping', 'mapping' in f['action'] and 'add it to the registry' not in f['action'])
        check('known candidates are not called missing projects', 'ambiguous' in f['title'] and 'does not list' not in f['title'])


def test_one_session_is_still_not_evidence_of_a_project() -> None:
    got = findings_for([{"name": "once", "sessions": 1,
                         "verdict": "estate-folder-gone", "folders": ["once"]}])
    check("a single session raises nothing", got == [],
          "someone worked here once is not a project (the floor predates this)")


if __name__ == "__main__":
    print("lost projects — the third fact the finding could not state\n")
    for fn in (test_paths_are_read_from_the_blobs_the_store_actually_writes,
               test_the_three_verdicts_are_distinguished,
               test_no_attribution_is_inferred_from_a_touched_folder,
               test_the_name_rules_still_attribute_what_they_should,
               test_a_lost_project_is_a_warning_whatever_its_session_count,
               test_an_existing_folder_reads_as_a_matcher_problem,
               test_no_evidence_is_reported_as_a_gap,
               test_one_session_is_still_not_evidence_of_a_project,
               test_ambiguous_known_projects_are_visible_after_one_session):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma project this machine had, said so in the estate's own words\033[0m")
