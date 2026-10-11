#!/usr/bin/env python3
"""The disk guard: pressure, the one rule that protects live work, and each class.

Synthetic only: temporary roots, throwaway git repositories and an invented `lsof`
listing. Nothing here reads or removes anything outside its temporary directory —
every root the guard walks is pointed into it, and the cache commands are fakes.
"""
from __future__ import annotations
import contextlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for sub in ("collectors", "tools"):
    sys.path.append(str(ROOT / sub))
import workspace

DAY = 86400


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}).stdout


def age(path: Path, days: float) -> None:
    """Make a path and everything under it look `days` old (mtime and atime)."""
    t = time.time() - days * DAY
    for dirpath, dirnames, filenames in os.walk(path, topdown=False):
        for n in filenames + dirnames:
            os.utime(os.path.join(dirpath, n), (t, t), follow_symlinks=False)
    os.utime(path, (t, t), follow_symlinks=False)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "workspace"
        self.env = patch.dict(os.environ, {"OBSERVATORY_HOME": str(self.home), "HOME": str(self.base)}, clear=True)
        self.env.start()
        workspace.initialize(self.home)
        import paths
        importlib.reload(paths)
        import disk_guard
        self.g = importlib.reload(disk_guard)
        self.t1 = self.base / "t1"
        self.t1.mkdir()
        # NOTHING OUTSIDE THE TEMPORARY DIRECTORY: the clone root and the temporary
        # directory are this test's own, so no class can reach the machine's.
        self.clones = self.base / "X"
        self.clones.mkdir()
        (self.base / "tmpdir").mkdir()
        for p in (patch.object(self.g, "clone_root", return_value=self.clones),
                  patch.object(tempfile, "tempdir", str(self.base / "tmpdir"))):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def cfg(self, **over):
        cfg = self.g.load_config()
        cfg.update({"temp_roots": [{"path": str(self.t1), "depth": 1}], "commands": []}, **over)
        return cfg

    def free(self, *names):
        """Holders that know of exactly these open paths."""
        return self.g.Holders([str(n) for n in names])


class Pressure(Base):
    def test_levels_follow_free_space(self):
        cfg = self.g.load_config()
        gb = self.g.GB
        self.assertEqual(self.g.level_for(100 * gb, cfg), "normal")
        self.assertEqual(self.g.level_for(20 * gb, cfg), "low")
        self.assertEqual(self.g.level_for(5 * gb, cfg), "critical")

    def test_a_workspace_file_without_a_guard_section_keeps_the_shipped_thresholds(self):
        # A workspace's config/cleanup.json predates the guard (it was copied at init).
        import paths
        own = paths.config_file("cleanup.json")
        own.parent.mkdir(parents=True, exist_ok=True)
        own.write_text(json.dumps({"auto": {}, "artifact_dirs": ["node_modules"]}))
        cfg = self.g.load_config()
        self.assertEqual(cfg["levels"]["critical"]["free_gb"], 10)
        self.assertTrue(cfg["commands"])
        own.write_text(json.dumps({"guard": {"levels": {"low": {"free_gb": 50}}}}))
        cfg = self.g.load_config()
        self.assertEqual(cfg["levels"]["low"]["free_gb"], 50)
        self.assertEqual(cfg["levels"]["critical"]["free_gb"], 10, "a partial override keeps the other level")


class Holding(Base):
    def test_a_path_held_or_worked_inside_is_busy_and_a_neighbour_is_not(self):
        h = self.free("/x/a/b/file.txt", "/x/c")
        self.assertTrue(h.busy("/x/a"))
        self.assertTrue(h.busy("/x/a/b"))
        self.assertTrue(h.busy("/x/c"))
        self.assertFalse(h.busy("/x/a-other"), "a sibling whose name only starts the same is free")
        self.assertFalse(h.busy("/x/b"))

    def test_without_a_listing_every_path_is_busy(self):
        h = self.g.Holders(None)
        self.assertFalse(h.known)
        self.assertTrue(h.busy(self.t1))


class Temp(Base):
    def test_old_unheld_entries_go_fresh_held_and_kept_ones_stay(self):
        old, fresh, held, sock = self.t1 / "old", self.t1 / "fresh", self.t1 / "held", self.t1 / "with-socket"
        for d in (old, fresh, held, sock):
            d.mkdir()
            (d / "f").write_text("x" * 100)
        (self.t1 / "keep.sock").write_text("")
        import socket
        s = socket.socket(socket.AF_UNIX)
        try:
            s.bind(str(sock / "s"))
        except OSError:
            self.skipTest("unix socket path too long here")
        for d in (old, held, sock):
            age(d, 5)
        age(self.t1 / "keep.sock", 5)
        # A file inside `fresh` written now makes the whole entry fresh, however old its directory.
        age(fresh, 5)
        (fresh / "new").write_text("now")
        plan = self.g.plan_temp(self.cfg(), "normal", self.free(held / "f"), time.time())
        s.close()
        self.assertEqual([Path(a["target"]).name for a in plan], ["old"])
        self.assertEqual(plan[0]["bytes"], 100)

    def test_pressure_shortens_the_age(self):
        (self.t1 / "two-days").mkdir()
        age(self.t1 / "two-days", 2)
        self.assertEqual(self.g.plan_temp(self.cfg(), "normal", self.free(), time.time()), [])
        self.assertEqual(len(self.g.plan_temp(self.cfg(), "low", self.free(), time.time())), 1)

    def test_a_nested_root_is_swept_at_its_own_depth_and_not_as_an_entry(self):
        sessions = self.t1 / "agent-501"
        (sessions / "project" / "old-session").mkdir(parents=True)
        (sessions / "project" / "live-session").mkdir(parents=True)
        age(sessions, 5)
        (sessions / "project" / "live-session" / "out").write_text("now")
        cfg = self.cfg(temp_roots=[{"path": str(self.t1), "depth": 1}, {"path": str(sessions), "depth": 2}])
        plan = self.g.plan_temp(cfg, "normal", self.free(), time.time())
        self.assertEqual([Path(a["target"]).name for a in plan], ["old-session"])

    def test_a_root_with_a_level_is_swept_only_at_that_pressure(self):
        derived = self.base / "derived"
        (derived / "App-abc").mkdir(parents=True)
        age(derived, 5)
        cfg = self.cfg(temp_roots=[{"path": str(derived), "depth": 1, "level": "low"}])
        self.assertEqual(self.g.plan_temp(cfg, "normal", self.free(), time.time()), [])
        self.assertEqual(len(self.g.plan_temp(cfg, "low", self.free(), time.time())), 1)

    def test_a_spent_budget_stops_planning_and_says_so(self):
        (self.t1 / "old").mkdir()
        age(self.t1 / "old", 9)
        degraded = []
        plan = self.g.plan_temp(self.cfg(), "normal", self.free(), time.time(), self.g.Budget(0, degraded))
        self.assertEqual(plan, [])
        self.assertEqual(degraded[0]["source"], "temp")

    def test_a_symlink_is_never_followed_or_removed(self):
        target = self.base / "precious"
        target.mkdir()
        (target / "f").write_text("keep")
        os.symlink(target, self.t1 / "link")
        age(target, 9)
        age(self.t1 / "link", 9)          # the link itself is old too: only the rule keeps it
        self.assertEqual(self.g.plan_temp(self.cfg(), "critical", self.free(), time.time()), [])


class Order(Base):
    def test_commands_first_then_largest_paths_and_the_cap_is_per_class(self):
        for name, size in (("small", 10), ("large", 5000), ("mid", 300)):
            (self.t1 / name).mkdir()
            (self.t1 / name / "f").write_bytes(b"x" * size)
            age(self.t1 / name, 9)
        cfg = self.cfg(commands=[{"argv": ["python3", "-c", "pass"], "level": "normal"}])
        plan = self.g.build_plan(cfg, "normal", self.free(), time.time(), [])
        self.assertEqual(plan[0]["class"], "cache-command")
        self.assertEqual([Path(a["target"]).name for a in plan if a["class"] == "temp"], ["large", "mid", "small"])
        with patch.object(self.g, "load_config", return_value={**cfg, "max_actions_per_class": 1}), \
                patch.object(self.g.Holders, "take", return_value=self.free()), \
                patch.object(self.g, "refresh", return_value="fresh"), \
                patch.object(self.g, "large_files", return_value=None), \
                patch.object(self.g, "running", return_value=False):
            doc = self.g.run(["--apply", "--level", "normal"])
        self.assertTrue((self.t1 / "small").exists() and (self.t1 / "mid").exists())
        self.assertFalse((self.t1 / "large").exists(), "one per class, the largest")
        self.assertEqual(doc["results"]["removed"], 2, "the command and one temp entry")


class Acting(Base):
    def test_a_path_that_became_held_after_the_plan_is_skipped(self):
        (self.t1 / "x").mkdir()
        rec = self.g.act({"class": "temp", "target": str(self.t1 / "x"), "bytes": 0},
                         self.free(self.t1 / "x" / "opened-since"))
        self.assertEqual(rec["result"], "skipped")
        self.assertTrue((self.t1 / "x").exists())

    def test_a_removal_records_what_the_disk_said(self):
        (self.t1 / "x").mkdir()
        frees = iter([1000 * 1048576, 1250 * 1048576])
        with patch.object(self.g, "free_bytes", side_effect=lambda *_: next(frees)):
            rec = self.g.act({"class": "temp", "target": str(self.t1 / "x"), "bytes": 4096}, self.free())
        self.assertEqual(rec["result"], "removed")
        self.assertEqual(rec["freed_mb"], 250)
        self.assertEqual(rec["kb"], 4)
        self.assertFalse((self.t1 / "x").exists())

    def test_a_cache_command_waits_for_its_tool_and_never_starts_a_daemon(self):
        a = {"class": "cache-command", "target": "tool prune", "argv": [sys.executable, "-c", "pass"],
             "busy_if": ["tool"], "needs": []}
        with patch.object(self.g, "running", return_value=True):
            self.assertEqual(self.g.act(a, self.free())["result"], "skipped")
        b = {**a, "busy_if": [], "needs": ["dockerd"]}
        with patch.object(self.g, "running", return_value=False):
            rec = self.g.act(b, self.free())
        self.assertEqual(rec["result"], "skipped")
        self.assertIn("starts nothing", rec["reason"])
        with patch.object(self.g, "running", return_value=False):
            self.assertEqual(self.g.act({**a, "busy_if": []}, self.free())["result"], "removed")

    def test_a_tool_that_says_its_cache_is_in_use_is_skipped_not_failed(self):
        a = {"class": "cache-command", "target": "tool prune", "busy_if": [], "needs": [], "busy_output": "in-use",
             "argv": [sys.executable, "-c", "import sys; sys.stderr.write('Cache is currently in-use'); sys.exit(2)"]}
        with patch.object(self.g, "running", return_value=False):
            self.assertEqual(self.g.act(a, self.free())["result"], "skipped")
            self.assertEqual(self.g.act({**a, "busy_output": ""}, self.free())["result"], "failed")

    def test_commands_are_chosen_by_pressure_and_by_what_is_installed(self):
        cfg = self.cfg(commands=[{"argv": ["python3", "-c", "pass"], "level": "normal"},
                                 {"argv": ["python3", "-V"], "level": "critical"},
                                 {"argv": ["no-such-tool-here", "prune"], "level": "normal"}])
        self.assertEqual([a["target"] for a in self.g.plan_commands(cfg, "normal")], ["python3 -c pass"])
        self.assertEqual(len(self.g.plan_commands(cfg, "critical")), 2)


class Clones(Base):
    def test_abandoned_clones_go_and_a_running_browsers_clone_stays(self):
        holder = self.clones / "com.example.Browser.code_sign_clone"
        old, live, new = (holder / f"code_sign_clone.{n}" for n in ("old", "live", "new"))
        for d in (old, live, new):
            (d / "App.bundle").mkdir(parents=True)
        for d in (old, live):
            age(d, 1)
        plan = self.g.plan_clones(self.cfg(), self.free(live / "App.bundle" / "binary"), time.time())
        self.assertEqual([Path(a["target"]).name for a in plan], ["code_sign_clone.old"])

    def test_no_clone_root_means_no_clone_is_planned(self):
        with patch.object(self.g, "clone_root", return_value=None):
            self.assertEqual(self.g.plan_clones(self.cfg(), self.free(), time.time()), [])


class Builds(Base):
    def repo(self, name: str, idle_days: float) -> Path:
        r = self.base / name
        r.mkdir()
        git(r, "init", "-q")
        (r / ".gitignore").write_text("node_modules/\n")
        (r / "app.js").write_text("x")
        git(r, "add", ".")
        git(r, "commit", "-qm", "init")
        (r / "node_modules" / "pkg").mkdir(parents=True)
        (r / "node_modules" / "pkg" / "index.js").write_text("y" * 50)
        age(r, idle_days)
        # The index is rewritten by any `git status` — the guard must not read it as work.
        git(r, "status", "--porcelain")
        return r

    def hygiene(self, *repos):
        import paths
        paths.SCRATCH.mkdir(parents=True, exist_ok=True)
        (paths.SCRATCH / "git-hygiene.json").write_text(json.dumps(
            {"checkouts": [{"path": str(r), "worktrees": []} for r in repos]}))

    def test_idle_checkouts_lose_ignored_output_and_busy_or_recent_ones_keep_it(self):
        idle, recent, held = self.repo("idle", 20), self.repo("recent", 2), self.repo("held", 20)
        self.hygiene(idle, recent, held)
        plan = self.g.plan_builds(self.g.load_config(), "normal", self.free(held / "app.js"), time.time(),
                                  ["node_modules"])
        self.assertEqual([a["target"] for a in plan], [str(idle / "node_modules")])
        self.assertEqual(plan[0]["bytes"], 50)
        low = self.g.plan_builds(self.g.load_config(), "low", self.free(held / "app.js"), time.time(), ["node_modules"])
        self.assertEqual([Path(a["target"]).parent.name for a in low], ["idle"], "2 days idle < 3 at low")
        crit = self.g.plan_builds(self.g.load_config(), "critical", self.free(held / "app.js"), time.time(), ["node_modules"])
        self.assertEqual(sorted(Path(a["target"]).parent.name for a in crit), ["idle", "recent"])

    def test_an_uncommitted_edit_counts_as_work_and_the_index_does_not(self):
        r = self.repo("edited", 20)
        self.hygiene(r)
        self.assertGreater(self.g.checkout_idle_days(str(r), time.time()), 19)
        (r / "app.js").write_text("edited now")
        self.assertLess(self.g.checkout_idle_days(str(r), time.time()), 1)

    def test_a_tracked_directory_named_like_output_is_never_removed(self):
        r = self.repo("tracked", 20)
        (r / "build").mkdir()
        (r / "build" / "script.sh").write_text("echo")
        git(r, "add", "build")
        git(r, "commit", "-qm", "tracked build dir")
        age(r, 20)
        self.hygiene(r)
        plan = self.g.plan_builds(self.g.load_config(), "critical", self.free(), time.time(), ["build", "node_modules"])
        self.assertEqual([Path(a["target"]).name for a in plan], ["node_modules"])

    def test_git_at_work_holds_the_checkout(self):
        r = self.repo("locked", 20)
        gitdir = r / ".git"
        (gitdir / "index.lock").write_text("")
        self.assertEqual(self.g.checkout_idle_days(str(r), time.time()), 0.0)


class Trash(Base):
    def test_items_go_by_when_they_were_trashed(self):
        trash = self.base / ".Trash"
        trash.mkdir()
        (trash / "old.txt").write_text("x")
        with patch.object(self.g, "trash_dirs", return_value=[str(trash)]):
            at_now = self.g.plan_trash(self.cfg(), "normal", time.time(), [])
            later = self.g.plan_trash(self.cfg(), "normal", time.time() + 31 * DAY, [])
            critical = self.g.plan_trash(self.cfg(), "critical", time.time() + 4 * DAY, [])
        self.assertEqual(at_now, [])
        self.assertEqual([Path(a["target"]).name for a in later], ["old.txt"])
        self.assertEqual(len(critical), 1)

    def test_an_unreadable_trash_is_named_not_guessed(self):
        degraded = []
        trash = self.base / ".Trash"
        trash.mkdir()
        with patch.object(self.g, "trash_dirs", return_value=[str(trash)]), \
                patch.object(self.g.os, "scandir", side_effect=PermissionError):
            self.assertEqual(self.g.plan_trash(self.cfg(), "normal", time.time(), degraded), [])
        self.assertEqual(degraded[0]["source"], "trash")


class Run(Base):
    def test_auto_with_cleanup_off_plans_and_removes_nothing(self):
        (self.t1 / "old").mkdir()
        age(self.t1 / "old", 9)
        settings = self.home / "config" / "settings.json"
        doc = json.loads(settings.read_text())
        doc.setdefault("features", {})["auto_cleanup"] = False
        settings.write_text(json.dumps(doc))
        with patch.object(self.g, "load_config", return_value=self.cfg()), \
                patch.object(self.g.Holders, "take", return_value=self.free()), \
                patch.object(self.g, "large_files", return_value=None):
            doc = self.g.run(["--auto"])
        self.assertFalse(doc["acting"])
        self.assertEqual(doc["planned"]["temp"], 1)
        self.assertTrue((self.t1 / "old").exists())

    def test_no_listing_of_open_files_means_no_path_is_removed(self):
        (self.t1 / "old").mkdir()
        age(self.t1 / "old", 9)
        with patch.object(self.g, "load_config", return_value=self.cfg()), \
                patch.object(self.g.Holders, "take", return_value=self.g.Holders(None)), \
                patch.object(self.g, "refresh", return_value="fresh"), \
                patch.object(self.g, "large_files", return_value=None), \
                patch.object(self.g.subprocess, "run", side_effect=OSError):
            doc = self.g.run(["--apply"])
        self.assertTrue((self.t1 / "old").exists())
        self.assertEqual(doc["planned"]["temp"], 0)
        self.assertEqual(doc["degraded"][0]["source"], "lsof")

    def test_apply_journals_each_action_with_the_guard_mode(self):
        (self.t1 / "old").mkdir()
        age(self.t1 / "old", 9)
        with patch.object(self.g, "load_config", return_value=self.cfg()), \
                patch.object(self.g.Holders, "take", return_value=self.free()), \
                patch.object(self.g, "refresh", return_value="fresh"), \
                patch.object(self.g, "large_files", return_value=None), \
                patch.object(self.g.subprocess, "run", side_effect=OSError):
            doc = self.g.run(["--apply"])
        import paths
        rows = [json.loads(l) for l in (paths.STATE / "logs" / "cleanup.jsonl").read_text().splitlines()]
        self.assertEqual(doc["results"]["removed"], 1)
        self.assertEqual(rows[-1]["mode"], "guard")
        self.assertIn("freed_mb", rows[-1])
        self.assertFalse((self.t1 / "old").exists())
        self.assertTrue((paths.SCRATCH / "disk-guard.json").exists())


class RunBudget(Base):
    """2026-10-11: the scheduled pass killed the guard at its ceiling and nothing was written."""

    def setUp(self):
        super().setUp()
        for name in ("a", "b", "c"):
            (self.t1 / name).mkdir()
            age(self.t1 / name, 9)

    def patched(self):
        return [patch.object(self.g, "load_config", return_value=self.cfg(run_reserve_seconds=0)),
                patch.object(self.g.Holders, "take", return_value=self.free()),
                patch.object(self.g, "large_files", return_value=None)]

    def test_a_spent_budget_stops_acting_says_so_and_still_reports(self):
        ran = []
        with contextlib.ExitStack() as stack:
            for p in self.patched():
                stack.enter_context(p)
            stack.enter_context(patch.object(self.g, "refresh", side_effect=lambda *a, **k: ran.append(a) or "x"))
            # A plan already made, so what is measured is the act phase stopping, not planning.
            stack.enter_context(patch.object(self.g, "build_plan", return_value=[
                {"class": "temp", "target": str(self.t1 / n), "bytes": 0} for n in "abc"]))
            doc = self.g.run(["--apply", "--budget", "0"])
        self.assertEqual(doc["results"]["removed"], 0)
        self.assertTrue(all((self.t1 / n).exists() for n in "abc"))
        self.assertIn("act", [d["source"] for d in doc["degraded"]])
        self.assertEqual(ran, [], "no survey is refreshed on a spent budget")
        self.assertEqual(doc["surveys"]["git-cleanup"], "skipped: the run budget is spent")
        import paths
        self.assertTrue((paths.SCRATCH / "disk-guard.json").exists())

    def test_the_journal_and_the_report_survive_a_run_stopped_midway(self):
        import paths
        calls = []
        real = self.g.act

        def act(a, holders):
            calls.append(a)
            if len(calls) == 2:
                raise KeyboardInterrupt          # the job's kill, as far as this process can tell
            return real(a, holders)
        with contextlib.ExitStack() as stack:
            for p in self.patched():
                stack.enter_context(p)
            stack.enter_context(patch.object(self.g, "act", side_effect=act))
            stack.enter_context(patch.object(self.g, "refresh", return_value="fresh"))
            with self.assertRaises(KeyboardInterrupt):
                self.g.run(["--apply", "--budget", "600"])
        rows = [json.loads(l) for l in (paths.STATE / "logs" / "cleanup.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 1, "the first removal is on the record before the second began")
        self.assertEqual(rows[0]["result"], "removed")
        report = json.loads((paths.SCRATCH / "disk-guard.json").read_text())
        self.assertEqual(report["results"]["removed"], 1)


class JsonLine(Base):
    def test_json_prints_one_line_the_maintenance_job_can_read(self):
        import io, contextlib
        out = io.StringIO()
        with patch.object(self.g, "load_config", return_value=self.cfg()), \
                patch.object(self.g.Holders, "take", return_value=self.free()), \
                patch.object(self.g, "large_files", return_value=None), contextlib.redirect_stdout(out):
            code = self.g.main(["--json"])
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 1)
        doc = json.loads(lines[0])
        self.assertEqual(set(doc), {"level", "acting", "free_gb_before", "free_gb_after", "freed_gb",
                                    "planned", "results", "seconds"})


class LargeFiles(Base):
    def test_finds_big_files_and_never_enters_a_guarded_place(self):
        (self.base / "Movies").mkdir()
        (self.base / "work").mkdir()
        for p in (self.base / "work" / "big.bin", self.base / "Movies" / "private.mov"):
            with open(p, "wb") as f:
                f.truncate(3 * 1048576)
        (self.base / "work" / "small.bin").write_bytes(b"x")
        cfg = self.cfg(large_file_min_mb=1, large_files_skip=[])
        found = self.g.large_files(cfg, None, True)
        names = [Path(f["path"]).name for f in found["files"]]
        self.assertIn("big.bin", names)
        self.assertNotIn("private.mov", names, "Movies is privacy-guarded: never walked")
        self.assertNotIn("small.bin", names)

    def test_a_fresh_measurement_is_reused(self):
        prev = {"large_files": {"measured_at": self.g.now(), "files": [{"path": "/a", "gb": 2}]}}
        self.assertEqual(self.g.large_files(self.cfg(), prev, False)["files"][0]["path"], "/a")


class Surfaces(Base):
    """What a person and an agent see: the finding, the page, the command line."""

    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(ROOT / "dashboard"))
        import paths
        self.raw = paths.SCRATCH
        self.raw.mkdir(parents=True, exist_ok=True)
        self.logs = paths.STATE / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)

    def report(self, at="2026-10-11T10:00:00Z"):
        (self.raw / "disk-guard.json").write_text(json.dumps({
            "at": at, "level": "low", "acting": True, "auto_enabled": True, "free_gb_before": 20.0,
            "free_gb_after": 26.5, "freed_gb": 6.5, "planned": {"temp": 3, "build-output": 2},
            "planned_gb": 7.0, "results": {"removed": 5, "skipped": 0, "failed": 0},
            "large_files": {"measured_at": at, "files": [{"path": "/data/alpha-web/big.img", "gb": 8.0,
                                                          "modified": "2026-09-20"}]},
            "memory": {"swap_gb": 12.0, "holders": [{"origin": "agent:example", "processes": 9, "rss_mb": 9000}],
                       "simulators": [{"name": "Phone A", "rss_mb": 4096,
                                       "command": "xcrun simctl shutdown \"Phone A\""}]},
            "degraded": []}))

    def test_a_silent_guard_is_a_warning_only_when_cleanup_is_on(self):
        import machine_findings as mf
        from datetime import datetime, timezone
        now = datetime(2026, 10, 11, 18, 0, tzinfo=timezone.utc)
        types = lambda on: [f["type"] for f in mf.findings(self.raw, self.logs, {}, now, auto_cleanup=on)]  # noqa: E731
        self.assertIn("machine.guard_idle", types(True), "never ran")
        self.assertNotIn("machine.guard_idle", types(False))
        self.report("2026-10-11T17:00:00Z")
        self.assertNotIn("machine.guard_idle", types(True), "ran an hour ago")
        self.report("2026-10-11T10:00:00Z")
        self.assertIn("machine.guard_idle", types(True), "eight hours of silence")

    def test_the_disk_low_finding_names_the_guard_the_swap_and_the_largest_files(self):
        import machine_findings as mf
        self.report()
        (self.raw / "machine.json").write_text(json.dumps({
            "measured_at": "2026-10-11T10:00:00Z", "memory": {}, "processes": {"groups": []},
            "disk": {"volume": {"free_gb": 3, "free_percent": 1.0}, "locations": []}}))
        from datetime import datetime, timezone
        low = [f for f in mf.findings(self.raw, self.logs, {}, datetime(2026, 10, 11, 10, 30, tzinfo=timezone.utc),
                                      auto_cleanup=True) if f["type"] == "machine.disk_low"][0]
        for text in ("pressure low", "Swap holds 12.0 GB", 'xcrun simctl shutdown \"Phone A\"', "big.img 8.0 GB"):
            self.assertIn(text, low["detail"])

    def test_freed_space_is_the_disks_count_when_a_row_has_one(self):
        import machine_findings as mf
        from datetime import datetime, timezone
        rows = [{"at": "2026-10-11T10:00:00Z", "class": "browser-clone", "result": "removed", "kb": 13 * 1048576,
                 "freed_mb": 0},
                {"at": "2026-10-11T10:00:00Z", "class": "temp", "result": "removed", "kb": 1, "freed_mb": 2048}]
        (self.logs / "cleanup.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        done = [f for f in mf.findings(self.raw, self.logs, {}, datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc),
                                       auto_cleanup=False) if f["type"] == "cleanup.done"][0]
        self.assertEqual(done["title_args"]["gb"], "2.0", "13 GB by du, 0 by the disk: the disk wins")

    def test_the_page_shows_the_guard_in_both_languages(self):
        import machine_view, machine_page
        from i18n import Translator
        self.report()
        summary = machine_view.summary()
        en = machine_page.machine_html({"machine": summary}, Translator("en"))
        ru = machine_page.machine_html({"machine": summary}, Translator("ru"))
        for text in ("Disk guard", "Largest files", "/data/alpha-web/big.img", "Running simulators", "Phone A"):
            self.assertIn(text, en)
        for text in ("Страж диска", "Самые большие файлы", "временные файлы", "Запущенные симуляторы", "мало места"):
            self.assertIn(text, ru)
        self.assertNotIn("&lt;span", en + ru, "a marked word passed as an argument prints its markup")

    def test_an_unrun_guard_says_how_to_run_it(self):
        import machine_view, machine_page
        from i18n import Translator
        en = machine_page.machine_html({"machine": machine_view.summary()}, Translator("en"))
        self.assertIn("The disk guard has not run yet", en)

    def test_the_command_line_takes_the_guard_and_refuses_an_unknown_level(self):
        self.assertEqual(workspace.parse(["disk-guard", "--level", "low"]).level, "low")
        with self.assertRaises(SystemExit):
            workspace.parse(["disk-guard", "--level", "extreme"])


if __name__ == "__main__":
    unittest.main()
