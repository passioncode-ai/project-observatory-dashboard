"""PB-132: a tick that died, or ticks that stopped, are visible from outside the tick."""
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tick_health as T  # noqa: E402

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class TickHealth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name).resolve()
        self.raw = self.state / "raw"
        self.raw.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def receipts(self, started=None, finished=None):
        if started:
            (self.raw / "tick-lease.json").write_text(json.dumps({"last_acquired_at": z(started)}))
        if finished:
            (self.raw / "tick.json").write_text(json.dumps({"finished_at": z(finished), "failed_steps": []}))

    def verdict(self, **kw):
        return T.health(self.state, self.raw, now=NOW, **kw)["verdict"]

    def test_verdicts(self):
        self.assertEqual(self.verdict(), "never")
        self.receipts(NOW - timedelta(minutes=40), NOW - timedelta(minutes=10))
        self.assertEqual(self.verdict(), "ok")
        self.receipts(NOW - timedelta(minutes=5))
        self.assertEqual(self.verdict(), "interrupted", "started after the last finish, and no lock is held")
        self.receipts(NOW - timedelta(hours=9), NOW - timedelta(hours=8))
        self.assertEqual(self.verdict(), "stale")
        self.assertEqual(self.verdict(scheduler_enabled=False), "disabled")

    def test_a_held_lock_is_running_and_a_long_hold_is_named(self):
        lock = self.state / "tick.lock"
        lock.touch()
        holder = subprocess.Popen([sys.executable, "-c",
            "import fcntl,os,sys,time;fd=os.open(sys.argv[1],os.O_RDWR);fcntl.flock(fd,fcntl.LOCK_EX);"
            "print('held',flush=True);time.sleep(30)", str(lock)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            self.receipts(NOW - timedelta(minutes=5), NOW - timedelta(minutes=40))
            self.assertEqual(self.verdict(), "running")
            self.receipts(NOW - timedelta(hours=7), NOW - timedelta(hours=8))
            self.assertEqual(self.verdict(), "running-long")
        finally:
            holder.kill(); holder.wait()
        self.assertEqual(self.verdict(), "interrupted", "the holder is gone and the tick never finished")

    def test_the_probe_does_not_create_the_lock_or_keep_it(self):
        self.receipts(NOW - timedelta(minutes=40), NOW - timedelta(minutes=10))
        self.verdict()
        self.assertFalse((self.state / "tick.lock").exists())
        (self.state / "tick.lock").touch()
        self.verdict()
        fd = os.open(self.state / "tick.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # would raise if the probe kept a lock
        finally:
            os.close(fd)

    def test_degraded_only_when_the_data_may_be_old(self):
        real = datetime.now(timezone.utc)
        self.receipts(real - timedelta(minutes=40), real - timedelta(minutes=10))
        self.assertEqual(T.degraded(self.state, self.raw), [])
        real = datetime.now(timezone.utc)
        self.receipts(real - timedelta(minutes=1), real - timedelta(minutes=50))
        [d] = T.degraded(self.state, self.raw)
        self.assertEqual(d["source"], "tick")
        self.assertTrue(d["reason"].startswith("interrupted"))

    # OBS-40: 42 ticks stopped at their ceiling on one machine and Health said nothing,
    # because it reads only the LAST run. It now says how many of the recent ones finished.
    def runs(self, *outcomes):
        for i, outcome in enumerate(outcomes):
            T.record(self.raw, {"started_at": z(NOW - timedelta(hours=len(outcomes) - i)),
                                "ended_at": z(NOW - timedelta(hours=len(outcomes) - i, minutes=-25)),
                                "outcome": outcome, "exit": 0 if outcome == "finished" else 124,
                                "reason": "" if outcome == "finished" else "the tick reached its 1500 s ceiling"})

    def test_health_counts_the_recent_ticks_that_did_not_finish(self):
        self.receipts(NOW - timedelta(minutes=40), NOW - timedelta(minutes=10))
        self.runs(*(["timeout"] * 3 + ["finished"] * 6 + ["failed"]))
        recent = T.health(self.state, self.raw, now=NOW)["recent"]
        self.assertEqual(recent, {"runs": 10, "incomplete": 4, "outcomes": {"finished": 6, "timeout": 3, "failed": 1}})
        [d] = [d for d in T.degraded(self.state, self.raw) if d["source"] == "tick-history"]
        self.assertTrue(d["reason"].startswith("4 of the last 10 ticks did not finish"), d["reason"])

    def test_an_occasional_incomplete_tick_is_counted_but_not_an_alarm(self):
        self.receipts(datetime.now(timezone.utc) - timedelta(minutes=40), datetime.now(timezone.utc) - timedelta(minutes=10))
        self.runs(*(["finished"] * 8 + ["timeout"] * 2))
        self.assertEqual(T.health(self.state, self.raw)["recent"]["incomplete"], 2)
        self.assertEqual(T.degraded(self.state, self.raw), [])

    def test_the_history_keeps_the_last_fifty_runs(self):
        self.runs(*(["finished"] * 60))
        lines = (self.raw / T.RUNS_FILE).read_text().splitlines()
        self.assertEqual(len(lines), T.KEEP_RUNS)


if __name__ == "__main__":
    unittest.main()
