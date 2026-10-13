"""Validate the full-rotation fake and retain red production properties."""
import contextlib
import copy
import hashlib
import io
import random
import unittest
from unittest.mock import patch
import adv_j1_property as A


class ModelBinding(unittest.TestCase):
    def setUp(self):
        guard = patch('socket.socket.connect', side_effect=AssertionError('synthetic only'))
        guard.start(); self.addCleanup(guard.stop)
        self.m, self.p, self.door, _ = A.make_door(random.Random(7))
        self.p.echo = lambda x: x
        self.p.drift = 0
        self.p.page = self.m.PAGE = 100
        self.m.PAGES_MAX = 100
        self.pol = A.T._pol(1, limit=10)

    def issue(self):
        # Standalone scope exercises actual rotation mechanics; policy-managed
        # admission now refuses overlap separately. The full adversary is unchanged.
        self.m.issue_key('project-a-prod-k0',10,'example','vault:project-a/prod/K0','project-a')
        return next(iter(self.m.ledger()['issued']))

    def test_producer_refresh_preserves_holders_and_has_no_door_effects(self):
        name = self.issue(); row = self.m.ledger()['issued'][name]
        h = row['hash']; consumers = {'worker': self.p.keys[h]['label']}
        before = (copy.deepcopy(self.m.ledger()), copy.deepcopy(self.p.keys), self.p.calls, self.p.writes)
        receipt = A.refresh_consumers(self.m, self.p, consumers)
        self.assertEqual(receipt['schema_version'], 1)
        self.assertEqual(receipt['degraded'], [])
        self.assertEqual(receipt['destination_hashes']['worker']['hash'], h)
        self.m._revoke_scan()
        self.assertEqual(before[2:], (self.p.calls, self.p.writes))
        self.assertEqual(A.revoke(self.m, name), 1)
        receipt = A.refresh_consumers(self.m, self.p, consumers)
        self.assertEqual(receipt['destinations'], consumers)
        # Revoke reads only; refresh itself must not consume fake-door calls.
        self.assertEqual(before[:2], (self.m.ledger(), self.p.keys))
        self.assertEqual(before[3], self.p.writes)

    def test_revoke_cli_boundary_preserves_effect_and_failed_ledger_write(self):
        name = self.issue(); before = copy.deepcopy(self.m.ledger())
        handle = before['issued'][name]['hash']
        self.door['save'] = [1]
        self.assertEqual(A.remedy_revoke(self.m, self.p, name, {}), 2)
        self.assertNotIn(handle, self.p.keys)  # DELETE happened before save failed.
        self.assertEqual(self.m.ledger(), before)
        self.assertIsNone(self.door['save'])  # The original failure hook was consumed.
        self.assertEqual(A.untracked(self.m, self.p), [])
        self.assertEqual(A.remedy_revoke(self.m, self.p, name, {}), 0)
        self.assertNotIn(name, self.m.ledger()['issued'])

    def test_revoke_cli_boundary_catches_only_the_real_entrypoint_tuple(self):
        for error in (OSError, ValueError, RuntimeError):
            with patch.object(self.m, 'cmd_revoke', side_effect=error('synthetic')):
                self.assertEqual(A.revoke(self.m, 'fixture'), 2)
        with patch.object(self.m, 'cmd_revoke', side_effect=LookupError('unexpected')):
            with self.assertRaises(LookupError): A.revoke(self.m, 'fixture')

    def test_producer_empty_positive_unlisted_and_stale_negative(self):
        import datetime, json
        name = self.issue(); h = self.m.ledger()['issued'][name]['hash']
        consumers = {'worker': self.p.keys[h]['label']}
        calls = self.p.calls
        doc = A.refresh_consumers(self.m, self.p, consumers, listed=False)
        self.assertTrue(doc['degraded'])
        self.assertEqual(A.revoke(self.m, name), 1)
        self.assertEqual(self.p.calls, calls)
        doc = A.refresh_consumers(self.m, self.p, {})
        self.assertEqual(doc['degraded'], [])
        doc['scanned_at'] = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=37)).isoformat()
        path = self.m.paths.SCRATCH / 'openrouter.json'; path.write_text(json.dumps(doc))
        self.assertEqual(A.revoke(self.m, name), 1)
        path.unlink(); self.assertEqual(A.revoke(self.m, name), 1)
        A.refresh_consumers(self.m, self.p, {})
        self.assertEqual(A.revoke(self.m, name), 0)
        self.assertNotIn(h, self.p.keys)

    def test_convergence_refresh_does_not_erase_observed_consumers(self):
        seen = []
        real = A.refresh_consumers
        def observed(m, p, consumers, **kwargs):
            seen.append(dict(consumers))
            return real(m, p, consumers, **kwargs)
        with patch.object(A, 'refresh_consumers', side_effect=observed):
            A.case(7000099)
        prior = {}
        self.assertTrue(any(seen))
        for current in seen:
            self.assertTrue(prior.items() <= current.items())
            prior = current

    def limit_fixture(self):
        name = self.issue(); row = self.m.ledger()['issued'][name]
        # Source-backed measured near-target observation, not invented prose.
        self.p.keys[row['hash']]['limit'] = 9.004
        plan = self.m.issue_for_policy(self.pol, 'example', limit_override=9)
        return name, row['hash'], plan

    def held_limit_fixture(self):
        name = self.issue(); handle = self.m.ledger()['issued'][name]['hash']
        self.p.keys[handle]['limit'] = 10.004
        plan = self.m.issue_for_policy(self.pol, 'example')
        self.assertEqual(plan[0]['action'], 'held-ceiling')
        self.assertIn(f'limit {name} --set 10', plan[-1]['why'])
        return name, handle, plan

    def test_actual_emitted_held_ceiling_remedy_uses_same_declared_target(self):
        name, handle, plan = self.held_limit_fixture()
        before = (self.p.calls, self.p.writes, copy.deepcopy(self.m.ledger()))
        proposals = A.limit_remedies(self.m, self.p, plan)
        self.assertEqual(len(proposals), 1)
        proposal, = proposals
        self.assertEqual((self.p.calls, self.p.writes, self.m.ledger()), before)
        self.assertEqual(proposal['amount'], 10)
        self.assertEqual(A.apply_limit_remedy(self.m, self.p, plan, proposal)['status'], 'operator-applied')
        self.assertEqual(self.p.keys[handle]['limit'], 10)
        self.assertEqual(self.p.writes, before[1] + 1)
        self.assertEqual(self.m.issue_for_policy(self.pol, 'example')[0]['action'], 'in-place')

    def test_held_ceiling_remedy_still_requires_emission_identity_and_unchanged_revision(self):
        name, handle, plan = self.held_limit_fixture()
        proposals = A.limit_remedies(self.m, self.p, plan)
        self.assertEqual(len(proposals), 1)
        proposal, = proposals
        writes = self.p.writes
        missing = copy.deepcopy(plan); missing[0].pop('repair_command')
        self.assertEqual(A.limit_remedies(self.m, self.p, missing), [])
        bad = copy.deepcopy(plan); bad[0]['why'] = f'limit {name} --set 10'
        self.assertEqual(A.limit_remedies(self.m, self.p, bad), [])
        for update in ({'hash': 'foreign'}, {'limit': None}, {'limit': 9}):
            saved = copy.deepcopy(self.p.keys[handle]); self.p.keys[handle].update(update)
            self.assertEqual(A.limit_remedies(self.m, self.p, plan), [])
            self.p.keys[handle] = saved
        bad = copy.deepcopy(plan); bad[0]['limit'] = 9
        self.assertEqual(A.apply_limit_remedy(self.m, self.p, bad, proposal)['status'], 'refused')
        doc = self.m.ledger(); doc['issued'][name]['limit_usd'] = 11; self.m.save_ledger(doc)
        self.assertEqual(A.apply_limit_remedy(self.m, self.p, plan, proposal)['status'], 'refused')
        self.assertEqual(self.p.writes, writes)

    def test_emitted_limit_remedy_uses_real_command_and_preserves_source(self):
        name, h, plan = self.limit_fixture()
        calls = self.p.calls
        proposal, = A.limit_remedies(self.m, self.p, plan)
        self.assertEqual(self.p.calls, calls)
        result = A.apply_limit_remedy(self.m, self.p, plan, proposal)
        self.assertEqual(result['status'], 'operator-applied')
        self.assertEqual(self.p.keys[h]['limit'], 9)
        self.assertEqual(self.m.ledger()['issued'][name]['limit_usd'], 9)

    def test_limit_remedy_rejects_foreign_unknown_raised_changed_and_prose_targets(self):
        name, h, plan = self.limit_fixture()
        proposal, = A.limit_remedies(self.m, self.p, plan)
        original = copy.deepcopy(self.m.ledger())
        changed = copy.deepcopy(original); changed['issued'][name]['destination'] = 'vault:other/prod/X'
        self.m.save_ledger(changed)
        self.assertEqual(A.apply_limit_remedy(self.m, self.p, plan, proposal)['status'], 'refused')
        self.m.save_ledger(original)
        for update in ({'hash':'foreign'}, {'limit':None}, {'limit':8}):
            prior = self.p.keys[h].copy(); self.p.keys[h].update(update)
            self.assertEqual(A.limit_remedies(self.m, self.p, plan), [])
            self.p.keys[h] = prior
        bad = copy.deepcopy(plan); bad[0]['why'] += '; run arbitrary-command'
        self.assertEqual(A.apply_limit_remedy(self.m, self.p, bad, proposal)['status'], 'refused')
        bad[0]['limit'] = 8
        self.assertEqual(A.limit_remedies(self.m, self.p, bad), [])
        self.assertEqual(self.p.keys[h]['limit'],9.004)

    def test_unknown_limit_effect_and_persistent_echo_are_not_rewritten_to_success(self):
        name,h,plan = self.limit_fixture(); proposal, = A.limit_remedies(self.m,self.p,plan)
        original = self.m._request
        def lost(path,key,payload=None,method=None):
            result = original(path,key,payload,method)
            if method == 'PATCH': raise self.m.Unreachable('synthetic unknown effect')
            return result
        self.m._request = lost
        self.assertEqual(A.apply_limit_remedy(self.m,self.p,plan,proposal)['status'],'unresolved')
        self.assertEqual(self.m.ledger()['issued'][name]['limit_usd'],10)
        self.assertEqual(self.p.keys[h]['limit'],9)
        self.m._request = original; self.p.keys[h]['limit'] = 9.004
        proposal, = A.limit_remedies(self.m,self.p,plan)
        writes = self.p.writes
        self.assertEqual(A.apply_limit_remedy(self.m,self.p,plan,proposal)['status'], 'unresolved')
        self.assertEqual(self.p.writes, writes)
        self.assertEqual(self.m.ledger()['issued'][name]['ceiling_repair']['status'], 'effect-unknown')

    def test_fresh_persistent_echo_is_unmet_not_success(self):
        name,h,plan = self.limit_fixture()
        self.p.echo = lambda x: x + .0004
        proposal, = A.limit_remedies(self.m,self.p,plan)
        self.assertEqual(A.apply_limit_remedy(self.m,self.p,plan,proposal)['status'], 'unresolved')
        self.assertEqual(self.p.keys[h]['limit'],9.0004)

    def test_guarded_guidance_round_trips_large_planned_target(self):
        name = self.issue(); handle = self.m.ledger()['issued'][name]['hash']
        target = 123456.78
        self.p.keys[handle]['limit'] = target + .004
        plan = self.m.issue_for_policy(A.T._pol(1, limit=target), 'example')
        command = plan[0]['repair_command']
        self.assertEqual(command, f'limit {name} --set 123456.78 --decrease-only')
        self.assertIn(command, plan[0]['why'])
        self.assertIn(command, plan[-1]['why'])
        proposal, = A.limit_remedies(self.m, self.p, plan)
        self.assertEqual(proposal['amount'], target)

    def test_guarded_guidance_is_emitted_without_aggregate_overflow(self):
        name = self.issue(); handle = self.m.ledger()['issued'][name]['hash']
        self.p.keys[handle]['limit'] = 9.004
        plan = self.m.issue_for_policy(self.pol, 'example', limit_override=9)
        self.assertEqual([r['action'] for r in plan], ['held-ceiling'])
        self.assertEqual(plan[0]['repair_command'], f'limit {name} --set 9.00 --decrease-only')
        proposal, = A.limit_remedies(self.m, self.p, plan)
        before = self.p.writes
        self.assertEqual(A.apply_limit_remedy(self.m, self.p, plan, proposal)['status'], 'operator-applied')
        self.assertEqual(self.p.writes, before + 1)
        self.assertEqual(self.m.ledger()['issued'][name]['ceiling_repair']['status'], 'observed-satisfied')

    def test_measured_unmet_response_emits_guarded_request_not_auto_retry(self):
        name = self.issue(); self.p.echo = lambda x: x + .004
        before = self.p.writes
        plan = self.m.issue_for_policy(self.pol, 'example', limit_override=9)
        self.assertEqual(plan[0]['action'], 'held-ceiling')
        self.assertEqual(self.p.writes, before + 1)
        self.assertEqual(plan[0]['repair_command'], f'limit {name} --set 9.00 --decrease-only')
        proposal, = A.limit_remedies(self.m, self.p, plan)
        self.assertEqual(A.apply_limit_remedy(self.m, self.p, plan, proposal)['status'], 'unresolved')
        self.assertEqual(self.p.writes, before + 2)
        self.assertEqual(self.m.ledger()['issued'][name]['ceiling_repair']['status'], 'observed-unmet')

    def test_guarded_remedy_uses_actual_parser_and_exact_entry_exception_tuple(self):
        name, handle, plan = self.held_limit_fixture()
        proposal, = A.limit_remedies(self.m, self.p, plan)
        for error in (OSError, ValueError, RuntimeError):
            with patch.object(self.m, 'cmd_limit', side_effect=error('synthetic')):
                self.assertEqual(A.apply_limit_remedy(self.m, self.p, plan, proposal)['status'], 'unresolved')
        with patch.object(self.m, 'cmd_limit', side_effect=LookupError('unexpected')):
            with self.assertRaises(LookupError): A.apply_limit_remedy(self.m, self.p, plan, proposal)
        argv = []; main = self.m.main
        def parsed():
            argv.append(list(self.m.sys.argv[1:])); return main()
        with patch.object(self.m, 'main', side_effect=parsed):
            self.assertEqual(A.apply_limit_remedy(self.m, self.p, plan, proposal)['status'], 'operator-applied')
        self.assertEqual(argv, [['limit', name, '--set', '10.00', '--decrease-only']])

    def test_unflagged_manual_guidance_is_not_guarded_authority(self):
        name = self.issue()
        plan = [{'name':name, 'limit':9, 'action':'in-place'},
                {'action':'over-limit', 'why':f'OVER LIMIT; it ends after: limit {name} --set 9'}]
        before = (self.p.calls, self.p.writes, copy.deepcopy(self.m.ledger()))
        self.assertEqual(A.limit_remedies(self.m, self.p, plan), [])
        self.assertEqual((self.p.calls, self.p.writes, self.m.ledger()), before)

    def test_actual_cli_prints_exact_command_and_proposal_has_no_effect(self):
        import json
        name = self.issue(); handle = self.m.ledger()['issued'][name]['hash']
        self.p.keys[handle]['limit'] = 123456.784
        policy = A.T._pol(1, limit=123456.78)
        path = self.m.paths.SCRATCH / 'guidance-policy.json'
        path.write_text(json.dumps(policy))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.m.cmd_issue_for_policy(str(path), 'example', None, False)
        self.assertIn(f'limit {name} --set 123456.78 --decrease-only', output.getvalue())
        plan = self.m.issue_for_policy(policy, 'example')
        before = (self.p.calls, self.p.writes, copy.deepcopy(self.p.keys), copy.deepcopy(self.m.ledger()))
        with patch.object(self.m, 'main', side_effect=AssertionError('proposal must not execute')):
            proposal, = A.limit_remedies(self.m, self.p, plan)
        self.assertEqual((self.p.calls, self.p.writes, self.p.keys, self.m.ledger()), before)
        duplicate = copy.deepcopy(plan); duplicate.append(copy.deepcopy(plan[0]))
        self.assertEqual(A.limit_remedies(self.m, self.p, duplicate), [])
        hidden = copy.deepcopy(plan); hidden[0]['why'] = 'no emitted command'
        self.assertEqual(A.limit_remedies(self.m, self.p, hidden), [])
        unflagged = copy.deepcopy(plan)
        unflagged[0]['repair_command'] = f'limit {name} --set 123456.78'
        unflagged[0]['why'] = f'; request one observed decrease: `{unflagged[0]["repair_command"]}`'
        self.assertEqual(A.limit_remedies(self.m, self.p, unflagged), [])

    def pending_fixture(self):
        name = 'project-a-prod-pending'
        self.m.save_ledger({'issued':{name:{'pending':True,'hash':None,'account':'example',
                          'destination':'vault:project-a/prod/PENDING','limit_usd':1}}})
        return name

    def test_pending_attestation_separate_absence_proof_and_required_journal(self):
        name=self.pending_fixture(); events=[]
        self.m._journal=lambda *args,**kwargs: events.append((args,kwargs))
        proof=A.pending_absence_attestation(self.m,self.p,name,{},complete=True)
        calls=self.p.calls
        result=A.apply_pending_attestation(self.m,self.p,name,{},proof,complete=True)
        self.assertEqual(result['status'],'operator-attested')
        self.assertNotIn(name,self.m.ledger()['issued']); self.assertEqual(self.p.calls,calls)
        self.assertEqual(events[0][0][0],'forget-pending-attested')
        self.assertTrue(events[0][1]['require_success'])

    def test_pending_attestation_refuses_live_unknown_holder_hash_and_changed_revision(self):
        name=self.pending_fixture()
        proof=A.pending_absence_attestation(self.m,self.p,name,{},complete=True)
        self.assertIsNone(A.pending_absence_attestation(self.m,self.p,name,{},complete=False))
        self.assertIsNone(A.pending_absence_attestation(self.m,self.p,name,{'worker':'known'},complete=True))
        self.p.keys['live']={'name':name,'hash':'live'}
        self.assertIsNone(A.pending_absence_attestation(self.m,self.p,name,{},complete=True))
        del self.p.keys['live']
        doc=self.m.ledger();doc['issued'][name]['limit_usd']=2;self.m.save_ledger(doc)
        self.assertEqual(A.apply_pending_attestation(self.m,self.p,name,{},proof,complete=True)['status'],'refused')
        doc['issued'][name]['hash']='known';self.m.save_ledger(doc)
        self.assertIsNone(A.pending_absence_attestation(self.m,self.p,name,{},complete=True))
        self.assertIn(name,self.m.ledger()['issued'])

    def test_pending_attestation_never_infers_absence_from_renamed_unknown_identity(self):
        name=self.pending_fixture()
        self.p.keys['unknown']={'hash':'unknown','name':'renamed-away','_proj':True,'limit':1}
        self.assertIsNone(A.pending_absence_attestation(self.m,self.p,name,{},complete=True))
        for ownership in (None, 'false', 0):
            self.p.keys['unknown']['_proj']=ownership
            self.assertIsNone(A.pending_absence_attestation(self.m,self.p,name,{},complete=True))
        self.assertIn(name,self.m.ledger()['issued'])

    def test_operator_admission_has_no_rng_or_provider_hook_and_binds_inventory(self):
        name=self.pending_fixture(); calls=self.p.calls; state=random.getstate()
        self.p.hook=(calls+1,lambda: self.fail('operator observation called the provider'))
        proof=A.pending_absence_attestation(self.m,self.p,name,{},complete=True)
        self.p.keys['foreign']={'hash':'foreign','name':'unrelated'}
        self.assertEqual(A.apply_pending_attestation(self.m,self.p,name,{},proof,complete=True)['status'],'refused')
        self.assertEqual(self.p.calls,calls);self.assertEqual(random.getstate(),state)
        self.assertIn(name,self.m.ledger()['issued'])
        del self.p.keys['foreign']
        self.assertEqual(A.apply_pending_attestation(self.m,self.p,name,{},proof,complete=True)['status'],'operator-attested')
        self.assertEqual(self.p.calls,calls)
        self.assertTrue(self.door['journal'][-2][1]['require_success'])

    def test_pending_attestation_journal_failure_and_missing_reason_preserve_row(self):
        name=self.pending_fixture();proof=A.pending_absence_attestation(self.m,self.p,name,{},complete=True)
        missing=dict(proof,reason='')
        self.assertEqual(A.apply_pending_attestation(self.m,self.p,name,{},missing,complete=True)['status'],'refused')
        self.m._journal=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('synthetic journal failure'))
        self.assertEqual(A.apply_pending_attestation(self.m,self.p,name,{},proof,complete=True)['status'],'unresolved')
        self.assertIn(name,self.m.ledger()['issued'])

    def test_unique_values_bind_real_hash_and_positive_rotation_reaches_delivery(self):
        first = self.p('/keys', 'synthetic', {'name': 'project-a-one', 'limit': 1, 'limit_reset': 'monthly'})
        second = self.p('/keys', 'synthetic', {'name': 'project-a-two', 'limit': 1, 'limit_reset': 'monthly'})
        self.assertNotEqual(first['key'], second['key'])
        for row in (first, second):
            self.assertEqual(row['data']['hash'], hashlib.sha256(row['key'].encode()).hexdigest())
        name = self.issue()
        result = self.m.rotate_one(name)
        self.assertEqual(result['rotations'], 1)

    def test_successor_is_explicitly_represented_after_delivery_failure(self):
        name = self.issue(); self.door['deliver'] = 'deliver'
        A.rotate(self.m, self.p, name, 1000)
        row = self.m.ledger()['issued'][name]
        self.assertIn('successor_hash', row)
        self.assertEqual(A.untracked(self.m, self.p), [])
        self.assertTrue(any(v.startswith('(a-rot)') for v in self.p.violations))

    def test_unknown_mint_reservation_covers_at_most_one_matching_successor(self):
        name = self.issue(); self.p.fail['mint'] = [1, '502-after']
        A.rotate(self.m, self.p, name, 1000)
        self.assertEqual(A.untracked(self.m, self.p), [])
        orphan = next(r for r in self.p.keys.values() if r['name'] == name + '-next')
        self.p.keys['unrelated-extra'] = copy.deepcopy(orphan)
        self.p.keys['unrelated-extra']['hash'] = 'unrelated-extra'
        self.assertEqual(len(A.untracked(self.m, self.p)), 1)

    def test_reservation_does_not_cover_wrong_name_or_excess_capacity(self):
        name = self.issue(); self.p.fail['mint'] = [1, '502-after']
        A.rotate(self.m, self.p, name, 1000)
        orphan = next(r for r in self.p.keys.values() if r['name'] == name + '-next')
        orphan['_asked'] = 11
        self.assertEqual(len(A.untracked(self.m, self.p)), 1)
        orphan['_asked'] = 10; orphan['name'] = 'foreign-unrepresented'
        self.assertEqual(len(A.untracked(self.m, self.p)), 1)

    def test_bad_identity_remains_a_negative_case(self):
        name = self.issue()
        original = self.p
        def wrong(*args, **kwargs):
            row = original(*args, **kwargs)
            if isinstance(row, dict) and 'key' in row: row['key'] = 'wrong-synthetic-plaintext'
            return row
        self.m._request = wrong
        self.m.deliver = lambda *a, **k: self.fail('unbound value delivered')
        with self.assertRaisesRegex(RuntimeError, 'identity'): self.m.rotate_one(name)
        self.assertEqual(A.untracked(self.m, self.p), [])

    def test_invalid_budget_seed_7000104_and_planted_mutation_remain_checked(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output): issues = A.case(7000104, trace=True)
        self.assertIn('invalid-input partition: expected rejection, state checked', output.getvalue())
        self.assertFalse(any(reason.startswith('(invalid)') for reason in issues))
        # This full rotation seed can still fail (a-rot); never suppress it.

        def mutation(*args, **kwargs):
            self.m.save_ledger({'issued': {'bad': {'pending': True}}})
            raise ValueError('invalid budget')
        self.m.issue_for_policy = mutation
        A.run(self.m, self.p, A.T._pol(4, limit=.03), {}, 3)
        self.assertTrue(any('(invalid)' in r and 'changed' in r for r in self.p.violations))

    def test_ledger_failure_cannot_hide_untracked_keys(self):
        self.m.ledger = lambda: (_ for _ in ()).throw(OSError('synthetic disk failure'))
        with self.assertRaises(RuntimeError): A.untracked(self.m, self.p)

    def test_held_rotation_is_not_mistaken_for_convergence(self):
        name = self.issue(); self.p.fail['delete'] = [1, True]
        A.rotate(self.m, self.p, name, 1000)
        self.assertTrue(any(v.startswith('(a-rot)') for v in self.p.violations))
        self.p.fail.clear()
        for _ in range(2):
            plan, error = A.run(self.m, self.p, self.pol, {}, 1000)
            self.assertFalse(A.serves_every_slot(plan, [row['ref'] for row in self.pol['credentialSources']]))
            self.assertTrue(any(r.get('action') == 'held-rotation' for r in plan))
        self.assertEqual(A.untracked(self.m, self.p), [])


if __name__ == '__main__': unittest.main()
