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


#: OBS-220: the tail of the tick that started 2026-10-24T00:42:45Z on one installed 0.29.0 under a
#: load average above 70, seconds per step as its stage receipt recorded them (the optional steps,
#: which were `not started`, and `findings`, which was stopped at 17 s, at their 20-run means).
NIGHT = {
    "merge": 25.8, "emit": 15.3, "validate": 1.4, "events": 211.5, "snapshot": 9.2, "diff": 1.8,
    "fabric-attribution": 2.7, "index": 2.2, "retention": 9.5, "sweep": 0.9, "corroborate": 27.4,
    "ledger": 3.1, "lost": 0.5, "plugins": 35.2, "rollup": 2.1, "agent": 95.5, "findings": 33.0,
    "dashboard": 17.1, "smoke": 2.4, "notify": 0.9, "registry": 0.8, "project-into-vault": 1.4,
    "projection": 1.9, "links": 3.3, "logs": 1.3,
}
#: The 90th percentile of each reserved step over the same machine's last 50 receipts.
MEASURED_P90 = {
    "merge": 85.0, "emit": 37.2, "validate": 1.5, "events": 123.9, "snapshot": 6.7, "diff": 1.8,
    "index": 2.2, "ledger": 2.5, "rollup": 2.8, "findings": 32.8, "dashboard": 17.1, "smoke": 4.0,
    "notify": 0.9, "registry": 0.9, "project-into-vault": 1.9, "projection": 2.0, "links": 7.6,
    "logs": 1.5,
}
#: The worst each of them took on that machine, all in one tick.
MEASURED_MAX = {
    "merge": 148.6, "emit": 53.1, "validate": 4.8, "events": 259.1, "snapshot": 17.0, "diff": 15.9,
    "fabric-attribution": 22.3, "index": 23.9, "retention": 46.7, "sweep": 3.0, "corroborate": 198.8,
    "ledger": 35.2, "lost": 0.5, "plugins": 415.2, "rollup": 15.7, "agent": 339.4, "findings": 49.1,
    "dashboard": 55.3, "smoke": 24.8, "notify": 3.4, "registry": 4.1, "project-into-vault": 10.9,
    "projection": 21.8, "links": 14.4, "logs": 10.3,
}
SOFT_FLOOR = min(120.0, CEILING / 12)


#: The tail began 292 s before the ceiling that night (the last collector stopped inside the
#: reserve's first seconds), and every step, started or not, cost about a second of process start.
LATE = 8.0
OVERHEAD = 1.0
#: The tail's core before OBS-220 (`tick_lease.CORE_TAIL_STEPS` as it was in 0.29.0).
OLD_CORE = {"merge", "emit", "validate", "events", "snapshot", "diff", "findings", "dashboard", "smoke",
            "findings-recheck", "notify", "registry", "project-into-vault", "projection", "links", "logs"}


def old_limit(name, left):
    """The rule before OBS-220: a core step runs to the ceiling, an optional one leaves 120 s."""
    return left - (0.0 if name in OLD_CORE else SOFT_FLOOR)


def run_tail(durations, limit_of, budget):
    """{step: (outcome, seconds given)} of the tail run in TAIL_ORDER with `budget` seconds left."""
    left, out = float(budget), {}
    for name in tick_budget.TAIL_ORDER:
        if name == "findings-recheck":
            continue                                   # runs only when the page renders blank
        left -= OVERHEAD
        limit = limit_of(name, left)
        if limit <= 0:
            out[name] = ("not_started", 0.0)
            continue
        # A step the tick gained after the night measured (`review-policy`, OBS-227) took nothing on it.
        took = durations.get(name, 0.0)
        took = took(limit) if callable(took) else took
        spent = min(took, limit)
        left -= spent
        out[name] = ("ran" if took <= limit else "stopped", limit)
    return out


class TailBudget(unittest.TestCase):
    """OBS-220: the collectors leave the tail what it has measured, and every tail step leaves the
    steps that must finish after it theirs."""

    def history(self, values, runs=20):
        return [{"stages": [{"step": name, "outcome": "ran", "duration_seconds": seconds}
                            for name, seconds in values.items()]} for _ in range(runs)]

    def test_the_tail_order_is_the_order_the_tick_runs(self):
        sys.path.insert(0, str(ROOT / "tests"))
        import tick_reader
        calls = [name for _line, _fn, name, _words in tick_reader.tick_calls(ROOT)]
        tail = list(dict.fromkeys(calls[calls.index("merge"):]))
        self.assertEqual(tail, list(tick_budget.TAIL_ORDER))
        self.assertLessEqual(tick_budget.RESERVED, set(tick_budget.TAIL_ORDER))
        for name in ("registry", "emit", "findings", "dashboard", "index", "ledger", "rollup"):
            self.assertIn(name, tick_budget.MUST_FINISH)

    def test_a_need_is_the_90th_percentile_of_the_recent_runs(self):
        runs = [{"stages": [{"step": "events", "outcome": "ran", "duration_seconds": float(n)},
                            {"step": "findings", "outcome": "stopped", "duration_seconds": 17.0},
                            {"step": "findings", "outcome": "not_started", "duration_seconds": 0.2}]}
                for n in range(1, 31)]
        need = tick_budget.needs(runs)
        self.assertEqual(need["events"], 28.0, "the nearest-rank 90th percentile of the newest 20 runs (11..30)")
        self.assertEqual(need["findings"], 17.0, "a stopped run counts; a step not started does not")
        self.assertEqual(need["dashboard"], tick_budget.DEFAULT_NEED["dashboard"])
        self.assertNotIn("dashboard", tick_budget.needs(runs, defaults=False))

    def test_a_damaged_history_is_no_history(self):
        runs = [None, "x", {"stages": "x"}, {"stages": [None, {"step": "emit", "outcome": "ran",
                                                               "duration_seconds": "x"},
                                                        {"step": "emit", "outcome": "ran",
                                                         "duration_seconds": float("nan")}]}]
        self.assertEqual(tick_budget.needs(runs), {k: float(v) for k, v in tick_budget.DEFAULT_NEED.items()})

    def test_without_a_history_the_collectors_keep_the_old_reserve(self):
        self.assertLess(tick_budget.NEED_MARGIN * sum(tick_budget.DEFAULT_NEED.values()), TAIL_RESERVE)
        self.assertEqual(tick_budget.collector_reserve({}, CEILING, TAIL_RESERVE), TAIL_RESERVE)

    def test_the_collectors_leave_the_measured_tail_within_a_cap(self):
        reserve = tick_budget.collector_reserve(MEASURED_P90, CEILING, TAIL_RESERVE)
        self.assertAlmostEqual(reserve, sum(tick_budget.given(name, MEASURED_P90) for name in MEASURED_P90))
        self.assertGreaterEqual(reserve, tick_budget.NEED_MARGIN * sum(MEASURED_P90.values()))
        self.assertGreater(reserve, TAIL_RESERVE)
        slow = {name: 500.0 for name in tick_budget.RESERVED}
        self.assertEqual(tick_budget.collector_reserve(slow, CEILING, TAIL_RESERVE),
                         tick_budget.RESERVE_CAP * CEILING)

    def test_the_night_of_2026_10_24_stopped_findings_under_the_old_rule(self):
        # The flat 300 s reserve, and `events` running into it: what the receipt recorded.
        tail = run_tail(NIGHT, old_limit, TAIL_RESERVE - LATE)
        self.assertEqual(tail["findings"][0], "stopped")
        self.assertLess(tail["findings"][1], NIGHT["findings"])
        self.assertEqual(tail["index"][0], "not_started")

    def test_the_same_night_now_finishes_every_step_that_must_finish(self):
        need = tick_budget.needs(self.history(MEASURED_P90))
        budget = tick_budget.collector_reserve(tick_budget.needs(self.history(MEASURED_P90), defaults=False),
                                               CEILING, TAIL_RESERVE)
        tail = run_tail(NIGHT, lambda name, left: tick_budget.tail_limit(
            name, left, need, SOFT_FLOOR if name not in tick_budget.RESERVED else 0.0), budget - LATE)
        for name in tick_budget.MUST_FINISH:
            self.assertEqual(tail[name][0], "ran", (name, tail[name]))
        self.assertIn(tail["events"][0], ("ran", "stopped"))

    def test_the_worst_measured_tail_still_gives_each_step_that_must_finish_its_measured_time(self):
        need = tick_budget.needs(self.history(MEASURED_P90))
        budget = tick_budget.collector_reserve(MEASURED_P90, CEILING, TAIL_RESERVE)
        tail = run_tail(MEASURED_MAX, lambda name, left: tick_budget.tail_limit(
            name, left, need, SOFT_FLOOR if name not in tick_budget.RESERVED else 0.0), budget - LATE)
        # Every step at its worst in one tick is more than any reserve holds: something is cut. What
        # is promised is that each step that must finish, up to the notice a person reads, starts and
        # is given at least its own measured time; the wiki projection after it may wait a tick.
        upto = tick_budget.TAIL_ORDER[:tick_budget.TAIL_ORDER.index("notify") + 1]
        for name in (n for n in upto if n in tick_budget.MUST_FINISH):
            outcome, given = tail[name]
            self.assertNotEqual(outcome, "not_started", name)
            self.assertGreaterEqual(given + 1e-6, min(MEASURED_MAX[name],
                                                      tick_budget.NEED_MARGIN * need[name]), name)
        self.assertEqual(tail["merge"][0], "ran")
        self.assertGreaterEqual(tail["findings"][1], tick_budget.NEED_MARGIN * need["findings"])

    def test_a_validator_after_a_long_emit_still_gets_its_minimum(self):
        # Review of #352 (M1), replayed on one machine's receipts: after a 270 s emit, `validate`,
        # whose 90th percentile is about a second and a half on a quiet run but which measures 3 to
        # 4 s under load, was handed 1.6 to 4.2 s, stopped, and read as a red validator.
        for validate_p90, validate_now in ((1.29, 3.1), (1.5, 3.9), (3.39, 4.24)):
            hist = {**MEASURED_P90, "validate": validate_p90}
            need = tick_budget.needs(self.history(hist))
            budget = tick_budget.collector_reserve(tick_budget.needs(self.history(hist), defaults=False),
                                                   CEILING, TAIL_RESERVE)
            # The emit takes all it is given and finishes on its limit: the 270 s of the review.
            night = {**NIGHT, "merge": 30.0, "emit": lambda limit: limit, "validate": validate_now}
            tail = run_tail(night, lambda name, left: tick_budget.tail_limit(
                name, left, need, SOFT_FLOOR if name not in tick_budget.RESERVED else 0.0), budget - LATE)
            self.assertEqual(tail["emit"][0], "ran", tail["emit"])
            self.assertEqual(tail["validate"][0], "ran", (validate_p90, tail["validate"]))
            self.assertGreaterEqual(tail["validate"][1], tick_budget.MUST_FINISH_MIN["validate"])
            for name in ("findings", "dashboard", "notify"):
                self.assertEqual(tail[name][0], "ran", (validate_p90, name, tail[name]))

    def test_every_step_that_must_finish_has_a_minimum(self):
        need = {name: 0.1 for name in tick_budget.RESERVED}
        for name in tick_budget.MUST_FINISH:
            self.assertGreaterEqual(tick_budget.given(name, need), tick_budget.MUST_FINISH_MIN_DEFAULT, name)
            self.assertEqual(tick_budget.tail_limit(name, 1000.0 if name == "logs" else 0.5, need) > 0, True, name)
        self.assertGreaterEqual(tick_budget.given("findings", need), 30.0)
        # The minimums are reserved by every step before them, the optional ones included.
        self.assertGreaterEqual(tick_budget.reserve_after("events", need),
                                sum(tick_budget.MUST_FINISH_MIN.get(n, tick_budget.MUST_FINISH_MIN_DEFAULT)
                                    for n in tick_budget.TAIL_ORDER[tick_budget.TAIL_ORDER.index("events") + 1:]
                                    if n in tick_budget.MUST_FINISH))

    def test_a_step_that_must_finish_takes_its_own_time_out_of_what_is_left(self):
        need = {**tick_budget.DEFAULT_NEED, "findings": 40.0, "dashboard": 20.0}
        # 30 s left: less than the dashboard and the rest are owed, but findings still runs.
        self.assertGreater(tick_budget.tail_limit("findings", 30.0, need), 0)
        self.assertEqual(tick_budget.tail_limit("findings", 30.0, need), 30.0)
        # An optional step with the same 30 s does not start.
        self.assertLessEqual(tick_budget.tail_limit("events", 30.0, need), 0)
        self.assertLessEqual(tick_budget.tail_limit("plugins", 30.0, need, SOFT_FLOOR), 0)


class TailWatchdog(unittest.TestCase):
    """The real watchdog (`tick_lease.bounded`) reads the history and hands each tail step its limit."""

    def handed(self, name: str, left: float, history: list[dict]) -> float | None:
        with tempfile.TemporaryDirectory() as tmp:
            # The history goes where the watchdog reads it, the workspace's scratch, named outright.
            scratch = Path(tmp).resolve() / "scratch"
            scratch.mkdir()
            (scratch / "tick-runs.jsonl").write_text(
                "".join(json.dumps({"outcome": "finished", "stage_progress": run}) + "\n" for run in history),
                encoding="utf-8")
            env = {**os.environ, "OBSERVATORY_HOME": str(Path(tmp).resolve() / "home"),
                   "OBSERVATORY_SCRATCH": str(scratch),
                   "OBSERVATORY_TICK_CEILING_SECONDS": str(int(CEILING)),
                   "OBSERVATORY_TICK_DEADLINE": f"{time.time() + left:.3f}"}
            env.pop("OBSERVATORY_STATE", None)
            before = time.time()
            p = subprocess.run([sys.executable, "tools/tick_lease.py", "step", "--ungated", name, "--",
                                sys.executable, "-c",
                                "import os; print(os.environ['OBSERVATORY_STEP_DEADLINE'])"],
                               cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
            if p.returncode == 124:
                return None
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            return float(p.stdout.strip().splitlines()[-1]) - before

    def history(self):
        return [{"stages": [{"step": n, "outcome": "ran", "duration_seconds": s}
                            for n, s in MEASURED_P90.items()]} for _ in range(5)]

    def test_events_leaves_the_steps_after_it_their_measured_time(self):
        need = tick_budget.needs(self.history())
        handed = self.handed("events", 300, self.history())
        self.assertIsNotNone(handed)
        self.assertLessEqual(handed, 300 - tick_budget.reserve_after("events", need) + 3)

    def test_findings_runs_with_its_measured_time_even_late(self):
        need = tick_budget.needs(self.history())
        handed = self.handed("findings", 45, self.history())
        self.assertIsNotNone(handed, "findings was not started with 45 s left")
        self.assertGreaterEqual(handed, min(45, tick_budget.NEED_MARGIN * need["findings"]) - 3)

    def test_a_collector_leaves_the_measured_reserve(self):
        reserve = tick_budget.collector_reserve(tick_budget.needs(self.history(), defaults=False),
                                                CEILING, TAIL_RESERVE)
        self.assertGreater(reserve, TAIL_RESERVE)
        self.assertIsNone(self.handed("scan-gh", reserve - 5, self.history()),
                          "a collector started inside the measured reserve")
        handed = self.handed("scan-gh", reserve + 60, self.history())
        self.assertIsNotNone(handed)
        self.assertLessEqual(handed, 63)


if __name__ == "__main__":
    unittest.main()
