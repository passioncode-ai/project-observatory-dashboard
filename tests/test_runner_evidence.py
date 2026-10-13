"""A successful interpreter exit is not proof that a selected test suite ran."""
import ast
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('runner_evidence', ROOT / 'observatory/engine/tests/run_portable.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class SuiteEvidence(unittest.TestCase):
    def run_case(self, source, *, protocol='unittest', minimum=1):
        with tempfile.TemporaryDirectory(prefix='observatory-runner-evidence-') as temporary:
            base=Path(temporary).resolve(); template=base/'template'
            (template/'tests').mkdir(parents=True)
            (template/'tests/test_synthetic.py').write_text(source,encoding='utf-8')
            reporters = {'synthetic'} if protocol == 'assertion-lines' else set()
            with mock.patch.object(runner,'ASSERTION_LINE_SUITES',reporters,create=True), \
                 mock.patch.object(runner,'SUITE_MINIMUMS',{'synthetic':minimum},create=True):
                return runner.run_suite('synthetic',base,template,10)

    def test_unittest_without_main_is_error_not_success(self):
        row=self.run_case('import unittest\nclass Cases(unittest.TestCase):\n def test_bad(self): self.fail("must execute")\n')
        self.assertEqual(row['status'],'ERROR')
        self.assertEqual(row['exit_code'],0)
        self.assertEqual(row['unittest_cases'],0)
        self.assertEqual(row['reason_code'],'no_test_evidence')
        self.assertEqual(runner.overall([row]),'FAIL')
        self.assertTrue(any(r['scope']=='suite:tests/test_synthetic.py' for r in runner.not_run([row],None)))

    def test_zero_case_unittest_cannot_borrow_a_pass_marker(self):
        row=self.run_case('import unittest\nprint("PASS unrelated setup")\nunittest.main(exit=False)\n')
        self.assertEqual(row['status'],'ERROR')
        self.assertEqual(row['executed_checks'],0)

    def test_silent_direct_assert_is_not_implicitly_accepted(self):
        for protocol in ('unittest','assertion-lines'):
            with self.subTest(protocol=protocol):
                row=self.run_case('assert 1 + 1 == 2\n',protocol=protocol)
                self.assertEqual(row['status'],'ERROR')
                self.assertEqual(row['exit_code'],0)

    def test_positive_unittest_reports_cases(self):
        row=self.run_case('import unittest\nclass Cases(unittest.TestCase):\n def test_ok(self): self.assertEqual(2,1+1)\nunittest.main()\n')
        self.assertEqual(row['status'],'PASS')
        self.assertEqual(row['executed_checks'],1)
        self.assertEqual(row['test_protocol'],'unittest')

    def test_reported_direct_assert_remains_supported(self):
        row=self.run_case('assert 1 + 1 == 2\nprint("PASS arithmetic")\n',protocol='assertion-lines')
        self.assertEqual(row['status'],'PASS')
        self.assertEqual(row['executed_checks'],1)
        self.assertEqual(row['unittest_cases'],0)

    def test_full_skip_unittest_is_skip_not_pass_or_error(self):
        row=self.run_case('import unittest\nclass Cases(unittest.TestCase):\n @unittest.skip("synthetic platform")\n def test_skip(self): pass\nunittest.main()\n')
        self.assertEqual(row['status'],'SKIP')
        self.assertEqual(row['executed_checks'],0)
        self.assertEqual(runner.overall([row]),'SKIP')

    def test_reporter_full_skip_is_separate_from_missing_evidence(self):
        row=self.run_case('print("SKIP synthetic optional runtime")\n',protocol='assertion-lines')
        self.assertEqual(row['status'],'SKIP')
        self.assertEqual(row['executed_checks'],0)

    def test_stderr_failed_assertion_cannot_pass_on_zero_exit(self):
        row=self.run_case('import sys\nprint("PASS setup")\nprint("FAIL actual check",file=sys.stderr)\n',protocol='assertion-lines')
        self.assertEqual(row['status'],'FAIL')
        self.assertEqual(row['exit_code'],0)

    def test_unittest_failures_stay_failed_if_suite_swallows_exit(self):
        row=self.run_case('import unittest\nclass Cases(unittest.TestCase):\n def test_bad(self): self.fail("synthetic")\nunittest.main(exit=False)\n')
        self.assertEqual(row['status'],'FAIL')
        self.assertEqual(row['exit_code'],0)

    def test_nonzero_exit_stays_failed_even_with_positive_evidence(self):
        row=self.run_case('print("PASS setup")\nraise SystemExit(7)\n',protocol='assertion-lines')
        self.assertEqual(row['status'],'FAIL'); self.assertEqual(row['exit_code'],7)

    def test_suite_minimum_cannot_be_met_by_skips(self):
        row=self.run_case('print("PASS first")\nprint("SKIP second")\n',protocol='assertion-lines',minimum=2)
        self.assertEqual(row['status'],'ERROR')
        self.assertEqual(row['reason_code'],'insufficient_test_evidence')

    def test_unittest_summary_must_be_complete_not_a_quoted_counter(self):
        for source in ('print("example Ran 12 tests in 0.01s")\n', 'print("Ran 12 tests in 0.01s")\n'):
            self.assertEqual(self.run_case(source)['status'],'ERROR')

    def test_protocol_minimum_must_be_positive_integer(self):
        for minimum in (0,-1,True,1.5):
            self.assertEqual(self.run_case('print("PASS check")\n',protocol='assertion-lines',minimum=minimum)['status'],'ERROR')

    def test_impossible_skip_count_is_invalid_evidence(self):
        row=self.run_case('print("Ran 1 test in 0.001s\\n\\nOK (skipped=2)")\n')
        self.assertEqual(row['status'],'ERROR')
        self.assertEqual(row['reason_code'],'invalid_test_evidence')

    def test_malformed_skip_counter_is_not_zero_skips(self):
        for value in ('-1','unknown','1, skipped=1'):
            row=self.run_case('print("Ran 2 tests in 0.001s\\n\\nOK (skipped='+value+')")\n')
            self.assertEqual(row['status'],'ERROR')
            self.assertEqual(row['reason_code'],'invalid_test_evidence')

    def test_last_empty_summary_cannot_borrow_earlier_cases(self):
        row=self.run_case('print("Ran 4 tests in 0.001s\\n\\nOK\\nRan 0 tests in 0.001s\\n\\nOK")\n')
        self.assertEqual(row['status'],'ERROR')
        self.assertEqual(row['unittest_cases'],0)

    def test_earlier_unittest_failure_is_sticky_across_later_runs(self):
        for later in ('self.assertTrue(True)', 'self.skipTest("synthetic")'):
            source = ('import unittest\n'
                      'class First(unittest.TestCase):\n def test_bad(self): self.fail("synthetic")\n'
                      'class Last(unittest.TestCase):\n def test_last(self): '+later+'\n'
                      'for case in (First, Last):\n'
                      ' unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(case))\n')
            with self.subTest(later=later):
                row=self.run_case(source)
                self.assertEqual(row['status'],'FAIL')
                self.assertEqual(row['reason_code'],'reported_test_failure')
                self.assertEqual(row['exit_code'],0)

    def test_trailing_incomplete_summary_cannot_borrow_completed_result(self):
        row=self.run_case('print("Ran 1 test in 0.001s\\n\\nOK\\nRan 0 tests in 0.001s")\n')
        self.assertEqual(row['status'],'ERROR')
        self.assertEqual(row['reason_code'],'invalid_test_evidence')
        self.assertEqual(row['executed_checks'],0)

    def test_well_formed_reports_are_a_protocol_not_authentication(self):
        # An adversarial suite can lie; this gate does not authenticate its author.
        row=self.run_case('print("Ran 1 test in 0.001s\\n\\nOK")\n')
        self.assertEqual(row['status'],'PASS')
        self.assertEqual(row['test_protocol'],'unittest')

    def test_legacy_protocol_list_matches_current_registered_sources(self):
        self.assertFalse(set(runner.ASSERTION_LINE_SUITES) - set(runner.SUITES))
        for name in runner.SUITES:
            tree=ast.parse((runner.ROOT/'tests'/('test_'+name+'.py')).read_text())
            has_unittest_main=any(isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)
                and isinstance(n.func.value,ast.Name) and n.func.value.id=='unittest'
                and n.func.attr=='main' for n in ast.walk(tree))
            if has_unittest_main:
                self.assertNotIn(name,runner.ASSERTION_LINE_SUITES)


if __name__=='__main__': unittest.main()
