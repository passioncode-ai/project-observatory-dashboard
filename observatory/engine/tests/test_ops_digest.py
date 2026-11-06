#!/usr/bin/env python3
"""The engine's reading of an operations digest agrees with the contract's (OBS-330, DEC-0153).

Everything here runs over the contract's own files, vendored byte for byte at its release tag v0.1.0+2026-11-06.1
(`fabric/ops-digest/README.md`):

* the vectors: every hourly case (11) and every daily case through `fold_hour` and `fold_day`, each answer equal to
  the contract's, and each answer read back by `check` with no finding;
* the fixtures: each positive line passes, each negative one fails the schema, each semantic one reports exactly the
  rule codes the contract's `opsDigestRules` reports;
* the detectors (`secretValue`, `hasHomePath`) on the examples `docs/specification/ops-digest.md` gives;
* where Python and JavaScript read a pattern differently (`$` before a final line feed, case folding outside ASCII,
  `3.0` as a count), the JavaScript reading;
* the digests of the vendored files, so a hand edit is caught.

Values shaped like a credential are built here from pieces, never written whole.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ops_digest as OD  # noqa: E402

VENDORED = ROOT / "fabric" / "ops-digest"
VECTORS = json.loads((VENDORED / "ops-digest.json").read_text(encoding="utf-8"))


def kind_of(name: str) -> str:
    return "ops-digest-daily" if "daily" in name else "ops-digest-hourly"


class TheVendoredFilesAreTheContractsBytes(unittest.TestCase):
    def test_every_digest_the_readme_names_matches(self) -> None:
        readme = (VENDORED / "README.md").read_text(encoding="utf-8")
        rows = dict(re.findall(r"^\| `([^`]+)` \| `([0-9a-f]{64})` \|$", readme, re.M))
        files = sorted(p.relative_to(VENDORED).as_posix() for p in VENDORED.rglob("*.json"))
        self.assertEqual(sorted(rows), files)
        for name, digest in rows.items():
            self.assertEqual(hashlib.sha256((VENDORED / name).read_bytes()).hexdigest(), digest, name)

    def test_the_readme_names_the_release_tag_and_its_commit(self) -> None:
        readme = (VENDORED / "README.md").read_text(encoding="utf-8")
        self.assertIn("`v0.1.0+2026-11-06.1`", readme)
        self.assertIn("eac1a594aa131851f079da8bb7b2149e1951f35f", readme)

    def test_the_vectors_hold_eleven_hourly_cases(self) -> None:
        self.assertEqual(len(VECTORS["hour"]), 11)
        self.assertGreaterEqual(len(VECTORS["day"]), 1)


class TheVectors(unittest.TestCase):
    def test_every_hourly_case(self) -> None:
        for case in VECTORS["hour"]:
            with self.subTest(case["name"]):
                got = OD.fold_hour(copy.deepcopy(case["input"]["counts"]), case["input"]["from"],
                                   copy.deepcopy(case["input"]["previous"]))
                self.assertEqual(got, case["expected"])
                # Order too: `ops` by name, an hourly `risen` in the order of `ops` (the contract compares in it).
                self.assertEqual(json.dumps(got), json.dumps(case["expected"]))
                self.assertEqual(OD.check("ops-digest-hourly", got), [])

    def test_every_daily_case(self) -> None:
        for case in VECTORS["day"]:
            with self.subTest(case["name"]):
                got = OD.fold_day(case["input"]["day"], copy.deepcopy(case["input"]["hourly"]))
                self.assertEqual(json.dumps(got), json.dumps(case["expected"]))
                if got is not None:
                    self.assertEqual(OD.check("ops-digest-daily", got), [])

    def test_every_hour_a_daily_case_folds_is_a_line_the_engine_reads(self) -> None:
        for case in VECTORS["day"]:
            for hour in case["input"]["hourly"]:
                with self.subTest(case["name"], hour=hour["from"]):
                    self.assertEqual(OD.check("ops-digest-hourly", hour), [])


class TheFixtures(unittest.TestCase):
    def test_positive_lines_pass(self) -> None:
        for path in sorted((VENDORED / "positive").glob("*.json")):
            with self.subTest(path.name):
                self.assertEqual(OD.check(kind_of(path.name), json.loads(path.read_text(encoding="utf-8"))), [])

    def test_negative_lines_fail_the_schema(self) -> None:
        paths = sorted((VENDORED / "negative").glob("*.json"))
        self.assertEqual(len(paths), 9)
        for path in paths:
            with self.subTest(path.name):
                findings = OD.check(kind_of(path.name), json.loads(path.read_text(encoding="utf-8")))
                self.assertTrue(findings)
                self.assertEqual({f["code"] for f in findings}, {"schema"})

    def test_semantic_lines_report_the_contracts_codes(self) -> None:
        paths = sorted((VENDORED / "semantic").glob("*.json"))
        self.assertEqual(len(paths), 14)
        for path in paths:
            doc = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(path.name):
                codes = {f["code"] for f in OD.check(doc["kind"], doc["input"])}
                self.assertEqual(codes, set(doc["expect"]))

    def test_a_finding_never_copies_a_refused_name(self) -> None:
        doc = json.loads((VENDORED / "semantic/ops-digest-hourly-op-credential.json").read_text(encoding="utf-8"))
        at = next(i for i, c in enumerate(doc["input"]["ops"]) if OD.secret_value(c["op"]))
        bad = doc["input"]["ops"][at]["op"]
        twice = copy.deepcopy(doc["input"])
        twice["ops"].append(dict(twice["ops"][at]))
        twice["total"] = {k: sum(c[k] for c in twice["ops"]) for k in OD.COUNTERS}
        text = json.dumps(OD.check("ops-digest-hourly", twice))
        self.assertIn("is counted once", text)
        self.assertNotIn(bad, text)


class TheDetectors(unittest.TestCase):
    """The examples docs/specification/ops-digest.md gives for FAC-SEM-167, and the boundaries of each shape."""

    def test_home_folders(self) -> None:
        for refused in ("api.users/list", "api.get_users/list", "api.my_users/list", "ui.home/open",
                        "users/jdoe/fabric", "x.-Users-jdoe", "%2FUsers%2Fjdoe", "~/x", "a ~jdoe/x", "~"):
            self.assertTrue(OD.has_home_path(refused), refused)
        for passed in ("api.user/list", "api.get_user/list", "my-users-list", "homework", "feature/home-page",
                       "Users-guide", "a/~1/x"):
            self.assertFalse(OD.has_home_path(passed), passed)

    def test_credentials(self) -> None:
        provider = "s" + "k-" + "proj-" + "Ab12Cd34Ef56Gh78"
        for refused in (provider, "ops.risk-assessment-completed", "queue.task-assigned-to-worker",
                        "e" + "yJabc.def.ghi", "Bearer x", "BEARER x", "A" * 20 + "b" * 19 + "1",
                        "AK" + "IA" + "ABCDEFGHIJKLMNOP"):
            self.assertTrue(OD.secret_value(refused), refused)
        for passed in ("work.session-end-task-back", "board.tick", "a" * 64, "Bearer", "bearer \n",
                       "abcdef0123456789" * 3):
            self.assertFalse(OD.secret_value(passed), passed)

    def test_case_folding_stays_ascii(self) -> None:
        """Python's IGNORECASE folds U+017F onto `s` and U+212A onto `k`; JavaScript without the `u` flag does not."""
        self.assertFalse(OD.secret_value("ſ" + "k-" + "x" * 16))
        self.assertFalse(OD.secret_value("sK-" + "x" * 16))

    def test_a_source_label(self) -> None:
        self.assertIsNone(OD.source_refused("app"))
        self.assertIsNone(OD.source_refused("fabric-board"))
        self.assertIn("credential", OD.source_refused("mask-" + "abcdefghijklmnop"))
        for bad in ("App", "", "a" * 33, "1app", "app\n", None):
            self.assertIsNotNone(OD.source_refused(bad), bad)


class WherePythonAndJavaScriptDiffer(unittest.TestCase):
    def daily(self) -> dict:
        return json.loads((VENDORED / "positive/ops-digest-daily.json").read_text(encoding="utf-8"))

    def test_a_final_line_feed_fails_as_it_does_in_javascript(self) -> None:
        line = self.daily()
        line["ops"][-1]["op"] += "\n"
        line["ops"].sort(key=lambda c: c["op"])
        self.assertEqual([f["instancePath"] for f in OD.check("ops-digest-daily", line)], ["/ops/2/op"])
        line = self.daily()
        line["day"] += "\n"
        self.assertEqual([f["code"] for f in OD.check("ops-digest-daily", line)], ["schema"])

    def test_a_whole_float_is_a_count_and_is_written_back_as_one(self) -> None:
        line = self.daily()
        line["ops"][0]["ok"] = float(line["ops"][0]["ok"])
        line["total"]["ok"] = float(line["total"]["ok"])
        line["hours"] = float(line["hours"])
        self.assertEqual(OD.check("ops-digest-daily", line), [])
        normal = OD.normalized(line)
        self.assertEqual(json.dumps(normal), json.dumps(self.daily()))

    def test_the_window(self) -> None:
        self.assertIsNone(OD.whole_hour("2026-02-30T00:00:00Z"))
        self.assertIsNone(OD.whole_hour("2026-11-04T24:00:00Z"))
        self.assertIsNotNone(OD.whole_hour("2026-11-04T23:00:00.000Z"))
        self.assertFalse(OD.real_day("2026-13-01"))
        self.assertTrue(OD.real_day("2028-02-29"))
        self.assertFalse(OD.real_day("2027-02-29"))


class TheFoldsRefuseWhatNoReaderTakes(unittest.TestCase):
    def test_a_refused_name_writes_no_hour(self) -> None:
        with self.assertRaisesRegex(ValueError, "FAC-SEM-167"):
            OD.fold_hour([{"op": "api.users/list", "ok": 1, "failed": 0, "unknown": 0}], "2026-11-04T01:00:00Z", None)
        with self.assertRaisesRegex(ValueError, "grammar"):
            OD.fold_hour([{"op": "/logs", "ok": 1, "failed": 0, "unknown": 0}], "2026-11-04T01:00:00Z", None)

    def test_a_day_without_its_own_hour_has_no_digest(self) -> None:
        self.assertIsNone(OD.fold_day("2026-11-05", []))
        with self.assertRaises(ValueError):
            OD.fold_day("2026-11-31", [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
