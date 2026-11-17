#!/usr/bin/env python3
"""`fabric.job.cancel` lets a runner finish its own cleanup before any forced stop (OBS-590).

On Windows a forced stop is `taskkill /T /F`: no handler runs, so a runner stopped that way never withdraws what it
filed (a secret prompt kept its request until its deadline). The cancel therefore asks first: it marks the record
`cancelled`, a runner that listens (`jobs.listen`, then `jobs.pause` between polls) sees it, runs its cleanup and
exits; only a runner still there after the bounded wait, or one that never listened, is stopped by force. The same
assertions run on POSIX and on the Windows CI job. No test reads the wall clock for its answer: the cooperative case
waits on the runner's own exit, the bounded case injects the clock.
"""
from __future__ import annotations

import itertools
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tmp as tmpdir                                   # noqa: E402,F401  a sandboxed workspace for `paths`

import jobs                                            # noqa: E402
import osproc                                          # noqa: E402

#: A bound on a hang, never the answer (OBS-588).
HANG_GUARD_SECONDS = 300

#: A runner that listens for its cancel and, when it comes, does its own cleanup (here: writes what stopped it).
LISTENING = """
import sys
sys.path.insert(0, sys.argv[1])
import jobs
job_id, out = sys.argv[2], sys.argv[3]
jobs.listen(job_id, statusMessage="polling")
try:
    while True:
        jobs.pause(job_id, 2)
except SystemExit as exc:
    open(out, "w").write(type(exc).__name__)
    raise
"""
#: A runner that says it listens and never looks: the bounded wait must still end in a stop.
DEAF = """
import sys, time
sys.path.insert(0, sys.argv[1])
import jobs
jobs.listen(sys.argv[2], statusMessage="polling")
while True:
    time.sleep(0.05)
"""
#: A runner that never listens (an LLM call, a scan): stopped at once, as before.
PLAIN = """
import sys, time
sys.path.insert(0, sys.argv[1])
import jobs
jobs.update(sys.argv[2], statusMessage="polling")
while True:
    time.sleep(0.05)
"""


class Base(unittest.TestCase):
    def start(self, code: str) -> tuple[str, subprocess.Popen, Path]:
        out = Path(tmpdir.mkdtemp(prefix="observatory-cancel-")) / "stopped-by"
        children: list[subprocess.Popen] = []

        def spawn(argv, env):
            child = subprocess.Popen([sys.executable, "-c", code, str(ROOT), argv[-1], str(out)], env=env,
                                     stdin=subprocess.DEVNULL, **osproc.new_group())
            children.append(child)
            return child
        doc, joined = jobs.start("test.cancel", {"traceId": "t"}, spawn=spawn, exclusive=False)
        self.assertFalse(joined)
        child = children[0]
        self.addCleanup(lambda: (osproc.stop_group(child.pid, 0.5), child.wait(timeout=HANG_GUARD_SECONDS)))
        def polling():
            if (jobs.get(doc["id"]) or {}).get("statusMessage") == "polling":
                return True
            if child.poll() is not None:
                self.fail(f"the runner exited before it polled: {child.returncode}")
            return False
        self.wait_for(polling, "the runner polling")
        return doc["id"], child, out

    def wait_for(self, done, what: str):
        end = time.monotonic() + HANG_GUARD_SECONDS
        while time.monotonic() < end:
            answer = done()
            if answer:
                return answer
            time.sleep(0.05)
        self.fail(f"{what} never happened")


class ACancelAsksFirst(Base):
    def test_a_listening_runner_does_its_own_cleanup_and_is_never_forced(self):
        job_id, child, out = self.start(LISTENING)
        with mock.patch.object(jobs.osproc, "signal_group", wraps=osproc.signal_group) as force:
            doc = jobs.cancel(job_id, wait=HANG_GUARD_SECONDS)
        # The cancel returns once the runner exited by itself: its cleanup ran, and no forced stop was sent.
        self.assertEqual(doc["status"], "cancelled")
        self.assertEqual(force.call_count, 0, "a runner that listened was stopped by force")
        self.assertEqual(out.read_text(encoding="utf-8"), "Cancelled")
        child.wait(timeout=HANG_GUARD_SECONDS)
        self.assertEqual(jobs.get(job_id)["status"], "cancelled")

    def test_a_runner_that_listens_and_never_looks_is_stopped_after_the_bounded_wait(self):
        job_id, child, out = self.start(DEAF)
        ticks = itertools.count(0, 100)
        with mock.patch.object(jobs.osproc, "signal_group", wraps=osproc.signal_group) as force:
            doc = jobs.cancel(job_id, wait=10, clock=lambda: next(ticks), sleep=lambda s: None)
        self.assertEqual(doc["status"], "cancelled")
        self.assertEqual([c.args for c in force.call_args_list], [(child.pid, signal.SIGTERM)])
        child.wait(timeout=HANG_GUARD_SECONDS)
        self.assertFalse(out.exists())
        self.assertEqual(jobs.get(job_id)["status"], "cancelled")

    def test_a_runner_that_never_listened_is_stopped_at_once(self):
        job_id, child, _ = self.start(PLAIN)

        def no_wait(seconds):
            raise AssertionError("the cancel waited for a runner that never listened")
        with mock.patch.object(jobs.osproc, "signal_group", wraps=osproc.signal_group) as force:
            doc = jobs.cancel(job_id, sleep=no_wait)
        self.assertEqual(doc["status"], "cancelled")
        self.assertEqual(force.call_count, 1)
        child.wait(timeout=HANG_GUARD_SECONDS)

    def test_a_finished_job_is_returned_unchanged(self):
        job_id, child, _ = self.start(PLAIN)
        jobs.cancel(job_id)
        child.wait(timeout=HANG_GUARD_SECONDS)
        before = jobs.get(job_id)
        with mock.patch.object(jobs.osproc, "signal_group") as force:
            self.assertEqual(jobs.cancel(job_id), before)
        force.assert_not_called()


class PauseListens(unittest.TestCase):
    """`pause` without a process: the record is the signal, the clock is injected."""

    def record(self) -> str:
        doc, _ = jobs.start("test.pause", {"traceId": "t"}, spawn=lambda argv, env: (_ for _ in ()).throw(OSError()),
                            exclusive=False)
        # The spawn failed on purpose, so the job is terminal; make it a working one the test controls.
        jobs._write({**jobs._read(doc["id"]), "status": "working", "pid": None})
        return doc["id"]

    def test_pause_returns_after_its_time_when_nobody_cancels(self):
        job_id = self.record()
        ticks = itertools.count(0, 1)
        slept: list[float] = []
        jobs.pause(job_id, 2, clock=lambda: next(ticks), sleep=slept.append)
        self.assertTrue(slept)
        self.assertTrue(all(0 < s <= jobs.LISTEN_STEP for s in slept))

    def test_pause_raises_cancelled_once_the_record_says_so(self):
        job_id = self.record()
        jobs._write({**jobs._read(job_id), "status": "cancelled"})
        with self.assertRaises(jobs.Cancelled) as caught:
            jobs.pause(job_id, 2, clock=lambda: 0, sleep=lambda s: None)
        # A SystemExit, like the SIGTERM handler's, so every cleanup written for the signal runs for it too.
        self.assertIsInstance(caught.exception, SystemExit)

    def test_listen_marks_the_record_and_never_revives_a_finished_job(self):
        job_id = self.record()
        jobs.listen(job_id, statusMessage="polling")
        self.assertEqual(jobs.get(job_id)["cancel"], "cooperative")
        jobs._write({**jobs._read(job_id), "status": "cancelled"})
        jobs.listen(job_id, statusMessage="again")
        self.assertEqual(jobs.get(job_id)["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
