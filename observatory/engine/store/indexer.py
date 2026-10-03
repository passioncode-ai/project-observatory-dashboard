#!/usr/bin/env python3
"""Build the derived projections: a vector index and a lexical one.

**These carry no canonical meaning.** Losing them is a rebuild, never a data
loss — which is why `rebuild` exists and is tested, rather than being a comment
promising it would work.

The outbox is what makes this safe. A ledger append and its outbox row are one
transaction, so this consumes the outbox rather than scanning the ledger: no
index write can precede a committed revision, and the work is idempotent by
(memory_id, revision, projection_version) so a crash mid-run costs one repeat
and nothing else.

    python3 store/indexer.py index      # consume the outbox
    python3 store/indexer.py rebuild    # drop the projections, rebuild from canon
    python3 store/indexer.py status     # what is indexed, what is pending
"""
from __future__ import annotations
import argparse, json, sqlite3, sys, pathlib
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agent"))
import paths
import textkeys
from store import db as store_db
from store import ledger
import store_faults
import providers

#: One owner, in the store's own vocabulary. This file used to declare its own
#: constant while `store/ledger.py` stamped the literal `1` — see
#: `store/db.py:PROJECTION_VERSION` for what turning the knob did.
PROJECTION_VERSION = store_db.PROJECTION_VERSION


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_vec(conn: sqlite3.Connection) -> bool:
    """True when sqlite-vec loaded. False degrades to the lexical index alone —
    a machine without the extension still gets search, just not by similarity."""
    try:
        import sqlite_vec
    except ImportError:
        return False
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception:
        return False
    return True


def ensure_vec_table(conn: sqlite3.Connection, dims: int) -> None:
    # `memory_id` is a metadata column, NOT a partition key, and the difference is
    # hundreds of megabytes. vec0 reserves a chunk of 1024 vectors per
    # PARTITION, so keying on memory_id gave every single memory its own
    # 1024 × {dims} × 4 bytes — megabytes apiece for the one or two revisions it
    # actually holds, until the vector chunks table dwarfed the data in it.
    #
    # It bought nothing. A partition key prunes a search to one partition, and
    # nothing here searches within a single memory — similarity runs across all
    # of them, and memory_id is used only to DELETE a superseded revision, which
    # a metadata column filters just as well at this scale.
    #
    # It also cost more than space. On a nearly full volume, the write that has
    # to succeed is the one extending a huge allocation, and a store can be left
    # corrupt mid-write.
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_notes USING vec0("
        f"memory_id TEXT, revision INTEGER, embedding float[{dims}])")
    conn.commit()


def indexable(conn: sqlite3.Connection, memory_id: str, revision: int) -> sqlite3.Row | None:
    """A revision worth indexing: it exists, it has text, and its RECORD is not
    tombstoned.

    A tombstoned record is skipped rather than indexed and later purged — the
    contract requires an erasure to reach the projections, and the cheapest way
    to honour that is never to put it there.

    **The join is on `memory_id` alone.** It matched `(memory_id, revision)`
    until 2026-09-07, which let an erased record re-enter this index through an
    unconsumed outbox row for an OLDER revision — and `retention.py`'s own
    exemption for `outbox` rests on this function refusing exactly that
    ("a pending row cannot re-index erased text — verified in
    `store/indexer.py`, not assumed"). Record-wide is also what every reader
    means by a tombstone: `ledger.live()`, `survey.search`, `review.py` and
    `build_findings.py` all join on `memory_id`."""
    return conn.execute(
        "SELECT l.memory_id, l.revision, l.statement, l.why, l.project_id, l.state, l.kind,"
        " l.body_json"
        " FROM ledger l LEFT JOIN tombstones t"
        "   ON t.memory_id = l.memory_id"
        " WHERE l.memory_id = ? AND l.revision = ? AND t.memory_id IS NULL",
        (memory_id, revision)).fetchone()


def text_of(row: sqlite3.Row) -> str:
    parts = [row["statement"] or ""]
    if row["why"]:
        parts.append(row["why"])
    return "\n".join(p for p in parts if p.strip())


def index_batch(conn: sqlite3.Connection, rows: list[sqlite3.Row], have_vec: bool,
                dims: int) -> tuple[int, float, int, bool]:
    """Embed and write one batch. Returns (written, cost, tokens, vectors_written).

    The fourth value is what `cmd_index` needs to decide whether the batch's
    outbox rows may be consumed. Before it existed, a provider failure was
    caught here, the lexical index was written, and the rows were marked
    consumed anyway — so "writing the lexical index only" stopped being a
    degradation of THIS RUN and became a permanent hole. Measured 2026-09-07
    against a 200-row queue with the provider failing from the second batch:
    200 lexical rows, 64 vectors, an empty outbox, and a final line claiming
    "indexed 200 revision(s)". Nothing would ever have retried the 136."""
    if not rows:
        return 0, 0.0, 0, True
    written, cost, tokens = 0, 0.0, 0
    vectors = None
    # A WORKFLOW'S RECORDS ARE LEXICAL ONLY. A checkpoint is written after every
    # step of every workflow, and its text is the work in progress of a project:
    # sending each revision to the configured embedding provider would spend per
    # step and carry project-internal state off the machine, which
    # docs/design/AGENT-MEMORY.md rules out until a local embedding model indexes them. They
    # are found by workflow id, and by the lexical index for related records.
    embedded = [i for i, r in enumerate(rows) if _embeddable(r)]
    if have_vec and embedded:
        try:
            res = providers.embed([text_of(rows[i]) for i in embedded])
            got, cost, tokens = res["vectors"], res["cost"], res["tokens"]
            vectors = {i: got[k] for k, i in enumerate(embedded)}
        except providers.ProviderError as exc:
            print(f"  embedding unavailable ({type(exc).__name__}: {exc});"
                  f" writing the lexical index only — these revisions stay QUEUED so a "
                  f"later run can embed them", file=sys.stderr)
            vectors = None
    # There was a bare `import sqlite_vec` here, commented "(only reached when
    # have_vec)". It was not: it sits in the function body, so it ran on every
    # call — and on a machine without the package it raised ImportError instead
    # of building the lexical index, which is the exact degradation `cmd_index`
    # announces two functions below. `tick.sh` swallowed the traceback into a log
    # line. Nothing needed it: `load_vec` does the loading and
    # `serialize_float32` is imported at its own use site, under `vectors`.
    # A store whose lexical index predates search keys (migration 0009) — a
    # hand-made fixture — is written without them, as `ledger` writes it.
    keyed = "stems" in [c[1] for c in conn.execute("PRAGMA table_info(search_notes)")]
    with conn:
        for i, r in enumerate(rows):
            # `<=`, not `=`: a new revision REPLACES the record in the index.
            # Keeping older revisions made every search answer with each one as
            # a separate, live-looking result.
            conn.execute("DELETE FROM search_notes WHERE memory_id = ? AND revision <= ?",
                         (r["memory_id"], r["revision"]))
            if keyed:
                body = r["body_json"] if "body_json" in r.keys() else None
                conn.execute("INSERT INTO search_notes (memory_id, revision, statement, why, stems)"
                             " VALUES (?,?,?,?,?)",
                             (r["memory_id"], r["revision"], r["statement"], r["why"] or "",
                              textkeys.stems_of(r["statement"], r["why"], body)))
            else:
                conn.execute("INSERT INTO search_notes (memory_id, revision, statement, why)"
                             " VALUES (?,?,?,?)",
                             (r["memory_id"], r["revision"], r["statement"], r["why"] or ""))
            if have_vec:
                # Older revisions leave the vector index too, whether or not
                # this one is embedded: a workflow record never is, and a stale
                # vector must not outlive the statement it was made from.
                conn.execute("DELETE FROM vec_notes WHERE memory_id = ? AND revision < ?",
                             (r["memory_id"], r["revision"]))
            if vectors is not None and i in vectors:
                from sqlite_vec import serialize_float32
                conn.execute("DELETE FROM vec_notes WHERE memory_id = ? AND revision = ?",
                             (r["memory_id"], r["revision"]))
                conn.execute("INSERT INTO vec_notes (memory_id, revision, embedding)"
                             " VALUES (?,?,?)",
                             (r["memory_id"], r["revision"], serialize_float32(vectors[i])))
            written += 1
    return written, cost, tokens, (vectors is not None or not have_vec or not embedded)


def _embeddable(row: sqlite3.Row) -> bool:
    """False for the kinds that are never sent to an embedding provider."""
    keys = row.keys()
    return "kind" not in keys or row["kind"] not in ledger.WORKFLOW_KINDS


def cmd_index(conn: sqlite3.Connection, limit: int) -> int:
    cfg = providers.config()["embedding"]
    have_vec = load_vec(conn)
    if have_vec:
        ensure_vec_table(conn, cfg["dims"])
    else:
        print("sqlite-vec is not loadable here — the lexical index is built, the vector "
              "one is not. `python -m pip install 'project-observatory[full]'` installs it.", file=sys.stderr)

    # `<=`, not `=`. Equality meant a row enqueued under an older contract was
    # invisible rather than in need of re-projection, so the queue could report
    # itself empty while holding every revision the estate has. A row stamped
    # ABOVE this code's version is a different matter — that is an indexer older
    # than the store it is pointed at, and it is reported rather than consumed.
    ahead = conn.execute(
        "SELECT count(*) FROM outbox WHERE consumed_at IS NULL"
        " AND projection_version > ?", (PROJECTION_VERSION,)).fetchone()[0]
    if ahead:
        print(f"{ahead} outbox row(s) are stamped for a NEWER projection contract than "
              f"this indexer builds (version {PROJECTION_VERSION}). Refusing: a mixed "
              f"index answers searches from two contracts at once.", file=sys.stderr)
        return 1

    pending = list(conn.execute(
        "SELECT seq, memory_id, revision FROM outbox"
        " WHERE consumed_at IS NULL AND projection_version <= ?"
        " ORDER BY seq LIMIT ?", (PROJECTION_VERSION, limit)))
    if not pending:
        # Only ever say this when the projections were BUILT at the version this
        # code projects for. `vec_meta.projection_version` records what is on
        # disk; a bump leaves an index that is complete under the old contract
        # and stale under the new one, and "every committed revision is already
        # projected" is then true of the wrong contract.
        built = conn.execute("SELECT projection_version FROM vec_meta LIMIT 1").fetchone()
        if built and built[0] != PROJECTION_VERSION:
            print(f"outbox empty, but the projections on disk were built at version "
                  f"{built[0]} and this indexer builds {PROJECTION_VERSION}. "
                  f"Run `project-observatory full reindex` — the index is stale, not current.",
                  file=sys.stderr)
            return 1
        print("outbox empty — every committed revision is already projected")
        return 0

    batch, seqs, skipped = [], [], 0
    batch_seqs: list[int] = []                                                
    done_seqs: list[int] = []                                          
    held_seqs: list[int] = []                                                        
    total_written = total_cost = total_tokens = 0
    size = int(cfg.get("batch_size", 64))

    def flush() -> None:
        nonlocal batch, batch_seqs, total_written, total_cost, total_tokens
        if not batch:
            return
        try:
            w, c, tok, vectors_ok = index_batch(conn, batch, have_vec, cfg["dims"])
        except sqlite3.Error as exc:
            # RECORD, THEN RE-RAISE. A failure here (for example
            # `vtable constructor failed: search_notes`) used to exit the step
            # with nothing captured but a message in the tick log, while
            # `integrity_check` answered `ok` minutes later. `store_faults`
            # writes free space, WAL size, holder count and the sqlite error
            # CODE, none of which survives to a later investigation otherwise.
            #
            # Swallowing it would be worse than the crash: `file is not a
            # database` means the file really is broken, and a step that carries
            # on keeps a corrupt store in service. `tick.step_failed` already
            # reports a stopped step, so the crash was never the gap — the
            # evidence was.
            store_faults.record("index", exc)
            raise
        total_written += w; total_cost += c; total_tokens += tok
        # A batch whose vector half failed keeps its rows QUEUED. Consuming them
        # turned a run's degradation into a permanent one: the lexical entry
        # exists, the vector never will, and nothing retries. When sqlite-vec is
        # not loadable at all the rows ARE consumed — that is a property of this
        # machine, announced at the top of the run, not a transient failure, and
        # holding them would grow the queue without bound and keep the
        # `projection.lagging` finding lit for ever.
        (done_seqs if vectors_ok else held_seqs).extend(batch_seqs)
        batch, batch_seqs = [], []

    for p in pending:
        row = indexable(conn, p["memory_id"], p["revision"])
        seqs.append(p["seq"])
        if row is None or not text_of(row).strip():
            skipped += 1
            done_seqs.append(p["seq"])                                         
            continue
        batch.append(row)
        batch_seqs.append(p["seq"])
        if len(batch) >= size:
            flush()
    flush()

    # Checkpoint only what was actually handled, and only after the writes above
    # committed. An outbox row marked consumed before its projection landed is a
    # revision that never gets indexed and nothing ever notices.
    with conn:
        conn.executemany("UPDATE outbox SET consumed_at = ? WHERE seq = ?",
                         [(now(), s) for s in done_seqs])
        if done_seqs:
            conn.execute("UPDATE vec_meta SET checkpointed_seq = ?", (max(done_seqs),))
    still = conn.execute(
        "SELECT count(*) FROM outbox WHERE consumed_at IS NULL").fetchone()[0]
    # BOTH halves in the count. This said "indexed 200 revision(s)" when 64 had
    # a vector and 136 had only a lexical entry — one number for two indexes,
    # and the half that was missing was the one a caller could not see.
    full = total_written - len(held_seqs)
    half = f", {len(held_seqs)} lexical-only" if held_seqs else ""
    # "into both indexes" is false on a machine with no sqlite-vec, where there
    # is only one. The run announces the missing extension at the top; a summary
    # that then contradicts it is the same defect one paragraph later.
    where = "both indexes" if have_vec else "the lexical index"
    print(f"indexed {full} revision(s) into {where}{half}, skipped {skipped} "
          f"(tombstoned or empty), checkpoint "
          f"{max(done_seqs) if done_seqs else 'unchanged'}")
    if held_seqs:
        print(f"  {len(held_seqs)} revision(s) kept in the queue: the lexical index has "
              f"them, the vector one does not, and a later run will embed them",
              file=sys.stderr)
    # NO SILENT CAP. The run used to say "indexed 500" and stop, with 700 rows
    # still queued and nothing in the output to say so — measured 2026-09-07.
    # A caller reading that line would reasonably conclude the queue was drained.
    if still:
        print(f"  {still} revision(s) still queued — this run was capped at {limit}. "
              f"Run `project-observatory full index` again until it reports the outbox empty."
              if still > len(held_seqs) else
              f"  {still} revision(s) still queued.", file=sys.stderr)
    if total_tokens:
        print(f"  {total_tokens} embedding token(s), {total_cost:.8f} credits ESTIMATED "
              f"from a dated price in agent/models.json — OpenAI reports no cost")
    return 0


def cmd_rebuild(conn: sqlite3.Connection) -> int:
    """Drop both projections and rebuild from the ledger.

    This is the invariant, not a convenience: a derived projection must be
    rebuildable without changing canonical identity or revision history.
    """
    have_vec = load_vec(conn)
    cfg = providers.config()["embedding"]
    with conn:
        conn.execute("DELETE FROM search_notes")
        if have_vec:
            conn.execute("DROP TABLE IF EXISTS vec_notes")
        conn.execute("UPDATE outbox SET consumed_at = NULL")
        # Re-stamp the queue and record the contract the projections are now
        # built at. Without this, `rebuild` is the only way out of a version bump
        # and cannot itself complete one: the rows keep their old stamp and
        # `vec_meta` keeps the old version, so `index` would go on refusing.
        conn.execute("UPDATE outbox SET projection_version = ?", (PROJECTION_VERSION,))
        conn.execute("UPDATE vec_meta SET checkpointed_seq = 0, projection_version = ?",
                     (PROJECTION_VERSION,))
    if have_vec:
        ensure_vec_table(conn, cfg["dims"])
    n = conn.execute("SELECT count(*) FROM outbox WHERE consumed_at IS NULL").fetchone()[0]
    print(f"projections dropped; {n} outbox row(s) re-queued at version {PROJECTION_VERSION}")
    # A cap that silently truncates a REBUILD is worse than one that truncates a
    # tick: the projections were just dropped, so whatever it does not reach is
    # missing rather than merely late. Say so instead of returning 0 quietly.
    CAP = 10_000
    rc = cmd_index(conn, limit=CAP)
    left = conn.execute(
        "SELECT count(*) FROM outbox WHERE consumed_at IS NULL").fetchone()[0]
    if left:
        print(f"{left} revision(s) still unprojected — the rebuild cap is {CAP} per run. "
              f"Run `project-observatory full index` again until it reports the outbox empty.",
              file=sys.stderr)
    return rc


def cmd_status(conn: sqlite3.Connection) -> int:
    have_vec = load_vec(conn)
    counts = {
        "ledger revisions": conn.execute("SELECT count(*) FROM ledger").fetchone()[0],
        "tombstoned": conn.execute("SELECT count(*) FROM tombstones").fetchone()[0],
        "outbox pending": conn.execute(
            "SELECT count(*) FROM outbox WHERE consumed_at IS NULL").fetchone()[0],
        "lexical rows": conn.execute("SELECT count(*) FROM search_notes").fetchone()[0],
    }
    if have_vec:
        try:
            counts["vector rows"] = conn.execute("SELECT count(*) FROM vec_notes").fetchone()[0]
        except sqlite3.Error:
            counts["vector rows"] = "table absent"
    else:
        counts["vector rows"] = "sqlite-vec not loadable"
    meta = conn.execute("SELECT * FROM vec_meta LIMIT 1").fetchone()
    for k, v in counts.items():
        print(f"  {k:<20} {v}")
    if meta:
        print(f"  contract             {meta['embedding_provider']}/"
              f"{meta['embedding_model']} dims={meta['dims']} "
              f"checkpoint={meta['checkpointed_seq']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["index", "rebuild", "status"])
    ap.add_argument("--limit", type=int, default=500)
    a = ap.parse_args()
    conn = store_db.connect()
    try:
        if a.command == "index":
            return cmd_index(conn, a.limit)
        if a.command == "rebuild":
            return cmd_rebuild(conn)
        return cmd_status(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    import configuration
    if (len(sys.argv) < 2 or sys.argv[1] in {"index", "reindex"}) and not configuration.enabled("embeddings", "features"):
        print("Not configured: enable features.embeddings explicitly")
        raise SystemExit(0)

    raise SystemExit(main())
