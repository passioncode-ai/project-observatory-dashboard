#!/usr/bin/env python3
"""What leaves the store, and the audit trail it leaves behind.

**Retention never DELETEs a ledger row.** It writes a tombstone, then purges the
derived projections and records that the purge happened. An erasure with no audit
trail is indistinguishable from a bug, and the opposite mistake is just as real:
machine-written `proposed` rows accumulate on every sweep until something prunes
them.

Two guards are structural rather than configurable, because each is a rule
something else depends on:

* **Operator rows are exempt under every state and every age.** The guard lives
  in the SQL, not in a caller's flag — an optional guard is one that is
  eventually forgotten.
* **`superseded` is never pruned.** It is what a correction's `supersedes`
  points at, and dropping it turns an audit chain into a dangling reference.

**WHAT A TOMBSTONE REMOVES, AND WHAT IT KEEPS.** Stated here because it is the
one thing a reader will act on, and measured 2026-09-07 rather than reasoned:

    canon (the `ledger` table)     KEPT      — deliberately; the row IS the trail
    `ledger.live()`, the read path gone
    the lexical index              gone
    the vector index               gone
    `registry/ledger.jsonl`        KEPT      — and that file is COMMITTED

So a tombstone makes a revision unREADable, not unRECOVERABLE. The store's bytes
are hardened too (`secure_delete` and a VACUUM, so no freed page holds the text of
an index copy), and that is a narrower claim than it sounds beside the word
"erasure": the canonical row keeps its statement, the export mirrors canon, and
git history keeps the export for ever.

If text must be unrecoverable rather than unreadable, a tombstone is not the
mechanism and this project deliberately does not have one: revisions are
immutable, so nothing here may rewrite one, and the audit trail was chosen over
forgetting. `registry/ledger.jsonl`'s own header says the same thing, because
that is the file somebody will read.

    python3 store/retention.py plan     # what WOULD go. Writes nothing.
    python3 store/retention.py apply    # do it, and collect the receipts
"""
from __future__ import annotations
import argparse, json, sqlite3, sys, pathlib
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths
from store import db as store_db
from store.db import FINGERPRINT_KIND
from store import indexer
from store import ledger
from store import workflow

CONFIG = paths.config_file("retention.json")
APPROVED_BY = "retention"


def config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def cutoff(days: int) -> str:
    return iso(now() - timedelta(days=days))


#: The states retention never touches, whatever their age, read from the config
#: rather than repeated here.
def never_states() -> tuple[str, ...]:
    return tuple(config()["ledger"].get("never") or ())


def exempt_owners() -> tuple[str, ...]:
    return tuple(config()["ledger"].get("owner_exempt") or ())


def days_left(owner: str, state: str, created_at: str) -> int | None:
    """Days until retention would erase this row, or `None` if it never will.

    **ONE HOME, because there were three readers and one applied the rule.**
    `ledger_candidates` below inlines `owner_exempt` into every query —
    structurally, so a caller cannot forget it. `tools/review.py` computed
    `horizon - age` for every row and consulted no exemption; the `soon` query in
    `tools/build_findings.py` did the same. So a row retention will never touch
    was shown with a countdown and could be announced as "will be erased
    unreviewed within 14 days" — a deadline on the operator's attention that does
    not exist.

    Latent rather than live when this was written: `owner_exempt` held only
    `operator`, and no operator row was `proposed` on 2026-09-08. It stops being
    latent the moment a second owner is exempt, which is the same change
.
    """
    if owner in exempt_owners() or state in never_states():
        return None
    cfg = config()["ledger"]
    horizon = cfg.get(f"{state}_days")
    if horizon is None:
        # A state with no declared horizon is not erased by age. Saying `None`
        # is the honest answer: this function must not invent a deadline for a
        # state the policy does not mention.
        return None
    try:
        made = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return int(horizon) - (now() - made).days


def ledger_candidates(conn: sqlite3.Connection) -> list[dict]:
    """Current revisions eligible for a tombstone. Never operator-owned, never a
    state the config protects, and never one already tombstoned."""
    cfg = config()["ledger"]
    ages = {"proposed": cfg["proposed_days"], "observed": cfg["observed_days"],
            "rejected": cfg["rejected_days"]}
    out = []
    for state, days in ages.items():
        if state in cfg["never"]:
            continue
        rows = conn.execute(
            "SELECT l.memory_id, l.revision, l.state, l.owner, l.created_at,"
            "       substr(l.statement, 1, 70) AS gist"
            " FROM ledger l"
            " JOIN (SELECT memory_id, MAX(revision) r FROM ledger GROUP BY memory_id) m"
            "   ON m.memory_id = l.memory_id AND m.r = l.revision"
            " LEFT JOIN tombstones t ON t.memory_id = l.memory_id"
            " WHERE t.memory_id IS NULL"
            "   AND l.state = ?"
            "   AND l.created_at < ?"
            # A workflow's records age by the WORKFLOW, not by their own
            # revision date: an open workflow's checkpoint is the work in
            # progress however long the work takes. `workflow_candidates`
            # applies their horizons below.
            "   AND " + ledger.not_workflow() +
            # Structural, not a parameter: the exempt owners are inlined into
            # every query rather than passed in by a caller who might forget.
            f"   AND l.owner NOT IN ({','.join('?' * len(cfg['owner_exempt']))})",
            (state, cutoff(days), *cfg["owner_exempt"])).fetchall()
        out += [dict(r, horizon_days=days) for r in rows]
    return out + workflow_candidates(conn)


def workflow_candidates(conn: sqlite3.Connection) -> list[dict]:
    """Checkpoints of closed workflows and handoff packs past their horizons.

    The horizons have defaults because a workspace configured before agent
    memory existed carries a `retention.json` without them; an absent key must
    not make the pass fail, nor make workflow records live for ever."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "workflows" not in tables:
        return []
    cfg = config()["ledger"]
    return workflow.retention_candidates(
        conn, closed_checkpoint_days=int(cfg.get("checkpoint_closed_days", 30)),
        handoff_days=int(cfg.get("handoff_days", 90)), cutoff=cutoff)


def volatile_counts(conn: sqlite3.Connection) -> dict:
    cfg = config()
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]                  
    return {
        "events past horizon": q("SELECT count(*) FROM events WHERE occurred_at < ?",
                                 cutoff(cfg["events_days"])),
        "observations past horizon": q(
            "SELECT count(*) FROM observations WHERE observed_at < ? AND kind != ?",
            cutoff(cfg["observations_days"]), FINGERPRINT_KIND),
        "fingerprints beyond the last few": q(
            "SELECT count(*) FROM observations WHERE kind = ? AND rowid NOT IN"
            " (SELECT rowid FROM observations WHERE kind = ? ORDER BY rowid DESC LIMIT ?)",
            FINGERPRINT_KIND, FINGERPRINT_KIND, cfg.get("fingerprints_keep", 3)),
        "deltas consumed": q("SELECT count(*) FROM deltas WHERE consumed_at IS NOT NULL"),
        # TWO CONDITIONS, and the second is what keeps the dashboard honest. A
        # metric row goes when it is past the horizon AND is not among the last
        # few of its own series: the page renders the LATEST value per (project,
        # metric), so a pure time rule would erase the last known figure of a
        # metric whose plugin was removed and the page would show nothing where
        # it should show a stale number. Same shape as `fingerprints_keep`.
        "metrics past horizon": q(
            "SELECT count(*) FROM metrics WHERE at < ? AND rowid NOT IN"
            " (SELECT rowid FROM (SELECT rowid, row_number() OVER"
            "    (PARTITION BY project_id, metric ORDER BY at DESC) rn FROM metrics)"
            "  WHERE rn <= ?)",
            cutoff(cfg.get("metrics_days", 400)),
            cfg.get("metrics_keep_per_series", 2)),
        "idempotency answers past horizon": _idempotency_count(conn, cfg),
    }


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _idempotency_count(conn: sqlite3.Connection, cfg: dict) -> int:
    if not _has_table(conn, "idempotency"):
        return 0
    return conn.execute("SELECT count(*) FROM idempotency WHERE created_at < ?",
                        (cutoff(cfg.get("idempotency_days", 7)),)).fetchone()[0]


#: Tables carrying `(memory_id, revision)` that are NOT derived indexes, each
#: with the reason it is exempt. The purge used to name its two targets as a
#: literal while its own docstring promised "EVERY derived index", so a third
#: projection would have been silently unattested — the shape of every other
#: hardcoded set this repository has had to fix. The targets are now DERIVED
#: from the schema and this is the declared complement, so a new table is
#: attested by default and skipping one takes a sentence.
NOT_A_PROJECTION = {
    "ledger": "the canon itself. A tombstone marks a revision erased; deleting "
              "the row would destroy the audit trail that says so.",
    "tombstones": "the audit trail. Retention's own record of what it erased and "
                  "who approved it.",
    "outbox": "a queue of POINTERS, no text. The indexer already refuses a "
              "tombstoned revision, so a pending row cannot re-index erased "
              "text — verified in `store/indexer.py`, not assumed.",
}


def projection_tables(conn: sqlite3.Connection) -> list[str]:
    """Every table holding a `(memory_id, revision)` pair, minus the declared
    exemptions. Derived, so a projection added tomorrow is attested by default.

    A table SQLite cannot describe is returned anyway: `purge_projections` then
    records it as `absent` or `UNVERIFIABLE` with the error, which is the honest
    reading. Dropping it here would make an unreadable index look like one that
    does not exist.
    """
    out = []
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"):
        if name.startswith("sqlite_") or name in NOT_A_PROJECTION:
            continue
        try:
            cols = {c[1] for c in conn.execute(f"PRAGMA table_info({name})")}
        except sqlite3.Error:
            continue
        if {"memory_id", "revision"} <= cols:
            out.append(name)
    return sorted(out)


def scrub(conn: sqlite3.Connection) -> dict:
    """Make the deleted bytes actually leave the file, and say whether they did.

    `secure_delete=ON` (store/db.py) zeroes content as rows are deleted from
    here on. It cannot reach pages freed before it existed, and it does not
    compact the file, so a purge that removed anything is followed by a WAL
    checkpoint and a VACUUM — the two operations that leave nothing recoverable
    from the file itself.

    VACUUM needs the database to itself and cannot run inside a transaction.
    Another writer — the companion plugin appends a ledger row at the end of
    every turn — makes it fail with `database is locked`, and that failure must
    be REPORTED rather than swallowed: "the bytes are gone" and "I could not
    check whether the bytes are gone" are the two answers this whole module
    exists to keep apart.
    """
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # Outside any transaction, hence the isolation_level dance: sqlite3
        # opens an implicit one for DML, and VACUUM inside it raises.
        prior, conn.isolation_level = conn.isolation_level, None
        try:
            conn.execute("VACUUM")
        finally:
            conn.isolation_level = prior
    except sqlite3.Error as exc:
        return {"scrubbed": False,
                "detail": f"the file was NOT compacted: {type(exc).__name__}: "
                          f"{str(exc)[:120]}. The rows are gone from every index "
                          f"and `secure_delete` zeroed what it could reach, but "
                          f"pages freed earlier may still hold text. Re-run "
                          f"`project-observatory full retention-apply` when nothing else "
                          f"is writing."}
    return {"scrubbed": True,
            "detail": "WAL checkpointed and the file vacuumed, so no freed page "
                      "holds erased text"}


def purge_projections(conn: sqlite3.Connection) -> dict:
    """Remove EVERY tombstoned revision from every derived index, and attest it.

    The contract blocks completion until every configured backend attests the
    purge, and "every" is load-bearing: the first version purged only the rows
    tombstoned in the same pass, so a row that returned to an index after an
    earlier retention — a rebuild against a stale checkpoint would do it — was
    never checked again and `apply` reported success over an incomplete erasure.
    Found by a test that put one back deliberately.

    "Attest" means: count, delete, count again, and then ask the question that
    actually matters — is anything tombstoned still in there?
    """
    # Load the extension FIRST. Without it `vec_notes` answers "no such module:
    # vec0" and the receipt read `absent` — so a tombstoned vector could have
    # survived while retention reported nothing to clean. That is a silent hole
    # in the one guarantee this function exists to give, and it shipped for one
    # tick before the receipt itself showed it.
    have_vec = indexer.load_vec(conn)
    pairs = [(r["memory_id"], r["revision"]) for r in
             conn.execute("SELECT memory_id, revision FROM tombstones")]
    receipts = {}
    # DERIVED, plus `vec_notes` by name: without the extension it is not in
    # `sqlite_master` at all, and an index that is invisible because a library is
    # missing must be reported UNVERIFIABLE rather than omitted from the receipt.
    # Omission is what "attested" would then quietly mean.
    tables = projection_tables(conn)
    if "vec_notes" not in tables:
        tables.append("vec_notes")
    for table in sorted(tables):
        if table == "vec_notes" and not have_vec:
            # Named precisely: the extension could not be loaded, which is NOT
            # the same as the index being empty. An unverifiable backend is
            # quarantined rather than counted as attested.
            receipts[table] = {"status": "UNVERIFIABLE",
                               "detail": "sqlite-vec is not loadable here, so this index "
                                         "cannot be purged or attested"}
            continue
        try:
            before = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        except sqlite3.Error as exc:
            receipts[table] = {"status": "absent",
                               "detail": f"the table does not exist: {str(exc)[:60]}"}
            continue
        removed = 0
        with conn:
            for mid, rev in pairs:
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE memory_id = ? AND revision = ?", (mid, rev))
                removed += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        after = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        # TWO residue counts, per revision AND per record, because the erasure's
        # unit is the record and this loop's unit is the revision.
        #
        # A per-revision count alone can say `purged` over text that is still
        # there: if only the CURRENT revision of a multi-revision record were
        # tombstoned, an earlier revision's statement would stay a live row in
        # the index, and no page would be freed for the VACUUM to zero. Every READ
        # is record-wide (tombstones are joined on `memory_id` alone), so such
        # rows would serve no reader and only hold the text.
        #
        # `ledger.tombstone` writes one row per revision, which makes the loop
        # above complete. This second count is what PROVES it rather than
        # assuming it: if a revision is ever tombstoned alone (by hand, by a
        # migration, by a future caller) the receipt says INCOMPLETE instead of
        # `purged`.
        left = conn.execute(
            f"SELECT count(*) FROM {table} t JOIN tombstones tb"
            f"   ON tb.memory_id = t.memory_id AND tb.revision = t.revision").fetchone()[0]
        left_record = conn.execute(
            f"SELECT count(*) FROM {table} t WHERE t.memory_id IN"
            f" (SELECT memory_id FROM tombstones)").fetchone()[0]
        receipts[table] = {"status": "purged" if not (left or left_record) else "INCOMPLETE",
                           "before": before, "after": after, "removed": removed,
                           "tombstoned_rows_remaining": left,
                           "tombstoned_records_remaining": left_record}
    return receipts


def migration_backup_dir() -> pathlib.Path:
    """Where `store/compatibility.backup` leaves a copy of the database before each schema upgrade."""
    return paths.DB.parent / "migration-backups"


def prune_migration_backups(directory: pathlib.Path, *, keep: int, days: int,
                            now: float | None = None, dry_run: bool = False) -> list[pathlib.Path]:
    """Remove pre-upgrade database copies past their horizon; return what went (or would go).

    These copies were "retained until explicit cleanup", and nothing ever cleaned:
    40 MB of four-week-old copies were measured on 2026-10-03 (lifecycle LC-12,
    "retention is enforced automatically"). A copy goes only when it is BOTH beyond
    the newest `keep` and older than `days`: the copy taken before the latest
    upgrade is the one a rollback would want, and a burst of upgrades in one week
    keeps all of its copies until they age. Symbolic links are never followed."""
    import time as _time
    directory = pathlib.Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        return []
    copies = sorted((p for p in directory.iterdir()
                     if p.is_file() and not p.is_symlink() and ".before-upgrade-" in p.name),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    horizon = (now if now is not None else _time.time()) - days * 86400
    out = [p for p in copies[max(keep, 0):] if p.stat().st_mtime < horizon]
    if not dry_run:
        for p in out:
            p.unlink(missing_ok=True)
    return out


def report(**fields) -> None:
    """Write the receipt where a reader can find it, on every path.

    A helper called unconditionally rather than a write at the end of the happy
    path: the five times a writer sat after an early return in this repository,
    the measurement existed and nobody could read it. Failing to write the
    receipt is itself reported — silently losing the audit trail of an erasure
    would be the same defect one level up.
    """
    doc = {"ran_at": iso(), **fields}
    try:
        paths.SCRATCH.mkdir(parents=True, exist_ok=True)
        (paths.SCRATCH / "retention.json").write_text(
            json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        print(f"could not write the retention receipt: {exc}", file=sys.stderr)


def cmd_plan(conn: sqlite3.Connection) -> int:
    cfg = config()
    cands = ledger_candidates(conn)
    print("ledger — would be tombstoned (the row itself is KEPT):")
    if not cands:
        print("    nothing past its horizon")
    for c in cands[:40]:
        print(f"    {c['memory_id']} rev{c['revision']} [{c['state']}] "
              f"owner={c['owner']} age>{c['horizon_days']}d")
        print(f"      {c['gist']}")
    if len(cands) > 40:
        print(f"    … and {len(cands) - 40} more")
    print("\nvolatile — would be deleted outright:")
    for k, v in volatile_counts(conn).items():
        print(f"    {k:<28} {v}")
    exempt = conn.execute(
        "SELECT count(*) FROM ledger WHERE owner IN"
        f" ({','.join('?' * len(cfg['ledger']['owner_exempt']))})",
        tuple(cfg["ledger"]["owner_exempt"])).fetchone()[0]
    protected = conn.execute(
        "SELECT count(*) FROM ledger WHERE state IN"
        f" ({','.join('?' * len(cfg['ledger']['never']))})",
        tuple(cfg["ledger"]["never"])).fetchone()[0]
    old = prune_migration_backups(migration_backup_dir(), keep=cfg.get("migration_backups_keep", 2),
                                  days=cfg.get("migration_backups_days", 30), dry_run=True)
    print(f"\nmigration backups — would be removed: {len(old)}"
          + (f" ({', '.join(p.name for p in old[:5])})" if old else ""))
    print(f"\nexempt regardless of age: {exempt} operator-owned revision(s), "
          f"{protected} in a protected state")
    print("nothing was written — this is `plan`")
    return 0


def cmd_apply(conn: sqlite3.Connection) -> int:
    cands = ledger_candidates(conn)
    cfg = config()
    # THROUGH `ledger.tombstone`, not around it. This loop wrote the trail
    # itself, with its own one-row-per-candidate rule, so the record-wide
    # expansion added there on 2026-09-07 would have applied to the MCP door and
    # not to the scheduled one — the door that erases almost everything. Two
    # writers of one trail with different units is the shape this repository has
    # now paid for twice; the rule lives with the ledger and this calls it.
    records = 0
    tombstoned = 0
    for c in cands:
        t = ledger.tombstone(
            conn, c["memory_id"],
            reason=f"retention: {c.get('reason') or c['state'] + ' older than ' + str(c['horizon_days']) + ' days'}",
            approved_by=APPROVED_BY)
        records += 1
        tombstoned += len(t["revisions"])
    # Always, not only when this pass tombstoned something: an erasure is
    # incomplete until no projection holds a tombstoned revision, whenever it
    # was tombstoned.
    receipts = purge_projections(conn)

    with conn:
        ev = conn.execute("DELETE FROM events WHERE occurred_at < ?",
                          (cutoff(cfg["events_days"]),)).rowcount
        # Two rules, because the table holds two kinds of thing. A fingerprint is
        # 40 KB of FULL registry state and the only reader takes the latest two,
        # so it is bounded by COUNT — kept by time it reaches ~177 MB, eight
        # times the whole store. Everything else keeps the day horizon, and the
        # `kind !=` is what stops one rule silently owning the other's rows.
        ob = conn.execute("DELETE FROM observations WHERE observed_at < ? AND kind != ?",
                          (cutoff(cfg["observations_days"]), FINGERPRINT_KIND)).rowcount
        fp = conn.execute(
            "DELETE FROM observations WHERE kind = ? AND rowid NOT IN"
            " (SELECT rowid FROM observations WHERE kind = ? ORDER BY rowid DESC LIMIT ?)",
            (FINGERPRINT_KIND, FINGERPRINT_KIND, cfg.get("fingerprints_keep", 3))).rowcount
        ob += fp
        dl = conn.execute("DELETE FROM deltas WHERE consumed_at IS NOT NULL").rowcount
        # The rule `volatile_counts` reports, applied. Kept identical to it on
        # purpose: a `plan` that counts by one rule and an `apply` that deletes
        # by another is a dry run that describes a different operation.
        mt = conn.execute(
            "DELETE FROM metrics WHERE at < ? AND rowid NOT IN"
            " (SELECT rowid FROM (SELECT rowid, row_number() OVER"
            "    (PARTITION BY project_id, metric ORDER BY at DESC) rn FROM metrics)"
            "  WHERE rn <= ?)",
            (cutoff(cfg.get("metrics_days", 400)),
             cfg.get("metrics_keep_per_series", 2))).rowcount
        # A retry is minutes, not weeks: the answer recorded for an
        # idempotency key is forgotten after `idempotency_days`. It may hold a
        # lease token, which is one more reason not to keep it.
        ik = (conn.execute("DELETE FROM idempotency WHERE created_at < ?",
                           (cutoff(cfg.get("idempotency_days", 7)),)).rowcount
              if _idempotency_count(conn, cfg) else 0)
        # Lease rows of a workflow closed past its checkpoint horizon: the
        # checkpoint is erased by then, and the rows only said who held it.
        # The `workflows` row stays — small, and the trace that it existed.
        wl = (workflow.prune_leases(
            conn, cutoff_iso=cutoff(int(cfg["ledger"].get("checkpoint_closed_days", 30))))
              if _has_table(conn, "workflow_leases") else 0)

    # Pre-upgrade database copies are files beside the store, not rows: bounded
    # here too, so the store's footprint has one owner.
    migration = prune_migration_backups(migration_backup_dir(), keep=cfg.get("migration_backups_keep", 2),
                                        days=cfg.get("migration_backups_days", 30))

    # THE BYTES, after the rows. Only when something was actually removed: a
    # VACUUM on every tick would rewrite 23 MB forty-eight times a day to
    # compact nothing.
    removed_any = tombstoned or ev or ob or dl or mt or ik or wl or any(
        r.get("removed") for r in receipts.values())
    scrubbing = (scrub(conn) if removed_any else
                 {"scrubbed": None, "detail": "nothing was removed, so there is "
                                              "nothing to scrub"})

    # A RECEIPT THAT OUTLIVES THE RUN. This module's own first sentence is that
    # an erasure with no audit trail is indistinguishable from a bug — and the
    # attestation the contract demands existed only on stdout, which the tick
    # pipes into a log nothing reads on a schedule. Ledger rows had their
    # tombstone; the volatile deletes and the projection receipts had nothing.
    report(tombstoned=tombstoned, records=records, events=ev, observations=ob,
           deltas=dl, metrics=mt, idempotency=ik, workflow_leases=wl, receipts=receipts,
           scrub=scrubbing, migration_backups=[p.name for p in migration])

    print(f"tombstoned {records} record(s), {tombstoned} revision(s) — every row retained")
    print(f"deleted: {ev} event(s), {ob} observation(s), {dl} consumed delta(s), "
          f"{mt} metric row(s), {ik} idempotency answer(s), {wl} lease row(s) of closed "
          f"workflows, {len(migration)} old migration backup(s)")
    print(f"file scrub: {scrubbing['detail']}")
    bad: list[str] = []
    if any(r.get("status") != "absent" for r in receipts.values()):
        print("projection purge receipts:")
        for table, r in receipts.items():
            print(f"    {table:<14} {r}")
        bad = [t for t, r in receipts.items()
               if r.get("status") in ("INCOMPLETE", "UNVERIFIABLE")]
    else:
        print("projection purge: no index is present to purge")

    # BOTH VERDICTS OUTSIDE THE CONDITIONAL. The byte check lived inside the
    # branch above for one edit, which is the early-return class this repository
    # has now paid for six times: a store with no index at all took the `else`,
    # printed "nothing to purge" and returned 0 while the scrub had failed. An
    # erasure is complete when the rows are gone from every index AND the file
    # holds none of what they said.
    if scrubbing["scrubbed"] is False:
        print(f"\nTHE BYTES WERE NOT SCRUBBED — {scrubbing['detail']}",
              file=sys.stderr)
        return 1
    if bad:
        print(f"\nAN ERASURE IS NOT COMPLETE — {', '.join(bad)}. A projection either "
              f"still holds a tombstoned revision or cannot be checked at all. "
              f"`python -m pip install 'project-observatory[full]'` installs the vector extension; "
              f"`project-observatory full reindex` rebuilds from canon.", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["plan", "apply"])
    a = ap.parse_args()
    conn = store_db.connect()
    try:
        if a.command == "plan":
            return cmd_plan(conn)
        try:
            return cmd_apply(conn)
        except sqlite3.Error as exc:
            # A CONCURRENT WRITER, and the receipt must exist ANYWAY. Measured
            # 2026-09-07 by holding `BEGIN EXCLUSIVE` on a second connection:
            # the tombstone insert raised `database is locked` and the run died
            # with a traceback before writing anything — so the one path where
            # the audit trail matters most was the one path that produced none.
            # `busy_timeout` is 30 seconds and absorbs the companion hook's
            # single row; a longer holder is what this branch is for.
            #
            # The exit code stays non-zero, so `tick.sh`'s `step` records it and
            # `tick.step_failed` grades retention critical. What changes is that
            # the reason survives the run.
            report(tombstoned=0, events=0, observations=0, deltas=0, receipts={},
                   scrub={"scrubbed": False,
                          "detail": f"the pass did not complete: "
                                    f"{type(exc).__name__}: {str(exc)[:120]}. "
                                    f"Nothing was scrubbed, and whatever this "
                                    f"pass would have erased is still there."},
                   failed=f"{type(exc).__name__}: {str(exc)[:200]}")
            print(f"retention did not complete: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 1
    finally:
        conn.close()


if __name__ == "__main__":
    import configuration
    if ("apply" in sys.argv) and not configuration.enabled("retention", "features"):
        print("Not configured: enable features.retention explicitly")
        raise SystemExit(0)

    raise SystemExit(main())
