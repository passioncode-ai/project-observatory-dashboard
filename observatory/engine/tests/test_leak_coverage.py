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
