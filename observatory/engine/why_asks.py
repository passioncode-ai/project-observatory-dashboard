#!/usr/bin/env python3
"""The ask for a `why`: journalled once per record, measured, and checked for reach.

The companion's Stop hook records the FACTS of a turn without the agent's help;
the only thing left to ask the agent for is the `why`. Measured over fourteen days
of one machine's transcripts, the old ask ran 3,593 times — once per changed turn,
as a Stop `systemMessage` — for 34 answers: at most 0.95 %. This module holds the
three things that change that:

1. **Once per record.** `ask_once` appends `{at, memoryId, session, project}` to
   `store/raw/why-asks.jsonl` the first time a record is asked about and reports
   every later turn of the same record as already asked. A session is one record
   revised per turn (`tools/record_turn.py`), so a session is asked once.
2. **Measured on what was DELIVERED.** The ask is journalled at Stop, but it reaches
   the model only with the session's next prompt — and a headless session that runs
   one prompt never has a next one. So the plugin's `prompt-why.py` appends a
   `{"event": "delivered"}` row when it hands the ask over, and the rate is
   answered ÷ delivered: an ask still pending, or never deliverable, is never counted
   as unanswered. An ask is ANSWERED when its record later carries a `why`.
   `measure` gives `{asked, delivered, pending, answered, rate, window}`;
   `tools/build_findings.py` raises `observatory.why_unanswered` below `FLOOR_RATE`
   with at least `FLOOR_ASKS` delivered asks in `WINDOW_DAYS`.
3. **Reach.** `reached` classifies a Claude Code transcript's records: a hook's
   `additionalContext` is recorded as an `attachment` of type
   `hook_additional_context` with a `rendered` text and `renderedRole` — the text
   the model receives — while a Stop `systemMessage` is a `hook_system_message`
   with no rendering, a notice for the person. (Both shapes read from Claude Code
   transcripts on 2026-10-14; `tests/test_ask_why_reach.py` holds a synthetic
   fixture of each.)

**Rotation, so an old record is not asked twice.** The journal is read WHOLE, both
generations: at `ROTATE_BYTES` the current file is renamed to `why-asks.jsonl.1`
(under a lock, so two sessions cannot rotate at once and drop a generation), and
the previous `.1` is replaced. A record is therefore asked again only after two
rotations — some fourteen thousand asks later — not when it scrolls out of a tail.

**Failure is toward asking.** A journal that cannot be read or appended never
silences an ask: the old behaviour — ask every turn — is the safe degradation, and
`ask_once` says which happened in `journal`. Like `companion_faults.py`, the file is
appended, never rewritten, because every session on the machine writes it; and its
reader takes a bounded tail and says when the bound was reached.

Standard library only: this runs inside the Stop hook of every watched session.
"""
from __future__ import annotations
import json
import os
import pathlib
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import paths                                                          # noqa: E402

#: One JSON document per line, appended by the Stop hook's recorder in every session
#: on the machine — so `observatory.py` declares it a foreign write to the gate and
#: `workspace_upgrade.SESSION_RECEIPTS` lets it change while an update is staged.
JOURNAL = paths.SCRATCH / "why-asks.jsonl"

#: The previous generation, read beside the current one.
PREVIOUS = JOURNAL.with_name(JOURNAL.name + ".1")

#: The size at which the current journal becomes the previous generation. At about
#: 150 bytes a line, some seven thousand asks.
ROTATE_BYTES = 1 << 20

#: A ceiling on what one generation may cost a reader. A generation is rotated at
#: `ROTATE_BYTES`, so only a writer that could not rotate (a held lock, a full disk)
#: reaches it; a bound reached is reported (`floor`) rather than hidden.
READ_BYTES = 4 << 20

ASKED, DELIVERED = "asked", "delivered"

WINDOW_DAYS = 7
FLOOR_ASKS = 20
FLOOR_RATE = 0.10

#: The fields a journal row carries, written out so a caller cannot grow the file
#: with a payload no reader expects. No path, no statement, no value.
FIELDS = ("at", "memoryId", "event", "session", "project")

_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def _iso(when: datetime | None = None) -> str:
    return (when or datetime.now(timezone.utc)).strftime(_STAMP)


def _parse(stamp: str) -> datetime | None:
    try:
        return datetime.strptime(stamp, _STAMP).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def tail(path: pathlib.Path = None, limit: int = READ_BYTES) -> tuple[list[dict], int, bool]:
    """(readable rows oldest first, unreadable line count, whether the bound cut it)
    for ONE generation.

    Raises OSError when the file exists and cannot be read: the caller decides
    what an unreadable journal means, and for the hook it means "ask".
    """
    path = path or JOURNAL
    if not path.exists():
        return [], 0, False
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        cut = size > limit
        fh.seek(max(0, size - limit))
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if cut and lines:
        lines = lines[1:]                     # the first line was entered mid-way
    rows, bad = [], 0
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(row, dict) and row.get("memoryId"):
            rows.append(row)
        else:
            bad += 1
    return rows, bad, cut


def journal_rows() -> tuple[list[dict], int, bool]:
    """Both generations, previous first: (rows, unreadable lines, any bound reached)."""
    rows, bad, cut = [], 0, False
    for path in (PREVIOUS, JOURNAL):
        r, b, c = tail(path)
        rows += r
        bad += b
        cut = cut or c
    return rows, bad, cut


def _event(row: dict) -> str:
    return str(row.get("event") or ASKED)       # rows before the field were asks


def _rotate() -> None:
    """Make the current journal the previous generation once it passes ROTATE_BYTES.

    Under an exclusive lock taken without waiting, and with the size checked again
    inside it: two sessions that both see a full journal rotate it once, not twice —
    the second rename would otherwise replace the previous generation with a file a
    few lines long, and every record in it could be asked again. Appends need no
    lock: a writer that opened the file before the rename lands in `.1`, which is
    read too.
    """
    try:
        if JOURNAL.stat().st_size <= ROTATE_BYTES:
            return
        import oslocks
        with open(JOURNAL.with_name(JOURNAL.name + ".lock"), "a") as lock:
            try:
                oslocks.flock(lock.fileno(), oslocks.LOCK_EX | oslocks.LOCK_NB)
            except OSError:
                return                                # another session is rotating
            try:
                if JOURNAL.stat().st_size > ROTATE_BYTES:
                    os.replace(JOURNAL, PREVIOUS)
            finally:
                oslocks.flock(lock.fileno(), oslocks.LOCK_UN)
    except Exception:                                                  # noqa: BLE001
        return                                # a journal that grows is not a failure


def ask_once(memory_id: str, session: str = "", project: str = "") -> dict:
    """Should this turn ask for `memory_id`'s why? Journals the first ask.

    `{"ask": bool, "askedAt": str | None, "journal": "written" | "seen" |
    "unreadable" | "unwritten"}`. Never raises; every failure answers `ask: True`.
    """
    try:
        rows, _bad, _cut = journal_rows()
    except Exception:                                                  # noqa: BLE001
        return {"ask": True, "askedAt": None, "journal": "unreadable"}
    for row in reversed(rows):
        if row.get("memoryId") == memory_id:
            return {"ask": False, "askedAt": row.get("at"), "journal": "seen"}
    doc = {"at": _iso(), "memoryId": memory_id, "event": ASKED,
           "session": session or None, "project": project or None}
    doc = {k: doc[k] for k in FIELDS if doc.get(k) is not None}
    try:
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        with JOURNAL.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, ensure_ascii=False) + "\n")
    except Exception:                                                  # noqa: BLE001
        # DELIBERATELY BARE: a full disk or a missing store must cost a duplicate
        # ask later, never this one.
        return {"ask": True, "askedAt": doc["at"], "journal": "unwritten"}
    _rotate()
    return {"ask": True, "askedAt": doc["at"], "journal": "written"}


def measure(conn, days: int = WINDOW_DAYS, *, rows: list[dict] | None = None,
            now: str | None = None) -> dict:
    """`{asked, delivered, pending, answered, rate, window, unreadable, floor}`.

    `asked` counts distinct records first asked inside the last `days` (a stamp in
    the future is outside: a skewed clock must not keep a row in every window);
    `delivered` those of them the prompt hook handed to the model; `pending` the
    rest — not yet delivered, or never deliverable because the session had no
    further prompt. `answered` counts delivered records whose ledger holds a
    revision with a non-blank `why`, and `rate` is answered ÷ delivered: an ask the
    model never received cannot be unanswered. `rate` is None when nothing was
    delivered; `conn` None leaves `answered` and `rate` None.
    """
    bad, cut = 0, False
    if rows is None:
        rows, bad, cut = journal_rows()
    end = _parse(now) if now else None
    end = end or datetime.now(timezone.utc)
    since = end - timedelta(days=days)
    first: dict[str, datetime] = {}
    delivered_ids: set[str] = set()
    for row in rows:
        when = _parse(str(row.get("at") or ""))
        mid = str(row.get("memoryId") or "")
        if not mid or when is None:
            continue
        if _event(row) == DELIVERED:
            delivered_ids.add(mid)
            continue
        if mid not in first or when < first[mid]:
            first[mid] = when
    asked = sorted(m for m, w in first.items() if since <= w <= end)
    delivered = [m for m in asked if m in delivered_ids]
    window = {"days": days, "since": _iso(since)}
    base = {"asked": len(asked), "delivered": len(delivered),
            "pending": len(asked) - len(delivered), "window": window,
            "unreadable": bad, "floor": cut}
    if conn is None:
        return {**base, "answered": None, "rate": None}
    answered: set[str] = set()
    for i in range(0, len(delivered), 500):
        chunk = delivered[i:i + 500]
        marks = ",".join("?" * len(chunk))
        answered |= {r[0] for r in conn.execute(
            f"SELECT DISTINCT memory_id FROM ledger WHERE memory_id IN ({marks})"
            " AND why IS NOT NULL AND trim(why) != ''", chunk)}
    rate = round(len(answered) / len(delivered), 3) if delivered else None
    return {**base, "answered": len(answered), "rate": rate}


def measure_store(days: int = WINDOW_DAYS) -> dict:
    """`measure` against the workspace's own store, opened read-only."""
    import sqlite3
    conn = None
    try:
        if paths.DB.is_file():
            conn = sqlite3.connect(paths.sqlite_uri(paths.DB), uri=True)
        return measure(conn, days)
    finally:
        if conn is not None:
            conn.close()


def _texts(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out += _texts(item.get("content") if isinstance(item, dict) else item)
        return out
    if isinstance(value, dict):
        return _texts(value.get("content"))
    return []


def reached(lines, memory_id: str) -> dict:
    """Where a transcript carries `memory_id`: `{"model": n, "person": n}`.

    `model` counts records whose text is part of the model's input — a hook's
    `additionalContext`, recorded with a `rendered` text and a `renderedRole`.
    `person` counts notices shown to the person only — a hook's `systemMessage`
    (`hook_system_message`, never rendered). A line that does not parse is skipped:
    this reads a transcript, not a ledger.
    """
    seen = {"model": 0, "person": 0}
    for line in lines:
        if memory_id not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        att = rec.get("attachment") if isinstance(rec, dict) else None
        if not isinstance(att, dict):
            continue
        if att.get("type") == "hook_additional_context" and rec.get("renderedRole") \
                and any(memory_id in t for t in _texts(rec.get("rendered"))):
            seen["model"] += 1
        elif att.get("type") == "hook_system_message" \
                and any(memory_id in t for t in _texts(att.get("content"))):
            seen["person"] += 1
    return seen


def main(argv: list[str]) -> int:
    """`why_asks.py` — the measurement; `why_asks.py reached <transcript> <memory id>`."""
    if len(argv) >= 4 and argv[1] == "reached":
        try:
            lines = pathlib.Path(argv[2]).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            print(f"cannot read the transcript: {type(exc).__name__}", file=sys.stderr)
            return 2
        got = reached(lines, argv[3])
        print(json.dumps(got))
        return 0 if got["model"] else 1
    if len(argv) > 1 and not re.fullmatch(r"\d+", argv[1]):
        print("usage: why_asks.py [days] | why_asks.py reached <transcript.jsonl> <memory id>",
              file=sys.stderr)
        return 2
    print(json.dumps(measure_store(int(argv[1]) if len(argv) > 1 else WINDOW_DAYS), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
