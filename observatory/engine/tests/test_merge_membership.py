#!/usr/bin/env python3
"""The merge's own blind spots — what decides that a project exists.

`collectors/merge.py` was the only collector with NO degradation channel, and it
is the one every other module reads: the emitter turns its output into the
canonical registry, and the survey, the dashboard and the findings read that.

**What the silence cost, measured on a real estate.** `canonical()` follows a
GitHub transfer so a clone pointing at a moved address does not enter the
registry as a repository that belongs to nobody. It answered "not moved" for
every failure — no `gh`, no credential, a 500. Running the merge with `gh` off
PATH added two repositories and a project, and emptied `transfers_followed`:
two phantom addresses, both transferred long ago, became repositories, one of
them anchoring a project of its own. That emptiness is indistinguishable from
"no clone points at a dead address". Nothing was logged, nothing exited
non-zero, and the emitter then wrote all of it into the canonical registry.

**And the record of the transfers was read by nobody.** `transfers_followed` was
written into the model on every tick and had no reader at all. Clones naming a
dead address need one `git remote set-url` each, which the operator cannot run
without being told which checkout to run it in.

Three more, each latent rather than live, and each written down because latent
is how the first two started:

* The owned organisations were a literal in the merge while the GitHub scan
  discovers owners, so a new organisation would have every repository classed
  `external` — and `estate.py` refuses to record a session for anything but an
  owned or work repository.
* `ks = [...] or ks` reverted the leftovers list to its unfiltered self when
  every leftover was retired, so two retired repositories could form an
  ORGANISATION project — the one thing the INACTIVE set exists to prevent.
* Folder names were excluded inline with no reason given, while
  `session_name_exclusions.json` carries a `why` per row, because an exclusion
  nobody can justify is indistinguishable from a mistake.

Every input is synthetic (`tests/merge_fixture.py`): a private workspace with
the GitHub integration on, one owned organisation, and an offline `gh` stub.
"""
from __future__ import annotations
import json, os, pathlib, shutil, stat, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
import tmp as tmpdir  # noqa: E402
import merge_fixture  # noqa: E402

OWNER = merge_fixture.OWNER

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def raw_copy() -> pathlib.Path:
    """Own complete inputs; no personal raw store or network credentials."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-merge-"))
    merge_fixture.seed(d)
    return d / "raw"


def merge(raw: pathlib.Path, gh_absent: bool = False) -> tuple[int, str, dict]:
    env = merge_fixture.environment(raw.parent, absent=gh_absent)
    p = subprocess.run([PY, "collectors/merge.py", str(raw)], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=900)
    if p.returncode != 0:
        return p.returncode, p.stdout + p.stderr, {}
    return 0, p.stdout + p.stderr, json.loads((raw / "model.json").read_text(encoding="utf-8"))


PHANTOMS = tuple(merge_fixture.TRANSFERS)


# ─────────── a question not asked has no answer ────────────────────────

def test_a_transfer_that_cannot_be_checked_is_not_called_unmoved() -> None:
    raw = raw_copy()
    code, out, base = merge(raw)
    check("the control run works", code == 0 and base, out[-200:])
    if not base:
        return
    check("and it follows the transfers", len(base["transfers_followed"]) >= 2,
          str(base["transfers_followed"]))
    check("so no phantom is in the model",
          not [k for k in PHANTOMS if k in base["repositories"]],
          str([k for k in PHANTOMS if k in base["repositories"]]))

    code, out, doc = merge(raw, gh_absent=True)
    check("the merge still runs without gh", code == 0 and doc, out[-200:])
    if not doc:
        return
    check("and STILL keeps the phantoms out",
          not [k for k in PHANTOMS if k in doc["repositories"]],
          "the previous run's answer is used rather than a fresh wrong one")
    check("because the previous answer was carried forward",
          doc["transfers_followed"] == base["transfers_followed"],
          f"{doc['transfers_followed']} vs {base['transfers_followed']}")
    check("the repository count does not move", len(doc["repositories"]) ==
          len(base["repositories"]),
          f"{len(base['repositories'])} -> {len(doc['repositories'])}")
    check("nor the project count", len(doc["projects"]) == len(base["projects"]),
          f"{len(base['projects'])} -> {len(doc['projects'])}")


def test_the_merge_says_what_it_could_not_measure() -> None:
    raw = raw_copy()
    merge(raw)                                    # seed the previous answer
    code, out, doc = merge(raw, gh_absent=True)
    if not doc:
        check("the merge answered", False, out[-200:])
        return
    deg = doc.get("degraded")
    check("the model carries a degraded list", isinstance(deg, list) and deg,
          str(type(deg)))
    if not deg:
        return
    carried = [d for d in deg if "using the previous run's answer" in d["reason"]]
    check("naming the carried answers", len(carried) >= 2, str(len(carried)))
    check("and the address as ASKED, not as resolved",
          any(d["source"] == "transfer:old/fixture-a" for d in deg),
          str(sorted(d["source"] for d in carried)))
    check("the run prints them", "degraded:" in out, out[-160:])
    unchecked = [d for d in deg if "no earlier run had answered" in d["reason"]]
    check("an address nobody has ever resolved says so", unchecked,
          "external clones have no previous mapping")
    if unchecked:
        check("and admits it may be a repository that does not exist",
              "does not exist" in unchecked[0]["reason"], unchecked[0]["reason"][-90:])


def test_a_404_is_an_answer_and_not_a_degradation() -> None:
    """The distinction that keeps the channel honest: "this address is gone" is
    a measurement, "I could not ask" is not."""
    src = (ROOT / "collectors/merge.py").read_text(encoding="utf-8")
    check("a 404 returns no reason", '"Not Found" in err or "404" in err' in src,
          "otherwise every deleted repository becomes a degradation for ever")
    check("and a missing gh does return one", "is not installed" in src)


# ─────────── the record now has a reader ───────────────────────────────

def test_a_stale_remote_reaches_a_finding() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-stale-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "registry/stale-remotes.json").write_text(json.dumps({
        "schema_version": 1, "clones": [
            {"was": "old-org/alpha-web", "now": "example-org/alpha-web",
             "folder": "alpha-web", "path": str(d / "projects/alpha-web")},
            {"was": "old/gone", "now": "new/gone", "folder": "", "path": ""}]}),
        encoding="utf-8")
    p = subprocess.run([PY, "tools/build_findings.py", "--json"], cwd=ROOT,
                       env=dict(os.environ, OBSERVATORY_REGISTRY=str(d / "registry"),
                                OBSERVATORY_SCRATCH=str(d / "scratch"),
                                OBSERVATORY_DB=str(d / "absent.db")),
                       capture_output=True, text=True, timeout=600)
    try:
        found = json.loads(p.stdout)["findings"]
    except (ValueError, KeyError):
        check("findings built", False, (p.stdout + p.stderr)[-300:])
        return
    stale = [f for f in found if f["type"] == "repo.stale_remote"]
    check("the clone is reported", len(stale) == 1, str(len(stale)))
    if not stale:
        return
    f = stale[0]
    check("named by its folder", f["subject"] == "clone:alpha-web", f["subject"])
    check("the title names both addresses",
          "old-org/alpha-web" in f["title"] and "example-org/alpha-web" in f["title"],
          f["title"])
    check("the action is the command, in the right directory",
          f["action"].startswith(f"git -C {d / 'projects/alpha-web'} remote set-url"),
          f["action"])
    check("it explains why nothing broke", "redirect" in f["detail"], f["detail"][:120])
    check("a transfer with no local checkout raises nothing",
          not [x for x in stale if x["subject"] == "clone:"],
          "there is no directory to repoint, so it is history rather than a finding")


def test_the_models_degradation_reaches_a_finding() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-mdeg-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "scratch/model.json").write_text(json.dumps({"degraded": [
        {"source": "transfer:x/y", "reason": "`gh` is not installed, so no "
                                             "transfer could be followed"}]}),
        encoding="utf-8")
    p = subprocess.run([PY, "tools/build_findings.py", "--json"], cwd=ROOT,
                       env=dict(os.environ, OBSERVATORY_REGISTRY=str(d / "registry"),
                                OBSERVATORY_SCRATCH=str(d / "scratch"),
                                OBSERVATORY_DB=str(d / "absent.db")),
                       capture_output=True, text=True, timeout=600)
    try:
        found = json.loads(p.stdout)["findings"]
    except (ValueError, KeyError):
        check("findings built", False, (p.stdout + p.stderr)[-300:])
        return
    md = [f for f in found if f["type"] == "model.degraded"]
    check("the merge's own degradation is raised", len(md) == 1, str(len(md)))
    if md:
        check("carrying the reason", "gh` is not installed" in md[0]["detail"],
              md[0]["detail"][:120])
        check("and saying the registry was still written",
              "still written" in md[0]["detail"], md[0]["detail"][:200])


def test_one_reader_for_every_collectors_degradation() -> None:
    """The shape rule, in one place now that it has two callers."""
    import degradations
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-degread-"))
    previous_scratch = os.environ.get("OBSERVATORY_SCRATCH")
    os.environ["OBSERVATORY_SCRATCH"] = str(d)
    import importlib, paths
    importlib.reload(paths)
    importlib.reload(degradations)
    (d / "list.json").write_text('[{"source":"a","reason":"b"}]', encoding="utf-8")
    (d / "obj.json").write_text('{"degraded":[{"source":"c","reason":"d"}]}', encoding="utf-8")
    (d / "broken.json").write_text("{not json", encoding="utf-8")
    check("a bare list is read", len(degradations.collector("list.json")) == 1)
    check("an object is read", len(degradations.collector("obj.json")) == 1)
    check("an absent file is NOT 'measured and fine'",
          degradations.collector("gone.json") == [])
    check("and an unreadable one is its own measurement",
          degradations.collector("broken.json")[0]["reason"] == "collector output is unreadable")
    if previous_scratch is None:
        os.environ.pop('OBSERVATORY_SCRATCH', None)
    else:
        os.environ['OBSERVATORY_SCRATCH'] = previous_scratch
    importlib.reload(paths)
    importlib.reload(degradations)
    survey_src = (ROOT / "survey.py").read_text(encoding="utf-8")
    finds_src = (ROOT / "tools/build_findings.py").read_text(encoding="utf-8")
    check("the survey uses the shared reader", "degradations.collector" in survey_src,
          "a second copy is how the two shapes disagreed in the first place")
    check("and so does the findings builder", "degradations.collector" in finds_src)


# ─────────── the latent three ──────────────────────────────────────────

def test_an_undeclared_owner_is_reported_rather_than_classed_external() -> None:
    raw = raw_copy()
    (raw / "gh" / "brand-new-org.json").write_text(json.dumps(
        [{"nameWithOwner": "brand-new-org/thing", "url": "https://x",
          "visibility": "PUBLIC", "isArchived": False, "isFork": False}]),
        encoding="utf-8")
    code, out, doc = merge(raw)
    if not doc:
        check("the merge answered", False, out[-200:])
        return
    own = [d for d in doc.get("degraded", []) if d["source"] == "ownership"]
    check("an owner the merge does not declare is reported", len(own) == 1,
          str(doc.get("degraded"))[:200])
    if own:
        check("naming it", "brand-new-org" in own[0]["reason"], own[0]["reason"][:120])
        # THE CONSEQUENCE OF THIS CASE, not of the class. The sentence used to
        # say "estate.py will refuse to record sessions for them" whatever the
        # instance, and the real one had no checkout at all — nothing to lose,
        # and an alarm spent on a classification. This fixture's org has no
        # clone, so the honest half is the quiet one.
        check("and the consequence THIS case carries",
              ("no session is being lost" in own[0]["reason"]
               or "observed and then dropped" in own[0]["reason"]),
              own[0]["reason"][-160:])
        check("saying it is the operator's decision, not a collector's",
              "operator" in own[0]["reason"], own[0]["reason"][-90:])
    check("the run says so on stdout", "UNDECLARED owner" in out, out[-200:])
    p = doc["projects"].get("brand-new-org-thing") or {}
    check("and the project is still reported external rather than guessed owned",
          p.get("ownership") == "external", str(p.get("ownership")))


def test_a_submodule_is_measured_composition_and_dissolves_the_phantom() -> None:
    """`.gitmodules` beats a stale wiki note, and the phantom never forms.

    The measured case: a parent repository with several submodules reached the
    registry as repositories "named in vault notes", while its modules stood
    beside it as standalone projects. Driven with synthetic parent and module
    repositories; the submodule declaration joins them and removes standalone
    module projects.
    """
    raw = raw_copy()
    doc = json.loads((raw / "local.json").read_text(encoding="utf-8"))
    row = next(f for f in doc["folders"] if f["folder"] == "fixture-parent")
    row["submodules"] = [
        {"path": "apps/module-a", "url": f"git@github.com:{OWNER}/fixture-module-a.git",
         "nwo": f"{OWNER}/fixture-module-a"},
        {"path": "apps/module-b", "url": f"git@github.com:{OWNER}/fixture-module-b.git",
         "nwo": f"{OWNER}/fixture-module-b"}]
    (raw / "local.json").write_text(json.dumps(doc), encoding="utf-8")
    rc, out, model = merge(raw)
    check("the merge runs over the planted submodules", rc == 0, out[-200:])
    if rc != 0:
        return
    proj = model["projects"]["fixture-parent"]
    check("both modules joined the parent project",
          f"{OWNER}/fixture-module-a" in proj["repos"]
          and f"{OWNER}/fixture-module-b" in proj["repos"], str(proj["repos"]))
    check("each with the submodule rule naming its PATH",
          any("git submodule at apps/module-a" in r for r in proj["rules"]),
          str([r for r in proj["rules"] if "submodule" in r]))
    phantoms = [k for k, v in model["projects"].items()
                if any(r in v["repos"] for r in (f"{OWNER}/fixture-module-a",
                                                 f"{OWNER}/fixture-module-b"))
                and v.get("anchor") == "repository"]
    check("and no standalone phantom stands beside the parent", not phantoms,
          str(phantoms))


def test_retired_repositories_never_form_an_organisation_project() -> None:
    """The `or ks` hole, driven: two retired leftovers under one owned org."""
    raw = raw_copy()
    (raw / "gh" / "retiredorg.json").write_text(json.dumps([
        {"nameWithOwner": f"{OWNER}/fixture-retired-a", "url": "https://a", "visibility": "PUBLIC",
         "isArchived": False, "isFork": False},
        {"nameWithOwner": f"{OWNER}/fixture-retired-b", "url": "https://b", "visibility": "PUBLIC",
         "isArchived": False, "isFork": False}]), encoding="utf-8")
    st = {'repositories': {name: {'status': 'inactive', 'why': 'Synthetic retired fixture'}
                          for name in (f'{OWNER}/fixture-retired-a', f'{OWNER}/fixture-retired-b')}}
    merge_fixture.write(merge_fixture.curation(raw.parent) / 'repo_status.json', st)
    rc, out, doc = merge(raw)
    check('retired fixture merge runs', rc == 0, out[-300:])
    if rc:
        return
    orgs = [k for k, pr in doc["projects"].items()
            if pr["anchor"] == "organisation" and pr["name"] == OWNER]
    check("two retired leftovers form NO organisation project", not orgs, str(orgs))
    standalone = [k for k, pr in doc["projects"].items()
                  if pr["repos"] in ([f"{OWNER}/fixture-retired-a"], [f"{OWNER}/fixture-retired-b"])]
    check("and no standalone project either", not standalone, str(standalone))
    check("while the repositories are still recorded",
          f"{OWNER}/fixture-retired-a" in doc["repositories"],
          "retired means it anchors nothing, not that it vanishes")


def test_every_folder_exclusion_carries_its_reason() -> None:
    """The shipped default and a planted workspace file both carry reasons."""
    f = ROOT / "defaults/folder_exclusions.json"
    check("the exclusions live in a file", f.is_file(),
          "names were once inline in merge.py with no explanation")
    if not f.is_file():
        return
    doc = json.loads(f.read_text(encoding="utf-8"))
    rows = doc["prefixes"] + doc["names"]
    check("every row has a why", all((r.get("why") or "").strip() for r in rows),
          str([r for r in rows if not r.get("why")]))
    check("each why is a sentence, not a label",
          all(len(r["why"].split()) >= 5 for r in rows),
          str([r["why"] for r in rows if len(r["why"].split()) < 5]))
    src = (ROOT / "collectors/merge.py").read_text(encoding="utf-8")
    check("reading the workspace file rather than an inline list",
          "config_file('folder_exclusions.json')" in src
          or 'config_file("folder_exclusions.json")' in src, "")

    raw = raw_copy()
    code, out, model = merge(raw)
    check('the exclusion fixture merges', code == 0, out[-300:])
    if model:
        check('the planted excluded folder stays out',
              not any(p['name']=='fixture-excluded' for p in model['projects'].values()))


def test_session_dates_pass_through_real_merge() -> None:
    raw = raw_copy()
    merge_fixture.write(raw/'sessions.json', {'sessions': [
        {'project_id':'project:fixture-parent','started_on':'2001-02-03','ended_on':'2001-02-04'},
        {'project_id':'project:fixture-parent','started_on':'2001-02-01','ended_on':'2001-02-02'}]})
    rc, out, model = merge(raw)
    check('session fixture merges', rc == 0, out[-300:])
    if rc == 0:
        check('newest session, not row order, supplies activity',
              model['projects']['fixture-parent']['last_activity'] == '2001-02-04')


def test_404_is_driven_without_previous_answer() -> None:
    raw = raw_copy()
    rc, out, model = merge(raw)
    check('offline stub answered the control', rc == 0, out[-300:])
    if rc:
        return
    calls=[json.loads(line) for line in (raw.parent/'gh-calls.jsonl').read_text().splitlines()]
    check('unknown remote was actually queried', any(c[1]=='repos/unknown/gone' for c in calls))
    check('404 carries no transport degradation',
          not any(d['source']=='transfer:unknown/gone' for d in model['degraded']))
    rc, out, model = merge(raw, gh_absent=True)
    check('missing executable remains a degraded answer', rc == 0 and any(
          d['source']=='transfer:unknown/gone' for d in model.get('degraded', [])), out[-300:])


if __name__ == "__main__":
    print("the merge — what decides that a project exists\n")
    for fn in (test_a_transfer_that_cannot_be_checked_is_not_called_unmoved,
               test_the_merge_says_what_it_could_not_measure,
               test_a_404_is_an_answer_and_not_a_degradation,
               test_a_stale_remote_reaches_a_finding,
               test_the_models_degradation_reaches_a_finding,
               test_one_reader_for_every_collectors_degradation,
               test_an_undeclared_owner_is_reported_rather_than_classed_external,
               test_retired_repositories_never_form_an_organisation_project,
               test_every_folder_exclusion_carries_its_reason,
               test_a_submodule_is_measured_composition_and_dissolves_the_phantom,
               test_session_dates_pass_through_real_merge,
               test_404_is_driven_without_previous_answer):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe model now says what it could not measure, and its record has a reader\033[0m")
