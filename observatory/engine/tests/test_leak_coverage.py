"""PB-032: the leak scan says what it could not read, and suppression needs a reason and an expiry."""
import ast
import contextlib
import datetime
import importlib.util
import io
import json
import os
import re
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


#: The variables that move an agent's transcript home away from HOME (paths.transcript_homes).
HOME_OVERRIDES = ("CODEX_HOME", "CODEX_SQLITE_HOME", "HERMES_HOME", "HERMES_DATA_DIR_SUFFIX",
                  "XDG_DATA_HOME", "OPENCODE_DB", "CLAUDE_CONFIG_DIR")


def isolate_home(case: unittest.TestCase, user: Path) -> None:
    """HOME is a synthetic folder for the whole test: the real `~/.codex`, `~/.hermes` and
    opencode data are never read (I-5), and no override in the caller's environment leaks in."""
    user.mkdir(parents=True, exist_ok=True)
    env = patch.dict(os.environ, {"HOME": str(user)})
    env.start()
    case.addCleanup(env.stop)
    for name in HOME_OVERRIDES:
        os.environ.pop(name, None)


class Coverage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        isolate_home(self, base / "user")
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


def _raw_sqlite_uris(source: str) -> list[int]:
    """Lines of every `….connect(<uri>, uri=True)` whose URI is not built by a percent-encoding
    builder: `sqlite_uri(…)`, `readonly_uri(…)`, `<path>.as_uri() + …`, or a name that the
    enclosing function binds only to one of those (I-5 polish, F2)."""
    tree = ast.parse(source)
    parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def builds(node) -> bool:
        if isinstance(node, ast.Call):
            f = node.func
            return (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)) in ("sqlite_uri", "readonly_uri")
        while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            node = node.left
        return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "as_uri"

    def scope(node):
        while node in parent and not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node = parent[node]
        return node

    bad = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        f = call.func
        if (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)) != "connect":
            continue
        if not any(k.arg == "uri" and isinstance(k.value, ast.Constant) and k.value.value is True
                   for k in call.keywords):
            continue
        target = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "database"), None)
        if isinstance(target, ast.Name):
            values = [n.value for n in ast.walk(scope(call))
                      if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None
                      and any(isinstance(t, ast.Name) and t.id == target.id
                              for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
            ok = bool(values) and all(builds(v) for v in values)
        else:
            ok = target is not None and builds(target)
        if not ok:
            bad.append(call.lineno)
    return bad


def _sqlite(path: Path, table: str, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(f"CREATE TABLE {table}(id INTEGER PRIMARY KEY, content TEXT)")
        conn.execute(f"INSERT INTO {table}(content) VALUES (?)", (text,))
        conn.commit()


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TranscriptHomes(unittest.TestCase):
    """I-5: Codex, Hermes and OpenCode transcripts are read like Claude's, under an isolated HOME.

    Every location here is the tool's own (paths.transcript_homes names the evidence). A home that
    is absent is not a degradation; one that exists and cannot be read is named in `not_scanned`.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.user = self.base / "user"
        isolate_home(self, self.user)
        saved = {(L, "STATE"): L.STATE, (L, "OUT"): L.OUT, (L, "known_values"): L.known_values,
                 (L, "SESSIONS"): L.SESSIONS, (L, "ROOT"): L.ROOT, (L, "SUPPRESSIONS"): L.SUPPRESSIONS,
                 (paths, "COMPANION_DB"): paths.COMPANION_DB, (paths, "DB"): paths.DB,
                 (paths, "STATE"): paths.STATE, (paths, "DASHBOARD_HTML"): paths.DASHBOARD_HTML,
                 (scan_env, "salt"): scan_env.salt}
        self.addCleanup(lambda: [setattr(o, n, v) for (o, n), v in saved.items()])
        L.STATE, L.OUT = self.base / "state.json", self.base / "leak-scan.json"
        L.known_values = lambda: ({SECRET: "demo/API_TOKEN"}, [], [])
        L.SESSIONS = self.base / "no-claude-sessions"
        L.ROOT = self.base / "engine"            # no Git marker: an installed package, not a hole
        L.SUPPRESSIONS = self.base / "no-rules.json"
        paths.COMPANION_DB, paths.DB = self.base / "absent-companion.db", self.base / "absent-own.db"
        paths.STATE, paths.DASHBOARD_HTML = self.base / "runtime", self.base / "dashboard.html"
        scan_env.salt = lambda: "e" * 64

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, full=True):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(L.main(["leaks"] + (["--full"] if full else [])), 0)
        return json.loads(L.OUT.read_text())

    def wheres(self, doc):
        return [h["where"] for h in doc["hits"]]

    def holes(self, doc):
        return [n for n in doc["not_scanned"] if n.get("unreadable")]

    # ── the failing test the node names: a Codex rollout ──

    def test_a_value_planted_in_a_codex_rollout_is_a_sighting(self):
        rollout = _write(self.user / ".codex" / "sessions" / "2026" / "10" / "13"
                         / "rollout-2026-10-13T09-00-00-0199aaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl",
                         '{"type":"response_item","payload":{"output":"auth failed for ' + SECRET + '"}}\n')
        doc = self.scan()
        self.assertIn(str(rollout), self.wheres(doc), doc["not_scanned"])
        self.assertEqual(self.holes(doc), [])
        self.assertNotIn(SECRET, json.dumps(doc), "the report names the file, never the value")

    def test_codex_home_and_archived_sessions_follow_codex_home(self):
        elsewhere = self.base / "codex-elsewhere"
        os.environ["CODEX_HOME"] = str(elsewhere)
        archived = _write(elsewhere / "archived_sessions" / "rollout-2026-09-01T00-00-00-x.jsonl", SECRET)
        ignored = _write(self.user / ".codex" / "sessions" / "rollout-ignored.jsonl", SECRET)
        doc = self.scan()
        self.assertIn(str(archived), self.wheres(doc))
        self.assertNotIn(str(ignored), self.wheres(doc), "CODEX_HOME replaces ~/.codex, as it does for Codex")

    def test_a_compressed_codex_rollout_is_read_or_named(self):
        """Codex compresses finished rollouts to `.jsonl.zst` (codex-rs/rollout/src/compression.rs)."""
        try:
            from compression import zstd
        except ImportError:
            zstd = None
        path = self.user / ".codex" / "sessions" / "2026" / "10" / "01" / "rollout-old.jsonl.zst"
        path.parent.mkdir(parents=True)
        if zstd is None:
            path.write_bytes(b"\x28\xb5\x2f\xfd synthetic frame")
            doc = self.scan()
            self.assertTrue(any("rollout-old.jsonl.zst" in n["what"] and "zstd" in n["why"]
                                for n in self.holes(doc)), doc["not_scanned"])
            return
        path.write_bytes(zstd.compress(("x" * 5000 + SECRET + "\n").encode()))
        doc = self.scan()
        self.assertIn(str(path), self.wheres(doc))
        self.assertEqual(self.scan(full=False)["hits"], [], "an unchanged compressed rollout is not re-read")

    def test_without_a_zstd_decoder_a_compressed_rollout_is_a_named_hole(self):
        """Python 3.11–3.13 has no `compression.zstd`: the file is unmeasured, never clean."""
        _write(self.user / ".codex" / "sessions" / "rollout-old.jsonl.zst", "frame")
        with patch.dict(sys.modules, {"compression.zstd": None, "compression": None}):
            doc = self.scan()
        named = [n for n in self.holes(doc) if n["what"].endswith("rollout-old.jsonl.zst")]
        self.assertTrue(named and "zstd" in named[0]["why"], doc["not_scanned"])
        self.assertEqual({h["agent"]: h["state"] for h in doc["coverage"]["transcript_homes"]}["codex"],
                         "unreadable")

    def test_a_corrupt_compressed_rollout_is_a_hole(self):
        path = _write(self.user / ".codex" / "sessions" / "rollout-bad.jsonl.zst", "not a zstd frame")
        doc = self.scan()
        self.assertTrue(any(n["what"].endswith("rollout-bad.jsonl.zst") for n in self.holes(doc)),
                        doc["not_scanned"])
        self.assertIn("source-unreadable", {d["code"] for d in doc["degraded"]})
        self.assertTrue(path.exists())

    # ── Hermes: state.db, sessions/<id>.json(l), profiles ──

    def test_hermes_state_db_session_files_and_profiles_are_read(self):
        hermes = self.user / ".hermes"
        _sqlite(hermes / "state.db", "messages", f"tool error: {SECRET}")
        log = _write(hermes / "sessions" / "20261013_090000_abc.json", '{"messages":["' + SECRET + '"]}')
        diverted = _write(hermes / "sessions" / "abc.jsonl", SECRET + "\n")
        _sqlite(hermes / "profiles" / "work" / "state.db", "messages", SECRET)
        doc = self.scan()
        where = self.wheres(doc)
        self.assertIn(f"{hermes / 'state.db'}#messages.content", where)
        self.assertIn(f"{hermes / 'profiles' / 'work' / 'state.db'}#messages.content", where)
        self.assertIn(str(log), where)
        self.assertIn(str(diverted), where)
        self.assertEqual(self.holes(doc), [])

    def test_hermes_home_override_is_followed(self):
        other = self.base / "hermes-profile"
        os.environ["HERMES_HOME"] = str(other)
        _sqlite(other / "state.db", "messages", SECRET)
        self.assertIn(f"{other / 'state.db'}#messages.content", self.wheres(self.scan()))

    def test_a_store_that_will_not_open_is_named(self):
        _write(self.user / ".hermes" / "state.db", "this is not a SQLite database at all, only text")
        doc = self.scan()
        self.assertTrue(any("state.db" in n["what"] for n in doc["not_scanned"]), doc["not_scanned"])
        self.assertIn("source-unreadable", {d["code"] for d in doc["degraded"]})

    # ── OpenCode: opencode.db (and channel databases), the legacy storage tree ──

    def test_opencode_database_and_legacy_storage_are_read(self):
        data = self.user / ".local" / "share" / "opencode"
        _sqlite(data / "opencode.db", "part", SECRET)
        _sqlite(data / "opencode-beta-2.db", "part", SECRET)
        legacy = _write(data / "storage" / "part" / "msg_1" / "prt_1.json", '{"text":"' + SECRET + '"}')
        doc = self.scan()
        where = self.wheres(doc)
        self.assertIn(f"{data / 'opencode.db'}#part.content", where)
        self.assertIn(f"{data / 'opencode-beta-2.db'}#part.content", where)
        self.assertIn(str(legacy), where)

    def test_opencode_follows_xdg_data_home_and_opencode_db(self):
        xdg = self.base / "xdg"
        os.environ["XDG_DATA_HOME"] = str(xdg)
        os.environ["OPENCODE_DB"] = "custom.db"
        _sqlite(xdg / "opencode" / "custom.db", "part", SECRET)
        self.assertIn(f"{xdg / 'opencode' / 'custom.db'}#part.content", self.wheres(self.scan()))
        absolute = self.base / "elsewhere.db"
        os.environ["OPENCODE_DB"] = str(absolute)
        _sqlite(absolute, "part", SECRET)
        self.assertIn(f"{absolute}#part.content", self.wheres(self.scan()))

    # ── absence is not a hole; an unreadable home is ──

    def test_missing_homes_are_not_a_degradation(self):
        doc = self.scan()
        self.assertEqual(doc["degraded"], [])
        self.assertEqual(self.holes(doc), [])
        self.assertEqual({h["agent"]: h["state"] for h in doc["coverage"]["transcript_homes"]},
                         {"claude": "absent", "codex": "absent", "hermes": "absent", "opencode": "absent"})

    @unittest.skipIf(os.geteuid() == 0, "root lists a mode-000 folder")
    def test_a_home_that_exists_but_will_not_read_is_named_with_a_reason(self):
        for agent, folder in (("codex", self.user / ".codex"), ("hermes", self.user / ".hermes"),
                              ("opencode", self.user / ".local" / "share" / "opencode")):
            with self.subTest(agent=agent):
                (folder / "sessions").mkdir(parents=True)
                folder.chmod(0)
                try:
                    doc = self.scan()
                finally:
                    folder.chmod(0o700)
                label = "~/" + str(folder.relative_to(self.user))
                named = [n for n in self.holes(doc) if n["what"].startswith(label)]
                self.assertTrue(named and all(n["why"] for n in named), doc["not_scanned"])
                self.assertIn("source-unreadable", {d["code"] for d in doc["degraded"]})
                state = {h["agent"]: h["state"] for h in doc["coverage"]["transcript_homes"]}
                self.assertEqual(state[agent], "unreadable")

    def test_coverage_names_each_home_it_read(self):
        _write(self.user / ".codex" / "sessions" / "rollout-a.jsonl", "nothing here")
        homes = {h["agent"]: h for h in self.scan()["coverage"]["transcript_homes"]}
        self.assertEqual(homes["codex"]["state"], "scanned")
        self.assertEqual(homes["codex"]["home"], "~/.codex")
        self.assertEqual(homes["hermes"]["state"], "absent")

    # ── a rewritten JSON file is read again whole, an appended JSONL from its offset ──

    def test_a_rewritten_session_file_is_read_again_whole(self):
        log = _write(self.user / ".hermes" / "sessions" / "s1.json", '{"messages":["' + "x" * 60 + '"]}')
        self.assertEqual(self.scan()["hits"], [])
        stamp = log.stat().st_mtime_ns
        log.write_text('{"messages":["' + SECRET + '", "' + "x" * 60 + '"]}', encoding="utf-8")
        os.utime(log, ns=(stamp + 10**9, stamp + 10**9))
        self.assertIn(str(log), self.wheres(self.scan(full=False)),
                      "the value sits before the old size: an offset would have skipped it")

    # ── round 1 review: unit tier ──

    def homes(self, doc):
        return {h["home"]: h["state"] for h in doc["coverage"]["transcript_homes"]}

    def stores_read(self, doc):
        return {s["store"]: s for s in doc["coverage"]["transcript_stores"]}

    def test_a_home_the_deadline_cut_is_partial_not_scanned(self):
        """U1: nothing under the home was read, so it was not `scanned`."""
        _write(self.user / ".codex" / "sessions" / "rollout-cut.jsonl", SECRET)
        _sqlite(self.user / ".hermes" / "state.db", "messages", SECRET)
        os.environ["OBSERVATORY_STEP_DEADLINE"] = f"{__import__('time').time() + 30:.3f}"
        doc = self.scan()
        self.assertTrue(doc["partial"])
        self.assertEqual(self.homes(doc)["~/.codex"], "partial")
        self.assertEqual(self.homes(doc)["~/.hermes"], "partial")

    def test_a_symlinked_profile_and_a_link_loop_are_named(self):
        """U2: a profile that is a link, or a link loop, is a hole, never silently gone."""
        profiles = self.user / ".hermes" / "profiles"
        (profiles / "real").mkdir(parents=True)
        outside = self.base / "outside-profile"
        _sqlite(outside / "state.db", "messages", SECRET)
        (profiles / "linked").symlink_to(outside, target_is_directory=True)
        (profiles / "loop").symlink_to(profiles / "loop")
        (profiles / "stray.txt").write_text("not a profile")
        doc = self.scan()
        named = {n["what"]: n["why"] for n in self.holes(doc)}
        self.assertIn("~/.hermes/profiles/linked", named)
        self.assertIn("~/.hermes/profiles/loop", named)
        self.assertIn("symbolic link", named["~/.hermes/profiles/loop"])
        self.assertIn("~/.hermes/profiles/stray.txt", named)
        self.assertFalse(any(str(outside) in w for w in self.wheres(doc)), "a linked profile is not followed")

    def _incremental(self, mutate, db, table="part"):
        first = self.scan()
        self.assertEqual(first["hits"], [], "the first pass sees no value")
        mutate()
        doc = self.scan(full=False)
        self.assertTrue(doc["incremental"])
        self.assertIn(f"{db}#{table}.data", self.wheres(doc))

    def _opencode(self, rows, text_pk=True):
        db = self.user / ".local" / "share" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE part(id TEXT PRIMARY KEY, time_updated INTEGER NOT NULL, data TEXT NOT NULL)"
                         if text_pk else "CREATE TABLE part(id INTEGER PRIMARY KEY, data TEXT)")
            conn.executemany("INSERT INTO part(id, time_updated, data) VALUES (?, ?, ?)" if text_pk else
                             "INSERT INTO part(id, data) VALUES (?, ?)", rows)
            conn.commit()
        return db

    def test_a_store_replaced_by_one_with_a_higher_rowid_is_read_again(self):
        """U3a: a new file under the old name; the mark of the old one proves nothing. Its
        newest old row and its count are what the old file's were, so only the inode tells."""
        old = [(f"p{i}", i, "clean " * 4) for i in range(3)]
        db = self._opencode(old)

        def replace():
            fresh = db.with_name("fresh.db")
            with closing(sqlite3.connect(fresh)) as conn:
                conn.execute("CREATE TABLE part(id TEXT PRIMARY KEY, time_updated INTEGER NOT NULL, data TEXT NOT NULL)")
                conn.executemany("INSERT INTO part VALUES (?, ?, ?)",
                                 [old[0], ("p1", 1, SECRET), old[2]] + [(f"q{i}", 0, "clean") for i in range(3)])
                conn.commit()
            os.replace(fresh, db)
        self._incremental(replace, db)

    def test_a_row_inserted_below_the_mark_is_read(self):
        """U3d: a row deleted and inserted again under its old rowid, newest row untouched."""
        db = self._opencode([(i, "clean") for i in range(1, 4)], text_pk=False)

        def reinsert():
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("DELETE FROM part WHERE id = 2")
                conn.commit()
            self.assertEqual(self.scan(full=False)["hits"], [])
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("INSERT INTO part(id, data) VALUES (2, ?)", (SECRET,))
                conn.commit()
        self._incremental(reinsert, db)

    def test_a_reused_rowid_of_the_deleted_newest_row_is_read(self):
        """U3b: a TEXT primary key table hands the next row the deleted newest rowid. No update
        column here (Hermes `system_prompts`), so only the newest row's digest tells."""
        db = self.user / ".hermes" / "state.db"
        db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE system_prompts(hash TEXT PRIMARY KEY, data TEXT NOT NULL)")
            conn.executemany("INSERT INTO system_prompts VALUES (?, ?)", [(f"h{i}", "clean") for i in range(3)])
            conn.commit()

        def reuse():
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("DELETE FROM system_prompts WHERE rowid = (SELECT max(rowid) FROM system_prompts)")
                conn.execute("INSERT INTO system_prompts VALUES ('h9', ?)", (SECRET,))
                conn.commit()
                self.assertEqual(conn.execute("SELECT rowid FROM system_prompts WHERE hash='h9'").fetchone()[0], 3)
        self._incremental(reuse, db, table="system_prompts")

    def test_a_row_rewritten_in_place_is_read_again(self):
        """U3c: opencode rewrites a part while it streams and bumps `time_updated`."""
        db = self._opencode([(f"p{i}", 100 + i, "clean") for i in range(3)])

        def rewrite():
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("UPDATE part SET data = ?, time_updated = 200 WHERE id = 'p0'", (SECRET,))
                conn.commit()
        self._incremental(rewrite, db)

    def test_a_hermes_session_updated_in_place_is_read_again(self):
        """U3c: Hermes rewrites a `sessions` row (TEXT id) and moves `last_activity_at`."""
        db = self.user / ".hermes" / "state.db"
        db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT, last_activity_at REAL)")
            conn.executemany("INSERT INTO sessions VALUES (?, ?, ?)", [(f"s{i}", "t", 1.0 + i) for i in range(3)])
            conn.commit()

        def touch():
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("UPDATE sessions SET title = ?, last_activity_at = 9.0 WHERE id = 's0'", (SECRET,))
                conn.commit()
        self.assertEqual(self.scan()["hits"], [])
        touch()
        self.assertIn(f"{db}#sessions.title", self.wheres(self.scan(full=False)))

    def test_a_store_larger_than_a_page_is_read_to_the_end(self):
        """U4: pages are separate statements; the last row of the last page is still read."""
        db = self._opencode([(f"p{i:05d}", i, SECRET if i == 1234 else "clean") for i in range(1300)])
        doc = self.scan()
        self.assertIn(f"{db}#part.data", self.wheres(doc))
        self.assertEqual(self.stores_read(doc)["~/.local/share/opencode/opencode.db"]["rows_read"], 1300)

    # ── planted reverts that stayed green in round 1 ──

    @unittest.skipIf(os.geteuid() == 0, "root lists a folder without read permission")
    def test_r2d_an_unlistable_opencode_data_folder_without_storage_is_named(self):
        data = self.user / ".local" / "share" / "opencode"
        data.mkdir(parents=True)
        data.chmod(0o300)                   # searchable, not listable: storage/ is plainly absent
        try:
            doc = self.scan()
        finally:
            data.chmod(0o700)
        self.assertTrue(any(n["what"] == "~/.local/share/opencode" for n in self.holes(doc)), doc["not_scanned"])

    @unittest.skipIf(os.geteuid() == 0, "root lists a folder without read permission")
    def test_r2e_an_unlistable_hermes_profiles_folder_is_named(self):
        profiles = self.user / ".hermes" / "profiles"
        profiles.mkdir(parents=True)
        profiles.chmod(0o300)
        try:
            doc = self.scan()
        finally:
            profiles.chmod(0o700)
        self.assertTrue(any(n["what"] == "~/.hermes/profiles" for n in self.holes(doc)), doc["not_scanned"])

    def test_r3_an_unchanged_json_is_not_read_again(self):
        _write(self.user / ".hermes" / "sessions" / "s1.json", '{"messages":["' + "x" * 300 + '"]}')
        self.assertGreater(self.scan()["read_bytes"], 0)
        self.assertEqual(self.scan(full=False)["read_bytes"], 0)

    def test_r4_a_second_incremental_pass_reads_no_row_and_repeats_no_sighting(self):
        hermes = self.user / ".hermes" / "state.db"
        opencode = self.user / ".local" / "share" / "opencode" / "opencode.db"
        _sqlite(hermes, "messages", SECRET)
        _sqlite(opencode, "part", SECRET)
        first = self.scan()
        self.assertEqual(len(first["hits"]), 2)
        second = self.scan(full=False)
        self.assertEqual(second["hits"], [], "a row read once is not reported again")
        rows = self.stores_read(second)
        self.assertEqual(rows["~/.hermes/state.db"]["rows_read"], 0)
        self.assertEqual(rows["~/.local/share/opencode/opencode.db"]["rows_read"], 0)
        marks = json.loads(L.STATE.read_text())["sqlite"]["stores"]
        self.assertEqual(marks[str(hermes)], {"messages": 1})
        self.assertEqual(marks[str(opencode)], {"part": 1})

    # ── round 1 review: product tier ──

    def test_codex_and_claude_histories_and_codex_databases_are_read(self):
        codex = self.user / ".codex"
        history = _write(codex / "history.jsonl", '{"text":"' + SECRET + '"}\n')
        claude = _write(self.user / ".claude" / "history.jsonl", '{"display":"' + SECRET + '"}\n')
        thread = codex / "thread_history_1.sqlite"
        with closing(sqlite3.connect(thread)) as conn:
            conn.execute("CREATE TABLE thread_items(thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, "
                         "item_id TEXT NOT NULL, rollout_ordinal INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, "
                         "item_json TEXT NOT NULL, PRIMARY KEY (thread_id, turn_id, item_id))")
            conn.execute("CREATE TABLE thread_turns(thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, "
                         "status TEXT NOT NULL, error_json TEXT, PRIMARY KEY (thread_id, turn_id))")
            conn.execute("INSERT INTO thread_items VALUES ('t', 'u', 'i', 0, 0, ?)", ('{"text":"' + SECRET + '"}',))
            conn.execute("INSERT INTO thread_turns VALUES ('t', 'u', 'failed', ?)", (SECRET,))
            conn.commit()
        _sqlite(codex / "logs_2.sqlite", "logs", SECRET)
        _sqlite(codex / "memories_1.sqlite", "stage1_outputs", SECRET)
        _sqlite(codex / "memories_v2_1.sqlite", "stage1_outputs", SECRET)
        _write(codex / "queue_1.sqlite-wal", "not a database")     # a sidecar is not a store
        where = self.wheres(self.scan())
        for expected in (str(history), str(claude), f"{thread}#thread_items.item_json",
                         f"{thread}#thread_turns.error_json", f"{codex / 'logs_2.sqlite'}#logs.content",
                         f"{codex / 'memories_1.sqlite'}#stage1_outputs.content",
                         f"{codex / 'memories_v2_1.sqlite'}#stage1_outputs.content"):
            self.assertIn(expected, where)
        self.assertFalse(any("sqlite-wal" in w for w in where))

    def test_codex_sqlite_home_and_hermes_data_dir_suffix_are_followed(self):
        os.environ["CODEX_SQLITE_HOME"] = str(self.base / "codex-db")
        os.environ["HERMES_DATA_DIR_SUFFIX"] = "-beta"
        _sqlite(self.base / "codex-db" / "state_5.sqlite", "threads", SECRET)
        _sqlite(self.user / ".hermes-beta" / "state.db", "messages", SECRET)
        _sqlite(self.user / ".hermes" / "state.db", "messages", SECRET)
        where = self.wheres(self.scan())
        self.assertIn(f"{self.base / 'codex-db' / 'state_5.sqlite'}#threads.content", where)
        self.assertIn(f"{self.user / '.hermes-beta' / 'state.db'}#messages.content", where)
        self.assertNotIn(f"{self.user / '.hermes' / 'state.db'}#messages.content", where,
                         "the suffix replaces ~/.hermes, as it does for Hermes")

    # ── round 2 review ──

    def test_a_blob_in_an_update_column_does_not_break_the_scan(self):
        """F1: the signature kept raw bytes and the state file would not serialise."""
        db = self.user / ".local" / "share" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE part(id TEXT PRIMARY KEY, time_updated BLOB, data TEXT)")
            conn.execute("INSERT INTO part VALUES ('p0', ?, 'clean')", (b"\x00\x01binary",))
            conn.execute("INSERT INTO part VALUES ('p1', ?, 'clean')", (b"\x00\x02binary",))
            conn.commit()
        self.assertEqual(self.scan()["hits"], [])
        self.assertIn("sqlite_signatures", json.loads(L.STATE.read_text()))
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("UPDATE part SET data = ?, time_updated = ? WHERE id = 'p0'", (SECRET, b"\x00\x09new"))
            conn.commit()
        self.assertIn(f"{db}#part.data", self.wheres(self.scan(full=False)))

    def _threads(self, rows):
        db = self.user / ".codex" / "state_5.sqlite"
        db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, title TEXT NOT NULL, updated_at INTEGER NOT NULL)")
            conn.executemany("INSERT INTO threads VALUES (?, ?, ?)", rows)
            conn.commit()
        return db

    def test_an_edit_that_keeps_the_update_column_at_its_max_is_read_once(self):
        """F2: Codex `updated_at` is in seconds; an edit within the max's second does not move it."""
        db = self._threads([("t0", "clean", 1000), ("t1", "clean", 1001), ("t2", "clean", 1002)])
        self.assertEqual(self.scan()["hits"], [])
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("UPDATE threads SET title = ?, updated_at = 1002 WHERE id = 't0'", (SECRET,))
            conn.commit()
        self.assertIn(f"{db}#threads.title", self.wheres(self.scan(full=False)))
        self.assertEqual(self.scan(full=False)["hits"], [], "the row at the max is not reported every tick")

    def test_the_finest_update_column_is_preferred(self):
        self.assertLess(L.UPDATED_COLUMNS.index("updated_at_ms"), L.UPDATED_COLUMNS.index("updated_at"))

    def test_a_changed_rowid_set_with_the_same_count_is_read(self):
        """F3: ids 1,2,4,5; delete 1 and insert 3 between two passes: same count, same newest row."""
        db = self.user / ".local" / "share" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE part(id INTEGER PRIMARY KEY, data TEXT)")
            conn.executemany("INSERT INTO part VALUES (?, 'clean')", [(1,), (2,), (4,), (5,)])
            conn.commit()

        def swap():
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("DELETE FROM part WHERE id = 1")
                conn.execute("INSERT INTO part VALUES (3, ?)", (SECRET,))
                conn.commit()
        self._incremental(swap, db)

    def test_each_page_is_its_own_bounded_statement(self):
        """F5/U4: no single long read holds the database; a writer waits for one page at most."""
        db = self._opencode([(f"p{i:05d}", i, "clean") for i in range(1300)])
        seen: list[str] = []
        original = sqlite3.connect

        def traced(*args, **kwargs):
            conn = original(*args, **kwargs)
            if str(db) in str(args[0] if args else kwargs.get("database", "")):
                conn.set_trace_callback(seen.append)
            return conn
        with patch.object(sqlite3, "connect", traced):
            self.scan()
        reads = [s for s in seen if 'FROM "part" WHERE rowid >' in s]
        self.assertGreaterEqual(len(reads), 3, seen)
        self.assertTrue(all(f"LIMIT {L.PAGE}" in s for s in reads), reads)

    def test_claude_config_dir_moves_the_prompt_history(self):
        moved = _write(self.base / "claude-config" / "history.jsonl", SECRET + "\n")
        ignored = _write(self.user / ".claude" / "history.jsonl", SECRET + "\n")
        os.environ["CLAUDE_CONFIG_DIR"] = str(moved.parent)
        where = self.wheres(self.scan())
        self.assertIn(str(moved), where)
        self.assertNotIn(str(ignored), where, "CLAUDE_CONFIG_DIR replaces ~/.claude, as it does for Claude Code")

    def test_every_codex_database_name_is_globbed(self):
        """F5/F6: each pattern on its own — queue, goals and the agent message board."""
        codex = self.user / ".codex"
        names = ("state_5", "logs_2", "thread_history_1", "memories_1", "memories_v2_1", "queue_1", "goals_1",
                 "agent_message_board_1")
        for name in names:
            _sqlite(codex / f"{name}.sqlite", "t", SECRET)
        where = self.wheres(self.scan())
        for name in names:
            self.assertIn(f"{codex / (name + '.sqlite')}#t.content", where, name)

    @unittest.skipIf(os.geteuid() == 0, "root lists a mode-000 folder")
    def test_a_hole_in_a_nested_home_leaves_the_outer_home_partial(self):
        """F7: a locked folder under ~/.claude/projects is that home's hole; ~/.claude is partial."""
        projects = self.user / ".claude" / "projects"
        (projects / "locked").mkdir(parents=True)
        L.SESSIONS = projects
        history = _write(self.user / ".claude" / "history.jsonl", SECRET + "\n")
        (projects / "locked").chmod(0)
        try:
            doc = self.scan()
        finally:
            (projects / "locked").chmod(0o700)
        self.assertIn(str(history), self.wheres(doc))
        states = self.homes(doc)
        self.assertEqual(states["~/.claude/projects"], "unreadable")
        self.assertEqual(states["~/.claude"], "partial")

    # ── round 3 review ──

    @staticmethod
    def _damage(db: Path) -> None:
        """Zero the type byte of a table leaf page about three quarters into the file."""
        raw = bytearray(db.read_bytes())
        size = int.from_bytes(raw[16:18], "big") or 65536
        pages = len(raw) // size
        for page in range(int(pages * 0.75), pages + 1):
            if raw[(page - 1) * size] == 0x0D:
                raw[(page - 1) * size] = 0
                db.write_bytes(bytes(raw))
                break
        else:
            raise AssertionError("no table leaf page to damage")
        with closing(sqlite3.connect(db)) as conn, contextlib.suppress(sqlite3.DatabaseError):
            conn.execute("SELECT count(*), sum(length(data)) FROM part").fetchone()
            raise AssertionError("the damage did not reach the table")

    def _big(self, db: Path, table: str = "part", first: str | None = None) -> Path:
        db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db)) as conn:
            if first:
                conn.execute("CREATE TABLE note(id INTEGER PRIMARY KEY, data TEXT)")
                conn.execute("INSERT INTO note(data) VALUES (?)", (first,))
            conn.execute(f"CREATE TABLE {table}(id INTEGER PRIMARY KEY, data TEXT)")
            conn.executemany(f"INSERT INTO {table}(data) VALUES (?)", [("clean " + "x" * 180,)] * 2000)
            conn.commit()
        return db

    def test_a_damaged_page_is_a_hole_not_a_clean_table(self):
        """N1: any error from a table used to be read as WITHOUT ROWID and its loop broke in silence."""
        db = self._big(self.user / ".local" / "share" / "opencode" / "opencode.db", first=SECRET)
        self._damage(db)
        doc = self.scan()
        self.assertIn(f"{db}#note.data", self.wheres(doc), "the readable table still counts")
        self.assertTrue(any(n["what"] == str(db) for n in self.holes(doc)), doc["not_scanned"])
        self.assertIn("source-unreadable", {d["code"] for d in doc["degraded"]})
        self.assertEqual(self.homes(doc)["~/.local/share/opencode"], "unreadable")

    def test_a_damaged_hermes_state_db_is_unreadable_end_to_end(self):
        db = self._big(self.user / ".hermes" / "state.db")
        self._damage(db)
        doc = self.scan()
        self.assertTrue(any(n["what"] == str(db) and n["why"] for n in self.holes(doc)), doc["not_scanned"])
        self.assertEqual(self.homes(doc)["~/.hermes"], "unreadable")
        self.assertFalse(self.stores_read(doc)["~/.hermes/state.db"]["read"])

    def test_invalid_utf8_in_the_newest_row_is_read_not_skipped(self):
        db = self._opencode([(1, "clean"), (2, "clean")], text_pk=False)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("INSERT INTO part(id, data) VALUES (3, CAST(? AS TEXT))", (b"\xff\xfe" + SECRET.encode(),))
            conn.commit()
        doc = self.scan()
        self.assertIn(f"{db}#part.data", self.wheres(doc))
        self.assertEqual(self.holes(doc), [])
        self.assertNotIn(SECRET, json.dumps(doc))

    def test_invalid_utf8_in_the_update_column_keeps_the_scan_and_its_state(self):
        db = self.user / ".local" / "share" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE part(id TEXT PRIMARY KEY, time_updated TEXT, data TEXT)")
            conn.execute("INSERT INTO part VALUES ('p0', CAST(? AS TEXT), ?)", (b"\xff1", SECRET))
            conn.execute("INSERT INTO part VALUES ('p1', CAST(? AS TEXT), 'clean')", (b"\xff2",))
            conn.commit()
        self.assertIn(f"{db}#part.data", self.wheres(self.scan()))
        self.assertIn("sqlite_signatures", json.loads(L.STATE.read_text()))
        self.assertEqual(self.scan(full=False)["hits"], [], "read once, not every tick")

    def test_a_bulk_update_past_the_digest_cap_is_reported_once(self):
        """L1: 200 rows share the new maximum: one digest of the set, not 200, and no repeat."""
        db = self._opencode([(f"p{i:03d}", i, SECRET if i == 0 else "clean") for i in range(200)])
        self.assertEqual(len(self.scan()["hits"]), 1)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("UPDATE part SET time_updated = 500")
            conn.commit()
        self.assertEqual(len(self.scan(full=False)["hits"]), 1, "the bulk update is read once")
        for _ in range(2):
            idle = self.scan(full=False)
            self.assertEqual(idle["hits"], [], "an idle pass reports nothing again")
            self.assertEqual(self.stores_read(idle)["~/.local/share/opencode/opencode.db"]["rows_read"], 0)

    def test_a_rowid_sum_collision_is_read(self):
        """L2: {2,5} -> {3,4} and r -> r + 2147483647 keep the count and the sum modulo the prime."""
        for label, ids, swap in (
                ("same-sum set", [1, 2, 5, 6], ([2, 5], [(3, SECRET), (4, "clean")])),
                ("modulus move", [1, 3, 5_000_000_000], ([3], [(3 + 2147483647, SECRET)]))):
            with self.subTest(label):
                db = self.user / ".local" / "share" / "opencode" / "opencode.db"
                if db.exists():
                    db.unlink()
                db = self._opencode([(i, "clean") for i in ids], text_pk=False)
                L.STATE.unlink(missing_ok=True)

                def move(gone=swap[0], new=swap[1]):
                    with closing(sqlite3.connect(db)) as conn:
                        conn.executemany("DELETE FROM part WHERE id = ?", [(g,) for g in gone])
                        conn.executemany("INSERT INTO part(id, data) VALUES (?, ?)", new)
                        conn.commit()
                self._incremental(move, db)

    def test_a_varchar_column_is_read(self):
        """L6: `VARCHAR(255)` has TEXT affinity; the exact-name match skipped it."""
        db = self.user / ".hermes" / "state.db"
        db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, body VARCHAR(255), kind NCHAR(10))")
            conn.execute("INSERT INTO notes(body, kind) VALUES (?, 'x')", (SECRET,))
            conn.commit()
        self.assertIn(f"{db}#notes.body", self.wheres(self.scan()))

    def test_a_real_without_rowid_table_is_still_read(self):
        db = self.user / ".hermes" / "state.db"
        db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID")
            conn.execute("INSERT INTO kv VALUES ('a', ?)", (SECRET,))
            conn.commit()
        doc = self.scan()
        self.assertIn(f"{db}#kv.v", self.wheres(doc))
        self.assertEqual(self.holes(doc), [])

    # ── round 4 review ──

    def _plant_names(self, db: Path, table: bytes | None = None, column: bytes | None = None) -> None:
        """Rename a table or a column to bytes that are not UTF-8, through writable_schema."""
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("PRAGMA writable_schema=ON")
            if table is not None:
                conn.execute("UPDATE sqlite_master SET name = CAST(? AS TEXT), tbl_name = CAST(? AS TEXT), "
                             "sql = CAST(? AS TEXT) WHERE name = 'bad'",
                             (table, table, b'CREATE TABLE "' + table + b'"(id INTEGER PRIMARY KEY, data TEXT)'))
            if column is not None:
                conn.execute("UPDATE sqlite_master SET sql = CAST(? AS TEXT) WHERE name = 'cols'",
                             (b'CREATE TABLE cols(id INTEGER PRIMARY KEY, data TEXT, "' + column + b'" TEXT)',))
            conn.commit()

    def _report_written_and_other_tables_read(self, db: Path, expected: list[str]) -> dict:
        L.OUT.unlink(missing_ok=True)
        doc = self.scan()                                   # main returned 0 and wrote its report
        for where in expected:
            self.assertIn(f"{db}#{where}", self.wheres(doc))
        hole = [n for n in self.holes(doc) if n["what"] == str(db)]
        self.assertTrue(hole and hole[0]["why"].startswith(L.PARTIAL), doc["not_scanned"])
        self.assertIn("~/.codex", {h["home"] for h in doc["coverage"]["transcript_homes"]},
                      "every other home is still measured")
        return doc

    def test_a_table_name_that_is_not_utf8_is_a_hole_not_a_crash(self):
        """M1: `PRAGMA table_info("zzz\\xff")` raised UnicodeEncodeError, not a sqlite3.Error."""
        db = self.user / ".hermes" / "state.db"
        db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE bad(id INTEGER PRIMARY KEY, data TEXT)")
            conn.execute("CREATE TABLE other(id INTEGER PRIMARY KEY, data TEXT)")
            conn.execute("INSERT INTO other(data) VALUES (?)", (SECRET,))
            conn.commit()
        self._plant_names(db, table=b"zzz\xff")
        doc = self._report_written_and_other_tables_read(db, ["other.data"])
        self.assertIn("UnicodeEncodeError", next(n["why"] for n in self.holes(doc) if n["what"] == str(db)))

    def test_a_column_name_that_is_not_utf8_leaves_the_rest_of_the_table_read(self):
        """M1: `SELECT *` raised UnicodeDecodeError decoding the cursor's column names."""
        db = self.user / ".hermes" / "state.db"
        db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE cols(id INTEGER PRIMARY KEY, data TEXT, yyyq TEXT)")
            conn.execute("INSERT INTO cols(data, yyyq) VALUES (?, 'x')", (SECRET,))
            conn.execute("CREATE TABLE other(id INTEGER PRIMARY KEY, data TEXT)")
            conn.execute("INSERT INTO other(data) VALUES (?)", (SECRET,))
            conn.commit()
        self._plant_names(db, column=b"yyy\xff")
        self._report_written_and_other_tables_read(db, ["cols.data", "other.data"])

    def test_a_store_that_raises_anything_is_a_hole_and_the_report_is_written(self):
        """M1: whatever a store raises, the other homes are measured and the report is written."""
        _sqlite(self.user / ".hermes" / "state.db", "messages", SECRET)
        _write(self.user / ".codex" / "sessions" / "rollout-x.jsonl", SECRET)
        L.OUT.unlink(missing_ok=True)
        with patch.object(L, "scan_sqlite", side_effect=RuntimeError("synthetic")):
            doc = self.scan()
        self.assertTrue(any(n["what"].endswith("state.db") and "RuntimeError" in n["why"]
                            for n in self.holes(doc)), doc["not_scanned"])
        self.assertTrue(any(w.endswith("rollout-x.jsonl") for w in self.wheres(doc)))

    def _generated(self, db: Path, tables=("g", "h")) -> None:
        db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db)) as conn:
            for t in tables:
                conn.execute(f"CREATE TABLE {t}(id INTEGER PRIMARY KEY, data TEXT)")
                conn.execute(f"INSERT INTO {t}(data) VALUES (?)", (SECRET,))
                # VIRTUAL generated column added after the row: reading it raises an error whose
                # message quotes the cell (`bad JSON path: '<value>'`).
                conn.execute(f"ALTER TABLE {t} ADD COLUMN x TEXT GENERATED ALWAYS AS (json_extract('{{}}', data)) VIRTUAL")
            conn.commit()

    def test_a_generated_column_does_not_blind_its_table(self):
        """L3: `SELECT *` evaluated the generated column; main read both tables."""
        db = self.user / ".hermes" / "state.db"
        self._generated(db)
        doc = self.scan()
        self.assertIn(f"{db}#g.data", self.wheres(doc))
        self.assertIn(f"{db}#h.data", self.wheres(doc))
        self.assertEqual(self.holes(doc), [])
        self.assertNotIn(SECRET, L.OUT.read_text())

    def test_a_hole_never_carries_the_error_message(self):
        """M2: SQLite's message can quote a cell; only the error's type reaches the report."""
        db = self.user / ".hermes" / "state.db"
        _sqlite(db, "messages", "clean")

        def quoting(*_args, **_kwargs):
            raise sqlite3.OperationalError(f"bad JSON path: '{SECRET}'")
        with patch.object(L, "table_signature", side_effect=quoting):
            doc = self.scan()
        self.assertTrue(any(n["what"] == str(db) for n in self.holes(doc)), doc["not_scanned"])
        self.assertNotIn(SECRET, L.OUT.read_text())
        self.assertNotIn(SECRET, json.dumps(F.findings(doc)))
        self.assertNotIn(SECRET, L.STATE.read_text())

    def test_a_trailing_comment_does_not_hide_without_rowid(self):
        """L5: the `)` inside `/* (old) */` was taken for the end of the column list."""
        db = self.user / ".hermes" / "state.db"
        db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID /* (old) */")
            conn.execute("INSERT INTO kv VALUES ('a', ?)", (SECRET,))
            conn.commit()
        doc = self.scan()
        self.assertIn(f"{db}#kv.v", self.wheres(doc))
        self.assertEqual(self.holes(doc), [])

    def test_a_partial_store_note_says_read_in_part_with_the_table_and_type(self):
        """L7: the wording of the hole a damaged table leaves."""
        db = self._big(self.user / ".hermes" / "state.db")
        self._damage(db)
        why = next(n["why"] for n in self.holes(self.scan()) if n["what"] == str(db))
        self.assertTrue(why.startswith("a SQLite store read only in part; these tables could not be read: "), why)
        self.assertIn("part (DatabaseError)", why)
        self.assertNotIn("would not open", why)

    # ── round 5 review ──

    def _everything(self, doc) -> str:
        return L.OUT.read_text() + (L.STATE.read_text() if L.STATE.exists() else "") + json.dumps(F.findings(doc))

    def _scan_printing(self) -> tuple[dict, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(L.main(["leaks", "--full"]), 0)
        return json.loads(L.OUT.read_text()), out.getvalue()

    def test_a_schema_error_quoting_a_value_never_reaches_the_report(self):
        """F1: `malformed database schema (t) - unknown table option: '<value>'` was copied whole."""
        db = self.user / ".hermes" / "state.db"
        _sqlite(db, "t", "clean")
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("PRAGMA writable_schema=ON")
            conn.execute("UPDATE sqlite_master SET sql = ? WHERE name = 't'",
                         (f"CREATE TABLE t(id INTEGER PRIMARY KEY, content TEXT) '{SECRET}'",))
            conn.commit()
        doc, printed = self._scan_printing()
        self.assertTrue(any(n["what"] == str(db) for n in self.holes(doc)), doc["not_scanned"])
        self.assertNotIn(SECRET, self._everything(doc))
        self.assertNotIn(SECRET, printed)

    def test_an_open_error_quoting_a_value_never_reaches_the_report(self):
        db = self.user / ".hermes" / "state.db"
        _sqlite(db, "t", "clean")
        original = sqlite3.connect

        def refusing(*args, **kwargs):
            if str(db) in str(args[0] if args else ""):
                raise sqlite3.OperationalError(f"cannot open '{SECRET}'")
            return original(*args, **kwargs)
        with patch.object(sqlite3, "connect", refusing):
            doc, printed = self._scan_printing()
        self.assertTrue(any(n["what"] == str(db) and "would not open" in n["why"] for n in self.holes(doc)))
        self.assertNotIn(SECRET, self._everything(doc) + printed)

    def test_a_store_that_stops_midway_keeps_its_sightings_and_says_so(self):
        """F3: the catch-all dropped sightings already counted and said `would not open`."""
        db = self.user / ".hermes" / "state.db"
        db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE a_first(id INTEGER PRIMARY KEY, data TEXT)")
            conn.execute("INSERT INTO a_first(data) VALUES (?)", (SECRET,))
            conn.execute("CREATE TABLE z_last(id INTEGER PRIMARY KEY, data TEXT)")
            conn.commit()
        original = L.table_signature

        def stopping(conn, table, *args, **kwargs):
            if table == "z_last":
                raise RuntimeError(f"stopped near '{SECRET}'")
            return original(conn, table, *args, **kwargs)
        with patch.object(L, "table_signature", stopping):
            doc, printed = self._scan_printing()
        self.assertIn(f"{db}#a_first.data", self.wheres(doc), "a sighting counted before the stop stays")
        why = next(n["why"] for n in self.holes(doc) if n["what"] == str(db))
        self.assertEqual(why, "a SQLite store stopped while reading (RuntimeError)")
        self.assertEqual(self.stores_read(doc)["~/.hermes/state.db"]["rows_read"], 1,
                         "L5: the rows it read before it stopped, not 0")
        self.assertNotIn(SECRET, self._everything(doc) + printed)

    def test_a_column_named_rowid_does_not_shadow_the_real_rowid(self):
        """F2: `rowid TEXT` with NULL, `rowid INTEGER` duplicates, `rowid` holding text."""
        db = self.user / ".hermes" / "state.db"
        db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE a_first(id INTEGER PRIMARY KEY, data TEXT)")
            conn.execute("INSERT INTO a_first(data) VALUES (?)", (SECRET,))
            conn.execute("CREATE TABLE nul(rowid TEXT, body TEXT)")
            conn.execute("INSERT INTO nul(rowid, body) VALUES (NULL, ?)", (SECRET,))
            conn.execute("CREATE TABLE dup(rowid INTEGER, body TEXT)")
            conn.executemany("INSERT INTO dup(rowid, body) VALUES (1, ?)", [("clean",)] * 600 + [(SECRET,)])
            conn.execute("CREATE TABLE txt(rowid TEXT, body TEXT)")
            conn.execute("INSERT INTO txt(rowid, body) VALUES ('a', 'clean'), ('b', ?)", (SECRET,))
            conn.commit()
        for full in (True, False):
            with self.subTest(full=full):
                doc = self.scan(full=full) if full else self.scan(full=False)
                where = self.wheres(doc)
                if full:
                    for t in ("a_first.data", "nul.body", "dup.body", "txt.body"):
                        self.assertIn(f"{db}#{t}", where)
                    self.assertEqual(self.holes(doc), [])
                    self.assertEqual(self.stores_read(doc)["~/.hermes/state.db"]["rows_read"], 1 + 1 + 601 + 2)
                else:
                    self.assertEqual(doc["hits"], [], "and nothing is read twice")

    def test_the_rowid_sum_helper_is_type_safe(self):
        """F2: a text value reached `_mod` through a shadowed rowid and raised TypeError, which
        turned the whole store into a hole. Anything but an integer counts as SQLite counts it."""
        self.assertEqual(L._mod("a", 7), 0)
        self.assertEqual(L._mod(None, 7), 0)
        self.assertEqual(L._mod(-9, 7), -2, "SQLite's % keeps the dividend's sign")

    def test_a_table_shadowing_every_rowid_alias_is_read_whole(self):
        db = self.user / ".hermes" / "state.db"
        db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute('CREATE TABLE all3(rowid TEXT, "_ROWID_" TEXT, oid TEXT, body TEXT)')
            conn.execute("INSERT INTO all3 VALUES (NULL, NULL, NULL, ?)", (SECRET,))
            conn.commit()
        doc = self.scan()
        self.assertIn(f"{db}#all3.body", self.wheres(doc))
        self.assertEqual(self.holes(doc), [])

    def test_a_stored_generated_column_is_read(self):
        """F5: a STORED generated column holds the only whole copy; reading it evaluates nothing."""
        db = self.user / ".hermes" / "state.db"
        db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("CREATE TABLE g(id INTEGER PRIMARY KEY, a TEXT, b TEXT, "
                         "joined TEXT GENERATED ALWAYS AS (a || b) STORED)")
            conn.execute("INSERT INTO g(a, b) VALUES (?, ?)", (SECRET[:20], SECRET[20:]))
            conn.commit()
        where = self.wheres(self.scan())
        self.assertIn(f"{db}#g.joined", where)
        self.assertNotIn(f"{db}#g.a", where)

    def test_a_quote_inside_a_name_is_doubled(self):
        """F6: `_q` doubles `"`; without it the statement breaks and the table becomes a hole."""
        db = self.user / ".hermes" / "state.db"
        db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute('CREATE TABLE "we""ird"(id INTEGER PRIMARY KEY, "co""l" TEXT, updated_at INTEGER)')
            conn.execute('INSERT INTO "we""ird"("co""l", updated_at) VALUES (?, 1)', (SECRET,))
            conn.commit()
        doc = self.scan()
        self.assertIn(f'{db}#we"ird.co"l', self.wheres(doc))
        self.assertEqual(self.holes(doc), [])

    def test_the_fallback_without_rowid_parser_on_its_own(self):
        """F4/F6: SQLite before 3.37 has no pragma_table_list; the parser must agree with SQLite."""

        class NoTableList:
            def __init__(self, conn):
                self.conn = conn

            def execute(self, sql, *args):
                if "pragma_table_list" in sql:
                    raise sqlite3.OperationalError("no such table: pragma_table_list")
                return self.conn.execute(sql, *args)

        cases = [
            "CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT) without rowid",
            "CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT) /* WITHOUT ROWID */",
            "CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT) -- WITHOUT ROWID",
            "CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID /* (old) */",
            "CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT DEFAULT ') WITHOUT ROWID')",
            "CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT DEFAULT '/*') WITHOUT ROWID",
            "CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT DEFAULT '--') WITHOUT ROWID",
            'CREATE TABLE kv(k TEXT PRIMARY KEY, "v--" TEXT) WITHOUT ROWID',
            "CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT) STRICT, WITHOUT ROWID",
            "CREATE TABLE kv(k TEXT, v TEXT DEFAULT '/*') -- */ WITHOUT ROWID",
            "CREATE TABLE kv(k TEXT PRIMARY KEY, [v)x] TEXT) WITHOUT ROWID",
            "CREATE TABLE kv(k TEXT PRIMARY KEY, [it's] TEXT) WITHOUT ROWID",
            "CREATE TABLE kv(k TEXT PRIMARY KEY, `it's` TEXT) WITHOUT ROWID",
            "CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT) -- it's\nWITHOUT ROWID",
        ]
        for sql in cases:
            with self.subTest(sql=sql), closing(sqlite3.connect(":memory:")) as mem:
                mem.execute(sql)
                stored = mem.execute("SELECT sql FROM sqlite_master WHERE name = 'kv'").fetchone()[0]
                real = bool(mem.execute("SELECT wr FROM pragma_table_list WHERE name = 'kv'").fetchone()[0])
                self.assertEqual(L._without_rowid(NoTableList(mem), "kv", stored), real)

    # ── round 6 review ──

    def test_a_store_path_with_uri_characters_is_read_read_only(self):
        """N1: `file:{path}?mode=ro` cut the path at `?` or `#`, lost `mode=ro`, read nothing
        and created an empty file; `%XX` was decoded into another path."""
        for name in ("ho?me", "ho#me", "ho%41me", "ho me", "ho%2Fme"):
            with self.subTest(name=name):
                home = self.base / name / ".hermes"
                os.environ["HERMES_HOME"] = str(home)
                db = home / "state.db"
                _sqlite(db, "messages", SECRET)
                L.STATE.unlink(missing_ok=True)
                before = {p for p in self.base.rglob("*")}
                doc = self.scan()
                created = {p for p in self.base.rglob("*")} - before - {L.STATE, L.OUT}
                self.assertEqual(created, set(), "the scan creates nothing beside what it reads")
                self.assertIn(f"{db}#messages.content", self.wheres(doc))
                self.assertEqual(self.holes(doc), [])

    def test_no_sqlite_uri_is_built_from_a_raw_path(self):
        """N1, everywhere: the same `f"file:{path}?mode=…"` read Observatory's own store. An AST
        scan (polish F2): every `connect(…, uri=True)` in the engine takes a URI built by
        `sqlite_uri`, `readonly_uri` or `<path>.as_uri() + …`, or a name bound only to those."""
        engine = Path(L.__file__).resolve().parents[1]
        offenders = []
        for py in sorted(engine.rglob("*.py")):
            if "tests" in py.relative_to(engine).parts:
                continue
            offenders += [f"{py.relative_to(engine)}:{n}" for n in _raw_sqlite_uris(py.read_text(encoding="utf-8"))]
        self.assertEqual(offenders, [])

    def test_the_uri_scan_catches_every_way_of_building_a_raw_uri(self):
        """Polish F2: the line regex caught 2 of 12 forms; each of these must be named."""
        bad = [
            'sqlite3.connect(f"file:{db}?mode=ro", uri=True)',
            'u = f"file:{db}?mode=ro"\nsqlite3.connect(u, uri=True)',
            'sqlite3.connect(\n    f"file:{db}?mode=ro",\n    uri=True)',
            'sqlite3.connect("file:%s?mode=ro" % db, uri=True)',
            'sqlite3.connect("file:" + str(db) + "?mode=ro", uri=True)',
            'sqlite3.connect("file:{}?mode=ro".format(db), uri=True)',
            'sqlite3.connect(rf"file:{db}?mode=ro", uri=True)',
            'sqlite3.connect(F"file:{db}?mode=ro", uri=True)',
            'sqlite3.connect(f"""file:{db}?mode=ro""", uri=True)',
            'sqlite3.connect(f"file://{db}?mode=ro", uri=True)',
            'sqlite3.connect(database=f"file:{db}?mode=ro", uri=True)',
            '_sq.connect(f"file:{db}?mode=ro", uri=True)',
            'u = paths.sqlite_uri(db)\nif x:\n    u = f"file:{db}"\nsqlite3.connect(u, uri=True)',
        ]
        good = [
            "sqlite3.connect(paths.sqlite_uri(db), uri=True)",
            'sqlite3.connect(sqlite_uri(db, "rw"), uri=True, timeout=60)',
            "u = paths.sqlite_uri(db)\nsqlite3.connect(u, uri=True)",
            "u = readonly_uri(db)\nsqlite3.connect(u, uri=True, timeout=30)",
            'sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)',
            'sqlite3.connect(db.as_uri() + "?mode=ro" + ("&immutable=1" if x else ""), uri=True)',
            "sqlite3.connect(db)",
            "real_connect(target, *a, **k)",
        ]

        def wrapped(snippet):
            return "def f(db, x, paths, sqlite3, _sq, sqlite_uri, readonly_uri, real_connect, target, a, k):\n" + \
                "".join("    " + line + "\n" for line in snippet.splitlines())
        for snippet in bad:
            with self.subTest(bad=snippet):
                self.assertTrue(_raw_sqlite_uris(wrapped(snippet)), snippet)
        for snippet in good:
            with self.subTest(good=snippet):
                self.assertEqual(_raw_sqlite_uris(wrapped(snippet)), [], snippet)

    def test_sqlite_uri_never_touches_the_filesystem(self):
        """Polish F3: `Path.resolve()` raises RuntimeError on a symlink loop on Python 3.11
        and 3.12; the callers catch only sqlite3.Error, so a loop became a traceback."""
        with patch.object(Path, "resolve", side_effect=RuntimeError("Symlink loop")):
            uri = paths.sqlite_uri(self.base / "loop" / "state.db")
        self.assertTrue(uri.startswith("file:///") and uri.endswith("?mode=ro"), uri)

    def test_a_symlink_loop_store_is_unreadable_not_a_traceback(self):
        """Polish F3, end to end: the store under a loop is a sqlite3 failure for its caller."""
        loop = self.base / "loop"
        loop.symlink_to(loop)
        spec = importlib.util.spec_from_file_location("disk_usage_probe", ROOT / "plugins" / "disk_usage.py")
        disk_usage = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(disk_usage)
        with patch.object(paths, "DB", loop / "observatory.db"), \
                patch.object(Path, "resolve", side_effect=RuntimeError("Symlink loop")):
            self.assertEqual(disk_usage.last_measured(), {})
        with self.assertRaises(sqlite3.Error):
            sqlite3.connect(paths.sqlite_uri(loop), uri=True).execute("SELECT 1")

    def test_dot_dot_after_a_symlink_names_the_file_a_plain_open_names(self):
        """Round 8: `os.path.abspath` collapsed `C/lnk/../store.db` to `C/store.db` on paper,
        while the operating system — and the plain-path writer — follow `lnk` first and reach
        `A/B/store.db`. Reader and writer must name one file."""
        inner = self.base / "A" / "B" / "inner"
        inner.mkdir(parents=True)
        (self.base / "C").mkdir()
        (self.base / "C" / "lnk").symlink_to(inner, target_is_directory=True)
        _sqlite(self.base / "A" / "B" / "store.db", "t", "the writer's file")
        _sqlite(self.base / "C" / "store.db", "t", "a different file")
        dotted = self.base / "C" / "lnk" / ".." / "store.db"
        with closing(sqlite3.connect(dotted)) as plain:
            expected = plain.execute("SELECT content FROM t").fetchone()[0]
        with closing(sqlite3.connect(paths.sqlite_uri(dotted), uri=True)) as reader:
            self.assertEqual(reader.execute("SELECT content FROM t").fetchone()[0], expected)
        self.assertEqual(expected, "the writer's file")

    def test_the_uri_encodes_question_mark_hash_and_percent(self):
        uri = paths.sqlite_uri(Path("/s/a?b#c%d e/x.db"))
        self.assertIn("a%3Fb%23c%25d%20e", uri)
        self.assertTrue(uri.endswith("/x.db?mode=ro"), uri)

    def test_a_trailing_slash_is_dropped_as_pathlib_drops_it(self):
        """Polish F4: the URI names the normalised path; `state.db/` and `state.db` are one
        file, as `Path("state.db/")` already is. Intended, and documented in ONBOARDING."""
        self.assertEqual(paths.sqlite_uri(str(self.base / "state.db") + "/"), paths.sqlite_uri(self.base / "state.db"))

    def test_sqlite_uri_keeps_the_path_whole_and_read_only(self):
        uri = paths.sqlite_uri(Path("/tmp/ho?me#x/%41 b/state.db"))
        self.assertTrue(uri.startswith("file:///"), uri)
        self.assertTrue(uri.endswith("?mode=ro"), uri)
        self.assertEqual(uri.count("?"), 1)
        self.assertNotIn("#", uri)
        self.assertIn("%2541", uri)

    def _shadowed(self, rows: int, at_max: int | None) -> Path:
        """A table with its own column named `rowid` (so the real one is `_rowid_`)."""
        db = self.user / ".local" / "share" / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db)) as conn:
            if at_max is None:
                conn.execute("CREATE TABLE part(rowid TEXT, data TEXT)")
                conn.executemany("INSERT INTO part(rowid, data) VALUES (?, 'clean')",
                                 [(f"r{i}",) for i in range(rows)])
            else:
                conn.execute("CREATE TABLE part(rowid TEXT, time_updated INTEGER, data TEXT)")
                conn.executemany("INSERT INTO part(rowid, time_updated, data) VALUES (?, ?, 'clean')",
                                 [(f"r{i}", at_max) for i in range(rows)])
            conn.commit()
        return db

    def test_a_shadowed_rowid_newest_row_rewritten_is_read(self):
        """L1: `_row_digest` must key on the real rowid; the declared `rowid` holds text."""
        db = self._shadowed(4, at_max=None)
        self.assertEqual(self.scan()["hits"], [])
        with closing(sqlite3.connect(db)) as conn:
            conn.execute("UPDATE part SET data = ? WHERE _rowid_ = (SELECT max(_rowid_) FROM part)", (SECRET,))
            conn.commit()
        self.assertIn(f"{db}#part.data", self.wheres(self.scan(full=False)))

    def test_a_shadowed_rowid_at_max_set_is_read_once_and_edits_are_seen(self):
        """L1: the at-max rows, `_set_digest` and `at_old_max` must key on the real rowid —
        per-row digests (5 rows) and one set digest (100 rows)."""
        for size in (5, 100):
            with self.subTest(size=size):
                db = self.user / ".local" / "share" / "opencode" / "opencode.db"
                db.unlink(missing_ok=True)
                L.STATE.unlink(missing_ok=True)
                db = self._shadowed(size, at_max=7)
                self.assertEqual(self.scan()["hits"], [])
                idle = self.scan(full=False)
                self.assertEqual(idle["hits"], [])
                self.assertEqual(self.stores_read(idle)["~/.local/share/opencode/opencode.db"]["rows_read"], 0,
                                 "an unchanged at-max set is not read again")
                with closing(sqlite3.connect(db)) as conn:
                    conn.execute("UPDATE part SET data = ? WHERE rowid = 'r2'", (SECRET,))
                    conn.commit()
                self.assertIn(f"{db}#part.data", self.wheres(self.scan(full=False)), "an edit at the max is read")
                self.assertEqual(self.scan(full=False)["hits"], [], "and reported once")

    def test_a_text_mark_from_an_old_state_file_is_read_from_the_start(self):
        """L2: `int("…")` raised ValueError and blinded the store until the weekly pass."""
        db = self.user / ".hermes" / "state.db"
        _sqlite(db, "messages", SECRET)
        self.scan()
        state = json.loads(L.STATE.read_text())
        state["sqlite"]["stores"][str(db)]["messages"] = "not-a-rowid"
        L.STATE.write_text(json.dumps(state))
        doc = self.scan(full=False)
        self.assertIn(f"{db}#messages.content", self.wheres(doc))
        self.assertEqual(self.holes(doc), [])

    def test_the_table_names_the_four_agents(self):
        rows = paths.transcript_homes(sessions=self.base / "claude")
        self.assertEqual(list(dict.fromkeys(r["agent"] for r in rows)), ["claude", "codex", "hermes", "opencode"])
        self.assertTrue(all(r["evidence"] for r in rows), "each location carries its source")


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
