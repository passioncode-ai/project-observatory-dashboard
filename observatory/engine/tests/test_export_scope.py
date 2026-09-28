#!/usr/bin/env python3
"""What an erasure actually reaches — measured per surface, not asserted once.

`store/retention.py` opens with "an erasure with no audit trail is
indistinguishable from a bug", and the bytes were hardened: `secure_delete`
plus a WAL checkpoint plus a VACUUM, so no freed page keeps the text of an index
copy. Both are true. Together they read as ERASURE, and that word is broader
than what happens. Measured one surface at a time:

    canon (the `ledger` table)      KEPT   — deliberately; the row IS the trail
    `ledger.live()`, the read path  gone
    the lexical index               gone
    the vector index                gone
    the `ledger.jsonl` export       KEPT   — and that file is COMMITTED

So a tombstone makes a revision unREADable, not unRECOVERABLE. The export
mirrors canon, the export is in git, and git keeps it for ever. That is a
deliberate choice — revisions are immutable, so nothing here may rewrite one —
but it was a choice nobody had written down, and the file a person would reach
for said nothing about it. The export's own header now does, and this suite is
what keeps the sentence there.

**And measuring the surfaces found a real gap, which is the other half of this
file.** `ledger.tombstone` marked the CURRENT revision only, while every reader
is record-wide — `ledger.live()`, `survey.search`'s hydration, `review.py` and
`build_findings.py` all `LEFT JOIN tombstones ON t.memory_id = l.memory_id`. So
on a two-revision record:

    purge receipt : {"status": "purged", "removed": 1, "tombstoned_rows_remaining": 0}
    search_notes  : [(1, 'ALPHA the first text …')]      <- still there
    FTS MATCH     : 1 row

Nothing leaked to a caller, because hydration drops the record by `memory_id`.
The text simply stayed in the file the scrub exists to clean, no page was freed,
and the VACUUM had nothing to zero — under a receipt that said `purged`.
Single-revision records, which is nearly all of them, made the two units
coincide and hid it.

The unit is now the record: one tombstone row per revision, the purge's
per-revision loop therefore complete, and a SECOND residue count that proves it
rather than assuming it.
"""
from __future__ import annotations
import json, os, pathlib, sqlite3, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir                                                # noqa: E402

FAILURES: list[str] = []
NEEDLE = "QQZX-scope-canary-71b4e0"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def fixture(revisions: int = 1) -> tuple[sqlite3.Connection, str, list[str]]:
    """A store with one record, `revisions` deep, and the index row the indexer
    would have written for each — plus nothing else, so every count is legible.

    The FTS rows are inserted directly. The indexer embeds and writes in one
    step, and embedding costs money against a live key; what it writes into
    `search_notes` is `statement` + `why`, which is what these rows hold.
    """
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-scope-"))
    os.environ["OBSERVATORY_DB"] = str(d / "observatory.db")
    os.environ["OBSERVATORY_SCRATCH"] = str(d / "scratch")
    (d / "scratch").mkdir()
    import importlib
    import paths
    importlib.reload(paths)
    from store import db as store_db
    importlib.reload(store_db)
    from store import ledger as L
    importlib.reload(L)
    conn = store_db.connect()
    texts = [f"{NEEDLE} revision {i + 1} of the statement" for i in range(revisions)]
    r = L.append(conn, owner="agent:test", statement=texts[0], why="the first why",
                 project_id="project:x", kind="observation")
    mid, rev = r["memoryId"], r["revision"]
    for text in texts[1:]:
        r = L.append(conn, owner="agent:test", memory_id=mid, expected_revision=rev,
                     statement=text, why="a later why", project_id="project:x",
                     kind="observation")
        rev = r["revision"]
    for i, text in enumerate(texts, start=1):
        conn.execute("INSERT INTO search_notes (memory_id, revision, statement, why)"
                     " VALUES (?,?,?,?)", (mid, i, text, "the why"))
    conn.commit()
    return conn, mid, texts


# ─────────── the four surfaces, one at a time ──────────────────────────

def test_each_surface_is_measured_separately() -> None:
    import importlib
    conn, mid, texts = fixture()
    from store import ledger as L
    from store import retention
    importlib.reload(retention)
    import survey
    importlib.reload(survey)
    import tools.export_ledger as ex                              # noqa: PLC0415
    importlib.reload(ex)

    L.tombstone(conn, mid, reason="scope probe", approved_by="operator")
    retention.purge_projections(conn)
    retention.scrub(conn)

    canon = conn.execute("SELECT statement FROM ledger WHERE memory_id = ?",
                         (mid,)).fetchall()
    check("canon KEEPS the statement", any(NEEDLE in (r[0] or "") for r in canon),
          "the ledger row is the audit trail; retention never DELETEs it")
    check("`live()` no longer returns the record",
          not any(r["memory_id"] == mid for r in L.live(conn)),
          "the read path must not serve an erased record")
    r = survey.search(NEEDLE, limit=5)
    check("search returns nothing for it", r["count"] == 0,
          f"{r['count']} hit(s): {[h.get('memoryId') for h in r['results']]}")
    left = conn.execute("SELECT count(*) FROM search_notes WHERE search_notes MATCH ?",
                        (f'"{NEEDLE}"',)).fetchone()[0]
    check("and the lexical index itself no longer matches it", left == 0,
          f"{left} row(s) — filtered at hydration but still in the file")

    dump = ex.render(ex.rows(conn))
    check("the EXPORT keeps the text", NEEDLE in dump,
          "the export mirrors canon, so it carries what canon carries")
    kinds = [json.loads(line).get("_kind") for line in dump.splitlines()[1:]]
    check("with a tombstone row beside it", "tombstone" in kinds, str(set(kinds)))
    head = json.loads(dump.splitlines()[0])
    check("and its header says so, because the file is committed",
          "TOMBSTONED REVISION" in (head.get("_tombstoned_text_is_here") or ""),
          str(sorted(head)))


def test_the_committed_export_carries_the_sentence() -> None:
    """Not the renderer's constant — the file the export tool writes, which is
    what a person opens and what gets committed. Written into a registry of its
    own so the workspace's registry is not touched."""
    reg = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-scope-export-")) / "registry"
    p = subprocess.run([sys.executable, "tools/export_ledger.py"], cwd=ROOT,
                       env=dict(os.environ, OBSERVATORY_REGISTRY=str(reg)),
                       capture_output=True, text=True, timeout=300)
    f = reg / "ledger.jsonl"
    if not f.is_file():
        check("the export exists to be checked", False,
              f"{f} is missing: {(p.stdout + p.stderr)[-200:]}")
        return
    head = json.loads(f.read_text(encoding="utf-8").splitlines()[0])
    s = head.get("_tombstoned_text_is_here") or ""
    check("the written export declares what it holds", "TOMBSTONED REVISION" in s,
          str(sorted(head)))
    check("it names the surfaces that DO forget", "live()" in s and "index" in s, s[:120])
    check("and it says the project has no redaction mechanism",
          "unrecoverable" in s and "does not have one" in s, s[-140:])


def test_the_documents_state_the_same_scope() -> None:
    ret = (ROOT / "store/retention.py").read_text(encoding="utf-8")
    check("retention.py distinguishes unreadable from unrecoverable",
          "unREADable, not unRECOVERABLE" in ret or "unreadable" in ret,
          "the module whose docstring a reader reaches for first")
    docs = (ROOT / "tools/check_docs.py").read_text(encoding="utf-8")
    check("and a doc rule keeps the sentence there",
          "TOMBSTONED REVISION" in docs,
          "a documentary fix with nothing checking it is next week's drift")


# ─────────── the unit of an erasure is the RECORD ──────────────────────

def test_every_revision_is_tombstoned_and_purged() -> None:
    import importlib
    conn, mid, texts = fixture(revisions=3)
    from store import ledger as L
    from store import retention
    importlib.reload(retention)

    before = conn.execute("SELECT count(*) FROM search_notes WHERE memory_id = ?",
                          (mid,)).fetchone()[0]
    check("the fixture indexed every revision", before == 3, str(before))
    t = L.tombstone(conn, mid, reason="scope probe", approved_by="operator")
    check("the trail names every revision", t.get("revisions") == [1, 2, 3],
          str(t.get("revisions")))
    check("and still reports the current one for its callers", t["revision"] == 3,
          str(t["revision"]))
    rows = conn.execute("SELECT count(*) FROM tombstones WHERE memory_id = ?",
                        (mid,)).fetchone()[0]
    check("one tombstone row per revision", rows == 3, str(rows))

    rec = retention.purge_projections(conn)["search_notes"]
    check("the purge removed all three index rows", rec["removed"] == 3, json.dumps(rec))
    check("nothing tombstoned is left, per revision",
          rec["tombstoned_rows_remaining"] == 0, json.dumps(rec))
    check("nor per record — the count that would have caught this",
          rec["tombstoned_records_remaining"] == 0, json.dumps(rec))
    check("so the receipt may say purged", rec["status"] == "purged", json.dumps(rec))
    for i, text in enumerate(texts, start=1):
        hit = conn.execute("SELECT count(*) FROM search_notes WHERE statement = ?",
                           (text,)).fetchone()[0]
        check(f"revision {i}'s text is out of the index", hit == 0, f"{hit} row(s)")


def test_a_replanted_row_makes_the_receipt_say_incomplete() -> None:
    """The green above is worth nothing until it has been watched failing. This
    puts one revision's index row back — a rebuild against a stale checkpoint
    does exactly that — and demands INCOMPLETE, not `purged`."""
    import importlib
    conn, mid, texts = fixture(revisions=2)
    from store import ledger as L
    from store import retention
    importlib.reload(retention)

    L.tombstone(conn, mid, reason="scope probe", approved_by="operator")
    retention.purge_projections(conn)
    conn.execute("INSERT INTO search_notes (memory_id, revision, statement, why)"
                 " VALUES (?,?,?,?)", (mid, 1, texts[0], "replanted"))
    conn.commit()
    rec = retention.purge_projections(conn)["search_notes"]
    check("the replanted row is removed by the next pass", rec["removed"] == 1,
          json.dumps(rec))

    # And the harder case: a row the per-revision delete cannot reach, because
    # its revision is not the one in `tombstones`. This is precisely the state
    # the old code produced, so it is the state the new count must refuse.
    conn.execute("DELETE FROM tombstones WHERE memory_id = ? AND revision = 1", (mid,))
    conn.execute("INSERT INTO search_notes (memory_id, revision, statement, why)"
                 " VALUES (?,?,?,?)", (mid, 1, texts[0], "replanted"))
    conn.commit()
    rec = retention.purge_projections(conn)["search_notes"]
    check("a row of an erased RECORD is refused even when its revision is not tombstoned",
          rec["status"] == "INCOMPLETE", json.dumps(rec))
    check("and the record-wide count is what says so",
          rec["tombstoned_records_remaining"] == 1, json.dumps(rec))


def test_the_indexer_refuses_an_erased_records_older_revision() -> None:
    """`retention.py` exempts `outbox` from the purge on the ground that "the
    indexer already refuses a tombstoned revision, so a pending row cannot
    re-index erased text". The join was per revision, so a pending row for an
    older revision could — which made that exemption's stated reason false."""
    import importlib
    conn, mid, texts = fixture(revisions=2)
    from store import ledger as L
    from store import indexer
    importlib.reload(indexer)
    L.tombstone(conn, mid, reason="scope probe", approved_by="operator")
    for rev in (1, 2):
        check(f"revision {rev} of an erased record is not indexable",
              indexer.indexable(conn, mid, rev) is None,
              "an unconsumed outbox row would have put the text back")


#: file -> what it would do with a tombstoned record if it did not exclude one.
#: Every READ path already joined tombstones on `memory_id`; these are the paths
#: that read canon in order to WRITE, and the guard in `_commit_revision` turns
#: each of them into a raise rather than a silent loss. A raise is right, and a
#: query that never selects the row is better than a refusal reported once a run
#: for ever.
WRITE_PATHS = {
    "tools/corroborate.py":
        "an erased record would return to the promotion queue on every run, be "
        "refused by the ledger, and be reported as a finding for ever",
    "tools/record_turn.py":
        "the Stop hook would raise on every turn of the session that owned the "
        "record, and report `recorded: false` instead of starting a fresh one",
    "agent/observe.py":
        "the correction would raise inside the agent's write loop",
}


def test_every_write_path_agrees_on_the_unit() -> None:
    import check_paths
    for f, why in WRITE_PATHS.items():
        src = check_paths.prose_removed((ROOT / f).read_text(encoding="utf-8"))
        ok = ("tombstones" in src and
              ("NOT IN (SELECT memory_id FROM tombstones)" in src
               or "t.memory_id IS NULL" in src))
        check(f"{f} does not read an erased record back", ok, why)


def test_one_writer_owns_the_trail() -> None:
    """Two writers of one audit trail with different units is how the record-wide
    rule would apply to the MCP door and not to the scheduled one."""
    import check_paths
    src = check_paths.prose_removed((ROOT / "store/retention.py").read_text(encoding="utf-8"))
    check("retention no longer writes the trail itself",
          "INSERT OR REPLACE INTO tombstones" not in src,
          "it inserted one row per candidate, bypassing ledger.tombstone")
    check("it calls the ledger instead", "ledger.tombstone(" in src, "")
    check("and reports records and revisions separately",
          "records=records" in src and "tombstoned=tombstoned" in src,
          "one number for two units reads as whichever the reader assumes")


if __name__ == "__main__":
    print("the scope of an erasure — four surfaces, and the unit it applies to\n")
    for fn in (test_each_surface_is_measured_separately,
               test_the_committed_export_carries_the_sentence,
               test_the_documents_state_the_same_scope,
               test_every_revision_is_tombstoned_and_purged,
               test_a_replanted_row_makes_the_receipt_say_incomplete,
               test_the_indexer_refuses_an_erased_records_older_revision,
               test_every_write_path_agrees_on_the_unit,
               test_one_writer_owns_the_trail):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32man erasure reaches every read and every revision, and the export "
          "says what it keeps\033[0m")
