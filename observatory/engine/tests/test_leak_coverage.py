"""PB-032: the leak scan says what it could not read, and suppression needs a reason and an expiry."""
import datetime
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

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

    def scan(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(L.main(["leaks", "--full"]), 0)
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
        self.rules_file({"secret": "demo/API_TOKEN", "where": "log-a.txt", "reason": "a fixture the test prints",
                         "expires_on": (TODAY + datetime.timedelta(days=30)).isoformat()})
        doc = self.scan()
        self.assertEqual(doc["hits"], [])
        self.assertEqual(doc["suppressed"][0]["reason"], "a fixture the test prints")
        types = {f["type"] for f in F.findings(doc)}
        self.assertNotIn("secret.seen_outside_its_home", types)
        self.assertIn("secret.sighting_suppressed", types)

    def test_an_expired_or_incomplete_rule_is_not_applied_and_is_reported(self):
        self.rules_file({"secret": "demo/API_TOKEN", "where": "log-a.txt", "reason": "old decision",
                         "expires_on": (TODAY - datetime.timedelta(days=1)).isoformat()},
                        {"secret": "demo/API_TOKEN", "where": "log-a.txt", "expires_on": "2099-01-01"})
        doc = self.scan()
        self.assertEqual(len(doc["hits"]), 1, "the sighting is reported again")
        self.assertEqual(len(doc["suppression_problems"]), 2)
        bad = [f for f in F.findings(doc) if f["type"] == "secret.suppression_not_applied"]
        self.assertEqual(bad[0]["severity"], "warning")

    def test_the_report_never_holds_the_value(self):
        self.assertNotIn(SECRET, json.dumps(self.scan()))


if __name__ == "__main__":
    unittest.main()
