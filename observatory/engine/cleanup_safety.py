"""Shared fail-closed exclusions for paths proposed as disposable output.

Git collapses an ignored directory to one status row, hiding nested repositories
and local databases. Prove its contents are ordinary output before treating that
row as disposable. This is a bounded observation, not a lock against concurrent
writers; callers still recheck at their removal boundary.
"""
from __future__ import annotations

import os
import stat
import time

STATE_MARKERS = frozenset({".git", ".wrangler"})


def protected_entry_reason(name: str, mode: int) -> str:
    """One predicate shared by cleanup and the disk guard's own bounded walker."""
    if name in STATE_MARKERS:
        return "repository or local-state marker"
    if stat.S_ISLNK(mode):
        return "a symlink prevents proving disposable contents"
    if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
        return "a special file prevents proving disposable contents"
    return ""


def protected_tree_reason(root: str, *, max_entries: int = 50_000, timeout: float = 2.0) -> str:
    """Empty only for a complete, bounded walk without protected or uncertain entries."""
    deadline, count = time.monotonic() + timeout, 0
    pending = [root]
    while pending:
        if time.monotonic() >= deadline or count >= max_entries:
            return "artifact inspection budget exhausted"
        path = pending.pop()
        count += 1
        try:
            current = os.lstat(path)
            reason = protected_entry_reason(os.path.basename(path), current.st_mode)
            if reason:
                return reason
            if stat.S_ISDIR(current.st_mode):
                with os.scandir(path) as entries:
                    for entry in entries:
                        if time.monotonic() >= deadline or count + len(pending) >= max_entries:
                            return "artifact inspection budget exhausted"
                        pending.append(entry.path)
        except OSError:
            return "artifact contents could not be completely inspected"
    return ""
