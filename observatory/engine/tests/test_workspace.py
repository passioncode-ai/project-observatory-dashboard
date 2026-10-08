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
    def test_doctor_lists_every_known_name_with_its_state(self):
        # A fresh doctor said `integrations: {}` and `features: {}` while the
        # onboarding says "list current settings with doctor": every name the
        # `configure` command accepts is listed, on or off, and every source
        # says whether it is configured.
        sys.path.insert(0, str(ROOT))
        import configuration
        self.run_cli('init')
        self.run_cli('configure', 'integrations', 'github', 'true')
        doc = json.loads(self.run_cli('doctor').stdout)
        self.assertEqual(set(doc['integrations']), set(configuration.known_names('integrations')))
        self.assertEqual(set(doc['features']), set(configuration.known_names('features')))
        self.assertEqual(set(doc['sources']), set(configuration.known_names('sources')))
        self.assertIs(doc['integrations']['github'], True)
        self.assertIs(doc['integrations']['heroku'], False)
        self.assertTrue(all(v is False for v in doc['features'].values()))
        self.assertEqual(doc['sources']['cloudflare_snapshot'], {'configured': False})
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
        # The sessions integration reads the transcripts folder; pointed at a folder
        # that is gone, the collector reads nothing, so doctor must not stay silent.
        self.run_cli('init')
        self.configure_projects()
        transcripts = self.base / 'transcripts'
        transcripts.mkdir()
        self.run_cli('configure', 'integrations', 'sessions', 'true')
        self.run_cli('configure', 'sources', 'sessions', str(transcripts))
        self.assertEqual(json.loads(self.run_cli('doctor').stdout)['coverage_warnings'], [])
        transcripts.rmdir()
        warnings = json.loads(self.run_cli('doctor').stdout)['coverage_warnings']
        rows = [w for w in warnings if w['source'] == 'sessions']
        self.assertEqual(len(rows), 1, warnings)
        row = rows[0]
        self.assertEqual(row['integration'], 'sessions')
        self.assertIn('does not exist', row['problem'])
        self.assertEqual(row['fix'], 'project-observatory full configure sources sessions PATH')
        self.assertEqual(row['disable'], 'project-observatory full configure integrations sessions false')
        # Both advertised commands are real: each one clears the warning.
        self.run_cli(*row['disable'].split()[2:])
        self.assertEqual(json.loads(self.run_cli('doctor').stdout)['coverage_warnings'], [])
        self.run_cli('configure', 'integrations', 'sessions', 'true')
        transcripts.mkdir()
        self.run_cli(*row['fix'].replace('PATH', str(transcripts)).split()[2:])
        self.assertEqual(json.loads(self.run_cli('doctor').stdout)['coverage_warnings'], [])

    def test_a_retired_companion_database_is_not_a_coverage_gap(self):
        # The sessions collector reads the Stop hook's own records first; the claude-mem
        # companion is optional (retired on the maintainer's machine 2026-10-01). Its
        # database being absent is "not installed", and doctor must not send a person
        # after a file that will never exist again.
        self.run_cli('init')
        self.configure_projects()
        transcripts = self.base / 'transcripts'
        transcripts.mkdir()
        self.run_cli('configure', 'integrations', 'sessions', 'true')
        self.run_cli('configure', 'sources', 'sessions', str(transcripts))
        self.run_cli('configure', 'sources', 'companion_db', str(self.base / 'gone.db'))
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
    def test_complete_local_workflow_and_twelve_pages(self):
        self.run_cli('init')
        projects=self.base/'projects';project=projects/'example-project';project.mkdir(parents=True)
        (project/'package.json').write_text('{"name":"example-project","dependencies":{"example":"1"}}')
        self.run_cli('configure','sources','projects',str(projects))
        for step in ['scan','merge','emit','validate','scan-events','plugins','findings','dashboard','smoke-pages']:
            self.run_cli(step)
        pages=list((self.home/'docs/dashboard').glob('*.html'))
        self.assertEqual(len(pages),12)
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
        # ONE run: the first board a new user opens. Two runs here hid that the
        # first one still said "not verified" — `findings` runs before `smoke`.
        self.run_cli('local')
        types={f.get('type') for f in json.loads((self.home/'registry/findings.json').read_text())['findings']}
        self.assertNotIn('dashboard.unverified',types)
        self.assertNotIn('dashboard.blank',types)
        # and the page itself says so: the board the user reads was built after the verdict
        page=(self.home/'docs/dashboard/findings.html').read_text()
        self.assertNotIn('dashboard.unverified',page)
        # a second run keeps it clean (the receipt describes the page on disk)
        self.run_cli('local')
        types={f.get('type') for f in json.loads((self.home/'registry/findings.json').read_text())['findings']}
        self.assertNotIn('dashboard.unverified',types)
    def test_the_first_local_reads_as_a_person_reads_it(self):
        # A new user's first `local` printed Python dicts from the merge
        # (`anchor: {'local-folder': 2}`), listed integrations they never
        # switched on under "degraded", printed the registrar counters of an
        # estate with no domains, and showed the findings counts from BEFORE
        # `settle` rebuilt the board, so the last numbers on screen were not
        # the board's.
        import shutil
        self.run_cli('init')
        projects=self.base/'projects';(projects/'alpha-web').mkdir(parents=True)
        (projects/'alpha-web'/'package.json').write_text('{"name":"alpha-web"}')
        self.run_cli('configure','sources','projects',str(projects))
        out=self.run_cli('local').stdout
        self.assertNotRegex(out, r"\{'[a-z-]+': \d+")
        self.assertNotIn('duplicate repo names: []', out)
        self.assertNotIn('namecheap_', out)
        self.assertNotIn('cloudflare_invalid_nameservers', out)
        merge = out.split('── merge', 1)[1].split('── emit', 1)[0]
        self.assertNotIn('degraded:', merge, merge)
        self.assertIn('switched off', merge)
        self.assertIn('github', merge)
        board = json.loads((self.home/'registry/findings.json').read_text())['findings']
        counts = {sev: sum(1 for f in board if f.get('severity') == sev) for sev in ('critical', 'warning', 'info')}
        last = [l for l in out.splitlines() if 'critical ' in l and 'warning ' in l][-1]
        self.assertIn(f"critical {counts['critical']}", last)
        self.assertIn(f"info {counts['info']}", last)
        if shutil.which('node'):
            # settle rebuilt the board, so it is the one that reprints the counts
            self.assertTrue(last.startswith('settle:'), last)
    def test_scan_mcp_summary_has_no_dangling_dash(self):
        self.run_cli('init')
        (self.user/'.claude.json').write_text('{}')
        self.run_cli('configure','sources','mcp_config_root',str(self.user))
        self.run_cli('configure','integrations','mcp','true')
        out=self.run_cli('scan-mcp').stdout
        line=next(l for l in out.splitlines() if l.startswith('mcp:'))
        self.assertFalse(line.rstrip().endswith('—'), line)
    def test_configure_sets_the_model_chain_and_the_budget(self):
        # A fresh models.json has chain [] and every ceiling 0.0, and nothing
        # offered a way to set them but editing the file by hand.
        self.run_cli('init')
        models=self.home/'config/models.json';before=json.loads(models.read_text())
        self.run_cli('configure','model','chain','example/model-a, example/model-b:free')
        for name,value in (('daily_ceiling','0.5'),('monthly_ceiling','10'),('velocity_ceiling','0.25')):
            self.run_cli('configure','budget',name,value)
        doc=json.loads(models.read_text())
        self.assertEqual([e['id'] for e in doc['chain']],['example/model-a','example/model-b:free'])
        self.assertEqual((doc['wallet']['daily_ceiling'],doc['wallet']['monthly_ceiling'],doc['wallet']['velocity_ceiling']),(0.5,10.0,0.25))
        self.assertEqual(doc['embedding'],before['embedding'])                # every other field kept
        self.assertEqual(doc['wallet']['denomination'],before['wallet']['denomination'])
        for args in (('budget','yearly_ceiling','1'),('budget','daily_ceiling','-1'),('budget','daily_ceiling','nan'),
                     ('budget','daily_ceiling','lots'),('model','order','x'),('model','chain',' , '),
                     ('model','chain','has space/model')):
            p=self.run_cli('configure',*args,ok=False)
            self.assertEqual(p.returncode,2,args)
        self.assertEqual(json.loads(models.read_text()),doc)
        # doctor names the agent's readiness once the agent is switched on.
        self.assertNotIn('agent',json.loads(self.run_cli('doctor').stdout))
        self.run_cli('configure','features','agent','true')
        # NO KEY IS NOT READY. A chain and three ceilings without a key read
        # `ready` with `next: []`, while the first ask failed on the key.
        agent=json.loads(self.run_cli('doctor').stdout)['agent']
        self.assertEqual((agent['model_status'],agent['key_status'],agent['key_source']),('no-key','absent',None))
        self.assertIn('install_key.py" --for observatory',' '.join(agent['next']))
        # A key inherited from the shell counts, and doctor says that is where it came from.
        self.env['OPENROUTER_API_KEY']='sk-or-v1-'+'FAKE'*16
        agent=json.loads(self.run_cli('doctor').stdout)['agent']
        self.assertEqual((agent['model_status'],agent['key_status']),('ready','present'))
        self.assertIn('OPENROUTER_API_KEY',agent['key_source'])
        self.assertNotIn('FAKE',json.dumps(agent))
        self.run_cli('configure','budget','daily_ceiling','0')
        agent=json.loads(self.run_cli('doctor').stdout)['agent']
        self.assertEqual(agent['model_status'],'no-budget')
        self.assertIn('project-observatory full configure budget daily_ceiling',' '.join(agent['next']))
    def test_local_refreshes_the_env_inventory(self):
        # `local` never ran `env`, so the ENV page and observatory_credentials
        # kept the inventory of whenever `full env` was last typed by hand.
        self.run_cli('init')
        projects=self.base/'projects';(projects/'alpha-web').mkdir(parents=True)
        (projects/'alpha-web'/'package.json').write_text('{"name":"alpha-web"}')
        (projects/'alpha-web'/'.env').write_text('EXAMPLE_PORT=3000\n')
        self.run_cli('configure','sources','projects',str(projects))
        self.run_cli('local')
        doc=json.loads((self.home/'registry/env-inventory.json').read_text())
        self.assertTrue(doc.get('scanned_on'),doc)
        self.assertIn('EXAMPLE_PORT',json.dumps(doc['files']))
        self.assertNotIn('3000',json.dumps(doc['files']))
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
