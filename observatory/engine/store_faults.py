#!/usr/bin/env python3
"""What the machine looked like when the store failed.

Four store failures in three days left nothing to diagnose with:

    disk I/O error                          an agent's write
    file is not a database                  a multi-step job
    database disk image is malformed        an agent's write
    disk I/O error                          an agent's write

`tools/check_store.py` answers `integrity_check` = `ok` every time afterwards, so
the only surviving trace is a message in `store/logs/tick.log`. Free space, WAL
size, how many processes held the file — none of it is recorded, and all of it is
gone by the time anybody looks. Every diagnosis is therefore a guess, and one of
those guesses was handed to this repository's operator as a cause on the strength
of a single correlation.

**Three properties, and the module is small because they are the whole design.**

1. **It does not depend on the store.** Nothing here opens the database or
   imports `store_db`: an instrument that needs the failing component cannot run
   at the moment it is needed. `paths` (the file locations) and `journal_writer`
   (the line, OBS-614) are the only local imports.

2. **It cannot raise.** `record()` returns the row it wrote, or `None` when it
   could not write — never an exception. A recorder that raises inside an
   `except` block replaces a diagnosable failure with an undiagnosable one, and
   the traceback the operator then reads is the recorder's, not the fault's.

3. **It does not change behaviour.** Callers record and then RE-RAISE. Three tick
   steps (`retention`, `corroborate`, `notify_findings`) still carry no sqlite
   guard, and that is deliberate: `file is not a database` means the file really
   is broken, so a step that swallows it and carries on keeps a corrupt store in
   service. A stopped step is already reported by `tick.step_failed`.

**The sqlite CODE, not only the message.** `sqlite_errorname` is set by the
sqlite3 module on the exception it raises (never on one constructed by hand), and
it is what separates `SQLITE_IOERR` from `SQLITE_CORRUPT` from `SQLITE_NOTADB` —
where the incidents above are five different English strings for what may be
three different causes.

**Pure append, no read-modify-write.** One line, one append through the
journal writer (`journal_writer.append`, one `write`). Trimming would mean reading the file back during a failure, which is
both a second chance to fail and a way to lose a concurrent writer's line. The
READER caps instead, and says so. At roughly 400 bytes a fault and four faults in
three days, the file is not a growth problem; when it becomes one, the cap
belongs in `tools/retention.py` beside every other purge.
"""
from __future__ import annotations
import json, os, pathlib, shutil, subprocess, sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import paths                                                                      

#: One JSON document per line, appended. Under `store/raw/`, which is gitignored
#: and written by runs — so the gate's purity verdict has to know the tick writes
#: it, the same way it knows about `integrity.json`.
LOG = paths.SCRATCH / "store-faults.jsonl"

#: How many of the most recent lines a reader consults. Named so the bound is
#: findable, and `recent()`'s callers are told when it was reached rather than
#: being handed a silently shortened history.
READ_TAIL = 200

#: `lsof` is asked with a timeout, because a diagnostic that hangs during a
#: failure is a worse outcome than one that reports "could not ask".
HOLDER_TIMEOUT_S = 2.0


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _size(p: pathlib.Path) -> int | None:
    try:
        return p.stat().st_size
    except OSError:
        return None


def _holders(db: pathlib.Path) -> tuple[int | None, str]:
    """How many processes hold the database open, or None with the reason.

    THREE OUTCOMES, and the third is why this returns a pair: "nobody held it"
    and "nobody could be asked" are different facts, and a zero standing in for
    both would make every concurrency hypothesis untestable.
    """
    try:
        p = subprocess.run(["lsof", "-t", str(db)], capture_output=True, text=True,
                           timeout=HOLDER_TIMEOUT_S)
    except FileNotFoundError:
        return None, "lsof is not installed on this machine"
    except subprocess.TimeoutExpired:
        return None, f"lsof did not answer within {HOLDER_TIMEOUT_S:.0f}s"
    except OSError as exc:
        return None, f"lsof could not run: {type(exc).__name__}"
    # rc 1 with empty output is lsof's ordinary "no process has it open".
    return len([x for x in p.stdout.split() if x.strip()]), ""


def record(op: str, exc: BaseException | None = None, *, detail: str = "") -> dict | None:
    """Append one fault record. Returns it, or None if nothing could be written.

    `op` is the CALLER'S own word for what it was doing — `index`, `agent`,
    `retention` — because the remedy is addressed by operation, not by exception
    class. `detail` carries a fault with no exception object, such as a store
    that did not open at all.
    """
    try:
        db = paths.DB
        holders, holders_why = _holders(db)
        try:
            usage = shutil.disk_usage(db.parent)
            free_bytes, total_bytes = usage.free, usage.total
        except OSError:
            free_bytes, total_bytes = None, None
        row = {
            "at": _iso(),
            "op": str(op)[:60],
            "error": (f"{type(exc).__name__}: {exc}" if exc is not None
                      else str(detail))[:300],
            # None for a non-sqlite fault, and explicitly so: an absent code is
            # a fact about the fault, not a gap in the record.
            "sqlite_errorname": getattr(exc, "sqlite_errorname", None),
            "sqlite_errorcode": getattr(exc, "sqlite_errorcode", None),
            "db_bytes": _size(db),
            "wal_bytes": _size(db.with_name(db.name + "-wal")),
            "shm_bytes": _size(db.with_name(db.name + "-shm")),
            "free_bytes": free_bytes,
            "total_bytes": total_bytes,
            "holders": holders,
            "pid": os.getpid(),
        }
        if holders is None and holders_why:
            row["holders_why"] = holders_why
        if detail and exc is not None:
            row["detail"] = str(detail)[:200]
        import journal_writer
        journal_writer.append(LOG, [row])
        return row
    except Exception:                                                               
        # DELIBERATELY BARE. Every branch above can fail on a machine whose disk
        # is full or whose store directory has gone — the exact conditions this
        # module exists for. Returning None loses the record; raising would lose
        # the fault it was recording.
        return None


def recent(days: float = 7.0) -> list[dict]:
    """Faults inside the window, newest last. Bounded by READ_TAIL.

    A line that does not parse is skipped rather than fatal: half a line is what
    a crash mid-write leaves, and one bad line must not hide the rest.
    """
    try:
        lines = LOG.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    out: list[dict] = []
    for line in lines[-READ_TAIL:]:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        stamp = str(row.get("at") or "")
        try:
            when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            # An unreadable timestamp is KEPT, not dropped: the fault happened,
            # and discarding it because its stamp is unparseable would make the
            # count understate the very thing being counted.
            out.append(row)
            continue
        if when.timestamp() >= cutoff:
            out.append(row)
    return out


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="the store's fault history")
    ap.add_argument("--days", type=float, default=7.0)
    args = ap.parse_args(argv)
    rows = recent(args.days)
    if not rows:
        print(f"no store fault recorded in the last {args.days:.0f} day(s) — "
              f"the log begins when the instrument was installed, and the four "
              f"incidents that preceded it live in store/logs/tick.log")
        return 0
    print(f"{len(rows)} store fault(s) in the last {args.days:.0f} day(s)"
          + (f" (the last {READ_TAIL} lines were consulted)"
             if len(rows) >= READ_TAIL else ""))
    for r in rows:
        free = r.get("free_bytes")
        print(f"  {r.get('at')}  {r.get('op'):10} {r.get('sqlite_errorname') or '-':16}"
              f" free {free / 2**30:.2f} GiB" if isinstance(free, int) else
              f"  {r.get('at')}  {r.get('op')}  free unknown")
        print(f"      {r.get('error', '')[:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
