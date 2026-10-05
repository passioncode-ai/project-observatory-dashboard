#!/usr/bin/env python3
"""Keep what only somebody else's store remembers.

`collectors/scan_sessions.py` can say that a project existed under the projects
root and is gone: the name matches nothing in the registry, and the paths its
work touched name a folder that is not there. Such a project can carry many
session summaries and observations while no trace of it survives on the disk,
in the archive folders, or among the registry's repositories.

**The problem is where that knowledge lives.** It is in claude-mem — a store this
system opens READ-ONLY and does not own, with its own retention and its own
reasons to prune. The registry cannot hold the fact either: it is derived from
what EXISTS, so an entry with no anchor is wiped by the next emit. So the estate
can currently SAY a project is lost and cannot KEEP it.

The ledger is the one authored, append-only record here, and this writes into it
under the rules that already govern every automated writer:

* `state='proposed'` — an automated writer may not promote its own row, and
  `tools/corroborate.py` sends any kind but `session` to `needs_person`, which
  is right: what a vanished project MEANT is not mechanically checkable.
* `project_id` is NULL, deliberately. The project is not in the registry, and
  minting an id for it would be inventing the thing this record exists to say is
  gone.
* the statement carries measured numbers only — counts, dates, the folder — and
  the `why` says why it is worth keeping at all. No guess about what happened to
  it: a fabricated cause is read as true by everything downstream.

**Idempotent, or it would add one record per tick.** The name → `memory_id`
mapping lives in `cursors` — the table for "where a collector got to", which is
the same character of state: what this writer has already written. The
alternative was matching a marker inside `evidence_json` with LIKE, and a
string search inside JSON is a key that breaks the first time the formatting
changes.

    record_lost_projects.py              write or revise, one record per project
    record_lost_projects.py --dry-run    print what would be written
"""
from __future__ import annotations
import argparse, json, pathlib, sqlite3, sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import atomic                                                       # noqa: E402
import paths                                                        # noqa: E402
from store import db as store_db                                    # noqa: E402
from store import ledger as L                                       # noqa: E402

OWNER = "agent:estate-history"
#: Not `observation` and not `session`: a fact about the estate's own PAST,
#: which is a third subject. Nothing filters reads by kind, and
#: `tools/corroborate.py` routes every kind but `session` to a person — which is
#: the correct destination for "what became of this project".
KIND = "estate-history"
#: How long the fact "this project is gone" holds before a run must confirm it again.
#: Each run's statement carries its scan date, so a daily run renews it.
FACT_DAYS = 30
#: One cursor per lost project, holding the record this tool has already written
#: for it. The prefix keeps the namespace legible beside `deltas.diffed_through`.
CURSOR_PREFIX = "estate-history.lost:"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def lost_from_sessions() -> tuple[list[dict], str | None]:
    """(the lost projects, why not) — read from the collector's own output.

    `store/raw/sessions.json` is where `scan_sessions` puts the verdict, so this
    tool needs no second reading of claude-mem and no network, and it degrades
    honestly when the collector has not run.
    """
    f = paths.SCRATCH / "sessions.json"
    if not f.is_file():
        return [], f"{f} does not exist — `project-observatory full scan-sessions` writes it"
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return [], f"{f} is unreadable: {type(exc).__name__}: {exc}"
    rows = [r for r in doc.get("unattributed") or []
            if r.get("verdict") == "estate-folder-gone" and (r.get("sessions") or 0) >= 2]
    return rows, None


def statement_for(row: dict, scanned_on: str) -> str:
    folders = ", ".join(str(paths.DATA / f) for f in row.get("folders") or [])
    return (f"{row['name']!r} was a project on this machine and its folder is gone. "
            f"claude-mem holds {row['sessions']} session summar"
            f"{'y' if row['sessions'] == 1 else 'ies'} of work under that name, and "
            f"the paths its observations record name {folders or 'no folder'} — "
            f"absent from the disk, from the archive folders and from the registry's "
            f"repositories as of {scanned_on}. What became of it is not recorded "
            f"anywhere in this estate.")


WHY = ("The only other record of this work is claude-mem, which this system opens "
       "read-only and does not own — it has its own retention and its own reasons "
       "to prune. The registry cannot hold the fact either: it is derived from what "
       "exists, so an entry with no anchor is erased by the next emit. Recorded here "
       "so the estate keeps what it has already noticed, and left `proposed` because "
       "what a vanished project meant is a judgement only its operator can make.")


def cursor_for(conn, name: str) -> str | None:
    row = conn.execute("SELECT value FROM cursors WHERE name = ?",
                       (CURSOR_PREFIX + name,)).fetchone()
    return row[0] if row else None


def remember(conn, name: str, memory_id: str) -> None:
    conn.execute("INSERT INTO cursors (name, value, updated_at) VALUES (?,?,?)"
                 " ON CONFLICT(name) DO UPDATE SET value = excluded.value,"
                 " updated_at = excluded.updated_at",
                 (CURSOR_PREFIX + name, memory_id, now()))


def report(**fields) -> None:
    try:
        atomic.write_json(paths.SCRATCH / "lost-projects.json",
                          {"ran_at": now(), **fields})
    except Exception as exc:                                        # noqa: BLE001
        print(f"the receipt could not be written: {type(exc).__name__}: {exc}",
              file=sys.stderr)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be written and write nothing")
    args = ap.parse_args(argv[1:])

    rows, why_not = lost_from_sessions()
    if why_not:
        print(f"nothing read: {why_not}", file=sys.stderr)
        report(written=0, revised=0, unchanged=0, degraded=[
            {"source": "sessions", "reason": why_not}])
        return 1
    if not rows:
        print("no lost project to record — every unattributed name still has its folder")
        report(written=0, revised=0, unchanged=0, degraded=[])
        return 0

    scanned_on = json.loads(
        (paths.SCRATCH / "sessions.json").read_text(encoding="utf-8")
    ).get("scanned_on") or now()[:10]
    conn = store_db.connect()
    written = revised = unchanged = 0
    notes: list[dict] = []
    try:
        for row in sorted(rows, key=lambda r: -r["sessions"]):
            statement = statement_for(row, scanned_on)
            mid = cursor_for(conn, row["name"])
            prior = L.current(conn, mid) if mid else None
            if prior is not None and prior["statement"] == statement:
                # Nothing moved. A revision saying exactly what the last one said
                # is the append-only equivalent of noise, and this tool runs on
                # every tick.
                unchanged += 1
                notes.append({"name": row["name"], "memoryId": mid,
                              "outcome": "unchanged"})
                continue
            if args.dry_run:
                notes.append({"name": row["name"], "memoryId": mid,
                              "outcome": "would-revise" if prior else "would-write",
                              "statement": statement})
                continue
            try:
                res = L.append(
                    conn, owner=OWNER, kind=KIND, statement=statement, why=WHY,
                    # NULL project_id, deliberately: the project is not in the
                    # registry and minting an id would invent the thing this
                    # record exists to say is gone.
                    project_id=None, function="semantic", scope="global",
                    state="proposed", confidence=0.9,
                    # A FACT SAYS UNTIL WHEN IT HOLDS (PB-137 N-014): thirty days from now,
                    # renewed by the next run that still finds the project gone.
                    valid_to=(datetime.now(timezone.utc) + timedelta(days=FACT_DAYS)
                              ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    memory_id=mid if prior else None,
                    expected_revision=prior["revision"] if prior else None,
                    provenance=[{"source": "tools/record_lost_projects", "measured": True,
                                 "reads": "store/raw/sessions.json"}],
                    evidence=[{"kind": "session-store", "source_ref": "SRC-0012",
                               "sessions": row["sessions"],
                               "folders": row.get("folders") or [],
                               "scanned_on": scanned_on}])
            except L.LedgerError as exc:
                # NAMED, and the run continues: one project's record failing must
                # not silence the rest, and a swallowed LedgerError here would be
                # the same silence the companion's own failure hid for fifteen
                # hours.
                print(f"  {row['name']}: NOT recorded — {type(exc).__name__}: {exc}",
                      file=sys.stderr)
                notes.append({"name": row["name"], "outcome": "refused",
                              "reason": f"{type(exc).__name__}: {exc}"})
                continue
            with conn:
                remember(conn, row["name"], res["memoryId"])
            if prior:
                revised += 1
            else:
                written += 1
            notes.append({"name": row["name"], "memoryId": res["memoryId"],
                          "revision": res["revision"],
                          "outcome": "revised" if prior else "written"})
            print(f"  {row['name']}: {res['memoryId']}@{res['revision']} "
                  f"({'revised' if prior else 'recorded'}, state proposed)")
    finally:
        conn.close()

    if args.dry_run:
        for n in notes:
            print(f"  [dry-run] {n['name']}: {n['outcome']}")
            if n.get("statement"):
                print(f"      {n['statement'][:150]}")
        return 0
    print(f"{written} recorded, {revised} revised, {unchanged} unchanged")
    report(written=written, revised=revised, unchanged=unchanged,
           projects=notes, degraded=[])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
