"""PB-032: the leak scan says what it could not read, and suppression needs a reason and an expiry."""
import datetime
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "tools", ROOT / "collectors"):
    sys.path.insert(0, str(p))

import leak_findings as F  # noqa: E402
import paths  # noqa: E402
import scan_env  # noqa: E402
import scan_leaks as L  # noqa: E402

SECRET = "synthetic-value-" + "d" * 24
# The scanner judges expiry against the UTC date (scan_leaks.py), so the fixtures
# must too: a local date runs a day ahead between local and UTC midnight east of
# Greenwich, and "expired yesterday" was then still valid today.
TODAY = datetime.datetime.now(datetime.timezone.utc).date()


class Coverage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.readable = base / "log-a.txt"
        self.readable.write_text(f"prefix {SECRET} suffix")
        self.locked = base / "log-b.txt"
        self.locked.write_text(f"also {SECRET}")
        self.locked.chmod(0)
        self.rules = base / "leak_suppressions.json"
        self.saved = (L.STATE, L.OUT, L.known_values, L.targets, paths.COMPANION_DB, scan_env.salt, L.SUPPRESSIONS)
        L.STATE, L.OUT = base / "state.json", base / "leak-scan.json"
        L.known_values = lambda: ({SECRET: "demo/API_TOKEN"}, [], [])
        L.targets = lambda days: ([self.readable, self.locked], [])
        paths.COMPANION_DB = base / "absent.db"
        scan_env.salt = lambda: "e" * 64
        L.SUPPRESSIONS = self.rules

    def tearDown(self):
        self.locked.chmod(0o600)
        (L.STATE, L.OUT, L.known_values, L.targets, paths.COMPANION_DB, scan_env.salt, L.SUPPRESSIONS) = self.saved
        self.tmp.cleanup()

    def scan(self, full=True):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(L.main(["leaks"] + (["--full"] if full else [])), 0)
        return json.loads(L.OUT.read_text())

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode-000 file")
    def test_an_unreadable_file_is_listed_not_counted_clean(self):
        doc = self.scan()
        self.assertEqual(doc["coverage"]["targets_unreadable"], 1)
        self.assertEqual([h["where"] for h in doc["hits"]], [str(self.readable)])
        self.assertTrue(any(n.get("unreadable") and n["what"] == str(self.locked) for n in doc["not_scanned"]))
        blind = [f for f in F.findings(doc) if f["type"] == "secret.leak_scan_blind"]
        self.assertEqual(blind[0]["severity"], "warning")

    def rules_file(self, *rules):
        self.rules.write_text(json.dumps({"suppressions": list(rules)}))

    def test_a_suppression_with_reason_and_expiry_moves_the_sighting_aside(self):
        self.rules_file({**self.scan()["hits"][0], "reason": "a fixture the test prints",
                         "expires_on": (TODAY + datetime.timedelta(days=30)).isoformat()})
        doc = self.scan()
        self.assertEqual(doc["hits"], [])
        self.assertEqual(doc["suppressed"][0]["reason"], "a fixture the test prints")
        types = {f["type"] for f in F.findings(doc)}
        self.assertNotIn("secret.seen_outside_its_home", types)
        self.assertIn("secret.sighting_suppressed", types)

    def test_an_expired_or_incomplete_rule_is_not_applied_and_is_reported(self):
        self.rules_file({**self.scan()["hits"][0], "reason": "old decision",
                         "expires_on": (TODAY - datetime.timedelta(days=1)).isoformat()},
                        {"secret": "demo/API_TOKEN", "where": "log-a.txt", "expires_on": "2099-01-01"})
        doc = self.scan()
        self.assertEqual(len(doc["hits"]), 1, "the sighting is reported again")
        self.assertEqual(len(doc["suppression_problems"]), 2)
        bad = [f for f in F.findings(doc) if f["type"] == "secret.suppression_not_applied"]
        self.assertEqual(bad[0]["severity"], "warning")

    def test_a_rotated_value_is_not_covered_by_the_old_decision(self):
        original = self.scan()["hits"][0]
        self.rules_file({**original, "reason": "accepted old fixture",
                         "expires_on": (TODAY + datetime.timedelta(days=30)).isoformat()})
        self.assertEqual(self.scan()["hits"], [])
        replacement = "synthetic-rotated-" + "f" * 24
        self.readable.write_text(replacement)
        L.known_values = lambda: ({replacement: "demo/API_TOKEN"}, [], [])
        doc = self.scan()
        self.assertEqual(len(doc["hits"]), 1, "a decision about an old value cannot hide its replacement")
        self.assertEqual(doc["suppressed"], [])

    def test_a_similarly_named_path_is_not_covered(self):
        original = self.scan()["hits"][0]
        self.rules_file({**original, "reason": "accepted this file only",
                         "expires_on": (TODAY + datetime.timedelta(days=30)).isoformat()})
        another = self.readable.with_name("log-a.txt.copy")
        another.write_text(SECRET)
        L.targets = lambda days: ([self.readable, another], [])
        self.assertEqual([h["where"] for h in self.scan()["hits"]], [str(another)])

    def test_malformed_json_shapes_are_reported_without_crashing(self):
        for payload in [[], 1, "wrong", {"suppressions": "wrong"},
                        {"suppressions": [42]},
                        {"suppressions": [{"secret": 1, "where": [], "reason": {}, "expires_on": 9}]}]:
            with self.subTest(payload=payload):
                self.rules.write_text(json.dumps(payload))
                doc = self.scan()
                self.assertEqual(len(doc["hits"]), 1)
                self.assertTrue(doc["suppression_problems"])

    def test_legacy_unversioned_rules_do_not_suppress(self):
        self.rules_file({"secret": "demo/API_TOKEN", "where": str(self.readable),
                         "reason": "old config", "expires_on": TODAY.isoformat()})
        doc = self.scan()
        self.assertEqual(len(doc["hits"]), 1)
        self.assertTrue(doc["suppression_problems"])

    def test_two_values_with_the_same_name_are_distinct_in_files_and_sqlite(self):
        other = "synthetic-other-" + "f" * 24
        self.readable.write_text(SECRET + " " + other)
        L.known_values = lambda: ({SECRET: "demo/API_TOKEN", other: "demo/API_TOKEN"}, [], [])
        db = self.readable.parent / "memory.db"
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE notes(body TEXT)")
            conn.execute("INSERT INTO notes VALUES (?)", (SECRET + " " + other,))
            conn.commit()
        paths.COMPANION_DB = db
        original = self.scan()["hits"]
        self.assertEqual(len(original), 4)
        version = original[0]["version_id"]
        self.rules_file(*[{**h, "reason": "only the first version", "expires_on": TODAY.isoformat()}
                          for h in original if h["version_id"] == version])
        doc = self.scan()
        self.assertEqual(len(doc["hits"]), 2)
        self.assertEqual(len(doc["suppressed"]), 2)
        self.assertTrue(all(h["version_id"] != version for h in doc["hits"]))

    def test_missing_or_changed_salt_never_reuses_a_decision(self):
        original = self.scan()["hits"][0]
        self.rules_file({**original, "reason": "fixture", "expires_on": TODAY.isoformat()})
        for salt in [lambda: "f" * 64, lambda: (_ for _ in ()).throw(OSError("missing"))]:
            scan_env.salt = salt
            self.assertEqual(len(self.scan()["hits"]), 1)

    def test_expiry_rechecks_old_file_and_sqlite_content_on_incremental_scan(self):
        db = self.readable.parent / "memory.db"
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE notes(body TEXT)")
            conn.execute("INSERT INTO notes VALUES (?)", (SECRET,))
            conn.commit()
        paths.COMPANION_DB = db
        original = self.scan()["hits"]
        self.rules_file(*[{**h, "reason": "today only", "expires_on": TODAY.isoformat()} for h in original])
        self.assertEqual(len(self.scan(full=False)["suppressed"]), 2)
        self.assertEqual(self.scan(full=False)["hits"], [])
        class Tomorrow(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.datetime.now(tz) + datetime.timedelta(days=1)
        with patch.object(L, "datetime", Tomorrow):
            doc = self.scan(full=False)
        self.assertFalse(doc["incremental"])
        self.assertEqual(doc["companion_store"]["why"], "the effective suppression rules changed")
        self.assertEqual(len(doc["hits"]), 2, "expiry must replay old evidence, not just future writes")
        self.assertEqual(len(doc["suppression_problems"]), 2)

    def test_new_value_rechecks_old_file_content_without_full_flag(self):
        other = "synthetic-other-" + "f" * 24
        self.readable.write_text(other)
        self.assertEqual(self.scan()["hits"], [])
        L.known_values = lambda: ({other: "demo/API_TOKEN"}, [], [])
        self.assertEqual(len(self.scan(full=False)["hits"]), 1)

    def test_the_report_never_holds_the_value(self):
        self.assertNotIn(SECRET, json.dumps(self.scan()))


if __name__ == "__main__":
    unittest.main()
