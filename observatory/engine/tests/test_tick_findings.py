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
    def test_a_core_step_out_of_time_says_the_tick_went_on(self):
        # OBS-220: the findings and the page are built over the registry as it stood.
        import tick_findings as F
        row = {'step': 'emit', 'exit': 124, 'outcome': 'stopped'}
        went_on = F.findings({'failed_steps': [row], 'registry_refreshed': False, 'registry_went_on': ['emit']}, None)
        self.assertEqual(went_on[0]['severity'], 'warning')
        self.assertIn('built over the registry as it stood', went_on[0]['detail'])
        stopped = F.findings({'failed_steps': [row], 'registry_refreshed': False}, None)
        self.assertIn('the tick stopped before replacing further outputs', stopped[0]['detail'])

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
            runner.probe = lambda name, *args: False
            with patch.object(paths, 'STATE', state):
                with self.assertRaises(SystemExit):
                    tick.steps(runner, scratch, state / 'dashboard.html')
            receipt = json.loads((state / 'tick-rotation.json').read_text())
            self.assertEqual(calls[0], 'integrity')
            self.assertEqual(calls.count('integrity'), 1)
            self.assertIn('integrity', runner.order)
            self.assertEqual(receipt['collectors']['integrity']['not_started_streak'], 1)
            self.assertIsNone(receipt['collectors']['integrity']['last_ran_at'])

class StepShare(unittest.TestCase):
    """OBS-52: one step that takes most of the tick is reported, whether or not it finished. Measured
    2026-10-21: the leak scan took a median 1125 s of a 1500 s ceiling and nothing on the board said
    one step was eating the tick. A step is over when it used ALERT_FRACTION of its own share (the
    collectors' share, so a collector inside it never fires: review of #328, L2), and the finding needs
    ALERT_MIN of the last ALERT_WINDOW ticks, so one slow tick does not make it flap."""

    def receipt(self, rid, stages):
        return {'schema': 1, 'run_id': rid * 32, 'stages': stages}

    def stage(self, step, seconds):
        return {'step': step, 'duration_seconds': seconds, 'outcome': 'ran', 'exit': 0}

    def tick_doc(self, stages, ceiling=1500, rid='a'):
        progress = {**self.receipt(rid, stages), 'write_failures': 0}
        if ceiling is not None:
            progress['ceiling_seconds'] = ceiling
        return {'progress': progress}

    def found(self, tick, runs=()):
        import tick_findings as F
        return [f['subject'] for f in F.findings(tick, None, runs=list(runs)) if f['type'] == 'tick.step_over_share']

    def over(self, step, ceiling=1500):
        import tick_budget
        return tick_budget.ALERT_FRACTION * tick_budget.share(step) * ceiling + 1

    def test_a_step_over_its_share_in_enough_ticks_is_reported(self):
        import tick_budget
        runs = [self.receipt(c, [self.stage('git-hygiene', self.over('git-hygiene'))]) for c in 'bc']
        self.assertEqual(self.found(self.tick_doc([self.stage('git-hygiene', self.over('git-hygiene'))]), runs),
                         ['step:git-hygiene'])
        self.assertEqual(tick_budget.ALERT_MIN, 3)

    def test_a_resumable_step_using_its_share_by_design_does_not_fire(self):
        import tick_budget
        self.assertIn('leaks', tick_budget.RESUMABLE)
        runs = [self.receipt(c, [self.stage('leaks', 0.98 * tick_budget.share_limit('leaks', 1500))]) for c in 'bcde']
        doc = self.tick_doc([self.stage('leaks', 0.98 * tick_budget.share_limit('leaks', 1500))])
        self.assertEqual(self.found(doc, runs), [])

    def test_one_slow_tick_does_not_fire(self):
        runs = [self.receipt(c, [self.stage('git-hygiene', 10)]) for c in 'bcde']
        self.assertEqual(self.found(self.tick_doc([self.stage('git-hygiene', self.over('git-hygiene'))]), runs), [])

    def test_a_collector_inside_its_share_never_fires(self):
        import tick_budget
        inside = tick_budget.ALERT_FRACTION * tick_budget.COLLECTOR_SHARE * 1500 - 1
        runs = [self.receipt(c, [self.stage('scan-fs', inside)]) for c in 'bcde']
        self.assertGreater(inside, 1500 / 3, 'a third of the ceiling is inside a collector share')
        self.assertEqual(self.found(self.tick_doc([self.stage('scan-fs', inside)]), runs), [])

    def test_the_same_run_is_counted_once(self):
        doc = self.tick_doc([self.stage('git-hygiene', self.over('git-hygiene'))], rid='a')
        twice = [doc['progress'], doc['progress'], self.receipt('b', [self.stage('git-hygiene', self.over('git-hygiene'))])]
        self.assertEqual(self.found(doc, twice), [])

    def test_the_ceiling_comes_from_the_receipt(self):
        runs = [self.receipt(c, [self.stage('scan-fs', self.over('scan-fs', 900))]) for c in 'bc']
        self.assertEqual(self.found(self.tick_doc([self.stage('scan-fs', self.over('scan-fs', 900))], ceiling=900), runs),
                         ['step:scan-fs'])

    def test_unreadable_stages_raise_nothing_and_crash_nothing(self):
        for stages in (None, 'x', [None, {'step': 'leaks'}, {'step': 'leaks', 'duration_seconds': 'x'},
                                   {'step': 'leaks', 'duration_seconds': float('nan')}]):
            with self.subTest(stages=stages):
                doc = {'progress': {'stages': stages, 'ceiling_seconds': 'x'}}
                self.assertEqual(self.found(doc, [None, 'x', {'stages': 'x'}]), [])

    def test_one_row_per_step_even_when_it_ran_twice(self):
        stages = [self.stage('findings', 900), self.stage('findings', 800)]
        runs = [self.receipt(c, stages) for c in 'bc']
        self.assertEqual(self.found(self.tick_doc(stages), runs), ['step:findings'])

    def test_starvation_begins_at_the_shared_threshold(self):
        import tick_budget
        import tick_findings as F
        for streak, expected in ((tick_budget.STARVED_TICKS, True), (tick_budget.STARVED_TICKS - 1, False)):
            with self.subTest(streak=streak):
                found = F.findings({'collector_order': ['google']}, {'schema': 2, 'collectors': {'google': {
                    'outcome': 'not_started', 'not_started_streak': streak, 'last_ran_at': 9990}}}, now=10000)
                self.assertEqual('tick.collector_starved' in {f['type'] for f in found}, expected)


if __name__ == '__main__':
    unittest.main()
