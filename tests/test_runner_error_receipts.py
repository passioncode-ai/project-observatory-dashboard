"""A failed suite setup must not erase unrelated synthetic verification evidence."""
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
spec = importlib.util.spec_from_file_location('runner_receipts', ROOT / 'observatory/engine/tests/run_portable.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class SuiteFailureReceipts(unittest.TestCase):
    def test_failed_summary_permissions_publish_no_success_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / 'report'
            stream = io.StringIO()
            row = {'file': 'tests/test_time.py', 'status': 'PASS', 'exit_code': 0}
            argv = ['runner', '--suite', 'time', '--report-dir', str(destination)]
            chmod = Path.chmod

            def fail_summary(path, mode, **kwargs):
                if path.name.startswith('summary'):
                    raise PermissionError('synthetic permission refusal')
                return chmod(path, mode, **kwargs)

            with mock.patch.object(sys, 'argv', argv), \
                 mock.patch.object(runner, 'copy_source'), \
                 mock.patch.object(runner, 'run_suite', return_value=row), \
                 mock.patch.object(Path, 'chmod', fail_summary), \
                 contextlib.redirect_stdout(stream):
                code = runner.main()
            result = json.loads(stream.getvalue())
            self.assertEqual(1, code)
            self.assertEqual([row], result['suites'])
            self.assertFalse((destination / 'summary.json').exists())

    def test_report_write_failure_keeps_stdout_receipts(self):
        with tempfile.TemporaryDirectory() as temporary:
            stream = io.StringIO()
            row = {'file': 'tests/test_time.py', 'status': 'PASS', 'exit_code': 0}
            argv = ['runner', '--suite', 'time', '--report-dir', temporary]
            with mock.patch.object(sys, 'argv', argv), \
                 mock.patch.object(runner, 'copy_source'), \
                 mock.patch.object(runner, 'run_suite', return_value=row), \
                 contextlib.redirect_stdout(stream):
                code = runner.main()
            result = json.loads(stream.getvalue())
            self.assertEqual(1, code)
            self.assertEqual('FAIL', result['status'])
            self.assertEqual([row], result['suites'])
            self.assertEqual('FileExistsError', result['report_error_type'])

    def test_setup_failure_preserves_every_selected_suite_and_report(self):
        def execute(name, base, template, timeout):
            if name == 'time':
                raise OSError('synthetic setup failure; body must not reach receipt')
            work = base / name
            work.mkdir()
            (work / 'run.log').write_text('synthetic evidence\n')
            return {'file': 'tests/test_' + name + '.py', 'status': 'PASS',
                    'exit_code': 0, 'log': name + '/run.log', 'unittest_cases': 1}

        with tempfile.TemporaryDirectory() as temporary:
            report_dir = Path(temporary) / 'report'
            stream = io.StringIO()
            argv = ['runner', '--suite', 'ledger', '--suite', 'time', '--suite', 'vault',
                    '--jobs', '2', '--report-dir', str(report_dir)]
            with mock.patch.object(sys, 'argv', argv), \
                 mock.patch.object(runner, 'copy_source'), \
                 mock.patch.object(runner, 'run_suite', side_effect=execute), \
                 contextlib.redirect_stdout(stream):
                code = runner.main()
            result = json.loads(stream.getvalue())
            self.assertEqual(1, code)
            self.assertEqual('FAIL', result['status'])
            self.assertEqual(3, result['suite_count'])
            self.assertEqual(['PASS', 'ERROR', 'PASS'], [r['status'] for r in result['suites']])
            failure = result['suites'][1]
            self.assertEqual('OSError', failure['error_type'])
            self.assertEqual('tests/test_time.py', failure['file'])
            self.assertNotIn('body must not', stream.getvalue())
            self.assertEqual(result, json.loads((report_dir / 'summary.json').read_text()))
            self.assertEqual('synthetic evidence\n', (report_dir / 'vault/run.log').read_text())
            self.assertTrue(any(r['scope'] == 'suite:tests/test_time.py' for r in result['not_run']))


if __name__ == '__main__':
    unittest.main()
