"""Space pressure is not permission to remove active work; synthetic only."""
from __future__ import annotations
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.append(str(ROOT / 'tools'))


class Space(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        self.env = patch.dict(os.environ, {'HOME': str(self.home), 'OBSERVATORY_HOME': str(self.home / 'workspace')}, clear=True)
        self.env.start()
        import workspace
        workspace.initialize(self.home / 'workspace')
        import paths
        importlib.reload(paths)
        from tools import space_manager
        self.m = importlib.reload(space_manager)
        self.manager = self.m.Manager()
        self.tools = patch('shutil.which', return_value='/synthetic/bin/tool')
        self.tools.start()
        self.desktop = patch.object(self.m.Manager, 'desktop_notice', return_value='unavailable')
        self.desktop_mock = self.desktop.start()

    def tearDown(self):
        self.desktop.stop()
        self.tools.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_threshold_is_strict_and_disabled_means_no_action(self):
        with patch.object(self.manager, 'free_bytes', return_value=10_000_000_000), patch.object(self.manager, 'execute') as run:
            self.manager.check_pressure()
            run.assert_not_called()
        with patch.object(self.manager, 'free_bytes', return_value=1), patch.object(self.manager, 'execute') as run:
            self.manager.check_pressure()
            run.assert_not_called()
            self.assertEqual(self.manager.status()['pressure'], 'critical')

    def test_no_arbitrary_adapter_or_browser_path(self):
        for ids in (['../../elsewhere'], ['pip'], ['worktrees'], ['node_modules']):
            with self.assertRaises(ValueError):
                self.manager.plan(ids)

    def test_linked_cache_is_never_eligible(self):
        outside = self.home / 'important'; outside.mkdir()
        (self.home / '.npm').symlink_to(outside, target_is_directory=True)
        row = self.manager.cache_row('npm', measure=False)
        self.assertFalse(row['eligible'])
        self.assertEqual(row['reason'], 'linked-path')

    def test_unknown_or_busy_consumers_block_cleanup(self):
        (self.home / '.cache/uv').mkdir(parents=True)
        with patch.object(self.manager, 'consumers', return_value=None):
            self.assertEqual(self.manager.cache_row('uv', measure=False)['reason'], 'activity-unknown')
        with patch.object(self.manager, 'consumers', return_value={'python'}):
            self.assertEqual(self.manager.cache_row('uv', measure=False)['reason'], 'in-use')

    def test_invalid_plan_does_not_invoke_commands(self):
        with patch.object(self.manager, 'run_command') as run:
            with self.assertRaises(ValueError): self.manager.execute('not-a-plan')
            run.assert_not_called()

    def test_fresh_plan_rechecks_busy_state(self):
        (self.home / '.cache/uv').mkdir(parents=True)
        with patch.object(self.manager, 'consumers', return_value=set()), patch.object(self.manager, 'run_command', return_value=(0, 'ignoring in-use checks')):
            plan = self.manager.plan(['uv'])
        with patch.object(self.manager, 'consumers', return_value={'python'}), patch.object(self.manager, 'run_command') as run:
            result = self.manager.execute(plan['id'])
            run.assert_not_called()
            self.assertEqual(result['actions'][0]['result'], 'skipped')
            self.assertEqual(result['actions'][0]['reason'], 'in-use')
        with self.assertRaises(ValueError): self.manager.execute(plan['id'])

    def test_stale_plan_refused(self):
        plan = self.manager.plan([])
        with patch('time.time', return_value=plan['created_epoch'] + 601):
            with self.assertRaises(ValueError): self.manager.execute(plan['id'])

    def test_native_command_allowlist_does_not_include_legacy_cleanup(self):
        commands = [self.manager.command(k) for k in self.m.CLEANABLE]
        flat = repr(commands)
        self.assertNotIn('system', flat)
        self.assertNotIn('volume', flat)
        self.assertNotIn('rmtree', flat)
        self.assertNotIn('cleanup.py', flat)
        self.assertNotIn('--force', repr(self.manager.command('uv')))
        self.assertIn('168h', flat)

    def test_negative_space_delta_is_not_claimed_as_reclaimed(self):
        with patch.object(self.manager, 'free_bytes', side_effect=[20_000, 10_000]):
            plan = self.manager.plan([])
            result = self.manager.execute(plan['id'])
        self.assertEqual(result['free_delta_bytes'], -10_000)

    def test_machine_lock_refuses_second_workspace(self):
        with self.manager.cleanup_lock():
            with self.assertRaises(BlockingIOError):
                with self.m.Manager().cleanup_lock(): pass

    def test_auto_cooldown_and_stop_target(self):
        self.manager.set_auto(True)
        with patch.object(self.manager, 'free_bytes', return_value=1), patch.object(self.manager, 'execute', return_value={}) as run:
            self.manager.check_pressure()
            self.manager.check_pressure()
            self.assertEqual(run.call_count, 1)

    def test_corrupt_state_and_policy_fail_closed(self):
        self.manager.state_file.write_text('{')
        with self.assertRaises(ValueError): self.manager.plan([])

    def test_wrong_state_shapes_fail_closed(self):
        for state in ({'runs': {}}, {'running': True}, {'plan': []}, {'notifications': [1]}):
            self.manager.state_file.write_text(json.dumps(state))
            with self.assertRaises(ValueError): self.manager.plan([])

    def test_cancelled_plan_cannot_be_replayed(self):
        plan = self.manager.plan([])
        self.manager.cancel(plan['id'])
        with self.assertRaises(ValueError): self.manager.execute(plan['id'])

    def test_interrupted_run_recovers_without_claiming_reclaimed_space(self):
        self.manager.write_state({'running': {'id': 'old', 'actions': [], 'status': 'running'}})
        self.manager.plan([])
        state = self.manager.state()
        self.assertEqual(state['runs'][-1]['status'], 'interrupted')
        self.assertNotIn('free_delta_bytes', state['runs'][-1])

    def test_auto_stops_at_target_and_honors_disable(self):
        row = {'id': 'uv', 'path': '/synthetic/cache', 'eligible': True}
        for enabled, free in ((True, self.m.TARGET), (False, 1)):
            self.manager.set_auto(enabled)
            with patch.object(self.manager, 'cache_row', return_value=row):
                plan = self.manager.plan(['uv'])
                with patch.object(self.manager, 'free_bytes', return_value=free), patch.object(self.manager, 'run_command') as run:
                    result = self.manager.execute(plan['id'], automatic=True)
                    run.assert_not_called()
                    self.assertEqual(result['status'], 'skipped')

    def test_command_failure_is_journalled_and_low_space_stays_visible(self):
        row = {'id': 'uv', 'path': '/synthetic/cache', 'eligible': True}
        with patch.object(self.manager, 'cache_row', return_value=row):
            plan = self.manager.plan(['uv'])
            with patch.object(self.manager, 'free_bytes', return_value=1), patch.object(self.manager, 'run_command', side_effect=TimeoutError):
                result = self.manager.execute(plan['id'])
        self.assertEqual(result['status'], 'partial')
        self.assertIsNone(self.manager.state()['running'])
        self.assertEqual(self.manager.state()['notifications'][-1]['kind'], 'still-critical')

    def test_notifications_are_deduplicated_and_recovery_is_recorded(self):
        with patch.object(self.manager, 'free_bytes', return_value=1):
            self.manager.check_pressure(); self.manager.check_pressure()
        self.assertEqual(self.desktop_mock.call_count, 1)
        with patch.object(self.manager, 'free_bytes', return_value=self.m.TARGET):
            self.manager.check_pressure()
        self.assertEqual([n['kind'] for n in self.manager.state()['notifications']], ['critical', 'recovered'])

    def test_remote_docker_and_unverified_uv_lock_are_protected(self):
        with patch.dict(os.environ, {'DOCKER_HOST': 'tcp://example.invalid:2375'}), patch.object(self.manager, 'run_command') as run:
            self.assertEqual(self.manager.cache_row('docker-build', measure=False)['reason'], 'local-daemon-unverified')
            run.assert_not_called()
        (self.home / '.cache/uv').mkdir(parents=True)
        with patch.object(self.manager, 'consumers', return_value=set()), patch.object(self.manager, 'run_command', return_value=(0, 'old tool')):
            self.assertEqual(self.manager.cache_row('uv', measure=False)['reason'], 'native-lock-unverified')

    def test_uv_path_traversal_and_npm_deletion_are_refused(self):
        with patch.dict(os.environ, {'UV_CACHE_DIR': str(self.home / '../another-user')}):
            self.assertEqual(self.manager.cache_row('uv', measure=False)['reason'], 'outside-home')
        with self.assertRaises(ValueError): self.manager.plan(['npm'])
        with self.assertRaises(ValueError): self.manager.command('npm')

    def test_cli_exposes_status_and_explicit_policy_without_cleanup(self):
        import subprocess
        env = {**os.environ, 'OBSERVATORY_PROFILE': 'public'}
        for action, enabled in (('enable', True), ('disable', False)):
            p = subprocess.run([sys.executable, str(ROOT / 'observatory.py'), 'space', action],
                               env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(json.loads(p.stdout)['auto_enabled'], enabled)
        p = subprocess.run([sys.executable, str(ROOT / 'observatory.py'), 'space', 'status'],
                           env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)['runs'], [])

    def test_one_unreadable_cache_is_protected_without_losing_the_register(self):
        with patch.object(self.manager, '_cache_row', side_effect=PermissionError):
            row = self.manager.cache_row('uv')
        self.assertFalse(row['eligible'])
        self.assertIsNone(row['size_bytes'])
        self.assertEqual(row['reason'], 'cache-unavailable')


class HttpBoundary(unittest.TestCase):
    def request(self, path='/api/space', headers=None, body=None):
        import io
        from types import SimpleNamespace
        import serverd
        body = json.dumps(body or {'action': 'plan'}).encode()
        pairs = [('Host', '127.0.0.1:43210'), ('Origin', 'http://127.0.0.1:43210'),
                 ('Content-Type', 'application/json'), ('X-Observatory-Action', 'space'),
                 ('Content-Length', str(len(body)))] if headers is None else headers
        raw = ('POST ' + path + ' HTTP/1.0\r\n' + ''.join(k + ': ' + v + '\r\n' for k,v in pairs) + '\r\n').encode() + body
        class Peer:
            output = b''
            def makefile(self, *args): return io.BytesIO(raw)
            def sendall(self, data): self.output += bytes(data)
        peer = Peer()
        serverd.Handler(peer, ('127.0.0.1', 1), SimpleNamespace(server_address=('127.0.0.1', 43210)))
        return int(peer.output.split(b' ')[1])

    def test_cross_origin_missing_origin_and_wrong_content_type_never_act(self):
        from tools import space_manager
        from unittest.mock import MagicMock
        for pairs in (
            [('Host','127.0.0.1:43210'),('Origin','https://example.invalid')],
            [('Host','127.0.0.1:43210')],
            [('Host','127.0.0.1:43210'),('Origin','http://127.0.0.1:43210'),('Content-Type','text/plain')],
            [('Host','example.invalid'),('Origin','http://127.0.0.1:43210')],
        ):
            with patch.object(space_manager, 'Manager') as manager:
                self.assertEqual(self.request(headers=pairs), 403)
                manager.assert_not_called()

    def test_only_bounded_actions_and_arguments(self):
        from tools import space_manager
        with patch.object(space_manager, 'Manager') as manager:
            manager.return_value.plan.return_value = {'id': 'preview'}
            self.assertEqual(self.request(), 200)
            manager.return_value.plan.assert_called_once_with(None)
        for body in ({'action':'shell','command':'rm -rf /'}, {'action':'plan','path':'/'},
                     {'action':'policy','enabled':'yes'}):
            self.assertEqual(self.request(body=body), 400)


if __name__ == '__main__':
    unittest.main()
