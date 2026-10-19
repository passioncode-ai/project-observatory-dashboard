#!/usr/bin/env python3
"""Bound only owned synthetic capture children; no live process inventory."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import signal
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import holders


@unittest.skipIf(os.name == "nt", "POSIX capture; Windows remains unknown")
class ExistingCaptureBounds(unittest.TestCase):
    def test_output_above_payload_cap_is_unknown(self):
        out = holders._run([sys.executable, "-c",
                            "import os; os.write(1, b'x' * (2*1024*1024+1))"], 5)
        self.assertTrue(out is None, "over-limit stdout must be unknown")

    def test_subsecond_budget_is_not_rounded_up(self):
        out = holders._run([sys.executable, "-c",
                            "import time; time.sleep(.2); print('late')"], .05)
        self.assertTrue(out is None, "an expired budget must not accept later output")

    def test_hint_inventory_does_not_fall_back_to_unbounded_proc(self):
        with patch.object(holders, "lsof_paths", return_value=None), \
                patch.object(holders, "proc_paths", side_effect=AssertionError("unbounded fallback")):
            self.assertFalse(holders.Holders.take(timeout=.1).known)


@unittest.skipIf(os.name == "nt", "POSIX capture; Windows remains unknown")
class CaptureContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="holder-capture-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def capture(self, code, *, seconds=2, cap=1024):
        return holders._capture([sys.executable, "-c", code],
                                deadline=time.monotonic() + seconds, max_bytes=cap)

    def test_exact_cap_is_complete_and_one_extra_byte_is_not(self):
        for size, expected in ((1024, True), (1025, False)):
            with self.subTest(size=size):
                result = self.capture(f"import os; os.write(1, b'x'*{size})")
                self.assertEqual(result["complete"], expected)
                self.assertLessEqual(len(result["stdout"]), 1024)
                self.assertTrue(result["reaped"])

    def test_stdout_stderr_and_combined_floods_are_bounded(self):
        for writes in ("os.write(1,b'x'*4096)", "os.write(2,b'x'*4096)",
                       "os.write(1,b'x'*512); os.write(2,b'y'*513)",
                       "os.write(2,b'y'*513); os.write(1,b'x'*512)"):
            with self.subTest(writes=writes):
                result = self.capture("import os\nfor _ in range(10000):\n " + writes)
                self.assertFalse(result["complete"])
                self.assertEqual(result["reason"], "output-limit")
                self.assertLessEqual(len(result["stdout"]), 1024)
                self.assertLessEqual(result["bytes_seen"], 1024 + 65536)
                self.assertTrue(result["reaped"])
                self.assertNotIn("stderr", result)

    def test_silent_partial_and_term_ignoring_children_stop_on_deadline(self):
        # A 1 s budget leaves the full CLEANUP_SECONDS reserve for SIGKILL and the reap.
        # With 0.35 s the reserve was 87 ms, and a loaded macOS runner took longer to
        # reap a TERM-ignoring child, so the capture honestly said cleanup-incomplete
        # (PR #303, run 39201288577). The bound under test is the deadline, not the speed.
        for prefix in ("", "print('partial',flush=True); ",
                       "signal.signal(signal.SIGTERM,signal.SIG_IGN); "):
            with self.subTest(prefix=prefix):
                started = time.monotonic()
                result = self.capture("import time,signal; " + prefix + "time.sleep(30)", seconds=1)
                self.assertFalse(result["complete"])
                self.assertEqual(result["reason"], "command-timeout")
                self.assertTrue(result["reaped"])
                self.assertLess(time.monotonic() - started, 2.5)

    def test_missing_command_read_error_and_invalid_encoding_are_unknown(self):
        result = holders._capture([str(self.base / "missing")], deadline=time.monotonic()+1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["reason"], "command-unavailable")
        with patch.object(holders, "_read", side_effect=OSError("PRIVATE_CAPTURE_ERROR")):
            result = self.capture("print('ready',flush=True); import time; time.sleep(30)")
        self.assertFalse(result["complete"])
        self.assertTrue(result["reaped"])
        self.assertNotIn("PRIVATE_CAPTURE_ERROR", repr(result))
        self.assertIsNone(holders._run([sys.executable,"-c","import os; os.write(1,b'\\xff')"], 2))

    def test_stderr_is_not_retained_or_accepted_as_success(self):
        result = self.capture("import os; os.write(2,b'SYNTHETIC_DIAGNOSTIC')")
        self.assertTrue(result["complete"])
        self.assertTrue(result["stderr_seen"])
        self.assertEqual(result["stdout"], b"")
        self.assertNotIn("SYNTHETIC_DIAGNOSTIC", repr(result))
        self.assertIsNone(holders._run([sys.executable, "-c",
                          "import sys; print('ok'); print('warning',file=sys.stderr)"], 2))

    def test_interrupted_read_still_reaps_owned_child(self):
        created = []
        original = subprocess.Popen
        def spawn(*args, **kwargs):
            child = original(*args, **kwargs)
            created.append(child)
            return child
        with patch.object(holders.subprocess, "Popen", side_effect=spawn), \
                patch.object(holders, "_read", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.capture("print('ready',flush=True); import time; time.sleep(30)")
        self.assertEqual(len(created), 1)
        self.assertIsNotNone(created[0].poll())
        self.assertTrue(created[0].stdout.closed)
        self.assertTrue(created[0].stderr.closed)

    def test_invalid_and_expired_deadlines_do_not_spawn(self):
        with patch.object(holders.subprocess, "Popen", side_effect=AssertionError("must not spawn")):
            for timeout in (0, -1, float("nan"), float("inf"), True, 10**500):
                self.assertIsNone(holders._run(["unused"], timeout))
            result = holders._capture(["unused"], deadline=time.monotonic()-1)
            self.assertFalse(result["complete"])

    def test_owned_descendant_is_stopped_and_outside_child_survives(self):
        outside = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                   start_new_session=True, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: outside.kill() if outside.poll() is None else None)
        self.addCleanup(lambda: outside.wait(timeout=2) if outside.poll() is not None else None)
        pidfile = self.base / "descendant.pid"
        code = ("import os,time,pathlib\nchild=os.fork()\n"
                f"if child:\n pathlib.Path({str(pidfile)!r}).write_text(str(child)); os._exit(0)\n"
                "time.sleep(30)\n")
        child = None
        try:
            result = self.capture(code, seconds=.8)
            self.assertFalse(result["complete"])
            self.assertTrue(result["reaped"])
            child = int(pidfile.read_text())
            # Only the exact PID created by the fixture is inspected, never argv.
            state = subprocess.run(["ps", "-p", str(child), "-o", "stat="],
                                   capture_output=True, text=True, timeout=2).stdout.strip()
            self.assertTrue(not state or state.startswith("Z"), "owned descendant still running")
            self.assertIsNone(outside.poll())
        finally:
            if child is None and pidfile.exists(): child = int(pidfile.read_text())
            if child is not None:
                try: os.kill(child, signal.SIGKILL)
                except ProcessLookupError: pass
            if outside.poll() is None: outside.kill()
            outside.wait(timeout=2)

    def test_repeated_capture_closes_descriptors(self):
        directory = Path("/dev/fd")
        if not directory.is_dir(): self.skipTest("descriptor view unavailable")
        before = len(list(directory.iterdir()))
        for _ in range(8):
            self.assertTrue(self.capture("print('ok')")["complete"])
        self.assertLessEqual(len(list(directory.iterdir())), before)


@unittest.skipIf(os.name == "nt", "POSIX observations; Windows remains unknown")
class ParseAndBudget(unittest.TestCase):
    def test_huge_tokens_field_counts_and_hint_counts_are_unknown(self):
        for text in ("/" + "x"*4096, "x "*100001, "/a "*10001):
            with self.subTest(kind=len(text)), patch.object(holders, "_run", return_value=text):
                self.assertIsNone(holders.argument_paths())
        with patch.object(holders.shutil, "which", return_value="lsof"), \
                patch.object(holders, "_run", return_value="n/a\n"*10001):
            self.assertIsNone(holders.lsof_paths())

    def test_first_phase_cannot_donate_a_fresh_budget_to_second(self):
        def slow(*args, **kwargs):
            time.sleep(.03)
            return ["/synthetic"]
        with patch.object(holders, "lsof_paths", side_effect=slow), \
                patch.object(holders, "argument_paths", side_effect=AssertionError("budget exhausted")):
            self.assertFalse(holders.Holders.take(timeout=.01).known)

    def test_fresh_probe_exhaustion_never_launches_ps(self):
        def slow(*args, **kwargs):
            time.sleep(.03)
            return {"complete": True, "stderr_seen": False, "returncode": 1, "stdout": b""}
        with patch.object(holders.shutil, "which", return_value="synthetic-lsof"), \
                patch.object(holders, "_capture", side_effect=slow), \
                patch.object(holders, "_run", side_effect=AssertionError("no remaining budget")):
            self.assertIsNone(holders.fresh_busy("/synthetic", timeout=.01))

    def test_positive_hints_and_unknown_absence_stay_distinct(self):
        with patch.object(holders, "_run", return_value="python /synthetic/app.py\n"):
            self.assertEqual(holders.argument_paths(), ["/synthetic/app.py"])
            self.assertTrue(holders.running(["python"]))
            self.assertIsNone(holders.running(["missing"]))
        with patch.object(holders, "_run", side_effect=AssertionError("empty query must not spawn")):
            self.assertFalse(holders.running([]))


class WindowsCaptureContract(unittest.TestCase):
    def test_unknown_without_process_launch(self):
        with patch.object(holders.os, "name", "nt"), \
                patch.object(holders.subprocess, "Popen", side_effect=AssertionError("no Windows collector")):
            self.assertIsNone(holders._run(["unused"], 1))
            self.assertIsNone(holders.fresh_busy("synthetic"))
            self.assertIsNone(holders.running(["tool"]))
            self.assertFalse(holders.Holders.take().known)
            self.assertFalse(holders.running([]))


if __name__ == "__main__":
    unittest.main()
