"""The portable runner's disk footprint stays bounded (OBS-167).

A full run copied the engine's source once per suite (273 copies) and removed none of them
until the run ended: about 3.5 GB under the temporary folder, and four parallel runs on
2026-10-19 left a machine under 1 GB free. Each suite's sandbox now goes when the suite
ends, its log kept; and the runner refuses to start below a stated free-space floor.
"""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('runner_footprint', ROOT / 'observatory/engine/tests/run_portable.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


PASSING = 'import unittest\nclass T(unittest.TestCase):\n    def test_it(self):\n        print("synthetic child ran")\nunittest.main()\n'


def synthetic_template(base: Path, body: str = PASSING) -> Path:
    template = base / 'template'
    (template / 'tests').mkdir(parents=True)
    (template / 'tests/test_synthetic.py').write_text(body)
    (template / 'payload.bin').write_bytes(b'\0' * 4096)
    return template


class SandboxGoesWhenTheSuiteEnds(unittest.TestCase):
    def leftovers(self, work: Path) -> list[str]:
        return sorted(p.relative_to(work).as_posix() for p in work.rglob('*'))

    def test_a_passing_suite_keeps_only_its_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            row = runner.run_suite('synthetic', base, synthetic_template(base), 30)
            self.assertEqual('PASS', row['status'], row)
            self.assertEqual(['run.log'], self.leftovers(base / 'synthetic'))
            self.assertIn('synthetic child ran', (base / 'synthetic/run.log').read_text())
            # What it held before it went, so a run names the suites that write the most.
            self.assertGreaterEqual(row['sandbox_bytes'], 4096)

    def test_a_failing_suite_keeps_its_log_and_failure_tail_but_not_its_sandbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            body = 'import os\nos.makedirs("deep/inside", exist_ok=True)\nos.chmod("deep", 0o500)\nraise SystemExit(3)\n'
            row = runner.run_suite('synthetic', base, synthetic_template(base, body), 30)
            self.assertEqual('FAIL', row['status'], row)
            self.assertEqual(['run.log'], self.leftovers(base / 'synthetic'),
                             'even a directory the suite made read-only goes')

    def test_keep_keeps_the_sandbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            row = runner.run_suite('synthetic', base, synthetic_template(base), 30, keep=True)
            self.assertEqual('PASS', row['status'], row)
            self.assertTrue((base / 'synthetic/source/payload.bin').is_file())

    def test_a_suite_that_could_not_start_leaves_no_sandbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            template = synthetic_template(base)
            with mock.patch.object(runner.subprocess, 'Popen', side_effect=PermissionError('synthetic')):
                row = runner.run_suite_recorded('synthetic', base, template, 30)
            self.assertEqual('ERROR', row['status'])
            self.assertEqual([], self.leftovers(base / 'synthetic'))

    def test_main_passes_keep_through(self):
        seen = []

        def fake(name, base, template, timeout, keep=False):
            seen.append(keep)
            return {'file': 'tests/test_time.py', 'status': 'PASS', 'exit_code': 0}
        for argv, expected in ((['runner', '--suite', 'time'], False), (['runner', '--suite', 'time', '--keep'], True)):
            with mock.patch.object(sys, 'argv', argv), mock.patch.object(runner, 'copy_source'), \
                    mock.patch.object(runner, 'run_suite', side_effect=fake), \
                    contextlib.redirect_stdout(io.StringIO()):
                runner.main()
            self.assertEqual(expected, seen[-1])


class FreeSpaceFloor(unittest.TestCase):
    def run_main(self, argv, free):
        stream = io.StringIO()
        usage = mock.Mock(return_value=mock.Mock(free=free))
        with mock.patch.object(sys, 'argv', ['runner', *argv]), \
             mock.patch.object(runner.shutil, 'disk_usage', usage), \
             mock.patch.object(runner, 'copy_source') as copy, \
             mock.patch.object(runner, 'run_suite',
                               return_value={'file': 'tests/test_time.py', 'status': 'PASS', 'exit_code': 0}) as run, \
             contextlib.redirect_stdout(stream), contextlib.redirect_stderr(io.StringIO()) as err:
            code = runner.main()
        return code, json.loads(stream.getvalue()), copy, run, err.getvalue()

    def test_below_the_floor_nothing_is_copied_or_run(self):
        code, doc, copy, run, err = self.run_main(['--suite', 'time'], free=100 * 1024 * 1024)
        self.assertEqual(runner.EXIT_NO_SPACE, code)
        self.assertEqual(('FAIL', 'insufficient_disk'), (doc['status'], doc['reason_code']))
        self.assertLess(doc['free_bytes'], doc['needed_bytes'])
        self.assertEqual([], doc['suites'])
        copy.assert_not_called()
        run.assert_not_called()
        self.assertIn(str(doc['needed_bytes']), doc['reason'])
        self.assertIn('--min-free-gb 0', doc['reason'], 'the refusal names the flag that overrides it')
        self.assertIn(doc['reason'], err)

    def test_a_source_that_cannot_be_sized_is_a_fail_receipt_not_a_traceback(self):
        stream = io.StringIO()
        with mock.patch.object(sys, 'argv', ['runner', '--suite', 'time']), \
             mock.patch.object(runner, 'source_files', side_effect=FileNotFoundError('tests/test_gone.py')), \
             contextlib.redirect_stdout(stream):
            code = runner.main()
        doc = json.loads(stream.getvalue())
        self.assertEqual((1, 'FAIL', 'FileNotFoundError'), (code, doc['status'], doc['error_type']))

    def test_the_floor_scales_with_jobs(self):
        one = runner.space_needed(jobs=1, floor_gb=0)
        eight = runner.space_needed(jobs=8, floor_gb=0)
        self.assertGreater(one, 0)
        self.assertGreater(eight, one)
        self.assertEqual(runner.space_needed(jobs=1, floor_gb=2), one + 2 * 1024 ** 3)

    def test_the_flag_lowers_or_lifts_the_floor(self):
        code, doc, copy, run, _ = self.run_main(['--suite', 'time', '--min-free-gb', '0'], free=100 * 1024 * 1024)
        self.assertEqual((0, 'PASS'), (code, doc['status']), doc)
        copy.assert_called_once()
        code, doc, *_ = self.run_main(['--suite', 'time'], free=100 * 1024 ** 3)
        self.assertEqual((0, 'PASS'), (code, doc['status']), doc)


if __name__ == '__main__':
    unittest.main()
