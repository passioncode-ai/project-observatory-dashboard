"""Conditional arithmetic model only; no production reconciliation is enabled."""
import unittest
from decimal import Decimal
import rotation_capacity_model as M


class CapacityModel(unittest.TestCase):
    def plan(self, **kw):
        args = dict(policy='40', other='0', predecessor='40', successor='20',
                    availability_admitted=True, bounded_writes=True,
                    current_reads=True, closed_world=True)
        args.update(kw)
        return M.propose(**args)

    def test_existing_ledger_has_no_policy_origin_binding(self):
        # Compare actual door outputs; a folder/project is not policy provenance.
        import random
        from unittest.mock import patch
        import adv_j1_property as A
        with patch('socket.socket.connect',side_effect=AssertionError('synthetic only')):
            first, _, _, _ = A.make_door(random.Random(7))
            first.issue_for_policy(A.T._pol(1,limit=10),'example')
            name,row=next(iter(first.ledger()['issued'].items()))
            second, _, _, _ = A.make_door(random.Random(7))
            second.issue_key(name,row['limit_usd'],row['account'],row['destination'],row['project'],row['limit_reset'])
            self.assertEqual(row,second.ledger()['issued'][name])

    def test_larger_successor_keeps_prior_upper_until_restore_is_confirmed(self):
        plan=self.plan(policy='100',predecessor='20',successor='50')
        self.assertEqual(M.simulate(plan)['status'],'conditional-completion')
        for boundary in ('after-intent','before-effect','after-effect'):
            result=M.simulate(plan,fault=('restore',boundary))
            self.assertEqual(result['status'],'interrupted')
            self.assertEqual(result['final']['successor_upper'],'50')
            self.assertTrue(all(r['within_bound'] and r['represented'] for r in result['trace']))

    def test_success_preserves_strict_bound_at_every_boundary(self):
        result=M.simulate(self.plan())
        self.assertEqual(result['status'],'conditional-completion')
        self.assertEqual(result['final']['actual_total'],'40')
        self.assertEqual(result['final']['consumer'],'successor')
        self.assertFalse(result['actionable'])
        self.assertTrue(all(r['within_bound'] and r['represented'] for r in result['trace']))

    def test_every_effect_receipt_failure_retains_safe_capacity_and_stops(self):
        for stage in M.STAGES:
            for boundary in M.BOUNDARIES:
                with self.subTest(stage=stage,boundary=boundary):
                    result=M.simulate(self.plan(),fault=(stage,boundary))
                    self.assertEqual(result['status'],'interrupted')
                    self.assertFalse(result['actionable'])
                    self.assertTrue(all(r['within_bound'] and r['represented'] for r in result['trace']))
                    self.assertEqual(result['fault'],[stage,boundary])

    def test_unknown_mint_retains_reservation_without_identity_or_value(self):
        result=M.simulate(self.plan(),fault=('mint','after-effect'))
        row=result['final']
        self.assertEqual(row['upper_total'],'40')
        self.assertEqual(row['actual_total'],'40')
        self.assertFalse(row['successor_identified'])
        self.assertFalse(row['value_available'])
        self.assertIn('recover-identity-and-one-time-value',result['missing'])
        rollback_total = Decimal(row['actual_total']) - Decimal(row['predecessor_actual']) + Decimal('40')
        self.assertGreater(rollback_total, Decimal(row['original_bound']))

    def test_unknown_delivery_never_suggests_successor_deletion(self):
        result=M.simulate(self.plan(),fault=('deliver','after-effect'))
        self.assertEqual(result['final']['consumer'],'successor')
        self.assertFalse(result['final']['delivery_confirmed'])
        self.assertEqual(result['next'],'read-current-evidence')
        self.assertFalse(result['actionable'])

    def test_unknown_delete_keeps_capacity_until_authoritative_absence(self):
        result=M.simulate(self.plan(),fault=('delete','after-effect'))
        self.assertEqual(result['final']['actual_total'],'20')
        self.assertEqual(result['final']['upper_total'],'40')
        self.assertEqual(result['next'], 'read-current-evidence')

    def test_assumptions_are_required_and_not_provider_guarantees(self):
        for flag in ('availability_admitted','bounded_writes','current_reads','closed_world'):
            for value in (False,None,1,'true'):
                with self.subTest(flag=flag,value=value):
                    p=self.plan(**{flag:value})
                    self.assertEqual(p['status'],'blocked')
                    self.assertFalse(p['actionable'])
                    self.assertEqual(M.simulate(p)['trace'],[])

    def test_invalid_partition_and_valid_rotation_ceiling_are_distinct(self):
        self.assertFalse(M.valid_policy('0.03',4))
        self.assertTrue(M.valid_policy('0.03',3))
        # The full original seed's 0.034 actual cap is not rounded into 0.03.
        p=self.plan(policy='0.03',other='0.02',predecessor='0.014',successor='0.01')
        self.assertEqual(p['status'],'blocked')
        self.assertIn('already-over-policy',p['missing'])
        self.assertEqual(p['original_bound'],'0.034')

    def test_minimum_cent_prevents_splitting_a_one_cent_predecessor(self):
        p=self.plan(policy='0.03',other='0.02',predecessor='0.01',successor='0.01')
        self.assertEqual(p['status'],'blocked')
        self.assertIn('no-positive-cent-predecessor-capacity',p['missing'])

    def test_headroom_can_preserve_availability_without_lowering(self):
        p=self.plan(policy='60',predecessor='40',successor='20',availability_admitted=False)
        self.assertEqual(p['status'],'conditional-proposal')
        self.assertEqual(p['lower_to'],'40')
        self.assertEqual(M.simulate(p)['status'],'conditional-completion')

    def test_unknown_and_invalid_numbers_never_become_zero(self):
        for value in (None,True,-1,float('inf'),'NaN','-1','1e99999','not-number'):
            with self.subTest(value=value):
                p=self.plan(predecessor=value)
                self.assertEqual(p['status'],'blocked')
                self.assertIsNone(p['original_bound'])

    def test_missing_holder_or_lost_value_blocks_cleanup(self):
        for option in ('holders_complete','old_unused','value_preserved'):
            result=M.simulate(self.plan(),**{option:False})
            self.assertEqual(result['status'],'blocked')
            self.assertNotEqual(result['final']['predecessor_actual'],'0')
            self.assertTrue(all(r['within_bound'] for r in result['trace']))

    def test_capacity_transfer_can_interrupt_a_consumer_already_above_reduced_cap(self):
        result=M.simulate(self.plan(),predecessor_usage='35')
        self.assertTrue(any(r['predecessor_unserviceable'] for r in result['trace']))
        self.assertEqual(result['status'],'conditional-completion')

    def test_bounded_numerical_domain_keeps_all_fault_boundaries(self):
        count=0
        for cap in ('0.03','1','40'):
            for other in ('0','0.01'):
                for successor in ('0.01','0.02'):
                    old=Decimal(cap)-Decimal(other)
                    p=self.plan(policy=cap,other=other,predecessor=str(old),successor=successor)
                    if p['status']=='blocked': continue
                    for stage in M.STAGES:
                        for boundary in M.BOUNDARIES:
                            result=M.simulate(p,fault=(stage,boundary))
                            self.assertEqual(result['status'],'interrupted')
                            self.assertTrue(all(r['within_bound'] and r['represented'] for r in result['trace']))
                            count+=1
        self.assertEqual(count,275)

    def test_external_writer_and_unbounded_echo_are_explicit_countermodels(self):
        for args in ({'external_added':'1'},{'mint_overshoot':'0.001'}):
            result=M.simulate(self.plan(),**args)
            self.assertEqual(result['status'],'assumption-violation')
            self.assertTrue(any(not r['within_bound'] for r in result['trace']))
            self.assertFalse(result['actionable'])


if __name__=='__main__': unittest.main()
