#!/usr/bin/env python3
"""The six ways a Heroku inventory lies, each one planted and caught here.

Every check below is the negative of a defect that SHIPPED in the hand
measurement this feature grew out of, so none of them is hypothetical:

1. `/apps/<id>/addons` returns add-ons attached from ANOTHER application. Summed
   whole, two shared databases made the estate $14/month more expensive than it
   is and made one bot application look like it paid for a database it does not
   have.
2. A SUSPENDED application answers 403 on `/dynos`. Read as an empty list it
   becomes "scaled but never started" — a different diagnosis with a different
   remedy, and the remedy for the wrong one is a restart that cannot work.
3. A release is not a deployment. Config vars, Logplex and add-on attachment are
   releases too, and reading three of them made an application with a live
   deploy history look like it had never been deployed.
4. A name is not evidence. An application can deploy from a folder with a
   different name; an application whose name equals a project's must still be
   unlinked unless something was measured: never infer from a name.
5. A hand link with no evidence is the same guess wearing a curator's clothes,
   so `heroku_links.json` rows without `evidence` are refused rather than
   trusted.
6. Nineteen applications share one reason for having no project. Printed in
   full on every row it is one fact and eighteen lines of noise — the same
   defect the stale-clone rule once had, one subject over.

The live-estate checks are deliberately few: this suite must pass on a fresh
clone, where the registry's `heroku-apps.json` may hold no applications at all.
"""
from __future__ import annotations
import json, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "collectors"))
sys.path.insert(0, str(ROOT / "tools"))
import heroku_registry as HR                                        # noqa: E402
import heroku_findings as HF                                        # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def app(**kw) -> dict:
    """A scan row with everything the code reads, so a test names only what it means."""
    base = {"name": "an-app", "team": "personal", "region": "us", "stack": "heroku-24",
            "suspended": False, "scaled": 1, "running": 1, "crashed": [],
            "formation": [{"type": "web", "qty": 1, "size": "Basic"}],
            "addons": [], "addons_attached": [], "monthly_cost": 7, "dyno_cost": 7,
            "addon_cost": 0, "last_deploy": {"version": 3, "at": "2026-01-01T00:00:00Z"},
            "last_release": {"version": 4, "at": "2026-01-02T00:00:00Z"},
            "github": None, "auto_deploy": None, "local_folders": [], "web_url": None,
            "deploy_beyond_window": False}
    base.update(kw)
    return base


# ── 1. state ────────────────────────────────────────────────────────────────
def test_suspended_is_not_the_same_as_crashed():
    """403 from Heroku and a crash of our own are different facts with different remedies."""
    check("a suspended application is `suspended`, not `down`",
          HR.state_of(app(suspended=True, running=0, crashed=[])) == "suspended")
    check("scaled with nothing up is `down`",
          HR.state_of(app(running=0)) == "down")
    check("no dyno but a billed add-on is `resources-only`",
          HR.state_of(app(scaled=0, running=0, addons=[{"plan": "pg", "cents": 500}]))
          == "resources-only")
    check("no dyno and no add-on is `idle`",
          HR.state_of(app(scaled=0, running=0)) == "idle")
    # THE PLANTED DEFECT: order matters. A suspended application still has a
    # formation, so a `scaled > 0` test written first calls it `down`.
    ordered = HR.state_of(app(suspended=True, scaled=2, running=0))
    check("suspension outranks the formation it left behind", ordered == "suspended",
          f"got {ordered} — the scaled check ran before the suspension check")


# ── 2. money ────────────────────────────────────────────────────────────────
def test_an_addon_billed_to_another_application_is_not_ours():
    """The double count that made the estate $14/month bigger than it is."""
    own, attached = _split()
    check("an add-on owned by another application is separated out",
          [x["plan"] for x in own] == ["heroku-postgresql:essential-1"]
          and [x["owner"] for x in attached] == ["someone-else"])
    check("the separated one is still REPORTED, not dropped", len(attached) == 1,
          "deleting its owner deletes this application's data — that must be visible")


def _split():
    sys.path.insert(0, str(ROOT / "collectors"))
    import scan_heroku
    return scan_heroku.owned_addons([
        {"name": "a", "plan": {"name": "heroku-postgresql:essential-1"},
         "state": "provisioned", "billed_price": {"cents": 900}, "app": {"name": "mine"}},
        {"name": "b", "plan": {"name": "heroku-postgresql:essential-0"},
         "state": "provisioned", "billed_price": {"cents": 500}, "app": {"name": "someone-else"}},
    ], "mine")


# ── 3. links ────────────────────────────────────────────────────────────────
# THE FIXTURE IS THE TEST HERE. A folder literally named `alpha` and a
# repository literally named `acme/alpha` exist so that a name-matching rule
# WOULD link the application called `alpha` — the first version of this suite
# used `alpha-folder` and `acme/alpha-repo`, a name rule was planted, and every
# check stayed green because nothing in the fixture could match by name. A guard
# that cannot fail is not a guard.
PROJECTS = [{"id": "project:alpha", "name": "alpha",
             "local_folders": ["alpha-folder", "alpha"]},
            {"id": "project:beta", "name": "beta", "local_folders": ["beta-folder"]}]
REPOS = [{"id": "repository:acme/alpha-repo", "name_with_owner": "acme/alpha-repo"},
         {"id": "repository:acme/alpha", "name_with_owner": "acme/alpha"}]
RELATIONS = [{"type": "implemented_by", "from": "project:alpha",
              "to": "repository:acme/alpha-repo"},
             {"type": "implemented_by", "from": "project:alpha",
              "to": "repository:acme/alpha"}]


def _link(a: dict):
    return HR.link(a, HR._repo_owner_index(REPOS, RELATIONS),
                   HR._folder_index(PROJECTS), {})


def test_a_name_is_never_evidence():
    """Never infer from a name, as an assertion rather than a sentence in a doc."""
    project, rule, why = _link(app(name="alpha"))
    check("an application named exactly like a project is still unlinked",
          project is None and rule is None, f"linked by {rule}")
    check("and it says why rather than shrugging", bool(why and why[1]))
    check("the reason carries a KIND as well as a sentence", why[0] == "no-source",
          "fifteen rows sharing one sentence is why the kind exists")


def test_the_three_measured_rules_each_fire():
    import os
    home = str(HR._data_root())
    p, rule, _ = _link(app(github="acme/alpha-repo"))
    check("Heroku's own Deploy tab links an application",
          (p, rule) == ("project:alpha", "heroku-github-link"))
    p, rule, _ = _link(app(local_folders=[os.path.join(home, "beta-folder")]))
    check("a checkout carrying the app's git remote links it",
          (p, rule) == ("project:beta", "heroku-remote"))
    p, rule, _ = _link(app(local_folders=[os.path.join(home, "beta-folder", "deploy", "x")]))
    check("a checkout INSIDE a project's folder links it too",
          (p, rule) == ("project:beta", "heroku-remote-nested"),
          "containment on disk is measured; a similar name is not")


def test_a_hand_link_without_evidence_is_refused(tmp=None):
    """The curated file is an escape hatch for measurement, not for guessing."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        f = pathlib.Path(d) / "heroku_links.json"
        f.write_text(json.dumps({"links": [
            {"app": "with", "project": "project:alpha", "evidence": "a commit sha"},
            {"app": "without", "project": "project:beta"},
        ]}), encoding="utf-8")
        was, HR.LINKS = HR.LINKS, f
        try:
            got = HR.load_verified()
        finally:
            HR.LINKS = was
    check("a hand link carrying evidence is accepted", "with" in got)
    check("a hand link with no evidence is refused", "without" not in got,
          "an unsourced hand link is the guess rule 2 forbids, wearing a curator's clothes")


def test_an_edge_exists_only_where_a_link_does():
    apps, edges = HR.records({"apps": [app(name="alpha", github="acme/alpha-repo"),
                                       app(name="orphan")]},
                             PROJECTS, REPOS, RELATIONS)
    linked = [a for a in apps if a["project"]]
    check("one edge per linked application, none for the rest",
          len(edges) == 1 and len(linked) == 1 and edges[0]["type"] == "deployed_to")
    check("the edge points at the application's own id",
          edges[0]["to"] == "heroku:alpha" and edges[0]["from"] == "project:alpha")
    check("a link and its rule are one fact",
          all(bool(a["project"]) == bool(a["link_rule"]) for a in apps))


# ── 4. findings ─────────────────────────────────────────────────────────────
def test_no_scan_produces_no_findings():
    """A fresh clone has no document, and inventing a row about that would put a
    finding about the OBSERVATORY on a board about the ESTATE."""
    check("an absent document yields nothing", HF.findings(None) == [])
    check("an empty app list yields nothing", HF.findings({"apps": []}) == [])


def _rec(**kw):
    apps, _ = HR.records({"apps": [app(**kw)]}, PROJECTS, REPOS, RELATIONS)
    return apps


def test_each_rule_fires_on_its_own_subject():
    down = HF.findings({"apps": _rec(name="a", running=0)})
    check("a scaled application with nothing up raises app_down",
          [f["type"] for f in down].count("heroku.app_down") == 1)
    susp = HF.findings({"apps": _rec(name="a", suspended=True, running=0)})
    row = next(f for f in susp if f["type"] == "heroku.app_down")
    check("a suspension is critical, a crash is not", row["severity"] == "critical")
    check("and the action names support rather than a restart that cannot work",
          "support" in row["action"])
    waste = HF.findings({"apps": _rec(name="a", scaled=0, running=0, formation=[],
                                      addons=[{"plan": "pg", "cents": 500}])})
    check("resources with no dyno raise paying_for_nothing",
          any(f["type"] == "heroku.paying_for_nothing" for f in waste))
    # `heroku.no_local_clone` is NAMED here as well as exercised: the board's
    # own rule is that every finding type must be named by some suite, and an
    # aggregate that fires on the live estate but appears in no test string is
    # a rule nobody can find from the other direction.
    absent = HF.findings({"apps": _rec(name="a")})
    check("an application with no checkout raises heroku.no_local_clone",
          any(f["type"] == "heroku.no_local_clone" for f in absent))
    check("an unlinked running application raises exactly ONE aggregate row",
          sum(1 for f in HF.findings({"apps": _rec(name="a") + _rec(name="b")})
              if f["type"] == "heroku.orphan_app") == 1,
          "one row per orphan would be the clone.stale defect again")


def test_a_finding_carries_no_timestamp():
    """A committed document must be a function of the facts, not of the clock —
    as a check rather than a habit."""
    import re
    rows = HF.findings({"apps": _rec(name="a", running=0)})
    stamped = [f for f in rows
               if re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d", json.dumps(f))]
    check("no finding carries an instant", not stamped,
          f"{[f['type'] for f in stamped]} — a timestamp makes every rebuild a diff")


# ── 5. the pipeline reaches all of it ───────────────────────────────────────
def test_a_stale_snapshot_withholds_every_estate_row():
    """`emit` rewrites the document every tick from a raw file only a manual run
    refreshes, so without this a fixed application keeps raising a critical and
    one that broke raises nothing."""
    import datetime
    doc = {"scanned_on": "2026-01-01", "apps": _rec(name="a", running=0)}
    on_time = HF.findings(doc, datetime.date(2026, 1, 2))
    late = HF.findings(doc, datetime.date(2026, 1, 9))
    check("inside the window the estate rows are built",
          any(f["type"] == "heroku.app_down" for f in on_time))
    check("past it every estate row is withheld",
          [f["type"] for f in late] == ["heroku.snapshot_stale"],
          f"got {[f['type'] for f in late]}")
    check("and the withholding is SAID, not silent", "8 days old" in late[0]["title"])
    check("the fresh rows name the day they were measured",
          all("Measured 2026-01-01." in f["detail"] for f in on_time),
          "a crash from an hour ago and one from two days ago need different remedies")


def test_a_curated_link_is_a_fact_with_a_half_life():
    """The project id a hand link names is DERIVED; renaming a folder kills the row."""
    doc = {"links": [
        {"app": "a", "project": "project:alpha", "evidence": "a sha"},
        {"app": "b", "project": "project:gone", "evidence": "a sha"},
        {"app": "c", "project": "project:alpha"},
        {"app": "vanished", "project": "project:alpha", "evidence": "a sha"},
    ]}
    errs = HR.curated_link_errors(doc, {"project:alpha"}, {"a", "b", "c"})
    joined = " | ".join(errs)
    check("a live link raises nothing", "for a," not in joined and " a," not in joined[:40])
    check("a dead project id is named", "project:gone" in joined)
    check("a row with no evidence is named", "carries no evidence" in joined)
    check("an application the scan no longer sees is named", "vanished" in joined)
    check("a clean document raises nothing",
          HR.curated_link_errors({"links": [doc["links"][0]]}, {"project:alpha"}, {"a"}) == [])


def test_the_step_is_reachable_and_the_page_shows_the_tab():
    import observatory
    check("the collector has a step", "heroku" in observatory.STEPS)
    check("and a group reaches it",
          any("heroku" in steps for steps in observatory.GROUPS.values()),
          "a step no group runs is a step that never runs")
    import paths
    page = paths.DASHBOARD_HTML
    if not page.is_file():
        check("the page was not built, so its surface is unchecked here", True)
        return
    html = page.read_text(encoding="utf-8")
    for chip in ("noproject", "nofolder", "norepo", "down", "waste", "stale", "oldstack"):
        check(f"the Heroku tab offers the «{chip}» filter", f'data-f="{chip}"' in html)
    check("the projects tab can ask the reverse question",
          'data-f="noheroku"' in html and 'data-f="heroku"' in html)
    # The page is translated from a catalog now, so the state words are message
    # ids passed through `T(...)` rather than a Russian-only table.
    check("state is a word, never a colour alone",
          "STATE_LABEL" in html and 'T("running")' in html and 'T("suspended")' in html)


def test_the_collector_never_writes_a_token():
    """Rule 6: no secrets, ever — asserted against the source, not remembered."""
    src = (ROOT / "collectors/scan_heroku.py").read_text(encoding="utf-8")
    body = src.split('def main(', 1)[1]
    check("the token never reaches the written document",
          '"token"' not in body and "tok," not in body.split("atomic.write_json")[-1])
    import paths
    raw = paths.SCRATCH / "heroku.json"
    if raw.is_file():
        text = raw.read_text(encoding="utf-8")
        check("and no scan on this machine has one in it",
              "HRKU" not in text and "Bearer" not in text and "Authorization" not in text)


def test_the_emitted_document_agrees_with_itself():
    """Planted scan, real emit: the registry document and its relations must agree.

    A sandboxed copy of the synthetic registry and scratch gets a two-application
    Heroku scan — one reached by the Deploy tab's GitHub link, one reached by
    nothing — and `collectors/emit_registry.py` runs over it.
    """
    import os
    import shutil
    import paths
    sys.path.insert(0, str(ROOT / "tests"))
    import tmp as tmpdir
    work = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-heroku-"))
    reg, raw = work / "registry", work / "raw"
    shutil.copytree(paths.REGISTRY, reg)  # paths-check: allow — the synthetic registry is the fixture's source
    shutil.copytree(paths.SCRATCH, raw)
    repos = json.loads((reg / "repositories.json").read_text(encoding="utf-8"))["repositories"]
    nwo = repos[0]["name_with_owner"]
    scan = {"scanned_at": "2026-01-01T00:00:00Z", "account": "owner@example.com", "teams": [],
            "apps": [app(name="alpha-web", github=nwo), app(name="beta-api", running=0)],
            "degraded": []}
    (raw / "heroku.json").write_text(json.dumps(scan), encoding="utf-8")
    env = dict(os.environ, OBSERVATORY_REGISTRY=str(reg), OBSERVATORY_SCRATCH=str(raw))
    p = subprocess.run([sys.executable, str(ROOT / "collectors/emit_registry.py")], env=env,
                       capture_output=True, text=True, timeout=300)
    check("the registry emits over a planted Heroku scan", p.returncode == 0, p.stderr[-300:])
    if p.returncode:
        return
    d = json.loads((reg / "heroku-apps.json").read_text(encoding="utf-8"))
    apps = d["apps"]
    t = d["totals"]
    check("totals.apps counts the rows it summarises", t["apps"] == len(apps) == 2, str(t))
    check("totals.monthly_cost sums the rows it summarises",
          abs(t["monthly_cost"] - round(sum(a["monthly_cost"] for a in apps), 2)) < 0.01)
    check("every state is one of the five",
          all(a["state"] in ("running", "down", "suspended", "resources-only", "idle")
              for a in apps))
    check("every linked application names a rule",
          all(bool(a.get("project")) == bool(a.get("link_rule")) for a in apps))
    check("the GitHub-linked application reaches a project",
          sum(1 for a in apps if a.get("project")) == 1, str([(a["name"], a.get("project")) for a in apps]))
    check("every unlinked application says why",
          all(a.get("unlinked_reason") for a in apps if not a.get("project")))
    rel = json.loads((reg / "relations.json").read_text(encoding="utf-8"))
    ids = {a["id"] for a in apps}
    edges = [r for r in rel["relations"] if r["type"] == "deployed_to"]
    check("`deployed_to` is a declared relation type",
          "deployed_to" in rel["relation_types"])
    check("no edge points at an application the document does not hold",
          all(e["to"] in ids for e in edges))
    check("one edge per linked application",
          len(edges) == sum(1 for a in apps if a.get("project")))


def test_the_config_trail_keeps_names_and_never_values() -> None:
    """Heroku's release descriptions name the variables a change touched."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("scan_heroku", ROOT / "collectors/scan_heroku.py")
    sh = importlib.util.module_from_spec(spec); spec.loader.exec_module(sh)
    rels = [{"version": 1087, "created_at": "2026-09-14T01:31:45Z", "description": "Deploy b18f95b7"},
            {"version": 1086, "created_at": "2026-09-14T01:21:38Z", "description": "Set ALPHA_DATABASE_URL config vars"},
            {"version": 1084, "created_at": "2026-09-14T01:21:10Z", "description": "Set BETA_CONNECTION_ID, FEATURE_ENABLED config vars"},
            {"version": 1081, "created_at": "2026-09-14T00:11:46Z", "description": "Update DATABASE by heroku-postgresql"}]
    trail = sh.config_trail(rels)
    check("only config-changing releases are kept, deploys are not",
          [r["version"] for r in trail] == [1086, 1084, 1081], str(trail))
    check("a Set release carries every variable NAME it lists",
          trail[1]["vars"] == ["BETA_CONNECTION_ID", "FEATURE_ENABLED"], str(trail[1]))
    check("an addon rotation carries the variable and the addon that did it",
          trail[2]["vars"] == ["DATABASE"] and trail[2]["by"] == "heroku-postgresql", str(trail[2]))


if __name__ == "__main__":
    print("heroku — six ways an inventory lies\n")
    for fn in (test_suspended_is_not_the_same_as_crashed,
               test_an_addon_billed_to_another_application_is_not_ours,
               test_a_name_is_never_evidence,
               test_the_three_measured_rules_each_fire,
               test_a_hand_link_without_evidence_is_refused,
               test_an_edge_exists_only_where_a_link_does,
               test_no_scan_produces_no_findings,
               test_each_rule_fires_on_its_own_subject,
               test_a_finding_carries_no_timestamp,
               test_a_stale_snapshot_withholds_every_estate_row,
               test_a_curated_link_is_a_fact_with_a_half_life,
               test_the_step_is_reachable_and_the_page_shows_the_tab,
               test_the_collector_never_writes_a_token,
               test_the_emitted_document_agrees_with_itself,
               test_the_config_trail_keeps_names_and_never_values):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma name is still not evidence, and a suspended app is not a crashed one\033[0m")
