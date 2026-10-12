"""The one lock every cleanup takes: the disk guard, `tools/cleanup.py` (the tick's step and a
person's `full cleanup`), and the git tier the guard starts.

Before 2026-10-12 nothing serialised them: the tick's `cleanup --auto` and the guard's
`cleanup --auto --level L` could delete the same branch twice (the loser journalled "failed")
and write the plan file over each other (docs/design/DISK-GUARD.md). A run that finds the
lock taken does nothing and says so; a child the guard starts inherits the guard's hold
through `HELD_ENV`, so it does not wait on its own parent.
"""
from __future__ import annotations

import contextlib
import os

import oslocks
import osprivacy
import paths

HELD_ENV = "OBSERVATORY_CLEANUP_LOCK_HELD"


@contextlib.contextmanager
def held():
    """Yields True while this process holds the lock (or its parent guard does), False when
    another cleanup has it."""
    if os.environ.get(HELD_ENV) == "1":
        yield True
        return
    lock = paths.STATE / "cleanup.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = osprivacy.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            oslocks.flock(fd, oslocks.LOCK_EX | oslocks.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            with contextlib.suppress(OSError):
                oslocks.flock(fd, oslocks.LOCK_UN)
    finally:
        os.close(fd)
