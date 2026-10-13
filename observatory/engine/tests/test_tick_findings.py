"""Tick evidence stays distinct from disabled, unplanned and interrupted work."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]

class TickFindings(unittest.TestCase):
    def test_planned_starvation_excludes_disabled_and_unplanned(self):
        import tick_findings as F
        rows = {name: {'not_started_streak': 3, 'last_ran_at': 1, 'outcome': outcome}
                for name, outcome in [('leaks', 'not_started'), ('scan-gh', 'disabled'), ('remotes', 'not_started')]}
        found = F.findings({'collector_order': ['leaks', 'scan-gh']}, {'schema': 2, 'collectors': rows}, now=36000)
        self.assertEqual([(f['type'], f['subject'], f['severity']) for f in found],
                         [('tick.collector_starved', 'collector:leaks', 'critical')])

    def test_unknown_schema_and_interrupted_never_imply_healthy(self):
        import tick_findings as F
        for doc in (None, {'schema': 3}, {'schema': 1}, [], {'schema': 2, 'collectors': []}):
            self.assertIn('tick.rotation_degraded', {f['type'] for f in F.findings({'collector_order': ['leaks']}, doc)})
        found = F.findings({'collector_order': ['leaks']}, {'schema': 2, 'collectors': {'leaks': {
            'started_at': 2, 'finished_at': None, 'outcome': None, 'last_ran_at': 1}}}, now=36000)
        self.assertIn('interrupted', found[0]['detail'])

    def test_unknown_completion_and_malformed_numbers_are_not_healthy(self):
        import tick_findings as F
        for row in ({'started_at': 5, 'finished_at': None, 'outcome': None, 'last_ran_at': None, 'not_started_streak': 0},
                    {'outcome': 'ran', 'last_ran_at': 'invalid', 'not_started_streak': 'invalid'}):
            with self.subTest(row=row):
                found = F.findings({'collector_order': ['leaks'], 'finished_at': '2026-01-01T00:00:00Z'},
                                   {'schema': 2, 'collectors': {'leaks': row}}, now=10)
                self.assertIn('tick.rotation_degraded', {f['type'] for f in found})
                self.assertNotIn('tick.collector_starved', {f['type'] for f in found})

    def test_composed_unknown_reasons_preserve_one_id_and_rebuild(self):
        import contextlib
        import io
        from unittest.mock import patch
        import tick_findings as F
        import paths
        with patch.object(paths, "config_file", side_effect=lambda name: ROOT / "defaults" / name):
            import build_findings as B
        found = F.findings({'collector_order': ['interrupted', 'malformed', 'leaks']},
                           {'schema': 2, 'collectors': {
                               'interrupted': {'outcome': None, 'last_ran_at': None,
                                               'finished_at': None, 'not_started_streak': 0},
                               'malformed': {'outcome': 'ran', 'last_ran_at': 'invalid'},
                               'leaks': {'outcome': 'not_started', 'not_started_streak': 3}}}, now=10)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            with patch.object(B, 'OUT', root / 'findings.json'), patch.object(B, 'ACKS', root / 'acks.json'), \
                 patch.object(B, 'collect', return_value=found), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(B.main([]), 0)
            saved = json.loads((root / 'findings.json').read_text())['findings']
        unknown = [f for f in saved if f['type'] == 'tick.rotation_degraded']
        self.assertEqual(len(unknown), 1)
        self.assertIn('missing or invalid', unknown[0]['detail'])
        self.assertIn('unknown completion', unknown[0]['detail'])
        self.assertIn('tick.collector_starved:collector:leaks', {f['id'] for f in saved})

    def test_step_titles_and_severe_tail_keep_outcomes(self):
        import tick_findings as F
        doc = {'failed_steps': [{'step': name, 'exit': 124, 'outcome': outcome} for name, outcome in
                               [('index', 'not_started'), ('merge', 'not_started'), ('leaks', 'stopped'), ('emit', 'failed')]]}
        found = {f['subject']: f for f in F.findings(doc, None)}
        self.assertEqual(found['step:index']['severity'], 'info')
        self.assertEqual(found['step:merge']['severity'], 'warning')
        self.assertIn('did not start', found['step:merge']['title_id'])
        self.assertIn('stopped', found['step:leaks']['title_id'])
        self.assertIn('exited', found['step:emit']['title_id'])

    def test_disabled_outcome_does_not_freshen_last_measurement(self):
        spec = importlib.util.spec_from_file_location('tick_test', ROOT / 'tools/tick.py')
        tick = importlib.util.module_from_spec(spec); spec.loader.exec_module(tick)
        self.assertEqual(tick.outcome_of(0, 'disabled in workspace settings\n'), 'disabled')
        with tempfile.TemporaryDirectory() as raw:
            record = Path(raw).resolve() / 'rotation.json'
            record.write_text(json.dumps({'schema': 2, 'collectors': {'scan-gh': {'started_at': 10,
                'finished_at': 11, 'last_ran_at': 10, 'not_started_streak': 3, 'outcome': 'not_started'}}}))
            runner = tick.Tick(Path(raw).resolve()); runner.step = lambda *args: 'disabled'
            rotation = tick.Rotation(runner, record); rotation.step('scan-gh', 'unused'); rotation.run_in_turn()
            row = json.loads(record.read_text())['collectors']['scan-gh']
            self.assertEqual(row['last_ran_at'], 10)
            self.assertEqual(row['started_at'], 10)
            self.assertEqual(row['not_started_streak'], 0)

    def test_integrity_keeps_first_position_and_enters_rotation_receipt(self):
        from unittest.mock import patch
        import paths
        spec = importlib.util.spec_from_file_location('tick_integrity_test', ROOT / 'tools/tick.py')
        tick = importlib.util.module_from_spec(spec); spec.loader.exec_module(tick)
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw).resolve(); scratch = state / 'raw'; scratch.mkdir()
            runner = tick.Tick(scratch); calls = []
            def fake_step(name, *args):
                calls.append(name)
                return 'not_started' if name == 'integrity' else 'ran'
            runner.step = fake_step
            runner.bounded = lambda name, *args: (1, 'synthetic stop before merge')
            with patch.object(paths, 'STATE', state):
                with self.assertRaises(SystemExit):
                    tick.steps(runner, scratch, state / 'dashboard.html')
            receipt = json.loads((state / 'tick-rotation.json').read_text())
            self.assertEqual(calls[0], 'integrity')
            self.assertEqual(calls.count('integrity'), 1)
            self.assertIn('integrity', runner.order)
            self.assertEqual(receipt['collectors']['integrity']['not_started_streak'], 1)
            self.assertIsNone(receipt['collectors']['integrity']['last_ran_at'])

if __name__ == '__main__':
    unittest.main()
