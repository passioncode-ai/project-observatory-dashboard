#!/usr/bin/env python3
"""Which turns the companion could not record, kept where the next turn cannot take them.

`store/raw/record-turn.json` is a SLOT: one document, describing the last turn
the companion handled. Every Stop hook of every watched project on this machine
writes it, and forty-nine Claude processes were running when that was counted
. So a fault written at 10:04 lasts until any of them ends a turn —
including a turn that succeeded, in a project the fault had nothing to do with.

The board is rebuilt every 1800 seconds. `companion.not_recording` therefore
fired only for a fault that happened to be the newest turn on the whole machine
at the moment the tick ran. The incident that produced that rule survived
because it was PERMANENT — roughly seventy-two consecutive turns of one session
raised the same `IllegalTransition` — and the class this machine
actually suffers is the opposite: ten transient database errors in a single day,
42 `No space left` inside one tick.

**Three properties, and they are the whole design.**

1. **Pure append.** One line through the journal writer (`journal_writer.append`, one `write`). No read-modify-write:
   `store_faults.py` already argued this for the same hazard — reading the file
   back to trim it "is both a second chance to fail and a way to lose a
   concurrent writer's line". A carry-forward inside the slot would have been
   smaller code and would have lost exactly the fault it kept: session A reads
   no-fault, session B writes a fault, session A writes its document over it.

2. **It cannot raise.** `append()` returns the row or `None`. This runs inside
   an `except` block in a hook that must not fail the session it observes.

3. **The reader caps and says so.** A systemic fault is 49 sessions times every
   turn, so the tail is bounded here and the count reaching the board is a
   floor when the bound is hit. At roughly 200 bytes a line, the worst
   incident measured on this machine — 72 turns, one session — is 15 KB; the
   same fault across every session at once is under a megabyte a day. When that
   becomes a growth problem the purge belongs in `store/retention.py` beside
   every other one, not in this writer.

**An unreadable line is COUNTED, not skipped.** `store_faults.recent()` steps
over a torn line silently, and that is defensible where it lives: those faults
are written one at a time, by one process, on a path that is already failing.
Here the writers are concurrent by design, so a line nobody can parse is a
plausible outcome rather than a curiosity — and a count that quietly omits it
understates the exact thing it exists to report.
"""
from __future__ import annotations
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import paths                                                          # noqa: E402

#: One JSON document per line, appended. Under `store/raw/`, which is gitignored
#: and written by runs — and by EVERY session on the machine, which is why
#: `observatory.py` has to declare it a foreign write to the gate.
LOG = paths.SCRATCH / "companion-faults.jsonl"

#: How many of the most recent lines a reader consults. The newest are kept,
#: because a diagnosis reads forward from the last thing that worked.
READ_TAIL = 200

#: Fields a row carries. Written out rather than passed through, so a caller
#: cannot quietly grow the file with a payload the readers do not expect.
FIELDS = ("at", "session", "cwd", "reason", "pid")


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append(row: dict) -> dict | None:
    """Append one lost turn. Returns the row written, or None if it could not be.

    Never raises: the caller is a Stop hook's recorder, already inside its own
    exception handler.
    """
    try:
        doc = {k: row.get(k) for k in FIELDS if row.get(k) is not None}
        doc.setdefault("at", _iso())
        doc.setdefault("pid", os.getpid())
        import journal_writer
        journal_writer.append(LOG, [doc])
        return doc
    except Exception:                                                 # noqa: BLE001
        # DELIBERATELY BARE, for the same reason `store_faults.record` is: every
        # branch above fails on a full disk or a missing store directory, which
        # are conditions this file exists to record rather than to die of.
        return None


def _tail(lines: list[str]) -> list[str]:
    return [l for l in lines if l.strip()][-READ_TAIL:]


def parse(lines: list[str]) -> list[dict]:
    """The readable rows of the tail, oldest first."""
    out: list[dict] = []
    for line in _tail(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def unreadable(lines: list[str]) -> int:
    """How many lines of the tail could not be read — a fact, not a gap."""
    n = 0
    for line in _tail(lines):
        try:
            row = json.loads(line)
        except ValueError:
            n += 1
            continue
        if not isinstance(row, dict):
            n += 1
    return n


def read(days: float = 7.0) -> tuple[list[dict], int]:
    """Lost turns inside the window, newest last, plus the unreadable count.

    A row whose stamp will not parse is KEPT: the turn was lost, and dropping it
    because of its timestamp would make the count understate the thing counted.
    """
    try:
        lines = LOG.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return [], 0
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    out: list[dict] = []
    for row in parse(lines):
        try:
            when = datetime.strptime(str(row.get("at") or ""), "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            out.append(row)
            continue
        if when.replace(tzinfo=timezone.utc).timestamp() >= cutoff:
            out.append(row)
    return out, unreadable(lines)


def main(argv: list[str]) -> int:
    """`companion_faults.py [days]` — what the operator reads by hand."""
    days = float(argv[1]) if len(argv) > 1 else 7.0
    rows, bad = read(days)
    if not rows and not bad:
        print(f"no lost turns in the last {days:g} days ({LOG})")
        return 0
    for r in rows:
        print(f"{r.get('at', '?'):22} {str(r.get('session') or '?')[:8]:9} "
              f"{r.get('cwd') or '?'}\n{' ' * 32}{r.get('reason') or '?'}")
    print(f"\n{len(rows)} lost turn(s)"
          + (f", and {bad} line(s) could not be read" if bad else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
