#!/usr/bin/env python3
"""The tick's time is shared, and every collector gets a turn (OBS-52, tick_budget.py).

2026-10-21, one installed tick: 28 of its last 50 runs reached the ceiling. The leak scan took a
median 1125 s of the 1500 s ceiling and was stopped on most runs; eight collectors were `not
started` 28 times. The cause was two-fold: no collector had a share of the ceiling, and a
DISABLED reader (the companion scrub, the Bitbucket listing), which keeps its old start, sorted as
the most starved collector and dragged the heavy step it reads from (the leak scan, the folder
scan) to the head of every tick.

`Simulation` replays that estate on a fake clock through the REAL `Rotation` and the real share
rule, with the watchdog's arithmetic: it fails on the engine before this change (a collector goes
dozens of ticks without starting) and passes after it. `Share` runs the real watchdog
(`tick_lease.bounded`) and reads the deadline it hands a step.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tick_budget  # noqa: E402


def load_tick():
    spec = importlib.util.spec_from_file_location("tick_budget_under_test", ROOT / "tools/tick.py")
    tick = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tick)
    return tick


#: The collectors of the measured estate: seconds each takes when left to finish, or None for a
#: step whose feature is off (it answers `disabled` at once). Measured from the 2026-10-21
#: receipts; the small ones are rounded up.
ESTATE = {
    "scan-fs": 453, "scan-gh": 15, "scan-vault": 20, "scan-sessions": None, "remotes": 30,
    "bitbucket": None, "domains": 40, "backup": 10, "heroku": 10, "openrouter": 10, "env": 60,
    "scan-cloudflare": 15, "scan-digitalocean": None, "scan-mcp": 10, "scan-agents": 5,
    "scan-harness": None, "scan-spend": 20, "remote-env": 10, "google": 10, "leaks": 1125,
    "scrub-companion": None, "machine": 60, "git-hygiene": 418, "lifecycle": 5, "cleanup": 30,
}
CEILING = 1500.0
#: What the watchdog leaves the tail (`tick_lease.tail_reserve_seconds` at this ceiling).
TAIL_RESERVE = min(300.0, CEILING / 5)
#: A collector's own wall clock (`tick_lease.DEFAULT_STEP_SECONDS`, the leak scan's 1200 s).
OWN_LIMIT = {"leaks": 1200.0}
DEFAULT_LIMIT = 900.0


class Simulation(unittest.TestCase):
    TICKS = 48

    def setUp(self):
        self.tick = load_tick()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.record = Path(tmp.name).resolve() / "tick-rotation.json"
        self.clock = [1_000_000.0]
        self.tick.time = types.SimpleNamespace(time=lambda: self.clock[0])
        self.tick.log = lambda message: None

    def one_tick(self):
        start = self.clock[0]
        deadline = start + CEILING
        self.clock[0] += 3                     # integrity
        t = self.tick.Tick(self.record.parent)
        started, used = [], {}

        def step(name, *argv):
            # The watchdog's arithmetic (tick_lease.bounded): its own limit, its share of the
            # ceiling, and never into the tail's reserve.
            limit = min(OWN_LIMIT.get(name, DEFAULT_LIMIT), tick_budget.share_limit(name, CEILING),
                        deadline - TAIL_RESERVE - self.clock[0])
            if limit <= 0:
                return "not_started"
            started.append(name)
            needs = ESTATE[name]
            if needs is None:
                self.clock[0] += 0.2
                return "disabled"
            spent = min(needs, limit)
            self.clock[0] += spent
            used[name] = spent
            return "ran" if needs <= limit else "stopped"
        t.step = step
        rotation = self.tick.Rotation(t, self.record)
        for name in ESTATE:
            rotation.step(name, "x.py")
        rotation.run_in_turn()
        self.clock[0] = start + 1800          # the next tick, 30 minutes on
        return started, used

    def test_every_collector_starts_within_the_starvation_bound(self):
        last = {name: 0 for name in ESTATE}
        worst = {}
        for n in range(1, self.TICKS + 1):
            started, _ = self.one_tick()
            for name in ESTATE:
                if name in started:
                    worst[name] = max(worst.get(name, 0), n - last[name])
                    last[name] = n
        for name in ESTATE:
            worst[name] = max(worst.get(name, 0), self.TICKS + 1 - last[name])
        bound = tick_budget.STARVED_TICKS + 1
        self.assertLessEqual(max(worst.values()), bound,
                             {k: v for k, v in sorted(worst.items(), key=lambda kv: -kv[1])[:6]})

    def test_no_collector_takes_more_than_its_share(self):
        for _ in range(self.TICKS):
            _, used = self.one_tick()
            for name, spent in used.items():
                self.assertLessEqual(spent, tick_budget.share_limit(name, CEILING) + 1e-6, name)
                self.assertLessEqual(spent, tick_budget.COLLECTOR_SHARE * CEILING + 1e-6, name)

    def test_the_leak_scan_gets_its_bounded_share_and_still_runs_every_few_ticks(self):
        ran = []
        for _ in range(self.TICKS):
            _, used = self.one_tick()
            if "leaks" in used:
                ran.append(used["leaks"])
        self.assertTrue(ran)
        self.assertLessEqual(max(ran), tick_budget.share_limit("leaks", CEILING) + 1e-6)
        self.assertGreaterEqual(len(ran), self.TICKS // (tick_budget.STARVED_TICKS + 1))


class Priority(unittest.TestCase):
    def setUp(self):
        self.tick = load_tick()

    def test_a_disabled_reader_does_not_pull_its_writer_forward(self):
        reads = {"scrub": ("leaks",)}
        started = {"scrub": 1.0, "leaks": 500.0, "google": 100.0}
        order = self.tick.rotation_order(["leaks", "scrub", "google"], started, reads,
                                         priority={"scrub": 2})
        self.assertEqual(order, ["google", "leaks", "scrub"])

    def test_a_starved_collector_goes_ahead_of_one_started_earlier(self):
        started = {"a": 1.0, "b": 2.0, "c": 50.0}
        order = self.tick.rotation_order(["a", "b", "c"], started, {}, priority={"c": 0})
        self.assertEqual(order, ["c", "a", "b"])

    def test_the_priority_of_a_row(self):
        self.assertEqual(tick_budget.priority({"outcome": "disabled", "not_started_streak": 9}), 2)
        self.assertEqual(tick_budget.priority({"not_started_streak": tick_budget.STARVED_TICKS}), 0)
        self.assertEqual(tick_budget.priority({"not_started_streak": tick_budget.STARVED_TICKS - 1}), 1)
        self.assertEqual(tick_budget.priority(None), 1)
        self.assertEqual(tick_budget.priority({"not_started_streak": "x"}), 1)


class Share(unittest.TestCase):
    """The real watchdog hands a collector the smaller of its limit and its share of the ceiling."""

    def deadline_handed(self, name: str, ceiling: int) -> float:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "OBSERVATORY_HOME": str(Path(tmp).resolve() / "home"),
                   "OBSERVATORY_TICK_CEILING_SECONDS": str(ceiling),
                   "OBSERVATORY_TICK_DEADLINE": f"{time.time() + ceiling:.0f}"}
            env.pop("OBSERVATORY_STATE", None)
            before = time.time()
            p = subprocess.run([sys.executable, "tools/tick_lease.py", "step", "--ungated", name, "--",
                                sys.executable, "-c",
                                "import os; print(os.environ['OBSERVATORY_STEP_DEADLINE'])"],
                               cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            return float(p.stdout.strip().splitlines()[-1]) - before

    def test_the_leak_scan_is_held_to_its_share(self):
        handed = self.deadline_handed("leaks", 1000)
        self.assertLessEqual(handed, tick_budget.share_limit("leaks", 1000) + 2)
        self.assertGreater(handed, tick_budget.share_limit("leaks", 1000) - 5)

    def test_any_collector_is_held_to_the_collector_share(self):
        handed = self.deadline_handed("scan-fs", 1000)
        self.assertLessEqual(handed, tick_budget.COLLECTOR_SHARE * 1000 + 2)

    def test_a_tail_step_is_not_held_to_a_collector_share(self):
        handed = self.deadline_handed("findings", 1000)
        self.assertGreater(handed, tick_budget.COLLECTOR_SHARE * 1000 + 100)


if __name__ == "__main__":
    unittest.main()
