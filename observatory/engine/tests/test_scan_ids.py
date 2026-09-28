#!/usr/bin/env python3
"""A scan id is unique, and one function mints every one of them.

Trap: T12 — a timestamp at second resolution is not a unique id, and `scans.id`
is a TEXT PRIMARY KEY. It was found in `compute_deltas`, fixed there and in
`scan_events` — and then **written a third time** in
`collectors/scan_sessions.py`, whose insert sits in a `try/finally` with no
`except`, so the collision would have killed the step rather than degraded it.

That is the shape this file exists to refuse. Two fixes and a re-occurrence
means the fix was applied to instances, and a trap recorded without a check for
the CLASS is recorded for a reader who has to remember.

Three properties, and the third is the one that would have caught the third
occurrence:

1. the minted id survives a frozen clock — same second, distinct ids;
2. the constraint is real, driven against a live `scans` table, both ways:
   the old shape raises `IntegrityError`, the minted shape does not;
3. every module that writes `scans` obtains its id from `store.db.scan_id`.

Property 3 reads SOURCE, which usually proves nothing about data — and the
warning does not apply here, because the subject IS the source: the defect is a
literal written by hand in a module, and the value it produces is correct on
all but one run in a hundred thousand.
"""
from __future__ import annotations
import pathlib
import re
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths                                                       # noqa: E402
from store import db as store_db                                   # noqa: E402

FAILURES: list[str] = []
FROZEN = "2026-09-08T21:10:51Z"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def scans_table() -> sqlite3.Connection:
    """The real DDL, read from the schema rather than retyped.

    A fixture that declares its own `scans` table proves its own DDL. The
    constraint under test is `id TEXT PRIMARY KEY` in `store/schema.sql`, so it
    is the schema that has to be executed — otherwise dropping the primary key
    upstream would leave this file green.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript((ROOT / "store/schema.sql").read_text(encoding="utf-8"))
    return conn


#: How many ids the real world mints inside one second: a launchd tick and a
#: manual run, which is two. Fifty is that with room to spare, and the number is
#: bounded on purpose — the suffix is six hex digits, so 16.7M values, and the
#: birthday arithmetic makes 2000 draws collide **11% of the time**. The first
#: version of this file asked for 2000 and once went red with one
#: collision: a flaky test asserting a property that holds, which teaches a
#: reader to re-run rather than to look. Fifty draws collide once in ten
#: thousand runs; the assertion below is about a population of two.
MINTED_IN_ONE_SECOND = 50


def test_a_frozen_clock_still_yields_distinct_ids() -> None:
    """Trap: T12 — the whole defect is two runs inside one second."""
    n = MINTED_IN_ONE_SECOND
    ids = {store_db.scan_id("sessions", FROZEN) for _ in range(n)}
    check(f"{n} ids minted in one second are all distinct", len(ids) == n,
          f"{n - len(ids)} collision(s) — six hex digits is 16.7M values, and "
          f"the real population inside one second is two")
    check("the second is still readable in the id",
          all(FROZEN in i for i in ids), next(iter(ids)))
    check("and so is the collector that minted it",
          all(i.startswith("sessions-") for i in ids), next(iter(ids)))


def test_the_constraint_is_real_and_the_old_shape_hits_it() -> None:
    """Both directions against the real DDL: planted defect, then the fix.

    Trap: T12

    A test that only asserts the fix works never watches the constraint reject
    anything, and a constraint nobody has seen fire is one that may not exist.
    """
    conn = scans_table()
    old = f"sessions-{FROZEN}"                       # the hand-written shape
    conn.execute("INSERT INTO scans (id, started_at, collector_version)"
                 " VALUES (?,?,?)", (old, FROZEN, "scan_sessions/1"))
    try:
        conn.execute("INSERT INTO scans (id, started_at, collector_version)"
                     " VALUES (?,?,?)", (old, FROZEN, "scan_sessions/1"))
        check("the old shape collides on a second run in the same second", False,
              "the second insert succeeded — scans.id is no longer a primary key")
    except sqlite3.IntegrityError as exc:
        check("the old shape collides on a second run in the same second",
              "scans.id" in str(exc), str(exc))

    for _ in range(2):
        conn.execute("INSERT INTO scans (id, started_at, collector_version) VALUES (?,?,?)",
                     (store_db.scan_id("sessions", FROZEN), FROZEN, "scan_sessions/1"))
    n = conn.execute("SELECT count(*) FROM scans WHERE collector_version = 'scan_sessions/1'"
                     ).fetchone()[0]
    check("two minted ids in the same second both land", n == 3, f"{n} row(s) of 3")


#: Every module holding an `INSERT INTO scans`. Enumerated rather than globbed,
#: so a fourth writer is a deliberate line here — the third one is what got
#: written by hand while two others already carried the fix.
WRITERS = ("collectors/compute_deltas.py", "collectors/scan_events.py",
           "collectors/scan_sessions.py")


def test_every_writer_of_scans_mints_through_the_one_function() -> None:
    """Trap: T12 — the class, not the three instances of it."""
    found = sorted(p.relative_to(ROOT).as_posix()
                   for p in ROOT.rglob("*.py")
                   if ".venv" not in p.parts and "tests" not in p.parts
                   and "INSERT INTO scans" in p.read_text(encoding="utf-8", errors="replace"))
    check("the enumerated writers are all of them", found == sorted(WRITERS),
          f"found {found}")
    for rel in WRITERS:
        src = (ROOT / rel).read_text(encoding="utf-8")
        # The assignment, not a mention: a docstring may name the old shape.
        mints = [ln.strip() for ln in src.splitlines()
                 if re.match(r"\s*scan_id\s*=", ln)]
        check(f"{rel} assigns scan_id exactly once", len(mints) == 1, str(mints))
        check(f"{rel} mints it through store.db.scan_id",
              all("scan_id(" in m and "db.scan_id(" in m for m in mints), str(mints))
        check(f"{rel} does not build one from a bare timestamp",
              not any(re.search(r'scan_id\s*=\s*f"[^"]*\{now\(\)\}"\s*$', m) for m in mints),
              str(mints))


if __name__ == "__main__":
    print("scan ids — one mint, no collisions\n")
    for fn in (test_a_frozen_clock_still_yields_distinct_ids,
               test_the_constraint_is_real_and_the_old_shape_hits_it,
               test_every_writer_of_scans_mints_through_the_one_function):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mscan ids ok\033[0m")
