"""Cache declarations are not ownership or reclamation proof."""
import contextlib
import io
import json
from pathlib import Path
from unittest.mock import patch
from test_machine import Base


class CacheCoverage(Base):
    def setup_sources(self, locations, commands=None):
        (self.home / 'config/machine.json').write_text(json.dumps({'locations': locations}))
        (self.home / 'config/cleanup.json').write_text(json.dumps({'guard': {'cache_roots': [], 'commands': commands or []}}))

    def test_root_presence_and_carried_zero_size_do_not_imply_reclamation(self):
        import cache_coverage as coverage
        root = self.data / 'cache'; root.mkdir()
        missing = self.data / 'absent'
        self.setup_sources([{'path': str(root), 'kind': 'cache', 'reclaim': 'regenerable'}, {'path': str(missing), 'kind': 'cache'}])
        stamp = '2020-01-01T00:00:00Z'
        (self.home / 'store/raw/machine.json').write_text(json.dumps({'disk': {'locations': [{'path': str(root), 'gb': 0, 'measured_at': stamp, 'frees_at_least_gb': 99}]}}))
        with patch.object(coverage.os, 'scandir', side_effect=AssertionError('must not walk')), patch.object(coverage.subprocess, 'run', side_effect=AssertionError('must not execute')):
            result = coverage.collect()
        rows = {r['path']: r for r in result['locations']}
        self.assertTrue(rows[str(root)]['presence']['present'])
        self.assertFalse(rows[str(missing)]['presence']['present'])
        self.assertEqual(rows[str(root)]['size']['reported_gb'], 0)
        self.assertEqual(rows[str(root)]['size']['measured_at'], stamp)
        self.assertIsNone(rows[str(root)]['size']['complete'])
        self.assertIsNone(rows[str(root)]['reclaimable_bytes'])
        self.assertIsNone(rows[str(root)]['owner'])
        self.assertFalse(rows[str(root)]['regeneration']['verified'])

    def test_protected_root_is_not_statted_and_symlink_is_unknown(self):
        import cache_coverage as coverage
        protected = self.base / 'Library/Containers/example'
        with patch.object(Path, 'lstat', side_effect=AssertionError('protected metadata')):
            result = coverage.presence(protected)
        self.assertIsNone(result['present'])
        self.assertEqual(result['reason'], 'privacy-guarded')
        link = self.data / 'link'; link.symlink_to(self.data, target_is_directory=True)
        self.assertEqual(coverage.presence(link)['reason'], 'symlink-not-probed')

    def test_tool_availability_does_not_execute_configured_command_or_report_zero_bytes(self):
        import cache_coverage as coverage
        self.setup_sources([], [{'argv': ['synthetic-tool', 'purge', 'PRIVATE_ARGUMENT']}])
        with patch.object(coverage.shutil, 'which', return_value='/synthetic/tool'), patch.object(coverage.subprocess, 'run', side_effect=AssertionError('no commands')):
            result = coverage.collect()
        row = result['commands'][0]
        self.assertTrue(row['availability']['available'])
        self.assertIsNone(row['reclaimable_bytes'])
        self.assertEqual(row['active_use'], 'unknown')
        self.assertNotIn('PRIVATE_ARGUMENT', json.dumps(result))

    def test_budget_resume_and_source_failure_preserve_prior_unknowns(self):
        import cache_coverage as coverage
        roots = [self.data / str(i) for i in range(3)]
        for root in roots: root.mkdir()
        self.setup_sources([{'path': str(root), 'kind': 'cache'} for root in roots])
        first = coverage.collect(max_rows=1)
        second = coverage.collect(max_rows=1)
        self.assertNotEqual(first['cursor'], second['cursor'])
        self.assertTrue(second['partial'])
        (self.home / 'config/machine.json').write_text('{bad')
        failed = coverage.collect(seconds=0)
        self.assertTrue(failed['partial'])
        self.assertTrue(failed['degraded'])
        self.assertTrue(any(r['stale'] for r in failed['locations']))

    def test_denied_invalid_sources_and_sensitive_command_values_stay_unknown(self):
        import cache_coverage as coverage
        path = self.data / 'denied'
        with patch.object(Path, 'lstat', side_effect=PermissionError('PRIVATE BODY')):
            observed = coverage.presence(path)
        self.assertIsNone(observed['present'])
        self.assertEqual(observed['reason'], 'metadata-unavailable')
        self.setup_sources([None, {'path': 'bad\0path', 'kind': 'cache'}], [{'argv': ['sk-PRIVATEVALUE']}, None])
        result = coverage.collect()
        self.assertTrue(result['degraded'])
        self.assertTrue(all(r['availability']['available'] is None for r in result['commands']))
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_failed_size_source_keeps_prior_measurement_time_and_does_not_reprobe_lost_policy(self):
        import cache_coverage as coverage
        root = self.data / 'cache'; root.mkdir()
        self.setup_sources([{'path': str(root), 'kind': 'cache'}])
        survey = self.home / 'store/raw/machine.json'
        survey.write_text(json.dumps({'disk': {'locations': [{'path': str(root), 'gb': 2, 'measured_at': '2020-01-01T00:00:00Z'}]}}))
        coverage.collect()
        survey.write_text('{bad')
        carried = coverage.collect()['locations'][0]['size']
        self.assertEqual(carried['reported_gb'], 2)
        self.assertEqual(carried['measured_at'], '2020-01-01T00:00:00Z')
        (self.home / 'config/machine.json').write_text('{bad')
        with patch.object(coverage, 'presence', side_effect=AssertionError('lost declaration must not probe')):
            failed = coverage.collect()
        self.assertTrue(failed['locations'][0]['stale'])

    def test_invalid_nested_configuration_preserves_prior_rows_and_marks_degraded(self):
        import cache_coverage as coverage
        root = self.data / 'cache'; root.mkdir()
        self.setup_sources([{'path': str(root), 'kind': 'cache'}], [{'argv': ['synthetic-tool']}])
        coverage.collect()
        (self.home / 'config/machine.json').write_text(json.dumps({'locations': None}))
        (self.home / 'config/cleanup.json').write_text(json.dumps({'guard': []}))
        with patch.object(coverage, 'presence', side_effect=AssertionError('invalid declaration')), patch.object(coverage, 'availability', side_effect=AssertionError('invalid declaration')):
            result = coverage.collect()
        self.assertEqual(len(result['locations']), 1)
        self.assertEqual(len(result['commands']), 1)
        self.assertTrue(result['locations'][0]['stale'])
        self.assertTrue(result['commands'][0]['stale'])
        self.assertTrue(result['degraded'])

    def test_fifo_source_is_refused_without_blocking(self):
        import os, subprocess, sys
        import cache_coverage as coverage
        if not hasattr(os, 'mkfifo'): self.skipTest('POSIX FIFO')
        self.setup_sources([])
        source = self.home / 'config/machine.json'
        source.unlink(); os.mkfifo(source)
        script = "import cache_coverage,json; print(json.dumps(cache_coverage.collect(seconds=.01)))"
        result = subprocess.run([sys.executable, '-B', '-c', script], cwd=coverage.ROOT,
                                capture_output=True, text=True, timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(result.stdout)
        self.assertFalse(doc['measurement_complete'])
        self.assertTrue(any(r['source'] == 'config/machine.json' for r in doc['degraded']))

    def test_missing_schema_differs_from_explicit_empty_declarations(self):
        import cache_coverage as coverage
        self.setup_sources([])
        self.assertTrue(coverage.collect()['measurement_complete'])
        for name in ('machine.json', 'cleanup.json'):
            (self.home / 'config' / name).write_text('{}')
        result = coverage.collect()
        self.assertFalse(result['measurement_complete'])
        self.assertTrue(result['degraded'])
        self.setup_sources([])
        (self.home / 'config/cleanup.json').write_text('{"guard": {}}')
        self.assertFalse(coverage.collect()['measurement_complete'])

    def test_symlink_and_malformed_prior_rows_preserve_valid_peers(self):
        import cache_coverage as coverage
        root = self.data / 'cache'; root.mkdir()
        self.setup_sources([{'path': str(root), 'kind': 'cache'}])
        original = coverage.collect()
        original['locations'].append({'id': 'location:bad', 'declaration_sources': None})
        (self.home / 'store/raw/cache-coverage.json').write_text(json.dumps(original))
        source = self.home / 'config/machine.json'; source.unlink()
        target = self.data / 'foreign.json'; target.write_text('{"locations": []}')
        source.symlink_to(target)
        with patch.object(coverage, 'presence', side_effect=AssertionError('lost policy')):
            result = coverage.collect()
        self.assertEqual(len(result['locations']), 1)
        self.assertFalse(result['locations'][0]['declaration_current'])
        self.assertFalse(result['measurement_complete'])
        self.assertTrue(any(r['source'] == 'previous-cache-coverage' for r in result['degraded']))

    def test_cli_and_uncovered_system_areas_remain_read_only(self):
        import cache_coverage as coverage, workspace
        self.setup_sources([])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(workspace.machine_command('machine', ['--cache-coverage']), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['kind'], 'cache-coverage')
        self.assertFalse(result['coverage_complete'])
        self.assertTrue(result['uncovered'])
        self.assertTrue(all(r['presence']['present'] is None for r in result['uncovered']))


if __name__ == '__main__':
    import unittest
    unittest.main()
