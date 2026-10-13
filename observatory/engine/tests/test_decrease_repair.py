"""Supplemental actual-door decrease contract; does not rewrite original adversary events."""
import contextlib
import copy
import io
import json
import random
import unittest
from unittest.mock import patch
import adv_j1_property as A


class DecreaseRepair(unittest.TestCase):
    def setUp(self):
        guard = patch('socket.socket.connect', side_effect=AssertionError('synthetic only'))
        guard.start(); self.addCleanup(guard.stop)
        self.m, self.p, self.door, _ = A.make_door(random.Random(7))
        self.p.echo = lambda x: x; self.p.drift = 0
        self.p.page = self.m.PAGE = 100; self.m.PAGES_MAX = 100
        self.name = 'project-a-prod-k0'
        self.m.issue_key(self.name, 10, 'example', 'vault:project-a/prod/K0', 'project-a')
        self.handle = self.record()['hash']
        self.door['journal'].clear()

    def main(self, args):
        with patch.object(self.m.sys, "argv", ["openrouter.py"] + args):
            return self.m.main()

    def record(self):
        return self.m.ledger()['issued'][self.name]

    def invoke(self, amount=9):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return self.m.cmd_limit(self.name, amount, decrease_only=True)

    def tamper(self, fn):
        doc = self.m.ledger(); fn(doc)
        self.m.LEDGER.write_text(json.dumps(doc))  # deliberate external synthetic corruption

    def leave_unknown(self):
        original = self.m._request
        def request(path, admin, payload=None, method=None):
            result = original(path, admin, payload, method)
            if method == 'PATCH': raise self.m.Unreachable('synthetic lost receipt')
            return result
        self.m._request = request
        self.assertEqual(self.invoke(), 1)
        self.m._request = original
        return self.record()['ceiling_repair']

    def test_actual_cli_decrease_commits_actual_after_required_observation(self):
        events = []; original_save = self.m.save_ledger; original_req = self.m._request
        def save(doc):
            events.append(('save', doc['issued'][self.name].get('ceiling_repair', {}).get('status')))
            return original_save(doc)
        def request(path, admin, payload=None, method=None):
            if method == 'PATCH':
                self.assertEqual(self.record()['ceiling_repair']['status'], 'intent')
                events.append(('PATCH', payload['limit']))
            return original_req(path, admin, payload, method)
        self.m.save_ledger = save; self.m._request = request
        self.p.echo = lambda x: 9.004
        self.assertEqual(self.invoke(), 1)  # observed but requested share unmet
        rec = self.record(); proof = rec['ceiling_repair']
        self.assertEqual((rec['limit_usd'], proof['actual']), (9.004, 9.004))
        self.assertEqual(proof['status'], 'observed-unmet')
        self.assertEqual(events, [('save', 'intent'), ('PATCH', 9), ('save', 'observed-unmet')])
        self.assertTrue(all(k.get('require_success') for a, k in self.door['journal']))
        self.assertEqual(len(self.door['journal']), 2)

    def test_equal_target_is_read_only_and_smaller_current_refuses(self):
        before = (copy.deepcopy(self.m.ledger()), self.p.writes)
        self.assertEqual(self.invoke(10), 0)
        self.assertEqual((self.m.ledger(), self.p.writes), before)
        self.p.keys[self.handle]['limit'] = 8
        self.assertEqual(self.invoke(9), 1)
        self.assertEqual((self.m.ledger(), self.p.writes), before)

    def test_unknown_or_malformed_fresh_ceiling_never_patches(self):
        for value in (None, -1, float('nan'), float('inf'), True, 0):
            with self.subTest(value=value):
                self.p.keys[self.handle]['limit'] = value
                before = self.p.writes
                self.assertEqual(self.invoke(), 1)
                self.assertEqual(self.p.writes, before)
                self.assertNotIn('ceiling_repair', self.record())

    def test_intent_or_prejournal_failure_prevents_provider_effect(self):
        before = self.p.writes
        self.door['save'] = [1]
        with self.assertRaises(OSError): self.m.decrease_limit(self.name, 9)
        self.assertEqual(self.p.writes, before)
        self.m._journal = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('synthetic journal failure'))
        with self.assertRaises(RuntimeError): self.m.decrease_limit(self.name, 9)
        self.assertEqual(self.p.writes, before)
        self.assertEqual(self.record()['ceiling_repair']['status'], 'intent')

    def test_effect_unknown_retains_intent_and_does_not_blind_retry(self):
        repair = self.leave_unknown(); before = self.p.writes
        self.assertEqual(repair['status'], 'effect-unknown')
        self.assertIsNone(repair['actual'])
        self.assertEqual(self.record()['limit_usd'], 10)
        self.assertEqual(self.invoke(), 1)
        self.assertEqual(self.p.writes, before)
        observed = self.m.repair_status(self.name, refresh=True)
        self.assertEqual(observed['status'], 'observed-satisfied')
        self.assertEqual(observed['basis'], 'subsequent-current-read')
        self.assertEqual(self.record()['limit_usd'], 9)
        self.assertEqual(self.p.writes, before)

    def test_final_save_failure_keeps_intent_and_actual_effect_separate(self):
        self.door['save'] = [2]
        with self.assertRaises(OSError): self.m.decrease_limit(self.name, 9)
        self.assertEqual(self.p.keys[self.handle]['limit'], 9)
        self.assertEqual(self.record()['limit_usd'], 10)
        self.assertEqual(self.record()['ceiling_repair']['status'], 'intent')

    def test_failed_refresh_invalidates_old_success_before_get(self):
        self.assertEqual(self.invoke(), 0)
        previous = copy.deepcopy(self.record()['ceiling_repair'])
        self.p.unread.add(self.handle); before = self.p.writes
        self.assertEqual(self.m.repair_status(self.name, refresh=True)['status'], 'observation-unknown')
        rec = self.record()['ceiling_repair']
        self.assertIsNone(rec['actual'])
        self.assertEqual(rec['previous_observation']['actual'], previous['actual'])
        self.assertEqual(rec['previous_observation']['observed_at'], previous['observed_at'])
        self.assertEqual(self.p.writes, before)
        self.assertEqual(self.invoke(8), 1)
        self.assertEqual(self.p.writes, before)

    def test_unknown_repair_blocks_actual_alias_writers_and_policy_provenance(self):
        self.leave_unknown()
        self.tamper(lambda doc: doc['issued'].update({'alias': dict(doc['issued'][self.name],
                         ceiling_repair=None)}))
        # An alias with the same hash and no copied repair is a historical state,
        # not a permitted way to mutate the protected identity.
        self.tamper(lambda doc: doc['issued']['alias'].pop('ceiling_repair'))
        before = (copy.deepcopy(self.m.ledger()), self.p.writes)
        for operation in (lambda: self.m.set_limit('alias', 8),
                          lambda: self.m.toggle_key('alias', True),
                          lambda: self.m.rotate_one('alias'),
                          lambda: self.m.revoke_key('alias'),
                          lambda: self.m.issue_for_policy(A.T._pol(1, limit=9), 'example')):
            with self.subTest(operation=operation):
                with self.assertRaises((RuntimeError, ValueError)): operation()
                self.assertEqual((self.m.ledger(), self.p.writes), before)

    def test_malformed_or_context_changed_repair_blocks_before_publication(self):
        self.leave_unknown()
        self.tamper(lambda doc: doc['issued'][self.name].update({'destination':'vault:project-a/prod/OTHER'}))
        before = (copy.deepcopy(self.m.ledger()), self.p.calls, self.p.writes)
        with self.assertRaises(RuntimeError): self.m.repair_status(self.name, refresh=True)
        self.assertEqual((self.m.ledger(), self.p.calls, self.p.writes), before)
        for malformed in (None, {}, {'status':'complete'}):
            self.tamper(lambda doc: doc['issued'][self.name].update({'ceiling_repair':malformed}))
            before = (copy.deepcopy(self.m.ledger()), self.p.writes)
            with self.assertRaises(RuntimeError): self.m.set_limit(self.name, 8)
            self.assertEqual((self.m.ledger(), self.p.writes), before)

    def test_response_unknown_and_observation_journal_failure_keep_unresolved(self):
        original = self.m._request
        for mode in ('foreign', 'missing', 'wrong-reset', 'above-before'):
            with self.subTest(mode=mode):
                self.p.keys[self.handle]['limit'] = 10
                self.tamper(lambda doc: doc['issued'][self.name].pop('ceiling_repair', None))
                def request(path, admin, payload=None, method=None):
                    value = original(path, admin, payload, method)
                    if method == 'PATCH':
                        row = value['data']
                        if mode == 'foreign': row['hash'] = 'f' * 64
                        if mode == 'missing': row.pop('limit', None)
                        if mode == 'wrong-reset': row['limit_reset'] = 'weekly'
                        if mode == 'above-before': row['limit'] = 11
                    return value
                self.m._request = request; before = self.p.writes
                self.assertEqual(self.invoke(), 1)
                self.assertEqual(self.p.writes, before + 1)
                self.assertEqual(self.record()['ceiling_repair']['status'], 'effect-unknown')
                self.assertEqual(self.record()['limit_usd'], 10)
        self.m._request = original
        self.p.keys[self.handle]['limit'] = 10
        self.tamper(lambda doc: doc['issued'][self.name].pop('ceiling_repair'))
        journal = self.m._journal
        def fail_result(action, *a, **kw):
            if action == 'ceiling-repair-observed': raise RuntimeError('synthetic journal unavailable')
            return journal(action, *a, **kw)
        self.m._journal = fail_result
        with self.assertRaises(RuntimeError): self.m.decrease_limit(self.name, 9)
        self.assertEqual(self.record()['ceiling_repair']['status'], 'intent')
        self.assertEqual(self.record()['limit_usd'], 10)

    def test_refresh_journal_and_final_save_failures_leave_pending(self):
        self.assertEqual(self.invoke(), 0)
        self.door['save'] = [2]
        with self.assertRaises(OSError): self.m.repair_status(self.name, refresh=True)
        self.assertEqual(self.record()['ceiling_repair']['status'], 'observation-pending')
        self.m._journal = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('synthetic journal unavailable'))
        before = self.p.calls
        with self.assertRaises(RuntimeError): self.m.repair_status(self.name, refresh=True)
        self.assertEqual(self.p.calls, before)
        self.assertIsNone(self.record()['ceiling_repair']['actual'])

    def test_resolved_policy_reattest_supersedes_history_atomically(self):
        self.assertEqual(self.invoke(), 0)
        proof = copy.deepcopy(self.record()['ceiling_repair'])
        self.m.issue_for_policy(A.T._pol(1, limit=9), 'example')
        now = self.record()['ceiling_repair']
        self.assertEqual(now['status'], 'superseded')
        self.assertEqual(now['attempt_id'], proof['attempt_id'])
        self.assertIsNone(now['actual'])
        self.assertEqual(now['previous_observation']['actual'], 9)
        self.assertEqual(now['superseded_context'], self.m._repair_context(self.record()))
        # Subsequent legitimate policy change preserves the original actual history.
        self.m.issue_for_policy(A.T._pol(1, limit=8), 'example')
        self.assertEqual(self.record()['ceiling_repair']['previous_observation']['actual'], 9)
        self.tamper(lambda doc: doc['issued'][self.name].update(destination='vault:project-a/prod/OTHER'))
        before = (self.m.ledger(), self.p.writes)
        with self.assertRaises(RuntimeError): self.m.toggle_key(self.name, True)
        self.assertEqual((self.m.ledger(), self.p.writes), before)

    def test_supersession_failure_preserves_prechange_context(self):
        self.assertEqual(self.invoke(), 0)
        before = (self.m.ledger(), self.p.writes)
        journal = self.m._journal
        self.m._journal = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('synthetic journal unavailable'))
        with self.assertRaises(RuntimeError): self.m.issue_for_policy(A.T._pol(1, limit=9), 'example')
        self.assertEqual((self.m.ledger(), self.p.writes), before)
        self.m._journal = journal; self.door['save'] = [1]
        with self.assertRaises(OSError): self.m.issue_for_policy(A.T._pol(1, limit=9), 'example')
        self.assertEqual((self.m.ledger(), self.p.writes), before)

    def test_internal_and_direct_publication_aliases_refuse_unresolved(self):
        self.leave_unknown()
        self.tamper(lambda d: d['issued'].update(alias={k:v for k,v in d['issued'][self.name].items() if k != 'ceiling_repair'}))
        self.tamper(lambda d: d['issued']['alias'].update(account='other-local-label'))
        before = (self.m.ledger(), self.p.writes)
        for operation in (lambda: self.m._forget('alias'),
                          lambda: self.m._undo_mint('synthetic', 'alias', {'hash':self.handle}),
                          lambda: self.m._delivery_failed('synthetic', {'hash':self.handle}),
                          lambda: self.m.issue_key('alias', 8, 'example', 'vault:project-a/prod/K0', 'project-a')):
            with self.assertRaises(RuntimeError): operation()
            self.assertEqual((self.m.ledger(), self.p.writes), before)
        doc = self.m.ledger(); doc['issued']['alias']['destination'] = 'vault:project-a/prod/OTHER'
        with self.assertRaises(RuntimeError): self.m.save_ledger(doc)
        self.assertEqual((self.m.ledger(), self.p.writes), before)

    def test_malformed_resolved_semantics_do_not_authorize_writes(self):
        self.assertEqual(self.invoke(), 0)
        good = copy.deepcopy(self.record()['ceiling_repair'])
        for changes in ({'status':'observed-unmet'}, {'actual':11}, {'before':8},
                        {'reason':'not-observed'}, {'basis':'unknown'}, {'observed_at':None},
                        {'effect':{'status':'unconfirmed', 'actual':None, 'observed_at':None}},
                        {'effect':dict(good['effect'], actual=8)}):
            with self.subTest(changes=changes):
                self.tamper(lambda doc: doc['issued'][self.name].update(ceiling_repair=dict(good, **changes)))
                before = (self.m.ledger(), self.p.writes)
                with self.assertRaises(RuntimeError): self.m.toggle_key(self.name, True)
                self.assertEqual((self.m.ledger(), self.p.writes), before)

    def test_parser_ledger_only_status_and_invalid_flag_combinations(self):
        self.assertEqual(self.invoke(), 0)
        before = (self.p.calls, self.p.writes, self.m.ledger())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.main(['limit', self.name, '--repair-status']), 0)
            for flags in (['--refresh'], ['--decrease-only'], ['--repair-status', '--set', '8'],
                          ['--decrease-only', '--set', '8', '--reset', 'daily'],
                          ['--repair-status', '--decrease-only']):
                self.assertEqual(self.main(['limit', self.name] + flags), 2)
        self.assertEqual((self.p.calls, self.p.writes, self.m.ledger()), before)


    def test_refresh_preserves_unknown_effect_as_history(self):
        self.leave_unknown()
        proof = self.m.repair_status(self.name, refresh=True)
        self.assertEqual(proof['status'], 'observed-satisfied')
        self.assertEqual(proof['effect']['status'], 'unconfirmed')
        self.assertIsNone(proof['effect']['actual'])
        self.assertEqual(proof['basis'], 'subsequent-current-read')

    def test_exact_context_unknown_and_identity_guards_precede_effect(self):
        original = self.m.find_key
        for changes in ({'hash':'f'*64}, {'limit_reset':'weekly'}, {'limit':None}):
            with self.subTest(changes=changes):
                self.m.find_key = lambda *a, **kw: dict(original(*a, **kw), **changes)
                before = (self.m.ledger(), self.p.writes)
                self.assertEqual(self.invoke(), 1)
                self.assertEqual((self.m.ledger(), self.p.writes), before)
        self.m.find_key = original
        for changes in ({'pending':True}, {'rotation_status':'mint-pending'}, {'successor_hash':'f'*64}):
            with self.subTest(changes=changes):
                old = self.m.ledger()
                self.tamper(lambda d: d['issued'][self.name].update(changes))
                before = (self.m.ledger(), self.p.calls, self.p.writes)
                self.assertEqual(self.invoke(), 1)
                self.assertEqual((self.m.ledger(), self.p.calls, self.p.writes), before)
                self.m.LEDGER.write_text(json.dumps(old))

    def test_claim_alias_guard_and_manual_distinct_identity_compatibility(self):
        self.leave_unknown()
        before = (self.m.ledger(), self.p.calls, self.p.writes)
        self.m._label_of = lambda value: 'synthetic-label'
        with self.assertRaises(RuntimeError):
            self.m.claim_key('alias', 'synthetic-adversary-key-00000001', 'other-local-label', 'vault:project-a/prod/K1')
        self.assertEqual((self.m.ledger(), self.p.calls, self.p.writes), before)
        self.m.issue_key('independent', 10, 'example', 'vault:project-b/prod/K0', 'project-b')
        # General manually requested limit still supports raising a distinct identity.
        self.assertEqual(self.m.set_limit('independent', 12)['limit'], 12)

    def test_unknown_error_bodies_are_never_persisted_or_printed(self):
        original = self.m._request
        marker = 'synthetic-private-provider-body'
        def request(path, admin, payload=None, method=None):
            if method == 'PATCH': raise RuntimeError(marker)
            return original(path, admin, payload, method)
        self.m._request = request
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            self.assertEqual(self.m.cmd_limit(self.name, 9, decrease_only=True), 1)
        self.assertNotIn(marker, stream.getvalue())
        self.assertNotIn(marker, json.dumps(self.m.ledger()))
        self.assertNotIn(marker, repr(self.door['journal']))



    def test_context_changed_during_provider_read_cannot_be_overwritten(self):
        original = self.m.find_key
        def read(*args):
            row = original(*args)
            self.tamper(lambda d: d['issued'][self.name].update(destination='vault:project-a/prod/OTHER'))
            return row
        self.m.find_key = read
        before = self.p.writes
        self.assertEqual(self.invoke(), 1)
        self.assertEqual(self.p.writes, before)
        self.assertEqual(self.record()['destination'], 'vault:project-a/prod/OTHER')
        self.assertNotIn('ceiling_repair', self.record())

    def test_context_changed_during_effect_keeps_pending_and_external_context(self):
        original = self.m._request
        def request(path, admin, payload=None, method=None):
            row = original(path, admin, payload, method)
            if method == 'PATCH':
                self.tamper(lambda d: d['issued'][self.name].update(destination='vault:project-a/prod/OTHER'))
            return row
        self.m._request = request
        self.assertEqual(self.invoke(), 1)
        self.assertEqual(self.record()['destination'], 'vault:project-a/prod/OTHER')
        self.assertEqual(self.record()['ceiling_repair']['status'], 'intent')
        self.assertEqual(self.record()['limit_usd'], 10)


    def test_hashless_alias_discovery_cannot_bypass_unresolved_identity(self):
        self.leave_unknown()
        self.p.keys[self.handle]['name'] = 'legacy-alias'
        def add(doc):
            doc['issued']['legacy-alias'] = {k:v for k,v in doc['issued'][self.name].items()
                                            if k not in ('hash', 'ceiling_repair')}
            doc['issued']['legacy-alias']['pending'] = True
        self.tamper(add)
        before = (self.m.ledger(), self.p.writes)
        for operation in (lambda: self.m.set_limit('legacy-alias', 8),
                          lambda: self.m.toggle_key('legacy-alias', True)):
            with self.subTest(operation=operation):
                with self.assertRaises(RuntimeError): operation()
                self.assertEqual((self.m.ledger(), self.p.writes), before)

    def test_equal_target_refuses_context_drift_after_read(self):
        original = self.m.find_key
        def read(*args):
            row = original(*args)
            self.tamper(lambda d: d['issued'][self.name].update(destination='vault:project-a/prod/OTHER'))
            return row
        self.m.find_key = read; before = self.p.writes
        self.assertEqual(self.invoke(10), 1)
        self.assertEqual(self.p.writes, before)
        self.assertNotIn('ceiling_repair', self.record())
        self.assertEqual(self.record()['destination'], 'vault:project-a/prod/OTHER')

    def test_distinct_hashless_alias_retains_general_manual_semantics(self):
        self.leave_unknown()
        self.m.issue_key('independent', 10, 'example', 'vault:project-b/prod/K0', 'project-b')
        self.tamper(lambda d: d['issued']['independent'].pop('hash'))
        before = self.p.writes
        self.assertEqual(self.m.set_limit('independent', 12)['limit'], 12)
        self.m.toggle_key('independent', True)
        self.assertEqual(self.p.writes, before + 2)
        self.assertEqual(self.record()['ceiling_repair']['status'], 'effect-unknown')



    def test_hashless_alias_policy_provenance_cannot_precede_identity_guard(self):
        self.tamper(lambda d: d['issued'][self.name].update(destination='vault:project-b/prod/K0'))
        self.leave_unknown()
        self.p.keys[self.handle]['name'] = 'legacy-alias'
        def add(doc):
            row = {k:v for k,v in doc['issued'][self.name].items() if k not in ('hash', 'ceiling_repair')}
            row.update(destination='vault:project-a/prod/K0', pending=True)
            doc['issued']['legacy-alias'] = row
        self.tamper(add)
        before = (self.m.ledger(), self.p.writes)
        with self.assertRaises(RuntimeError): self.m.issue_for_policy(A.T._pol(1, limit=9), 'example')
        self.assertEqual((self.m.ledger(), self.p.writes), before)



    def test_hashless_attestation_unknown_refuses_and_distinct_identity_passes(self):
        self.tamper(lambda d: d['issued'][self.name].update(destination='vault:project-b/prod/K0'))
        self.leave_unknown()
        self.m.issue_key('independent', 10, 'example', 'vault:project-a/prod/K0', 'project-a')
        self.tamper(lambda d: d['issued']['independent'].pop('hash'))
        original = self.m.find_key
        for result in (None, {}, {'name':'independent', 'hash':'unknown'}):
            self.m.find_key = lambda *a, **kw: result
            before = (self.m.ledger(), self.p.writes)
            with self.assertRaises(RuntimeError): self.m.issue_for_policy(A.T._pol(1, limit=10), 'example')
            self.assertEqual((self.m.ledger(), self.p.writes), before)
        self.m.find_key = original
        before = self.p.writes
        plan = self.m.issue_for_policy(A.T._pol(1, limit=10), 'example')
        self.assertEqual(plan[0]['action'], 'in-place')
        self.assertIn('policy_binding', self.m.ledger()['issued']['independent'])
        self.assertEqual(self.p.writes, before)
        self.assertEqual(self.record()['ceiling_repair']['status'], 'effect-unknown')



    def test_hashless_held_identity_stays_unknown_without_provenance_change(self):
        policy = A.T._pol(1, limit=9)
        self.m.issue_for_policy(policy, 'example')
        def alias(doc):
            doc['issued']['protected'] = dict(doc['issued'][self.name], destination='vault:project-b/prod/K0')
            doc['issued'][self.name].pop('hash')
        self.tamper(alias)
        original = self.m._request; self.p.echo = lambda x: 9
        def lost(path, admin, payload=None, method=None):
            result = original(path, admin, payload, method)
            if method == 'PATCH': raise RuntimeError('synthetic unknown effect')
            return result
        self.m._request = lost
        self.assertEqual(self.m.decrease_limit('protected', 8)['status'], 'effect-unknown')
        self.m._request = original
        before = (self.m.ledger(), self.p.writes)
        state, row, reason = self.m._read_held(self.name, self.record(), 'example')
        self.assertEqual(state, 'held-unread'); self.assertIsNone(row)
        plan = self.m.issue_for_policy(policy, 'example')
        self.assertEqual(plan[0]['action'], 'held-unread')
        self.assertEqual((self.m.ledger(), self.p.writes), before)


if __name__ == '__main__': unittest.main()
