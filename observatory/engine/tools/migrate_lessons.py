#!/usr/bin/env python3
"""Turn the bridge's lesson NOTES into lesson records, once.

Before `observatory_lesson` existed, task-pipeline wrote each lesson with
`observatory_record` (`lessons.py emit-observatory`, the default tool): a note whose
statement starts `lesson: <classKey> — <title>`, whose why is `<trigger>; check: <check>`,
and whose evidence carries the commit and a `{"kind": "lesson", ...}` entry with the
project's pseudonymous key and the nouns. Those notes cannot be found by kind and the
corroborator cannot read them, so this converts each into `kind = "lesson"`.

**A new revision of the SAME record, by its own author.** The note's memory id and
history stay; the new revision carries the lesson body, `kind = "lesson"`, scope
`global`, and a provenance entry `migrated-from` naming the note's revision. State and
confidence are carried only for proposals. A promoted note is left for explicit review:
its generic corroboration does not establish the lesson's two-project evidence.
A note this tool cannot read as
a lesson (a missing field, a bound lesson/1 refuses, a `project:<slug>` id) is listed with
its reason and left as it is. A class the project already holds as a lesson record (the
agent wrote both) is left too: one record per (classKey, project).

    migrate_lessons.py [--dry-run]     # default: lists what would change, writes nothing
    migrate_lessons.py --apply         # writes, and prints one line per record
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from store import db as store_db                                                  # noqa: E402
from store import ledger as L                                                     # noqa: E402
from store import lessons as LS                                                   # noqa: E402

PREFIX = "lesson: "


def read_note(row) -> tuple[dict | None, str]:
    """The lesson a bridge note carries, or None and why it cannot be read as one."""
    statement, why = row["statement"] or "", row["why"] or ""
    if not statement.startswith(PREFIX) or " — " not in statement:
        return None, "the statement is not `lesson: <classKey> — <title>`"
    class_key, title = statement[len(PREFIX):].split(" — ", 1)
    if "; check: " not in why:
        return None, "the why is not `<trigger>; check: <check>`"
    trigger, check = why.split("; check: ", 1)
    try:
        evidence = json.loads(row["evidence_json"] or "[]")
    except ValueError:
        return None, "the evidence is not JSON"
    tag = next((e for e in evidence if isinstance(e, dict) and e.get("kind") == "lesson"), None)
    commit = next((e.get("sha") or e.get("ref") for e in evidence
                   if isinstance(e, dict) and e.get("kind") == "commit"), None)
    if not tag or not commit:
        return None, "the evidence carries no lesson entry or no commit"
    lesson = {"classKey": class_key.strip(), "title": title.strip(), "trigger": trigger.strip(),
              "check": check.strip(), "nouns": tag.get("nouns"),
              "origin": {"projectId": tag.get("project"), "commit": commit}}
    reasons = LS.validate(lesson)
    if reasons:
        return None, "; ".join(reasons)
    return lesson, ""


def candidates(conn) -> list:
    return conn.execute(
        "SELECT l.* FROM ledger l JOIN (SELECT memory_id, MAX(revision) r FROM ledger"
        " GROUP BY memory_id) m ON m.memory_id = l.memory_id AND m.r = l.revision"
        " LEFT JOIN tombstones t ON t.memory_id = l.memory_id"
        " WHERE t.memory_id IS NULL AND l.kind = 'note' AND l.statement LIKE 'lesson: %'"
        " AND l.state NOT IN ('rejected', 'archived', 'superseded')"
        " ORDER BY l.created_at, l.memory_id").fetchall()


def _migrate_candidate(conn, scanned, *, apply: bool) -> tuple[dict | None, str]:
    """Read current note and identity inside the caller's write transaction on apply."""
    row = L.current(conn, scanned["memory_id"])
    if row is None or row["revision"] != scanned["revision"] or row["kind"] != "note":
        return None, "the candidate changed since the scan; run migration again"
    if conn.execute("SELECT 1 FROM tombstones WHERE memory_id = ? LIMIT 1",
                    (row["memory_id"],)).fetchone() is not None:
        return None, "the candidate was erased since the scan"
    if row["state"] != "proposed" or row["owner"] == L.OPERATOR:
        return None, "operator-owned or promoted notes require explicit review; submit a new lesson proposal"
    lesson, why = read_note(row)
    if lesson is None:
        return None, why
    body = LS.body_of(lesson)
    if LS.existing(conn, body["classKey"], body["origin"]["projectId"]) is not None:
        return None, (f"{body['origin']['projectId']} already holds "
                      f"{body['classKey']} as a lesson record")
    if apply:
        L.append(conn, owner=row["owner"], memory_id=row["memory_id"],
                 expected_revision=row["revision"], kind=LS.KIND, function=LS.FUNCTION,
                 scope=LS.SCOPE, statement=LS.statement_of(body), why=LS.why_of(body),
                 state=row["state"], confidence=row["confidence"],
                 classification=row["classification"], evidence=LS.evidence_of(body),
                 body=body, provenance=json.loads(row["provenance_json"] or "[]") +
                 [{"source": "migrate_lessons",
                   "migrated-from": f"{row['memory_id']}@{row['revision']}"}])
    return {"memory_id": row["memory_id"], "classKey": body["classKey"],
            "project": body["origin"]["projectId"]}, ""


def migrate(conn, apply: bool) -> dict:
    moved, skipped = [], []
    for row in candidates(conn):
        if apply:
            # The lock precedes BOTH current-row/CAS and identity reads. Otherwise a
            # concurrent typed writer can create the same identity between read/write.
            with L.atomic_revisions(conn):
                result, why = _migrate_candidate(conn, row, apply=True)
        else:
            # A dry run is only an observation; no writer lock or ledger/index changes.
            result, why = _migrate_candidate(conn, row, apply=False)
        if result is None:
            skipped.append({"memory_id": row["memory_id"], "why": why})
        else:
            moved.append(result)
    return {"moved": moved, "skipped": skipped}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write the new revisions")
    ap.add_argument("--dry-run", action="store_true", help="the default: write nothing")
    args = ap.parse_args(argv)
    conn = store_db.connect()
    try:
        out = migrate(conn, apply=args.apply and not args.dry_run)
    finally:
        conn.close()
    verb = "migrated" if args.apply and not args.dry_run else "would migrate"
    print(f"migrate_lessons: {verb} {len(out['moved'])} lesson note(s); "
          f"{len(out['skipped'])} left as they are")
    for m in out["moved"]:
        print(f"  + {m['memory_id']}  {m['classKey']}  ({m['project']})")
    for s in out["skipped"]:
        print(f"  - {s['memory_id']}  {s['why'][:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
