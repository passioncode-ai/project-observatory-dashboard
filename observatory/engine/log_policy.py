"""One rotation policy for every log this engine writes (lifecycle LC-12).

WHY. Each writer used to decide for itself, and most decided nothing: the tick's
own log rotated at 2 MB in three generations, while `serverd.err` (written by
launchd) reached 2.1 MB and `secret-use.jsonl` grew by ~90 KB a day with no bound
at all, and one log another tool wrote was world-readable. Now one rule holds for
all of them: a file past MAX_BYTES becomes `<name>.1`, the older generations move
up one, at most GENERATIONS are kept, and every file is owner-only (0600).

TWO WAYS TO ROTATE, chosen by who holds the file open.

- **Rename** for files the engine opens, appends to and closes on every write
  (the `*.jsonl` journals, the gate log): the next write creates a fresh file.
- **Copy and truncate** for files launchd holds open as a job's stdout/stderr
  (HELD_OPEN). Renaming one would leave launchd appending to the renamed
  generation for as long as the job runs; truncating in place keeps its O_APPEND
  descriptor writing at the new end. A line written between the copy and the
  truncate can be lost — the price of rotating a file someone else holds.

Nothing here reads a log's content: rotation is by size, and the sweep never opens
what it does not rotate.
"""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import osprivacy

#: The lifecycle contract's default: 5 generations of 5 MB.
MAX_BYTES = 5 * 1024 * 1024
GENERATIONS = 5
#: What counts as a log in a swept directory.
SUFFIXES = (".log", ".err", ".out", ".jsonl")
#: Files a launchd job holds open as stdout or stderr: copied and truncated.
HELD_OPEN = frozenset({"serverd.err", "serverd.out", "tick.log", "tick.err"})


def _absolute(path) -> Path:
    """Absolute WITHOUT lexical `..` collapse: `abspath` turned `root/link/../logs` into
    `root/logs` while the operating system follows the link first."""
    path = Path(path)
    return path if path.is_absolute() else Path(os.getcwd()) / path


def _dir_state(directory: Path, root: Path | None = None) -> str:
    """`real`, `absent` or `unverified` for `directory` below the trusted `root`.

    `root` is resolved once with realpath, so links AT or ABOVE it are allowed: macOS's /tmp and
    /var, a home folder on a linked volume. Below it every component is checked with lstat: a
    link (a dangling one too) or a non-directory is `unverified`, and only a component that does
    not exist at all makes the folder `absent`. A `..` segment, or a directory outside `root`,
    is `unverified`. By default the root is the directory's own parent."""
    try:
        directory = _absolute(directory)
        root = _absolute(root) if root is not None else directory.parent
        if ".." in directory.parts or ".." in root.parts:
            return "unverified"
        rel = directory.relative_to(root)
        current = Path(os.path.realpath(root))
        try:
            if not stat.S_ISDIR(current.lstat().st_mode):
                return "unverified"
        except FileNotFoundError:
            return "absent"
        for part in rel.parts:
            current = current / part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                return "absent"
            if not stat.S_ISDIR(mode):
                return "unverified"
        return "real"
    except (OSError, ValueError):
        return "unverified"


def _real_dirs(directory: Path, root: Path | None = None) -> bool:
    """`directory` and every folder between `root` and it are real directories, not links."""
    return _dir_state(directory, root) == "real"


def _real_parents(path: Path, root: Path | None = None) -> bool:
    """Refuse linked or unreadable folders below the trusted root, explicit outside logs included."""
    path = _absolute(path)
    return _real_dirs(path.parent, root if root is not None else path.parent.parent)


def _single_file(path: Path, root: Path | None = None):
    try:
        info = path.lstat()
        return info if _real_parents(path, root) and stat.S_ISREG(info.st_mode) and info.st_nlink == 1 else None
    except OSError:
        return None


def _private(path: Path, root: Path | None = None) -> bool:
    try:
        if _single_file(path, root) is None:
            return False
        if not osprivacy.private(path):
            osprivacy.make_private(path)
        return True
    except OSError:
        return False


def rotate(path: Path, *, max_bytes: int | None = None, generations: int | None = None,
           copy_truncate: bool | None = None, _observation: dict | None = None,
           root: Path | None = None) -> bool:
    """Rotate `path` when it is larger than `max_bytes`; True when it did.

    `copy_truncate` defaults to whether the file's name is in HELD_OPEN. A symlink
    is never followed or rotated; hardlinked files and linked ancestors also refuse."""
    observation = _observation if _observation is not None else {}
    observation.update(status="refused", reason="file_unverified")
    path = Path(path)
    max_bytes = MAX_BYTES if max_bytes is None else max_bytes
    generations = GENERATIONS if generations is None else max(1, generations)
    if copy_truncate is None:
        copy_truncate = path.name in HELD_OPEN
    info = _single_file(path, root)
    if info is None:
        return False
    if info.st_size <= max_bytes:
        private = _private(path, root)
        observation.update(status="complete" if private else "failed",
                           reason=None if private else "permissions_unverified")
        return False
    observation["reason"] = "generation_unverified"
    gen = lambda n: path.with_name(f"{path.name}.{n}")                       # noqa: E731
    # Validate the whole chain before moving any generation. lexists semantics
    # matter: a broken symlink must not look like a vacant copy destination.
    for n in range(1, generations + 1):
        try:
            gen(n).lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return False
        if _single_file(gen(n), root) is None:
            return False
    observation.update(status="failed", reason="rotation_failed")
    gen(generations).unlink(missing_ok=True)
    for n in range(generations - 1, 0, -1):
        if gen(n).exists():
            os.replace(gen(n), gen(n + 1))
    if copy_truncate:
        fd = osprivacy.open(path, os.O_RDWR | osprivacy.NOFOLLOW)
        with os.fdopen(fd, "r+b") as source:
            current = os.fstat(source.fileno())
            if (current.st_dev, current.st_ino, current.st_nlink) != (info.st_dev, info.st_ino, 1):
                observation.update(status="refused", reason="identity_changed")
                return False
            # Exclusive creation cannot follow a destination link introduced after
            # preflight. Truncate only the verified source descriptor after copying.
            target = osprivacy.open(gen(1), os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                    osprivacy.NOFOLLOW, 0o600)
            with os.fdopen(target, "wb") as backup:
                shutil.copyfileobj(source, backup)
                backup.flush()
                os.fsync(backup.fileno())
            source.truncate(0)
    else:
        os.replace(path, gen(1))
    observation.update(status="complete", reason=None)
    for n in range(1, generations + 1):
        # Missing generations are expected; failures on existing files are not.
        if os.path.lexists(gen(n)) and not _private(gen(n), root):
            observation.update(status="failed", reason="permissions_unverified")
    if copy_truncate and not _private(path, root):
        observation.update(status="failed", reason="permissions_unverified")
    return True


# Only these bounded categories may enter maintenance receipts and readers.
REASONS = frozenset({"directory_unverified", "directory_read_failed", "file_unverified",
                     "generation_unverified", "identity_changed", "rotation_failed",
                     "permissions_unverified", "scope_failed", "receipt_unreadable"})


def unknown_report(reason: str = "scope_failed") -> dict:
    return {"status": "unknown", "observed_count": None, "rotated": None,
            "refused": None, "failed": None, "reasons": {reason: 1}}


def _sweep(directory: Path, *, max_bytes=None, generations=None, files=(), detailed=False, root=None):
    rotated: list[str] = []
    rotated_paths: set[Path] = set()
    directory = Path(directory)
    report = {"status": "complete", "observed_count": None, "rotated": 0,
              "refused": 0, "failed": 0, "reasons": {}}

    def reason(code):
        report["reasons"][code] = report["reasons"].get(code, 0) + 1
        if report["status"] == "complete":
            report["status"] = "partial"

    root = _absolute(root) if root is not None else _absolute(directory).parent
    # A log folder that does not exist yet (a fresh install, no hook has run) holds nothing to
    # retain: zero entries, measured. A dangling link or a `..` on the way is not absence.
    state = _dir_state(directory, root)
    absent, safe = state == "absent", state == "real"
    entries = []
    if absent:
        report["observed_count"] = 0
        report["absent"] = True
    elif not safe:
        report = unknown_report("directory_unverified")
    else:
        try:
            entries = list(directory.iterdir())
            report["observed_count"] = len(entries)
        except OSError:
            report = unknown_report("directory_read_failed")
    # Enumeration failure happened before these actions; zero attempts is known.
    # A lost whole-scope receipt instead uses unknown_report's null action counts.
    report.update(rotated=0, refused=0, failed=0)
    candidates = sorted(set(files) | {p for p in entries if p.suffix in SUFFIXES})
    for path in candidates:
        observation = {}
        try:
            # An explicit log outside the trusted root is judged against its own parent.
            inside = _absolute(path).is_relative_to(root)
            did_rotate = rotate(path, max_bytes=max_bytes, generations=generations,
                                _observation=observation, root=root if inside else None)
        except Exception:  # detailed mode isolates failures without serializing exceptions
            if not detailed:
                raise
            did_rotate = False
            observation = {"status": "failed", "reason": "rotation_failed"}
        if did_rotate:
            rotated.append(path.name)
            rotated_paths.add(path)
            report["rotated"] += 1
        if observation.get("status") in ("refused", "failed"):
            report[observation["status"]] += 1
            reason(observation["reason"])
    for path in entries:
        stem, _, tail = path.name.rpartition(".")
        if tail.isdigit() and Path(stem).suffix in SUFFIXES and path.with_name(stem) not in rotated_paths:
            if not _private(path, root):
                report["refused"] += 1
                reason("permissions_unverified")
    return rotated, report


def sweep(directory: Path, *, max_bytes: int | None = None, generations: int | None = None,
          files: tuple[Path, ...] = (), root: Path | None = None) -> list[str]:
    """Legacy list API: rotate once and return names; operation errors still raise."""
    return _sweep(directory, max_bytes=max_bytes, generations=generations, files=files, root=root)[0]


def sweep_report(directory: Path, *, max_bytes: int | None = None, generations: int | None = None,
                 files: tuple[Path, ...] = (), root: Path | None = None) -> dict:
    """One execution's bounded receipt, with no path, content or exception text.

    `observed_count` counts directory entries, including unrelated names; null
    means enumeration was not complete. A directory that does not exist is `absent`:
    complete, with zero entries. Links at or above `root` (default: the directory's
    parent) are followed; below it they refuse. Action counts are completed observations,
    not an estimate of remaining files. A failed rotation may have moved older
    generations; its effects are not claimed to be rolled back.
    """
    return _sweep(directory, max_bytes=max_bytes, generations=generations,
                  files=files, detailed=True, root=root)[1]
