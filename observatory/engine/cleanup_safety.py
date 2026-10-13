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


def bare_repository_reason(root: str) -> str:
    """Recognize repository structure without executing Git or trusting a directory name.

    Bare repositories have no .git marker. HEAD, objects and refs/config together
    are enough to preserve a tree; unreadable structural probes preserve it too.
    """
    def mode(name: str) -> int | None:
        try:
            return os.lstat(os.path.join(root, name)).st_mode
        except (FileNotFoundError, NotADirectoryError):
            return None
    try:
        head = mode("HEAD")
        if head is None or not (stat.S_ISREG(head) or stat.S_ISLNK(head)):
            return ""
        objects = mode("objects")
        if objects is None or not (stat.S_ISDIR(objects) or stat.S_ISLNK(objects)):
            return ""
        refs, config = mode("refs"), mode("config")
        if ((refs is not None and (stat.S_ISDIR(refs) or stat.S_ISLNK(refs)))
                or (config is not None and (stat.S_ISREG(config) or stat.S_ISLNK(config)))):
            return "bare repository structure"
    except OSError:
        return "repository structure could not be read"
    return ""


def bare_ancestry_reason(root: str) -> str:
    """Preserve configured subtrees inside a bare repository, lexical or resolved."""
    for candidate in {os.path.abspath(root), os.path.realpath(root)}:
        current = candidate
        while True:
            reason = bare_repository_reason(current)
            if reason:
                return reason
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    return ""


def protected_tree_reason(root: str, *, max_entries: int = 50_000, timeout: float = 2.0) -> str:
    """Empty only for a complete, bounded walk without protected or uncertain entries."""
    reason = bare_ancestry_reason(root)
    if reason:
        return reason
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
                reason = bare_repository_reason(path)
                if reason:
                    return reason
                with os.scandir(path) as entries:
                    for entry in entries:
                        if time.monotonic() >= deadline or count + len(pending) >= max_entries:
                            return "artifact inspection budget exhausted"
                        pending.append(entry.path)
        except OSError:
            return "artifact contents could not be completely inspected"
    return ""
