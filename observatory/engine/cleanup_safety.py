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

#: The file an agent service writes at the top of a folder that holds live state (in-flight
#: jobs, runner permissions) under a root the cleanup counts as regenerable, such as
#: `~/Library/Caches` or `~/.cache` (OBS-203). Wherever it appears in a tree, no tier removes
#: the tree: one service's job workspaces in its cache folder were lost on 2026-10-20.
KEEP_MARKER = ".observatory-keep"
KEEP_REASON = f"the folder carries {KEEP_MARKER}: an agent service keeps live state here"
STATE_MARKERS = frozenset({".git", ".wrangler", KEEP_MARKER})


def protected_entry_reason(name: str, mode: int) -> str:
    """One predicate shared by cleanup and the disk guard's own bounded walker."""
    if name == KEEP_MARKER:
        return KEEP_REASON
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


#: Folders whose children are mount points: a worktree directly inside one is a whole volume,
#: and a volume that is not mounted looks exactly like a folder that was deleted.
MOUNT_CONTAINERS = frozenset({"/Volumes", "/media", "/mnt", "/run/media"})


def worktree_presence(path: str) -> str:
    """`present`, `absent` or `unknown` for a registered worktree's directory.

    `absent` is claimed only when the parent directory can be listed and lacks the entry. An
    unplugged drive, a renamed parent folder, a permission error or an unmounted volume all
    make `os.path.isdir` false while the worktree and its commits may still exist; those are
    `unknown`, and nothing that would lose the registration or its HEAD may follow from them.
    """
    try:
        os.lstat(path)
        return "present"
    except FileNotFoundError:
        pass
    except OSError:
        return "unknown"
    parent, name = os.path.split(os.path.normpath(path))
    if not parent or not name or not os.path.isabs(path):
        return "unknown"
    try:
        real_parent = os.path.realpath(parent)
        if real_parent in MOUNT_CONTAINERS or os.path.dirname(real_parent) in MOUNT_CONTAINERS:
            return "unknown"
        if not stat.S_ISDIR(os.lstat(real_parent).st_mode):
            return "unknown"
        if name in os.listdir(real_parent):
            return "unknown"                  # listed but not stat-able: changing or unreadable
    except OSError:
        return "unknown"                      # a missing or unreadable ancestor
    return "absent"
