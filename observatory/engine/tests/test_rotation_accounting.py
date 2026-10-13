"""Rotation phases use fake providers and protected temporary ledgers only."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
VALUE = "synthetic-secret-value"
SUCCESSOR = hashlib.sha256(VALUE.encode()).hexdigest()

class RotationAccounting(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        spec = importlib.util.spec_from_file_location('rotation_under_test', ROOT / 'tools/openrouter.py')
        self.m = m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        m.LEDGER = Path(self.temp.name).resolve() / 'ledger.json'
        m.save_ledger({'issued': {'sample-prod-key': {'hash': 'old', 'account': 'sample',
            'destination': 'vault:sample/prod/KEY', 'limit_usd': 10, 'limit_reset': 'monthly'}}})
        self.provider = {'old': {'hash': 'old', 'limit': 5, 'limit_reset': 'daily'}}
        self.fault = None; self.mints = 0
        m.read_admin = lambda *args: ('sample', 'synthetic-admin')
        self.original_find_key = m.find_key
        m.find_key = lambda *args: dict(self.provider[self.row()['hash']])
        m._request = self.request; m.deliver = self.deliver
        m._journal = lambda *args, **kwargs: None

    def request(self, path, admin, payload=None, method=None):
        if path == '/keys' and payload:
            self.mints += 1
            self.provider[SUCCESSOR] = {'hash': SUCCESSOR, 'limit': payload['limit'], 'limit_reset': payload['limit_reset']}
            if self.fault == 'mint-502': raise RuntimeError('synthetic-secret-body')
            return {'data': dict(self.provider[SUCCESSOR]), 'key': VALUE}
        key = path.rsplit('/', 1)[-1]
        if method == 'DELETE':
            if self.fault == 'delete': raise RuntimeError('synthetic-secret-body')
            self.provider.pop(key, None); return {}
        if method == 'PATCH': return {'data': dict(self.provider[key])}
        if key not in self.provider: raise RuntimeError('openrouter answered HTTP 404')
        return {'data': dict(self.provider[key])}

    def deliver(self, *args, **kwargs):
        if self.fault == 'kill': raise KeyboardInterrupt()
        if self.fault == 'timeout': raise subprocess.TimeoutExpired(['synthetic-secret-body'], 1)
        return 'vault:sample/prod/KEY'

    def row(self):
        return self.m.ledger()['issued']['sample-prod-key']

    def test_foreign_predecessor_response_never_mints_or_mutates(self):
        self.provider['foreign'] = {'hash': 'foreign', 'limit': 5, 'limit_reset': 'daily'}
        self.m.find_key = lambda *args: dict(self.provider['foreign'])
        before = self.row()
        with self.assertRaisesRegex(RuntimeError, 'Predecessor identity'):
            self.m.rotate_one('sample-prod-key')
        self.assertEqual(self.mints, 0)
        self.assertEqual(self.row(), before)
        self.assertEqual(set(self.provider), {'old', 'foreign'})

    def test_successor_value_mismatch_never_delivers_or_revokes(self):
        original = self.request
        def mismatch(*args, **kwargs):
            result = original(*args, **kwargs)
            if isinstance(result, dict) and 'key' in result:
                result['key'] = 'different-synthetic-value'
            return result
        self.m._request = mismatch
        self.m.deliver = lambda *a, **k: self.fail('unbound value cannot be delivered')
        with self.assertRaisesRegex(RuntimeError, 'Successor value identity') as raised:
            self.m.rotate_one('sample-prod-key')
        self.assertNotIn('different-synthetic-value', str(raised.exception))
        self.assertEqual(set(self.provider), {'old', SUCCESSOR})
        self.assertEqual(self.row()['successor_hash'], SUCCESSOR)
        self.assertNotEqual(self.row()['rotation_status'], 'complete')
        with self.assertRaises(RuntimeError): self.m.rotate_one('sample-prod-key')
        self.assertEqual(self.mints, 1)

    def test_claim_cannot_alias_a_reserved_successor(self):
        doc = self.m.ledger()
        doc['issued']['sample-prod-key'].update(rotation_status='delivery-unknown',
            successor_hash=SUCCESSOR, successor_limit_usd=5)
        self.m.save_ledger(doc)
        self.m._label_of = lambda value: 'synthetic-label'
        self.m.read_admin = lambda *a: self.fail('reserved identity refused before provider access')
        with self.assertRaisesRegex(ValueError, 'already in the ledger'):
            self.m.claim_key('alias', VALUE, 'sample')
        self.assertNotIn('alias', self.m.ledger()['issued'])

    def test_existing_alias_cannot_mutate_another_unresolved_rotation(self):
        doc = self.m.ledger()
        doc['issued']['sample-prod-key'].update(rotation_status='delivery-unknown',
            successor_hash=SUCCESSOR, successor_limit_usd=5)
        doc['issued']['alias'] = {'hash': SUCCESSOR, 'account': 'sample',
            'destination': 'vault:sample/prod/OTHER', 'limit_usd': 5}
        self.m.save_ledger(doc)
        self.m.read_admin = lambda *a: self.fail('must refuse before provider access')
        for operation in (lambda: self.m.set_limit('alias', 5),
                          lambda: self.m.toggle_key('alias', False),
                          lambda: self.m.revoke_key('alias'),
                          lambda: self.m.rotate_one('alias')):
            with self.assertRaisesRegex(RuntimeError, 'rotation'):
                operation()

    def test_foreign_or_missing_exposure_identity_keeps_reservation(self):
        rec = {'hash': SUCCESSOR, 'account': 'sample', 'predecessor_hash': 'old',
               'predecessor_limit_usd': 5, 'rotation_status': 'successor-delivered-predecessor-pending'}
        for row in ({'hash': 'foreign', 'limit': 0}, {'limit': 0}, None):
            with self.subTest(row=row):
                self.m._request = lambda *a, **k: {'data': row}
                self.assertIsNone(self.m._rotation_exposure(rec))
                self.assertEqual(rec['predecessor_limit_usd'], 5)
                without_reserve = {k: v for k, v in rec.items() if k != 'predecessor_limit_usd'}
                self.assertIsNone(self.m._rotation_exposure(without_reserve))

    def test_foreign_primary_identity_is_unread_not_free_capacity(self):
        self.m.find_key = lambda *args: {'hash': 'foreign', 'limit': 0}
        rec = self.row()
        state, row, _reason = self.m._read_held('sample-prod-key', rec, 'sample')
        self.assertEqual(state, 'held-unread')
        self.assertIsNone(row)
        self.assertIsNone(self.m._counted(state, row, rec))

    def test_hash_lookup_malformed_response_is_not_absence(self):
        self.m.find_key = self.original_find_key
        for response in ({}, {'data': None}, {'data': {}}, [], {'data': {'hash': 'foreign', 'limit': 0}}):
            with self.subTest(response=response):
                self.m._request = lambda *a, **k: response
                state, row, _reason = self.m._read_held('sample-prod-key', self.row(), 'sample')
                self.assertEqual(state, 'held-unread')
                self.assertIsNone(row)
                self.assertIsNone(self.m._counted(state, row, self.row()))

    def test_unknown_mint_is_reserved_and_never_retried(self):
        self.fault = 'mint-502'
        with self.assertRaisesRegex(RuntimeError, 'unknown') as raised: self.m.rotate_one('sample-prod-key')
        self.assertNotIn('synthetic-secret', str(raised.exception))
        self.assertEqual(self.row()['rotation_status'], 'mint-pending')
        self.assertEqual(self.row()['successor_limit_usd'], 5)
        with self.assertRaises(RuntimeError): self.m.rotate_one('sample-prod-key')
        self.assertEqual(self.mints, 1)

    def test_kill_and_timeout_preserve_successor_before_delivery(self):
        for fault, error in [('kill', KeyboardInterrupt), ('timeout', RuntimeError)]:
            with self.subTest(fault=fault):
                self.setUp(); self.fault = fault
                with self.assertRaises(error): self.m.rotate_one('sample-prod-key')
                row = self.row()
                self.assertEqual(row['successor_hash'], SUCCESSOR)
                self.assertEqual(row['rotation_status'], 'delivery-unknown')
                self.assertEqual(set(self.provider), {'old', SUCCESSOR})

    def test_provider_ceiling_and_reset_survive_stale_ledger(self):
        self.m.rotate_one('sample-prod-key')
        self.assertEqual(self.provider[SUCCESSOR]['limit'], 5)
        self.assertEqual(self.provider[SUCCESSOR]['limit_reset'], 'daily')
        self.assertEqual(self.row()['limit_usd'], 5)

    def test_predecessor_failure_is_counted_separately(self):
        self.fault = 'delete'
        with self.assertRaises(RuntimeError): self.m.rotate_one('sample-prod-key')
        row = self.row()
        self.assertEqual(row['predecessor_hash'], 'old')
        self.assertEqual(self.m._rotation_exposure(row), 500)
        self.assertEqual(self.m._read_held('sample-prod-key', row, 'sample')[0], 'held-rotation')

    def test_policy_counts_extra_and_refuses_success_under_unknown_rotation(self):
        import contextlib
        import io
        self.fault = 'delete'
        with self.assertRaises(RuntimeError): self.m.rotate_one('sample-prod-key')
        self.m.find_key = lambda *args: dict(self.provider[self.row()['hash']])
        self.m._project_folder = lambda project, env, name: 'sample'
        policy = {'kind': 'spend-policy', 'project': 'sample', 'mode': 'limited',
                  'limitUsd': 5, 'period': 'month',
                  'credentialSources': [{'kind': 'project-key', 'ref': 'vault:sample/prod/KEY'}]}
        policy_path = Path(self.temp.name) / 'policy.json'; policy_path.write_text(json.dumps(policy))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = self.m.cmd_issue_for_policy(str(policy_path), 'sample', None, False)
        self.assertEqual(code, 1)
        self.assertIn('OVER LIMIT', output.getvalue())
        self.assertIn('$10.00', output.getvalue())
        self.assertEqual(self.mints, 1)

    def test_unresolved_rotation_cannot_be_forgotten_by_other_mutators(self):
        self.fault = 'delete'
        with self.assertRaises(RuntimeError): self.m.rotate_one('sample-prod-key')
        for operation in (lambda: self.m.set_limit('sample-prod-key', 10),
                          lambda: self.m.toggle_key('sample-prod-key', False),
                          lambda: self.m.revoke_key('sample-prod-key')):
            with self.assertRaisesRegex(RuntimeError, 'rotation'):
                operation()
        self.assertEqual(set(self.provider), {'old', SUCCESSOR})

    def test_provider_unread_exposure_and_missing_are_distinct(self):
        rec = {'hash': SUCCESSOR, 'account': 'sample', 'predecessor_hash': 'old', 'predecessor_limit_usd': 5}
        self.m._request = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('openrouter answered HTTP 500'))
        self.assertIsNone(self.m._rotation_exposure(rec))
        del rec['predecessor_limit_usd']
        self.assertIsNone(self.m._rotation_exposure(rec))
        self.m._request = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('openrouter answered HTTP 404'))
        self.assertEqual(self.m._rotation_exposure(rec), 0)

    def test_unknown_rotation_extras_never_supply_new_policy_headroom(self):
        import test_credential_doors as T
        for mode in ('unread', 'foreign', 'malformed', 'hashless', 'fresh', 'missing'):
            with self.subTest(mode=mode):
                m, p = T._r3_door()
                m.issue_for_policy(T._pol(1, limit=5), 'example')
                doc = m.ledger(); name, rec = next(iter(doc['issued'].items()))
                extra = p('/keys', 'synthetic', {'name': 'synthetic-successor', 'limit': 8,
                                               'limit_reset': 'monthly'})['data']['hash']
                rec.update(rotation_status='delivery-unknown', successor_hash=extra, successor_limit_usd=5)
                if mode == 'hashless':
                    rec.pop('successor_hash'); rec['rotation_status'] = 'mint-pending'
                if mode == 'missing': p.keys.pop(extra)
                if mode == 'unread': p.unread.add(extra)
                if mode in ('foreign', 'malformed'):
                    def request(path, *args, _mode=mode, **kwargs):
                        if path == '/keys/' + extra:
                            return {'data': {'hash': 'foreign', 'limit': 0}} if _mode == 'foreign' else {}
                        return p(path, *args, **kwargs)
                    m._request = request
                m.save_ledger(doc)
                before = (p.n, p.patches, p.total())
                plan = m.issue_for_policy(T._pol(2, limit=30 if mode == 'fresh' else 20), 'example')
                saved = m.ledger()['issued'][name]
                self.assertEqual(saved['successor_limit_usd'], 5)
                self.assertEqual(saved.get('successor_hash'), rec.get('successor_hash'))
                if mode in ('fresh', 'missing'):
                    self.assertEqual(p.n, before[0] + 1)
                    self.assertLessEqual(p.total(), 30 if mode == 'fresh' else 20)
                    self.assertEqual(m._rotation_exposure(saved), 800 if mode == 'fresh' else 0)
                else:
                    self.assertEqual((p.n, p.patches, p.total()), before)
                    self.assertIsNone(m._rotation_exposure(saved))
                    self.assertIn('blocked', [row['action'] for row in plan])

    def test_missing_or_malformed_rotation_identity_is_unknown_without_provider_calls(self):
        self.m._request = lambda *a, **k: self.fail('unknown identity cannot be queried')
        base = {'hash': 'primary', 'account': 'sample'}
        for fields in ({'predecessor_limit_usd': 5},
                       {'rotation_status': 'successor-delivered-predecessor-pending'},
                       {'successor_limit_usd': 5},
                       {'rotation_status': 'delivery-unknown'},
                       {'successor_hash': [], 'successor_limit_usd': 5},
                       {'rotation_status': 'unknown-phase'}):
            with self.subTest(fields=fields):
                self.assertIsNone(self.m._rotation_exposure(dict(base, **fields)))
        self.m.read_admin = lambda *a: (_ for _ in ()).throw(LookupError('unavailable account'))
        self.assertIsNone(self.m._rotation_exposure(dict(base, predecessor_hash='known-extra')))

    def test_completed_pending_issue_clears_pending(self):
        doc = self.m.ledger(); doc['issued']['sample-prod-key']['pending'] = True; self.m.save_ledger(doc)
        self.m.rotate_one('sample-prod-key')
        self.assertFalse(self.row().get('pending'))

    def test_every_ledger_mutator_refuses_while_another_writer_holds_lock(self):
        self.m.POLICY_LOCK_WAIT_S = 0.001
        self.m.read_admin = lambda *a: self.fail('must acquire lock before provider or credential reads')
        operations = [lambda: self.m.rotate_one('sample-prod-key'),
                      lambda: self.m.issue_key('new', 5, 'sample', 'vault:sample/prod/NEW', 'sample'),
                      lambda: self.m.set_limit('sample-prod-key', 5),
                      lambda: self.m.toggle_key('sample-prod-key', False),
                      lambda: self.m.revoke_key('sample-prod-key'),
                      lambda: self.m.claim_key('new', 'synthetic-value', 'sample'),
                      lambda: self.m.forget_pending_key('sample-prod-key', 'synthetic attestation')]
        with self.m._policy_lock():
            for operation in operations:
                with self.assertRaises(RuntimeError): operation()
        self.assertEqual(self.mints, 0)

    def test_each_post_mint_ledger_failure_retains_a_reservation_or_identity(self):
        for fail_at in (2, 3, 4):
            with self.subTest(fail_at=fail_at):
                self.setUp(); original = self.m.save_ledger; writes = []
                def save(doc):
                    writes.append(1)
                    if len(writes) == fail_at: raise OSError('synthetic write failure')
                    original(doc)
                self.m.save_ledger = save
                with self.assertRaises(RuntimeError): self.m.rotate_one('sample-prod-key')
                row = self.row()
                self.assertNotEqual(row['rotation_status'], 'complete')
                self.assertTrue(row.get('successor_limit_usd') or row.get('predecessor_hash'))
                self.assertEqual(self.mints, 1)
                if fail_at < 4: self.assertIn('old', self.provider)

    def test_unresolved_slot_is_nonzero_even_when_total_fits(self):
        import contextlib
        import io
        self.fault = 'delete'
        with self.assertRaises(RuntimeError): self.m.rotate_one('sample-prod-key')
        self.m._project_folder = lambda *a: 'sample'
        self.m.find_key = lambda *a: dict(self.provider[self.row()['hash']])
        policy = {'kind': 'spend-policy', 'project': 'sample', 'mode': 'limited', 'limitUsd': 100,
                  'period': 'month', 'credentialSources': [{'kind': 'project-key', 'ref': 'vault:sample/prod/KEY'}]}
        path = Path(self.temp.name) / 'policy.json'; path.write_text(json.dumps(policy))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.m.cmd_issue_for_policy(str(path), 'sample', None, False), 1)
        self.assertEqual(self.mints, 1)

    def test_intent_write_failure_prevents_mint(self):
        self.m.save_ledger = lambda doc: (_ for _ in ()).throw(OSError('synthetic-secret-body'))
        with self.assertRaises(RuntimeError): self.m.rotate_one('sample-prod-key')
        self.assertEqual(self.mints, 0)

if __name__ == '__main__': unittest.main()
