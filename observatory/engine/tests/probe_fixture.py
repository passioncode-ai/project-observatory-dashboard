"""Synthetic state for the published probe inputs, whose IDs are fixed labels.

No inventory is copied. The probe inputs under `fabric/fixtures/` name
`project:example-app` (detail, timeline, admission) and `project:example-sibling`
(name inference, which needs a second project under the same owner); these are
compatibility labels here, not ownership measurements.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from emitter_fixture import seed as emitter_seed

ROOT = Path(__file__).resolve().parents[1]

#: The project the detail/timeline/admission probes ask about.
PROJECT = 'example-app'
#: Two projects owned by one synthetic organization, so name inference has a
#: sibling to find and an independent project it must not confuse with it.
MEMBERSHIPS = {PROJECT: ['fixture/web', 'fixture/local-only'],
               'example-sibling': ['example-org/fixture-chat'],
               'fixture-independent': ['example-org/fixture-independent']}


@contextmanager
def active():
    previous = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix='observatory-probe-fixture-') as td:
        root = Path(td).resolve()
        env = emitter_seed(root)
        # No curation override is written: the engine reads overrides from the
        # selected workspace's config/, which belongs to the caller and must not
        # be rewritten by a fixture. The probes do not depend on a description.
        for name in ('state', 'estate', 'wiki', 'empty-vault'):
            (root/name).mkdir()
        env.update(OBSERVATORY_DB=str(root/'state/data.db'),
                   OBSERVATORY_DATA=str(root/'estate'), OBSERVATORY_VAULT=str(root/'wiki'),
                   CLAUDE_MEM_DB=str(root/'absent-companion.db'),
                   OBSERVATORY_DASHBOARD=str(root/'page.html'))
        model = root/'raw/model.json'
        doc = json.loads(model.read_text())
        project = doc['projects']['fixture-a']
        repository = doc['repositories']['fixture/service']
        doc['projects'] = {key: dict(project, name='Synthetic '+key, repos=repos,
                                    owners=['example-org' if key != PROJECT else 'fixture'],
                                    rules=['Synthetic explicit membership: '+r for r in repos])
                           for key, repos in MEMBERSHIPS.items()}
        doc['repositories'] = {name: dict(repository, url='https://github.com/'+name)
                               for repos in MEMBERSHIPS.values() for name in repos}
        doc['repositories']['fixture/local-only'].update(
            host='bitbucket', url='https://bitbucket.org/fixture/local-only',
            source='local-remote-only')
        model.write_text(json.dumps(doc))
        (root/'raw/bitbucket.json').write_text(json.dumps({'degraded': [
            {'source': 'bitbucket:fixture', 'reason': 'Synthetic unavailable listing'}]}))
        p = subprocess.run([sys.executable, str(ROOT/'collectors/emit_registry.py'), str(root/'raw')],
                           cwd=ROOT, env=env, capture_output=True, text=True)
        if p.returncode:
            raise RuntimeError('probe fixture emitter failed: '+p.stderr[-300:])
        os.environ.update(env)
        import paths
        importlib.reload(paths)
        from store import db, ledger
        importlib.reload(db); importlib.reload(ledger)
        try:
            conn = db.connect(root/'state/data.db')
            now = datetime.now(timezone.utc)
            with conn:
                for index in range(3):
                    stamp = (now-timedelta(hours=index)).strftime('%Y-%m-%dT%H:%M:%SZ')
                    conn.execute('INSERT INTO events(id,project_id,kind,ref,actor,occurred_at,payload_json) VALUES (?,?,?,?,?,?,?)',
                                 (f'fixture-{index}', 'project:' + PROJECT, 'commit',
                                  f'fixture-{index}', 'fixture', stamp, '{"subject":"Synthetic work"}'))
            ledger.append(conn, owner='agent:fixture', statement='Synthetic proposed note',
                          why='Synthetic evidence', project_id='project:' + PROJECT, kind='observation')
            conn.close()
            (root/'registry/findings.json').write_text(json.dumps({'findings': [
                {'id':'finding:fixture', 'type':'repo.stale_remote', 'subject':'repository:fixture/web',
                 'severity':'warning', 'title':'Synthetic finding', 'detail':'Synthetic evidence',
                 'action':'Inspect fixture', 'evidence':['SRC-0007']}]}))
            yield root
        finally:
            os.environ.clear(); os.environ.update(previous)
            importlib.reload(paths); importlib.reload(db); importlib.reload(ledger)
