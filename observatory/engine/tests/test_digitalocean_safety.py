"""Failure boundaries with fake provider/vault seams; no inherited credentials."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
OLD = 'synthetic-old-' + 'x' * 20
NEW = 'synthetic-new-' + 'y' * 20


class RotationSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.env = patch.dict(os.environ, {'HOME': str(self.home),
            'OBSERVATORY_HOME': str(self.home / 'workspace'), 'PATH': '/usr/bin:/bin'}, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)
        sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
        spec = importlib.util.spec_from_file_location('synthetic_do_safety', ROOT / 'tools/digitalocean.py')
        self.door = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.door)
        self.door.POLL = 0
        self.calls = []; self.receipts = []
        # `db_names` as the provider answers it: `--db` is checked against it (round 4).
        self.cluster = {'id': 'c-one', 'name': 'alpha-db', 'engine': 'pg', 'db_names': ['defaultdb', 'app'],
                        'connection': {'host': 'db.example.invalid', 'port': 25060}}
        self.specs = {'literal': {'name': 'alpha', 'envs': [{'key': 'DB_PASSWORD', 'value': OLD}]}}
        self.reset_result = [{'name': 'doadmin', 'password': NEW}]
        self.reset_error = None
        self.updated = {}
        for name, value in [('plan', {'project': 'alpha-web', 'env': 'prod', 'name': 'DATABASE_URL'}),
                            ('context_line', 'synthetic context'), ('cluster', self.cluster),
                            ('deliver', 'synthetic delivery')]:
            p = patch.object(self.door, name, return_value=value); p.start(); self.addCleanup(p.stop)
        p = patch.object(self.door, 'doctl', side_effect=self.provider); p.start(); self.addCleanup(p.stop)
        p = patch.object(self.door.private_files.paths, 'STATE', self.home / 'state'); p.start(); self.addCleanup(p.stop)

    def provider(self, *args, **kwargs):
        self.calls.append(args)
        if args[:3] == ('databases', 'user', 'get'):
            return [{'name': 'doadmin', 'password': OLD}]
        if args[:3] == ('databases', 'user', 'reset'):
            if self.reset_error:
                raise self.reset_error
            return self.reset_result
        if args[:3] == ('apps', 'spec', 'get'):
            return copy.deepcopy(self.specs[args[3]])
        if args[:2] == ('apps', 'list-deployments'):
            return [{'id': 'new', 'phase': 'ACTIVE'}] if self.reset_called() else []
        if args[:2] == ('apps', 'update'):
            self.updated[args[2]] = json.loads(Path(args[-1]).read_text())
        return []

    def reset_called(self):
        return any(c[:3] == ('databases', 'user', 'reset') for c in self.calls)

    def run_rotation(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.door.main(['db', 'rotate', 'alpha-db', 'doadmin', '--db', 'app',
                '--to', 'vault:alpha-web/prod/DATABASE_URL', '--wait', '1', *extra])
        text = out.getvalue() + err.getvalue()
        self.assertNotIn(OLD, text); self.assertNotIn(NEW, text)
        return rc, text

    def test_ambiguous_reset_never_returns_preflight_or_retries(self):
        for error in (self.door.DoorError('synthetic lost response'),
                      subprocess.TimeoutExpired('synthetic', 1), OSError('synthetic')):
            with self.subTest(error=type(error).__name__):
                self.calls.clear(); self.reset_error = error
                rc, text = self.run_rotation('--app', 'binding')
                self.assertEqual(rc, 5); self.assertIn('unknown', text.lower())
                self.assertNotIn('nothing was reset', text)
                self.assertEqual(sum(c[:3] == ('databases', 'user', 'reset') for c in self.calls), 1)
                self.assertFalse(any(c[:2] == ('apps', 'create-deployment') for c in self.calls))
                receipts = list((self.home / 'state' / 'digitalocean-rotations').glob('*.json'))
                self.assertTrue(receipts)
                self.assertEqual(json.loads(receipts[-1].read_text())['reset'], 'unknown')

    def test_malformed_reset_response_is_unknown(self):
        for answer in ([], None, {'password': NEW}, [{'name': 'other', 'password': NEW}],
                       [{'name': 'doadmin', 'password': {'unexpected': 'shape'}}]):
            with self.subTest(shape=type(answer).__name__):
                self.calls.clear(); self.reset_result = answer
                rc, text = self.run_rotation()
                self.assertEqual(rc, 5); self.assertIn('unknown', text.lower())

    def test_provider_key_and_encrypted_literal_refused_without_reset(self):
        for key, value in [('OPENAI_API_KEY', 'provider-' + 'q' * 25),
                           ('DB_PASSWORD', 'EV[1:synthetic]'), ('UNLABELLED', 'unrelated')]:
            with self.subTest(key=key):
                self.calls.clear(); self.specs['literal']['envs'] = [{'key': key, 'value': value}]
                rc, text = self.run_rotation('--spec-env', 'literal:' + key)
                self.assertEqual(rc, 1); self.assertFalse(self.reset_called())
                self.assertIn('cannot prove', text)

    def test_type_is_proven_by_provider_value_not_env_name(self):
        self.specs['literal']['envs'] = [{'key': 'CUSTOM_CONNECTION', 'value': OLD}]
        rc, _ = self.run_rotation('--spec-env', 'literal:CUSTOM_CONNECTION')
        self.assertEqual(rc, 0)
        self.assertEqual(self.updated['literal']['envs'][0]['value'], NEW)

    def test_plain_url_requires_same_engine_endpoint_user_and_password(self):
        valid = 'postgresql://doadmin:' + OLD + '@db.example.invalid:25060/app?sslmode=require'
        for value in (valid.replace('postgresql:', 'https:'), valid.replace('doadmin:', 'other:'),
                      valid.replace('db.example.invalid', 'other.example.invalid'), valid.replace(OLD, 'unrelated')):
            self.calls.clear(); self.specs['literal']['envs'] = [{'key': 'ANY_NAME', 'value': value}]
            rc, _ = self.run_rotation('--spec-env', 'literal:ANY_NAME')
            self.assertEqual(rc, 1); self.assertFalse(self.reset_called())
        self.specs['literal']['envs'] = [{'key': 'ANY_NAME', 'value': valid}]
        rc, _ = self.run_rotation('--spec-env', 'literal:ANY_NAME')
        self.assertEqual(rc, 0)
        self.assertEqual(self.updated['literal']['envs'][0]['value'], valid.replace(OLD, NEW))

    def test_each_scope_is_proven_before_reset(self):
        for kind in ('services', 'workers', 'jobs'):
            self.calls.clear(); self.specs['literal'] = {'envs': [{'key': 'DB_PASSWORD', 'value': OLD}],
                kind: [{'envs': [{'key': 'DB_PASSWORD', 'value': 'unrelated'}]}]}
            rc, _ = self.run_rotation('--spec-env', 'literal:DB_PASSWORD')
            self.assertEqual(rc, 1); self.assertFalse(self.reset_called())

    def test_post_reset_tempfile_failure_continues_binding(self):
        original = self.door.tempfile.mkstemp
        def fail_spec(*args, **kwargs):
            if kwargs.get('suffix') == '.json':
                raise OSError('synthetic disk full')
            return original(*args, **kwargs)
        with patch.object(self.door.tempfile, 'mkstemp', side_effect=fail_spec):
            rc, text = self.run_rotation('--spec-env', 'literal:DB_PASSWORD', '--app', 'binding')
        self.assertEqual(rc, 4); self.assertIn('WAS reset', text)
        self.assertIn(('apps', 'create-deployment', 'binding'), self.calls)

    def test_post_reset_unlink_failure_continues_binding(self):
        with patch.object(self.door.os, 'remove', side_effect=OSError('synthetic unlink failure')):
            rc, text = self.run_rotation('--spec-env', 'literal:DB_PASSWORD', '--app', 'binding')
        self.assertEqual(rc, 4); self.assertIn('WAS reset', text)
        self.assertIn(('apps', 'create-deployment', 'binding'), self.calls)

    def test_post_reset_delivery_oserror_continues_binding(self):
        with patch.object(self.door, 'deliver', side_effect=OSError('synthetic vault failure')):
            rc, text = self.run_rotation('--app', 'binding')
        self.assertEqual(rc, 3); self.assertIn('WAS reset', text)
        self.assertIn(('apps', 'create-deployment', 'binding'), self.calls)

    def test_write_failure_continues_binding_and_hides_error_body(self):
        with patch.object(self.door.json, 'dump', side_effect=OSError(NEW)):
            rc, text = self.run_rotation('--spec-env', 'literal:DB_PASSWORD', '--app', 'binding')
        self.assertEqual(rc, 4)
        self.assertIn(('apps', 'create-deployment', 'binding'), self.calls)

    def test_receipt_is_required_before_reset(self):
        with patch.object(self.door, 'start_receipt', side_effect=OSError('synthetic')):
            rc, text = self.run_rotation()
        self.assertEqual(rc, 1); self.assertFalse(self.reset_called())

    def test_receipt_failure_after_reset_keeps_consumers_and_returns_six(self):
        original = self.door.private_files._atomic_write
        calls = []
        def fail_second(path, value, *args, **kwargs):
            calls.append(path)
            if len(calls) > 1:
                raise OSError(NEW)
            return original(path, value, *args, **kwargs)
        with patch.object(self.door.private_files, '_atomic_write', side_effect=fail_second):
            rc, text = self.run_rotation('--app', 'binding')
        self.assertEqual(rc, 6)
        self.assertIn(('apps', 'create-deployment', 'binding'), self.calls)
        self.assertEqual(json.loads(calls[0].read_text())['reset'], 'unknown')

    def test_one_binding_failure_does_not_prevent_next_binding(self):
        original = self.provider
        def failed(*args, **kwargs):
            if args[:3] == ('apps', 'create-deployment', 'broken'):
                self.calls.append(args)
                raise OSError(NEW)
            return original(*args, **kwargs)
        with patch.object(self.door, 'doctl', side_effect=failed):
            rc, _ = self.run_rotation('--app', 'broken', '--app', 'binding')
        self.assertEqual(rc, 4)
        self.assertIn(('apps', 'create-deployment', 'binding'), self.calls)

    def test_committed_app_error_remains_unknown_and_peer_continues(self):
        for verb, app in (("update", "literal"), ("create-deployment", "binding")):
            with self.subTest(verb=verb):
                self.calls.clear(); attempted = []; applied = []
                original = self.provider
                def committed_then_error(*args, **kwargs):
                    if args[:3] == ("apps", verb, app):
                        self.calls.append(args); applied.append(app); attempted.append(kwargs.get("tries"))
                        raise self.door.DoorError(NEW)
                    return original(*args, **kwargs)
                with patch.object(self.door, 'doctl', side_effect=committed_then_error):
                    rc, text = self.run_rotation('--spec-env', 'literal:DB_PASSWORD', '--app', 'binding', '--app', 'peer')
                self.assertEqual(rc, 4); self.assertEqual(applied, [app]); self.assertEqual(attempted, [1])
                self.assertIn(('apps', 'create-deployment', 'peer'), self.calls)
                self.assertIn('outcome unknown', text)
                self.assertNotIn('was not updated', text); self.assertNotIn('was not redeployed', text)
                receipts = sorted((self.home / 'state' / 'digitalocean-rotations').glob('*.json'), key=lambda p:p.stat().st_mtime_ns)
                self.assertEqual(json.loads(receipts[-1].read_text())['apps'][app], 'unknown')

    def test_mutation_wrapper_never_retries_nonzero_response(self):
        spec = importlib.util.spec_from_file_location('synthetic_do_once', ROOT / 'tools/digitalocean.py')
        door = importlib.util.module_from_spec(spec); spec.loader.exec_module(door)
        result = subprocess.CompletedProcess([], 1, '', NEW)
        for args in [('apps', 'update', 'literal'), ('apps', 'create-deployment', 'binding')]:
            with patch.object(door.subprocess, 'run', return_value=result) as run, patch.object(door.time, 'sleep'):
                with self.assertRaises(door.DoorError):
                    door.doctl(*args)
                self.assertEqual(run.call_count, 1)

    def test_malformed_deployment_metadata_is_unconfirmed_and_never_echoed(self):
        for wrong in (NEW + 'ACTIVE', 7, {'unexpected': NEW}):
            with self.subTest(shape=type(wrong).__name__):
                self.calls.clear(); original = self.provider
                def malformed(*args, **kwargs):
                    if args[:2] == ('apps', 'list-deployments') and self.reset_called():
                        self.calls.append(args)
                        return [{'id': 'new', 'phase': wrong}]
                    return original(*args, **kwargs)
                with patch.object(self.door, 'doctl', side_effect=malformed):
                    rc, text = self.run_rotation('--app', 'binding')
                self.assertEqual(rc, 2); self.assertIn('not confirmed ACTIVE', text)

    def test_duplicate_binding_flag_makes_one_mutation(self):
        rc, _ = self.run_rotation('--app', 'binding', '--app', 'binding')
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls.count(('apps', 'create-deployment', 'binding')), 1)
        self.calls.clear()
        rc, _ = self.run_rotation('--spec-env', 'literal:DB_PASSWORD', '--app', 'literal', '--app', 'literal')
        self.assertEqual(rc, 0)
        self.assertEqual(sum(c[:3] == ('apps', 'update', 'literal') for c in self.calls), 1)
        self.assertFalse(any(c[:3] == ('apps', 'create-deployment', 'literal') for c in self.calls))

    def test_provider_stderr_not_in_exception(self):
        # Restore the real wrapper for this boundary; subprocess remains mocked.
        spec = importlib.util.spec_from_file_location('synthetic_do_errors', ROOT / 'tools/digitalocean.py')
        door = importlib.util.module_from_spec(spec); spec.loader.exec_module(door)
        sentinel = 'synthetic-provider-' + 'body'
        result = subprocess.CompletedProcess([], 1, '', sentinel)
        with patch.object(door.subprocess, 'run', return_value=result):
            with self.assertRaises(door.DoorError) as got:
                door.doctl('databases', 'user', 'reset', tries=1)
        self.assertNotIn(sentinel, str(got.exception))


if __name__ == '__main__':
    unittest.main()
