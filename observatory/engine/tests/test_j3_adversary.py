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
        self.m.issue_for_policy(self.pol, 'example')
        return next(iter(self.m.ledger()['issued']))

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
