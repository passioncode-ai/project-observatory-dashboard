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
