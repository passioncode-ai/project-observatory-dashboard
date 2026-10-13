"""Synthetic issued-key activity, never live provider credentials or effects."""
import contextlib
import importlib.util
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from unittest.mock import patch
from test_machine import Base


class IssuedSpend(Base):
    def setUp(self):
        super().setUp()
        path = Path(__file__).resolve().parents[1] / 'tools/openrouter.py'
        spec = importlib.util.spec_from_file_location('spend_test_door', path)
        self.m = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.m)
        self.day = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
        self.admin = 'SYNTHETIC_ADMIN_VALUE'
        self.handle = 'a' * 64
        self.doc = {'issued': {'example-agent': {'account': 'example', 'hash': self.handle}}}
        self.m.ledger = lambda: self.doc
        self.m.read_admin = lambda label: (label, self.admin)
        self.row = {'date': self.day, 'model': 'vendor/model', 'usage': 0.1,
                    'requests': 1, 'prompt_tokens': 2, 'completion_tokens': 3, 'reasoning_tokens': 0}

    def invoke(self, rows, day=None, as_json=True):
        out = io.StringIO()
        with patch.object(self.m, '_request', return_value={'data': rows}) as request, contextlib.redirect_stdout(out):
            result = self.m.cmd_spend('example-agent', day, as_json)
        return result, out.getvalue(), request

    def test_positive_aggregation_and_empty_zero(self):
        _, text, request = self.invoke([self.row, {**self.row, 'usage': .2}], self.day)
        doc = json.loads(text)
        self.assertEqual(doc['totalUsd'], .3)
        self.assertEqual(len(doc['rows']), 1)
        self.assertEqual(doc['rows'][0]['requests'], 2)
        self.assertIn('api_key_hash=' + self.handle, request.call_args.args[0])
        self.assertNotIn(self.handle, text)
        self.assertNotIn(self.admin, text)
        self.assertEqual(json.loads(self.invoke([])[1])['totalUsd'], 0)

    def test_missing_invalid_usage_never_becomes_zero(self):
        for value in (None, True, -1, float('nan'), float('inf'), '0.1', {}, []):
            with self.subTest(value=type(value).__name__), self.assertRaises(RuntimeError):
                self.invoke([{**self.row, 'usage': value}])
        with self.assertRaises(RuntimeError): self.invoke([None])

    def test_unknown_counters_are_null_and_bad_counts_refused(self):
        row = dict(self.row); row.pop('prompt_tokens')
        doc = json.loads(self.invoke([row, self.row])[1])
        self.assertIsNone(doc['rows'][0]['prompt_tokens'])
        for value in (True, -1, 1.5, '1'):
            with self.assertRaises(RuntimeError): self.invoke([{**self.row, 'requests': value}])

    def test_bad_dates_and_hash_refused_before_read(self):
        for value in ('2026-02-30', '2020-01-01', datetime.now(timezone.utc).date().isoformat(), '2099-01-01'):
            with patch.object(self.m, '_request', side_effect=AssertionError('provider called')):
                with self.assertRaises(ValueError): self.m.cmd_spend('example-agent', value, True)
        self.doc['issued']['example-agent']['hash'] = 'invalid&date=2099-01-01'
        with patch.object(self.m, '_request', side_effect=AssertionError('provider called')):
            with self.assertRaises((ValueError, LookupError)): self.m.cmd_spend('example-agent', None, True)

    def test_malformed_rows_and_provider_echoes_are_refused(self):
        for changes in ({'date': 'not-a-date'}, {'date': '2099-01-01'}, {'model': None},
                        {'model': 'vendor/' + self.handle}, {'model': 'vendor/' + self.handle.upper()}, {'model': 'vendor/' + self.admin},
                        {'model': 'bad\nmodel'}, {'usage': self.admin}):
            with self.assertRaises(RuntimeError): self.invoke([{**self.row, **changes}])
        text = self.invoke([{**self.row, 'key': self.admin, 'api_key_hash': self.handle}])[1]
        self.assertNotIn(self.admin, text); self.assertNotIn(self.handle, text)

    def test_identity_change_and_unresolved_rotation_are_refused(self):
        def changed(*args, **kwargs):
            self.doc['issued']['example-agent']['hash'] = 'b' * 64
            return {'data': [self.row]}
        with patch.object(self.m, '_request', side_effect=changed), self.assertRaises(RuntimeError):
            self.m.cmd_spend('example-agent', None, True)
        self.doc['issued']['example-agent']['rotation_status'] = 'delivery-unknown'
        with patch.object(self.m, '_request', side_effect=AssertionError('provider called')), self.assertRaises(RuntimeError):
            self.m.cmd_spend('example-agent', None, True)

    def test_rows_and_transport_bytes_are_bounded(self):
        with self.assertRaises(RuntimeError):
            self.invoke([self.row] * (self.m.ACTIVITY_MAX_ROWS + 1))
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(inner, count=None):
                self.assertEqual(count, self.m.ACTIVITY_MAX_BYTES + 1)
                return b' ' * count
        with patch.object(self.m.urllib.request, 'urlopen', return_value=Response()) as request:
            with self.assertRaises(RuntimeError): self.m.read_activity(self.admin, self.handle)
        req = request.call_args.args[0]
        self.assertEqual(req.get_method(), 'GET')
        self.assertEqual(request.call_args.kwargs['timeout'], 30)

    def test_scope_mismatch_aggregate_overflow_and_malformed_receipt_are_unknown(self):
        earlier = (datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat()
        with self.assertRaises(RuntimeError): self.invoke([{**self.row, 'date': earlier}], self.day)
        with self.assertRaises(RuntimeError): self.invoke([{**self.row, 'usage': 1e308}] * 2)
        for doc in (None, [], {}, {'data': None}):
            with patch.object(self.m, '_request', return_value=doc), self.assertRaises(RuntimeError):
                self.m.cmd_spend('example-agent', None, True)

    def test_text_unknown_counts_and_parser_positive_are_read_only(self):
        import copy
        before = copy.deepcopy(self.doc)
        row = {**self.row, 'requests': None, 'usage': 0}
        with patch.object(self.m, 'save_ledger', side_effect=AssertionError('ledger write')), patch.object(self.m, 'deliver', side_effect=AssertionError('delivery')), patch.object(self.m, '_journal', side_effect=AssertionError('journal')):
            text = self.invoke([row], as_json=False)[1]
        self.assertIn('unknown req', text)
        self.assertIn('retired keys excluded', text)
        self.assertEqual(before, self.doc)
        out = io.StringIO()
        with patch.object(sys, 'argv', ['openrouter.py', 'spend', 'example-agent', '--date', self.day, '--json']), patch.object(self.m, '_request', return_value={'data': [self.row]}), contextlib.redirect_stdout(out):
            self.assertEqual(self.m.main(), 0)
        doc = json.loads(out.getvalue())
        self.assertEqual(doc['scope'], 'current-issued-key-only')
        self.assertFalse(doc['retiredKeysIncluded'])

    def test_parser_refusal_withholds_error_body_and_figure(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['openrouter.py', 'spend', 'example-agent', '--json']), patch.object(self.m, '_request', side_effect=RuntimeError(self.admin + self.handle)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(self.m.main(), 1)
        self.assertEqual(out.getvalue(), '')
        self.assertNotIn(self.admin, err.getvalue()); self.assertNotIn(self.handle, err.getvalue())


if __name__ == '__main__':
    import unittest
    unittest.main()
