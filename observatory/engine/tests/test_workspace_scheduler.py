#!/usr/bin/env python3
"""Fixture-only scheduler, launchd plans, workspace locks and private Git tests."""
from __future__ import annotations
import importlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

class WorkspaceScheduler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / 'instance with spaces'
        self.home.mkdir()
        (self.home / 'config').mkdir()
        (self.home / 'workspace.json').write_text(json.dumps({'format_version':1,'minimum_reader':'0.2.0','minimum_writer':'0.2.0'}))
        self.settings = {'schema_version':1,'sources':{},'integrations':{},'features':{}}
        self.save()
        self.env = patch.dict(os.environ, {'HOME':str(self.base / 'user'),'OBSERVATORY_HOME':str(self.home)}, clear=True)
        self.env.start()
        import paths
        importlib.reload(paths)
        from tools import install_launchd, tick_lease, serverd, commit_registry
        self.launch = importlib.reload(install_launchd)
        self.lease = importlib.reload(tick_lease)
        self.server = importlib.reload(serverd)
        self.commit = importlib.reload(commit_registry)
        self.paths = paths

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def save(self):
        (self.home / 'config/settings.json').write_text(json.dumps(self.settings))

    def enable(self, *features):
        self.settings['features'].update({key:True for key in features})
        self.save()

    def git(self, *args):
        return subprocess.run(['/usr/bin/git',*args],cwd=self.home,capture_output=True,text=True,check=True)

    def command(self, code):
        return [sys.executable,str(ROOT / 'tools/tick_lease.py'),'run','--',sys.executable,'-c',code]

    def test_plist_is_instance_scoped_and_contains_no_credentials(self):
        with patch.dict(os.environ, {'PROVIDER_TOKEN':'synthetic-secret-never-copy'}):
            tick = self.launch.build(1800)
            server = self.server.build_plist()
        for plan in (tick, server):
            self.assertEqual(set(plan['EnvironmentVariables']), {'HOME','PATH','OBSERVATORY_HOME','OBSERVATORY_PYTHON'})
            self.assertEqual(plan['EnvironmentVariables']['OBSERVATORY_HOME'],str(self.home))
            self.assertNotIn(b'synthetic-secret',plistlib.dumps(plan))
            self.assertTrue(plan['StandardOutPath'].startswith(str(self.home)))
            self.assertEqual(plan['Umask'],0o077)
        self.assertEqual(server['ProcessType'],'Standard','the server answers hosts and agents; Background starves it under load')
        self.assertNotIn('Nice',server)
        self.assertNotIn('LowPriorityIO',server)
        self.assertEqual(tick['ProcessType'],'Background','the scheduled scan stays background work')
        self.assertNotEqual(tick['Label'],'dev.sshlg.observatory.tick')
        original = tick['Label']
        with patch.object(self.paths,'HOME',self.base / 'second'):
            self.assertNotEqual(original,self.launch.instance_label('tick'))

    def test_launch_path_keeps_safe_user_directories(self):
        base = Path(tempfile.mkdtemp(prefix='observatory-path-')).resolve()
        user_bin, writable, missing = base / 'bin', base / 'shared', base / 'absent'
        user_bin.mkdir(mode=0o755); writable.mkdir(); writable.chmod(0o777)
        group_own = base / 'brew'; group_own.mkdir(); group_own.chmod(0o775)
        joined = os.pathsep.join([str(user_bin), 'relative/bin', str(writable), str(missing), str(user_bin), str(group_own)])
        entries = self.launch.launch_path(joined).split(os.pathsep)
        self.assertEqual(entries[0], str(user_bin), 'the installer\'s own tools come first')
        self.assertEqual(entries.count(str(user_bin)), 1)
        for bad in ('relative/bin', str(writable), str(missing)):
            self.assertNotIn(bad, entries)
        self.assertIn('/usr/bin', entries, 'system directories are always present')
        self.assertIn(str(group_own), entries, 'a group-writable directory this user owns (Homebrew) is kept')
        self.assertEqual(self.launch.environment()['PATH'], self.launch.launch_path())
        self.assertEqual(self.launch.environment()['OBSERVATORY_PYTHON'], sys.executable,
                         'tick.sh must run with the interpreter that installed the engine')
        tick = (ROOT / 'tools/tick.sh').read_text()
        self.assertIn('PY="${OBSERVATORY_PYTHON:-', tick)

    def test_launch_build_does_not_create_logs(self):
        self.launch.build(1800)
        self.assertFalse(self.launch.LOG_DIR.exists())
        with self.assertRaises(ValueError):
            self.launch.build(0)

    def test_background_install_is_opt_in(self):
        with patch.object(self.server.subprocess,'run') as launchctl:
            self.assertEqual(self.server.install(),1)
            launchctl.assert_not_called()
        self.assertFalse(self.server.PLIST.exists())

    def test_private_log_permissions_and_symlink_refusal(self):
        self.launch.prepare_logs(('test.log',))
        log = self.launch.LOG_DIR / 'test.log'
        self.assertEqual(log.stat().st_mode & 0o777,0o600)
        self.assertEqual(log.parent.stat().st_mode & 0o777,0o700)
        other = self.base / 'other'
        other.write_text('unchanged')
        (log.parent / 'bad.log').symlink_to(other)
        with self.assertRaises(OSError):
            self.launch.prepare_logs(('bad.log',))
        self.assertEqual(other.read_text(),'unchanged')

    def test_disabled_tick_does_not_execute_child(self):
        marker = self.base / 'executed'
        result = subprocess.run(self.command(f'from pathlib import Path; Path({str(marker)!r}).touch()'),capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse((self.home / 'store').exists())

    def test_workspace_lock_blocks_parallel_tick(self):
        self.enable('scheduler')
        ready = self.base / 'ready'
        marker = self.base / 'second'
        first = subprocess.Popen(self.command(f'from pathlib import Path; import time; Path({str(ready)!r}).touch(); time.sleep(20)'),stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
        try:
            deadline = time.monotonic()+5
            while not ready.exists() and time.monotonic()<deadline:
                time.sleep(.03)
            self.assertTrue(ready.exists())
            second = subprocess.run(self.command(f'from pathlib import Path; Path({str(marker)!r}).touch()'),capture_output=True,text=True)
            self.assertEqual(second.returncode,0,second.stderr)
            self.assertIn('another run',second.stdout)
            self.assertFalse(marker.exists())
        finally:
            first.terminate()
            first.communicate(timeout=15)
        final = subprocess.run(self.command(f'from pathlib import Path; Path({str(marker)!r}).touch()'),capture_output=True,text=True)
        self.assertEqual(final.returncode,0,final.stderr)
        self.assertTrue(marker.exists())
        self.assertEqual((self.home / 'store/tick.lock').stat().st_mode & 0o777,0o600)

    def test_feature_gates_default_off(self):
        for step in self.lease.FEATURE_STEPS:
            self.assertFalse(self.lease.step_allowed(step),step)
        self.assertTrue(self.lease.step_allowed('rollup'))
        self.enable('agent','companion_remediation')
        self.assertTrue(self.lease.step_allowed('agent'))
        self.assertTrue(self.lease.step_allowed('scrub-companion'))

    def test_network_collectors_are_individually_opt_in(self):
        for step in self.lease.INTEGRATION_STEPS:
            self.assertFalse(self.lease.step_allowed(step), step)
        self.settings['integrations']['git_remotes'] = True
        self.save()
        self.assertTrue(self.lease.step_allowed('remotes'))
        self.assertFalse(self.lease.step_allowed('scan-gh'))

    def test_log_directory_refuses_symlinked_ancestor(self):
        external = self.base / 'external-logs'
        external.mkdir()
        linked = self.home / 'linked-store'
        linked.symlink_to(external, target_is_directory=True)
        with self.assertRaises(Exception):
            self.launch.prepare_logs(('gate.log',), linked / 'logs')
        self.assertEqual(list(external.iterdir()), [])

    def test_gate_logs_and_exit_code_are_workspace_scoped(self):
        source = self.base / 'synthetic source'
        (source / 'tools').mkdir(parents=True)
        import shutil
        shutil.copy2(ROOT / 'tools/gate.sh', source / 'tools/gate.sh')
        (source / 'tools/install_launchd.py').write_text(
            "import os\nfrom pathlib import Path\nLOG_DIR=Path(os.environ['OBSERVATORY_HOME'])/'store/logs'\n"
            "def prepare_logs(names):\n LOG_DIR.mkdir(parents=True,exist_ok=True)\n")
        (source / 'observatory.py').write_text(
            "import sys\nprint('fixture argv:', sys.argv[1:])\nsys.exit(7)\n")
        with patch.dict(os.environ, {'PATH': str(Path(sys.executable).parent) + ':/usr/bin:/bin'}):
            result = subprocess.run(['/bin/bash', str(source / 'tools/gate.sh'), 'check', '--suite', 'ledger'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 7, result.stderr)
        text = (self.home / 'store/logs/gate.log').read_text()
        self.assertIn("['check', '--suite', 'ledger']", text)
        self.assertFalse((source / 'store').exists())

    def test_source_git_never_accepted(self):
        self.assertFalse(self.commit.private_workspace(ROOT)[0])
        self.assertFalse(self.commit.private_workspace(ROOT.parent)[0])
        self.assertFalse(self.commit.private_workspace(ROOT / 'nested')[0])

    def test_private_git_commits_only_registry(self):
        self.enable('registry_history')
        self.git('init','-q')
        self.git('config','user.name','Fixture')
        self.git('config','user.email','fixture@example.invalid')
        self.git('remote','add','origin','https://example.invalid/private-never-contacted.git')
        (self.home / 'registry').mkdir()
        (self.home / 'registry/projects.json').write_text('{"projects":[]}')
        (self.home / 'private-state.txt').write_text('not tracked')
        result = subprocess.run([sys.executable,str(ROOT/'tools/commit_registry.py')],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.git('ls-files').stdout.strip(),'registry/projects.json')
        self.assertEqual(self.git('rev-list','--count','HEAD').stdout.strip(),'1')
        self.assertTrue((self.home / 'private-state.txt').exists())

    def test_registry_history_does_not_initialize_git(self):
        self.enable('registry_history')
        result = subprocess.run([sys.executable,str(ROOT/'tools/commit_registry.py')],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse((self.home / '.git').exists())

    def test_server_uses_workspace_secret_store(self):
        projects = self.home / 'secrets/projects'
        projects.mkdir(parents=True)
        (projects / 'leaks.jsonl').write_text(json.dumps({'event':'leaked','id':'demo','secret':'DEMO_TOKEN','where':'synthetic fixture'})+'\n')
        self.assertEqual(self.server.refresh_leaks()['open'],1)

    def test_server_refuses_rebinding_and_cross_origin(self):
        valid = self.server.local_request
        self.assertTrue(valid('127.0.0.1:47311',None,None,47311))
        self.assertTrue(valid('localhost:47311','http://localhost:47311','same-origin',47311))
        for host, origin, fetch in (
                ('attacker.invalid:47311',None,None),
                ('127.0.0.1:47311','https://attacker.invalid',None),
                ('127.0.0.1:47311',None,'cross-site'),
                ('127.0.0.1:47311','null',None),
                ('127.0.0.1:47311','http://localhost:47312',None),
                ('127.0.0.1:47311','http://user@localhost:47311',None)):
            self.assertFalse(valid(host,origin,fetch,47311))

    def test_tick_shell_is_valid_and_runtime_paths_are_quoted(self):
        subprocess.run(['/bin/bash','-n',str(ROOT/'tools/tick.sh')],check=True)
        active = '\n'.join(line for line in (ROOT/'tools/tick.sh').read_text().splitlines() if not line.lstrip().startswith('#'))
        self.assertNotIn('store/raw/',active)
        self.assertNotIn('docs/projects-dashboard.html',active)
        self.assertIn('"$SCRATCH/local.json"',active)
        self.assertIn('"$DASHBOARD"',active)

if __name__=='__main__':
    unittest.main()
