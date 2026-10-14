"""PB-131: the leak scan reads the companion store from its last mark, like a transcript.

tools/scan_leaks.py `scan_sqlite` + tools/sqlite_scan.py: a complete pass, then
only rows added since; a complete pass again when the value set changes, with
--full, or weekly. A sighting is reported by the pass that first reads its row.
The mark never holds a value.
"""
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "tools", ROOT / "collectors"):
    sys.path.insert(0, str(p))

import paths  # noqa: E402
import scan_env  # noqa: E402
import scan_leaks as L  # noqa: E402

SECRET_A = "synthetic-value-" + "a" * 24
SECRET_B = "synthetic-value-" + "b" * 24


class LeakScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        # The scan reads every agent's transcript home under HOME (I-5): a synthetic one here.
        env = patch.dict(os.environ, {"HOME": str(base / "user")})
        env.start()
        self.addCleanup(env.stop)
        for name in ("CODEX_HOME", "CODEX_SQLITE_HOME", "HERMES_HOME", "HERMES_DATA_DIR_SUFFIX",
                     "XDG_DATA_HOME", "OPENCODE_DB", "CLAUDE_CONFIG_DIR"):
            os.environ.pop(name, None)
        self.db = base / "companion.db"
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE observations(title TEXT, facts TEXT)")
        c.executemany("INSERT INTO observations VALUES (?, ?)", [(f"t{i}", f"fact {i}") for i in range(900)])
        c.execute("INSERT INTO observations VALUES ('x', ?)", (f"has {SECRET_A} inside",))
        c.commit(); c.close()
        self.values = {SECRET_A: "demo/A"}
        self.saved = (L.STATE, L.OUT, L.known_values, L.targets, paths.COMPANION_DB, scan_env.salt)
        L.STATE, L.OUT = base / "leak-scan-state.json", base / "leak-scan.json"
        L.known_values = lambda: (dict(self.values), [], [])
        L.targets = lambda days: ([], [])
        paths.COMPANION_DB = self.db
        scan_env.salt = lambda: "d" * 64

    def tearDown(self):
        (L.STATE, L.OUT, L.known_values, L.targets, paths.COMPANION_DB, scan_env.salt) = self.saved
        self.tmp.cleanup()

    def scan(self, *args):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(L.main(["leaks", *args]), 0)
        return json.loads(L.OUT.read_text())

    def insert(self, text):
        c = sqlite3.connect(self.db); c.execute("INSERT INTO observations VALUES ('n', ?)", (text,)); c.commit(); c.close()

    def test_a_row_is_read_once_and_a_new_row_is_read_next(self):
        first = self.scan()
        self.assertEqual((first["companion_store"]["mode"], first["companion_store"]["rows_read"]), ("full", 901))
        self.assertEqual([h["secret"] for h in first["hits"]], ["demo/A"])
        again = self.scan()
        self.assertEqual((again["companion_store"]["mode"], again["companion_store"]["rows_read"]), ("incremental", 0))
        self.assertEqual(again["hits"], [], "a sighting is reported by the pass that first reads its row")
        self.insert(f"new {SECRET_A}")
        third = self.scan()
        self.assertEqual(third["companion_store"]["rows_read"], 1)
        self.assertEqual(third["hits"][0]["occurrences"], 1)

    def test_a_new_value_is_looked_for_in_old_rows(self):
        self.scan()
        c = sqlite3.connect(self.db); c.execute("UPDATE observations SET facts=? WHERE rowid=3", (SECRET_B,)); c.commit(); c.close()
        self.values[SECRET_B] = "demo/B"
        out = self.scan()
        self.assertEqual(out["companion_store"]["mode"], "full")
        self.assertIn("demo/B", {h["secret"] for h in out["hits"]})

    def test_full_forces_a_complete_pass(self):
        self.scan()
        self.assertEqual(self.scan("--full")["companion_store"]["rows_read"], 901)

    def test_without_a_salt_every_pass_is_complete_and_no_mark_is_kept(self):
        def missing():
            raise RuntimeError("identity is missing")
        scan_env.salt = missing
        self.scan()
        self.assertEqual(self.scan()["companion_store"]["rows_read"], 901)
        self.assertNotIn("sqlite", json.loads(L.STATE.read_text()))

    def test_an_unreadable_store_keeps_its_old_mark(self):
        self.scan()
        before = json.loads(L.STATE.read_text())["sqlite"]
        self.db.write_bytes(b"not a database at all" * 100)
        out = self.scan()
        self.assertFalse(out["companion_store"]["read"])
        self.assertIn("source-unreadable", {d.get("code") for d in out["degraded"]})
        self.assertFalse(out["partial"], "an unreadable store is a hole, not a cut: the scan still finished")
        self.assertEqual(json.loads(L.STATE.read_text())["sqlite"]["stores"], before["stores"])

    def test_the_mark_holds_no_value(self):
        self.scan()
        self.assertNotIn(SECRET_A, L.STATE.read_text())

    def with_deadline(self, seconds_left):
        """Run a pass as a tick would, its deadline `seconds_left` away (OBS-52)."""
        import os, time
        saved = os.environ.get("OBSERVATORY_STEP_DEADLINE")
        os.environ["OBSERVATORY_STEP_DEADLINE"] = f"{time.time() + seconds_left:.3f}"
        try:
            return self.scan()
        finally:
            if saved is None:
                os.environ.pop("OBSERVATORY_STEP_DEADLINE", None)
            else:
                os.environ["OBSERVATORY_STEP_DEADLINE"] = saved

    def test_a_pass_the_deadline_cuts_short_writes_what_it_read_and_names_the_rest(self):
        base = Path(self.tmp.name).resolve()
        files = []
        for n in range(3):
            f = base / f"t{n}.jsonl"
            f.write_text(f"line {n} {SECRET_A}\n")
            files.append(f)
        L.targets = lambda days: (list(files), [])
        out = self.with_deadline(L.FILE_RESERVE_S - 1)
        self.assertTrue(out["partial"])
        self.assertIn("after 0 of 3 targets", out["degraded"][0]["why"])
        self.assertIn("UNMEASURED", out["degraded"][0]["effect"])
        self.assertEqual(out["degraded"][0]["code"], "deadline-targets", "a machine code beside the prose (ENG-001)")
        self.assertEqual(out["coverage"]["targets_read"], 0)
        self.assertEqual(out["coverage"]["stores_unread"], [])
        self.assertTrue(out["companion_store"]["read"], "the stores still fit in the time left")
        self.assertEqual(json.loads(L.STATE.read_text())["offsets"], {}, "no offset moved for an unread file")
        full = self.scan()
        self.assertFalse(full["partial"])
        self.assertEqual(full["degraded"], [])
        self.assertEqual(sum(1 for h in full["hits"] if h["where"].endswith(".jsonl")), 3, "the next pass reads them all")

    def test_a_store_the_deadline_reaches_is_unread_and_keeps_its_mark(self):
        self.scan()
        before = json.loads(L.STATE.read_text())["sqlite"]["stores"]
        self.insert(f"new row {SECRET_A}")
        out = self.with_deadline(L.STORE_RESERVE_S - 1)
        self.assertFalse(out["companion_store"]["read"])
        self.assertEqual(out["coverage"]["companion_store"], "unread")
        self.assertTrue(any("before" in d["why"] and "mark did not move" in d["why"] for d in out["degraded"]))
        self.assertIn("deadline-store", {d.get("code") for d in out["degraded"]})
        self.assertTrue(out["coverage"]["stores_unread"], "the unread store is named")
        self.assertNotIn(str(Path.home()) + "/", json.dumps(out["coverage"]["stores_unread"]),
                         "a store outside home is named as it is; one under home starts with ~")
        self.assertEqual(json.loads(L.STATE.read_text())["sqlite"]["stores"], before, "the mark did not move")
        later = self.scan()
        self.assertTrue(any(h["where"].startswith(str(self.db)) for h in later["hits"]), "the row is read by the next pass")


if __name__ == "__main__":
    unittest.main()
