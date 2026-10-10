#!/usr/bin/env python3
"""`oslocks.flock` keeps `fcntl.flock`'s contract on every OS (docs/design/WINDOWS-LINUX.md, W1).

Each case holds the lock from a SECOND process, as the engine's locks are meant to be held, and
asserts what the first sees — so the same assertions run on POSIX (`fcntl`) and on the Windows CI
row (`LockFileEx`), and a difference between the two is a red test rather than a field report.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import oslocks  # noqa: E402

HOLDER = """
import os, sys, time
sys.path.insert(0, {root!r})
import oslocks
fd = os.open({path!r}, os.O_RDWR | os.O_CREAT)
oslocks.flock(fd, getattr(oslocks, {mode!r}))
print("held", flush=True)
sys.stdin.readline()
"""


class Flock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # A cleanup, not tearDown: cleanups run last-in first-out, so the directory goes after
        # every descriptor and holder a test registered — Windows refuses to delete an open file.
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "lock")
        Path(self.path).write_bytes(b"data")

    def hold(self, mode: str) -> subprocess.Popen:
        p = subprocess.Popen([sys.executable, "-c", HOLDER.format(root=str(ROOT), path=self.path, mode=mode)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (p.stdin.close(), p.wait(timeout=30)))
        self.assertEqual(p.stdout.readline().strip(), "held")
        return p

    def open(self) -> int:
        fd = os.open(self.path, os.O_RDWR)
        self.addCleanup(os.close, fd)
        return fd

    def test_an_exclusive_lock_held_elsewhere_refuses_another(self):
        self.hold("LOCK_EX")
        with self.assertRaises(BlockingIOError):
            oslocks.flock(self.open(), oslocks.LOCK_EX | oslocks.LOCK_NB)
        with self.assertRaises(BlockingIOError):
            oslocks.flock(self.open(), oslocks.LOCK_SH | oslocks.LOCK_NB)

    def test_shared_locks_coexist_and_refuse_an_exclusive_one(self):
        self.hold("LOCK_SH")
        fd = self.open()
        oslocks.flock(fd, oslocks.LOCK_SH | oslocks.LOCK_NB)
        oslocks.flock(fd, oslocks.LOCK_UN)
        with self.assertRaises(BlockingIOError):
            oslocks.flock(self.open(), oslocks.LOCK_EX | oslocks.LOCK_NB)

    def test_a_released_lock_can_be_taken(self):
        p = self.hold("LOCK_EX")
        p.stdin.write("go\n")
        p.stdin.flush()
        p.wait(timeout=30)
        deadline = time.monotonic() + 10
        fd = self.open()
        while True:
            try:
                oslocks.flock(fd, oslocks.LOCK_EX | oslocks.LOCK_NB)
                break
            except BlockingIOError:
                self.assertLess(time.monotonic(), deadline, "the holder exited and its lock stayed")
                time.sleep(0.05)
        oslocks.flock(fd, oslocks.LOCK_UN)

    def test_the_lock_does_not_block_reading_or_writing_the_file(self):
        # Windows byte-range locks are mandatory; the lock sits past the end of the file so the
        # data stays readable and writable by others, as with POSIX advisory locks.
        self.hold("LOCK_EX")
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), b"data")
        with open(self.path, "ab") as f:
            f.write(b"+more")
        self.assertEqual(Path(self.path).read_bytes(), b"data+more")

    def test_unlocking_what_is_not_locked_is_quiet(self):
        oslocks.flock(self.open(), oslocks.LOCK_UN)

    def test_no_engine_module_imports_fcntl_directly(self):
        # Every lock goes through oslocks; a direct `import fcntl` is what stopped the engine
        # from importing on Windows.
        offenders = []
        for f in ROOT.rglob("*.py"):
            if f.name == "oslocks.py" or "node_modules" in f.parts:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            if text.startswith("# Vendored from"):
                # Kept byte for byte with its upstream kit (test_fabric_service); its Windows
                # support belongs upstream — docs/design/WINDOWS-LINUX.md, "Dependencies".
                continue
            if any(line.strip().startswith(("import fcntl", "from fcntl")) or line.strip().startswith("import ")
                   and "fcntl" in line.split("#")[0].replace(",", " ").split()
                   for line in text.splitlines()):
                offenders.append(str(f.relative_to(ROOT)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
