"""Synthetic receipts may not report unjudged money invariants as PASS."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ENGINE = Path(__file__).resolve().parents[1]
ROOT = ENGINE.parents[1]
sys.path.insert(0, str(ENGINE / 'tests'))
sys.path.insert(0, str(ENGINE / 'tools'))
import test_credential_doors as T


class Evidence(unittest.TestCase):
    def setUp(self):
        T.FAILURES.clear()
        guard = patch('socket.socket.connect', side_effect=AssertionError('network forbidden in synthetic checks'))
        guard.start()
        self.addCleanup(guard.stop)

    def test_named_environment_error_is_not_pass(self):
        out = io.StringIO()
        with patch.object(T, 'PROPERTY_REGRESSION_SEEDS', {1: 'synthetic'}), patch.object(T, '_prop_case', return_value=['ENVIRONMENT ENOSPC']), contextlib.redirect_stdout(out):
            T.test_or_policy_property_regression_seeds()
        self.assertTrue(T.FAILURES, out.getvalue())
        self.assertNotIn('PASS  seed', out.getvalue())

    def test_one_percent_unjudged_is_incomplete_not_pass(self):
        def case(seed):
            return ['ENVIRONMENT ENOSPC'] if seed == T.PROPERTY_SEED else []
        out = io.StringIO()
        with patch.object(T, 'PROPERTY_CASES', 100), patch.object(T, '_prop_case', side_effect=case), contextlib.redirect_stdout(out):
            T.test_or_policy_property()
        self.assertTrue(T.FAILURES, out.getvalue())
        self.assertNotIn('PASS  property', out.getvalue())

    def test_adversary_failure_and_crash_exit_nonzero_with_receipts(self):
        import adversarial_runner as runner
        for outcome in ('FAIL', 'ERROR', 'PASS'):
            def case(seed):
                if outcome == 'ERROR':
                    raise OSError('synthetic failure')
                return ['(a) planted violation'] if outcome == 'FAIL' else []
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = runner.main(case, ['one', '1', '2'], variant='fixture', source_paths=[__file__])
            report = json.loads(out.getvalue())
            self.assertEqual(report['status'], outcome)
            self.assertEqual(code, 0 if outcome == 'PASS' else 1)
            self.assertEqual(report['attempted'], 2)
            self.assertEqual(len(report['cases']), 2)
            self.assertTrue(report['sources'])
            self.assertTrue(report['source_commit'])

    def test_adversary_environment_error_is_incomplete(self):
        import adversarial_runner as runner
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = runner.main(lambda seed: ['ENVIRONMENT disk full'], ['5', '2'], variant='fixture', source_paths=[__file__])
        report = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(report['status'], 'ERROR')
        self.assertEqual(report['completed'], 0)

    def test_adversary_report_write_failure_is_not_success(self):
        import adversarial_runner as runner
        with tempfile.TemporaryDirectory() as temp:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = runner.main(lambda seed: [], ['1', '1', temp], variant='fixture', source_paths=[__file__])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out.getvalue())['status'], 'ERROR')

    def test_forget_attestation_failure_preserves_pending_row(self):
        m, provider = T._r3_door()
        m.save_ledger({'issued': {'pending': {'account': 'example', 'pending': True, 'hash': None}}})
        # Use the real door journal implementation with an isolated fake vault sink.
        original = T.orr()
        m._journal = original._journal
        import vault
        with patch.object(vault, 'journal', side_effect=OSError('synthetic journal failure')):
            with self.assertRaises(RuntimeError):
                m.forget_pending_key('pending', 'synthetic operator attestation')
        self.assertIn('pending', m.ledger()['issued'])
        self.assertFalse(provider.keys)

    def test_forget_attestation_precedes_clear_and_completion(self):
        m, _ = T._r3_door()
        m.save_ledger({'issued': {'pending': {'account': 'example', 'pending': True, 'hash': None}}})
        events = []
        def journal(event, secret, **details):
            events.append((event, 'pending' in m.ledger()['issued'], details))
        m._journal = journal
        m.forget_pending_key('pending', 'synthetic attestation')
        self.assertEqual([event[:2] for event in events], [('forget-pending-attested', True), ('forget-pending', False)])
        self.assertTrue(events[0][2]['require_success'])

    def test_rf_reports_observed_first_and_second_phase(self):
        import adv_repro
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = adv_repro.r_f()
        self.assertEqual(result['ledger_rows_after_first'], 1)
        self.assertEqual(result['keys_after_first'], 1)
        self.assertEqual(result['keys_after_retry'], 1)

    def test_invalid_policy_seed_7000104_is_rejected_without_mutation(self):
        import adv_norot_forget as adversary
        self.assertEqual(adversary.case(7000104), [])

    def test_invalid_input_partition_catches_state_mutation(self):
        import adv_norot_forget as adversary
        import random
        m, provider, _, _ = adversary.make_door(random.Random(1))
        policy = T._pol(4, limit=0.03)
        def bad(policy, account, **kw):
            m.save_ledger({'issued': {'unexpected': {'pending': True}}})
            raise ValueError('invalid budget')
        m.issue_for_policy = bad
        adversary.run(m, provider, policy, {}, 3)
        self.assertTrue(any('(invalid)' in reason and 'changed' in reason for reason in provider.violations))

    def test_forget_ledger_failure_leaves_only_attestation_and_pending_row(self):
        m, _ = T._r3_door()
        m.save_ledger({'issued': {'pending': {'account': 'example', 'pending': True, 'hash': None}}})
        events = []
        m._journal = lambda event, *args, **kwargs: events.append(event)
        with patch.object(m, 'save_ledger', side_effect=OSError('synthetic ledger failure')):
            with self.assertRaises(OSError):
                m.forget_pending_key('pending', 'synthetic attestation')
        self.assertIn('pending', m.ledger()['issued'])
        self.assertEqual(events, ['forget-pending-attested'])

    def test_split_report_cannot_turn_empty_error_receipt_into_success(self):
        import os
        import runpy
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / 'receipt.json'
            receipt.write_text(json.dumps({'schema': 'synthetic-adversary/1', 'status': 'ERROR',
                                           'requested': 2, 'completed': 0, 'cases': []}))
            with patch.object(sys, 'argv', ['adv_split.py', str(receipt)]), patch.dict(os.environ, {'ADV_MOD': 'adv_norot_forget'}), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as stopped:
                    runpy.run_path(str(ENGINE / 'tests/adv_split.py'), run_name='__main__')
            self.assertEqual(stopped.exception.code, 1)


if __name__ == '__main__':
    unittest.main()
