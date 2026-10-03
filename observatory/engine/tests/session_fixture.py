"""Disposable session source and registry, never the companion's real database."""
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def estate():
    with tempfile.TemporaryDirectory(prefix='observatory-session-fixture-') as td:
        root = Path(td)
        for folder in ('registry', 'collectors', 'estate'):
            (root / folder).mkdir()
        projects = [
            {'id': 'project:alpha', 'name': 'Alpha Reporting', 'local_folders': ['alpha-checkout'], 'ownership': 'owned'},
            {'id': 'project:beta', 'name': 'Beta', 'local_folders': ['beta'], 'ownership': 'external'},
        ]
        def write(relative, doc):
            (root / relative).write_text(json.dumps(doc))
        write('registry/projects.json', {'projects': projects})
        write('registry/repositories.json', {'repositories': [{'id': 'repository:fixture/service', 'name_with_owner': 'fixture/service'}]})
        write('registry/relations.json', {'relations': [{'type': 'implemented_by', 'from': 'project:alpha', 'to': 'repository:fixture/service'}]})
        write('collectors/session_name_exclusions.json', {'names': [{'name': 'home-folder', 'why': 'Synthetic home folder is explicitly excluded from project attribution.'}], 'shape_rules': [{'id': 'semver', 'pattern': r'^\d+\.\d+\.\d+$', 'why': 'Synthetic version directories are not projects in this test estate.'}]})
        source = root / 'companion.db'
        conn = sqlite3.connect(source)
        conn.executescript('CREATE TABLE session_summaries(memory_session_id TEXT, project TEXT, created_at TEXT, created_at_epoch INTEGER); CREATE TABLE observations(project TEXT, files_read TEXT, files_modified TEXT);')
        stamp = datetime.now(timezone.utc)
        rows = [('shared', 'alpha-checkout'), ('shared', 'alpha-checkout/docs'),
                ('shared', 'beta'), ('repository-session', 'service'),
                ('unknown-1', 'unknown-project'), ('unknown-2', 'unknown-project'),
                ('excluded-1', 'home-folder'), ('excluded-2', 'home-folder'),
                ('version', '13.4.0')]
        conn.executemany('INSERT INTO session_summaries VALUES (?,?,?,?)',
                         [(sid, name, stamp.isoformat(), int(stamp.timestamp()*1000)) for sid, name in rows])
        conn.commit(); conn.close()
        spec = importlib.util.spec_from_file_location('sessions_fixture_collector', ROOT / 'collectors/scan_sessions.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        module.paths = SimpleNamespace(ROOT=root, CURATION=root/'collectors', REGISTRY=root/'registry', DATA=root/'estate',
                                       DB=root/'store.db', config_file=lambda name: root/'collectors'/name)
        module.STORE = source
        from store import db
        with patch.object(db, 'DB_PATH', root/'events.db'):
            yield SimpleNamespace(root=root, source=source, collector=module, projects=projects, db=db)
