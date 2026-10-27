"""tools/check_windows_suites.py: the Windows known list only shrinks, and nothing passes silently (OBS-277)."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import check_windows_suites as C  # noqa: E402

SUITES = ("alpha", "beta", "gamma")
ROWS = {"OBS-10", "OBS-11"}


def receipt(**statuses):
    return {"suites": [{"file": f"tests/test_{name}.py", "status": status, "executed_checks": 1,
                        "skipped_checks": 0, **({"skip_reason": "no tool"} if status == "SKIP" else {})}
                       for name, status in statuses.items()]}


def known(**entries):
    return {"suites": {name: {"expect": expect, "row": row, "reason": "because"}
                       for name, (expect, row) in entries.items()}}


class KnownList(unittest.TestCase):
    def problems(self, rec, kn):
        return C.check(rec, kn, ROWS, SUITES)[0]

    def test_all_pass_and_an_empty_list_is_clean(self):
        self.assertEqual(self.problems(receipt(alpha="PASS", beta="PASS", gamma="PASS"), known()), [])

    def test_a_listed_failure_is_accepted(self):
        self.assertEqual(self.problems(receipt(alpha="FAIL", beta="ERROR", gamma="PASS"),
                                       known(alpha=("FAIL", "OBS-10"), beta=("FAIL", "OBS-11"))), [])

    def test_an_unlisted_failure_fails(self):
        found = self.problems(receipt(alpha="TIMEOUT", beta="PASS", gamma="PASS"), known())
        self.assertEqual(len(found), 1)
        self.assertIn("alpha: TIMEOUT and not on the known list", found[0])

    def test_a_listed_suite_that_passes_now_fails_so_the_list_shrinks(self):
        found = self.problems(receipt(alpha="PASS", beta="PASS", gamma="PASS"), known(alpha=("FAIL", "OBS-10")))
        self.assertEqual(len(found), 1)
        self.assertIn("passes now", found[0])

    def test_a_whole_suite_skip_must_be_listed_as_a_skip(self):
        self.assertIn("skipped as a whole", self.problems(receipt(alpha="SKIP", beta="PASS", gamma="PASS"), known())[0])
        self.assertEqual(self.problems(receipt(alpha="SKIP", beta="PASS", gamma="PASS"),
                                       known(alpha=("SKIP", "OBS-10"))), [])
        self.assertIn("the list expects SKIP",
                      self.problems(receipt(alpha="FAIL", beta="PASS", gamma="PASS"), known(alpha=("SKIP", "OBS-10")))[0])

    def test_a_missing_suite_and_a_stale_entry_fail(self):
        found = self.problems(receipt(alpha="PASS", beta="PASS"), known(delta=("FAIL", "OBS-10")))
        self.assertTrue(any("does not hold every suite" in p and "gamma" in p for p in found), found)
        self.assertTrue(any(p.startswith("delta: on the known list but not a suite") for p in found), found)

    def test_an_entry_needs_a_real_row_and_a_reason(self):
        kn = {"suites": {"alpha": {"expect": "FAIL", "row": "OBS-99", "reason": ""}}}
        found = self.problems(receipt(alpha="FAIL", beta="PASS", gamma="PASS"), kn)
        self.assertTrue(any("OBS-99 is not a row" in p for p in found), found)
        self.assertTrue(any("gives no reason" in p for p in found), found)

    def test_the_committed_list_names_real_rows_and_runner_suites(self):
        kn = json.loads((ROOT / "tools/windows-known.json").read_text(encoding="utf-8"))
        rows = C.backlog_rows((ROOT / "docs/backlog.md").read_text(encoding="utf-8"))
        suites = C.runner_suites()
        for name, entry in kn["suites"].items():
            self.assertIn(name, suites, name)
            self.assertIn(entry["row"], rows, name)
            self.assertIn(entry["expect"], ("FAIL", "SKIP"), name)
            self.assertTrue(entry["reason"].strip(), name)

    def test_the_command_exits_1_on_a_problem_and_writes_the_summary(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rec, kn, board, out = (Path(tmp, n) for n in ("r.json", "k.json", "b.md", "s.md"))
            rec.write_text(json.dumps(receipt(**{name: "PASS" for name in C.runner_suites()})), encoding="utf-8")
            kn.write_text(json.dumps(known()), encoding="utf-8")
            board.write_text("| OBS-10 | x | open | y |\n", encoding="utf-8")
            self.assertEqual(C.main([str(rec), "--known", str(kn), "--backlog", str(board), "--summary", str(out)]), 0)
            self.assertIn("Problems: none", out.read_text(encoding="utf-8"))
            kn.write_text(json.dumps(known(alpha=("FAIL", "OBS-10"))), encoding="utf-8")
            self.assertEqual(C.main([str(rec), "--known", str(kn), "--backlog", str(board)]), 1)
            self.assertEqual(C.main([str(Path(tmp, "absent.json")), "--known", str(kn), "--backlog", str(board)]), 2)


if __name__ == "__main__":
    unittest.main()
