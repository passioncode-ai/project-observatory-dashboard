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

    def test_nothing_to_look_for_carries_its_code(self):
        L.known_values = lambda: ({}, [], [])
        doc = self.scan()
        self.assertEqual([d["code"] for d in doc["degraded"]], ["no-values"])
        self.assertEqual(doc["degraded"][0]["source"], "leak-scan:no-values")
        self.assertEqual(doc["degraded"][0]["reason"], doc["degraded"][0]["why"])
        self.assertEqual([f["type"] for f in F.findings(doc)], ["secret.leak_scan_unmeasured"])

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode-000 file")
    def test_an_unreadable_file_is_a_coded_hole_and_its_neighbour_still_counts(self):
        doc = self.scan()
        self.assertIn("source-unreadable", [d["code"] for d in doc["degraded"]])
        self.assertTrue(all(d["source"] == "leak-scan:" + d["code"] and d["reason"] for d in doc["degraded"]),
                        "health and collector.degraded read `source` and `reason`")
        self.assertFalse(doc["partial"])
        self.assertEqual(doc["coverage"]["targets_read"], 2, "both were reached; one would not open")
        self.assertIn("secret.seen_outside_its_home", {f["type"] for f in F.findings(doc)})

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode-000 file")
    def test_a_hole_holds_health_degraded_and_counts_every_unreadable_note(self):
        """Decision (review round 2): an unread source is a gap, so /health reads it as degraded."""
        L.targets = lambda days: ([self.readable, self.locked],
                                  [{"what": "~/other-home", "why": "permission denied", "unreadable": True}])
        doc = self.scan()
        hole = next(d for d in doc["degraded"] if d["code"] == "source-unreadable")
        self.assertIn("2 source(s)", hole["why"], "the unreadable note from targets() counts too")
        import degradations
        saved = paths.SCRATCH
        paths.SCRATCH = L.OUT.parent
        try:
            rows = degradations.every_collector().get(L.OUT.name) or []
        finally:
            paths.SCRATCH = saved
        self.assertTrue(rows and rows[0]["source"] == "leak-scan:source-unreadable" and rows[0]["reason"], rows)

    def _real_targets_over(self, sessions):
        """The scanner's own `targets()`, over a synthetic sessions root."""
        L.targets = self.saved[3]
        saved = L.SESSIONS
        L.SESSIONS = sessions
        self.addCleanup(setattr, L, "SESSIONS", saved)

    def _health_rows(self):
        import degradations
        saved = paths.SCRATCH
        paths.SCRATCH = L.OUT.parent
        try:
            return degradations.every_collector().get(L.OUT.name) or []
        finally:
            paths.SCRATCH = saved

    @unittest.skipIf(os.geteuid() == 0, "root lists a mode-000 folder")
    def test_a_session_folder_that_will_not_list_is_a_hole(self):
        """Review round 3: rglob skipped a folder it could not open, and /health stayed green."""
        root = self.readable.parent / "sessions"
        (root / "projA").mkdir(parents=True)
        (root / "projB").mkdir()
        (root / "projA" / "a.jsonl").write_text(f"seen {SECRET}")
        (root / "projB" / "b.jsonl").write_text(f"seen {SECRET}")
        (root / "projB").chmod(0)
        self.addCleanup(lambda: (root / "projB").exists() and (root / "projB").chmod(0o700))
        self._real_targets_over(root)
        doc = self.scan()
        hole = [d for d in doc["degraded"] if d["code"] == "source-unreadable"]
        self.assertTrue(hole, doc["degraded"])
        self.assertTrue(any(n.get("unreadable") and "projB" in n["what"] for n in doc["not_scanned"]),
                        doc["not_scanned"])
        self.assertTrue(any(r["source"] == "leak-scan:source-unreadable" for r in self._health_rows()))
        self.assertTrue(any(h["where"].endswith("a.jsonl") for h in doc["hits"]), "the readable folder still counts")

    @unittest.skipIf(os.geteuid() == 0, "root lists a mode-000 folder")
    def test_a_sessions_root_that_will_not_list_is_a_hole(self):
        root = self.readable.parent / "sessions-root"
        (root / "projA").mkdir(parents=True)
        (root / "projA" / "a.jsonl").write_text("x")
        root.chmod(0)
        self.addCleanup(lambda: root.exists() and root.chmod(0o700))
        self._real_targets_over(root)
        doc = self.scan()
        self.assertIn("source-unreadable", {d["code"] for d in doc["degraded"]})
        self.assertTrue(self._health_rows())

    def _target_sources(self):
        base = self.readable.parent
        sessions = base / "sessions"
        sessions.mkdir()
        (sessions / "ok.jsonl").write_text(SECRET)
        self._real_targets_over(sessions)
        for obj, name, value in ((paths, "STATE", base / "runtime"),
                                 (paths, "DASHBOARD_HTML", base / "dashboard.html"),
                                 (L, "ROOT", base / "engine")):
            p = patch.object(obj, name, value); p.start(); self.addCleanup(p.stop)
        L.ROOT.mkdir()
        (L.ROOT / ".git").mkdir()
        return base

    def test_unreadable_logs_keep_sibling_hit_and_degrade_health(self):
        self._target_sources()
        logs = paths.STATE / "logs"
        logs.mkdir(parents=True)
        original = os.scandir
        def denied(path):
            if Path(path) == logs:
                raise PermissionError("synthetic unreadable logs")
            return original(path)
        with patch.object(os, "scandir", side_effect=denied), patch.object(L.safe_git, "run", return_value=type("Result", (), {"returncode": 0, "stdout": ""})()):
            doc = self.scan()
        self.assertIn("source-unreadable", {d["code"] for d in doc["degraded"]})
        self.assertTrue(any(h["where"].endswith("ok.jsonl") for h in doc["hits"]))
        self.assertTrue(self._health_rows())

    def test_git_failure_is_not_misreported_as_an_installed_package(self):
        self._target_sources()
        import subprocess
        for outcome in (PermissionError("synthetic"), subprocess.TimeoutExpired("git", 1),
                        type("Result", (), {"returncode": 128, "stdout": ""})()):
            with self.subTest(outcome=type(outcome).__name__):
                args = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with patch.object(L.safe_git, "run", **args):
                    doc = self.scan()
                self.assertIn("source-unreadable", {d["code"] for d in doc["degraded"]})
                self.assertTrue(any(h["where"].endswith("ok.jsonl") for h in doc["hits"]))

    def test_source_stat_failure_is_not_silent_absence(self):
        self._target_sources()
        original = Path.stat
        def denied(path, *args, **kwargs):
            if path == L.SESSIONS:
                raise PermissionError("synthetic source stat")
            return original(path, *args, **kwargs)
        with patch.object(Path, "stat", denied), patch.object(L.safe_git, "run", return_value=type("Result", (), {"returncode": 0, "stdout": ""})()):
            files, notes = L.targets(14)
        self.assertTrue(any(n.get("unreadable") for n in notes), notes)

    def test_installed_tree_without_git_marker_is_explicitly_unscanned(self):
        self._target_sources()
        (L.ROOT / ".git").rmdir()
        with patch.object(L.safe_git, "run") as git:
            files, notes = L.targets(14)
        git.assert_not_called()
        self.assertTrue(any("not a git checkout" in n["why"] for n in notes))
        self.assertFalse(any(n.get("unreadable") for n in notes), notes)

    def test_dashboard_directory_is_explicitly_unmeasured(self):
        self._target_sources()
        paths.DASHBOARD_HTML.mkdir()
        with patch.object(L.safe_git, "run", return_value=type("Result", (), {"returncode": 0, "stdout": ""})()):
            files, notes = L.targets(14)
        self.assertNotIn(paths.DASHBOARD_HTML, files)
        self.assertTrue(any(n.get("unreadable") and "dashboard.html" in n["what"] for n in notes), notes)

    def test_tracked_directory_is_explicitly_unmeasured(self):
        self._target_sources()
        (L.ROOT / "nested").mkdir()
        result = type("Result", (), {"returncode": 0, "stdout": "nested\0"})()
        with patch.object(L.safe_git, "run", return_value=result):
            files, notes = L.targets(14)
        self.assertNotIn(L.ROOT / "nested", files)
        self.assertTrue(any(n.get("unreadable") and "nested" in n["what"] for n in notes), notes)

    def test_special_log_file_is_not_opened_or_silently_skipped(self):
        self._target_sources()
        logs = paths.STATE / "logs"; logs.mkdir(parents=True)
        special = logs / "special.jsonl"; special.write_text("synthetic")
        original = Path.stat
        import stat
        def special_stat(path, *args, **kwargs):
            if path == special:
                return os.stat_result((stat.S_IFIFO | 0o600, 0, 0, 1, 0, 0, 0, 0, 0, 0))
            return original(path, *args, **kwargs)
        with patch.object(Path, "stat", special_stat), patch.object(L.safe_git, "run", return_value=type("Result", (), {"returncode": 0, "stdout": ""})()):
            files, notes = L.targets(14)
        self.assertNotIn(special, files)
        self.assertTrue(any(n.get("unreadable") and "special.jsonl" in n["what"] for n in notes), notes)

    def test_an_unreadable_vault_slot_or_env_file_is_a_hole(self):
        """known_values() dropped an unreadable slot silently and named an unreadable env file as
        merely skipped; both are holes now (review round 3)."""
        L.known_values = lambda: ({SECRET: "demo/API_TOKEN"}, [], [
            {"what": "vault:demo/API_KEY", "why": "could not be read (PermissionError)", "unreadable": True},
            {"what": "demo/.env", "why": "unreadable now", "unreadable": True}])
        doc = self.scan()
        hole = next(d for d in doc["degraded"] if d["code"] == "source-unreadable")
        self.assertIn("3 source(s)" if os.geteuid() != 0 else "2 source(s)", hole["why"])

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode-000 file")
    def test_known_values_marks_an_unreadable_slot_and_env_file(self):
        vault = self.readable.parent / "vault"
        (vault / "demo" / "prod").mkdir(parents=True)
        slot = vault / "demo" / "prod" / "API_KEY"
        slot.write_text("synthetic-slot-" + "q" * 24)
        slot.chmod(0)
        self.addCleanup(lambda: slot.exists() and slot.chmod(0o600))
        saved_vault, saved_scratch = L.VAULT, paths.SCRATCH
        L.VAULT, paths.SCRATCH = vault, self.readable.parent / "no-scratch"
        try:
            values, homes, skipped = self.saved[2]()
        finally:
            L.VAULT, paths.SCRATCH = saved_vault, saved_scratch
        self.assertTrue(any(s.get("unreadable") and "API_KEY" in s["what"] for s in skipped), skipped)

    # ── round 4: every unread folder of the vault and the env inventory is a hole ──

    def _known_values_over(self, vault=None, scratch=None, data=None):
        saved = (L.VAULT, paths.SCRATCH, paths.DATA, L.DESTINATIONS)
        L.VAULT = vault or self.readable.parent / "no-vault"
        paths.SCRATCH = scratch or self.readable.parent / "no-scratch"
        paths.DATA = data or self.readable.parent
        L.DESTINATIONS = {}      # never the machine's own key files
        try:
            return self.saved[2]()
        finally:
            L.VAULT, paths.SCRATCH, paths.DATA, L.DESTINATIONS = saved

    @unittest.skipIf(os.geteuid() == 0, "root lists a mode-000 folder")
    def test_every_vault_folder_that_will_not_list_is_a_hole(self):
        for depth in ("root", "project", "env"):
            with self.subTest(depth=depth):
                vault = self.readable.parent / f"vault-{depth}"
                (vault / "demo" / "prod").mkdir(parents=True)
                (vault / "demo" / "prod" / "API_KEY").write_text("synthetic-slot-" + "q" * 24)
                locked = {"root": vault, "project": vault / "demo", "env": vault / "demo" / "prod"}[depth]
                locked.chmod(0)
                try:
                    _values, _homes, skipped = self._known_values_over(vault=vault)
                finally:
                    locked.chmod(0o700)
                self.assertTrue(any(k.get("unreadable") and "vault" in k["what"] for k in skipped), skipped)

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode-000 file")
    def test_an_unreadable_env_file_is_marked_unreadable(self):
        """Fails when `"unreadable": True` is dropped for an env file (round 4)."""
        scratch = self.readable.parent / "scratch-env"
        scratch.mkdir()
        envfile = self.readable.parent / "demo.env"
        envfile.write_text(f"API_TOKEN={SECRET}\n")
        envfile.chmod(0)
        self.addCleanup(lambda: envfile.exists() and envfile.chmod(0o600))
        (scratch / "env.json").write_text(json.dumps({"files": [{
            "path": "demo.env", "project": "demo", "variables": [{"name": "API_TOKEN", "class": "secret"}]}]}))
        _values, _homes, skipped = self._known_values_over(scratch=scratch)
        self.assertTrue(any(k.get("unreadable") and k["what"] == "demo.env" for k in skipped), skipped)

    def test_a_corrupt_or_unreadable_env_inventory_is_a_hole_not_a_crash(self):
        scratch = self.readable.parent / "scratch-bad"
        scratch.mkdir()
        (scratch / "env.json").write_text("{not json")
        _values, _homes, skipped = self._known_values_over(scratch=scratch)
        self.assertTrue(any(k.get("unreadable") and "env" in k["what"] for k in skipped), skipped)

    @unittest.skipIf(os.geteuid() == 0, "root lists a mode-000 folder")
    def test_a_sessions_root_under_a_locked_ancestor_is_a_hole_not_absent(self):
        parent = self.readable.parent / "locked-parent"
        (parent / "sessions" / "projA").mkdir(parents=True)
        (parent / "sessions" / "projA" / "a.jsonl").write_text("x")
        parent.chmod(0)
        self.addCleanup(lambda: parent.exists() and parent.chmod(0o700))
        self._real_targets_over(parent / "sessions")
        doc = self.scan()
        self.assertIn("source-unreadable", {d["code"] for d in doc["degraded"]})

    def test_vault_and_env_holes_raise_the_blind_finding(self):
        doc = {"known": 3, "hits": [], "not_scanned": [], "degraded": [],
               "skipped": [{"what": "vault folder ~/v/demo", "why": "could not be listed", "unreadable": True}]}
        blind = [f for f in F.findings(doc) if f["type"] == "secret.leak_scan_blind"]
        self.assertTrue(blind and blind[0]["severity"] == "warning", F.findings(doc))

    # ── round 5: env.json is read as a regular file only; a wrong shape is a hole ──

    CHILD = (
        "import json, pathlib, sys\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "sys.path[:0] = [str(root), str(root / 'tools'), str(root / 'collectors')]\n"
        "import paths, scan_leaks as L\n"
        "paths.SCRATCH = pathlib.Path(sys.argv[2]); paths.DATA = pathlib.Path(sys.argv[3])\n"
        "L.VAULT = pathlib.Path(sys.argv[3]) / 'no-vault'; L.DESTINATIONS = {}\n"
        "print(json.dumps(L.known_values()[2]))\n")

    def _env_holes_in_child(self, scratch):
        """known_values() in a child with a timeout: a FIFO must never hang the scan."""
        import subprocess
        p = subprocess.run([sys.executable, "-c", self.CHILD, str(Path(L.__file__).resolve().parents[1]),
                            str(scratch), str(self.readable.parent)],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr[-400:])
        return json.loads(p.stdout)

    def _assert_env_hole(self, skipped):
        self.assertTrue(any(k.get("unreadable") and "env.json" in k["what"] for k in skipped), skipped)

    def test_env_json_that_is_a_fifo_is_a_hole_not_a_hang(self):
        scratch = self.readable.parent / "scratch-fifo"
        scratch.mkdir()
        os.mkfifo(scratch / "env.json")
        self._assert_env_hole(self._env_holes_in_child(scratch))

    def test_env_json_that_is_a_symlink_is_a_hole(self):
        base = self.readable.parent
        cases = {}
        dangling = base / "scratch-dangling"; dangling.mkdir()
        (dangling / "env.json").symlink_to(base / "nowhere.json")
        cases["dangling"] = dangling
        loop = base / "scratch-loop"; loop.mkdir()
        (loop / "env.json").symlink_to(loop / "env.json")
        cases["loop"] = loop
        locked_dir = base / "locked-env"; locked_dir.mkdir()
        (locked_dir / "env.json").write_text("{}")
        locked_dir.chmod(0)
        self.addCleanup(lambda: locked_dir.exists() and locked_dir.chmod(0o700))
        into_locked = base / "scratch-locked"; into_locked.mkdir()
        (into_locked / "env.json").symlink_to(locked_dir / "env.json")
        cases["into a locked folder"] = into_locked
        for label, scratch in cases.items():
            with self.subTest(link=label):
                self._assert_env_hole(self._env_holes_in_child(scratch))

    def test_env_json_with_a_wrong_shape_is_a_hole_not_a_crash(self):
        shapes = [{"files": "abc"}, {"files": [1, 2]}, {"files": None},
                  {"files": [{"path": "demo.env", "project": "demo", "variables": None}]},
                  {"files": [{"path": "demo.env", "project": "demo", "variables": [{"class": "secret"}]}]},
                  {"files": [{"project": "demo", "variables": [{"name": "X", "class": "secret"}]}]}]
        for i, shape in enumerate(shapes):
            with self.subTest(shape=shape):
                scratch = self.readable.parent / f"scratch-shape-{i}"
                scratch.mkdir()
                (scratch / "env.json").write_text(json.dumps(shape))
                _values, _homes, skipped = self._known_values_over(scratch=scratch)
                self._assert_env_hole(skipped)

    def test_an_absolute_or_parent_path_in_env_json_is_a_hole_and_never_read(self):
        for i, bad in enumerate(("/etc/hosts", "../outside.env", "a/../../outside.env")):
            with self.subTest(path=bad):
                scratch = self.readable.parent / f"scratch-path-{i}"
                scratch.mkdir()
                (scratch / "env.json").write_text(json.dumps({"files": [{
                    "path": bad, "project": "demo", "variables": [{"name": "X", "class": "secret"}]}]}))
                values, homes, skipped = self._known_values_over(scratch=scratch)
                self.assertFalse(homes, "nothing outside the data folder is read")
                self.assertTrue(any(k.get("unreadable") and bad in k["what"] for k in skipped), skipped)

    # ── round 6: a listed file is read as a regular file only; the inventory's edges ──

    READ_CHILD = (
        "import pathlib, sys\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "sys.path[:0] = [str(root), str(root / 'tools'), str(root / 'collectors')]\n"
        "import scan_leaks as L\n"
        "print(repr(L._read(pathlib.Path(sys.argv[2]))))\n")

    def test_read_never_opens_a_fifo_or_follows_a_link(self):
        """`_read` of a listed env file or vault slot swapped to a FIFO used to hang (round 6)."""
        import subprocess
        base = self.readable.parent
        fifo = base / "swapped.env"
        os.mkfifo(fifo)
        link = base / "linked.env"
        link.symlink_to(self.readable)
        for target in (fifo, link):
            with self.subTest(target=target.name):
                p = subprocess.run([sys.executable, "-c", self.READ_CHILD, str(Path(L.__file__).resolve().parents[1]),
                                    str(target)], capture_output=True, text=True, timeout=30)
                self.assertEqual((p.returncode, p.stdout.strip()), (0, "None"), p.stderr[-300:])
        self.assertIsNone(L._read(Path(str(base) + "/bad\x00name")), "a NUL in a path is None, not a ValueError")

    def test_a_listed_env_file_that_is_a_fifo_is_a_hole_not_a_hang(self):
        scratch = self.readable.parent / "scratch-listed-fifo"
        scratch.mkdir()
        os.mkfifo(self.readable.parent / "listed.env")
        (scratch / "env.json").write_text(json.dumps({"files": [{
            "path": "listed.env", "project": "demo", "variables": [{"name": "X", "class": "secret"}]}]}))
        skipped = self._env_holes_in_child(scratch)
        self.assertTrue(any(k.get("unreadable") and k["what"] == "listed.env" for k in skipped), skipped)

    def test_deep_nesting_or_a_nul_path_is_an_inventory_hole(self):
        for i, raw in enumerate(("[" * 200000, json.dumps({"files": [{
                "path": "a\x00b.env", "project": "demo", "variables": [{"name": "X", "class": "secret"}]}]}))):
            with self.subTest(case=i):
                scratch = self.readable.parent / f"scratch-edge-{i}"
                scratch.mkdir()
                (scratch / "env.json").write_text(raw)
                _values, _homes, skipped = self._known_values_over(scratch=scratch)
                self._assert_env_hole(skipped)

    def test_a_path_through_a_symlinked_folder_leaving_the_data_folder_is_a_hole(self):
        base = self.readable.parent
        data, outside = base / "data-root", base / "outside"
        data.mkdir(); outside.mkdir()
        (outside / "x.env").write_text(f"API_TOKEN={SECRET}\n")
        (data / "escape").symlink_to(outside, target_is_directory=True)
        scratch = base / "scratch-escape"; scratch.mkdir()
        (scratch / "env.json").write_text(json.dumps({"files": [{
            "path": "escape/x.env", "project": "demo", "variables": [{"name": "API_TOKEN", "class": "secret"}]}]}))
        values, homes, skipped = self._known_values_over(scratch=scratch, data=data)
        self.assertEqual((values, homes), ({}, []), "nothing outside the data folder is read")
        self.assertTrue(any(k.get("unreadable") and k["what"] == "escape/x.env" for k in skipped), skipped)

    def test_an_inventory_with_a_bom_is_read(self):
        scratch = self.readable.parent / "scratch-bom"; scratch.mkdir()
        # A value the leak scan searches for (`searchable`): SECRET reads as words.
        value = "Qx7mT2vR" + "9pL4kW8n" + "Z3bY6cH1"
        (self.readable.parent / "bom.env").write_text(f"API_TOKEN={value}\n")
        (scratch / "env.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"files": [{
            "path": "bom.env", "project": "demo", "variables": [{"name": "API_TOKEN", "class": "secret"}]}]}).encode())
        values, _homes, skipped = self._known_values_over(scratch=scratch)
        self.assertEqual(list(values.values()), ["env:demo/API_TOKEN"], skipped)

    def test_no_values_says_why_when_values_were_found_but_none_is_searchable(self):
        L.known_values = lambda: ({}, [], [{"what": "demo/.env:API_TOKEN", "why": "shorter than 12 characters"}])
        out = io.StringIO()
        with redirect_stdout(out):
            L.main(["leaks", "--full"])
        doc = json.loads(L.OUT.read_text())
        why = doc["degraded"][0]["why"]
        self.assertNotIn("no env scan", why)
        self.assertIn("searchable", why)
        self.assertNotIn("no env scan", out.getvalue())

    # ── round 7 ──

    def test_a_lone_surrogate_in_a_path_is_a_shape_hole_not_a_crash(self):
        for i, bad in enumerate(("\\udcff.env", "\\ud800x.env")):
            with self.subTest(path=bad):
                scratch = self.readable.parent / f"scratch-surrogate-{i}"
                scratch.mkdir()
                # Written as JSON text with the escape, as a damaged inventory would hold it.
                (scratch / "env.json").write_text('{"files": [{"path": "' + bad + '", "project": "demo", '
                                                  '"variables": [{"name": "X", "class": "secret"}]}]}')
                _values, _homes, skipped = self._known_values_over(scratch=scratch)
                self._assert_env_hole(skipped)
                self.assertFalse(any("outside the data folder" in k.get("why", "") for k in skipped), skipped)
                json.dumps(skipped).encode("utf-8")          # what atomic.write_json needs

    def test_the_read_is_checked_by_the_open_descriptor_not_a_path_check(self):
        """A folder swapped for a link after any path check: the descriptor's real path decides (round 7)."""
        base = self.readable.parent
        data, outside = base / "data-race", base / "outside-race"
        (data / "proj").mkdir(parents=True); outside.mkdir()
        (outside / "x.env").write_text("API_TOKEN=" + "Qx7mT2vR" + "9pL4kW8n" + "Z3bY6cH1" + "\n")
        (data / "proj" / "x.env").write_text("API_TOKEN=" + "Wm4nB8qZ" + "2xK7pL9v" + "R3tY6hC1" + "\n")
        scratch = base / "scratch-race"; scratch.mkdir()
        (scratch / "env.json").write_text(json.dumps({"files": [{
            "path": "proj/x.env", "project": "demo", "variables": [{"name": "API_TOKEN", "class": "secret"}]}]}))
        real_open = os.open

        def swapping_open(path, flags, *a, **k):
            # The swap lands between every check and the read.
            if str(path).endswith("proj/x.env") and not (data / "proj").is_symlink():
                (data / "proj" / "x.env").unlink(); (data / "proj").rmdir()
                (data / "proj").symlink_to(outside, target_is_directory=True)
            return real_open(path, flags, *a, **k)
        with patch.object(os, "open", swapping_open):
            values, _homes, skipped = self._known_values_over(scratch=scratch, data=data)
        self.assertEqual(values, {}, "a value read through the swapped link is not loaded")
        self.assertTrue(any(k.get("unreadable") and k["what"] == "proj/x.env" for k in skipped), skipped)

    def test_a_case_variant_data_path_is_not_a_false_hole(self):
        base = self.readable.parent
        (base / "Data-Case").mkdir()
        (base / "Data-Case" / "x.env").write_text("API_TOKEN=" + "Qx7mT2vR" + "9pL4kW8n" + "Z3bY6cH1" + "\n")
        variant = Path(str(base / "Data-Case").replace("Data-Case", "data-case"))
        if not variant.exists():
            self.skipTest("a case-sensitive volume: no case variant to test")
        scratch = base / "scratch-case"; scratch.mkdir()
        (scratch / "env.json").write_text(json.dumps({"files": [{
            "path": "x.env", "project": "demo", "variables": [{"name": "API_TOKEN", "class": "secret"}]}]}))
        values, _homes, skipped = self._known_values_over(scratch=scratch, data=variant)
        self.assertEqual(list(values.values()), ["env:demo/API_TOKEN"], skipped)

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode-0311 folder")
    def test_a_traverse_only_data_folder_is_still_verified(self):
        """L6: a data folder at mode 0311 cannot be opened for reading; its canonical
        path still comes from the OS (O_EVTONLY / O_PATH), never from realpath."""
        base = self.readable.parent
        data = base / "data-0311"; data.mkdir()
        (data / "x.env").write_text("API_TOKEN=" + "Qx7mT2vR" + "9pL4kW8n" + "Z3bY6cH1" + "\n")
        scratch = base / "scratch-0311"; scratch.mkdir()
        (scratch / "env.json").write_text(json.dumps({"files": [{
            "path": "x.env", "project": "demo", "variables": [{"name": "API_TOKEN", "class": "secret"}]}]}))
        data.chmod(0o311)
        self.addCleanup(lambda: data.exists() and data.chmod(0o700))
        values, _homes, skipped = self._known_values_over(scratch=scratch, data=data)
        self.assertEqual(list(values.values()), ["env:demo/API_TOKEN"], skipped)

    def test_an_unverifiable_data_folder_is_a_hole_not_a_realpath_guess(self):
        base = self.readable.parent
        data = base / "data-unverifiable"; data.mkdir()
        (data / "x.env").write_text("API_TOKEN=" + "Qx7mT2vR" + "9pL4kW8n" + "Z3bY6cH1" + "\n")
        scratch = base / "scratch-unverifiable"; scratch.mkdir()
        (scratch / "env.json").write_text(json.dumps({"files": [{
            "path": "x.env", "project": "demo", "variables": [{"name": "API_TOKEN", "class": "secret"}]}]}))
        with patch.object(L, "_fd_path", lambda fd: None):
            values, _homes, skipped = self._known_values_over(scratch=scratch, data=data)
        self.assertEqual(values, {})
        self.assertTrue(any(k.get("unreadable") and k["what"] == "x.env" for k in skipped), skipped)

    def test_inside_walks_ancestors_without_following_links(self):
        """M-G2 (round 9): the descriptor reports `o/dir/secret.env`; swapping `o/dir`
        for a link to the data folder must not make that path count as inside."""
        base = self.readable.parent
        data = base / "data-mg2"; data.mkdir()
        outside = base / "o" / "dir"; outside.mkdir(parents=True)
        (outside / "secret.env").write_text("x")
        real = str((outside / "secret.env").resolve())
        self.assertFalse(L._inside(real, data), "before the swap: outside")
        outside.rename(base / "o" / "dir-orig")
        (base / "o" / "dir").symlink_to(data, target_is_directory=True)
        self.assertFalse(L._inside(real, data), "after the swap: still outside — an ancestor that is a link "
                                                "is not the data folder")
        self.assertTrue(L._inside(str((data / "inner.env").resolve()), data), "a real path inside is inside")

    def test_a_lone_surrogate_in_any_label_never_breaks_the_report(self):
        """L7: a non-UTF-8 name on Linux decodes to a lone surrogate; the report escapes it."""
        bad = Path(str(self.readable.parent) + "/sess\udcffion.jsonl")
        L.targets = lambda days: ([self.readable, bad], [{"what": "session folder ~/p\udcffq", "why": "x",
                                                          "unreadable": True}])
        doc = self.scan()
        text = json.dumps(doc)
        text.encode("utf-8")
        self.assertIn("\\\\udcff", text, "the surrogate is written escaped, so it stays visible")

    def test_no_values_names_an_inventory_without_secrets_and_an_empty_vault(self):
        scratch = self.readable.parent / "scratch-nosecret"; scratch.mkdir()
        (scratch / "env.json").write_text(json.dumps({"files": [{
            "path": "a.env", "project": "demo", "variables": [{"name": "PORT", "class": "config"}]}]}))
        vault = self.readable.parent / "empty-vault"; vault.mkdir()
        saved = (L.VAULT, paths.SCRATCH, L.DESTINATIONS)
        L.known_values = self.saved[2]
        L.VAULT, paths.SCRATCH, L.DESTINATIONS = vault, scratch, {}
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                L.main(["leaks", "--full"])
        finally:
            L.VAULT, paths.SCRATCH, L.DESTINATIONS = saved
        why = json.loads(L.OUT.read_text())["degraded"][0]["why"]
        self.assertNotIn("no env scan", why + out.getvalue())
        self.assertIn("no secret", why)
        self.assertIn("vault", why)

    def test_holes_alone_still_raise_blind_and_say_why_nothing_was_known(self):
        doc = {"known": 0, "hits": [], "not_scanned": [],
               "degraded": [{"code": "no-values", "why": "nothing to look for"}],
               "skipped": [{"what": "vault:demo", "why": "could not be listed", "unreadable": True},
                           {"what": "env inventory (env.json)", "why": "not a regular file", "unreadable": True}]}
        rows = {f["type"]: f for f in F.findings(doc)}
        self.assertIn("secret.leak_scan_unmeasured", rows)
        self.assertIn("secret.leak_scan_blind", rows)
        detail = rows["secret.leak_scan_unmeasured"]["detail"]
        self.assertNotIn("no env scan, no vault", detail)
        self.assertIn("2", detail)

    def test_vault_unlistable_subtree_keeps_readable_values_and_marks_hole(self):
        vault = self.readable.parent / "vault"
        for project in ("good", "blocked"):
            slot = vault / project / "prod" / "KEY"
            slot.parent.mkdir(parents=True)
            slot.write_text("sk-synthetic-" + project + "a" * 32)
        original = os.scandir
        def denied(path):
            if Path(path) == vault / "blocked":
                raise PermissionError("synthetic body must not be copied")
            return original(path)
        with patch.object(L, "VAULT", vault), patch.object(paths, "SCRATCH", vault / "missing"), patch.object(L, "DESTINATIONS", {}), patch.object(os, "scandir", side_effect=denied):
            values, homes, skipped = self.saved[2]()
        self.assertEqual(list(values.values()), ["vault:good/prod/KEY"])
        self.assertTrue(any(n.get("unreadable") and n["what"] == "vault:blocked" for n in skipped), skipped)
        self.assertNotIn("synthetic body", json.dumps(skipped))

    def test_vault_discovery_boundaries_are_explicit_without_reading(self):
        vault = self.readable.parent / "vault"; vault.mkdir()
        external = self.readable.parent / "outside-vault"; external.mkdir()
        (vault / "linked").symlink_to(external, target_is_directory=True)
        special = vault / "project" / "prod" / "FIFO"
        special.parent.mkdir(parents=True); os.mkfifo(special)
        with patch.object(L, "VAULT", vault):
            notes = []; slots = list(L.vault_slots(notes))
            self.assertEqual(slots, [])
            self.assertEqual({n["what"] for n in notes}, {"vault:linked", "vault:project/prod/FIFO"})
            notes = []; self.assertEqual(list(L.vault_slots(notes, max_entries=0)), [])
            self.assertTrue(any("limit reached" in n["why"] and n["unreadable"] for n in notes))

    def test_session_directory_symlink_is_unmeasured_without_following(self):
        base = self._target_sources()
        external = base / "external"; external.mkdir()
        (external / "outside.jsonl").write_text(SECRET)
        link = L.SESSIONS / "project-link"; link.symlink_to(external, target_is_directory=True)
        with patch.object(L.safe_git, "run", return_value=type("Result", (), {"returncode": 0, "stdout": ""})()):
            files, notes = L.targets(14)
        self.assertTrue(any(n.get("unreadable") and "project-link" in n["what"] for n in notes), notes)
        self.assertTrue(any(p.name == "ok.jsonl" for p in files))
        self.assertFalse(any(p.name == "outside.jsonl" for p in files))

    def test_the_report_never_holds_the_value(self):
        self.assertNotIn(SECRET, json.dumps(self.scan()))


def _doc(codes, hit, known=3):
    """A leak-scan document as `scan_leaks.main` writes it, for the decision alone (ENG-001)."""
    why = {"no-values": "nothing to look for", "deadline-targets": "the step's time ran out after 1 of 4 targets",
           "deadline-store": "the step's time ran out before ~/store.db was read",
           "source-unreadable": "1 source could not be read"}
    return {"known": 0 if "no-values" in codes else known, "targets": 4,
            "hits": [{"secret": "demo/API_TOKEN", "where": "/tmp/t.jsonl", "occurrences": 1}] if hit else [],
            "coverage": {"targets": 4, "targets_read": 1 if "deadline-targets" in codes else 4,
                         "stores_unread": ["~/store.db"] if "deadline-store" in codes else []},
            "degraded": [{"code": c, "why": why[c], "effect": "UNMEASURED"} for c in codes]}


class PartialScanNeverHidesSightings(unittest.TestCase):
    """ENG-001: a scan the deadline cut short still reports what it saw."""

    def types(self, doc):
        return {(f["type"], f["severity"]) for f in F.findings(doc)}

    def test_a_partial_scan_with_a_credential_hit_is_critical_and_partial(self):
        got = self.types(_doc(["deadline-targets"], hit=True))
        self.assertIn(("secret.seen_outside_its_home", "critical"), got)
        self.assertIn(("secret.leak_scan_partial", "warning"), got)
        self.assertNotIn("secret.leak_scan_unmeasured", {t for t, _ in got})

    def test_every_code_combination_keeps_the_sighting(self):
        import itertools
        codes = ["no-values", "deadline-targets", "deadline-store", "source-unreadable"]
        for k in range(len(codes) + 1):
            for combo in itertools.combinations(codes, k):
                for hit in (True, False):
                    with self.subTest(codes=combo, hit=hit):
                        got = {t for t, _ in self.types(_doc(list(combo), hit))}
                        self.assertEqual("secret.seen_outside_its_home" in got, hit,
                                         "a sighting is reported under every combination")
                        self.assertEqual("secret.leak_scan_unmeasured" in got, "no-values" in combo)
                        self.assertEqual("secret.leak_scan_partial" in got,
                                         "no-values" not in combo
                                         and any(c.startswith("deadline-") for c in combo))

    def test_the_partial_finding_counts_what_was_read(self):
        row = next(f for f in F.findings(_doc(["deadline-targets", "deadline-store"], hit=True))
                   if f["type"] == "secret.leak_scan_partial")
        self.assertEqual(row["subject"], "estate:leak-scan-coverage")
        self.assertEqual(row["title_args"], {"read": 1, "n": 4, "stores": 1})
        self.assertIn("real", row["action"])

    def test_a_cut_scan_from_an_older_scanner_never_claims_it_read_everything(self):
        """The live document of a scanner before ENG-001 has no `targets_read` (review round 1)."""
        doc = _doc(["deadline-targets"], hit=True)
        doc["targets"] = doc["coverage"]["targets"] = 1000
        del doc["coverage"]["targets_read"], doc["coverage"]["stores_unread"]
        for d in doc["degraded"]:
            d.pop("code")
        # An older scanner wrote the store's absolute path into its prose.
        doc["degraded"].append({"why": f"the step's time ran out before {Path.home()}/ws/store.db was read"})
        row = next(f for f in F.findings(doc) if f["type"] == "secret.leak_scan_partial")
        self.assertEqual(row["title_id"], "the leak scan was cut short; this scanner version recorded no coverage")
        self.assertNotIn("read", row["title_args"])
        self.assertNotIn("1,000 of 1,000", row["title"] + row["detail"])
        self.assertNotIn(str(Path.home()) + "/", row["detail"], "a legacy why passes through the home-relative label")
        self.assertIn("~/ws/store.db", row["detail"])
        self.assertIn("any sightings reported beside this", row["action"])
        self.assertIn("secret.seen_outside_its_home", {f["type"] for f in F.findings(doc)})

    def test_no_values_is_unmeasured_only(self):
        self.assertEqual(self.types(_doc(["no-values"], hit=False)),
                         {("secret.leak_scan_unmeasured", "warning")})

    def test_a_legacy_document_without_codes_or_known_behaves_as_before(self):
        rows = F.findings({"degraded": [{"why": "no values"}], "hits": []})
        self.assertEqual([r["type"] for r in rows], ["secret.leak_scan_unmeasured"])

    def test_a_legacy_deadline_document_with_known_values_keeps_its_sighting(self):
        doc = _doc(["deadline-targets"], hit=True)
        for d in doc["degraded"]:
            d.pop("code")
        got = {t for t, _ in self.types(doc)}
        self.assertIn("secret.seen_outside_its_home", got)
        self.assertIn("secret.leak_scan_partial", got)


if __name__ == "__main__":
    unittest.main()
