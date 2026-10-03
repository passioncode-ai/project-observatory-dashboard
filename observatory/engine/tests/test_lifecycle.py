#!/usr/bin/env python3
"""The engine's side of the product lifecycle contract (fabric-workspace knowledge/lifecycle.md).

Each class proves one rule's "Check" column against this engine, with synthetic
fixtures only: a temporary HOME and workspace, a planted `claude` that is a shell
script, planted children that ignore SIGTERM, fake process tables. Nothing here
runs the real `claude`, reads the real launchd domain or measures this machine.

    F3  LC-04/LC-08  the tick never runs `claude mcp list`; an on-demand probe is reaped as a group
    F2  LC-06        background disk sizing never touches other apps' containers or ~/Downloads
    F5  LC-02/LC-03  every tick step has a wall-clock watchdog; the whole tick has a ceiling
    F6  LC-02        the tick plist's ExitTimeOut covers the supervisor's own grace
    F7  LC-08        the server's heartbeat writes only on change and backs off with no client
    F8  LC-12        one rotation policy for every log, files at 0600
    F4  LC-10        an MCP server whose code was replaced answers `stale-server`
    F11 LC-04        no engine subprocess carries a secret on argv
    F13 LC-05        plists carry a minimal PATH and never a Homebrew Cellar path
"""
from __future__ import annotations

import ast
import importlib
import json
import os
import pathlib
import plistlib
import re
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for sub in ("collectors", "tools", "dashboard"):
    sys.path.append(str(ROOT / sub))


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); it is dead for every purpose here.
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


def wait_gone(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


def write_settings(home: Path, **features) -> None:
    f = home / "config/settings.json"
    doc = json.loads(f.read_text())
    doc.setdefault("features", {}).update(features)
    doc.setdefault("integrations", {})["mcp"] = True
    f.write_text(json.dumps(doc))


class Workspace(unittest.TestCase):
    """A private HOME and workspace; every OBSERVATORY_* override points into it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "workspace"
        self.user = self.base / "user"
        self.user.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OBSERVATORY_", "FABRIC_"))}
        env.update(OBSERVATORY_HOME=str(self.home), HOME=str(self.user),
                   FABRIC_SERVICES_DIR=str(self.base / "services"))
        self.env = patch.dict(os.environ, env, clear=True)
        self.env.start()
        import workspace
        workspace.initialize(self.home)
        import paths
        importlib.reload(paths)
        self.paths = paths

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def reload(self, name: str):
        mod = importlib.import_module(name)
        return importlib.reload(mod)


# --- F3 ------------------------------------------------------------------------

FAKE_CLAUDE = """#!/bin/bash
# A planted `claude`: records that it ran, starts a child that ignores SIGTERM
# (the shape of an MCP server the real CLI leaves behind), prints one line.
echo ran >> "{marker}"
( trap '' TERM; exec sleep 60 ) &
echo $! > "{child}"
echo "alpha: npx alpha-mcp - ✔ Connected"
{tail}
"""


class McpProbeStaysOutOfTheTick(Workspace):
    def setUp(self):
        super().setUp()
        write_settings(self.home, scheduler=True)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.marker = self.base / "claude-ran"
        self.child = self.base / "claude-child.pid"
        cfg = self.user / ".claude.json"
        cfg.write_text(json.dumps({"mcpServers": {"alpha": {"command": "npx", "args": ["alpha-mcp"]}}}))
        settings = self.home / "config/settings.json"
        doc = json.loads(settings.read_text())
        doc.setdefault("sources", {})["mcp_config_root"] = str(self.user)
        settings.write_text(json.dumps(doc))
        importlib.reload(self.paths)

    def plant(self, tail: str = "") -> None:
        script = self.bin / "claude"
        script.write_text(FAKE_CLAUDE.format(marker=self.marker, child=self.child, tail=tail))
        script.chmod(0o755)
        os.environ["PATH"] = f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"

    def test_the_tick_reads_declarations_and_never_starts_claude(self):
        self.plant()
        sm = self.reload("scan_mcp")
        out = self.paths.SCRATCH / "mcp.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        self.assertEqual(sm.main(["scan_mcp.py", str(out), "--declarations-only"]), 0)
        self.assertFalse(self.marker.exists(), "the tick's scan ran `claude`")
        doc = json.loads(out.read_text())
        self.assertEqual([r["name"] for r in doc["servers"]], ["alpha"])
        self.assertEqual(doc["servers"][0]["liveness"], "not-probed")
        self.assertEqual(doc["probe"]["state"], "never")

    def test_the_tick_carries_the_last_on_demand_verdict_forward(self):
        self.plant()
        sm = self.reload("scan_mcp")
        out = self.paths.SCRATCH / "mcp.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        self.assertEqual(sm.main(["scan_mcp.py", str(out)]), 0)       # on demand: probes
        self.assertTrue(self.marker.exists())
        probed = json.loads(out.read_text())
        self.assertEqual(probed["servers"][0]["liveness"], "connected")
        self.marker.unlink()
        self.assertEqual(sm.main(["scan_mcp.py", str(out), "--declarations-only"]), 0)
        self.assertFalse(self.marker.exists())
        doc = json.loads(out.read_text())
        self.assertEqual(doc["servers"][0]["liveness"], "connected")
        self.assertEqual(doc["servers"][0]["liveness_at"], probed["probe"]["probed_at"])
        self.assertEqual(doc["probe"]["state"], "carried")

    def test_tick_sh_asks_for_declarations_only(self):
        tick = (ROOT / "tools/tick.sh").read_text()
        line = next(l for l in tick.splitlines() if "collectors/scan_mcp.py" in l)
        self.assertIn("--declarations-only", line)

    def test_an_on_demand_probe_leaves_no_child_behind(self):
        self.plant()
        sm = self.reload("scan_mcp")
        started = time.monotonic()
        heard, why, complete = sm.claude_probe(timeout=20)
        self.assertLess(time.monotonic() - started, 10,
                        "the probe waited on a server that inherited its output instead of the CLI's exit")
        self.assertIn("alpha", heard)
        pid = int(self.child.read_text())
        self.assertTrue(wait_gone(pid), f"the probe's child {pid} survived (it ignores SIGTERM)")

    def test_a_probe_cut_by_its_timeout_kills_the_whole_group(self):
        self.plant(tail="sleep 30")
        sm = self.reload("scan_mcp")
        started = time.monotonic()
        # 3 s, not 1: under a loaded machine bash needs time to plant the child
        # before the cut, and a probe cut before that proves nothing.
        heard, why, complete = sm.claude_probe(timeout=3)
        self.assertLess(time.monotonic() - started, 12)
        self.assertFalse(complete)
        self.assertIn("did not finish", why)
        pid = int(self.child.read_text())
        self.assertTrue(wait_gone(pid), f"the probe's child {pid} survived the timeout")


# --- F2 ------------------------------------------------------------------------

class DiskSizingStaysOutOfProtectedPlaces(Workspace):
    PLACES = ["~/.cache", "~/Downloads", "~/Documents/x", "~/Library/Group Containers/ABC.example.vm",
              "~/Library/Containers/com.example.other", "~/Library/Mobile Documents"]

    def setUp(self):
        super().setUp()
        for p in self.PLACES:
            (self.user / p[2:]).mkdir(parents=True, exist_ok=True)
        cfg = {"every_hours": 12, "disk_budget_seconds": 90, "disk_budget_seconds_manual": 900,
               "locations": [{"path": p, "kind": "x", "label": p, "reclaim": "manual"} for p in self.PLACES]}
        (self.home / "config/machine.json").write_text(json.dumps(cfg))
        self.sm = self.reload("scan_machine")

    def sized(self, force: bool) -> list[str]:
        calls = []
        def fake(path, timeout):
            calls.append(str(path))
            return 1024
        with patch.object(self.sm, "size_kb", fake), patch.object(self.sm, "swap_on_disk", lambda: None):
            self.doc = self.sm.survey_disk(None, force)
        return sorted(calls)

    def test_the_background_survey_sizes_only_unprotected_places(self):
        self.assertEqual(self.sized(False), [str(self.user / ".cache")])
        manual = {r["path"] for r in self.doc["manual_only"]}
        self.assertEqual(manual, set(self.PLACES) - {"~/.cache"})

    def test_an_explicit_disk_run_sizes_everything(self):
        self.assertEqual(len(self.sized(True)), len(self.PLACES))

    def test_protection_is_by_path_not_by_config(self):
        for p in ("~/Downloads", "~/Library/Containers/x", "~/Library/Group Containers/y", "~/Desktop",
                  "~/Library/Mobile Documents/z", "~/Pictures"):
            self.assertTrue(self.sm.protected_place(Path(os.path.expanduser(p))), p)
        for p in ("~/.cache", "~/Library/Caches", "~/Library/Developer/Xcode/DerivedData", "/Library/Developer"):
            self.assertFalse(self.sm.protected_place(Path(os.path.expanduser(p))), p)

    def test_the_shipped_defaults_mark_protected_places_manual(self):
        doc = json.loads((ROOT / "defaults/machine.json").read_text())
        for loc in doc["locations"]:
            path = Path(os.path.expanduser(loc["path"]))
            self.assertEqual(bool(loc.get("manual_only")), self.sm.protected_place(path), loc["path"])


# --- F5 / F6 -------------------------------------------------------------------

HANGING = """
trap '' TERM
( trap '' TERM; exec sleep 60 ) &
echo $! > "{child}"
exec sleep 60
"""


class EveryStepHasAWatchdog(Workspace):
    def setUp(self):
        super().setUp()
        write_settings(self.home, scheduler=True)
        self.tl = self.reload("tick_lease")
        self.child = self.base / "step-child.pid"

    def test_a_hanging_step_ends_inside_its_watchdog_and_leaves_no_child(self):
        started = time.monotonic()
        # 2 s: long enough for bash to plant its child on a loaded machine.
        rc = self.tl.bounded("hang", ["/bin/bash", "-c", HANGING.format(child=self.child)], limit=2, grace=1)
        took = time.monotonic() - started
        self.assertEqual(rc, self.tl.TIMED_OUT)
        self.assertLess(took, 10, f"the watchdog took {took:.1f} s")
        self.assertTrue(wait_gone(int(self.child.read_text())), "a child of the hung step survived")

    def test_a_step_after_the_tick_ceiling_does_not_start(self):
        marker = self.base / "started"
        os.environ["OBSERVATORY_TICK_DEADLINE"] = str(time.time() - 1)
        rc = self.tl.bounded("late", ["/bin/bash", "-c", f"touch '{marker}'"], limit=60)
        self.assertEqual(rc, self.tl.TIMED_OUT)
        self.assertFalse(marker.exists())

    def test_a_step_gets_no_more_than_the_time_left(self):
        os.environ["OBSERVATORY_TICK_DEADLINE"] = str(time.time() + 1)
        started = time.monotonic()
        rc = self.tl.bounded("long", ["/bin/bash", "-c", "exec sleep 30"], limit=600, grace=1)
        self.assertEqual(rc, self.tl.TIMED_OUT)
        self.assertLess(time.monotonic() - started, 8)

    def test_the_whole_tick_has_a_ceiling_and_leaves_a_status_record(self):
        os.environ["OBSERVATORY_TICK_CEILING_SECONDS"] = "2"
        started = time.monotonic()
        with patch.object(self.tl, "SUPERVISOR_GRACE", 1):
            rc = self.tl.supervised(["/bin/bash", "-c", HANGING.format(child=self.child)])
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(rc, self.tl.TIMED_OUT)
        self.assertTrue(wait_gone(int(self.child.read_text())))
        run = json.loads((self.paths.SCRATCH / "tick-run.json").read_text())
        self.assertEqual(run["outcome"], "timeout")
        self.assertTrue(run["started_at"] and run["ended_at"])
        self.assertIn("ceiling", run["reason"])

    def test_a_finished_tick_records_its_outcome(self):
        rc = self.tl.supervised(["/bin/bash", "-c", "exit 0"])
        self.assertEqual(rc, 0)
        run = json.loads((self.paths.SCRATCH / "tick-run.json").read_text())
        self.assertEqual(run["outcome"], "finished")

    def test_the_ceiling_stays_below_the_interval(self):
        il = self.reload("install_launchd")
        for interval in (300, 1800, 3600):
            env = il.build(interval)["EnvironmentVariables"]
            self.assertLess(int(env["OBSERVATORY_TICK_CEILING_SECONDS"]), interval)

    def test_launchd_waits_longer_than_the_supervisor_does(self):
        il = self.reload("install_launchd")
        plist = il.build(1800)
        self.assertGreaterEqual(plist["ExitTimeOut"], 20)
        self.assertGreater(plist["ExitTimeOut"], self.tl.SUPERVISOR_GRACE + self.tl.STEP_GRACE)

    def test_tick_sh_runs_every_command_through_the_watchdog(self):
        body = (ROOT / "tools/tick.sh").read_text()
        bare = [l for l in body.splitlines()
                if re.match(r'^\s*"\$PY" (collectors|tools|store|agent)/', l)
                and "tick_lease.py" not in l]
        self.assertEqual(bare, [], "commands the watchdog does not bound")


# --- F7 ------------------------------------------------------------------------

class HeartbeatIsIdle(Workspace):
    def setUp(self):
        super().setUp()
        self.sd = self.reload("serverd")
        self.writes = []
        real = self.sd.atomic.write_json
        def counting(path, doc, *a, **k):
            if Path(path) == self.sd.RECEIPT:
                self.writes.append(doc)
            return real(path, doc, *a, **k)
        self.patch = patch.object(self.sd.atomic, "write_json", counting)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def test_an_unchanged_estate_is_written_once(self):
        for _ in range(5):
            self.sd.heartbeat()
        self.assertEqual(len(self.writes), 1)

    def test_a_change_is_written(self):
        self.sd.heartbeat()
        reg = self.paths.REGISTRY / "repositories.json"
        reg.write_text(json.dumps({"repositories": [
            {"name_with_owner": "example-org/alpha-web", "local": {"sync": "ahead", "path": "/x"}}]}))
        os.utime(reg, (time.time() + 5, time.time() + 5))
        self.sd.heartbeat()
        self.assertEqual(len(self.writes), 2)
        self.assertEqual(self.writes[-1]["remote"]["at_risk_total"], 1)

    def test_an_unchanged_receipt_is_rewritten_only_at_the_keepalive(self):
        clock = [1000.0]
        with patch.object(self.sd.time, "time", lambda: clock[0]):
            self.sd.heartbeat()
            clock[0] += self.sd.KEEPALIVE_SECONDS - 1
            self.sd.heartbeat()
            self.assertEqual(len(self.writes), 1)
            clock[0] += 2
            self.sd.heartbeat()
        self.assertEqual(len(self.writes), 2)
        self.assertGreaterEqual(self.writes[-1]["silent_after_s"], self.sd.KEEPALIVE_SECONDS)

    def test_inputs_are_reread_only_when_they_change(self):
        reads = []
        real = self.sd._read_json
        with patch.object(self.sd, "_read_json", lambda p: (reads.append(Path(p).name), real(p))[1]):
            self.sd.refresh_remote()
            self.sd.refresh_remote()
        self.assertLessEqual(reads.count("repositories.json"), 1)

    def test_the_beat_backs_off_when_no_client_asks(self):
        self.assertEqual(self.sd.beat_interval(now=100.0, last_request=None), self.sd.IDLE_SECONDS)
        self.assertEqual(self.sd.beat_interval(now=100.0, last_request=95.0), self.sd.REFRESH_SECONDS)
        self.assertEqual(self.sd.beat_interval(now=100.0 + self.sd.CLIENT_WINDOW_SECONDS + 1, last_request=100.0),
                         self.sd.IDLE_SECONDS)
        self.assertGreaterEqual(self.sd.IDLE_SECONDS, 60)

    def test_an_idle_hour_costs_a_bounded_number_of_writes(self):
        clock = [5000.0]
        with patch.object(self.sd.time, "time", lambda: clock[0]):
            t = 0.0
            while t < 3600:
                self.sd.heartbeat()
                step = self.sd.beat_interval(now=t, last_request=None)
                clock[0] += step
                t += step
        self.assertLessEqual(len(self.writes), 3600 // self.sd.KEEPALIVE_SECONDS + 1)


# --- F8 ------------------------------------------------------------------------

class OneRotationPolicy(Workspace):
    def setUp(self):
        super().setUp()
        self.lp = self.reload("log_policy")
        self.logs = self.paths.STATE / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)

    def test_a_log_past_its_cap_rotates_and_keeps_n_generations(self):
        f = self.logs / "secret-use.jsonl"
        for i in range(8):
            f.write_text("x" * 200)
            self.lp.rotate(f, max_bytes=100, generations=3)
        names = sorted(p.name for p in self.logs.iterdir())
        self.assertEqual(names, ["secret-use.jsonl.1", "secret-use.jsonl.2", "secret-use.jsonl.3"])

    def test_a_file_held_open_by_launchd_is_copied_and_truncated(self):
        f = self.logs / "serverd.err"
        fd = os.open(f, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, b"a" * 300)
            self.lp.sweep(self.logs, max_bytes=100, generations=2)
            os.write(fd, b"after")
        finally:
            os.close(fd)
        self.assertEqual(f.read_bytes(), b"after", "the writer's descriptor no longer reaches the live file")
        self.assertEqual((self.logs / "serverd.err.1").read_bytes(), b"a" * 300)

    def test_every_log_is_owner_only_after_a_sweep(self):
        for name in ("gate.log", "cleanup.jsonl", "tick.err"):
            p = self.logs / name
            p.write_text("x")
            p.chmod(0o644)
        self.lp.sweep(self.logs)
        for p in self.logs.iterdir():
            self.assertEqual(p.stat().st_mode & 0o777, 0o600, p.name)

    def test_the_policy_is_the_lifecycle_default(self):
        self.assertEqual((self.lp.MAX_BYTES, self.lp.GENERATIONS), (5 * 1024 * 1024, 5))

    def test_tick_sh_has_no_rotation_of_its_own(self):
        body = (ROOT / "tools/tick.sh").read_text()
        self.assertNotIn("rotate_log()", body)
        self.assertIn("tools/rotate_logs.py", body)

    def test_the_tick_sweeps_the_scratch_journals_too(self):
        j = self.paths.SCRATCH / "store-faults.jsonl"
        j.parent.mkdir(parents=True, exist_ok=True)
        j.write_text("y" * 300)
        rl = self.reload("rotate_logs")
        with patch.object(rl.log_policy, "MAX_BYTES", 100):
            self.assertEqual(rl.main([]), 0)
        self.assertTrue((self.paths.SCRATCH / "store-faults.jsonl.1").exists())


class MigrationBackupsAreRetained(Workspace):
    """F9 / LC-12: pre-upgrade database copies used to be kept "until explicit cleanup",
    which nothing ever did (40 MB measured, four weeks old). Retention owns them now."""

    def setUp(self):
        super().setUp()
        self.R = self.reload("store.retention")
        self.dir = self.base / "migration-backups"
        self.dir.mkdir()
        now = time.time()
        self.now = now
        for name, age_days in (("a.db", 60), ("b.db", 45), ("c.db", 40), ("d.db", 3)):
            f = self.dir / f"observatory.db.before-upgrade-{name}"
            f.write_bytes(b"x" * 10)
            os.utime(f, (now - age_days * 86400, now - age_days * 86400))

    def test_old_copies_beyond_the_newest_two_go(self):
        gone = self.R.prune_migration_backups(self.dir, keep=2, days=30, now=self.now)
        self.assertEqual(sorted(p.name[-4:] for p in gone), ["a.db", "b.db"])
        self.assertEqual(sorted(p.name[-4:] for p in self.dir.iterdir()), ["c.db", "d.db"])

    def test_a_plan_removes_nothing(self):
        planned = self.R.prune_migration_backups(self.dir, keep=2, days=30, now=self.now, dry_run=True)
        self.assertEqual(len(planned), 2)
        self.assertEqual(len(list(self.dir.iterdir())), 4)

    def test_young_copies_stay_whatever_their_number(self):
        self.assertEqual(self.R.prune_migration_backups(self.dir, keep=0, days=90, now=self.now), [])

    def test_retention_apply_and_the_defaults_carry_it(self):
        src = (ROOT / "store/retention.py").read_text()
        apply_body = src[src.index("def cmd_apply"):src.index("def main")]
        self.assertIn("prune_migration_backups(", apply_body)
        cfg = json.loads((ROOT / "defaults/retention.json").read_text())
        self.assertEqual((cfg["migration_backups_keep"], cfg["migration_backups_days"]), (2, 30))


# --- F4 ------------------------------------------------------------------------

class ReplacedCodeAnswersStale(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.marker = self.dir / "configuration.py"
        self.marker.write_text('VERSION = "1.2.3"\n')
        import code_freshness
        self.cf = importlib.reload(code_freshness)

    def tearDown(self):
        self.tmp.cleanup()

    def test_untouched_code_is_fresh(self):
        f = self.cf.Freshness(self.marker, "1.2.3")
        self.assertIsNone(f.check())

    def test_a_new_version_on_disk_is_stale(self):
        f = self.cf.Freshness(self.marker, "1.2.3")
        replacement = self.dir / "new.py"
        replacement.write_text('VERSION = "1.3.0"\n')
        os.replace(replacement, self.marker)
        stale = f.check()
        self.assertEqual((stale["running"], stale["installed"]), ("1.2.3", "1.3.0"))

    def test_a_reinstall_of_the_same_version_is_stale_too(self):
        f = self.cf.Freshness(self.marker, "1.2.3")
        replacement = self.dir / "new.py"
        replacement.write_text('VERSION = "1.2.3"\n')
        os.replace(replacement, self.marker)
        self.assertIsNotNone(f.check())

    def test_removed_code_is_stale(self):
        f = self.cf.Freshness(self.marker, "1.2.3")
        self.marker.unlink()
        self.assertEqual(f.check()["installed"], None)

    def test_the_mcp_server_refuses_with_stale_server(self):
        sys.path.insert(0, str(ROOT / "mcp"))
        import capability_tools as ct
        with patch.object(ct, "FRESHNESS", self.cf.Freshness(self.marker, "1.2.3")):
            self.assertIsNone(ct.stale_answer())
            replacement = self.dir / "new.py"
            replacement.write_text('VERSION = "1.3.0"\n')
            os.replace(replacement, self.marker)
            answer = ct.stale_answer()
        self.assertTrue(answer.is_error)
        body = json.loads(answer.content[0].text)
        self.assertEqual(body["error"], "stale-server")
        self.assertIn("1.3.0", body["detail"])
        source = (ROOT / "mcp/capability_tools.py").read_text()
        call = source[source.index("async def call_tool"):]
        self.assertLess(call.index("stale_answer()"), call.index("is_job_tool ="),
                        "every call is checked before it is routed")


# --- F11 -----------------------------------------------------------------------

SECRET_NAME = re.compile(r"(token|secret|passphrase|password|passwd|api_?key|credential|bearer)", re.I)
CALLS = {"run", "Popen", "check_output", "check_call", "call"}


def argv_secrets(source: str) -> list[tuple[int, list[str]]]:
    """(line, names) for each subprocess call whose argv names a secret-looking variable."""
    tree = ast.parse(source)
    hits: dict[int, list[str]] = {}
    for scope in [n for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef))]:
        assigned: dict[str, list] = {}
        for n in ast.walk(scope):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        assigned.setdefault(t.id, []).append(n.value)
        for n in ast.walk(scope):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in CALLS
                    and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess" and n.args):
                argv = n.args[0]
                exprs = [argv] + (assigned.get(argv.id, []) if isinstance(argv, ast.Name) else [])
                names = sorted({x.id if isinstance(x, ast.Name) else x.attr for e in exprs for x in ast.walk(e)
                                if isinstance(x, (ast.Name, ast.Attribute))
                                and SECRET_NAME.search(x.id if isinstance(x, ast.Name) else x.attr)})
                if names:
                    # A call inside a function is seen from the module scope too.
                    hits.setdefault(n.lineno, names)
    return sorted(hits.items())


class NoSecretOnArgv(unittest.TestCase):
    def test_the_lint_catches_a_planted_secret(self):
        planted = textwrap.dedent("""
            import subprocess
            def push(api_token):
                argv = ["tool", "--token", api_token]
                subprocess.run(argv)
            def ok(value):
                subprocess.run(["tool", "--stdin"], input=value)
        """)
        self.assertEqual([names for _, names in argv_secrets(planted)], [["api_token"]])

    def test_no_engine_subprocess_carries_a_secret_on_argv(self):
        found = []
        for f in sorted(ROOT.rglob("*.py")):
            if "tests" in f.relative_to(ROOT).parts:
                continue
            for line, names in argv_secrets(f.read_text(encoding="utf-8")):
                found.append(f"{f.relative_to(ROOT)}:{line} {names}")
        self.assertEqual(found, [])


# --- F13 -----------------------------------------------------------------------

class PlistsCarryAStablePath(Workspace):
    def setUp(self):
        super().setUp()
        self.il = self.reload("install_launchd")
        self.fake = self.base / "fs"
        for d in ("opt/homebrew/bin", "plugins/cache/x/1.0/bin", "tools/bin",
                  "opt/homebrew/Cellar/python@3.99/3.99.1/bin", "opt/homebrew/opt/python@3.99/bin"):
            (self.fake / d).mkdir(parents=True)
        for exe in ("plugins/cache/x/1.0/bin/helper", "tools/bin/gh",
                    "opt/homebrew/Cellar/python@3.99/3.99.1/bin/python3.99"):
            p = self.fake / exe
            p.write_text("#!/bin/sh\n")
            p.chmod(0o755)
        os.symlink(self.fake / "opt/homebrew/Cellar/python@3.99/3.99.1/bin/python3.99",
                   self.fake / "opt/homebrew/opt/python@3.99/bin/python3.99")

    def test_the_path_names_tool_directories_and_nothing_else(self):
        current = os.pathsep.join([str(self.fake / "plugins/cache/x/1.0/bin"), str(self.fake / "tools/bin")])
        path = self.il.launch_path(current).split(os.pathsep)
        self.assertIn(str(self.fake / "tools/bin"), path)
        self.assertNotIn(str(self.fake / "plugins/cache/x/1.0/bin"), path)
        self.assertTrue(set(path) - {str(self.fake / "tools/bin")} <= set(self.il.SYSTEM_PATH))

    def test_a_cellar_interpreter_becomes_its_opt_link(self):
        cellar = self.fake / "opt/homebrew/Cellar/python@3.99/3.99.1/bin/python3.99"
        self.assertEqual(self.il.stable_interpreter(str(cellar)),
                         str(self.fake / "opt/homebrew/opt/python@3.99/bin/python3.99"))
        venv = "/some/venv/bin/python3"
        self.assertEqual(self.il.stable_interpreter(venv), venv)

    def test_the_lint_refuses_a_cellar_path(self):
        bad = {"ProgramArguments": ["/opt/homebrew/Cellar/python@3.99/3.99.1/bin/python3.99", "x.py"],
               "EnvironmentVariables": {"PATH": "/usr/bin"}}
        self.assertTrue(self.il.lint_plist(bad))
        self.assertTrue(self.il.lint_plist({"ProgramArguments": ["/bin/bash"],
                                            "EnvironmentVariables": {"OBSERVATORY_PYTHON": "/opt/homebrew/Cellar/p/1/bin/p"}}))
        self.assertEqual(self.il.lint_plist({"ProgramArguments": ["/bin/bash", "t.sh"],
                                             "EnvironmentVariables": {"PATH": "/usr/bin:/bin"}}), [])

    def test_both_shipped_plists_pass_the_lint(self):
        sd = self.reload("serverd")
        self.assertEqual(self.il.lint_plist(self.il.build(1800)), [])
        self.assertEqual(self.il.lint_plist(sd.build_plist()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
