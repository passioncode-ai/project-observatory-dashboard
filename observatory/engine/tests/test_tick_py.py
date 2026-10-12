#!/usr/bin/env python3
"""A whole tick runs through `tools/tick.py` on every OS (docs/design/WINDOWS-LINUX.md, W3).

The tick was a bash script; it is Python so that Windows runs it too. These cases run the REAL
tick — the supervisor, the lease, every step, the report — in a workspace of their own, and the
Windows CI job runs the same tick there. The wrapper `tools/tick.sh` still hands over to it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class WholeTick(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory(prefix="observatory-tick-")
        cls.addClassCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        cls.home = base / "runtime"
        projects = base / "projects" / "example"
        projects.mkdir(parents=True)
        (projects / "package.json").write_text('{"name":"example"}\n', encoding="utf-8")
        cls.env = {**os.environ, "OBSERVATORY_HOME": str(cls.home), "OBSERVATORY_SYSTEM_SETUP": "0",
                   "HOME": str(base / "user"), "USERPROFILE": str(base / "user"),
                   "OBSERVATORY_TICK_CEILING_SECONDS": "900"}
        for key in ("OBSERVATORY_TICK_SUPERVISOR_PID", "OBSERVATORY_TICK_DEADLINE", "AGENT_SYNC_RUN_ID",
                    "OBSERVATORY_SCRATCH", "OBSERVATORY_STATE", "OBSERVATORY_REGISTRY", "OBSERVATORY_DB"):
            cls.env.pop(key, None)
        (base / "user").mkdir()
        cls.run_engine(sys.executable, "observatory.py", "init")
        settings = cls.home / "config/settings.json"
        doc = json.loads(settings.read_text(encoding="utf-8"))
        doc["sources"]["projects"] = str(projects.parent)
        doc.setdefault("features", {})["scheduler"] = True
        settings.write_text(json.dumps(doc), encoding="utf-8")
        cls.tick = cls.run_engine(sys.executable, "tools/tick.py", timeout=900)

    @classmethod
    def run_engine(cls, *argv, timeout=120) -> subprocess.CompletedProcess:
        p = subprocess.run(argv, cwd=ROOT, env=cls.env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        if argv[1] != "tools/tick.py" and p.returncode:
            raise RuntimeError(f"{argv[1]} failed: {(p.stdout + p.stderr)[-600:]}")
        return p

    def test_the_tick_finishes_and_exits_zero(self):
        out = self.tick.stdout + self.tick.stderr
        self.assertEqual(self.tick.returncode, 0, out[-1500:])
        lines = [line[21:] for line in self.tick.stdout.splitlines()]
        self.assertIn("tick start", lines, out[-1500:])
        self.assertIn("tick done", lines, out[-1500:])

    def test_it_ran_under_the_supervisor(self):
        # Started directly, it re-entered through `tick_lease.py run`, which is what takes the
        # workspace's tick lock and sets the ceiling; unsupervised it would not reach a step.
        self.assertIn("integrity: ", self.tick.stdout, self.tick.stdout[-800:])

    def test_every_line_carries_a_utc_stamp(self):
        for line in self.tick.stdout.splitlines():
            self.assertRegex(line, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ", line)

    def test_the_report_says_no_step_failed(self):
        report = json.loads((self.home / "store/raw/tick.json").read_text(encoding="utf-8"))
        self.assertEqual(report["failed_steps"], [], self.tick.stdout[-1500:])
        self.assertTrue(report["finished_at"].startswith("20"))

    def test_the_tail_built_the_page(self):
        self.assertIn("findings: ", self.tick.stdout)
        self.assertIn("dashboard: ", self.tick.stdout)

    def test_the_collectors_ran_in_turn_and_their_starts_are_kept(self):
        report = json.loads((self.home / "store/raw/tick.json").read_text(encoding="utf-8"))
        order = report["collector_order"]
        self.assertEqual(order[0], "scan-fs", order)          # no record yet: the planned order
        self.assertNotIn("integrity", order)
        self.assertEqual(report["not_started"], [])
        started = json.loads((self.home / "store/tick-rotation.json").read_text(encoding="utf-8"))["started"]
        self.assertEqual(set(started), set(order))


def load_tick():
    import importlib.util
    spec = importlib.util.spec_from_file_location("tick_under_test", ROOT / "tools/tick.py")
    tick = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tick)
    return tick


class RotationOrder(unittest.TestCase):
    """2026-10-12, one machine at load average 26-30: `integrity` and `scan-fs` took the first
    thirteen minutes of every tick, the collectors after them were `not started`, and the leak scan
    and the lifecycle watch had not run for two days. The collectors now run least recently
    started first."""

    def setUp(self):
        self.tick = load_tick()

    def test_a_step_never_started_goes_first_and_ties_keep_the_planned_order(self):
        order = self.tick.rotation_order(["a", "b", "c", "d"], {"a": 30.0, "b": 10.0}, {})
        self.assertEqual(order, ["c", "d", "b", "a"])

    def test_with_every_step_started_in_turn_the_order_is_stable(self):
        planned = ["a", "b", "c"]
        self.assertEqual(self.tick.rotation_order(planned, {"a": 1.0, "b": 2.0, "c": 3.0}, {}), planned)

    def test_a_starved_reader_pulls_its_writer_forward_rather_than_waiting_behind_it(self):
        reads = {"leaks": ("env",), "scrub": ("leaks",)}
        started = {"scan-fs": 1.0, "env": 50.0, "leaks": 0.0, "scrub": 60.0}
        order = self.tick.rotation_order(["scan-fs", "env", "leaks", "scrub"], started, reads)
        self.assertEqual(order[:2], ["env", "leaks"], order)
        self.assertLess(order.index("leaks"), order.index("scrub"), order)

    def test_the_declared_reads_hold_under_every_starting_order(self):
        import itertools
        names = ["scan-fs", "remotes", "heroku", "remote-env", "env", "leaks", "scrub-companion",
                 "machine", "git-hygiene", "cleanup"]
        for perm in itertools.islice(itertools.permutations(range(len(names))), 0, 5000, 7):
            started = {n: float(t) for n, t in zip(names, perm)}
            order = self.tick.rotation_order(names, started)
            for reader, writers in self.tick.READS_FROM.items():
                for writer in writers:
                    if reader in order and writer in order:
                        self.assertLess(order.index(writer), order.index(reader), (reader, writer, order))

    def test_the_prefix_it_reads_is_the_one_the_watchdog_prints(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("lease_under_test", ROOT / "tools/tick_lease.py")
        lease = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lease)
        self.assertEqual(self.tick.NOT_STARTED, lease.NOT_STARTED)
        self.assertEqual(self.tick.TIMED_OUT, lease.TIMED_OUT)


class StepStarted(unittest.TestCase):
    """`Tick.step` tells a step the watchdog did not start from one that ran, through the real
    `tick_lease.py step`: only a step that ran moves to the back of the rotation."""

    def run_step(self, deadline: float) -> tuple[bool, list]:
        tick = load_tick()
        keys = ("OBSERVATORY_TICK_DEADLINE", "OBSERVATORY_HOME", "OBSERVATORY_STATE")
        saved = {k: os.environ.get(k) for k in keys}
        with tempfile.TemporaryDirectory() as tmp:
            # The watchdog leaves its step marker in the workspace's store: a workspace of its own.
            os.environ.update({"OBSERVATORY_TICK_DEADLINE": f"{deadline:.0f}",
                               "OBSERVATORY_HOME": str(Path(tmp).resolve() / "home")})
            os.environ.pop("OBSERVATORY_STATE", None)
            try:
                t = tick.Tick(Path(tmp))
                ran = t.step("rotation-probe", sys.executable, "-c", "print('ran')")
                return ran, t.failed
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_a_step_past_the_ceiling_is_not_started(self):
        import time
        ran, failed = self.run_step(time.time() - 5)
        self.assertFalse(ran)
        self.assertEqual(failed, [("rotation-probe", 124)])

    def test_a_step_inside_the_ceiling_ran(self):
        import time
        ran, failed = self.run_step(time.time() + 3600)
        self.assertTrue(ran)
        self.assertEqual(failed, [])


class RotationRun(unittest.TestCase):
    """`Rotation.run_in_turn` against a stub tick: what ran moves to the back, what was not started keeps
    its old time and goes first next tick."""

    def setUp(self):
        self.tick = load_tick()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.record = Path(tmp.name).resolve() / "tick-rotation.json"

    def stub(self, refuse=()):
        tick = self.tick.Tick(self.record.parent)
        ran = []

        def step(name, *argv):
            ran.append(name)
            return name not in refuse
        tick.step = step
        return tick, ran

    def plan(self, tick):
        rotation = self.tick.Rotation(tick, self.record)
        for name in ("scan-fs", "scan-gh", "leaks", "lifecycle"):
            rotation.step(name, "x.py")
        return rotation

    def test_a_step_not_started_runs_first_on_the_next_tick(self):
        first, ran = self.stub(refuse=("leaks", "lifecycle"))
        self.plan(first).run_in_turn()
        self.assertEqual(ran, ["scan-fs", "scan-gh", "leaks", "lifecycle"])
        self.assertEqual(first.not_started, ["leaks", "lifecycle"])
        second, ran = self.stub()
        self.plan(second).run_in_turn()
        self.assertEqual(ran, ["leaks", "lifecycle", "scan-fs", "scan-gh"])
        self.assertEqual(second.order, ran)
        third, ran = self.stub()
        self.plan(third).run_in_turn()
        self.assertEqual(ran, ["leaks", "lifecycle", "scan-fs", "scan-gh"])

    def test_an_unreadable_record_falls_back_to_the_planned_order_and_is_rewritten(self):
        self.record.write_text("{not json", encoding="utf-8")
        tick, ran = self.stub()
        self.plan(tick).run_in_turn()
        self.assertEqual(ran, ["scan-fs", "scan-gh", "leaks", "lifecycle"])
        self.assertEqual(set(json.loads(self.record.read_text(encoding="utf-8"))["started"]), set(ran))


class TailOrder(unittest.TestCase):
    """2026-10-09/10: `plugins` (92–497 s) and `agent` (135–244 s) ran first among the tail's
    optional steps and used the window every tick, so `index`, `retention` — the erasure —
    `sweep`, `corroborate`, `ledger` and `lost`, seconds each, were `not started` tick after
    tick. The cheap ones run first now; the heavy ones take what is left."""
    CHEAP = ("index", "retention", "sweep", "corroborate", "ledger", "lost")
    HEAVY = ("plugins", "rollup", "agent")

    def order(self):
        sys.path.insert(0, str(ROOT / "tests"))
        import tick_reader
        return [name for _line, _fn, name, _words in tick_reader.tick_calls(ROOT)]

    def test_the_cheap_optional_steps_run_before_the_heavy_ones(self):
        order = self.order()
        for cheap in self.CHEAP:
            for heavy in self.HEAVY:
                self.assertLess(order.index(cheap), order.index(heavy), f"{cheap} after {heavy}: {order}")

    def test_the_heavy_ones_still_run_before_the_findings(self):
        order = self.order()
        for heavy in self.HEAVY:
            self.assertLess(order.index(heavy), order.index("findings"), order)

    def test_the_change_of_the_tick_is_known_before_the_agent_asks(self):
        # `diff` decides whether the agent is called at all.
        order = self.order()
        self.assertLess(order.index("diff"), order.index("agent"), order)


class Bail(unittest.TestCase):
    def test_a_bail_still_releases_the_lease(self):
        # `steps` stops through `bail` (SystemExit); the release sits in `run`'s `finally`.
        import importlib.util
        spec = importlib.util.spec_from_file_location("tick_under_test", ROOT / "tools/tick.py")
        tick = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tick)
        calls = []
        tick.lease = lambda verb: (calls.append(verb), (0, ""))[1]

        def stop(t, scratch, dashboard):
            t.bail("emit", 1, "emit REFUSED a wholesale change")
        tick.steps = stop
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit) as stopped:
            tick.run(tick.Tick(Path(tmp)), Path(tmp), Path(tmp) / "page.html")
        self.assertEqual(stopped.exception.code, 0)
        self.assertEqual(calls, ["acquire", "release"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
