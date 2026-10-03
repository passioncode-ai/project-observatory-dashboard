#!/usr/bin/env python3
"""Session attribution and ingestion over a synthetic estate.

Historical live counts remain in Git history; they are not installation criteria.
The scanner and store are real. Only their source data and locations are fixtures.
Lost-project finding remedies are driven by tests/test_lost_projects.py.
"""
from __future__ import annotations
import importlib, importlib.util, json, os, pathlib, sqlite3, subprocess, sys, tempfile
from unittest.mock import patch
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT/'tests'))
from session_fixture import estate as session_estate
from emitter_fixture import seed, environment
PY = str(ROOT/'.venv/bin/python') if (ROOT/'.venv/bin/python').exists() else sys.executable
FAILURES = []
def check(name, ok, detail=''):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f' — {detail}' if detail and not ok else ''))
    if not ok: FAILURES.append(name)
def load_collector():
    spec=importlib.util.spec_from_file_location('scan_sessions_t', ROOT/'collectors/scan_sessions.py')
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    # Pure attribution checks must not read the author's committed inventory.
    mod.paths=type('FixturePaths', (), {'REGISTRY': pathlib.Path('/nonexistent/synthetic-registry')})
    return mod

def test_the_attribution_rules_are_ordered_by_evidence() -> None:
    m = load_collector()
    # The display name is DIFFERENT from the slug on purpose. `Alpha` folds to
    # `alpha`, which is also the id slug, so the slug rule claims it first —
    # correctly, since rules are ordered by how much evidence they carry. A
    # fixture where they coincide tests the ordering, not the name rule.
    projects = [
        {"id": "project:alpha", "name": "Alpha Reporting",
         "local_folders": ["alpha-checkout"]},
        {"id": "project:beta", "name": "beta", "local_folders": []},
    ]
    index, _ = m.build_index(projects)
    for name, want_pid, want_rule in (
            ("alpha-checkout", "project:alpha", "local folder name"),
            ("alpha", "project:alpha", "project id slug"),
            ("Alpha Reporting", "project:alpha", "project name"),
            ("beta", "project:beta", "project id slug")):
        pid, rule = m.attribute(name, index)
        check(f"{name!r} attributes to {want_pid} by {want_rule}",
              (pid, rule) == (want_pid, want_rule), f"{pid} by {rule}")


def test_a_nested_name_belongs_to_its_FIRST_segment() -> None:
    """A nested folder is work inside its FIRST segment's project, not a project of its own."""
    m = load_collector()
    projects = [{"id": "project:skills", "name": "skills",
                 "local_folders": ["example-skills"]},
                {"id": "project:noddy", "name": "noddy", "local_folders": ["noddy"]}]
    index, _ = m.build_index(projects)
    pid, rule = m.attribute("example-skills/noddy", index)
    check("the first path segment wins", pid == "project:skills", f"{pid} by {rule}")
    check("and the rule says which name it came from", "example-skills/noddy" in rule, rule)


def test_scan_reports_unknown_and_excluded_without_overlap():
    with session_estate() as f:
        before = f.source.read_bytes()
        out = f.collector.scan()
        check('source bytes remain unchanged', f.source.read_bytes() == before)
        check('unknown names remain visible', out['unattributed'] == [{'name':'unknown-project','sessions':2,'verdict':'no-path-recorded','folders':[]}])
        check('unknown count is measured', out['counts']['unmatched_names'] == 1)
        excluded = {r['name'] for r in out['excluded']}
        check('named and shape exclusions remain separate', excluded == {'home-folder', '13.4.0'})
        check('exclusions carry their reasons', all(r['why'] for r in out['excluded']))
        check('unknown evidence is reported as degraded', any('cannot be attributed to one project here' in d['reason'] for d in out['degraded']))
        check('repository spelling reaches its declared owner', any(s['session_id']=='repository-session' and s['project_id']=='project:alpha' and s['matched_by']=='repository name' for s in out['sessions']))


def test_multi_project_sessions_and_replay_keep_distinct_work():
    with session_estate() as f:
        out = f.collector.scan()
        shared = [s for s in out['sessions'] if s['session_id']=='shared']
        check('one session reaches both projects', {s['project_id'] for s in shared} == {'project:alpha','project:beta'})
        alpha = next(s for s in shared if s['project_id']=='project:alpha')
        check('aliases are folded before insertion', alpha['prompts']==2 and ' + ' in alpha['claude_mem_project'])
        check('work on an external project is retained', any(s['project_id']=='project:beta' for s in shared))
        inserted = f.collector.to_events(out)
        check('every attributed session is inserted', inserted == len(out['sessions']) == 3)
        check('replay is idempotent', f.collector.to_events(out) == 0)
        conn=f.db.connect()
        try:
            check('shared session has two independently stored refs', conn.execute("SELECT count(DISTINCT project_id) FROM events WHERE ref LIKE 'shared:project:%'").fetchone()[0] == 2)
            check('events keep attribution evidence', all(json.loads(r[0])['matchedBy'] for r in conn.execute('SELECT payload_json FROM events')))
        finally: conn.close()


def test_equal_strength_owners_remain_ambiguous():
    with session_estate() as f:
        path=f.root/'registry/relations.json'; doc=json.loads(path.read_text())
        doc['relations'].append({'type':'implemented_by','from':'project:beta','to':'repository:fixture/service'})
        answers=[]
        for rows in (doc['relations'], list(reversed(doc['relations']))):
            path.write_text(json.dumps({'relations':rows}))
            index, conflicts=f.collector.build_index(list(reversed(f.projects)))
            answers.append(f.collector.attribute('service',index))
            check('shared repository has explicit candidates', conflicts.get('service')==['project:alpha','project:beta'])
        check('row order cannot select a project', answers[0]==answers[1] and answers[0][0] is None)
        conn=sqlite3.connect(f.source)
        conn.execute("INSERT INTO session_summaries SELECT 'shared','service',created_at,created_at_epoch FROM session_summaries LIMIT 1")
        conn.commit();conn.close()
        out=f.collector.scan()
        check('ambiguous work is not an attributed event', not any(s['session_id']=='repository-session' for s in out['sessions']))
        row=next((r for r in out['unattributed'] if r['name']=='service'),{})
        check('ambiguous raw fact retains evidence', row.get('verdict')=='ambiguous-project' and row.get('candidates')==['project:alpha','project:beta'] and row.get('session_ids')==['repository-session','shared'] and bool(row.get('reason')))
        f.collector.to_events(out)
        # A previous version picked a candidate. Reconciliation must withdraw
        # that derived event without deleting independently attributed work.
        conn=f.db.connect()
        conn.execute("INSERT INTO events(id,project_id,kind,ref,actor,occurred_at,payload_json) VALUES (?,?,?,?,?,?,?)",
                     ('old-guessed-event','project:alpha','session','repository-session:project:alpha','operator','2026-08-01','{}'))
        conn.commit();conn.close()
        f.collector.to_events(out)
        conn=f.db.connect()
        try:
            check('ambiguity cannot reach or remain in the event store', conn.execute("SELECT count(*) FROM events WHERE ref LIKE 'repository-session:%'").fetchone()[0]==0)
            check('independently supported project work survives reconciliation', conn.execute("SELECT count(*) FROM events WHERE ref LIKE 'shared:project:%'").fetchone()[0]==2)
            check('withdrawal is counted in the scan receipt', any(json.loads(r[0]).get('sessions_ambiguous_withdrawn')==1 for r in conn.execute('SELECT counts_json FROM scans')))
        finally:conn.close()


def test_evidence_priority_is_not_iteration_priority():
    m=load_collector()
    projects=[{'id':'project:alpha','name':'shared','local_folders':['shared']},
              {'id':'project:shared','name':'Other','local_folders':['shared']}]
    results=[]
    for rows in (projects,list(reversed(projects))):
        idx,conflicts=m.build_index(rows)
        results.append(m.attribute('shared',idx))
        check('weaker slug does not break a folder tie', conflicts.get('shared')==['project:alpha','project:shared'])
        check('nested name preserves folder ambiguity', m.attribute('shared/docs',idx)[0] is None)
    check('equal evidence has deterministic refusal', results[0]==results[1] and results[0][0] is None)
    projects[1]['local_folders']=[]
    idx,_=m.build_index(projects)
    check('unique stronger folder beats weaker slug',m.attribute('shared',idx)[0]=='project:alpha')
    idx,conflicts=m.build_index(projects+[projects[0]])
    check('duplicate assertion is not a new candidate', not conflicts and m.attribute('shared',idx)[0]=='project:alpha')


def test_dangling_repository_owner_cannot_receive_work():
    with session_estate() as f:
        path=f.root/'registry/relations.json'
        path.write_text(json.dumps({'relations':[{'type':'implemented_by','from':'project:absent','to':'repository:fixture/service'}]}))
        index,_=f.collector.build_index(f.projects)
        check('unknown project ID is not an attribution target', f.collector.attribute('service',index)[0] is None)


def test_missing_corrupt_or_changed_source_degrades():
    with session_estate() as f:
        original = f.collector.STORE
        f.collector.STORE=f.root/'missing.db'
        out=f.collector.scan()
        check('missing source is not created', not f.collector.STORE.exists())
        check('missing source explains fallback', out['counts']['sessions']==0 and any('falls back to commits alone' in d['reason'] for d in out['degraded']))
        f.collector.STORE=f.root/'corrupt.db'; f.collector.STORE.write_bytes(b'synthetic non-database')
        out=f.collector.scan()
        check('corrupt source is explicit', out['counts']['sessions']==0 and bool(out['degraded']))
        f.collector.STORE=original
        conn=sqlite3.connect(original); conn.execute('UPDATE session_summaries SET created_at_epoch=created_at_epoch/1000'); conn.commit();conn.close()
        out=f.collector.scan()
        check('seconds instead of milliseconds cannot claim silence', not out['sessions'] and any('milliseconds' in d['reason'] for d in out['degraded']))


def test_the_stop_hook_records_are_the_history_without_the_companion():
    """The companion was retired; the Stop hook's own `session` records answer
    on their own, and the companion's absence stops reading as lost history."""
    with session_estate() as f:
        import importlib
        os.environ['OBSERVATORY_DB'] = str(f.root/'store.db')
        try:
            import paths as real_paths
            importlib.reload(real_paths)
            from store import db as sdb, ledger as L
            importlib.reload(sdb)
            conn = sdb.connect(f.root/'store.db')
            first = L.append(conn, owner='agent:observatory-log', kind='session',
                             statement='turn one', project_id='project:alpha',
                             session_id='0f8fad5b-d9cb-469f-a165-70867728950e')
            L.append(conn, owner='agent:observatory-log', kind='session', statement='turn two',
                     memory_id=first['memoryId'], expected_revision=1, project_id='project:alpha',
                     session_id='0f8fad5b-d9cb-469f-a165-70867728950e')
            conn.close()
        finally:
            os.environ.pop('OBSERVATORY_DB', None)
        f.collector.STORE = f.root/'absent'/'claude-mem.db'
        out = f.collector.scan()
        mine = [s for s in out['sessions'] if s['matched_by'] == 'stop-hook']
        check('the hook\'s session is read', len(mine) == 1 and mine[0]['project_id'] == 'project:alpha'
              and mine[0]['prompts'] == 2, str(mine))
        check('and the companion\'s absence is not a degradation of the history',
              not any(d['source'] == 'claude-mem' for d in out['degraded']), str(out['degraded']))
        check('it is said as not applicable instead',
              any('Stop hook' in d['reason'] for d in out.get('not_applicable', [])),
              str(out.get('not_applicable')))


def test_empty_source_is_a_measured_empty_result():
    with session_estate() as f:
        conn=sqlite3.connect(f.source);conn.execute('DELETE FROM session_summaries');conn.commit();conn.close()
        out=f.collector.scan()
        check('empty valid source reports zero', out['counts']['sessions']==0)
        check('empty valid source is not a read failure', out['degraded']==[])


def test_emitter_preserves_session_date_and_citation():
    with tempfile.TemporaryDirectory(prefix='observatory-session-emit-') as td:
        root=pathlib.Path(td);seed(root)
        model=root/'raw/model.json';doc=json.loads(model.read_text())
        # The model already has the merge's dates. This tests projection, not merge.
        project=doc['projects']['fixture-a'];project['last_session_on']='2026-08-01';project['last_activity']='2026-08-01'
        model.write_text(json.dumps(doc))
        p=subprocess.run([PY,str(ROOT/'collectors/emit_registry.py'),str(root/'raw')],cwd=ROOT,env=environment(root),capture_output=True,text=True)
        check('real emitter accepts the synthetic model', p.returncode==0, p.stderr[-400:])
        if p.returncode: return
        emitted=json.loads((root/'registry/projects.json').read_text())['projects']
        rows={p['id']:p for p in emitted}
        check('session date survives projection', rows['project:fixture-a'].get('last_session_on')=='2026-08-01')
        check('session date carries its citation', 'SRC-0012' in rows['project:fixture-a']['source_refs'])
        check('unobserved project has no false session citation', 'SRC-0012' not in rows['project:fixture-b']['source_refs'])
        check('source definition exists', any(s['id']=='SRC-0012' for s in json.loads((root/'registry/sources.json').read_text())['sources']))


def test_session_events_cannot_inflate_a_rollup() -> None:
    """DRIVEN, because the SQL no longer answers it.

    This asserted `"WHERE kind='commit'" in src` — a fact about how the query
    was spelled, which stopped being true when the rollup learned to read
    sessions as their own columns. The INVARIANT it was protecting is
    unchanged and is what matters: a session must never be counted as a commit.
    So it is now measured on a fixture instead of read off the source, which is
    also the only version that would have caught the reverse mistake — a query
    naming both kinds and adding them into one column.
    """
    import importlib
    from store import rollup
    importlib.reload(rollup)
    from datetime import date
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE events (id TEXT PRIMARY KEY, project_id TEXT, repo_id TEXT,"
        " kind TEXT, ref TEXT, actor TEXT, occurred_at TEXT, payload_json TEXT);")
    schema = (ROOT / "store/schema.sql").read_text(encoding="utf-8")
    start = schema.index("CREATE TABLE IF NOT EXISTS project_week")
    conn.executescript(schema[start:schema.index(";", schema.index(");", start)) + 1])
    for i in range(7):
        conn.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
                     (f"session:{i}", "project:x", "r", "session", f"s{i}",
                      "operator", "2026-09-02T12:00:00Z", "{}"))
    conn.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
                 ("commit:1", "project:x", "r", "commit", "c1", "a",
                  "2026-09-02T13:00:00Z", "{}"))
    conn.commit()
    rollup.refresh(conn, today=date(2026, 9, 7))
    r = conn.execute("SELECT * FROM project_week WHERE project_id='project:x'").fetchone()
    check("seven sessions and one commit make ONE commit", r["commits"] == 1,
          f"{r['commits']} — a rollup counting sessions as commits corrupts every week")
    check("the sessions are counted where they belong", r["sessions"] == 7,
          str(r["sessions"]))
    check("and `authors` stays commit actors", r["authors"] == 1, str(r["authors"]))
    conn.close()


if __name__ == '__main__':
    for fn in (test_the_attribution_rules_are_ordered_by_evidence,
               test_a_nested_name_belongs_to_its_FIRST_segment,
               test_scan_reports_unknown_and_excluded_without_overlap,
               test_multi_project_sessions_and_replay_keep_distinct_work,
               test_equal_strength_owners_remain_ambiguous,
               test_evidence_priority_is_not_iteration_priority,
               test_dangling_repository_owner_cannot_receive_work,
               test_missing_corrupt_or_changed_source_degrades,
               test_empty_source_is_a_measured_empty_result,
               test_the_stop_hook_records_are_the_history_without_the_companion,
               test_emitter_preserves_session_date_and_citation,
               test_session_events_cannot_inflate_a_rollup,):
        fn()
    raise SystemExit(bool(FAILURES))
