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
            try:
                os.utime(os.path.join(dirpath, n), (t, t), follow_symlinks=False)
            except FileNotFoundError:
                pass            # git's own short-lived files (a lock, a gc temp) come and go meanwhile
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
        self.real_fresh_busy = getattr(self.g, "fresh_busy", None)
        # No test probes the host process table or live holders.
        fresh = patch.object(self.g, "fresh_busy", return_value=False, create=True)
        fresh.start()
        self.addCleanup(fresh.stop)
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

    def identity(self, path):
        st = os.lstat(path)
        return {"dev": st.st_dev, "ino": st.st_ino}

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

    def test_a_root_with_its_own_hours_sweeps_old_runs_under_a_fresh_base(self):
        """pytest's base is as fresh as its newest run, so as one entry of the temporary root
        it never went; as its own root, each run goes by its own age, in hours."""
        base = self.t1 / "pytest-of-someone"
        for name, hours in (("pytest-1", 9), ("pytest-2", 3), ("pytest-3", 0)):
            (base / name).mkdir(parents=True)
            (base / name / "out").write_text("x" * 10)
            age(base / name, hours / 24)
        cfg = self.cfg(temp_roots=[{"path": str(self.t1), "depth": 1},
                                   {"path": str(base), "depth": 1,
                                    "hours": {"normal": 24, "low": 6, "critical": 2}}])
        names = lambda level: sorted(Path(a["target"]).name
                                     for a in self.g.plan_temp(cfg, level, self.free(), time.time()))
        self.assertEqual(names("normal"), [])
        self.assertEqual(names("low"), ["pytest-1"])
        self.assertEqual(names("critical"), ["pytest-1", "pytest-2"])

    def test_the_user_placeholder_is_the_login(self):
        import getpass
        self.assertEqual(self.g.expand("/x/pytest-of-{user}"), f"/x/pytest-of-{getpass.getuser()}")

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
    def test_paths_first_largest_first_commands_last_and_the_cap_is_per_class(self):
        for name, size in (("small", 10), ("large", 5000), ("mid", 300)):
            (self.t1 / name).mkdir()
            (self.t1 / name / "f").write_bytes(b"x" * size)
            age(self.t1 / name, 9)
        cfg = self.cfg(commands=[{"argv": ["python3", "-c", "pass"], "level": "normal"}])
        plan = self.g.build_plan(cfg, "normal", self.free(), time.time(), [])
        self.assertEqual(plan[-1]["class"], "cache-command", "a tool's command waits behind the paths")
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
        rec = self.g.act({"class": "temp", "target": str(self.t1 / "x"), "identity": self.identity(self.t1 / "x"), "bytes": 0},
                         self.free(self.t1 / "x" / "opened-since"))
        self.assertEqual(rec["result"], "skipped")
        self.assertTrue((self.t1 / "x").exists())

    def test_a_removal_records_what_the_disk_said(self):
        (self.t1 / "x").mkdir()
        frees = iter([1000 * 1048576, 1250 * 1048576])
        with patch.object(self.g, "free_bytes", side_effect=lambda *_: next(frees)):
            rec = self.g.act({"class": "temp", "target": str(self.t1 / "x"), "identity": self.identity(self.t1 / "x"), "bytes": 4096}, self.free())
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

    def test_a_command_held_up_on_a_lock_is_skipped_quickly(self):
        a = {"class": "cache-command", "target": "tool prune", "busy_if": [], "needs": [], "timeout": 1,
             "argv": [sys.executable, "-c", "import time; time.sleep(30)"]}
        started = time.monotonic()
        with patch.object(self.g, "running", return_value=False):
            rec = self.g.act(a, self.free())
        self.assertEqual(rec["result"], "skipped")
        self.assertLess(time.monotonic() - started, 10)

    def test_commands_are_chosen_by_pressure_and_by_what_is_installed(self):
        cfg = self.cfg(commands=[{"argv": ["python3", "-c", "pass"], "level": "normal"},
                                 {"argv": ["python3", "-V"], "level": "critical"},
                                 {"argv": ["no-such-tool-here", "prune"], "level": "normal"}])
        self.assertEqual([a["target"] for a in self.g.plan_commands(cfg, "normal")], ["python3 -c pass"])
        self.assertEqual(len(self.g.plan_commands(cfg, "critical")), 2)


class CacheDirs(Base):
    def test_old_unheld_caches_go_and_held_fresh_and_kept_ones_stay(self):
        caches = self.base / "Caches"
        for name in ("com.example.editor", "held-app", "fresh-tool", "ms-playwright"):
            (caches / name).mkdir(parents=True)
            (caches / name / "blob").write_bytes(b"x" * 10)
        age(caches, 9)
        (caches / "fresh-tool" / "new").write_text("now")
        cfg = self.cfg(cache_roots=[str(caches)], cache_keep=["ms-playwright"])
        plan = self.g.plan_cache_dirs(cfg, "normal", self.free(caches / "held-app" / "blob"), time.time())
        self.assertEqual([Path(a["target"]).name for a in plan], ["com.example.editor"])
        self.assertEqual(plan[0]["class"], "cache-dir")

    def test_pressure_shortens_the_age(self):
        caches = self.base / "Caches"
        (caches / "tool").mkdir(parents=True)
        age(caches, 3)
        cfg = self.cfg(cache_roots=[str(caches)], cache_keep=[])
        self.assertEqual(self.g.plan_cache_dirs(cfg, "normal", self.free(), time.time()), [])
        self.assertEqual(len(self.g.plan_cache_dirs(cfg, "low", self.free(), time.time())), 1)


class Cadence(Base):
    def run_auto(self, previous_at, free_gb):
        import paths
        paths.SCRATCH.mkdir(parents=True, exist_ok=True)
        (paths.SCRATCH / "disk-guard.json").write_text(json.dumps({"at": previous_at, "acting": True}))
        settings = self.home / "config" / "settings.json"
        doc = json.loads(settings.read_text())
        doc.setdefault("features", {})["auto_cleanup"] = True
        settings.write_text(json.dumps(doc))
        with patch.object(self.g, "load_config", return_value=self.cfg()), \
                patch.object(self.g, "free_bytes", return_value=int(free_gb * self.g.GB)), \
                patch.object(self.g.Holders, "take", return_value=self.free()), \
                patch.object(self.g, "refresh", return_value="fresh"), \
                patch.object(self.g, "large_files", return_value=None), \
                patch.object(self.g.subprocess, "run", side_effect=OSError):
            return self.g.run(["--auto"])

    def test_normal_pressure_acts_hourly_and_low_pressure_every_pass(self):
        just_now = self.g.now()
        self.assertTrue(self.run_auto(just_now, 100).get("not_due"), "normal, ran a minute ago: not due")
        self.assertFalse(self.run_auto(just_now, 20).get("not_due"), "low: every pass")
        self.assertFalse(self.run_auto("2026-01-01T00:00:00Z", 100).get("not_due"), "normal, an hour ago: due")


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
            {"measured_at": self.g.now(), "checkouts": [{"path": str(r), "worktrees": []} for r in repos]}))

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
        self.assertEqual(doc["degraded"][0]["source"], "holders")

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
        rows = [json.loads(l) for l in (paths.STATE / "logs" / "disk-guard.jsonl").read_text().splitlines()]
        self.assertEqual(rows[-1]["kind"], "summary", "every run ends with its summary row")
        rows = [r for r in rows if r.get("kind") != "summary"]
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
            stack.enter_context(patch.object(self.g, "planners", return_value=[
                ("temp", lambda b: [{"class": "temp", "target": str(self.t1 / n), "bytes": 0} for n in "abc"])]))
            doc = self.g.run(["--apply", "--budget", "0"])
        self.assertEqual(doc["results"]["removed"], 0)
        self.assertTrue(all((self.t1 / n).exists() for n in "abc"))
        self.assertIn("act", [d["source"] for d in doc["degraded"]])
        self.assertEqual(ran, [], "no survey is refreshed on a spent budget")
        self.assertEqual(doc["surveys"]["git-cleanup"], {"result": "skipped", "reason": "the run budget is spent"})
        import paths
        self.assertTrue((paths.SCRATCH / "disk-guard.json").exists())

    def test_the_git_tier_keeps_its_share_when_the_classes_use_all_of_theirs(self):
        """2026-10-12, the first scheduled 0.24.0 pass: six classes spent 425 of 480 s and the git
        tier — worktrees and their dependencies — was skipped. It now has its own share."""
        clock = [1000.0]
        got = []

        def busy_planner(b):
            clock[0] += max(0.0, b.left()) * 2          # the class uses its whole share
            return []
        with contextlib.ExitStack() as stack:
            for p in self.patched():
                stack.enter_context(p)
            stack.enter_context(patch.object(self.g, "load_config",
                                             return_value=self.cfg(run_reserve_seconds=60, git_tier_seconds=120)))
            stack.enter_context(patch.object(self.g.time, "monotonic", lambda: clock[0]))
            stack.enter_context(patch.object(self.g, "refresh", return_value="x"))
            stack.enter_context(patch.object(self.g, "planners", return_value=[
                (name, busy_planner) for name in self.g.CLASSES]))
            stack.enter_context(patch.object(self.g, "_git_tier",
                                             side_effect=lambda apply, level, seconds: got.append(seconds) or {"result": "ran"}))
            doc = self.g.run(["--apply", "--budget", "480", "--level", "critical"])
        self.assertEqual(doc["surveys"]["git-cleanup"], {"result": "ran"})
        self.assertTrue(got and got[0] >= 110, got)

    def test_a_command_never_runs_past_its_class_share(self):
        """2026-10-12: cache commands ran 271 s, each within its own 90-300 s timeout but together
        far past their share, and the git tier was skipped again. Each command's timeout is now
        what is left of the share, and none starts with less than 30 s left."""
        clock = [1000.0]
        timeouts = []

        def run_cmd(argv, **kw):
            timeouts.append(kw.get("timeout"))
            clock[0] += kw.get("timeout") or 0          # each command uses all the time it is given
            return subprocess.CompletedProcess(argv, 0, "", "")
        cmds = [{"class": "cache-command", "target": f"tool{i} clean", "argv": ["tool", str(i)], "timeout": 400}
                for i in range(5)]
        with contextlib.ExitStack() as stack:
            for p in self.patched():
                stack.enter_context(p)
            stack.enter_context(patch.object(self.g, "load_config",
                                             return_value=self.cfg(run_reserve_seconds=60, git_tier_seconds=120)))
            stack.enter_context(patch.object(self.g.time, "monotonic", lambda: clock[0]))
            stack.enter_context(patch.object(self.g, "refresh", return_value="x"))
            stack.enter_context(patch.object(self.g, "free_bytes", return_value=10 * 2**30))
            stack.enter_context(patch.object(self.g.subprocess, "run", side_effect=run_cmd))
            stack.enter_context(patch.object(self.g, "planners", return_value=[
                (name, (lambda b, n=name: cmds if n == "cache-command" else [])) for name in self.g.CLASSES]))
            stack.enter_context(patch.object(self.g, "_git_tier",
                                             side_effect=lambda apply, level, seconds: {"result": "ran", "seconds": seconds}))
            doc = self.g.run(["--apply", "--budget", "480", "--level", "critical"])
        self.assertLess(sum(timeouts), 480 - 60 - 120 + 1, timeouts)
        self.assertEqual(doc["surveys"]["git-cleanup"]["result"], "ran")
        self.assertGreaterEqual(doc["surveys"]["git-cleanup"]["seconds"], 110)

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
        rows = [json.loads(l) for l in (paths.STATE / "logs" / "disk-guard.jsonl").read_text().splitlines()]
        rows = [r for r in rows if r.get("kind") != "summary"]
        self.assertEqual(len(rows), 1, "the first removal is on the record before the second began")
        self.assertEqual(rows[0]["result"], "removed")
        report = json.loads((paths.SCRATCH / "disk-guard.json").read_text())
        self.assertEqual(report["results"]["removed"], 1)


class AnyDevice(Base):
    """The guard on macOS, Linux and Windows: one rule, three ways to know what is held."""

    def test_windows_probes_by_renaming_and_skips_what_is_held(self):
        (self.t1 / "held").mkdir()
        (self.t1 / "free").mkdir()
        h = self.g.Holders([])
        h.mode = "rename"
        self.assertFalse(h.busy(self.t1 / "held"), "Windows cannot know beforehand; the rename decides")
        real_rename = os.rename

        def rename(src, dst):
            if pathlib_name(src) == "held":
                raise PermissionError(32, "being used by another process")
            return real_rename(src, dst)
        with patch.object(self.g.os, "rename", side_effect=rename):
            held = self.g.act({"class": "temp", "target": str(self.t1 / "held"), "identity": self.identity(self.t1 / "held"), "bytes": 0}, h)
            free = self.g.act({"class": "temp", "target": str(self.t1 / "free"), "identity": self.identity(self.t1 / "free"), "bytes": 0}, h)
        self.assertEqual(held["result"], "skipped")
        self.assertTrue((self.t1 / "held").exists(), "nothing of a held folder is deleted")
        self.assertEqual(free["result"], "removed")
        self.assertFalse((self.t1 / "free").exists())
        self.assertFalse(list(self.t1.glob("*.observatory-removing")))

    def test_linux_without_lsof_reads_proc(self):
        if not os.path.isdir("/proc/self/fd"):
            self.skipTest("no /proc here")
        here = os.getcwd()
        with patch.object(self.g.shutil, "which", return_value=None):
            h = self.g.Holders.take()
        self.assertEqual(h.mode, "proc")
        self.assertTrue(h.busy(here), "this process works inside its own cwd")

    def test_no_way_to_know_means_nothing_is_removed(self):
        with patch.object(self.g.shutil, "which", return_value=None), \
                patch.object(self.g.os.path, "isdir", return_value=False), \
                patch.object(self.g.sys, "platform", "darwin"):
            h = self.g.Holders.take()
        self.assertFalse(h.known)
        self.assertTrue(h.busy(self.t1))

    def test_a_root_of_another_os_is_skipped_not_read_as_here(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOCALAPPDATA", None)
            self.assertEqual(self.g.expand("{localappdata}/npm-cache/_npx"), "")
        cfg = self.cfg(cache_roots=["{localappdata}/npm-cache/_npx"], cache_keep=[])
        self.assertEqual(self.g.plan_cache_dirs(cfg, "critical", self.free(), time.time()), [])


class GitTierPressure(Base):
    def test_pressure_lowers_only_the_tier_that_loses_nothing(self):
        import cleanup
        cfg = {"auto": {"clean_worktree_idle_days": 7, "merged_branch_idle_days": 7},
               "manual": {"dirty_worktree_idle_days": 7},
               "pressure": {"critical": {"clean_worktree_idle_days": 1, "merged_branch_idle_days": 30,
                                         "dirty_worktree_idle_days": 1}}}
        got = cleanup.under_pressure(cfg, "critical")
        self.assertEqual(got["auto"]["clean_worktree_idle_days"], 1)
        self.assertEqual(got["auto"]["merged_branch_idle_days"], 7, "pressure never raises a threshold")
        self.assertEqual(got["manual"]["dirty_worktree_idle_days"], 7, "the only copy of work never moves")
        self.assertIs(cleanup.under_pressure(cfg, "normal"), cfg)

    def test_an_older_workspace_file_still_gets_the_pressure_table(self):
        import importlib, cleanup, paths
        own = paths.config_file("cleanup.json")
        own.parent.mkdir(parents=True, exist_ok=True)
        own.write_text(json.dumps({"auto": {"clean_worktree_idle_days": 30, "merged_branch_idle_days": 7},
                                   "manual": {}}))
        cleanup = importlib.reload(cleanup)
        cfg = cleanup.load_config()
        self.assertEqual(cfg["auto"]["clean_worktree_idle_days"], 30, "the workspace's own value wins")
        self.assertIn("critical", cfg.get("pressure") or {}, "a key added by a release reaches it")
        lowered = cleanup.under_pressure(cfg, "critical")["auto"]
        self.assertEqual(lowered["clean_worktree_idle_days"], 30, "a person's own threshold is not lowered")
        self.assertEqual(lowered["merged_branch_idle_days"], 1, "a copied default is")

    def test_the_guard_hands_its_pressure_to_the_git_tier(self):
        seen = []

        def fake_run(argv, **kw):
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "cleanup: 0 removed\n", "")
        with patch.object(self.g, "load_config", return_value=self.cfg()), \
                patch.object(self.g.Holders, "take", return_value=self.free()), \
                patch.object(self.g, "refresh", return_value="fresh"), \
                patch.object(self.g, "large_files", return_value=None), \
                patch.object(self.g.subprocess, "run", side_effect=fake_run):
            self.g.run(["--apply", "--level", "critical"])
        calls = [a for a in seen if a and str(a[1]).endswith("cleanup.py")]
        self.assertTrue(calls, seen)
        argv = calls[0]
        self.assertEqual(argv[argv.index("--level") + 1], "critical")
        self.assertIn("--deadline-seconds", argv, "the git tier stops itself inside the guard's time")
        self.assertIn("--git-only", argv, "the guard's own build-output class walks build output; "
                                          "the git tier must not spend its share walking it again")


def pathlib_name(p) -> str:
    return Path(str(p)).name


class StaleRows(Base):
    """ENG-009: a git-hygiene pass the deadline cut short carries its unreached checkouts with
    `stale: true`, and the cleanup planned auto actions from them as from fresh rows. The act-time
    re-checks cover merge state, holders and cleanliness, not recency; the auto tier acts with
    no person present, so it waits for a fresh pass. The manual tier only proposes: its rows
    stay, marked with the age a person needs to see."""

    def checkout(self, name: str, measured_at: str, stale: bool) -> dict:
        row = {"repository": f"example-org/{name}", "path": str(self.base / name),
               "default_branch": "main", "measured_at": measured_at,
               "branches": [{"name": "old-feature", "class": "merged", "idle_days": 40},
                            {"name": "spike", "class": "unique", "ahead": 2, "idle_days": 40}],
               "worktrees": [{"path": str(self.base / f"{name}-gone"), "state": "missing", "idle_days": 40},
                             {"path": str(self.base / f"{name}-clean"), "state": "clean", "branch": "x", "idle_days": 40},
                             {"path": str(self.base / f"{name}-dirty"), "state": "dirty", "branch": "y",
                              "dirty_files": 3, "idle_days": 40}]}
        return {**row, "stale": True} if stale else row

    def hygiene(self, stale: bool, quiet_fresh: bool = False) -> dict:
        """One checkout this pass measured and, when `stale`, one it carried from an earlier pass:
        a reader that counted every row as carried, or none, gets a different number."""
        fresh = self.checkout("beta-api", "2026-10-12T09:00:00Z", stale=False)
        if quiet_fresh:
            fresh.update(branches=[], worktrees=[])
        rows = [fresh,
                self.checkout("alpha-web", "2026-10-01T09:00:00Z", stale=stale)]
        return {"measured_at": "2026-10-12T09:00:00Z", "partial": stale, "checkouts": rows}

    def test_a_stale_checkout_yields_no_auto_action_and_marked_manual_ones(self):
        import cleanup
        cfg = cleanup.load_config()
        stale_repo, fresh_repo = str(self.base / "alpha-web"), str(self.base / "beta-api")
        fresh = cleanup.plan_git(self.hygiene(stale=False), cfg)
        self.assertEqual(sorted(a["class"] for a in fresh if a["tier"] == "auto" and a["repo"] == stale_repo),
                         ["branch-merged", "worktree-clean", "worktree-missing"], "the fixture must plan auto work")
        plan = cleanup.plan_git(self.hygiene(stale=True), cfg)
        self.assertEqual([a for a in plan if a["tier"] == "auto" and a["repo"] == stale_repo], [],
                         "an auto action from a carried row")
        self.assertEqual(sorted(a["class"] for a in plan if a["tier"] == "auto" and a["repo"] == fresh_repo),
                         ["branch-merged", "worktree-clean", "worktree-missing"], "the fresh checkout still plans")
        manual = [a for a in plan if a["tier"] == "manual" and a["repo"] == stale_repo]
        self.assertEqual(sorted(a["class"] for a in manual), ["branch-unique", "worktree-dirty"])
        for a in manual:
            self.assertIs(a.get("stale"), True)
            self.assertEqual(a.get("measured_at"), "2026-10-01T09:00:00Z")
        self.assertTrue(all("stale" not in a for a in fresh), "a fresh row is not marked")
        self.assertTrue(all("stale" not in a for a in plan if a["repo"] == fresh_repo))

    def test_the_plan_and_the_page_count_carried_rows_with_one_function(self):
        import cleanup, machine_view
        # Reloaded in order, since other cases reload machine_view: cleanup must take the function
        # machine_view defines, not a copy of its own.
        machine_view = importlib.reload(machine_view)
        cleanup = importlib.reload(cleanup)
        self.assertIs(cleanup.carried, machine_view.carried, "two readers of `stale` would drift apart")
        self.assertEqual(cleanup.carried(self.hygiene(stale=True)),
                         {"checkouts": 1, "measured_at": "2026-10-01T09:00:00Z"})
        self.assertIsNone(cleanup.carried(self.hygiene(stale=False)))

    def enable_auto(self):
        import paths
        settings = self.home / "config" / "settings.json"
        doc = json.loads(settings.read_text())
        doc.setdefault("features", {})["auto_cleanup"] = True
        settings.write_text(json.dumps(doc))
        paths.SCRATCH.mkdir(parents=True, exist_ok=True)
        # The fresh checkout holds nothing to clean here, so the run has nothing to act on at all and
        # every count it prints is the carried one.
        (paths.SCRATCH / "git-hygiene.json").write_text(json.dumps(self.hygiene(stale=True, quiet_fresh=True)))
        return paths

    def test_the_plan_and_the_auto_run_say_what_waits_for_a_fresh_pass(self):
        paths = self.enable_auto()
        done = subprocess.run([sys.executable, str(ROOT / "tools/cleanup.py"), "--auto", "--git-only"],
                              cwd=ROOT, env={**os.environ}, capture_output=True, text=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stderr[-400:])
        self.assertIn("cleanup: 1 checkouts carried from 2026-10-01T09:00:00Z; their auto actions wait for a fresh pass",
                      done.stdout)
        self.assertIn("0 removed", done.stdout)
        plan = json.loads((paths.SCRATCH / "cleanup-plan.json").read_text())
        self.assertEqual(plan["carried"], {"checkouts": 1, "measured_at": "2026-10-01T09:00:00Z"})
        self.assertIs(plan["survey_partial"], True)

    def test_the_guards_report_keeps_the_carried_count(self):
        """Under `--json` the line goes to stderr and the guard keeps only the JSON summary, so the
        count travels inside the summary and reaches the guard's report and journal."""
        self.enable_auto()
        done = subprocess.run([sys.executable, str(ROOT / "tools/cleanup.py"), "--auto", "--git-only", "--json"],
                              cwd=ROOT, env={**os.environ}, capture_output=True, text=True, timeout=300)
        summary = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(summary.get("carried"), {"checkouts": 1, "measured_at": "2026-10-01T09:00:00Z"})
        tier = self.g._git_tier(False, "normal", 120)
        self.assertEqual(tier.get("carried"), {"checkouts": 1, "measured_at": "2026-10-01T09:00:00Z"})

    def test_the_machine_view_shows_the_partial_survey_and_its_carried_count(self):
        import paths
        import machine_view
        importlib.reload(machine_view)
        paths.SCRATCH.mkdir(parents=True, exist_ok=True)
        (paths.SCRATCH / "git-hygiene.json").write_text(json.dumps(self.hygiene(stale=True)))
        git_view = machine_view.summary()["git"]
        self.assertIs(git_view["partial"], True)
        self.assertEqual(git_view["carried"], {"checkouts": 1, "measured_at": "2026-10-01T09:00:00Z"})


class SecondIteration(Base):
    """The rules the three-tier review of 2026-10-12 set (docs/design/DISK-GUARD.md)."""

    def test_a_target_touched_after_the_plan_is_kept(self):
        old = self.t1 / "old"
        old.mkdir()
        (old / "f").write_text("x")
        age(old, 9)
        plan = self.g.plan_temp(self.cfg(), "normal", self.free(), time.time())
        self.assertEqual(len(plan), 1)
        (old / "written-since").write_text("now")
        rec = self.g.act(plan[0], self.free())
        self.assertEqual(rec["result"], "skipped")
        self.assertIn("changed since the plan", rec["reason"])
        self.assertTrue(old.exists())

    def test_a_tree_holding_another_accounts_file_is_not_removable(self):
        old = self.t1 / "mixed"
        (old / "sub").mkdir(parents=True)
        (old / "sub" / "root-owned").write_text("x")
        age(old, 9)
        real = self.g.osprivacy.owns
        with patch.object(self.g.osprivacy, "owns",
                          side_effect=lambda st: False if st.st_size == 1 and not stat_is_dir(st) else real(st)):
            r = self.g.inspect(str(old))
        self.assertFalse(r["complete"])
        self.assertIn("another account", r["reason"])

    def test_a_privacy_guarded_root_is_not_opened(self):
        guarded = self.base / "Documents" / "caches"
        (guarded / "tool").mkdir(parents=True)
        age(guarded, 9)
        degraded = []
        cfg = self.cfg(cache_roots=[str(guarded)], cache_keep=[])
        with patch("scan_machine.protected_place", return_value=True):
            plan = self.g.plan_cache_dirs(cfg, "critical", self.free(), time.time(), None, degraded)
        self.assertEqual(plan, [])
        self.assertEqual(degraded[0]["source"], "privacy")

    def test_a_held_lock_makes_the_run_a_busy_skip(self):
        import cleanup_lock
        (self.t1 / "old").mkdir()
        age(self.t1 / "old", 9)
        with cleanup_lock.held() as mine:
            self.assertTrue(mine)
            with patch.object(self.g, "load_config", return_value=self.cfg()), \
                    patch.dict(os.environ, {"OBSERVATORY_CLEANUP_LOCK_HELD": ""}):
                child = subprocess.run([sys.executable, str(ROOT / "tools" / "disk_guard.py"), "--apply", "--json"],
                                       capture_output=True, text=True, timeout=120,
                                       env={**os.environ, "OBSERVATORY_CLEANUP_LOCK_HELD": ""})
        doc = json.loads(child.stdout.strip().splitlines()[-1])
        self.assertTrue(doc["busy"])
        self.assertTrue((self.t1 / "old").exists())

    def test_cadence_is_measured_from_the_previous_start(self):
        import paths
        paths.SCRATCH.mkdir(parents=True, exist_ok=True)
        started = time.time() - 55 * 60                       # started 55 min ago, ended 50 min ago
        stamp = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))  # noqa: E731
        (paths.SCRATCH / "disk-guard.json").write_text(json.dumps(
            {"started_at": stamp(started), "at": stamp(started + 300), "acting": True}))
        settings = self.home / "config" / "settings.json"
        doc = json.loads(settings.read_text())
        doc.setdefault("features", {})["auto_cleanup"] = True
        settings.write_text(json.dumps(doc))
        with patch.object(self.g, "load_config", return_value=self.cfg()), \
                patch.object(self.g, "free_bytes", return_value=100 * self.g.GB), \
                patch.object(self.g.Holders, "take", return_value=self.free()), \
                patch.object(self.g, "refresh", return_value="fresh"), \
                patch.object(self.g, "large_files", return_value=None), \
                patch.object(self.g.subprocess, "run", side_effect=OSError):
            out = self.g.run(["--auto"])
        self.assertFalse(out.get("not_due"), "55 minutes after the last START is due (tolerance 7.5 min)")

    def test_a_whole_cache_clean_runs_at_most_once_a_day(self):
        cfg = self.cfg(commands=[{"argv": ["python3", "-c", "pass"], "level": "low", "every_hours": 24}])
        now_t = time.time()
        recent = {"python3 -c pass": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_t - 3600))}
        self.assertEqual(self.g.plan_commands(cfg, "low", recent, now_t), [])
        day_old = {"python3 -c pass": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_t - 25 * 3600))}
        self.assertEqual(len(self.g.plan_commands(cfg, "low", day_old, now_t)), 1)

    def test_an_unreadable_process_table_skips_commands(self):
        a = {"class": "cache-command", "target": "tool prune", "argv": [sys.executable, "-c", "pass"],
             "busy_if": ["tool"], "needs": []}
        with patch.object(self.g, "running", return_value=None):
            rec = self.g.act(a, self.free())
        self.assertEqual(rec["result"], "skipped")
        self.assertIn("could not be read", rec["reason"])

    def test_a_failed_removal_after_the_rename_puts_the_folder_back(self):
        (self.t1 / "keep").mkdir()
        (self.t1 / "keep" / "f").write_text("x")
        with patch.object(self.g, "_remove_now", side_effect=OSError(5, "I/O error")):
            with self.assertRaises(OSError):
                self.g.remove(str(self.t1 / "keep"), probe=True)
        self.assertTrue((self.t1 / "keep" / "f").exists())
        self.assertFalse(list(self.t1.glob("*.observatory-removing")))


class HoldersModule(unittest.TestCase):
    def test_lsof_escapes_are_decoded(self):
        import holders
        self.assertEqual(holders._decode_escapes("/tmp/\\xd1\\x84.bin".replace("\\\\", "\\")), "/tmp/ф.bin")

    def test_a_path_named_on_a_command_line_is_held(self):
        import holders
        h = holders.Holders(["/srv/cache/_npx/abc/node_modules/.bin/server"], "lsof")
        self.assertTrue(h.busy("/srv/cache/_npx/abc"))
        self.assertFalse(h.busy("/srv/cache/_npx/other"))
        self.assertFalse(h.busy("/srv/cache/_npx/ab"), "a sibling with a shared prefix is free")


class GitTierSafety(Base):
    def test_manual_removal_preserves_ignored_local_data(self):
        import cleanup
        repo = self.base / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        (repo / ".gitignore").write_text(".env.local\n")
        (repo / "a").write_text("original\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "init")
        wt = self.base / "worktree"
        git(repo, "worktree", "add", "-qb", "topic", str(wt))
        (wt / "a").write_text("uncommitted\n")
        (wt / ".env.local").write_text("synthetic local data\n")
        with patch.object(cleanup, "busy_now", return_value=False):
            rec = cleanup.act({"class": "worktree-dirty", "tier": "manual", "repo": str(repo),
                               "target": str(wt), "branch": "topic"}, self.base / "archive")
        self.assertEqual(rec["result"], "skipped", rec)
        self.assertIn("ignored", rec["reason"])
        self.assertEqual((wt / ".env.local").read_text(), "synthetic local data\n")
        self.assertEqual((wt / "a").read_text(), "uncommitted\n")

    def test_ignored_files_appearing_during_archive_keep_the_worktree(self):
        import cleanup
        repo = self.base / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        (repo / ".gitignore").write_text(".env.local\n")
        (repo / "a").write_text("original\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "init")
        wt = self.base / "worktree"
        git(repo, "worktree", "add", "-qb", "topic", str(wt))
        (wt / "a").write_text("uncommitted\n")
        export = cleanup.export_tracked

        def create_ignored(target, status):
            result = export(target, status)
            (wt / ".env.local").write_text("arrived during archive")
            return result

        with patch.object(cleanup, "busy_now", return_value=False), \
                patch.object(cleanup, "export_tracked", side_effect=create_ignored):
            rec = cleanup.act({"class": "worktree-dirty", "tier": "manual", "repo": str(repo),
                               "target": str(wt), "branch": "topic"}, self.base / "archive")
        self.assertEqual(rec["result"], "skipped", rec)
        self.assertEqual((wt / ".env.local").read_text(), "arrived during archive")
        self.assertTrue((Path(rec["archive"]) / "tracked.patch").exists())

    def test_local_state_is_protected_even_by_an_older_artifact_configuration(self):
        import cleanup
        import paths
        paths.config_file("cleanup.json").write_text(json.dumps({"artifact_dirs": [".wrangler", "dist"]}))
        self.assertNotIn(".wrangler", self.g.artifact_names())
        self.assertNotIn(".wrangler", cleanup.load_config()["artifact_dirs"])
        repo = self.base / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / ".gitignore").write_text(".wrangler/\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "init")
        (repo / ".wrangler" / "state").mkdir(parents=True)
        (repo / ".wrangler" / "state" / "db.sqlite").write_text("unique local data")
        self.assertEqual(cleanup.ignored_keepers(str(repo), {".wrangler", "dist"}), [".wrangler"])
        age(repo, 30)
        paths.SCRATCH.mkdir(parents=True, exist_ok=True)
        (paths.SCRATCH / "git-hygiene.json").write_text(json.dumps({"measured_at": self.g.now(), "checkouts": [{"path": str(repo), "worktrees": []}]}))
        self.assertEqual(self.g.plan_builds(self.g.load_config(), "critical", self.free(),
                                          time.time(), [".wrangler"]), [])
        with patch.object(cleanup, "idle_projects", return_value=[{"id": "alpha-web", "folders": [str(repo)], "idle_days": 30}]):
            self.assertEqual(cleanup.plan_artifacts({"artifact_dirs": [".wrangler"]}, self.free()), [])

    def test_artifact_names_below_local_state_never_make_it_disposable(self):
        import cleanup
        import paths
        repo = self.base / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / ".gitignore").write_text(".wrangler/state/dist/\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "init")
        state = repo / ".wrangler" / "state" / "dist"
        state.mkdir(parents=True)
        (state / "database").write_text("unique local state")
        self.assertEqual(cleanup.ignored_keepers(str(repo), {"dist"}), [".wrangler/state/dist"])
        age(repo, 30)
        paths.SCRATCH.mkdir(parents=True, exist_ok=True)
        (paths.SCRATCH / "git-hygiene.json").write_text(json.dumps({"measured_at": self.g.now(), "checkouts": [{"path": str(repo), "worktrees": []}]}))
        self.assertEqual(self.g.plan_builds(self.g.load_config(), "critical", self.free(),
                                          time.time(), ["dist"]), [])
        with patch.object(cleanup, "idle_projects", return_value=[{"id": "alpha-web", "folders": [str(repo)], "idle_days": 30}]):
            self.assertEqual(cleanup.plan_artifacts({"artifact_dirs": ["dist"]}, self.free()), [])

    def test_ignored_artifact_parent_keeps_nested_state_and_git(self):
        import cleanup
        repo = self.base / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / ".gitignore").write_text("dist/\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "init")
        for marker in (".wrangler", ".git"):
            with self.subTest(marker=marker):
                nested = repo / "dist" / "nested" / marker
                nested.mkdir(parents=True)
                (nested / "only-copy").write_text("unique data")
                self.assertEqual(cleanup.ignored_keepers(str(repo), {"dist"}), ["dist"])
                with patch.object(cleanup, "idle_projects", return_value=[{"id": "alpha-web", "folders": [str(repo)], "idle_days": 30}]):
                    self.assertEqual(cleanup.plan_artifacts({"artifact_dirs": ["dist"]}, self.free()), [])
                with patch.object(cleanup, "busy_now", return_value=False):
                    rec = cleanup.act({"class": "build-artifacts", "tier": "auto", "target": str(repo / "dist")}, self.base / "archive")
                self.assertEqual(rec["result"], "skipped")
                self.assertTrue((nested / "only-copy").exists())
                import shutil
                shutil.rmtree(repo / "dist")

    def test_uncertain_artifact_walk_is_a_keeper(self):
        import cleanup
        import cleanup_safety
        repo = self.base / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / ".gitignore").write_text("dist/\n")
        (repo / "dist").mkdir()
        (repo / "dist" / "output").write_text("regenerable")
        self.assertEqual(cleanup.ignored_keepers(str(repo), {"dist"}), [])
        with patch.object(cleanup_safety.os, "scandir", side_effect=PermissionError):
            self.assertEqual(cleanup.ignored_keepers(str(repo), {"dist"}), ["dist"])
        self.assertTrue(cleanup_safety.protected_tree_reason(str(repo / "dist"), timeout=0))
        self.assertTrue(cleanup_safety.protected_tree_reason(str(repo / "dist"), max_entries=0))
        (repo / "dist" / "link").symlink_to(repo / "elsewhere")
        self.assertEqual(cleanup.ignored_keepers(str(repo), {"dist"}), ["dist"])

    def test_a_clean_worktree_with_a_keeper_ignored_file_is_skipped(self):
        import cleanup
        repo = self.base / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / ".gitignore").write_text(".env.local\nnode_modules/\n")
        (repo / "a").write_text("x")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "init")
        (repo / ".env.local").write_text("LOCAL=1")
        (repo / "node_modules").mkdir()
        (repo / "node_modules" / "m.js").write_text("y")
        keep = cleanup.ignored_keepers(str(repo), {"node_modules"})
        self.assertEqual(keep, [".env.local"])
        os.remove(repo / ".env.local")
        self.assertEqual(cleanup.ignored_keepers(str(repo), {"node_modules"}), [])

    def test_build_output_with_a_force_tracked_file_is_not_planned(self):
        r = self.base / "forced"
        r.mkdir()
        git(r, "init", "-q")
        (r / ".gitignore").write_text("dist/\n")
        (r / "dist").mkdir()
        (r / "dist" / "kept.txt").write_text("x")
        git(r, "add", ".gitignore")
        git(r, "add", "-f", "dist/kept.txt")
        git(r, "commit", "-qm", "forced")
        age(r, 30)
        import paths
        paths.SCRATCH.mkdir(parents=True, exist_ok=True)
        (paths.SCRATCH / "git-hygiene.json").write_text(json.dumps({"measured_at": self.g.now(), "checkouts": [{"path": str(r), "worktrees": []}]}))
        plan = self.g.plan_builds(self.g.load_config(), "critical", self.free(), time.time(), ["dist"])
        self.assertEqual(plan, [])
        # git check-ignore already calls such a folder not ignored; `_force_tracked` is the
        # second line, asked directly so it is proven on its own.
        self.assertTrue(self.g._force_tracked(str(r), str(r / "dist"), 30))
        (r / "out").mkdir()
        (r / "out" / "x").write_text("y")
        self.assertFalse(self.g._force_tracked(str(r), str(r / "out"), 30))


def stat_is_dir(st) -> bool:
    import stat as _stat
    return _stat.S_ISDIR(st.st_mode)


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
        self.assertTrue({"level", "acting", "free_gb_before", "free_gb_after", "freed_gb", "planned", "results",
                         "seconds", "not_due", "busy"} <= set(doc), sorted(doc))


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
        for text in ("Контроль диска", "Самые большие файлы", "временные файлы", "Запущенные симуляторы", "мало места"):
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


class TargetRevalidation(Builds):
    """Only synthetic targets; every rule preserves bytes on a changed/unknown decision."""

    def build_action(self):
        root = self.repo("alpha-web", 20)
        self.hygiene(root)
        action = self.g.plan_builds(self.g.load_config(), "normal", self.free(),
                                   time.time(), ["node_modules"])[0]
        return root, action

    def test_generic_temp_preserves_git_repository_worktree_and_nested_repository(self):
        for name, marker in (("repo", ".git/config"), ("linked", ".git"),
                             ("session", "scratch/repo/.git/config")):
            root = self.t1 / name
            (root / marker).parent.mkdir(parents=True, exist_ok=True)
            (root / marker).write_text("only copy")
            age(root, 10)
        self.assertEqual(self.g.plan_temp(self.cfg(), "critical", self.free(), time.time()), [])

    def test_generic_temp_preserves_paths_beneath_git_ancestry(self):
        (self.t1 / ".git").write_text("gitdir: unavailable")
        old = self.t1 / "scratch"
        old.mkdir()
        (old / "only-copy").write_text("keep")
        age(self.t1, 10)
        self.assertEqual(self.g.plan_temp(self.cfg(), "critical", self.free(), time.time()), [])

    def test_apply_rejects_new_git_marker_even_with_old_timestamps(self):
        root = self.t1 / "old"
        root.mkdir()
        age(root, 10)
        action = self.g.plan_temp(self.cfg(), "critical", self.free(), time.time())[0]
        (root / ".git").write_text("gitdir: unavailable")
        age(root, 10)
        self.assertEqual(self.g.act(action, self.free())["result"], "skipped")
        self.assertTrue((root / ".git").exists())

    def test_apply_build_preserves_force_added_output(self):
        root, action = self.build_action()
        git(root, "add", "-f", "node_modules/pkg/index.js")
        self.assertEqual(self.g.act(action, self.free())["result"], "skipped")
        self.assertTrue((root / "node_modules/pkg/index.js").exists())

    def test_apply_build_preserves_output_no_longer_ignored(self):
        root, action = self.build_action()
        (root / ".gitignore").write_text("")
        age(root, 20)
        self.assertEqual(self.g.act(action, self.free())["result"], "skipped")
        self.assertTrue((root / "node_modules").exists())

    def test_apply_build_rechecks_checkout_activity(self):
        root, action = self.build_action()
        (root / "app.js").write_text("active again")
        self.assertEqual(self.g.act(action, self.free())["result"], "skipped")
        self.assertTrue((root / "node_modules").exists())

    def test_apply_build_rechecks_new_ignored_file(self):
        root, action = self.build_action()
        (root / "node_modules/new-file").write_text("new work")
        self.assertEqual(self.g.act(action, self.free())["result"], "skipped")
        self.assertTrue((root / "node_modules/new-file").exists())

    def test_apply_build_preserves_nested_git_marker(self):
        root, action = self.build_action()
        (root / "node_modules/pkg/.git").write_text("gitdir: unavailable")
        age(root, 20)
        self.assertEqual(self.g.act(action, self.free())["result"], "skipped")
        self.assertTrue((root / "node_modules/pkg/.git").exists())

    def test_apply_build_refuses_missing_revalidation_context(self):
        root = self.repo("alpha-web", 20)
        action = {"class": "build-output", "target": str(root / "node_modules"),
                  "identity": self.identity(root / "node_modules")}
        self.assertEqual(self.g.act(action, self.free())["result"], "skipped")
        self.assertTrue((root / "node_modules").exists())

    def test_apply_build_still_removes_unchanged_ignored_output(self):
        root, action = self.build_action()
        self.assertEqual(self.g.act(action, self.free())["result"], "removed")
        self.assertFalse((root / "node_modules").exists())
        self.assertTrue((root / "app.js").exists())

    def test_each_action_uses_fresh_holders_and_unknown_is_a_skip(self):
        for outcome in (True, None):
            with self.subTest(outcome=outcome):
                root = self.t1 / str(outcome)
                root.mkdir()
                with patch.object(self.g, "fresh_busy", return_value=outcome) as fresh:
                    rec = self.g.act({"class": "temp", "target": str(root), "identity": self.identity(root)}, self.free())
                self.assertEqual(rec["result"], "skipped")
                self.assertTrue(root.exists())
                fresh.assert_called_once()

    def test_expired_inspection_deadline_preserves_even_a_small_tree(self):
        self.assertFalse(self.g.inspect(str(self.t1), deadline=time.monotonic() - 1)["complete"])

    def test_apply_build_preserves_nested_local_state(self):
        root, action = self.build_action()
        (root / "node_modules/pkg/.wrangler").mkdir()
        (root / "node_modules/pkg/.wrangler/db").write_text("only copy")
        age(root, 20)
        self.assertEqual(self.g.act(action, self.free())["result"], "skipped")
        self.assertTrue((root / "node_modules/pkg/.wrangler/db").exists())

    def test_fresh_build_probe_covers_checkout_activity(self):
        root, action = self.build_action()
        with patch.object(self.g, "fresh_busy", return_value=True) as fresh:
            self.assertEqual(self.g.act(action, self.free())["result"], "skipped")
        self.assertEqual(fresh.call_args.args[0], str(root))
        self.assertTrue((root / "node_modules").exists())

    def test_small_tree_with_unknown_stat_is_preserved(self):
        entry = self.t1 / "only-copy"
        entry.write_text("keep")
        with patch.object(self.g.os, "scandir", side_effect=PermissionError):
            self.assertFalse(self.g.inspect(str(self.t1))["complete"])
        self.assertTrue(entry.exists())


@unittest.skipIf(os.name == "nt", "POSIX target probe; Windows uses rename")
class FreshTargetProbe(Base):
    def test_target_probe_is_bounded_and_unknown_never_means_free(self):
        self.assertIsNotNone(self.real_fresh_busy, "fresh target probe must exist")
        command_results = [subprocess.CompletedProcess([], 1, "", ""),
                           subprocess.CompletedProcess([], 0, "/usr/bin/editor", "")]
        with patch.object(self.g.shutil, "which", return_value="/usr/sbin/lsof"), \
                patch.object(self.g.subprocess, "run", side_effect=command_results) as call:
            self.assertIs(self.real_fresh_busy(str(self.t1), 5), False)
        self.assertTrue(any(str(self.t1) in c.args[0] for c in call.call_args_list))
        self.assertTrue(all(0 < c.kwargs["timeout"] <= 5 for c in call.call_args_list))
        with patch.object(self.g.shutil, "which", return_value="/usr/sbin/lsof"), \
                patch.object(self.g.subprocess, "run", side_effect=subprocess.TimeoutExpired("lsof", 5)):
            self.assertIsNone(self.real_fresh_busy(str(self.t1), 5))

    def test_target_probe_refuses_partial_listing(self):
        self.assertIsNotNone(self.real_fresh_busy, "fresh target probe must exist")
        with patch.object(self.g.shutil, "which", return_value="/usr/sbin/lsof"), \
                patch.object(self.g.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "permission denied")):
            self.assertIsNone(self.real_fresh_busy(str(self.t1), 5))


class CoverageTruth(Base):
    def test_missing_swap_is_unknown_and_measured_zero_is_zero(self):
        for memory, expected in (({}, None), ({"swap_used_mb": None}, None),
                                 ({"swap_used_mb": 0}, 0.0), ({"swap_used_mb": 2048}, 2.0)):
            with self.subTest(memory=memory), patch.object(self.g, "_read", return_value={
                    "measured_at": "2026-01-01T00:00:00Z", "memory": memory}):
                report = self.g.memory_report()
            self.assertEqual(report["swap_gb"], expected)
            self.assertEqual(report["measured_at"], "2026-01-01T00:00:00Z")
            self.assertEqual(report.get("swap_reason"), "missing-swap-measurement" if expected is None else None)

    def test_unreadable_directory_is_incomplete_with_count_not_error_body(self):
        broken = self.base / "unreadable"
        broken.mkdir()
        original = self.g.os.scandir
        def scan(path):
            if str(path) == str(broken):
                raise PermissionError("private error body")
            return original(path)
        with patch.object(self.g.os, "scandir", side_effect=scan):
            report = self.g.large_files(self.cfg(), None, True)
        self.assertFalse(report["complete"])
        self.assertEqual(report["errors"]["directory_read"], 1)
        self.assertNotIn("private error body", json.dumps(report))

    def test_missing_root_is_an_incomplete_measurement_not_exception(self):
        with patch.object(self.g.pathlib.Path, "home", return_value=self.base / "missing"):
            report = self.g.large_files(self.cfg(), None, True)
        self.assertFalse(report["complete"])
        self.assertEqual(report["errors"]["root_stat"], 1)
        self.assertEqual(report["files"], [])
        self.assertTrue(report["measured_at"])

    def test_stat_failure_is_counted_and_prior_success_is_retained(self):
        from types import SimpleNamespace
        good = self.base / "good.bin"
        good.write_bytes(b"x" * 2048)
        good_entry = SimpleNamespace(name=good.name, path=str(good), stat=lambda **kw: good.stat())
        def unavailable(**kw):
            raise FileNotFoundError("removed during traversal")
        bad_entry = SimpleNamespace(name="gone", path=str(self.base / "gone"), stat=unavailable)
        class Entries(list):
            def __enter__(self): return self
            def __exit__(self, *args): return False
        with patch.object(self.g.os, "scandir", return_value=Entries([good_entry, bad_entry])):
            report = self.g.large_files(self.cfg(large_file_min_mb=0.001), None, True)
        self.assertFalse(report["complete"])
        self.assertEqual(report["errors"]["entry_stat"], 1)
        self.assertEqual([r["path"] for r in report["files"]], [str(good)])

    def test_complete_empty_measurement_has_explicit_zero_errors(self):
        empty = self.base / "empty"
        empty.mkdir()
        with patch.object(self.g.pathlib.Path, "home", return_value=empty):
            report = self.g.large_files(self.cfg(), None, True)
        self.assertTrue(report["complete"])
        self.assertEqual(report.get("errors"), {"root_stat": 0, "directory_read": 0, "entry_stat": 0})
        self.assertIs(report.get("budget_exhausted"), False)

    def test_page_does_not_call_failed_empty_scan_no_large_files(self):
        sys.path.insert(0, str(ROOT / "dashboard"))
        import machine_page
        from i18n import Translator
        guard = {"largeFiles": {"complete": False, "files": [], "errors": {"directory_read": 1}}}
        html = machine_page.guard_html(guard, Translator("en"))
        self.assertIn("incomplete measurement", html)
        self.assertNotIn("No file above the size line.", html)
        guard["largeFiles"]["complete"] = True
        self.assertIn("No file above the size line.", machine_page.guard_html(guard, Translator("en")))

    def test_mcp_overview_retains_coverage_and_unknown_swap(self):
        sys.path.insert(0, str(ROOT / "mcp"))
        import server, machine_view
        sample = {"degraded": [], "guard": {"largeFiles": {"measured_at": "2026-01-01T00:00:00Z",
                    "complete": False, "files": [], "errors": {"directory_read": 1}},
                    "memory": {"measured_at": "2026-01-01T00:00:00Z", "swap_gb": None,
                               "swap_reason": "missing-swap-measurement"}}}
        with patch.object(machine_view, "summary", return_value=sample):
            result = server.observatory_machine()
        self.assertFalse(result["guard"]["largeFilesCoverage"]["complete"])
        self.assertEqual(result["guard"]["largeFilesCoverage"]["errors"]["directory_read"], 1)
        self.assertIsNone(result["guard"]["memory"]["swap_gb"])


class SnapshotSafety(Base):
    def test_bare_repository_is_preserved_by_temp_and_shared_artifact_scan(self):
        import cleanup_safety
        bare = self.t1 / "only-copy.git"
        bare.mkdir()
        git(bare, "init", "--bare", "-q")
        age(bare, 9)
        self.assertEqual(self.g.plan_temp(self.cfg(), "critical", self.free(), time.time()), [])
        self.assertTrue(cleanup_safety.protected_tree_reason(str(bare)))
        self.assertFalse(self.g.inspect(str(bare))["complete"])
        self.assertTrue((bare / "HEAD").exists())

    def test_bare_repository_ancestry_preserves_selected_nested_temp_root(self):
        bare = self.t1 / "only-copy.git"
        bare.mkdir()
        git(bare, "init", "--bare", "-q")
        (bare / "objects" / "old-pack").write_text("only copy")
        age(bare, 9)
        cfg = self.cfg(temp_roots=[{"path": str(bare / "objects"), "depth": 1}])
        self.assertEqual(self.g.plan_temp(cfg, "critical", self.free(), time.time()), [])

    def test_bare_ancestry_and_nested_tree_protect_other_cleanup_classes(self):
        import cleanup_safety
        bare = self.t1 / "only-copy.git"
        bare.mkdir()
        git(bare, "init", "--bare", "-q")
        data = bare / "objects" / "old-data"
        data.mkdir()
        (data / "only-copy").write_text("keep")
        age(bare, 9)
        self.assertTrue(cleanup_safety.protected_tree_reason(str(data)))
        self.assertFalse(self.g.inspect(str(data))["complete"])
        self.assertFalse(self.g.inspect(str(self.t1))["complete"])
        self.assertEqual(self.g.plan_cache_dirs(
            self.cfg(cache_roots=[str(bare / "objects")], cache_keep=[]),
            "critical", self.free(), time.time()), [])

    def test_replaced_directory_is_preserved_even_with_eligible_timestamps(self):
        root = self.t1 / "old"
        root.mkdir()
        (root / "old-file").write_text("old")
        age(root, 9)
        action = self.g.plan_temp(self.cfg(), "critical", self.free(), time.time())[0]
        root.rename(self.t1 / "original")
        root.mkdir()
        (root / "new-only-copy").write_text("keep")
        age(root, 9)
        rec = self.g.act(action, self.free())
        self.assertEqual(rec["result"], "skipped")
        self.assertTrue((root / "new-only-copy").exists())

    def test_missing_identity_requires_replanning_for_every_path_class(self):
        for kind in ("temp", "cache-dir", "trash", "browser-clone", "build-output"):
            with self.subTest(kind=kind):
                root = self.t1 / kind
                root.mkdir()
                result = self.g.act({"class": kind, "target": str(root)}, self.free())
                self.assertEqual(result["result"], "skipped")
                self.assertIn("identity", result["reason"])
                self.assertTrue(root.exists())

    def test_every_path_planner_binds_device_and_inode(self):
        root = self.t1 / "old"
        root.mkdir()
        age(root, 9)
        plans = self.g.plan_temp(self.cfg(), "critical", self.free(), time.time())
        plans += self.g.plan_cache_dirs(self.cfg(cache_roots=[str(self.t1)], cache_keep=[]),
                                        "critical", self.free(), time.time())
        with patch.object(self.g, "trash_dirs", return_value=[str(self.t1)]):
            plans += self.g.plan_trash(self.cfg(), "critical", time.time() + 31 * DAY, [])
        clone = self.clones / "com.example.Browser.code_sign_clone" / "code_sign_clone.old"
        clone.mkdir(parents=True)
        age(clone, 9)
        plans += self.g.plan_clones(self.cfg(), self.free(), time.time())
        self.assertEqual({a["class"] for a in plans}, {"temp", "cache-dir", "trash", "browser-clone"})
        for action in plans:
            st = os.lstat(action["target"])
            self.assertEqual(action.get("identity"), {"dev": st.st_dev, "ino": st.st_ino})


class RegistrationIntegration(Base):
    def test_real_registration_child_lists_linked_worktree_without_freshening_hygiene(self):
        import paths
        root = self.t1 / "repo"
        root.mkdir()
        git(root, "init", "-q")
        (root / "source").write_text("keep")
        git(root, "add", ".")
        git(root, "commit", "-qm", "initial")
        linked = self.t1 / "linked"
        git(root, "worktree", "add", "-qb", "topic", str(linked))
        (self.home / "registry/repositories.json").write_text(json.dumps({"repositories": [
            {"id": "repository:example-org/repo", "local": {"path": str(root)}}]}))
        (self.home / "registry/projects.json").write_text(json.dumps({"projects": []}))
        paths.SCRATCH.mkdir(parents=True, exist_ok=True)
        hygiene = paths.SCRATCH / "git-hygiene.json"
        hygiene.write_text(json.dumps({"measured_at": "2000-01-01T00:00:00Z", "checkouts": []}))
        before, stamp = hygiene.read_bytes(), hygiene.stat().st_mtime_ns
        with patch.dict(os.environ, {"OBSERVATORY_STEP_DEADLINE": "1"}):
            result = self.g.refresh("collectors/scan_git_hygiene.py", paths.SCRATCH / "worktree-registration.json",
                                    0, 10, ("--registrations-only", "--budget-seconds", "8"))
        self.assertEqual(result, "refreshed")
        self.assertEqual(self.g.checkouts(), sorted([str(root), str(linked)]))
        self.assertEqual(hygiene.read_bytes(), before)
        self.assertEqual(hygiene.stat().st_mtime_ns, stamp)


class RegistrationMalformed(Base):
    def write_receipt(self, **over):
        import paths
        paths.SCRATCH.mkdir(parents=True, exist_ok=True)
        doc = {"schema_version": 1, "kind": "worktree-registration", "partial": False,
               "repositories": [self.valid_row()]}
        doc.update(over)
        (paths.SCRATCH / "worktree-registration.json").write_text(json.dumps(doc))

    def valid_row(self):
        return {"path": str(self.t1), "registered_at": self.g.now(), "stale": False,
                "worktrees": [{"path": str(self.t1), "bare": False, "locked": False, "prunable": False}]}

    def test_malformed_collections_and_rows_preserve_valid_peers_with_degradation(self):
        for bad in (None, "bad", 1, {}, [None]):
            with self.subTest(bad=bad):
                self.write_receipt(repositories=bad)
                degraded = []
                self.assertEqual(self.g.checkouts(degraded), [])
                self.assertTrue(degraded)
        self.write_receipt(repositories=[None, self.valid_row()])
        degraded = []
        self.assertEqual(self.g.checkouts(degraded), [str(self.t1)])
        self.assertTrue(degraded)

    def test_invalid_worktree_metadata_is_never_eligible(self):
        for bad in (None, "bad", 1, {}, {"path": "relative"}, {"path": 4},
                    {"path": "/bad\0path"}, {"path": str(self.t1)}, {"path": str(self.t1), "bare": 0},
                    {"path": str(self.t1), "locked": "false"}):
            with self.subTest(bad=bad):
                row = self.valid_row()
                row["worktrees"] = [bad, row["worktrees"][0]]
                self.write_receipt(repositories=[row])
                degraded = []
                self.assertEqual(self.g.checkouts(degraded), [str(self.t1)])
                self.assertTrue(degraded)
        for bad in (None, "bad", {}, 1):
            row = {**self.valid_row(), "worktrees": bad}
            self.write_receipt(repositories=[row])
            degraded = []
            self.assertEqual(self.g.checkouts(degraded), [])
            self.assertTrue(degraded)

    def test_invalid_root_timestamp_stale_and_schema_are_refused(self):
        for field, value in (("path", "relative"), ("path", None), ("registered_at", []),
                             ("registered_at", "bad"), ("stale", 0), ("stale", "false")):
            self.write_receipt(repositories=[{**self.valid_row(), field: value}])
            degraded = []
            self.assertEqual(self.g.checkouts(degraded), [])
            self.assertTrue(degraded)
        self.write_receipt(schema_version=True)
        self.assertEqual(self.g.checkouts([]), [])

    def test_malformed_dated_fallback_preserves_valid_peers(self):
        import paths
        paths.SCRATCH.mkdir(parents=True, exist_ok=True)
        (paths.SCRATCH / "git-hygiene.json").write_text(json.dumps({
            "measured_at": self.g.now(), "checkouts": [None, {"path": str(self.t1), "worktrees": [None]}]}))
        degraded = []
        self.assertEqual(self.g.checkouts(degraded), [str(self.t1)])
        self.assertEqual(degraded[0]["invalid"], {"repositories": 1, "worktrees": 1})

    def test_run_reports_malformed_receipt_without_losing_other_phases(self):
        self.write_receipt(repositories=[None, self.valid_row()])
        with patch.object(self.g, "load_config", return_value=self.cfg()), \
                patch.object(self.g, "refresh", return_value="refreshed"), \
                patch.object(self.g.Holders, "take", return_value=self.free()), \
                patch.object(self.g, "checkout_idle_days", return_value=None), \
                patch.object(self.g, "large_files", return_value=None):
            doc = self.g.run([])
        self.assertIn("cache-command", doc["phases"])
        self.assertTrue(any(d.get("source") == "build-roots" for d in doc["degraded"]))
        self.assertEqual(doc["planned"]["build-output"], 0)


class RegistrationFairness(Base):
    def test_current_registration_roots_replace_undated_hygiene(self):
        import paths
        roots = [self.t1 / n for n in ("fresh", "stale", "old")]
        for root in roots:
            root.mkdir()
        paths.SCRATCH.mkdir(parents=True, exist_ok=True)
        (paths.SCRATCH / "git-hygiene.json").write_text(json.dumps({
            "checkouts": [{"path": str(roots[2])}]}))
        (paths.SCRATCH / "worktree-registration.json").write_text(json.dumps({
            "schema_version": 1, "kind": "worktree-registration", "partial": True,
            "repositories": [{"path": str(root), "registered_at": self.g.now(),
                "stale": i == 1, "worktrees": [{"path": str(root), "bare": False, "locked": False, "prunable": False}]} for i, root in enumerate(roots[:2])]}))
        self.assertEqual(self.g.checkouts(), [str(roots[0])])

    def test_undated_hygiene_is_not_current_registration(self):
        import paths
        paths.SCRATCH.mkdir(parents=True, exist_ok=True)
        out = paths.SCRATCH / "git-hygiene.json"
        doc = {"checkouts": [{"path": str(self.t1)}]}
        out.write_text(json.dumps(doc))
        self.assertEqual(self.g.checkouts(), [])
        doc["at"] = self.g.now()
        out.write_text(json.dumps(doc))
        self.assertEqual(self.g.checkouts(), [str(self.t1)])
        doc["at"] = "2000-01-01T00:00:00Z"
        out.write_text(json.dumps(doc))
        self.assertEqual(self.g.checkouts(), [])

    def test_refresh_child_gets_its_own_bounded_deadline(self):
        with patch.dict(os.environ, {"OBSERVATORY_STEP_DEADLINE": "1"}), \
                patch.object(self.g.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as call:
            self.g.refresh("collectors/scan_git_hygiene.py", self.t1 / "out.json", 0, 20)
        deadline = float(call.call_args.kwargs["env"]["OBSERVATORY_STEP_DEADLINE"])
        self.assertGreater(deadline, time.time() + 17)
        self.assertLessEqual(deadline, time.time() + 20)

    def test_refresh_precedes_build_planning_and_holder_failure_survives_command_phase(self):
        events = []
        with patch.object(self.g, "load_config", return_value=self.cfg()), \
                patch.object(self.g, "refresh", side_effect=lambda *a, **k: events.append(("refresh", a)) or "refreshed"), \
                patch.object(self.g.Holders, "take", return_value=self.g.Holders(None)), \
                patch.object(self.g, "large_files", return_value=None), \
                patch.object(self.g, "planners", side_effect=lambda *a, **k: events.append(("plan", None)) or [(n, lambda b: []) for n in self.g.CLASSES]):
            doc = self.g.run([])
        self.assertEqual(events[0][0], "refresh")
        self.assertIn("--registrations-only", events[0][1][4])
        self.assertNotEqual(doc["holders"], "none")
        self.assertFalse(doc["holders_by_phase"]["build-output"]["known"])
        self.assertEqual(doc["holders_by_phase"]["cache-command"]["mode"], "none")

    def test_cursor_is_persisted_before_expensive_scan_and_advances_next_pass(self):
        import paths
        roots = [str(self.t1 / n) for n in ("a", "b", "c")]
        calls = []
        def stop(root, at, timeout):
            state = json.loads((paths.STATE / "disk-guard-build-cursor.json").read_text())
            self.assertEqual(state["last_attempted"], root)
            calls.append(root)
            raise KeyboardInterrupt
        with patch.object(self.g, "checkouts", return_value=roots), \
                patch.object(self.g, "checkout_idle_days", side_effect=stop):
            for _ in range(2):
                with self.assertRaises(KeyboardInterrupt):
                    self.g.plan_builds(self.cfg(), "critical", self.free(), time.time(), ["dist"])
        self.assertEqual(calls, roots[:2])

    def test_build_directory_enumeration_itself_has_an_entry_bound(self):
        for name in ("a", "b", "c"):
            (self.t1 / name).mkdir()
        degraded = []
        with patch.object(self.g, "WALK_LIMIT", 1):
            rows = list(self.g._build_walk(str(self.t1), time.monotonic() + 2, degraded))
        self.assertEqual(rows, [])
        self.assertIn("incomplete", degraded[0]["reason"])

    def test_each_root_gets_only_its_quantum(self):
        roots = [str(self.t1 / n) for n in ("a", "b", "c")]
        clock, calls = [100.0], []
        def slow(root, at, timeout):
            calls.append((root, timeout))
            clock[0] += timeout
            return None
        with patch.object(self.g, "checkouts", return_value=roots), \
                patch.object(self.g.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(self.g, "checkout_idle_days", side_effect=slow):
            self.g.plan_builds(self.cfg(build_root_quantum_seconds=2), "critical", self.free(), time.time(),
                               ["dist"], self.g.Budget(7, []))
        self.assertEqual([row[0] for row in calls], roots)
        self.assertTrue(all(0 < row[1] <= 2 for row in calls))


if __name__ == "__main__":
    unittest.main()
