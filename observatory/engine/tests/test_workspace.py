"""Fresh installs, stable config and full original local workflow in isolated homes."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
from run_portable import runtime_environment

class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "instance"
        self.user = self.base / "user"
        self.user.mkdir()
        self.env = {**runtime_environment(), "PATH": os.environ.get("PATH", ""), "HOME": str(self.user),
                    "OBSERVATORY_HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C"}
    def tearDown(self):
        self.temp.cleanup()
    def run_cli(self, *args, ok=True):
        p = subprocess.run([sys.executable, str(ROOT / "observatory.py"), *args], cwd=ROOT,
                           env=self.env, text=True, capture_output=True, timeout=60)
        if ok:
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p
    def test_init_preserves_settings_and_unknown_optional_fields(self):
        self.run_cli('init')
        file = self.home/'config/settings.json'
        doc=json.loads(file.read_text());doc['extension_metadata']={'example':True}
        file.write_text(json.dumps(doc))
        self.run_cli('configure','features','scheduler','false')
        before=file.read_bytes()
        self.run_cli('init')
        self.assertEqual(before,file.read_bytes())
        self.assertEqual(json.loads(before)['extension_metadata'],{'example':True})
        self.assertEqual(file.stat().st_mode & 0o777,0o600)
    def test_init_creates_runtime_identities_once_and_never_replaces_them(self):
        self.run_cli('init')
        files = [self.home / 'store' / name for name in ('.keyserver-token', '.env-fingerprint-salt')]
        for f in files:
            self.assertTrue(f.is_file(), f)
            self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        before = [f.read_bytes() for f in files]
        self.run_cli('init')
        self.assertEqual(before, [f.read_bytes() for f in files], "re-init must keep identities")
        files[1].unlink()
        self.run_cli('init')
        self.assertTrue(files[1].is_file(), "re-init restores a missing identity")
        self.assertNotEqual(before[1], files[1].read_bytes())
        self.assertEqual(before[0], files[0].read_bytes())
    def test_doctor_names_enabled_switches_with_missing_sources(self):
        self.run_cli('init')
        self.run_cli('configure', 'integrations', 'sessions', 'true')
        self.run_cli('configure', 'features', 'companion_remediation', 'true')
        warnings = json.loads(self.run_cli('doctor').stdout)['coverage_warnings']
        named = {(w.get('integration') or w.get('feature'), w['source']) for w in warnings}
        self.assertIn(('sessions', 'sessions'), named)
        self.assertIn(('companion_remediation', 'companion_home'), named)
        transcripts = self.base / 'transcripts'
        transcripts.mkdir()
        self.run_cli('configure', 'sources', 'sessions', str(transcripts))
        warnings = json.loads(self.run_cli('doctor').stdout)['coverage_warnings']
        self.assertNotIn('sessions', {w['source'] for w in warnings})
        self.run_cli('configure', 'sources', 'sessions', str(self.base / 'gone'))
        warnings = json.loads(self.run_cli('doctor').stdout)['coverage_warnings']
        self.assertTrue(any(w['source'] == 'sessions' and 'does not exist' in w['problem'] for w in warnings))

    def test_doctor_names_an_unset_or_missing_projects_source(self):
        # The base scan reads `sources.projects`; with it unset, `full local` used to
        # end in a FileNotFoundError traceback while doctor reported no gap at all.
        self.run_cli('init')
        warnings = json.loads(self.run_cli('doctor').stdout)['coverage_warnings']
        row = next((w for w in warnings if w['source'] == 'projects'), None)
        self.assertIsNotNone(row, warnings)
        self.assertEqual(row['collector'], 'filesystem')
        self.assertIn('not configured', row['problem'])
        self.assertEqual(row['fix'], 'project-observatory full configure sources projects PATH')
        self.run_cli('configure', 'sources', 'projects', str(self.base / 'gone-projects'))
        warnings = json.loads(self.run_cli('doctor').stdout)['coverage_warnings']
        self.assertTrue(any(w['source'] == 'projects' and 'does not exist' in w['problem'] for w in warnings), warnings)
        (self.base / 'gone-projects').mkdir()
        self.assertEqual(json.loads(self.run_cli('doctor').stdout)['coverage_warnings'], [])

    def configure_projects(self):
        projects = self.base / 'projects'
        projects.mkdir(exist_ok=True)
        self.run_cli('configure', 'sources', 'projects', str(projects))

    def test_doctor_names_a_configured_source_that_was_deleted_with_its_fix(self):
        # The sessions collector reads the companion's database: pointed at a file
        # that is gone, every tick logs DEGRADED, so doctor must not stay silent.
        self.run_cli('init')
        self.configure_projects()
        db = self.base / 'companion.db'
        db.write_bytes(b'')
        transcripts = self.base / 'transcripts'
        transcripts.mkdir()
        self.run_cli('configure', 'integrations', 'sessions', 'true')
        self.run_cli('configure', 'sources', 'sessions', str(transcripts))
        self.run_cli('configure', 'sources', 'companion_db', str(db))
        self.assertEqual(json.loads(self.run_cli('doctor').stdout)['coverage_warnings'], [])
        db.unlink()
        warnings = json.loads(self.run_cli('doctor').stdout)['coverage_warnings']
        rows = [w for w in warnings if w['source'] == 'companion_db']
        self.assertEqual(len(rows), 1, warnings)
        row = rows[0]
        self.assertEqual(row['integration'], 'sessions')
        self.assertIn('does not exist', row['problem'])
        self.assertEqual(row['fix'], 'project-observatory full configure sources companion_db PATH')
        self.assertEqual(row['disable'], 'project-observatory full configure integrations sessions false')
        # Both advertised commands are real: each one clears the warning.
        self.run_cli(*row['disable'].split()[2:])
        self.assertEqual(json.loads(self.run_cli('doctor').stdout)['coverage_warnings'], [])
        self.run_cli('configure', 'integrations', 'sessions', 'true')
        db.write_bytes(b'')
        self.run_cli(*row['fix'].replace('PATH', str(db)).split()[2:])
        self.assertEqual(json.loads(self.run_cli('doctor').stdout)['coverage_warnings'], [])

    def test_doctor_names_a_directory_where_the_companion_database_file_should_be(self):
        self.run_cli('init')
        self.run_cli('configure', 'integrations', 'sessions', 'true')
        self.run_cli('configure', 'sources', 'sessions', str(self.base))
        self.run_cli('configure', 'sources', 'companion_db', str(self.base))
        warnings = json.loads(self.run_cli('doctor').stdout)['coverage_warnings']
        self.assertTrue(any(w['source'] == 'companion_db' and 'not a file' in w['problem'] for w in warnings), warnings)

    def test_a_missing_feature_source_names_the_feature_switch(self):
        self.run_cli('init')
        self.run_cli('configure', 'features', 'companion_remediation', 'true')
        self.run_cli('configure', 'sources', 'companion_home', str(self.base / 'gone-home'))
        warnings = json.loads(self.run_cli('doctor').stdout)['coverage_warnings']
        row = next(w for w in warnings if w['source'] == 'companion_home')
        self.assertIn('does not exist', row['problem'])
        self.assertEqual(row['fix'], 'project-observatory full configure sources companion_home PATH')
        self.assertEqual(row['disable'], 'project-observatory full configure features companion_remediation false')

    def test_future_config_and_workspace_refused_without_mutation(self):
        self.run_cli('init')
        for relative, field, value in [('config/settings.json','schema_version',99),('workspace.json','minimum_writer','99.0.0')]:
            file=self.home/relative; old=file.read_bytes(); doc=json.loads(old);doc[field]=value
            file.write_text(json.dumps(doc));before=file.read_bytes()
            p=self.run_cli('doctor',ok=False);self.assertNotEqual(p.returncode,0)
            self.assertEqual(before,file.read_bytes());file.write_bytes(old)
    def test_future_registry_refused_before_emit(self):
        self.run_cli('init')
        file=self.home/'registry/projects.json';doc=json.loads(file.read_text());doc['schema_version']=99
        file.write_text(json.dumps(doc));before=file.read_bytes()
        p=self.run_cli('emit',ok=False)
        self.assertNotEqual(p.returncode,0);self.assertEqual(before,file.read_bytes())
    def test_complete_local_workflow_and_eleven_pages(self):
        self.run_cli('init')
        projects=self.base/'projects';project=projects/'example-project';project.mkdir(parents=True)
        (project/'package.json').write_text('{"name":"example-project","dependencies":{"example":"1"}}')
        self.run_cli('configure','sources','projects',str(projects))
        for step in ['scan','merge','emit','validate','scan-events','plugins','findings','dashboard','smoke-pages']:
            self.run_cli(step)
        pages=list((self.home/'docs/dashboard').glob('*.html'))
        self.assertEqual(len(pages),11)
        # The Machine page builds before any machine survey: an empty state, not a blank page.
        self.assertIn('No process survey yet.',(self.home/'docs/dashboard/machine.html').read_text())
        registry=json.loads((self.home/'registry/projects.json').read_text())
        self.assertEqual(len(registry['projects']),1)
        self.assertIn('example-project',(self.home/'docs/dashboard/projects.html').read_text())
        self.assertFalse((self.user/'.config/agentgateway').exists())
    def test_local_before_a_projects_source_says_what_to_do(self):
        # A new user's first `full local` before `configure sources projects`.
        self.run_cli('init')
        p=self.run_cli('local',ok=False)
        out=p.stdout+p.stderr
        self.assertNotEqual(p.returncode,0)
        self.assertNotIn('Traceback',out)
        self.assertIn('project-observatory full configure sources projects',out)
        self.assertNotIn('purity checks',out)
        self.assertNotIn('\033[',out)      # colour is for a terminal, not a pipe
    def test_an_unknown_switch_or_source_name_is_refused_with_the_known_ones(self):
        # A typo used to be answered "configured" and then did nothing at all.
        self.run_cli('init')
        file=self.home/'config/settings.json';before=file.read_bytes()
        for section,name,value,known in (('integrations','githb','true','github'),
                                         ('features','schedular','true','scheduler'),
                                         ('sources','projcts',str(self.base),'projects')):
            p=self.run_cli('configure',section,name,value,ok=False)
            self.assertEqual(p.returncode,2,p.stdout+p.stderr)
            self.assertIn(name,p.stderr);self.assertIn(known,p.stderr)
        self.assertEqual(before,file.read_bytes())
        self.run_cli('configure','integrations','github','true')
        # A plugin's `integration:KEY` is a switch too.
        self.run_cli('configure','integrations','ga4','true')
    def test_every_switch_and_source_the_engine_reads_is_declared(self):
        # The refusal above is only safe while the declared names cover every name
        # the code reads or tells a user to configure; this keeps the two together.
        import re
        sys.path.insert(0,str(ROOT))
        import configuration
        read={'integrations':set(),'features':set(),'sources':set()}
        for path in ROOT.rglob('*'):
            if path.suffix not in {'.py','.sh','.js','.json'} or 'tests' in path.relative_to(ROOT).parts or not path.is_file():
                continue
            text=path.read_text(errors='replace')
            for m in re.finditer(r'enabled\(\s*["\']([a-z_0-9]+)["\'](\s*,\s*(?:section=)?["\'](integrations|features)["\'])?',text):
                read[m.group(3) or 'integrations'].add(m.group(1))
            for m in re.finditer(r'configure (integrations|features|sources) ([a-z_0-9]+)\b',text):
                read[m.group(1)].add(m.group(2))
            for m in re.finditer(r'source_path\(\s*["\']([a-z_0-9]+)["\']',text):
                read['sources'].add(m.group(1))
        # The tick's step gates, read as literals (importing tick_lease has side effects).
        import ast
        tree=ast.parse((ROOT/'tools/tick_lease.py').read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.Assign) and isinstance(node.targets[0],ast.Name) \
                    and node.targets[0].id in ('FEATURE_STEPS','INTEGRATION_STEPS'):
                section='features' if node.targets[0].id=='FEATURE_STEPS' else 'integrations'
                read[section]|=set(ast.literal_eval(node.value).values())
        for section,names in read.items():
            self.assertTrue(names,section)
            self.assertEqual(names-configuration.known_names(section),set(),section)
    def test_local_verifies_the_page_it_builds(self):
        # `local` built the pages and never ran `smoke`, so every run left
        # `dashboard.unverified` on a new user's board — about a page just built.
        import shutil
        if not shutil.which('node'):
            self.skipTest('node is not installed: smoke cannot run')
        self.run_cli('init')
        projects=self.base/'projects';(projects/'alpha-web').mkdir(parents=True)
        (projects/'alpha-web'/'package.json').write_text('{"name":"alpha-web"}')
        self.run_cli('configure','sources','projects',str(projects))
        for _ in range(2):
            self.run_cli('local')
        types={f.get('type') for f in json.loads((self.home/'registry/findings.json').read_text())['findings']}
        self.assertNotIn('dashboard.unverified',types)
        self.assertNotIn('dashboard.blank',types)
    def test_independent_homes(self):
        self.run_cli('init');first=json.loads((self.home/'workspace.json').read_text())
        other=self.base/'other';self.env['OBSERVATORY_HOME']=str(other)
        self.run_cli('init');second=json.loads((other/'workspace.json').read_text())
        self.assertNotEqual(first['instance_id'],second['instance_id'])
        self.run_cli('configure','features','scheduler','false')
        self.assertEqual(json.loads((self.home/'config/settings.json').read_text())['features'],{})
    def test_help_has_no_state_side_effects(self):
        self.run_cli('--help')
        self.assertFalse(self.home.exists())

if __name__=='__main__':
    unittest.main()
