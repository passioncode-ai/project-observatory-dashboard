"""Bounded saved coverage reads never authorize cleanup or refresh producers."""
import contextlib
import io
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from test_machine import Base


class CoverageView(Base):
    def put(self, name, **extra):
        doc = {'schema_version': 1, 'kind': name, 'attempted_at': datetime.now(timezone.utc).isoformat(),
               'partial': False, 'degraded': [], 'candidates': [], 'locations': [], 'commands': [], **extra}
        (self.home / 'store/raw' / (name + '.json')).write_text(json.dumps(doc))
        return doc

    def test_missing_and_explicit_empty_are_distinct(self):
        import coverage_view as view
        out = view.summary()['checkouts']
        self.assertIsNone(out['snapshot_count'])
        self.assertTrue(out['partial'])
        self.put('checkout-candidates')
        out = view.summary()['checkouts']
        self.assertEqual(out['snapshot_count'], 0)
        self.assertEqual(out['current_snapshot_count'], 0)
        self.assertIsNone(out['reclaimable_bytes'])
        self.assertFalse(out['actionable'])

    def test_old_future_and_unknown_times_never_claim_current_counts(self):
        import coverage_view as view
        for stamp in ('2020-01-01T00:00:00Z', '2099-01-01T00:00:00Z', None):
            self.put('cache-coverage', attempted_at=stamp)
            out = view.summary()['caches']
            self.assertEqual(out['snapshot_count'], 0)
            self.assertIsNone(out['current_snapshot_count'])
            self.assertTrue(out['degraded'])

    def test_malformed_rows_preserve_peers_and_samples_are_bounded(self):
        import coverage_view as view
        stamp = datetime.now(timezone.utc).isoformat()
        rows = [{'path': '/synthetic/' + str(i), 'stale': False, 'measured_at': stamp,
                 'classification': 'clone', 'argv': ['PRIVATE_ARGUMENT']} for i in range(15)]
        self.put('checkout-candidates', candidates=[None, *rows])
        out = view.summary()['checkouts']
        self.assertEqual(out['snapshot_count'], 15)
        self.assertEqual(len(out['rows']), 10)
        self.assertEqual(out['invalid_rows'], 1)
        self.assertIsNone(out['current_snapshot_count'])
        self.assertNotIn('PRIVATE_ARGUMENT', json.dumps(out))
        self.assertTrue(all(not r['actionable'] for r in out['rows']))

    def test_partial_and_carried_rows_never_become_current_total(self):
        import coverage_view as view
        stamp = datetime.now(timezone.utc).isoformat()
        self.put('cache-coverage', partial=True, locations=[{'path': '/cache', 'measured_at': stamp, 'stale': False}])
        self.assertIsNone(view.summary()['caches']['current_snapshot_count'])
        self.put('checkout-candidates', candidates=[{'path': '/repo', 'measured_at': '2020-01-01T00:00:00Z', 'stale': False}])
        self.assertIsNone(view.summary()['checkouts']['current_snapshot_count'])

    def test_caps_and_nonregular_sources_are_unknown(self):
        import coverage_view as view
        import os
        target = self.home / 'store/raw/checkout-candidates.json'
        target.write_bytes(b' ' * (view.MAX_BYTES + 1))
        self.assertIsNone(view.summary()['checkouts']['snapshot_count'])
        target.unlink()
        if hasattr(os, 'mkfifo'):
            os.mkfifo(target)
            self.assertIsNone(view.summary()['checkouts']['snapshot_count'])
            target.unlink()
        self.put('checkout-candidates', candidates=[{'path': '/repo'}] * (view.MAX_ROWS + 1))
        self.assertTrue(view.summary()['checkouts']['partial'])

    def test_invalid_collection_and_deep_json_are_unknown(self):
        import coverage_view as view
        self.put('checkout-candidates', candidates=None)
        out = view.summary()['checkouts']
        self.assertIsNone(out['snapshot_count'])
        self.assertTrue(out['degraded'])
        (self.home / 'store/raw/checkout-candidates.json').write_text('[' * 2000 + ']' * 2000)
        self.assertIsNone(view.summary()['checkouts']['snapshot_count'])

    def test_cli_does_not_run_collectors(self):
        import workspace, scan_machine, cache_coverage, checkout_inventory
        with patch.object(scan_machine, 'main', side_effect=AssertionError('survey')), patch.object(cache_coverage, 'collect', side_effect=AssertionError('cache')), patch.object(checkout_inventory, 'scan', side_effect=AssertionError('checkout')), contextlib.redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(workspace.machine_command('machine', ['--coverage']), 0)
        out = json.loads(buf.getvalue())
        self.assertIn('checkouts', out)
        self.assertIn('caches', out)

    def test_machine_view_and_actual_mcp_enum_expose_coverage(self):
        import machine_view, importlib, sys
        from pathlib import Path
        sys.path.insert(0, str(Path(machine_view.__file__).parent / 'mcp'))
        import server
        server = importlib.reload(server)
        import typing
        hint = typing.get_type_hints(server.observatory_machine, include_extras=True)['section']
        self.assertIn('coverage', typing.get_args(typing.get_args(hint)[0]))
        tool = server.server._tool_manager.get_tool('observatory_machine')
        self.assertIn('coverage', tool.parameters['properties']['section']['enum'])
        self.assertIn('coverage', server.observatory_machine()['sections'])
        out = server.observatory_machine(section='coverage')
        # An empty workspace has surveyed nothing: `degraded` names every source, because the
        # server's own contract reads an empty list as full coverage.
        named = {(d['source'], d['code']) for d in out['degraded']}
        self.assertIn(('checkout-candidates.json', 'not-surveyed'), named)
        self.assertIn(('cache-coverage.json', 'not-surveyed'), named)
        self.assertIn(('open-unlinked.json', 'not-surveyed'), named)
        self.assertTrue(all(d['reason'] and '`project-observatory full machine' in d['reason'] for d in out['degraded']))
        self.assertIn('checkouts', out['coverage'])
        self.assertIn('caches', out['coverage'])
        import jsonschema
        jsonschema.validate(out, tool.fn_metadata.output_schema)
        out['coverage']['actionable'] = True
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(out, tool.fn_metadata.output_schema)


    def test_the_overview_names_coverage_it_did_not_measure(self):
        # Every machine receipt current and clean: the overview's `degraded` must still name the
        # coverage sources it carries unmeasured, once each (an empty list reads as full coverage).
        import importlib
        import machine_view
        import memory_snapshot
        import server
        server = importlib.reload(server)
        stamp = '2026-10-19T10:00:00Z'
        docs = {'machine.json': {'measured_at': stamp, 'memory': {'degraded': []},
                                 'processes': {'degraded': []}, 'disk': {'degraded': []}},
                'git-hygiene.json': {'measured_at': stamp, 'checkouts': [], 'degraded': []},
                'cleanup-plan.json': {'planned_at': stamp}, 'disk-guard.json': {'at': stamp, 'degraded': []}}
        fresh = {'memory': {'degraded': []}, 'processes': {'degraded': []}}
        with patch.object(machine_view, '_load', side_effect=lambda name, *_: (docs[name], None)), \
                patch.object(memory_snapshot, 'read_sources', return_value=fresh), \
                patch.object(machine_view, 'guard_summaries', return_value=[]), \
                patch.object(machine_view, 'journal', return_value=[]):
            out = server.observatory_machine(section='overview')
        named = [(d['source'], d['code']) for d in out['degraded']]
        for source in ('checkout-candidates.json', 'cache-coverage.json', 'open-unlinked.json'):
            self.assertIn((source, 'not-surveyed'), named)
        self.assertIn(('open-unlinked.json', 'visibility-limited'), named)
        self.assertEqual(len(named), len(set(named)), 'each source once')
        self.assertEqual(out['coverage']['degraded'], [d for d in out['degraded'] if d in out['coverage']['degraded']])

if __name__ == '__main__':
    import unittest
    unittest.main()
