#!/usr/bin/env python3
"""The machine survey, git hygiene, the cleanup and the Machine page.

Synthetic only: an invented process table, throwaway git repositories under a
temporary directory, and a temporary workspace. Nothing here reads or removes
anything on the machine running the test.
"""
from __future__ import annotations
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for sub in ("collectors", "tools", "dashboard"):
    sys.path.append(str(ROOT / sub))
import workspace


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}).stdout


def commit(repo, name, text="x"):
    (Path(repo) / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-qm", name)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "workspace"
        self.data = self.base / "data"
        self.data.mkdir()
        self.env = patch.dict(os.environ, {"OBSERVATORY_HOME": str(self.home), "HOME": str(self.base)}, clear=True)
        self.env.start()
        workspace.initialize(self.home)
        doc = json.loads((self.home / "config/settings.json").read_text())
        doc.setdefault("sources", {})["projects"] = str(self.data)
        (self.home / "config/settings.json").write_text(json.dumps(doc))
        import paths
        importlib.reload(paths)
        for name in ("scan_machine", "scan_git_hygiene", "cleanup", "machine_findings", "machine_view"):
            if name in sys.modules:
                importlib.reload(sys.modules[name])

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class Origins(unittest.TestCase):
    TABLE = {
        50: {"pid": 50, "ppid": 1, "exe": "/Applications/Term.app/Contents/MacOS/Term", "user": "u"},
        60: {"pid": 60, "ppid": 50, "exe": "/bin/zsh", "user": "u"},
        70: {"pid": 70, "ppid": 60, "exe": "/opt/example/claude/versions/2.1.0", "user": "u"},
        71: {"pid": 71, "ppid": 70, "exe": "npm exec some-mcp@1.2.3 --flag", "user": "u"},
        80: {"pid": 80, "ppid": 1, "exe": "/opt/homebrew/bin/python3", "user": "u"},
        90: {"pid": 90, "ppid": 1, "exe": "/Library/Sim/launchd_sim", "user": "u"},
        91: {"pid": 91, "ppid": 90, "exe": "/Library/Sim/SpringBoard", "user": "u"},
        95: {"pid": 95, "ppid": 1, "exe": "node", "user": "u"},
        96: {"pid": 96, "ppid": 1, "exe": "/usr/libexec/helperd", "user": "u"},
        97: {"pid": 97, "ppid": 1, "exe": "/usr/sbin/cfprefsd", "user": "root"},
    }
    LABELS = {80: "com.example.worker", 90: "com.apple.CoreSimulator.SimDevice.UDID-1"}

    def test_each_origin(self):
        import scan_machine as sm
        o = lambda pid: sm.origin(pid, self.TABLE, self.LABELS, {"UDID-1": "Phone 1"})[0]  # noqa: E731
        self.assertEqual(o(71), "agent:claude-code")
        self.assertEqual(sm.origin(71, self.TABLE, self.LABELS)[1], 70)
        self.assertEqual(o(60), "app:Term")
        self.assertEqual(o(80), "launchd:com.example.worker")
        self.assertEqual(o(91), "simulator:Phone 1")
        self.assertEqual(o(95), "detached:node")
        self.assertEqual(o(96), "process:helperd")
        self.assertEqual(o(97), "system")

    def test_display_name_and_script_never_keep_secrets(self):
        import scan_machine as sm
        self.assertEqual(sm.display_name("npm exec some-mcp@1.2.3 --flag"), "npm:some-mcp")
        with patch.object(sm, "run", return_value="python3 server.py --port 1\n"):
            self.assertEqual(sm.script_of(1), "server.py")
        with patch.object(sm, "run", return_value="python3 --api-key=abc server.py\n"):
            self.assertEqual(sm.script_of(1), "")
        with patch.object(sm, "run", return_value="node /srv/app.js?token=abc\n"):
            self.assertEqual(sm.script_of(1), "")

    def test_explain_strips_environment_and_command_line(self):
        import scan_machine as sm
        witr = json.dumps({"Process": {"PID": 80, "Env": ["SECRET=value"], "Cmdline": "python3 --token=value"},
                           "Source": {"Type": "launchd"}, "Warnings": [], "Ancestry": [{"PID": 1, "Command": "launchd"}]})
        with patch.object(sm, "process_table", return_value=self.TABLE), \
                patch.object(sm, "launchd_labels", return_value=self.LABELS), \
                patch.object(sm, "simulator_names", return_value={}), \
                patch.object(sm.shutil, "which", return_value="/usr/bin/witr"), \
                patch.object(sm, "run", return_value=witr):
            out = sm.explain(80)
        text = json.dumps(out)
        self.assertNotIn("SECRET=value", text)
        self.assertNotIn("--token=value", text)
        self.assertEqual(out["origin"], "launchd:com.example.worker")
        self.assertEqual(out["witr"]["source"]["Type"], "launchd")


class DiskBudget(Base):
    def setUp(self):
        super().setUp()
        import scan_machine as sm
        # Hermetic: the host's own swap files must never enter a synthetic survey.
        stub = patch.object(sm, "swap_on_disk", return_value=None)
        stub.start()
        self.addCleanup(stub.stop)

    def config(self, **extra):
        places = []
        for name in ("a", "b", "c"):
            (self.base / name).mkdir()
            places.append({"path": str(self.base / name), "kind": "cache", "label": name, "reclaim": "regenerable"})
        (self.home / "config/machine.json").write_text(json.dumps({"every_hours": 12, "disk_budget_seconds": 2,
                                                                   "disk_location_timeout_seconds": 5,
                                                                   "locations": places, **extra}))

    def test_budget_oldest_first_and_previous_kept(self):
        import scan_machine as sm
        self.config()
        prev = {"disk": {"locations": [
            {"path": str(self.base / "a"), "gb": 1.0, "measured_at": "2026-01-01T00:00:00Z"},
            {"path": str(self.base / "b"), "gb": 2.0, "measured_at": "2026-01-02T00:00:00Z"},
            {"path": str(self.base / "c"), "gb": 3.0, "measured_at": "2026-01-03T00:00:00Z"}]}}
        calls = []
        def slow(path, timeout):
            calls.append(Path(path).name)
            time.sleep(0.6)
            return 1048576 * 9
        with patch.object(sm, "size_kb", side_effect=slow):
            out = sm.survey_disk(prev, force=False)
        self.assertEqual(calls, ["a", "b"])                     # oldest first, stops at the budget
        rows = {Path(r["path"]).name: r for r in out["locations"]}
        self.assertEqual(rows["a"]["gb"], 9.0)
        self.assertEqual(rows["c"]["gb"], 3.0)                  # not re-measured: last number kept
        self.assertEqual(rows["c"]["measured_at"], "2026-01-03T00:00:00Z")
        self.assertEqual(out["measured_now"], 2)

    def test_timeout_is_reported_not_zero(self):
        import scan_machine as sm
        self.config(disk_budget_seconds=30)
        prev = {"disk": {"locations": [{"path": str(self.base / "a"), "gb": 4.0, "measured_at": "2026-01-01T00:00:00Z"}]}}
        with patch.object(sm, "size_kb", return_value=None):
            out = sm.survey_disk(prev, force=False)
        self.assertEqual({Path(r["path"]).name: r["gb"] for r in out["locations"]}, {"a": 4.0})
        self.assertEqual(len(out["degraded"]), 3)

    def test_swap_is_a_disk_row(self):
        import scan_machine as sm
        self.config(disk_budget_seconds=30)
        with patch.object(sm, "size_kb", return_value=1048576), \
                patch.object(sm, "swap_on_disk", return_value={"path": "/vm", "gb": 15.0, "files": 3}):
            out = sm.survey_disk(None, force=False)
        swap = [r for r in out["locations"] if r["kind"] == "swap"]
        self.assertEqual(swap[0]["gb"], 15.0)
        self.assertEqual(out["locations"][0]["kind"], "swap")          # the largest row first
        with patch.object(sm, "size_kb", return_value=1048576), patch.object(sm, "swap_on_disk", return_value=None):
            self.assertFalse([r for r in sm.survey_disk(None, force=False)["locations"] if r["kind"] == "swap"])

    def test_du_stays_on_one_file_system(self):
        import scan_machine as sm
        seen = {}
        def fake(cmd, **kw):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 1, stdout="42\t/x\n", stderr="denied")
        with patch.object(sm.subprocess, "run", side_effect=fake):
            self.assertEqual(sm.size_kb(Path("/x"), 5), 42)     # a partial total still counts
        self.assertEqual(seen["cmd"][:2], ["du", "-skx"])
        with patch.object(sm.subprocess, "run", side_effect=subprocess.TimeoutExpired("du", 5)):
            self.assertIsNone(sm.size_kb(Path("/x"), 5))


class Estate(Base):
    """One repository with every class the hygiene survey knows."""

    def build(self):
        remote = self.base / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
        repo = self.data / "app"
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        commit(repo, "a")
        git(repo, "remote", "add", "origin", str(remote))
        git(repo, "push", "-q", "-u", "origin", "main")
        git(repo, "remote", "set-head", "origin", "main")
        git(repo, "branch", "merged-one")                        # merged: nothing main lacks
        git(repo, "checkout", "-qb", "pushed-one"); commit(repo, "p")
        git(repo, "push", "-q", "-u", "origin", "pushed-one")    # pushed: identical to upstream
        git(repo, "checkout", "-qb", "unique-one"); commit(repo, "u")  # unique: exists nowhere else
        git(repo, "checkout", "-qb", "keep/secret"); commit(repo, "k")
        git(repo, "checkout", "-q", "main")
        git(repo, "branch", "clean-wt")
        git(repo, "worktree", "add", "-q", str(self.base / "wt-clean"), "clean-wt")
        git(repo, "branch", "dirty-wt")
        git(repo, "worktree", "add", "-q", str(self.base / "wt-dirty"), "dirty-wt")
        (self.base / "wt-dirty" / "notes.txt").write_text("uncommitted")
        git(repo, "worktree", "add", "-q", "--detach", str(self.base / "wt-gone"))
        subprocess.run(["rm", "-rf", str(self.base / "wt-gone")], check=True)
        (repo / "node_modules").mkdir()
        (repo / "node_modules" / "x.js").write_text("x")
        (repo / ".gitignore").write_text("node_modules/\n")
        git(repo, "add", ".gitignore"); git(repo, "commit", "-qm", "ignore")
        reg = self.home / "registry"
        (reg / "repositories.json").write_text(json.dumps({"schema_version": 2, "repositories": [
            {"id": "repository:example/app", "host": "github", "local": {"path": str(repo)}}]}))
        old = "2020-01-01"
        (reg / "projects.json").write_text(json.dumps({"schema_version": 2, "projects": [
            {"id": "project:app", "name": "app", "ownership": "owned", "lifecycle": "active",
             "local_folders": ["app"], "last_activity_on": old}]}))
        return repo

    def survey(self, idle_days: float = 100):
        import scan_git_hygiene as h
        out = self.home / "store/raw/git-hygiene.json"
        with patch("scan_machine.working_dirs", return_value={}), \
                patch.object(h.time, "time", return_value=time.time() + idle_days * 86400):
            h.main(["x", str(out)])
        return json.loads(out.read_text())

    def test_classes(self):
        self.build()
        doc = self.survey()
        c = doc["checkouts"][0]
        classes = {b["name"]: b["class"] for b in c["branches"]}
        self.assertEqual(classes["merged-one"], "merged")
        self.assertEqual(classes["pushed-one"], "pushed")
        self.assertEqual(classes["unique-one"], "unique")
        self.assertNotIn("clean-wt", classes)          # checked out in a worktree: never a candidate
        states = {Path(w["path"]).name: w["state"] for w in c["worktrees"]}
        self.assertEqual(states, {"wt-clean": "clean", "wt-dirty": "dirty", "wt-gone": "missing"})

    def test_a_checkout_with_no_remote_is_surveyed_too(self):
        # A git folder with no remote has no repository row, only a project's
        # local folder; the survey used to report "0 checkouts" beside it.
        self.build()
        solo = self.data / "solo"
        subprocess.run(["git", "init", "-q", "-b", "main", str(solo)], check=True)
        commit(solo, "s")
        git(solo, "branch", "side")
        git(solo, "worktree", "add", "-q", str(self.base / "wt-solo"), "side")
        (self.data / "plain").mkdir()                          # not git: never a checkout
        reg = self.home / "registry"
        doc = json.loads((reg / "projects.json").read_text())
        doc["projects"] += [{"id": "project:local-solo", "name": "solo", "local_folders": ["solo"]},
                            {"id": "project:local-plain", "name": "plain", "local_folders": ["plain"]},
                            {"id": "project:app-again", "name": "app", "local_folders": ["app"]}]
        (reg / "projects.json").write_text(json.dumps(doc))
        got = {c["repository"]: c for c in self.survey()["checkouts"]}
        self.assertEqual(set(got), {"repository:example/app", "local:solo"})
        self.assertEqual([Path(w["path"]).name for w in got["local:solo"]["worktrees"]], ["wt-solo"])

    def run_cleanup(self, *args, auto_on=True, busy=None):
        import cleanup
        importlib.reload(cleanup)
        doc = json.loads((self.home / "config/settings.json").read_text())
        doc.setdefault("features", {})["auto_cleanup"] = auto_on
        (self.home / "config/settings.json").write_text(json.dumps(doc))
        with patch("scan_machine.working_dirs", return_value=busy or {}):
            return cleanup.main(["cleanup.py", *args])

    def branches(self, repo):
        return set(git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split())

    def test_auto_off_removes_nothing_and_plans(self):
        repo = self.build(); self.survey()
        before = self.branches(repo)
        self.assertEqual(self.run_cleanup("--auto", auto_on=False), 0)
        self.assertEqual(self.branches(repo), before)
        plan = json.loads((self.home / "store/raw/cleanup-plan.json").read_text())
        self.assertFalse(plan["auto_enabled"])
        self.assertGreater(plan["counts"]["branch-merged"], 0)

    def test_auto_removes_only_what_loses_nothing(self):
        repo = self.build(); self.survey()
        self.assertEqual(self.run_cleanup("--auto"), 0)
        left = self.branches(repo)
        self.assertNotIn("merged-one", left)
        self.assertNotIn("pushed-one", left)
        self.assertIn("unique-one", left)                # manual tier
        self.assertIn("keep/secret", left)               # protected
        self.assertFalse((self.base / "wt-clean").exists())
        self.assertTrue((self.base / "wt-dirty" / "notes.txt").exists())   # manual tier
        self.assertNotIn("wt-gone", git(repo, "worktree", "list"))
        self.assertFalse((repo / "node_modules").exists())
        rows = [json.loads(l) for l in (self.home / "store/logs/cleanup.jsonl").read_text().splitlines()]
        merged = next(r for r in rows if r["target"] == "merged-one")
        self.assertEqual(len(merged["sha"]), 40)          # enough to bring it back

    def test_recheck_skips_what_changed_since_the_survey(self):
        repo = self.build(); self.survey()
        git(repo, "checkout", "-q", "merged-one"); commit(repo, "late"); git(repo, "checkout", "-q", "main")
        (self.base / "wt-clean" / "late.txt").write_text("work started after the survey")
        self.run_cleanup("--auto", busy={999: str(repo / "node_modules")})
        self.assertIn("merged-one", self.branches(repo))
        self.assertTrue((self.base / "wt-clean" / "late.txt").exists())
        self.assertTrue((repo / "node_modules").exists())
        reasons = {r["target"]: r.get("reason", "") for r in map(json.loads,
                   (self.home / "store/logs/cleanup.jsonl").read_text().splitlines())}
        self.assertIn("lacks", reasons["merged-one"])
        self.assertIn("uncommitted", reasons[str(self.base / "wt-clean")])

    def test_manual_archives_before_removing(self):
        repo = self.build(); self.survey()
        self.run_cleanup("--apply", "manual")
        self.assertNotIn("unique-one", self.branches(repo))
        self.assertIn("keep/secret", self.branches(repo))   # protected even in the manual tier
        self.assertFalse((self.base / "wt-dirty").exists())
        rows = {r["target"]: r for r in map(json.loads, (self.home / "store/logs/cleanup.jsonl").read_text().splitlines())}
        bundle = Path(rows["unique-one"]["archive"])
        self.assertEqual(subprocess.run(["git", "-C", str(repo), "bundle", "verify", str(bundle)],
                                        capture_output=True).returncode, 0)
        git(repo, "fetch", "-q", str(bundle), "refs/heads/unique-one:refs/heads/restored")
        self.assertIn("restored", self.branches(repo))
        archived = Path(rows[str(self.base / "wt-dirty")]["archive"])
        import tarfile
        with tarfile.open(archived / "untracked.tgz") as tar:
            self.assertIn("notes.txt", tar.getnames())

    def test_idle_threshold_protects_fresh_work(self):
        repo = self.build(); self.survey(idle_days=0)
        self.run_cleanup("--auto")
        self.assertIn("merged-one", self.branches(repo))
        self.assertTrue((self.base / "wt-clean").exists())


class Findings(Base):
    def write(self, name, doc):
        (self.home / "store/raw" / name).write_text(json.dumps(doc))

    def test_disk_memory_detached_and_worktrees(self):
        import machine_findings as mf
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        self.write("machine.json", {"measured_at": "2026-01-01T11:30:00Z",
                                    "memory": {"swap_used_mb": 8192},
                                    "processes": {"groups": [{"origin": "simulator:Phone", "processes": 100, "rss_mb": 9000},
                                                             {"origin": "detached:npm:some-mcp", "processes": 2, "rss_mb": 300},
                                                             {"origin": "detached:helperd", "processes": 1, "rss_mb": 50}]},
                                    "disk": {"volume": {"free_gb": 10, "free_percent": 3},
                                             "locations": [{"label": "Caches", "gb": 30, "command": "clean it"},
                                                           {"label": "Swap files", "kind": "swap", "gb": 15.0}]}})
        self.write("git-hygiene.json", {"checkouts": [{"repository": "repository:e/a", "worktrees": [
            {"path": "/w/old", "state": "dirty", "idle_days": 30}, {"path": "/w/today", "state": "dirty", "idle_days": 0}],
            "branches": [{"name": "b", "class": "unique", "idle_days": 40}]}]})
        out = {f["type"]: f for f in mf.findings(self.home / "store/raw", self.home / "store/logs",
                                                 {"free_space_warning_percent": 15, "free_space_critical_percent": 5,
                                                  "memory_group_warning_mb": 4096}, now)}
        self.assertEqual(out["machine.disk_low"]["severity"], "critical")
        self.assertIn("clean it", out["machine.disk_low"]["detail"])
        self.assertIn("15.0 GB of it is swap", out["machine.disk_low"]["detail"])
        self.assertIn("simulator:Phone", out["machine.memory_pressure"]["detail"])
        self.assertIn("machine.heavy_origin", out)
        self.assertIn("npm:some-mcp", out["machine.detached_servers"]["detail"])
        self.assertNotIn("helperd", out["machine.detached_servers"]["detail"])
        self.assertIn("old", out["git.idle_dirty_worktrees"]["detail"])
        self.assertNotIn("today", out["git.idle_dirty_worktrees"]["detail"])
        self.assertIn("git.idle_unique_branches", out)
        self.assertNotIn("machine.stale", out)

    def test_silent_before_any_survey(self):
        import machine_findings as mf
        self.assertEqual(mf.findings(self.home / "store/raw", self.home / "store/logs", {}), [])


class Page(Base):
    def test_renders_every_section_in_both_languages_and_empty_states(self):
        import machine_view, machine_page
        from i18n import Translator
        (self.home / "store/raw/machine.json").write_text(json.dumps({
            "measured_at": "2026-01-01T00:00:00Z", "memory": {"total_mb": 16384, "free_mb": 1024, "swap_used_mb": 0},
            "processes": {"count": 3, "groups": [{"origin": "app:Editor", "processes": 3, "sessions": 1, "rss_mb": 2048, "cpu": 1.0}],
                          "top": [{"pid": 7, "name": "Editor", "origin": "app:Editor", "rss_mb": 2048, "cpu": 1.0}], "projects": []},
            "disk": {"volume": {"free_gb": 100, "free_percent": 40, "total_gb": 250},
                     "locations": [{"path": "~/x", "label": "X cache", "kind": "cache", "reclaim": "regenerable", "gb": 1.5}]}}))
        summary = machine_view.summary()
        en = machine_page.machine_html({"machine": summary}, Translator("en"))
        ru = machine_page.machine_html({"machine": summary}, Translator("ru"))
        for text in ("Memory by origin", "app:Editor", "X cache", "Where the disk goes"):
            self.assertIn(text, en)
        self.assertIn("Память по источникам", ru)
        self.assertIn("кэш", ru)
        self.assertIn("Nothing was cleaned in the last 7 days.", en)   # empty state, not a blank card
        self.assertIn("git-hygiene.json", json.dumps(summary["degraded"]))


class Unmeasured(Base):
    def test_nothing_measured_is_never_shown_as_all_clean(self):
        # With no git-hygiene survey and no cleanup plan the page said "Every
        # worktree is clean…", "No branch holds commits found nowhere else" and
        # "Nothing to clean" — claims about checkouts nobody looked at.
        import machine_view, machine_page
        from i18n import Translator
        summary = machine_view.summary()
        en = machine_page.machine_html({"machine": summary}, Translator("en"))
        for claim in ("Every worktree is clean", "No branch holds commits found nowhere else", "Nothing to clean."):
            self.assertNotIn(claim, en)
        self.assertIn("Not measured yet", en)
        self.assertIn("project-observatory full machine", en)
        # The degraded reasons are message ids, so the Russian page is Russian.
        ru = machine_page.machine_html({"machine": summary}, Translator("ru"))
        import re
        shown = re.sub(r'data-t(-args)?="[^"]*"', "", ru)     # the ids themselves are English by design
        self.assertNotIn("not surveyed yet", shown)
        self.assertNotIn("Not measured yet", shown)
        self.assertIn("project-observatory full machine", ru)


class Tool(Base):
    """The MCP tool an agent reads: bounded by default, whole only on request."""

    def setUp(self):
        super().setUp()
        groups = [{"origin": f"app:App{i}", "processes": 1, "sessions": 0, "rss_mb": 100 + i, "cpu": 0.1}
                  for i in range(30)]
        top = [{"pid": 100 + i, "name": f"App{i}", "origin": f"app:App{i}", "rss_mb": 100 + i, "cpu": 0.1}
               for i in range(30)]
        (self.home / "store/raw/machine.json").write_text(json.dumps({
            "measured_at": "2026-01-01T00:00:00Z", "memory": {"total_mb": 16384, "free_mb": 1024},
            "processes": {"count": 30, "groups": groups, "top": top, "projects": []},
            "disk": {"volume": {"free_gb": 100, "free_percent": 40, "total_gb": 250},
                     "locations": [{"path": f"~/c{i}", "label": f"C{i}", "kind": "cache",
                                    "reclaim": "regenerable", "gb": float(i)} for i in range(30)]}}))
        sys.path.insert(0, str(ROOT / "mcp"))
        import server
        self.server = importlib.reload(server)

    def test_overview_is_bounded_and_names_the_sections(self):
        out = self.server.observatory_machine()
        self.assertEqual(len(out["processes"]["top"]), self.server.MACHINE_TOP)
        self.assertEqual(out["processes"]["groups"][0]["origin"], "app:App29")   # heaviest first
        self.assertEqual(out["disk"]["locations"][0]["gb"], 29.0)
        self.assertEqual(out["processes"]["count"], 30)                            # the total stays whole
        self.assertIn("processes", out["sections"])
        self.assertIn("degraded", out)

    def test_a_named_section_is_whole(self):
        out = self.server.observatory_machine(section="processes")
        self.assertEqual(len(out["processes"]["groups"]), 30)
        self.assertNotIn("disk", out)

    def test_explaining_one_process_returns_only_the_explanation(self):
        import scan_machine
        with patch.object(scan_machine, "explain", return_value={"pid": 7, "origin": "app:Editor"}):
            out = self.server.observatory_machine(explainPid=7)
        self.assertEqual(out["explain"], {"pid": 7, "origin": "app:Editor"})
        self.assertEqual(set(out), {"explain", "measuredAt", "degraded"})



class Command(Base):
    """`full machine` names the page it fills: the split dashboard's machine.html,
    not the single page the split retired (2026-10-06)."""

    def test_the_survey_names_the_machine_page(self):
        import io, contextlib, paths, scan_machine

        def survey(argv):
            pathlib_out = Path(argv[1])
            pathlib_out.parent.mkdir(parents=True, exist_ok=True)
            pathlib_out.write_text(json.dumps({"memory": {}, "disk": {"volume": {}}, "processes": {}}))
            return 0
        out = io.StringIO()
        with patch.object(scan_machine, "main", side_effect=survey), contextlib.redirect_stdout(out):
            self.assertEqual(workspace.machine_command("machine", []), 0)
        page = json.loads(out.getvalue())["page"]
        self.assertEqual(page, str(paths.DASHBOARD_DIR / "machine.html"))
        self.assertNotIn("projects-dashboard.html", out.getvalue())

if __name__ == "__main__":
    unittest.main()
