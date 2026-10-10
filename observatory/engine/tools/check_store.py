#!/usr/bin/env python3
"""Ask SQLite whether the store is structurally sound, and record the answer.

**Why this exists.** A read can fail with `database disk image is malformed` and
the same pragma answer `ok` minutes later, so nothing about the store is wrong by
the time anyone looks. That transient fault is reported by the code that hits
it — it records the fault, and `events` journals the finding, so a recurrence
leaves a series.

**This checks something else, and says so rather than borrowing that
justification.** Real corruption — a store file with no SQLite header at all —
is otherwise found by a read FAILING, which is to say after it has already
broken a run and left `finding` rows pointing at the wrong cause. A periodic
check finds a damaged store before a reader does.

**The cost, measured on a store of a few tens of MiB, because the figures here
reversed under measurement:**

    PRAGMA quick_check         15 / 15 / 15 ms
    PRAGMA integrity_check    204 / 89 / 87 ms      (first run cold)

The old note read `quick_check 129 ms` against `integrity_check 97 ms` and
concluded "the cheaper pragma is not even cheaper in practice". That is the
benchmark-ordering error: whichever runs first pays for the cold cache. The cheap
pragma is six times cheaper, and the receipt records a still different number —
`took_ms: 1623` — because the tick runs this while its own collectors contend for
the file. Three numbers, three conditions, and the honest form is to name all
three rather than one.

**And the reason given for the choice was the wrong example.** It said the
difference is index CONTENT verification, "exactly the class of damage a reader
would otherwise discover by getting a wrong answer" — while the damage this store
actually recorded was `SQLITE_CORRUPT: vtable constructor failed: search_notes`
(2026-09-08T00:54:56Z, 12.09 GiB free, so not the disk-full episode). Driven
against a planted defect — a copy of the live store with `search_notes_data`
dropped — BOTH pragmas name it:

    quick_check       61 ms  ->  malformed inverted index for FTS5 table main.search_notes
    integrity_check   91 ms  ->  malformed inverted index for FTS5 table main.search_notes

So `integrity_check` is kept for what SQLite documents it to add — verifying that
each ORDINARY index agrees with its table, which `quick_check` skips — and not
for the FTS5 class, which the cheap one catches too. Ninety milliseconds on a
thirty-minute cycle for a strictly larger check is still the right trade; the
evidence for it is now the documentation plus a measurement, rather than a
reversed benchmark.

**And its cost is bounded (2026-10-10).** One tick spent 785 s here under a load
average near 400 — the same 81 MiB store checks in 4–38 s on this machine — and was
killed with nothing written, after which `scan-fs` never started. So the full
`integrity_check` runs once a day, and after any verdict that was not `ok`;
`quick_check`, which catches the FTS5 damage this store has actually had, runs in
between. The step stops itself before its deadline (`OBSERVATORY_STEP_DEADLINE`,
`step_budget`) with `sqlite3.Connection.interrupt()` and records `unmeasured`
instead of being killed silent; `full_checked_at` carries the last full pass from
receipt to receipt, so the board can say how old it is.

FIVE verdicts, because four of them are not `ok` for different reasons and a
reader acting on them does different things:

    ok           SQLite verified every page and index
    damaged      it opened and the check reported problems — the detail is
                 SQLite's own text
    unopenable   `file is not a database`; a fact about the FILE, and reporting
                 it as a failed integrity check sends a reader looking for a
                 damaged page in something that has no pages
    absent       there is no store yet, which is a fresh clone rather than a fault
    unmeasured   the step's deadline came first; nothing is known either way

    check_store.py            check and write the receipt
    check_store.py --print    also print the verdict
"""
from __future__ import annotations
import argparse, json, pathlib, sqlite3, sys, threading, time
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import atomic                                                       # noqa: E402
import paths                                                        # noqa: E402
import step_budget                                                  # noqa: E402

#: `integrity_check` and not `quick_check`. The extra work is index content
#: verification, and an index that disagrees with its table gives a reader a
#: WRONG ANSWER rather than an error — the failure mode nothing else here can
#: catch. Measured at 97 ms against 129 ms on 21.7 MiB, so the cheaper pragma is
#: not even cheaper in practice.
PRAGMA = "integrity_check"
#: The cheap pragma for the runs in between: it skips only the ordinary-index content
#: check, and it names the FTS5 damage this store has actually had (measured above).
QUICK = "quick_check"
#: Hours between full checks while every verdict is `ok`.
FULL_EVERY_HOURS = 24
#: Seconds kept before the step's deadline to write the receipt.
MARGIN_SECONDS = 5

#: How many of SQLite's lines to keep. It reports one per problem and a badly
#: damaged file produces thousands; the finding needs enough to name the shape.
MAX_LINES = 8


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def choose(previous: dict) -> str:
    """The full check once a day and after any verdict that was not `ok`; the quick one
    in between."""
    full_at = str(previous.get("full_checked_at") or "")
    if previous.get("verdict") != "ok" or not full_at:
        return PRAGMA
    try:
        when = datetime.strptime(full_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return PRAGMA
    return PRAGMA if datetime.now(timezone.utc) - when >= timedelta(hours=FULL_EVERY_HOURS) else QUICK


def inspect(db: pathlib.Path, pragma: str = PRAGMA, budget: float = float("inf")) -> dict:
    """The verdict, the reason, and what it cost to find out — within `budget` seconds."""
    if not db.is_file():
        return {"verdict": "absent", "detail": f"{db} does not exist",
                "took_ms": 0, "bytes": 0}
    size = db.stat().st_size
    if budget <= 0:
        # An interrupt sent before a statement runs is lost (SQLite clears it when none is
        # pending), so a check with no time left is not started at all.
        return {"verdict": "unmeasured", "took_ms": 0, "bytes": size,
                "detail": "not started: the step's deadline left no time for it"}
    started = time.perf_counter()
    try:
        # READ-ONLY, and that matters twice: a check must not be the thing that
        # writes to a store it suspects, and opening read-write would run any
        # pending migration first.
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return {"verdict": "unopenable", "detail": f"{type(exc).__name__}: {exc}",
                "took_ms": round((time.perf_counter() - started) * 1000, 1),
                "bytes": size}
    # A WATCHDOG, not a one-shot timer: an interrupt sent before the statement starts is
    # lost, so past the deadline it is sent again every 50 ms until the pragma returns.
    done = threading.Event()

    def watchdog() -> None:
        if done.wait(budget):
            return
        while not done.is_set():
            conn.interrupt()
            done.wait(0.05)
    timer = None
    if budget != float("inf"):
        timer = threading.Thread(target=watchdog, daemon=True)
        timer.start()
    try:
        rows = [r[0] for r in conn.execute(f"PRAGMA {pragma}").fetchall()]
    except sqlite3.OperationalError as exc:
        if "interrupted" not in str(exc):
            return {"verdict": "unopenable", "detail": f"{type(exc).__name__}: {exc}",
                    "took_ms": round((time.perf_counter() - started) * 1000, 1),
                    "bytes": size}
        took = round((time.perf_counter() - started) * 1000, 1)
        return {"verdict": "unmeasured", "took_ms": took, "bytes": size,
                "detail": f"stopped at the step's deadline after {took / 1000:.0f} s, before "
                          f"PRAGMA {pragma} finished; nothing is known either way"}
    except sqlite3.DatabaseError as exc:
        # `file is not a database` arrives HERE, not at connect: SQLite opens
        # lazily, so the header is read on the first statement.
        return {"verdict": "unopenable", "detail": f"{type(exc).__name__}: {exc}",
                "took_ms": round((time.perf_counter() - started) * 1000, 1),
                "bytes": size}
    finally:
        done.set()
        if timer is not None:
            timer.join()
        conn.close()
    took = round((time.perf_counter() - started) * 1000, 1)
    if rows == ["ok"]:
        return {"verdict": "ok", "detail": "", "took_ms": took, "bytes": size}
    kept = rows[:MAX_LINES]
    detail = "; ".join(kept)
    if len(rows) > MAX_LINES:
        detail += f" (+{len(rows) - MAX_LINES} more line(s) not shown)"
    return {"verdict": "damaged", "detail": detail, "took_ms": took, "bytes": size}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print", action="store_true", dest="show",
                    help="print the verdict as well as recording it")
    args = ap.parse_args(argv[1:])

    target = paths.SCRATCH / "integrity.json"
    try:
        previous = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    previous = previous if isinstance(previous, dict) else {}
    pragma = choose(previous)
    result = inspect(paths.DB, pragma, step_budget.remaining() - MARGIN_SECONDS)
    ran_at = now()
    full_at = (ran_at if result["verdict"] == "ok" and pragma == PRAGMA
               else previous.get("full_checked_at") if result["verdict"] in ("ok", "unmeasured") else None)
    doc = {"ran_at": ran_at, "pragma": pragma, **result,
           **({"full_checked_at": full_at} if full_at else {})}
    try:
        paths.SCRATCH.mkdir(parents=True, exist_ok=True)
        atomic.write_json(paths.SCRATCH / "integrity.json", doc)
    except Exception as exc:                                        # noqa: BLE001
        # NAMED, never fatal. A check that cannot write its receipt has still
        # learned something, and the tick must not stop because a scratch file
        # would not open.
        print(f"the integrity receipt could not be written: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)

    if result["verdict"] == "ok":
        if args.show:
            print(f"store ok — {pragma} in {result['took_ms']:.0f} ms over "
                  f"{result['bytes'] / 1024 ** 2:.1f} MiB")
        return 0
    if result["verdict"] == "unmeasured":
        if args.show:
            print(f"not measured — {result['detail']}")
        return 0
    if result["verdict"] == "absent":
        if args.show:
            print("no store yet — nothing to check, which is a fresh clone "
                  "rather than a fault")
        return 0
    # NON-ZERO, so a person running it by hand learns from the exit code too.
    # The TICK records the step and continues by its own design — a cycle that
    # writes into a suspect store makes it worse, but the collectors' scratch
    # output does not need the store to succeed.
    print(f"store {result['verdict'].upper()}: {result['detail'][:300]}",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
