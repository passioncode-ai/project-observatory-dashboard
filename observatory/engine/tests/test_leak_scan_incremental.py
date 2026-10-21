"""PB-131: the leak scan reads the companion store from its last mark, like a transcript.

tools/scan_leaks.py `scan_sqlite` + tools/sqlite_scan.py: a complete pass, then
only rows added since; a complete pass again when the value set changes, with
--full, or weekly. A sighting is reported by the pass that first reads its row.
The mark never holds a value.
"""
import importlib.util
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


class StopsInsideAUnit(LeakScan):
    """OBS-52, 2026-10-21: the leak scan was killed by its watchdog on ten ticks in a row (`exceeded
    its 1197 s limit`) and so wrote nothing, though it checks the deadline between files and between
    stores: one large transcript, or one store, outran the reserve. It now also checks between the
    chunks of a file and the pages of a store, and stops with what it read."""

    def clock(self, *left):
        """`step_budget.remaining` answering `left` in turn, then the last value for ever."""
        answers = list(left)

        def remaining():
            return answers.pop(0) if len(answers) > 1 else answers[0]
        patcher = patch.object(L.step_budget, "remaining", remaining)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_large_file_stops_between_chunks_and_the_next_pass_resumes_it(self):
        base = Path(self.tmp.name).resolve()
        big = base / "big.jsonl"
        big.write_bytes(f"{SECRET_A}\n".encode() + b"x" * (L.CHUNK * 2) + f"\n{SECRET_A}\n".encode())
        L.targets = lambda days: ([big], [])
        # The loop's check, then the first chunk's, then out of time before the second chunk.
        self.clock(1000, 1000, 0)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(L.main(["leaks"]), 0)
        first = json.loads(L.OUT.read_text())
        self.assertTrue(first["partial"])
        self.assertIn("deadline-targets", {d["code"] for d in first["degraded"]})
        self.assertEqual(sum(h["occurrences"] for h in first["hits"] if h["where"] == str(big)), 1)
        offset = json.loads(L.STATE.read_text())["offsets"][str(big)]
        self.assertGreater(offset, 0, "what was read is kept")
        self.assertLess(offset, big.stat().st_size, "what was not read is not marked read")
        patch.stopall()
        second = self.scan()
        self.assertFalse(second["partial"])
        self.assertEqual(sum(h["occurrences"] for h in second["hits"] if h["where"] == str(big)), 1,
                         "the rest is read by the next pass, and the start is not counted twice")
        self.assertEqual(json.loads(L.STATE.read_text())["offsets"][str(big)], big.stat().st_size)

    def test_a_value_split_across_the_stopping_chunk_is_found_by_the_next_pass(self):
        """The resume offset steps back by the overlap kept between chunks (review of #328, L4): a value
        that straddles the chunk the pass stopped after is read whole by the next pass."""
        base = Path(self.tmp.name).resolve()
        big = base / "split.jsonl"
        half = len(SECRET_A) // 2
        big.write_bytes(b"x" * (L.CHUNK - half) + SECRET_A.encode() + b"\n" + b"y" * L.CHUNK)
        L.targets = lambda days: ([big], [])
        self.clock(1000, 1000, 0)
        with redirect_stdout(io.StringIO()):
            L.main(["leaks"])
        first = json.loads(L.OUT.read_text())
        self.assertFalse([h for h in first["hits"] if h["where"] == str(big)], "the first pass saw half of it")
        patch.stopall()
        second = self.scan()
        self.assertEqual(sum(h["occurrences"] for h in second["hits"] if h["where"] == str(big)), 1)

    @unittest.skipUnless(importlib.util.find_spec("compression") and importlib.util.find_spec("compression.zstd"),
                         "this Python has no zstd decoder")
    def test_a_compressed_file_stopped_midway_is_not_recorded_as_read(self):
        from compression import zstd
        base = Path(self.tmp.name).resolve()
        z = base / "rollout.jsonl.zst"
        z.write_bytes(zstd.compress(b"x" * (L.CHUNK * 2) + f"\n{SECRET_A}\n".encode()))
        L.targets = lambda days: ([z], [])
        self.clock(1000, 1000, 0)
        with redirect_stdout(io.StringIO()):
            L.main(["leaks"])
        first = json.loads(L.OUT.read_text())
        self.assertIn("deadline-targets", {d["code"] for d in first["degraded"]})
        self.assertNotIn(str(z), json.loads(L.STATE.read_text())["signatures"], "a cut read leaves no signature")
        patch.stopall()
        self.assertEqual(sum(h["occurrences"] for h in self.scan()["hits"] if h["where"] == str(z)), 1,
                         "so the next pass reads it whole")

    def test_a_store_stops_between_pages_and_keeps_its_mark(self):
        self.scan()
        before = json.loads(L.STATE.read_text())["sqlite"]["stores"]
        self.insert(f"new row {SECRET_A}")
        # The store's start check passes; the check before its first page does not.
        self.clock(1000, 0)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(L.main(["leaks"]), 0)
        out = json.loads(L.OUT.read_text())
        self.assertFalse(out["companion_store"]["read"])
        self.assertIn("deadline-store", {d.get("code") for d in out["degraded"]})
        self.assertEqual(json.loads(L.STATE.read_text())["sqlite"]["stores"], before, "the mark did not move")
        patch.stopall()
        self.assertTrue(any(h["where"].startswith(str(self.db)) for h in self.scan()["hits"]))


class AFullPassResumesAcrossTicks(LeakScan):
    """Review of #328, M1: under its 30 % share the leak scan reaches the databases with a few minutes
    left, and the set of known values changes almost daily, so most passes are full. A full pass kept no
    progress per database: each tick began every database at rowid 0 in the same order, and one large
    store (a 1.9 GB, 286,000-row Codex history was measured) kept the databases behind it unread for ever.
    A full pass now keeps how far it read each table and resumes there, the databases it has not finished
    first; it is recorded complete only once every database is."""

    def database(self, name, rows):
        db = Path(self.tmp.name).resolve() / name
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE messages(body TEXT)")
        c.executemany("INSERT INTO messages VALUES (?)", [(f"row {i}",) for i in range(rows)])
        c.execute("INSERT INTO messages VALUES (?)", (f"late {SECRET_A}",))
        c.commit(); c.close()
        return db

    def test_every_database_is_read_once_across_ticks_and_then_the_pass_is_complete(self):
        dbs = [self.database(f"agent{n}.sqlite", 3 * L.PAGE) for n in range(3)]
        self.addCleanup(setattr, L, "transcript_stores", L.transcript_stores)
        L.transcript_stores = lambda notes: list(dbs)
        checks = {"left": 0}

        def remaining():
            checks["left"] -= 1
            return 1000 if checks["left"] >= 0 else 0
        patcher = patch.object(L.step_budget, "remaining", remaining)
        patcher.start()
        self.addCleanup(patcher.stop)
        seen, rows_read, passes = set(), 0, 0
        while passes < 20:
            passes += 1
            checks["left"] = 5               # a few pages a tick: no database fits in one
            with redirect_stdout(io.StringIO()):
                self.assertEqual(L.main(["leaks"]), 0)
            out = json.loads(L.OUT.read_text())
            rows_read += sum(r["rows_read"] for r in out["coverage"]["transcript_stores"])
            seen |= {h["where"].split("#")[0] for h in out["hits"]}
            if "sqlite_pending" not in json.loads(L.STATE.read_text()):
                break
        self.assertLess(passes, 20, "the full pass never completed")
        self.assertEqual(seen & {str(d) for d in dbs}, {str(d) for d in dbs}, "every database was read")
        self.assertLessEqual(rows_read, 3 * (3 * L.PAGE + 1) + 3 * L.PAGE,
                             "no database was read from the start again")
        state = json.loads(L.STATE.read_text())
        self.assertEqual(state["sqlite"]["values"], json.loads(L.STATE.read_text())["values_digest"])
        self.assertIsNotNone(state["sqlite"]["full_on"], "the full pass is recorded complete")
        self.assertNotIn("sqlite_pending", state, "nothing is left pending")
        patch.stopall()
        again = self.scan()
        self.assertEqual(again["companion_store"]["mode"], "incremental")

    def test_a_changed_value_set_starts_the_pending_pass_again(self):
        db = self.database("agent.sqlite", 3 * L.PAGE)
        self.addCleanup(setattr, L, "transcript_stores", L.transcript_stores)
        L.transcript_stores = lambda notes: [db]
        self.clock_left = {"n": 3}

        def remaining():
            self.clock_left["n"] -= 1
            return 1000 if self.clock_left["n"] >= 0 else 0
        with patch.object(L.step_budget, "remaining", remaining), redirect_stdout(io.StringIO()):
            L.main(["leaks"])
        pending = json.loads(L.STATE.read_text()).get("sqlite_pending")
        self.assertTrue(pending, "a cut full pass keeps its progress")
        self.values[SECRET_B] = "demo/B"
        out = self.scan()
        self.assertFalse(out["partial"])
        state = json.loads(L.STATE.read_text())
        self.assertNotIn("sqlite_pending", state)
        self.assertEqual(sum(r["rows_read"] for r in out["coverage"]["transcript_stores"]), 3 * L.PAGE + 1,
                         "a new value is looked for in every row, not from the old progress")


class APendingPassMissesNothing(LeakScan):
    """Confirming review of #328: a database finished earlier in a pending complete pass was re-read
    only above its progress, without the change checks of an incremental read, so a row updated in
    place meanwhile was never read, in that pass or any later one. A finished database is now read
    incrementally from where the pass read it to, with its signature; an unfinished one is resumed
    only if it is still the same file; a pass pending too long starts over; a database that will not
    open does not hold the pass open."""

    def database(self, name, rows, updated=False):
        db = Path(self.tmp.name).resolve() / name
        c = sqlite3.connect(db)
        if updated:
            c.execute("CREATE TABLE messages(body TEXT, updated_at INTEGER)")
            c.executemany("INSERT INTO messages VALUES (?, ?)", [(f"row {i}", 100) for i in range(rows)])
        else:
            c.execute("CREATE TABLE messages(body TEXT)")
            c.executemany("INSERT INTO messages VALUES (?)", [(f"row {i}",) for i in range(rows)])
        c.commit(); c.close()
        return db

    def tick(self, cut=None):
        """One pass; `cut` maps a database to the pages it may read before its time runs out."""
        cut = {str(k): v for k, v in (cut or {}).items()}
        budget = [None]
        real = L.scan_sqlite

        def scan(db, *args, **kwargs):
            budget[0] = cut.get(str(db))
            try:
                return real(db, *args, **kwargs)
            finally:
                budget[0] = None

        def remaining():
            if budget[0] is None:
                return 1000
            budget[0] -= 1
            return 1000 if budget[0] >= 0 else 0
        with patch.object(L.step_budget, "remaining", remaining), patch.object(L, "scan_sqlite", scan), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(L.main(["leaks"]), 0)
        return json.loads(L.OUT.read_text())

    def stores(self, *dbs):
        self.addCleanup(setattr, L, "transcript_stores", L.transcript_stores)
        L.transcript_stores = lambda notes: list(dbs)

    def pending(self):
        return json.loads(L.STATE.read_text()).get("sqlite_pending")

    def test_a_row_updated_in_place_in_a_finished_database_is_read(self):
        small = self.database("small.sqlite", 10, updated=True)
        big = self.database("big.sqlite", 4 * L.PAGE)
        self.stores(small, big)
        self.tick({big: 2})                        # small is finished, big is cut after two pages
        pending = self.pending()
        self.assertIn(str(small), pending["complete"])
        self.assertNotIn(str(big), pending["complete"])
        c = sqlite3.connect(small)
        c.execute("UPDATE messages SET body = ?, updated_at = 200 WHERE rowid = 3", (f"edited {SECRET_A}",))
        c.commit(); c.close()
        seen = []
        for _ in range(4):
            out = self.tick()
            seen += [h for h in out["hits"] if h["where"].startswith(str(small))]
        self.assertTrue(seen, "the row updated in place was read")
        self.assertIsNone(self.pending())

    def test_an_unfinished_database_replaced_by_another_file_is_read_from_the_start(self):
        big = self.database("big.sqlite", 4 * L.PAGE)
        self.stores(big)
        self.tick({big: 2})
        self.assertTrue(self.pending()["stores"].get(str(big)), "partly read")
        other = self.database("other.sqlite", 4 * L.PAGE)
        c = sqlite3.connect(other)
        c.execute("UPDATE messages SET body = ? WHERE rowid = 2", (f"early {SECRET_A}",))
        c.commit(); c.close()
        os.replace(other, big)
        out = self.tick()
        self.assertTrue([h for h in out["hits"] if h["where"].startswith(str(big))],
                        "a low row of the new file was read")

    def test_a_pass_pending_too_long_starts_over(self):
        big = self.database("big.sqlite", 4 * L.PAGE)
        self.stores(big)
        self.tick({big: 2})
        state = json.loads(L.STATE.read_text())
        state["sqlite_pending"]["started_on"] = "2000-01-01"
        L.STATE.write_text(json.dumps(state))
        out = self.tick()
        rows = sum(r["rows_read"] for r in out["coverage"]["transcript_stores"])
        self.assertEqual(rows, 4 * L.PAGE, "read from the start, not from the old progress")
        self.assertIsNone(self.pending())

    def test_a_database_that_will_not_open_does_not_hold_the_pass_open(self):
        good = self.database("good.sqlite", 10)
        bad = Path(self.tmp.name).resolve() / "bad.sqlite"
        bad.write_bytes(b"not a database" * 200)
        self.stores(good, bad)
        out = self.tick()
        self.assertIsNone(self.pending(), "the pass is complete with the hole named")
        self.assertIn("source-unreadable", {d["code"] for d in out["degraded"]})
        self.assertIsNotNone(json.loads(L.STATE.read_text())["sqlite"]["full_on"])


if __name__ == "__main__":
    unittest.main()
