#!/usr/bin/env python3
"""Publishing a project detached its own history, silently.

A project with no remote is anchored on its folder and gets the id
`project:local-<folder>` (`collectors/merge.py`). Give it a remote and it becomes
repository-anchored: `project:<owner>-<name>`. **Every row already keyed to the
old id is then orphaned** — and `memory.orphan_subject` says exactly what that
costs: "a note keyed to a subject the registry does not hold never appears in
that project's view."

The stake is largest for a project an agent has been working in for a while
without a remote: all of its measured commit events are one `git remote add`
away from detaching in a single step — in a system whose purpose is to keep the
record of work per project.

**The resolution needs no new state and asserts nothing.** The folder name is
inside the old id, the registry says which project owns that folder now, and
comparing MINTED ids rather than inverting the slug is exact:
`re.sub(r"[^a-z0-9.-]+","-",…)` maps both `a_b` and `a-b` to `a-b`, so an inverse
would be a guess. So `identity.py` owns the one rule for how a local-folder
project is named, `merge.py` mints through it, and readers ask it for the ids a
project used to carry.
"""
from __future__ import annotations
import importlib, json, os, pathlib, sqlite3, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir                                                # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


# ─────────── one rule for how a local-folder project is named ──────────

def test_the_naming_rule_lives_in_one_place() -> None:
    import identity as I
    importlib.reload(I)
    check("a folder becomes a key", I.local_key("sample-site") == "local-sample-site",
          I.local_key("sample-site"))
    check("and an id", I.local_id("sample-site") == "project:local-sample-site",
          I.local_id("sample-site"))
    check("the slug is applied, not assumed",
          I.local_key("Some_Folder Name") == "local-some-folder-name",
          I.local_key("Some_Folder Name"))
    check("a dot survives, because the registry's slug keeps it",
          I.local_key("example.chat") == "local-example.chat", I.local_key("example.chat"))
    src = (ROOT / "collectors/merge.py").read_text(encoding="utf-8")
    check("and the merge mints through it rather than spelling it again",
          "identity.local_key" in src or "local_key(" in src,
          "a second spelling of an id rule is how two ids for one project appear")


def test_a_projects_former_ids_are_derived_from_its_folders() -> None:
    import identity as I
    importlib.reload(I)
    p = {"id": "project:sample-site", "anchor": "vault-folder",
         "local_folders": ["sample-site"]}
    check("the id it would have had as a local folder is offered",
          I.former_ids(p) == ["project:local-sample-site"], str(I.former_ids(p)))
    many = {"id": "project:x", "anchor": "repository",
            "local_folders": ["a", "b"]}
    check("one per folder", I.former_ids(many) ==
          ["project:local-a", "project:local-b"], str(I.former_ids(many)))
    own = {"id": "project:local-solo", "anchor": "local-folder",
           "local_folders": ["solo"]}
    check("a project still anchored on its folder has no FORMER id",
          I.former_ids(own) == [],
          "its own id is not a former one, and offering it would make every "
          "lookup resolve to itself")
    check("no folders, no former ids",
          I.former_ids({"id": "project:y", "local_folders": []}) == [], "")


def test_the_index_maps_old_ids_to_the_project_that_holds_them_now() -> None:
    import identity as I
    importlib.reload(I)
    projects = [
        {"id": "project:sample-site", "anchor": "vault-folder", "local_folders": ["sample-site"]},
        {"id": "project:local-sample-agent", "anchor": "local-folder",
         "local_folders": ["sample-agent"]},
    ]
    idx = I.former_index(projects)
    check("the published project claims its former id",
          idx.get("project:local-sample-site") == "project:sample-site", str(idx))
    check("and the unpublished one claims nothing",
          "project:local-sample-agent" not in idx, str(idx))
    check("resolving an id nothing claims answers None",
          I.resolve_former("project:local-nowhere", projects) is None, "")
    check("and resolving a live id answers None too",
          I.resolve_former("project:sample-site", projects) is None,
          "a resolver that returns the input teaches its callers nothing")


# ─────────── the finding tells the two apart ───────────────────────────

def findings_for(rows: list[tuple[str, str]], projects: list[dict]) -> list[dict]:
    """`rows` are (project_id, statement) ledger rows."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-ident-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "registry/projects.json").write_text(json.dumps({"projects": projects}))
    (d / "registry/repositories.json").write_text('{"repositories": []}')
    (d / "registry/relations.json").write_text('{"relations": []}')
    (d / "registry/sources.json").write_text('{"sources": []}')
    db = d / "observatory.db"
    env = dict(os.environ, OBSERVATORY_DB=str(db),
               OBSERVATORY_REGISTRY=str(d / "registry"),
               OBSERVATORY_SCRATCH=str(d / "scratch"))
    subprocess.run([PY, "store/migrate.py"], cwd=ROOT, env=env,
                   capture_output=True, text=True, timeout=600)
    con = sqlite3.connect(db)
    for i, (pid, stmt) in enumerate(rows):
        con.execute(
            "INSERT INTO ledger (memory_id, revision, kind, project_id, owner,"
            " function, scope, statement, state, confidence, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"mem:{i:016x}", 1, "observation", pid, "agent:observer",
             "semantic", "project", stmt, "proposed", 0.5, "2026-09-07T00:00:00Z"))
    con.commit()
    con.close()
    for k, v in env.items():
        if k.startswith("OBSERVATORY_"):
            os.environ[k] = v
    import paths
    importlib.reload(paths)
    import build_findings as B
    importlib.reload(B)
    try:
        return [f for f in B.collect() if f["type"].startswith("memory.")]
    finally:
        for k in ("OBSERVATORY_DB", "OBSERVATORY_REGISTRY", "OBSERVATORY_SCRATCH"):
            os.environ.pop(k, None)
        importlib.reload(paths)


PUBLISHED = {"id": "project:sample-site", "name": "sample-site", "anchor": "vault-folder",
             "lifecycle": "active", "ownership": "owned",
             "local_folders": ["sample-site"], "membership_rules": [],
             "sites": [], "stack": [], "has_vault_note": False}


def test_a_row_that_follows_a_publication_is_not_called_an_orphan() -> None:
    got = findings_for([("project:local-sample-site", "it was worked on")], [PUBLISHED])
    orphan = [f for f in got if f["type"] == "memory.orphan_subject"]
    check("no orphan is reported", orphan == [],
          "the id resolves to project:sample-site, which the registry does hold")
    followed = [f for f in got if f["type"] == "memory.followed_rename"]
    check("the transition is reported instead", len(followed) == 1, str(got)[:220])
    if followed:
        f = followed[0]
        check("as info, since nothing is lost", f["severity"] == "info", f["severity"])
        check("naming both ids",
              "project:local-sample-site" in f["detail"] and "project:sample-site" in f["detail"],
              f["detail"][:240])
        check("and saying the view follows it",
              "view" in f["detail"] or "resolved" in f["detail"], f["detail"][:240])


def test_a_row_nothing_claims_is_still_an_orphan() -> None:
    got = findings_for([("project:gone-for-good", "a note about nothing")], [PUBLISHED])
    orphan = [f for f in got if f["type"] == "memory.orphan_subject"]
    check("it is reported", len(orphan) == 1, str(got)[:220])
    if orphan:
        check("naming the id", "project:gone-for-good" in orphan[0]["detail"],
              orphan[0]["detail"][:200])
        check("and it does not claim a resolution",
              "resolve" not in orphan[0]["detail"].lower()
              or "no project" in orphan[0]["detail"].lower(),
              orphan[0]["detail"][:240])


def test_the_two_kinds_are_counted_apart() -> None:
    got = findings_for([("project:local-sample-site", "followed"),
                        ("project:gone-for-good", "really gone"),
                        ("project:also-gone", "also really gone")], [PUBLISHED])
    orphan = [f for f in got if f["type"] == "memory.orphan_subject"]
    followed = [f for f in got if f["type"] == "memory.followed_rename"]
    check("one orphan finding covering two ids", len(orphan) == 1, str(got)[:200])
    if orphan:
        check("and its title counts two, not three", "2 project id" in orphan[0]["title"],
              orphan[0]["title"])
    check("with the followed one reported separately", len(followed) == 1, str(got)[:200])


def test_a_registry_with_no_orphans_is_silent() -> None:
    got = findings_for([("project:sample-site", "a live note")], [PUBLISHED])
    check("nothing is raised", got == [], str(got)[:200])


# ─────────── the project's view follows the former id ──────────────────

def planted_store() -> tuple[pathlib.Path, dict]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-view-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "registry/projects.json").write_text(json.dumps({"projects": [PUBLISHED]}))
    (d / "registry/repositories.json").write_text('{"repositories": []}')
    (d / "registry/relations.json").write_text('{"relations": []}')
    (d / "registry/sources.json").write_text('{"sources": []}')
    db = d / "observatory.db"
    env = dict(os.environ, OBSERVATORY_DB=str(db),
               OBSERVATORY_REGISTRY=str(d / "registry"),
               OBSERVATORY_SCRATCH=str(d / "scratch"))
    subprocess.run([PY, "store/migrate.py"], cwd=ROOT, env=env,
                   capture_output=True, text=True, timeout=600)
    con = sqlite3.connect(db)
    # One event and one note under the OLD id, one of each under the new one.
    for pid, ref in (("project:local-sample-site", "old-sha"),
                     ("project:sample-site", "new-sha")):
        con.execute("INSERT INTO events (id, project_id, repo_id, kind, ref, actor,"
                    " occurred_at, payload_json) VALUES (?,?,?,?,?,?,?,?)",
                    (f"commit:{ref}", pid, None, "commit", ref, "t",
                     "2026-09-06T00:00:00Z", "{}"))
    for i, pid in enumerate(("project:local-sample-site", "project:sample-site")):
        con.execute(
            "INSERT INTO ledger (memory_id, revision, kind, project_id, owner,"
            " function, scope, statement, state, confidence, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"mem:v{i:015x}", 1, "observation", pid, "agent:observer",
             "semantic", "project", f"note under {pid}", "observed", 0.9,
             "2026-09-06T00:00:00Z"))
    con.commit()
    con.close()
    for k, v in env.items():
        if k.startswith("OBSERVATORY_"):
            os.environ[k] = v
    import paths
    importlib.reload(paths)
    # `store.db` as well: it captures `paths.DB` at ITS import, so reloading only
    # `paths` leaves survey opening the LIVE store — the order
    # tests/test_project_surface.py already established.
    from store import db as store_db
    importlib.reload(store_db)
    return d, env


def test_the_timeline_includes_what_was_recorded_before_publication() -> None:
    d, env = planted_store()
    try:
        import survey
        importlib.reload(survey)                                   
        tl = survey.timeline("project:sample-site")
        refs = sorted(e["ref"] for e in tl.get("events") or [])
        check("both events are in the timeline", refs == ["new-sha", "old-sha"],
              str(refs) + " " + json.dumps(tl.get("degraded") or []))
    finally:
        for k in ("OBSERVATORY_DB", "OBSERVATORY_REGISTRY", "OBSERVATORY_SCRATCH"):
            os.environ.pop(k, None)
        import paths
        importlib.reload(paths)


def test_the_detail_includes_the_notes_written_before_publication() -> None:
    d, env = planted_store()
    try:
        import survey
        importlib.reload(survey)                                   
        det = survey.project_detail("project:sample-site")
        blob = json.dumps(det, ensure_ascii=False)
        check("the note written under the old id is present",
              "note under project:local-sample-site" in blob, blob[:300])
        check("and so is the new one",
              "note under project:sample-site" in blob, blob[:300])
        check("the answer still says which project it is about",
              det.get("project", {}).get("id") == "project:sample-site"
              or det.get("projectId") == "project:sample-site", json.dumps(det)[:200])
    finally:
        for k in ("OBSERVATORY_DB", "OBSERVATORY_REGISTRY", "OBSERVATORY_SCRATCH"):
            os.environ.pop(k, None)
        import paths
        importlib.reload(paths)


def test_an_unpublished_project_asks_for_no_extra_ids() -> None:
    """A `local-folder`-anchored project must not widen its own query to its own
    id twice — harmless, but it would mean the resolver was returning itself."""
    import identity as I
    importlib.reload(I)
    p = {"id": "project:local-solo", "anchor": "local-folder", "local_folders": ["solo"]}
    check("the id set is just the project",
          I.ids_for(p) == ["project:local-solo"], str(I.ids_for(p)))
    pub = {"id": "project:sample-site", "anchor": "vault-folder",
           "local_folders": ["sample-site"]}
    check('without population context only the current ID is returned',
          I.ids_for(pub) == [pub['id']])
    check("and a published one carries both",
          I.ids_for(pub, [pub]) == ["project:sample-site", "project:local-sample-site"],
          str(I.ids_for(pub, [pub])))


# ─────────── the rollup folds instead of the reader summing ────────────

def test_the_rollup_folds_a_former_id_into_the_current_one() -> None:
    """Widening the reader's query to former ids makes one ISO week arrive twice
    for a published project — once under the former id and once under the new
    one — so the week would be counted twice. Folding at WRITE time is what
    makes the numbers right: `active_days`, `authors` and `worked_days` are set
    sizes computed from the events, and two rows of those cannot be added.
    """
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-fold-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "registry/projects.json").write_text(json.dumps({"projects": [PUBLISHED]}))
    for name, body in (("repositories.json", '{"repositories": []}'),
                       ("relations.json", '{"relations": []}'),
                       ("sources.json", '{"sources": []}')):
        (d / "registry" / name).write_text(body)
    db = d / "observatory.db"
    env = dict(os.environ, OBSERVATORY_DB=str(db),
               OBSERVATORY_REGISTRY=str(d / "registry"),
               OBSERVATORY_SCRATCH=str(d / "scratch"))
    subprocess.run([PY, "store/migrate.py"], cwd=ROOT, env=env,
                   capture_output=True, text=True, timeout=600)
    con = sqlite3.connect(db)
    # THE SAME DAY under both ids, by two different authors. A reader summing
    # two rows would report 2 active days; the truth is 1.
    stamp = "2026-09-07T10:00:00Z"
    for i, (pid, actor) in enumerate((("project:local-sample-site", "ann"),
                                      ("project:sample-site", "bob"))):
        con.execute("INSERT INTO events (id, project_id, repo_id, kind, ref, actor,"
                    " occurred_at, payload_json) VALUES (?,?,?,?,?,?,?,?)",
                    (f"commit:c{i}", pid, None, "commit", f"c{i}", actor, stamp, "{}"))
    con.commit()
    con.close()
    r = subprocess.run([PY, "store/rollup.py", "refresh"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=900)
    check("the rollup runs", r.returncode == 0, (r.stdout + r.stderr)[-300:])
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute("select project_id, week, commits, active_days, authors"
                       " from project_week order by project_id").fetchall()
    con.close()
    check("one row, not two", len(rows) == 1,
          str([(x["project_id"], x["week"], x["commits"]) for x in rows]))
    if not rows:
        return
    row = rows[0]
    check("under the project's current id", row["project_id"] == "project:sample-site",
          row["project_id"])
    check("with both commits", row["commits"] == 2, str(row["commits"]))
    check("ONE active day, because it is a set size and not a sum",
          row["active_days"] == 1, str(row["active_days"]))
    check("and both authors", row["authors"] == 2, str(row["authors"]))
    rep = json.loads((d / "scratch/rollup.json").read_text(encoding="utf-8"))
    check("the receipt names what a rename moved",
          "weeks_superseded_by_rename" in rep and "weeks_kept_unfolded" in rep,
          str(sorted(rep)))


def test_a_former_row_with_no_counterpart_is_kept_and_counted() -> None:
    """Deleting a row whose week was NOT recomputed would lose it: a frozen week
    whose events retention has pruned holds numbers nothing can reproduce."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-keep-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "registry/projects.json").write_text(json.dumps({"projects": [PUBLISHED]}))
    for name, body in (("repositories.json", '{"repositories": []}'),
                       ("relations.json", '{"relations": []}'),
                       ("sources.json", '{"sources": []}')):
        (d / "registry" / name).write_text(body)
    db = d / "observatory.db"
    env = dict(os.environ, OBSERVATORY_DB=str(db),
               OBSERVATORY_REGISTRY=str(d / "registry"),
               OBSERVATORY_SCRATCH=str(d / "scratch"))
    subprocess.run([PY, "store/migrate.py"], cwd=ROOT, env=env,
                   capture_output=True, text=True, timeout=600)
    con = sqlite3.connect(db)
    # A week under the FORMER id with no events behind it at all.
    con.execute("INSERT INTO project_week (project_id, week, week_start, commits,"
                " active_days, authors, computed_at, frozen_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                ("project:local-sample-site", "2020-W01", "2019-12-30", 9, 3, 1,
                 "2020-01-06T00:00:00Z", "2020-01-06T00:00:00Z"))
    con.commit()
    con.close()
    r = subprocess.run([PY, "store/rollup.py", "refresh"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=900)
    check("the rollup runs", r.returncode == 0, (r.stdout + r.stderr)[-300:])
    con = sqlite3.connect(db)
    left = con.execute("select count(*) from project_week where project_id = ?",
                       ("project:local-sample-site",)).fetchone()[0]
    con.close()
    check("the irreproducible row survives", left == 1, str(left))
    rep = json.loads((d / "scratch/rollup.json").read_text(encoding="utf-8"))
    check("and it is counted rather than silently kept",
          rep.get("weeks_kept_unfolded") == 1, json.dumps(rep)[:200])
    check("nothing was reported as superseded",
          rep.get("weeks_superseded_by_rename") == 0, json.dumps(rep)[:200])


def test_ambiguous_aliases_never_choose_by_order() -> None:
    import identity as I
    a = {'id':'project:a','anchor':'repository','local_folders':['shared']}
    b = {'id':'project:b','anchor':'repository','local_folders':['shared']}
    for rows in ([a,b], [b,a]):
        check('a shared folder claims no former ID', I.former_index(rows) == {}, str(I.former_index(rows)))
    b = {**b, 'local_folders':['a-b']}
    a = {**a, 'local_folders':['a_b']}
    check('colliding minted slugs claim no former ID', I.former_index([a,b]) == {})
    check('duplicate evidence for one project remains unambiguous',
          I.former_index([a,a]) == {'project:local-a-b':'project:a'})
    live = {'id':'project:local-a-b','anchor':'local-folder','local_folders':['a-b']}
    check('a live ID cannot be taken by a former-ID guess', I.former_index([a,live]) == {})
    check('resolving a live collision returns no rename', I.resolve_former(live['id'],[a,live]) is None)


def test_ambiguous_history_stays_under_its_original_id() -> None:
    import surface_fixture
    for reverse in (False, True):
        with surface_fixture.active() as root:
            import survey
            from store import db, ledger
            from datetime import datetime, timezone
            path=root/'registry/projects.json';doc=json.loads(path.read_text())
            for p in doc['projects']:
                p['anchor']='repository';p['local_folders']=['shared']
            if reverse:doc['projects'].reverse()
            path.write_text(json.dumps(doc))
            old='project:local-shared';stamp=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            conn=db.connect()
            with conn:
                conn.execute('INSERT INTO events(id,project_id,kind,ref,occurred_at,payload_json) VALUES (?,?,?,?,?,?)',
                             ('legacy-event',old,'commit','legacy-ref',stamp,'{}'))
            ledger.append(conn, owner='agent:fixture', statement='Ambiguous legacy note', why='Synthetic observation', project_id=old, kind='observation')
            conn.close()
            for pid in (surface_fixture.PROJECT, surface_fixture.UNMEASURED):
                tl=survey.timeline(pid)
                check('candidate timeline does not borrow ambiguous history',
                      'legacy-ref' not in [e['ref'] for e in tl['events']])
                detail=survey.project_detail(pid, timeline_limit=0)
                check('candidate notes do not borrow ambiguous history',
                      not any(n['statement']=='Ambiguous legacy note' for n in detail['notes']))
                check('ambiguous history is reported on the project surface',
                      any(d['source']=='identity' for d in detail['degraded']))
            check('the original ID still returns its event',
                  [e['ref'] for e in survey.timeline(old)['events']] == ['legacy-ref'])
            result=subprocess.run([PY,'store/rollup.py','refresh'],cwd=ROOT,env=dict(os.environ),capture_output=True,text=True,timeout=120)
            check('rollup refresh runs on the ambiguous fixture',result.returncode==0,result.stderr[-300:])
            conn=db.connect()
            counts={r['project_id']:r['commits'] for r in conn.execute('SELECT project_id,commits FROM project_week')}
            check('newly computed history is not folded into a guessed owner',counts.get(old)==1 and counts.get(surface_fixture.PROJECT)==1 and surface_fixture.UNMEASURED not in counts,str(counts))
            check('source event and note remain under their recorded ID',
                  conn.execute('SELECT count(*) FROM events WHERE project_id=?',(old,)).fetchone()[0]==1 and
                  conn.execute('SELECT count(*) FROM ledger WHERE project_id=?',(old,)).fetchone()[0]==1)
            conn.close()
            result=survey.survey(include_external=True)
            rows={p['id']:p for p in result['projects']}
            check('survey uses the same unique mapping as detail and rollup',
                  rows[surface_fixture.PROJECT]['recentActivity']['commits']==1 and
                  'recentActivity' not in rows[surface_fixture.UNMEASURED])
            check('estate totals retain the unresolved work explicitly',
                  result['estateWork']['commitsUnattributed']==1)
            import build_findings
            memory=[f for f in build_findings.collect() if f['type'].startswith('memory.')]
            check('ambiguous notes are not announced as a followed rename',
                  not any(f['type']=='memory.followed_rename' for f in memory) and
                  any('no unambiguous project' in f['detail'] for f in memory))


def test_an_overridden_project_keeps_its_relation_ids() -> None:
    """With identity_overrides pinning a project id, its edges must be named by that id."""
    sys.path.insert(0, str(ROOT / "tests"))
    from emitter_fixture import seed
    root = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-override-rel-")).resolve()
    env = seed(root)
    home = root / "home"
    env = {**env, "OBSERVATORY_HOME": str(home)}
    subprocess.run([sys.executable, str(ROOT / "observatory.py"), "init"], cwd=ROOT, env=env,
                   capture_output=True, timeout=120, check=True)
    (home / "config/identity_overrides.json").write_text(json.dumps({"overrides": {"fixture-a": "stable-a"}}))
    p = subprocess.run([sys.executable, str(ROOT / "collectors/emit_registry.py")], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    check("the emitter runs with an override", p.returncode == 0, (p.stdout + p.stderr)[-300:])
    rels = json.loads((root / "registry/relations.json").read_text())["relations"]
    mine = [r for r in rels if r.get("from") == "project:stable-a"]
    check("the overridden project has its implemented_by edge", bool(mine), str(rels)[:300])
    check("and the edge id names the pinned id, not the merge key",
          all(r["id"].startswith("relation:stable-a:") for r in mine), str([r["id"] for r in mine]))
    check("and every implemented_by edge says why it exists",
          all(r.get("rule") for r in rels if r["type"] == "implemented_by"), str(rels)[:300])


def test_a_row_without_ownership_is_named_not_fatal() -> None:
    """OBS-24: a hand-made registry row with no `ownership` took the survey down."""
    d, env = planted_store()
    unowned = {k: v for k, v in PUBLISHED.items() if k != "ownership"}
    unowned["id"] = "project:hand-made"
    (d / "registry/projects.json").write_text(json.dumps({"projects": [PUBLISHED, unowned]}))
    import survey
    importlib.reload(survey)
    try:
        got = survey.survey({"kind": "estate"})
    except KeyError as exc:
        check("a row without ownership does not raise", False, repr(exc))
        return
    ids = [p["id"] for p in got["projects"]]
    check("the well-formed project is served", "project:sample-site" in ids, str(ids))
    check("the row without ownership is not served", "project:hand-made" not in ids, str(ids))
    check("and it is named in degraded",
          any(x.get("source") == "registry" and "project:hand-made" in x.get("reason", "")
              for x in got["degraded"]), str(got["degraded"])[:300])


if __name__ == "__main__":
    print("project identity — publishing a project detached its history\n")
    for fn in (test_a_row_without_ownership_is_named_not_fatal,
               test_an_overridden_project_keeps_its_relation_ids,
               test_the_naming_rule_lives_in_one_place,
               test_a_projects_former_ids_are_derived_from_its_folders,
               test_the_index_maps_old_ids_to_the_project_that_holds_them_now,
               test_a_row_that_follows_a_publication_is_not_called_an_orphan,
               test_a_row_nothing_claims_is_still_an_orphan,
               test_the_two_kinds_are_counted_apart,
               test_a_registry_with_no_orphans_is_silent,
               test_the_timeline_includes_what_was_recorded_before_publication,
               test_the_detail_includes_the_notes_written_before_publication,
               test_an_unpublished_project_asks_for_no_extra_ids,
               test_the_rollup_folds_a_former_id_into_the_current_one,
               test_a_former_row_with_no_counterpart_is_kept_and_counted,
               test_ambiguous_aliases_never_choose_by_order,
               test_ambiguous_history_stays_under_its_original_id):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma published project keeps the history it had before\033[0m")
