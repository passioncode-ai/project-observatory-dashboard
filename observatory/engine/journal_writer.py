"""One append-only journal writer for every `*.jsonl` this engine keeps (OBS-614).

WHY. About twenty-five modules each kept a helper that appended a line to a journal, and
each decided for itself: some opened through a symbolic link, some left the file readable
by others until a later chmod, some wrote one record per call and some several writes per
call. One writer now holds the rule:

- one line per record, every record of a call in ONE write on an `O_APPEND` descriptor,
  so two writers never interleave inside a call;
- the folder is created owner-only (0700), the file is created owner-only (0600) and an
  existing loose file is tightened;
- never through a symbolic link (`osprivacy.NOFOLLOW`), and only into a regular file;
- rotation, when the caller asks, is `log_policy.rotate`: the one rotation rule. A journal
  that is read back as a record (decisions, an undo) does not ask; `log_policy.sweep` and
  the maintenance pass bound the rest.

What a failed write means is the caller's: this writer raises `OSError` (and `ValueError`
for a record that would span two lines), and the caller swallows it, names it in
`degraded`, or lets it stop the act, as it did before.
"""
from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path
from typing import Any, Iterable

import osprivacy


def append(path, records: Iterable[Any], *, rotate: bool = False, fsync: bool = False,
           sort_keys: bool = False, ensure_ascii: bool = False) -> None:
    """Each record as one JSON line (`json.dumps` with the caller's options), appended to `path`."""
    append_lines(path, [json.dumps(r, sort_keys=sort_keys, ensure_ascii=ensure_ascii) for r in records],
                 rotate=rotate, fsync=fsync)


def append_lines(path, lines: Iterable[str], *, rotate: bool = False, fsync: bool = False) -> None:
    """Each text as one line appended to `path`. A text holding a line break is refused before anything
    is written: one record is one line."""
    lines = list(lines)
    if not lines:
        return
    if any("\n" in line or "\r" in line for line in lines):
        raise ValueError("a journal record is one line: a line break inside one is refused")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if rotate:
        import log_policy
        log_policy.rotate(path)
    fd = osprivacy.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | osprivacy.NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "a journal is a regular file", str(path))
        if not osprivacy.private(path):
            osprivacy.make_private(path)
        data = "".join(line + "\n" for line in lines).encode("utf-8")
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
        if fsync:
            os.fsync(fd)
    finally:
        os.close(fd)
