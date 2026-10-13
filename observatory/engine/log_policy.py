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


def _real_parents(path: Path) -> bool:
    """Refuse linked or unreadable ancestors, including explicit out-of-directory logs."""
    try:
        return all(stat.S_ISDIR(parent.lstat().st_mode) for parent in path.absolute().parents)
    except OSError:
        return False


def _single_file(path: Path):
    try:
        info = path.lstat()
        return info if _real_parents(path) and stat.S_ISREG(info.st_mode) and info.st_nlink == 1 else None
    except OSError:
        return None


def _private(path: Path) -> None:
    try:
        if _single_file(path) is not None and not osprivacy.private(path):
            osprivacy.make_private(path)
    except OSError:
        pass

def rotate(path: Path, *, max_bytes: int | None = None, generations: int | None = None,
           copy_truncate: bool | None = None) -> bool:
    """Rotate `path` when it is larger than `max_bytes`; True when it did.

    `copy_truncate` defaults to whether the file's name is in HELD_OPEN. A symlink
    is never followed or rotated; hardlinked files and linked ancestors also refuse."""
    path = Path(path)
    max_bytes = MAX_BYTES if max_bytes is None else max_bytes
    generations = GENERATIONS if generations is None else max(1, generations)
    if copy_truncate is None:
        copy_truncate = path.name in HELD_OPEN
    info = _single_file(path)
    if info is None:
        return False
    if info.st_size <= max_bytes:
        _private(path)
        return False
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
        if _single_file(gen(n)) is None:
            return False
    gen(generations).unlink(missing_ok=True)
    for n in range(generations - 1, 0, -1):
        if gen(n).exists():
            os.replace(gen(n), gen(n + 1))
    if copy_truncate:
        fd = osprivacy.open(path, os.O_RDWR | osprivacy.NOFOLLOW)
        with os.fdopen(fd, "r+b") as source:
            current = os.fstat(source.fileno())
            if (current.st_dev, current.st_ino, current.st_nlink) != (info.st_dev, info.st_ino, 1):
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
    for n in range(1, generations + 1):
        _private(gen(n))
    _private(path)
    return True


def sweep(directory: Path, *, max_bytes: int | None = None, generations: int | None = None,
          files: tuple[Path, ...] = ()) -> list[str]:
    """Rotate every log in `directory` (and each of `files`) past the cap; make all 0600.

    Returns the names rotated. Generations (`name.N`) are made private but never
    rotated themselves."""
    rotated: list[str] = []
    directory = Path(directory)
    try:
        safe_directory = _real_parents(directory) and stat.S_ISDIR(directory.lstat().st_mode)
    except OSError:
        safe_directory = False
    candidates: list[Path] = list(files)
    try:
        if safe_directory:
            candidates += [p for p in directory.iterdir() if p.suffix in SUFFIXES]
    except OSError:
        pass
    for p in sorted(set(candidates)):
        if rotate(p, max_bytes=max_bytes, generations=generations):
            rotated.append(p.name)
    try:
        for p in directory.iterdir() if safe_directory else ():
            stem, _, tail = p.name.rpartition(".")
            if tail.isdigit() and Path(stem).suffix in SUFFIXES:
                _private(p)
    except OSError:
        pass
    return rotated
