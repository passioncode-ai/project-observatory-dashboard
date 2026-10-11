#!/usr/bin/env python3
"""`osproc` keeps the engine's process-group contract on every OS (docs/design/WINDOWS-LINUX.md, W2).

The same assertions run on POSIX and on the Windows CI job: a child in a group of its own, a
check that a process is alive that never touches it, and a stop that reaches the grandchild.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import osproc  # noqa: E402

SLEEPER = "import time; time.sleep(120)"
# A parent that starts a grandchild, writes its pid, and waits.
PARENT = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(sys.argv[1], "w").write(str(child.pid))
time.sleep(120)
"""


def _wait_dead(pid: int, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not osproc.alive(pid):
            return True
        time.sleep(0.05)
    return False


class Processes(unittest.TestCase):
    def spawn(self, code: str, *args: str) -> subprocess.Popen:
        p = subprocess.Popen([sys.executable, "-c", code, *args], stdin=subprocess.DEVNULL,
                             **osproc.new_group())
        self.addCleanup(lambda: (osproc.stop_group(p.pid, 0.5), p.wait(timeout=30)))
        return p

    def test_asking_whether_a_process_is_alive_never_stops_it(self):
        # On Windows `os.kill(pid, 0)` terminates the process; `alive` must only ask.
        p = self.spawn(SLEEPER)
        for _ in range(5):
            self.assertTrue(osproc.alive(p.pid))
        time.sleep(0.3)
        self.assertIsNone(p.poll(), "the check stopped the process it asked about")

    def test_a_finished_process_is_not_alive(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait(timeout=30)
        self.assertTrue(_wait_dead(p.pid))
        self.assertFalse(osproc.alive(0))
        self.assertFalse(osproc.alive(-1))
        self.assertFalse(osproc.alive("12"))

    def test_this_process_is_alive(self):
        import os
        self.assertTrue(osproc.alive(os.getpid()))

    def test_stopping_the_group_reaches_the_grandchild(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "grandchild.pid"
            p = self.spawn(PARENT, str(marker))
            deadline = time.monotonic() + 30
            while not (marker.exists() and marker.read_text().strip()):
                self.assertLess(time.monotonic(), deadline, "the grandchild never started")
                time.sleep(0.05)
            grandchild = int(marker.read_text())
            self.assertTrue(osproc.group_alive(p.pid))
            osproc.stop_group(p.pid, 2.0)
            p.wait(timeout=30)
            self.assertTrue(_wait_dead(grandchild), "the grandchild outlived its group")
            self.assertFalse(osproc.group_alive(p.pid))

    def test_stopping_a_group_that_is_gone_is_quiet(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"], **osproc.new_group())
        p.wait(timeout=30)
        osproc.stop_group(p.pid, 0.1)
        self.assertFalse(osproc.signal_group(p.pid, signal.SIGTERM))

    def test_every_stop_signal_can_be_handled_here(self):
        sigs = osproc.stop_signals()
        self.assertIn(signal.SIGTERM, sigs)
        self.assertIn(signal.SIGINT, sigs)
        for sig in sigs:
            previous = signal.signal(sig, lambda *_: None)
            signal.signal(sig, previous)

    def test_a_deadline_interrupts_the_work_and_is_gone_after_it(self):
        class Expired(Exception):
            pass

        def expire():
            raise Expired
        started = time.monotonic()
        with self.assertRaises(Expired):
            with osproc.deadline(1, expire):
                while time.monotonic() - started < 30:
                    time.sleep(0.05)
        self.assertLess(time.monotonic() - started, 10)
        with osproc.deadline(1, expire):
            pass
        time.sleep(1.5)  # a deadline that was left must not fire later

    def test_a_disarmed_deadline_never_fires(self):
        def expire():
            raise AssertionError("the disarmed deadline fired")
        with osproc.deadline(1, expire):
            osproc.disarm_deadline()
            time.sleep(1.5)
        osproc.disarm_deadline()  # nothing running: quiet

    def test_unblocking_the_stop_signals_is_safe_everywhere(self):
        osproc.unblock_stop_signals()

    def test_no_engine_module_manages_process_groups_by_hand(self):
        # `os.killpg`, `start_new_session=True` and `os.kill(pid, 0)` are POSIX-only, and the
        # last one kills on Windows: every one goes through osproc.
        pattern = re.compile(r"os\.killpg\(|start_new_session\s*=\s*True|os\.kill\([^)]*,\s*0\s*\)"
                             r"|signal\.SIGHUP\b|signal\.pthread_sigmask\(|signal\.alarm\(")
        offenders = []
        for f in ROOT.rglob("*.py"):
            rel = f.relative_to(ROOT)
            if f.name == "osproc.py" or rel.parts[0] == "tests" or "node_modules" in rel.parts:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            if text.startswith("# Vendored from"):
                continue  # upstream kit: docs/design/WINDOWS-LINUX.md, "Dependencies"
            for n, line in enumerate(text.splitlines(), 1):
                if pattern.search(line.split("#")[0]):
                    offenders.append(f"{rel.as_posix()}:{n}")
        self.assertEqual(offenders, [])



class Program(unittest.TestCase):
    """W7: a tool npm installs is `<name>.cmd` on Windows, which a bare name does not start."""

    def test_a_path_and_an_unknown_name_are_left_alone(self):
        import osproc
        self.assertEqual(osproc.program([sys.executable, "-V"]), [sys.executable, "-V"])
        self.assertEqual(osproc.program(["no-such-tool-xyz", "a"]), ["no-such-tool-xyz", "a"])

    @unittest.skipUnless(os.name == "nt", "PATHEXT is Windows'")
    def test_a_cmd_tool_resolves_to_its_full_path(self):
        import osproc
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            tool = Path(folder) / "obs-probe-tool.cmd"
            tool.write_text("@echo off\r\necho ran %1\r\n")
            with mock.patch.dict(os.environ, {"PATH": folder + os.pathsep + os.environ.get("PATH", "")}):
                argv = osproc.program(["obs-probe-tool", "x"])
                self.assertEqual(Path(argv[0]).name.lower(), "obs-probe-tool.cmd")
                out = subprocess.run(argv, capture_output=True, text=True)
                self.assertIn("ran x", out.stdout)

if __name__ == "__main__":
    unittest.main(verbosity=2)
