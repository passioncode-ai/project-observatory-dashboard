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
from unittest import mock
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
        record = json.loads((self.home / "store/tick-rotation.json").read_text(encoding="utf-8"))
        self.assertEqual(record["schema"], 2)
        self.assertEqual(set(record["collectors"]), set(order))
        for name, row in record["collectors"].items():
            self.assertIn(row["outcome"], ("ran", "failed", "stopped"), (name, row))
            self.assertLessEqual(row["started_at"], row["finished_at"], (name, row))


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
                outcome = t.step("rotation-probe", sys.executable, "-c", "print('ran')")
                t.write_report()
                self.report = json.loads((Path(tmp) / "tick.json").read_text(encoding="utf-8"))
                return outcome, t.failed
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_a_step_past_the_ceiling_is_not_started(self):
        import time
        outcome, failed = self.run_step(time.time() - 5)
        self.assertEqual(outcome, "not_started")
        self.assertEqual(failed, [("rotation-probe", 124)])

    def test_the_report_row_of_a_step_not_started_says_so(self):
        """ENG-007: a not-started step was one more "exited 124" in `failed_steps`, read exactly like
        one the watchdog stopped. Its row now carries `outcome`, and keeps `exit` for old readers."""
        import time
        self.run_step(time.time() - 5)
        self.assertEqual(self.report["failed_steps"],
                         [{"step": "rotation-probe", "exit": 124, "outcome": "not_started"}])

    def test_a_step_inside_the_ceiling_ran(self):
        import time
        outcome, failed = self.run_step(time.time() + 3600)
        self.assertEqual(outcome, "ran")
        self.assertEqual(failed, [])
        self.assertEqual(self.report["failed_steps"], [])


class Outcome(unittest.TestCase):
    """How a step ended is a class, not an exit code (ENG-007): the watchdog's 124 meant both
    "not started for want of time" and "stopped at its limit"."""

    def test_each_exit_is_classed(self):
        tick = load_tick()
        cases = [(0, "", "ran"), (124, "not started — the tick's ceiling has been reached\n", "not_started"),
                 (124, "half of a scan\n", "stopped"), (124, None, "stopped"), (137, "", "stopped"),
                 (143, "x", "stopped"), (-9, "", "stopped"), (1, "", "failed"), (2, "not started", "failed")]
        for rc, first, want in cases:
            self.assertEqual(tick.outcome_of(rc, first), want, (rc, first))

    def test_a_bail_row_carries_its_outcome(self):
        tick = load_tick()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                tick.Tick(Path(tmp)).bail("emit", 1, "emit REFUSED a wholesale change")
            doc = json.loads((Path(tmp) / "tick.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["failed_steps"], [{"step": "emit", "exit": 1, "outcome": "failed"}])


class BoundedRotation(unittest.TestCase):
    """ENG-007: `rotation_order` moved writers before readers in an unbounded `while moved` loop, so
    a cycle in READS_FROM hung the tick (exit 142 in the audit). It now gives up, says which names
    form the cycle, and runs the planned order."""

    def setUp(self):
        self.tick = load_tick()
        self.said: list[str] = []
        self.tick.log = self.said.append

    def order_within(self, seconds, *args):
        import threading
        box = {}
        worker = threading.Thread(target=lambda: box.setdefault("order", self.tick.rotation_order(*args)),
                                  daemon=True)
        worker.start()
        worker.join(seconds)
        self.assertFalse(worker.is_alive(), f"rotation_order did not return within {seconds} s")
        return box["order"]

    def test_a_two_cycle_returns_within_a_second_with_the_planned_order(self):
        planned = ["scan-fs", "a", "b", "leaks"]
        order = self.order_within(1.0, planned, {"a": 5.0, "b": 1.0, "leaks": 0.0, "scan-fs": 9.0},
                                  {"a": ("b",), "b": ("a",)})
        self.assertEqual(order, planned)
        cycle = [line for line in self.said if "cycle" in line]
        self.assertTrue(cycle and "a" in cycle[0] and "b" in cycle[0] and "planned order" in cycle[0], self.said)

    def test_a_longer_cycle_is_named_too(self):
        planned = ["x", "y", "z"]
        order = self.order_within(1.0, planned, {}, {"x": ("z",), "y": ("x",), "z": ("y",)})
        self.assertEqual(order, planned)
        self.assertTrue(any("x" in l and "y" in l and "z" in l and "cycle" in l for l in self.said), self.said)

    def test_a_cycle_outside_the_planned_steps_is_not_a_cycle_here(self):
        order = self.order_within(1.0, ["scan-fs", "env", "leaks"], {"env": 5.0, "leaks": 1.0},
                                  {"leaks": ("env",), "a": ("b",), "b": ("a",)})
        self.assertEqual(order, ["scan-fs", "env", "leaks"])
        self.assertFalse(any("cycle" in l for l in self.said), self.said)


class RotationRun(unittest.TestCase):
    """`Rotation.run_in_turn` against a stub tick: what ran moves to the back, what was not started keeps
    its old time and goes first next tick."""

    def setUp(self):
        self.tick = load_tick()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.record = Path(tmp.name).resolve() / "tick-rotation.json"

    def stub(self, refuse=(), outcomes=None, sleep=0.0):
        outcomes = outcomes or {}
        tick = self.tick.Tick(self.record.parent)
        ran = []

        def step(name, *argv):
            ran.append(name)
            if sleep:
                import time
                time.sleep(sleep)
            return outcomes.get(name, "not_started" if name in refuse else "ran")
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
        self.assertEqual(set(json.loads(self.record.read_text(encoding="utf-8"))["collectors"]), set(ran))


class RotationRecord(unittest.TestCase):
    """The rotation record, schema 2 (ENG-005, ENG-007): per collector the start stamped BEFORE the
    step (the time after it was recorded as the start), when it finished, how it ended, how many
    ticks in a row it was not started and when it last ran to the end. Schema 1 is still read."""

    def setUp(self):
        self.tick = load_tick()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.record = Path(tmp.name).resolve() / "tick-rotation.json"
        self.said: list[str] = []
        self.tick.log = self.said.append

    def run_tick(self, outcomes=None, sleep=0.0):
        tick = self.tick.Tick(self.record.parent)
        ran = []

        def step(name, *argv):
            ran.append(name)
            if sleep:
                import time
                time.sleep(sleep)
            return (outcomes or {}).get(name, "ran")
        tick.step = step
        rotation = self.tick.Rotation(tick, self.record)
        for name in ("scan-fs", "leaks", "lifecycle"):
            rotation.step(name, "x.py")
        rotation.run_in_turn()
        return ran, json.loads(self.record.read_text(encoding="utf-8"))

    def test_the_start_is_stamped_before_the_step(self):
        _, doc = self.run_tick(sleep=0.05)
        self.assertEqual(doc["schema"], 2)
        for name, row in doc["collectors"].items():
            self.assertLess(row["started_at"], row["finished_at"], (name, row))
            self.assertGreaterEqual(row["finished_at"] - row["started_at"], 0.04, (name, row))
            self.assertEqual(row["outcome"], "ran")
            self.assertEqual(row["last_ran_at"], row["started_at"])
            self.assertEqual(row["not_started_streak"], 0)

    def test_every_outcome_is_kept_and_a_streak_counts_ticks_not_started(self):
        self.run_tick()
        _, doc = self.run_tick(outcomes={"leaks": "not_started", "lifecycle": "failed", "scan-fs": "stopped"})
        rows = doc["collectors"]
        self.assertEqual({n: r["outcome"] for n, r in rows.items()},
                         {"leaks": "not_started", "lifecycle": "failed", "scan-fs": "stopped"})
        ran_at = rows["leaks"]["last_ran_at"]
        _, doc = self.run_tick(outcomes={"leaks": "not_started"})
        _, doc = self.run_tick(outcomes={"leaks": "not_started"})
        leaks = doc["collectors"]["leaks"]
        self.assertEqual(leaks["not_started_streak"], 3)
        self.assertEqual(leaks["last_ran_at"], ran_at, "a tick that did not start it does not move its last run")
        self.assertTrue(any("leaks not started for 2 ticks" in l for l in self.said), self.said)
        self.assertIsNotNone(doc["collectors"]["lifecycle"]["last_ran_at"])
        _, doc = self.run_tick()
        self.assertEqual(doc["collectors"]["leaks"]["not_started_streak"], 0)
        self.assertGreater(doc["collectors"]["leaks"]["last_ran_at"], ran_at)

    def test_a_failed_or_stopped_step_moves_back_but_has_not_run(self):
        _, doc = self.run_tick(outcomes={"scan-fs": "failed", "leaks": "stopped"})
        rows = doc["collectors"]
        self.assertIsNone(rows["scan-fs"]["last_ran_at"])
        self.assertIsNone(rows["leaks"]["last_ran_at"])
        self.assertIsNotNone(rows["scan-fs"]["started_at"])

    def test_a_collector_never_started_keeps_a_null_start(self):
        """Verification round 1: a collector the watchdog never started was given `started_at: 0.0`,
        which reads as a start in 1970. It keeps null and its streak; only the ordering treats null as
        oldest, and the log says "never started"."""
        self.run_tick(outcomes={"leaks": "not_started"})
        _, doc = self.run_tick(outcomes={"leaks": "not_started"})
        leaks = doc["collectors"]["leaks"]
        self.assertIsNone(leaks["started_at"])
        self.assertEqual(leaks["not_started_streak"], 2)
        self.assertIsNone(leaks["last_ran_at"])
        self.assertTrue(any("leaks (never started)" in l for l in self.said), self.said)

    def test_the_start_is_kept_before_the_step_runs(self):
        """A tick killed inside a step must still move that step back: a provisional row (its start,
        no outcome yet) is saved before the step is called."""
        seen = {}
        tick = self.tick.Tick(self.record.parent)

        def step(name, *argv):
            seen[name] = json.loads(self.record.read_text(encoding="utf-8"))["collectors"][name]
            return "ran"
        tick.step = step
        rotation = self.tick.Rotation(tick, self.record)
        rotation.step("leaks", "x.py")
        rotation.run_in_turn()
        self.assertIsNotNone(seen["leaks"]["started_at"])
        self.assertIsNone(seen["leaks"]["outcome"])

    def test_a_bad_row_is_skipped_not_the_whole_record(self):
        self.record.write_text(json.dumps({"schema": 2, "collectors": {
            "scan-fs": "not a row", "leaks": {"started_at": "yesterday", "not_started_streak": "x"},
            "lifecycle": {"started_at": 50.0, "outcome": "ran", "not_started_streak": 0, "last_ran_at": 50.0}}}),
            encoding="utf-8")
        ran, doc = self.run_tick(outcomes={"leaks": "not_started", "lifecycle": "not_started"})
        self.assertEqual(ran[0], "scan-fs", ran)
        self.assertEqual(doc["collectors"]["leaks"]["not_started_streak"], 1, "a bad streak is coerced to 0")
        self.assertIsNone(doc["collectors"]["leaks"]["started_at"])
        # The good row was READ: not started this tick, it keeps exactly what the record held.
        self.assertEqual(doc["collectors"]["lifecycle"]["started_at"], 50.0)
        self.assertEqual(doc["collectors"]["lifecycle"]["last_ran_at"], 50.0)
        self.assertTrue(any("scan-fs" in l and "skipped" in l for l in self.said), self.said)

    def test_non_finite_numbers_are_not_times(self):
        self.record.write_text('{"schema": 2, "collectors": {"leaks": {"started_at": Infinity, '
                               '"not_started_streak": Infinity, "last_ran_at": NaN}}}', encoding="utf-8")
        ran, doc = self.run_tick(outcomes={"leaks": "not_started"})
        self.assertEqual(ran, ["scan-fs", "leaks", "lifecycle"], "the tick completes")
        leaks = doc["collectors"]["leaks"]
        self.assertIsNone(leaks["started_at"])
        self.assertIsNone(leaks["last_ran_at"])
        self.assertEqual(leaks["not_started_streak"], 1)

    def test_an_unreadable_shape_that_is_not_a_newer_schema_is_rewritten(self):
        """Confirming read of round 2: every unknown shape was pinned as a newer engine's record, so
        `{}` or a string schema ran the planned order on every tick for ever. Only an int schema past
        this engine's is kept; anything else is logged and the first save writes schema 2."""
        for body in ({}, {"schema": "2", "collectors": {}}):
            self.record.write_text(json.dumps(body), encoding="utf-8")
            _, doc = self.run_tick()
            self.assertEqual(doc["schema"], 2, body)
            self.assertEqual(set(doc["collectors"]), {"scan-fs", "leaks", "lifecycle"}, body)

    def test_a_schema_it_does_not_know_is_left_untouched(self):
        """A newer engine's record survives a tick of this one byte for byte: a downgrade must not
        erase it. The tick runs the planned order and says why."""
        body = json.dumps({"schema": 3, "collectors": {"leaks": {"whatever": 1}}}) + "\n"
        self.record.write_text(body, encoding="utf-8")
        tick = self.tick.Tick(self.record.parent)
        ran = []
        tick.step = lambda name, *argv: ran.append(name) or "ran"
        rotation = self.tick.Rotation(tick, self.record)
        for name in ("scan-fs", "leaks", "lifecycle"):
            rotation.step(name, "x.py")
        rotation.run_in_turn()
        self.assertEqual(ran, ["scan-fs", "leaks", "lifecycle"])
        self.assertEqual(self.record.read_text(encoding="utf-8"), body)
        self.assertTrue(any("unreadable" in l and "schema 3" in l for l in self.said), self.said)
        self.assertTrue(any("not rewritten" in l for l in self.said), self.said)

    def test_a_schema_1_record_is_read_and_rewritten_as_schema_2(self):
        self.record.write_text(json.dumps({"started": {"scan-fs": 300.0, "leaks": 100.0, "lifecycle": 200.0}}),
                               encoding="utf-8")
        ran, doc = self.run_tick(outcomes={"lifecycle": "not_started"})
        self.assertEqual(ran, ["leaks", "lifecycle", "scan-fs"], "the old start times still decide the order")
        self.assertEqual(doc["schema"], 2)
        self.assertNotIn("started", doc)
        self.assertEqual(doc["collectors"]["lifecycle"]["started_at"], 200.0,
                         "a step not started keeps its old start, so it goes first next tick")
        self.assertEqual(doc["collectors"]["lifecycle"]["not_started_streak"], 1)
        self.assertEqual(doc["collectors"]["lifecycle"]["last_ran_at"], 200.0,
                         "the schema-1 time seeds the last run, so an upgrade does not read as starvation")


class TailBail(unittest.TestCase):
    """Verification round 2: merge, emit and validate bailed with a literal exit 1, so a merge the
    watchdog stopped or never started read as `failed`/1. Their own exit and first line now decide."""

    def bail_row(self, name, rc, out):
        tick = load_tick()
        import paths
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            t = tick.Tick(base)
            t.step = lambda *a, **k: "ran"
            t.bounded = lambda step, *argv: (rc, out) if step == name else (0 if step != "backup-due" else 1, "")
            with mock.patch.object(paths, "STATE", base), self.assertRaises(SystemExit):
                tick.steps(t, base, base / "page.html")
            return json.loads((base / "tick.json").read_text(encoding="utf-8"))["failed_steps"]

    def test_a_merge_past_its_limit_is_stopped(self):
        self.assertEqual(self.bail_row("merge", 124, "half a merge\n"),
                         [{"step": "merge", "exit": 124, "outcome": "stopped"}])

    def test_a_merge_not_started_says_so(self):
        self.assertEqual(self.bail_row("merge", 124, "not started — the tick's ceiling has been reached\n"),
                         [{"step": "merge", "exit": 124, "outcome": "not_started"}])

    def test_emit_and_validate_keep_their_exit(self):
        self.assertEqual(self.bail_row("emit", 3, "REFUSED\n"), [{"step": "emit", "exit": 3, "outcome": "failed"}])
        self.assertEqual(self.bail_row("validate", 137, ""),
                         [{"step": "validate", "exit": 137, "outcome": "stopped"}])


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
