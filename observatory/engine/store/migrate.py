#!/usr/bin/env python3
"""Schema-compatible data migrations, applied once and recorded.

`store/schema.sql` creates the shape. This file fixes CONTENT that a past
version wrote wrongly — which schema DDL cannot express and which a one-off
script nobody remembers to run does not fix either. Each migration is named,
idempotent, and recorded in a `migrations` table, so `db.connect()` can apply
what is outstanding and skip what is done with a single SELECT.

The first one exists because of a defect measured on 2026-09-06: 8,249 of 9,013
`events.occurred_at` values carried a local offset — `+02:00`, `+03:00`,
`+01:00`, `-05:00` — because `collectors/scan_events.py` took git's `%cI`
verbatim while every other writer in this store wrote UTC `Z`.

**Why that is a correctness bug and not a formatting one.** The column is TEXT
and every comparison over it is lexicographic: `survey.timeline` orders and
windows with `ORDER BY occurred_at DESC` and `occurred_at >= ?`, and
`store/retention.py` prunes with `occurred_at < '…Z'`. In that ordering
`+` (0x2B) sorts before `-` (0x2D) sorts before `Z` (0x5A) — so for one and the
same instant the three shapes sort into three different places, "what happened
last week" is wrong by up to 26 hours across the offset boundary, and the
retention cutoff is off by a timezone.
"""
from __future__ import annotations
import sqlite3
import ast, hashlib, inspect, textwrap
from datetime import datetime, timezone

UTC_Z = "%Y-%m-%dT%H:%M:%SZ"


def to_utc_z(value: str) -> str | None:
    """One instant, one spelling. None when the value carries no zone at all.

    A naive timestamp is NOT assumed to be UTC: every writer in this store
    stamps a zone, so a naive one means something unknown happened and guessing
    would bury it. The caller counts those and says so rather than converting."""
    if value.endswith("Z") and len(value) == 20:
        return value
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc).strftime(UTC_Z)


def _events_utc(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT id, occurred_at FROM events WHERE occurred_at NOT LIKE '%Z'").fetchall()
    converted, refused = 0, []
    for row in rows:
        iso = to_utc_z(row["occurred_at"] if hasattr(row, "keys") else row[1])
        if iso is None:
            refused.append(row[0])
            continue
        conn.execute("UPDATE events SET occurred_at = ? WHERE id = ?", (iso, row[0]))
        converted += 1
    note = f"{converted} event timestamp(s) normalised to UTC"
    if refused:
        note += f"; {len(refused)} carried no zone and were LEFT ALONE: {refused[:3]}"
    return note


def _drop_foreign_events(conn: sqlite3.Connection) -> str:
    """Commits from projects whose history is not this estate's own work.

    `collectors/scan_events.py` recorded every checkout until 2026-09-06, while
    the companion plugin's recorder had always refused anything but `owned` and
    `work-bitbucket`. The rule now lives in `estate.py` and both read it; these
    are the rows written before it did."""
    import json as _json
    import pathlib as _pathlib
    import sys as _sys
    _sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1]))
    import estate
    import paths
    projects_file = paths.REGISTRY / "projects.json"
    if not projects_file.exists():
        return "no registry to read ownership from; nothing dropped"
    own = {p["id"]: p.get("ownership")
           for p in _json.loads(projects_file.read_text(encoding="utf-8"))["projects"]}
    foreign = [pid for pid, o in own.items() if not estate.records_events(o)]
    if not foreign:
        return "no project is outside the estate's own work"
    marks = ",".join("?" * len(foreign))
    # `AND kind = 'commit'` — the narrowing this docstring always claimed. The
    # statement deleted every event of a foreign project regardless of kind,
    # while the sentence above says "commits", and the difference became real
    # when SRC-0012 started recording `session` events: a session in a
    # third-party clone is the OPERATOR's work — they sat down and worked in it
    # — where a commit there is somebody else's history, which is the whole
    # reason this migration exists. Narrowing a destructive statement to its
    # own documented intent; the rows it will now spare did not exist when it
    # ran on this machine, so its recorded effect is unchanged.
    n = conn.execute(f"DELETE FROM events WHERE kind = 'commit'"
                     f" AND project_id IN ({marks})", foreign).rowcount
    return f"{n} event(s) dropped from {len(foreign)} project(s) outside this estate's own work"


def _project_week_table(conn: sqlite3.Connection) -> str:
    """`schema.sql` creates it for a fresh store; `db.connect()` applies that
    script only when the database is new, so an existing one needs it here."""
    execute_statements(conn, """
      CREATE TABLE IF NOT EXISTS project_week (
        project_id TEXT NOT NULL, week TEXT NOT NULL, week_start TEXT NOT NULL,
        commits INTEGER NOT NULL, active_days INTEGER NOT NULL, authors INTEGER NOT NULL,
        first_at TEXT, last_at TEXT, computed_at TEXT NOT NULL, frozen_at TEXT,
        PRIMARY KEY (project_id, week));
      CREATE INDEX IF NOT EXISTS project_week_by_week ON project_week(week_start);
    """)
    return "project_week is available for weekly rollups"


def _metrics_table(conn: sqlite3.Connection) -> str:
    """Plugin measurements. `schema.sql` creates it for a fresh store; an
    existing one needs it here."""
    execute_statements(conn, """
      CREATE TABLE IF NOT EXISTS metrics (
        project_id TEXT NOT NULL, metric TEXT NOT NULL, at TEXT NOT NULL,
        value REAL NOT NULL, unit TEXT NOT NULL DEFAULT '', source TEXT NOT NULL,
        payload_json TEXT, recorded_at TEXT NOT NULL,
        PRIMARY KEY (project_id, metric, at));
      CREATE INDEX IF NOT EXISTS metrics_by_metric ON metrics(metric, at);
      CREATE INDEX IF NOT EXISTS metrics_by_source ON metrics(source);
    """)
    return "metrics is available for plugin measurements"


def _project_week_sessions(conn: sqlite3.Connection) -> str:
    """Three nullable columns, so a week worked on without a commit can exist.

    NULL is the load-bearing choice. An existing row was computed before these
    columns did, and 0 would claim it had been measured and found empty. For an
    unfrozen row the next refresh fills it in; for a frozen one it can never be
    completed, which `tools/build_findings.py` raises rather than hides.

    `ADD COLUMN` is guarded per column: SQLite raises on a duplicate, and this
    migration must stay re-runnable against a store where `schema.sql` already
    created the table in its new shape.
    """
    have = {c[1] for c in conn.execute("PRAGMA table_info(project_week)")}
    added = []
    for col in ("sessions", "session_days", "worked_days"):
        if col not in have:
            conn.execute(f"ALTER TABLE project_week ADD COLUMN {col} INTEGER")
            added.append(col)
    return (f"project_week gained {', '.join(added)}" if added
            else "project_week already carries the session columns")


def _collector_cursors(conn: sqlite3.Connection) -> str:
    """`cursors` — where a named collector got to. See schema.sql for why.

    The cursor is SEEDED from `deltas` rather than left empty, because an empty
    cursor on an existing store would make the next `diff` compare against the
    previous fingerprint again and re-write deltas already reported. The most
    recent `to_scan` IS the last state that was diffed; reading it once, here,
    is safe in a way that reading it on every run is not — retention deletes a
    delta as soon as the agent consumes it, so the inference is available now
    and will not be later.
    """
    conn.execute("CREATE TABLE IF NOT EXISTS cursors ("
                 " name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")
    # `deltas` MAY NOT EXIST. A migration runs against whatever shape the store
    # is in, and that includes a store built by hand to exercise an earlier
    # migration — `tests/test_time.py` creates `events` alone, and this line
    # failed the gate the moment it was written. A seed is an optimisation over
    # an empty cursor; a missing source for it is not an error.
    try:
        row = conn.execute("SELECT to_scan FROM deltas ORDER BY rowid DESC LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        return f"cursors is available; nothing to seed from ({str(exc)[:60]})"
    if row is None:
        return "cursors is available; no delta on record, so nothing to seed"
    conn.execute("INSERT OR IGNORE INTO cursors (name, value, updated_at) VALUES (?,?,?)",
                 ("deltas.diffed_through", row[0],
                  datetime.now(timezone.utc).strftime(UTC_Z)))
    return f"cursors seeded: deltas.diffed_through = {row[0]}"


#: (id, function). Append only — an applied id is never renamed or reordered,
#: because the record of what ran is keyed by it.
def _drop_retention_policy(conn: sqlite3.Connection) -> str:
    """Remove `ledger.retention_policy`, which nothing ever read.

    Declared in the schema, `NOT NULL DEFAULT 'default'`, written on every row
    — by a literal `"default"` in `ledger.append`, the only writer — exported
    into `registry/ledger.jsonl`, listed in `docs/ARCHITECTURE.md`, and
    consulted by no code path. Retention decides by state age and
    `owner_exempt`.

    **Implementing it instead was refused on a measurement, not a preference.**
    A per-row policy is set at INSERT time, which is exactly the "flag at the
    call site" that `store/retention.json` argues against — and there is a
    concrete case: `agent:estate-history` joined `owner_exempt` on 2026-09-08,
    and the two rows it had already written carry `'default'`. Under a
    column-driven rule those two would still be unprotected; under the owner
    list the exemption applied to every row, past and future, the moment it was
    added. The owner list is the correct locus.

    Adding a column back is `ALTER TABLE ADD COLUMN`, so this is the cheap
    direction to travel.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(ledger)")]
    if "retention_policy" not in cols:
        return "ledger.retention_policy was already absent"
    conn.execute("ALTER TABLE ledger DROP COLUMN retention_policy")
    return (f"dropped ledger.retention_policy from {len(cols)} columns; "
            f"nothing read it and `append` wrote a constant")


def _agent_memory_workflows(conn: sqlite3.Connection) -> str:
    """Workflows, executor leases and idempotency keys; four ledger columns.

    The design is docs/design/AGENT-MEMORY.md: a workflow's state is a chain of
    checkpoint revisions in the ledger, a handoff pack is an immutable ledger
    record, and the right to continue is a lease. Every statement is guarded so
    the migration also runs against a store `schema.sql` already created in the
    new shape — `db.connect()` applies the schema to a fresh store and then
    every migration, so a fresh store meets each `ADD COLUMN` a second time.
    """
    # A STORE WITHOUT A LEDGER is a store built by hand to exercise another
    # migration (`tests/test_time.py` creates `events` alone); `schema.sql`
    # creates the ledger in its current shape wherever one is really needed.
    have = {c[1] for c in conn.execute("PRAGMA table_info(ledger)")}
    added = []
    if have:
        for col in ("workflow_id", "step_id", "executor_json", "body_json"):
            if col not in have:
                conn.execute(f"ALTER TABLE ledger ADD COLUMN {col} TEXT")
                added.append(col)
        conn.execute("CREATE INDEX IF NOT EXISTS ledger_workflow ON ledger(workflow_id, kind)"
                     " WHERE workflow_id IS NOT NULL")
    execute_statements(conn, """
      CREATE TABLE IF NOT EXISTS workflows (
        workflow_id TEXT PRIMARY KEY, project_id TEXT, created_by TEXT NOT NULL,
        created_at TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('open','closed')),
        closed_at TEXT, closed_by TEXT) STRICT;
      CREATE TABLE IF NOT EXISTS workflow_leases (
        lease_ref TEXT PRIMARY KEY,
        workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
        state TEXT NOT NULL CHECK (state IN ('offered','active','ended')),
        token TEXT UNIQUE, holder TEXT, executor_json TEXT NOT NULL DEFAULT '{}',
        handoff_id TEXT, granted_at TEXT NOT NULL, expires_at TEXT, accepted_at TEXT,
        ended_at TEXT, ended_reason TEXT) STRICT;
      CREATE UNIQUE INDEX IF NOT EXISTS workflow_one_active
        ON workflow_leases(workflow_id) WHERE state = 'active';
      CREATE UNIQUE INDEX IF NOT EXISTS workflow_one_offer
        ON workflow_leases(workflow_id) WHERE state = 'offered';
      CREATE UNIQUE INDEX IF NOT EXISTS workflow_lease_handoff
        ON workflow_leases(handoff_id) WHERE handoff_id IS NOT NULL;
      CREATE TABLE IF NOT EXISTS idempotency (
        principal TEXT NOT NULL, operation TEXT NOT NULL, key TEXT NOT NULL,
        request_sha256 TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY (principal, operation, key)) STRICT;
    """)
    return (f"ledger gained {', '.join(added)}; " if added else
            "ledger already carries the workflow columns; " if have else
            "no ledger here to extend; ") + \
        "workflows, workflow_leases and idempotency are available"


def _search_stems(conn: sqlite3.Connection) -> str:
    """The lexical index gains a `stems` column, rebuilt from canon.

    FTS5 has no ALTER, and the index is a projection — losing it is a rebuild,
    never a data loss — so it is dropped, recreated with the column and refilled
    from the latest revision of every record no tombstone hides. Done here rather
    than left to the indexer, because the indexer only projects the outbox, and
    every revision already consumed would otherwise stay unsearchable by stem."""
    import pathlib as _pathlib
    import sys as _sys
    _sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1]))
    import textkeys
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "ledger" not in tables:
        return "no ledger here to index"
    cols = [r[1] for r in conn.execute("PRAGMA table_info(search_notes)")] \
        if "search_notes" in tables else []
    if "stems" in cols:
        return "search_notes already carries stems"
    conn.execute("DROP TABLE IF EXISTS search_notes")
    conn.execute("CREATE VIRTUAL TABLE search_notes USING fts5("
                 "memory_id UNINDEXED, revision UNINDEXED, statement, why, stems)")
    rows = conn.execute(
        "SELECT l.memory_id, l.revision, l.statement, l.why, l.body_json FROM ledger l"
        " JOIN (SELECT memory_id, MAX(revision) r FROM ledger GROUP BY memory_id) m"
        "   ON m.memory_id = l.memory_id AND m.r = l.revision"
        " WHERE l.memory_id NOT IN (SELECT memory_id FROM tombstones)"
        "   AND trim(coalesce(l.statement, '')) != ''").fetchall() \
        if "tombstones" in tables else []
    for mid, rev, statement, why, body in rows:
        conn.execute("INSERT INTO search_notes (memory_id, revision, statement, why, stems)"
                     " VALUES (?,?,?,?,?)",
                     (mid, rev, statement, why or "", textkeys.stems_of(statement, why, body)))
    return (f"search_notes rebuilt with stems from {len(rows)} current record(s)"
            + ("" if textkeys.STEMMER else "; no stemmer installed, keys are lower-cased words"))


MIGRATIONS: list[tuple[str, object]] = [
    ("0001-events-occurred-at-utc", _events_utc),
    ("0002-drop-events-outside-the-estate", _drop_foreign_events),
    ("0003-project-week-rollup", _project_week_table),
    ("0004-plugin-metrics", _metrics_table),
    ("0005-project-week-sessions", _project_week_sessions),
    ("0006-collector-cursors", _collector_cursors),
    ("0007-drop-inert-retention-policy", _drop_retention_policy),
    ("0008-agent-memory-workflows", _agent_memory_workflows),
    ("0009-search-stems", _search_stems),
]


class CompatibilityError(RuntimeError):
    """The database requires a different executable; no automatic downgrade."""


def execute_statements(conn: sqlite3.Connection, script: str) -> None:
    """Run DDL without executescript's implicit COMMIT."""
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            conn.execute(pending)
            pending = ""
    if pending.strip():
        raise ValueError("Incomplete migration SQL")


def checksum(fn: object) -> str:
    # AST excludes comments; documentation-only edits do not change the contract.
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(
                    node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
                node.body.pop(0)
    return hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()


def validate(conn: sqlite3.Connection) -> set[str]:
    """Read-only guard. Unknown histories and changed applied code fail closed."""
    if sqlite3.sqlite_version_info < (3, 37, 0):
        raise CompatibilityError("SQLite 3.37 or newer is required")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > len(MIGRATIONS):
        raise CompatibilityError("Database schema is newer than this executable; restore a compatible backup to downgrade")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    done = {r[0] for r in conn.execute("SELECT id FROM migrations")} if "migrations" in tables else set()
    known = dict(MIGRATIONS)
    if done - known.keys():
        raise CompatibilityError("Database contains migrations unknown to this executable")
    if "migration_checksums" in tables:
        for mid, digest in conn.execute("SELECT id, checksum FROM migration_checksums"):
            if mid not in known or mid not in done or checksum(known[mid]) != digest:
                raise CompatibilityError("Applied migration code does not match its recorded checksum")
    return done


def needs_upgrade(conn: sqlite3.Connection) -> bool:
    done = validate(conn)
    if done != {mid for mid, _ in MIGRATIONS}:
        return True
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "migration_checksums" not in tables:
        return True
    recorded = {r[0] for r in conn.execute("SELECT id FROM migration_checksums")}
    return recorded != done or conn.execute("PRAGMA user_version").fetchone()[0] != len(MIGRATIONS)


def apply(conn: sqlite3.Connection) -> list[str]:
    """Atomic migrations; nested callers keep ownership of their transaction.

    Legacy histories are adopted once: their missing checksums are recorded for
    this release, without pretending to attest which old code actually ran.
    Production callers use db.connect(), which takes the backup and process lock.
    """
    if not needs_upgrade(conn):
        return []
    nested = conn.in_transaction
    conn.execute("SAVEPOINT observatory_migrations" if nested else "BEGIN IMMEDIATE")
    try:
        done = validate(conn)                                                     
        conn.execute("CREATE TABLE IF NOT EXISTS migrations ("
                     " id TEXT PRIMARY KEY, applied_at TEXT NOT NULL, note TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS migration_checksums ("
                     " id TEXT PRIMARY KEY REFERENCES migrations(id), checksum TEXT NOT NULL)")
        notes = []
        for mid, fn in MIGRATIONS:
            if mid not in done:
                note = fn(conn)
                conn.execute("INSERT INTO migrations (id, applied_at, note) VALUES (?,?,?)",
                             (mid, datetime.now(timezone.utc).strftime(UTC_Z), note))
                notes.append(f"{mid}: {note}")
            conn.execute("INSERT OR IGNORE INTO migration_checksums (id, checksum) VALUES (?,?)",
                         (mid, checksum(fn)))
        conn.execute(f"PRAGMA user_version={len(MIGRATIONS)}")
        if nested:
            conn.execute("RELEASE SAVEPOINT observatory_migrations")
        else:
            conn.commit()
        return notes
    except BaseException:
        if nested:
            conn.execute("ROLLBACK TO SAVEPOINT observatory_migrations")
            conn.execute("RELEASE SAVEPOINT observatory_migrations")
        else:
            conn.rollback()
        raise


if __name__ == "__main__":
    import pathlib, sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from store import db as store_db
    conn = store_db.connect()
    applied = apply(conn)
    print("\n".join(applied) if applied else "every migration is already applied")
    left = conn.execute("SELECT COUNT(*) FROM events WHERE occurred_at NOT LIKE '%Z'").fetchone()[0]
    print(f"events not in UTC Z: {left}")
    raise SystemExit(1 if left else 0)
