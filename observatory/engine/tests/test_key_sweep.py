#!/usr/bin/env python3
"""The key sweep over the estate's repositories (OBS-554, CP-08, decision D3).

Every repository here is made by the test in a temporary folder, every value is fake, and the external
scanner is a fake runner that writes the report a gitleaks run would write: nothing reaches a network,
and the stored files are read back to show the value, its line and its plain hash are in none of them.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "collectors"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir                                                # noqa: E402
import key_sweep as ks                                              # noqa: E402
import key_sweep_findings as kf                                     # noqa: E402

KEY = b"test-workspace-fingerprint-salt-0123456789"
VALUE = "FAKE-sweep-value-0123456789-abcdefghijkl"
NEW = "FAKE-sweep-value-rotated-9876543210-zyxw"
OTHER = "FAKE-other-shape-only-ABCDEFGHIJ-0123456789"
SLOT = "vault:alpha-web/prod/API_KEY"
TODAY = "2026-11-16"


def git(repo: pathlib.Path, *args: str) -> str:
    p = subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                        "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
                       cwd=repo, check=True, capture_output=True, text=True)
    return p.stdout.strip()


def commit(repo: pathlib.Path, files: dict[str, str | None], message: str = "change") -> str:
    for rel, text in files.items():
        path = repo / rel
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def new_repo(files: dict[str, str]) -> pathlib.Path:
    repo = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-sweep-"))
    git(repo, "init", "-q")
    commit(repo, files, "first")
    return repo


class Clock:
    """A monotonic clock that moves one second each time it is read."""

    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


def sweep(repo, values, *, previous=None, state=None, settled=None, today=TODAY, **kw):
    repos = [{"repo": "alpha-web", "project": "project:alpha-web", "path": repo}]
    return ks.run_pass(repos, values, KEY, previous=previous, state=state, settled=settled, today=today,
                       at=today + "T10:00:00Z", **kw)


def stored_text(*docs) -> str:
    return "\n".join(json.dumps(d, sort_keys=True) for d in docs)


def assert_clean(test: unittest.TestCase, text: str, *values: str) -> None:
    for v in values:
        test.assertNotIn(v, text)
        test.assertNotIn(hashlib.sha256(v.encode()).hexdigest(), text)
        test.assertNotIn(v[8:24], text, "not even a part of the value")


class KnownValueSweep(unittest.TestCase):
    def test_a_planted_key_is_found_in_the_tree_and_in_history_by_fingerprint_and_place(self):
        repo = new_repo({"app/config.py": "# settings\nAPI_KEY = '" + VALUE + "'\n"})
        head = git(repo, "rev-parse", "HEAD")
        doc, state = sweep(repo, {VALUE: SLOT})
        [s] = doc["sightings"]
        self.assertEqual(s["rules"], [ks.KNOWN])
        self.assertEqual(s["slot"], SLOT)
        self.assertEqual(s["slot_state"], "current")
        self.assertTrue(s["in_tree"])
        self.assertEqual(s["status"], "open")
        self.assertTrue(s["fingerprint"].startswith("hmac-sha256:"))
        places = {(p["where"], p["path"], p["line"], p.get("commit")) for p in s["places"]}
        self.assertEqual(places, {("tree", "app/config.py", 2, None), ("history", "app/config.py", 2, head)})
        self.assertTrue(doc["complete"])
        [r] = doc["repositories"]
        self.assertEqual((r["history_through"], r["sightings_by_rule"], r["open"]), (head, {ks.KNOWN: 1}, 1))
        text = stored_text(doc, state)
        assert_clean(self, text, VALUE)
        self.assertNotIn("API_KEY = ", text, "the matched line is never kept")

    def test_the_same_value_in_two_files_is_one_sighting(self):
        repo = new_repo({"a.env.example": f"KEY={VALUE}\n", "b/notes.md": f"use {VALUE} here\n"})
        doc, _ = sweep(repo, {VALUE: SLOT})
        self.assertEqual(len(doc["sightings"]), 1)
        self.assertEqual({p["path"] for p in doc["sightings"][0]["places"] if p["where"] == "tree"},
                         {"a.env.example", "b/notes.md"})

    def test_a_value_removed_from_the_tree_stays_in_history(self):
        repo = new_repo({"config.py": f"KEY = '{VALUE}'\n"})
        first = git(repo, "rev-parse", "HEAD")
        commit(repo, {"config.py": "KEY = read_from_vault()\n"})
        doc, _ = sweep(repo, {VALUE: SLOT})
        [s] = doc["sightings"]
        self.assertFalse(s["in_tree"])
        self.assertEqual([(p["where"], p["commit"]) for p in s["places"]], [("history", first)])
        self.assertEqual(s["status"], "open", "removing it from the tree does not unpublish it")

    def test_ignored_files_are_not_the_repositorys(self):
        repo = new_repo({".gitignore": ".env\n", "README.md": "hello\n"})
        (repo / ".env").write_text(f"KEY={VALUE}\n", encoding="utf-8")
        doc, _ = sweep(repo, {VALUE: SLOT})
        self.assertEqual(doc["sightings"], [])

    def test_an_untracked_file_git_would_commit_is_read(self):
        repo = new_repo({"README.md": "hello\n"})
        (repo / "draft.txt").write_text(f"{VALUE}\n", encoding="utf-8")
        doc, _ = sweep(repo, {VALUE: SLOT})
        self.assertEqual([p["path"] for p in doc["sightings"][0]["places"]], ["draft.txt"])


class Budget(unittest.TestCase):
    def test_the_history_resumes_after_the_budget_ran_out(self):
        values = {f"FAKE-resume-value-{i:02d}-abcdefghijklmnop": f"vault:alpha-web/prod/K{i}" for i in range(6)}
        repo = new_repo({"README.md": "start\n"})
        shas = [commit(repo, {f"f{i}.txt": f"v = {v}\n"}) for i, v in enumerate(values)]
        doc, state = sweep(repo, values, clock=Clock(), pass_budget=8)
        self.assertFalse(doc["complete"])
        partial = doc["repositories"][0]
        self.assertNotEqual(partial["history_through"], shas[-1])
        self.assertTrue(any(d["code"] == "out-of-time" for d in partial["degraded"]))
        reads, passes = partial["commits_read"], 1
        while not doc["complete"] and passes < 20:
            doc, state = sweep(repo, values, previous=doc, state=state, clock=Clock(), pass_budget=8)
            passes += 1
        self.assertTrue(doc["complete"])
        self.assertGreater(passes, 1)
        self.assertEqual(doc["repositories"][0]["history_through"], shas[-1])
        self.assertEqual(doc["repositories"][0]["commits_read"], 7, "each commit read once, the first included")
        self.assertLess(reads, 7)
        self.assertEqual(len(doc["sightings"]), 6)
        assert_clean(self, stored_text(doc, state), *values)

    def test_a_spent_day_reads_nothing_until_tomorrow(self):
        repo = new_repo({"config.py": f"KEY = '{VALUE}'\n"})
        doc, state = sweep(repo, {VALUE: SLOT}, state={"day": TODAY, "budget_left_s": 0})
        self.assertEqual(doc["sightings"], [])
        self.assertIn("budget-spent", {d["code"] for d in doc["degraded"]})
        doc, _ = sweep(repo, {VALUE: SLOT}, state=state, today="2026-11-17")
        self.assertEqual(len(doc["sightings"]), 1)

    def test_a_new_value_makes_the_history_read_again(self):
        repo = new_repo({"config.py": f"KEY = '{VALUE}'\n"})
        commit(repo, {"config.py": "KEY = None\n"})
        doc, state = sweep(repo, {})
        self.assertEqual(doc["sightings"], [])
        doc, state = sweep(repo, {VALUE: SLOT}, previous=doc, state=state)
        self.assertEqual([p["where"] for p in doc["sightings"][0]["places"]], ["history"])


def fake_scanner(repo: pathlib.Path, calls: list, *, file: str = "settings.ini"):
    """A runner that writes what a redacted gitleaks run writes, with the secret fields filled in as an
    unredacted run would, so the test proves they are dropped rather than never sent."""
    def run(argv, **kw):
        calls.append({"argv": list(argv), "report": argv[argv.index("-r") + 1]})
        mode = argv[1]
        rows = []
        if mode == "dir" and (repo / file).exists() and OTHER in (repo / file).read_text():
            line = (repo / file).read_text().split("\n")[1]
            rows.append({"RuleID": "generic-api-key", "File": str(repo / file), "StartLine": 2, "EndLine": 2,
                         "StartColumn": 1, "EndColumn": len(line), "Secret": OTHER, "Match": line, "Line": line,
                         "Entropy": 4.2, "Fingerprint": f"{file}:generic-api-key:2"})
        if mode == "git":
            sha = git(repo, "rev-list", "--max-parents=0", "HEAD")
            line = f'token = "{OTHER}"'
            rows.append({"RuleID": "generic-api-key", "File": file, "StartLine": 2, "EndLine": 2, "StartColumn": 1,
                         "EndColumn": len(line), "Commit": sha, "Secret": OTHER, "Match": line, "Line": line,
                         "Author": "t", "Email": "t@example.invalid"})
        with open(calls[-1]["report"], "w", encoding="utf-8") as fh:
            json.dump(rows, fh)
        return subprocess.CompletedProcess(argv, 0)
    return run


class ExternalScanner(unittest.TestCase):
    def test_absent_scanner_is_not_a_failure(self):
        self.assertIsNone(ks.find_scanner(which=lambda name: None))
        self.assertTrue(ks.find_scanner(which=lambda name: "/opt/bin/" + name if name == "betterleaks" else None)
                        .endswith("betterleaks"))
        repo = new_repo({"config.py": f"KEY = '{VALUE}'\n"})
        doc, _ = sweep(repo, {VALUE: SLOT}, scanner=None)
        self.assertIsNone(doc["scanners"]["external"])
        self.assertEqual(doc["repositories"][0]["degraded"], [])
        self.assertEqual(len(doc["sightings"]), 1)

    def test_the_report_is_redacted_read_then_deleted_and_its_secret_fields_never_kept(self):
        repo = new_repo({"settings.ini": f'[api]\ntoken = "{OTHER}"\n'})
        calls: list = []
        doc, state = sweep(repo, {}, scanner="/opt/bin/gitleaks", runner=fake_scanner(repo, calls))
        self.assertEqual([c["argv"][1] for c in calls], ["dir", "git"])
        for c in calls:
            self.assertIn("--redact", c["argv"])
            self.assertFalse(os.path.exists(c["report"]), "the report is deleted")
            self.assertFalse(os.path.exists(os.path.dirname(c["report"])), "and its private folder")
            self.assertNotIn(OTHER, " ".join(c["argv"]))
        [s] = doc["sightings"]
        self.assertEqual((s["rules"], s["digest_of"], s["slot"]), (["generic-api-key"], "match", None))
        self.assertEqual({p["where"] for p in s["places"]}, {"tree", "history"}, "one value, one sighting")
        self.assertEqual(doc["scanners"]["external"], "gitleaks")
        text = stored_text(doc, state, kf.findings(doc))
        assert_clean(self, text, OTHER)
        for field in ("Entropy", "Author", "t@example.invalid", "token = "):
            self.assertNotIn(field, text)

    def test_a_scanner_that_fails_is_named_and_its_report_still_deleted(self):
        repo = new_repo({"README.md": "x\n"})
        reports = []

        def broken(argv, **kw):
            reports.append(argv[argv.index("-r") + 1])
            raise subprocess.TimeoutExpired(argv, 1)
        doc, _ = sweep(repo, {}, scanner="/opt/bin/gitleaks", runner=broken)
        self.assertIn("external-scanner", {d["code"] for d in doc["repositories"][0]["degraded"]})
        self.assertTrue(reports and not any(os.path.exists(r) for r in reports))

    def test_a_scanner_match_on_a_known_value_names_its_slot(self):
        repo = new_repo({"settings.ini": f'[api]\ntoken = "{OTHER}"\n'})
        doc, _ = sweep(repo, {OTHER: SLOT}, scanner="/opt/bin/gitleaks", runner=fake_scanner(repo, []))
        [s] = doc["sightings"]
        self.assertEqual((s["slot"], s["digest_of"]), (SLOT, "value"))
        self.assertEqual(set(s["rules"]), {ks.KNOWN, "generic-api-key"})


class Closing(unittest.TestCase):
    def test_a_rotated_slot_closes_the_finding_once_the_value_is_gone_from_the_tree(self):
        repo = new_repo({"config.py": f"KEY = '{VALUE}'\n"})
        doc, state = sweep(repo, {VALUE: SLOT})
        [f] = kf.findings(doc)
        self.assertEqual((f["type"], f["severity"], f["slot"]), ("secret.in_repository", "critical", SLOT))
        self.assertTrue(f["subject"].startswith("secret:alpha-web/key-sweep/"))
        self.assertIn("rotate", f["action"])
        # Rotated, still in the tree: found again by its digest alone, open, no longer critical.
        doc, state = sweep(repo, {NEW: SLOT}, previous=doc, state=state, today="2026-11-17",
                           rotated_at=lambda label: "2026-11-17T09:00:00Z")
        [s] = doc["sightings"]
        self.assertEqual((s["slot_state"], s["in_tree"], s["status"], s["rotated_at"]),
                         ("rotated", True, "open", "2026-11-17T09:00:00Z"))
        self.assertEqual(kf.findings(doc)[0]["severity"], "warning")
        commit(repo, {"config.py": "KEY = read_from_vault()\n"})
        doc, state = sweep(repo, {NEW: SLOT}, previous=doc, state=state, today="2026-11-18")
        [s] = doc["sightings"]
        self.assertEqual((s["in_tree"], s["status"], s["closed_by"]), (False, "closed", "rotated"))
        self.assertEqual(kf.findings(doc), [])
        assert_clean(self, stored_text(doc, state), VALUE, NEW)

    def test_an_unknown_value_closes_when_gone_and_settled(self):
        repo = new_repo({"settings.ini": f'[api]\ntoken = "{OTHER}"\n'})
        run = fake_scanner(repo, [])
        doc, state = sweep(repo, {}, scanner="/opt/bin/gitleaks", runner=run)
        [f] = kf.findings(doc)
        self.assertEqual(f["severity"], "warning")
        self.assertIn("--settle " + f["fingerprint"], f["action"])
        fp = f["fingerprint"]
        refused = ks.settle(doc, {}, fp, "revoked at the issuer", by="person:test")
        self.assertEqual(refused["error"]["code"], "still-in-tree")
        commit(repo, {"settings.ini": "[api]\ntoken = from-env\n"})
        doc, state = sweep(repo, {}, previous=doc, state=state, scanner="/opt/bin/gitleaks", runner=run,
                           today="2026-11-17")
        self.assertEqual(doc["sightings"][0]["status"], "open", "gone from the tree is not settled")
        self.assertEqual(ks.settle(doc, {}, fp, "  ", by="person:test")["error"]["code"], "invalid-reason")
        self.assertEqual(ks.settle(doc, {}, "hmac-sha256:" + "0" * 64, "x", by="p")["error"]["code"],
                         "unknown-fingerprint")
        settled = ks.settle(doc, {}, fp, "revoked at the issuer", by="person:test")["settled"]
        doc, state = sweep(repo, {}, previous=doc, state=state, settled=settled, scanner="/opt/bin/gitleaks",
                           runner=run, today="2026-11-17")
        [s] = doc["sightings"]
        self.assertEqual((s["status"], s["closed_by"], s["settled"]["by"]), ("closed", "settled", "person:test"))
        self.assertEqual(kf.findings(doc), [])
        assert_clean(self, stored_text(doc, state, settled), OTHER)

    def test_a_value_its_slot_still_holds_cannot_be_settled(self):
        repo = new_repo({"config.py": f"KEY = '{VALUE}'\n"})
        commit(repo, {"config.py": "KEY = None\n"})
        doc, _ = sweep(repo, {VALUE: SLOT})
        fp = doc["sightings"][0]["fingerprint"]
        self.assertEqual(ks.settle(doc, {}, fp, "done", by="p")["error"]["code"], "slot-holds-it")


class Surfaces(unittest.TestCase):
    def test_findings_name_places_never_the_value(self):
        repo = new_repo({"config.py": f"KEY = '{VALUE}'\n"})
        doc, _ = sweep(repo, {VALUE: SLOT})
        [f] = kf.findings(doc)
        self.assertIn("alpha-web/config.py:1", f["detail"])
        self.assertEqual(f["project"], "project:alpha-web")
        assert_clean(self, json.dumps(f), VALUE)
        self.assertEqual(kf.findings({"enabled": False, "sightings": doc["sightings"]}), [])
        self.assertEqual(kf.findings(None), [])

    def test_the_agent_view_reads_per_repository_and_names_what_is_missing(self):
        none = ks.agent_view(None)
        self.assertEqual(none["repositories"], [])
        self.assertEqual(none["degraded"][0]["source"], "key-sweep")
        self.assertIn("switched off", ks.agent_view({"enabled": False})["degraded"][0]["reason"])
        repo = new_repo({"config.py": f"KEY = '{VALUE}'\n"})
        doc, _ = sweep(repo, {VALUE: SLOT})
        view = ks.agent_view(doc)
        self.assertEqual(view["repositories"][0]["sightings_by_rule"], {ks.KNOWN: 1})
        self.assertEqual(view["degraded"], [])
        self.assertEqual(ks.agent_view(doc, repository="beta")["error"]["code"], "unknown-repository")
        assert_clean(self, json.dumps(view), VALUE)

    def test_repositories_are_the_projects_git_folders(self):
        data = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-sweep-data-"))
        (data / "alpha-web").mkdir()
        git(data / "alpha-web", "init", "-q")
        (data / "notes").mkdir()
        projects = [{"id": "project:alpha-web", "local_folders": ["alpha-web", "alpha-web", "notes", "../x", "/abs"]}]
        self.assertEqual([r["repo"] for r in ks.repositories(projects, data)], ["alpha-web"])

    def test_the_diff_reader_numbers_added_lines_and_unquotes_paths(self):
        diff = (b'diff --git a/x b/x\n+++ b/dir/x.txt\n@@ -0,0 +3,2 @@\n+one\n+two\n'
                b'+++ "b/caf\\303\\251.txt"\n@@ -1 +7 @@\n+three\n+++ /dev/null\n@@ -1 +0,0 @@\n')
        self.assertEqual(list(ks.added_lines(diff)),
                         [("dir/x.txt", 3, "one"), ("dir/x.txt", 4, "two"), ("café.txt", 7, "three")])


class FindingsPageSection(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / "dashboard"))
        import key_sweep_page
        from i18n import Translator
        self.page, self.en, self.ru = key_sweep_page, Translator("en"), Translator("ru")

    def test_the_section_counts_per_repository_and_shows_no_value_or_digest(self):
        repo = new_repo({"config.py": f"KEY = '{VALUE}'\n"})
        doc, _ = sweep(repo, {VALUE: SLOT})
        view = self.page.summary(doc)
        self.assertEqual((view["open"], view["rows"][0]["open"], view["rows"][0]["by_rule"]), (1, 1, {ks.KNOWN: 1}))
        for t in (self.en, self.ru):
            out = self.page.key_sweep_html({"key_sweep": view}, t)
            self.assertIn("alpha-web", out)
            assert_clean(self, out, VALUE)
            self.assertNotIn("hmac-sha256", out)
            self.assertNotIn("`", out, "no command on a page")
        self.assertIn("Ключи, найденные в репозиториях", self.page.key_sweep_html({"key_sweep": view}, self.ru))

    def test_not_run_and_off_are_said(self):
        self.assertIn("has not run", self.page.key_sweep_html({}, self.en))
        self.assertIn("switched off", self.page.key_sweep_html({"key_sweep": self.page.summary({"enabled": False})},
                                                               self.en))

    def test_a_reasons_command_is_cut(self):
        view = {"state": "ran", "at": TODAY, "rows": [], "degraded": ["no value is held; run `full env` first"]}
        out = self.page.key_sweep_html({"key_sweep": view}, self.en)
        self.assertIn("no value is held", out)
        self.assertNotIn("full env", out)


class MaintenanceStep(unittest.TestCase):
    def test_the_pass_runs_the_sweep_unless_switched_off(self):
        import maintenance
        from unittest import mock
        self.enterContext(mock.patch.dict(os.environ))
        os.environ.pop("OBSERVATORY_REGISTRY", None)
        os.environ.pop("OBSERVATORY_OFFLINE", None)
        base = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-sweep-home-"))
        (base / "config").mkdir()
        self.assertEqual(maintenance.step_key_sweep(base, {}, maintenance.now(), runner=None)["result"],
                         "no-registry", "no project yet: nothing is started")
        (base / "registry").mkdir()
        (base / "registry" / "projects.json").write_text('{"projects": []}')
        calls = []

        def runner(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, '{"result": "ran", "open": 1, "complete": false}\n', "")
        state: dict = {}
        got = maintenance.step_key_sweep(base, state, maintenance.now(), runner=runner)
        self.assertEqual((got["result"], got["open"], got["complete"]), ("ran", 1, False))
        self.assertTrue(any(str(a).endswith("key_sweep.py") for a in calls[0]))
        self.assertIn("--json", calls[0])
        (base / "config" / "settings.json").write_text(json.dumps({"integrations": {"key_sweep": False}}))
        calls.clear()
        self.assertEqual(maintenance.step_key_sweep(base, {}, maintenance.now(), runner=runner)["result"], "off")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
