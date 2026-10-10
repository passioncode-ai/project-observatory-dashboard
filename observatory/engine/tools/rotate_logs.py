#!/usr/bin/env python3
"""Apply the one rotation policy (log_policy.py) to every log this workspace holds.

    tools/rotate_logs.py        # the tick's `logs` step; also safe by hand

What it covers: everything in `store/logs/` (the tick's and the server's launchd
logs, the gate log, the update, cleanup, scrub, keyserver and secret-use
journals), the two fault journals in `store/raw/`, and the job runner's log.
Each file past log_policy.MAX_BYTES keeps log_policy.GENERATIONS generations, and
every one ends at mode 0600. Prints one line per rotated file; exits 0 unless the
log directory itself is unusable.
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import log_policy  # noqa: E402
import osprivacy  # noqa: E402
import paths  # noqa: E402

#: Journals outside store/logs that grow by appending, one line per event.
SCRATCH_JOURNALS = ("store-faults.jsonl", "companion-faults.jsonl")


def targets() -> tuple[pathlib.Path, tuple[pathlib.Path, ...]]:
    logs = paths.STATE / "logs"
    extra = tuple(paths.SCRATCH / name for name in SCRATCH_JOURNALS)
    extra += (paths.STATE / "jobs" / "runner.log",)
    return logs, extra


def main(argv: list[str]) -> int:
    logs, extra = targets()
    if logs.is_symlink():
        print(f"not rotated: {logs} is a symbolic link", file=sys.stderr)
        return 1
    rotated = log_policy.sweep(logs, files=tuple(p for p in extra if p.is_file()))
    # A journal outside store/logs keeps its generations beside it; sweep only
    # makes the generations of store/logs private, so do these here.
    for p in extra:
        for n in range(1, log_policy.GENERATIONS + 1):
            g = p.with_name(f"{p.name}.{n}")
            if g.is_file() and not g.is_symlink() and not osprivacy.private(g):
                osprivacy.make_private(g)
    for name in rotated:
        print(f"rotated {name}: past {log_policy.MAX_BYTES} bytes, "
              f"{log_policy.GENERATIONS} generations kept")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
