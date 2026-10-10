#!/usr/bin/env python3
"""Open the store, applying the schema on first use.

The store is rebuildable: MEASURED tables come from the machine, DERIVED tables
from CANONICAL. Only the ledger is authored.

That last sentence used to end "and it is exported to the wiki". It was FALSE —
nothing exported it, and `project_into_vault.py` never mentioned it — so the only
copy of every conclusion the agent had drawn lived in this one gitignored file.
It was found corrupt on 2026-09-06 and came back because `.recover` happened to
find the pages intact, which is luck. `tools/export_ledger.py` now writes
`registry/ledger.jsonl` on every tick, and the claim above is true of that file.
"""
from __future__ import annotations
import sqlite3, sys, pathlib, uuid, os

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import paths
import osprivacy

DB_PATH = paths.DB
SCHEMA = paths.ROOT / "store" / "schema.sql"

#: The `observations.kind` written by the registry fingerprint, named here rather
#: than in the collector because the store's own retention has to know it too —
#: and a vocabulary word spelled in two files is a word that eventually differs.
FINGERPRINT_KIND = "project-fingerprints"

#: The projection contract's version — the third component of the idempotency key
#: `docs/ARCHITECTURE.md:198` declares for the outbox. It is named HERE for the
#: reason above, and the reason was not hypothetical: `store/ledger.py` wrote the
#: literal `1` into every outbox row while `store/indexer.py` filtered on its own
#: `PROJECTION_VERSION` constant. Turning the declared knob to 2 therefore made
#: the pending query match nothing, and the indexer printed *"outbox empty —
#: every committed revision is already projected"* and exited 0. A knob that
#: silently stops the work it versions, and reports success. Measured 2026-09-07.
PROJECTION_VERSION = 1


def connect(path: pathlib.Path | None = None) -> sqlite3.Connection:
    """Open a compatible store, backing up existing data before an atomic upgrade.

    The process lock coordinates upgraded versions. SQLite's write transaction
    also excludes other database writers during migration. Stop old schedulers
    before upgrading: already-running historical executables cannot know this
    lock or the forward-version refusal introduced here.
    """
    from store import migrate, compatibility
    target = pathlib.Path(path or DB_PATH)
    if target.is_symlink():
        raise migrate.CompatibilityError("Database path must not be a symbolic link")
    # Fast refusal of an unsupported version before waiting for the lock. A file
    # another process is creating right now can be unreadable for a moment; the
    # check repeated under the lock below is the one that decides.
    try:
        compatibility.preflight(target)
    except sqlite3.DatabaseError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    with compatibility.upgrade_lock(target):
        compatibility.preflight(target)
        fresh = not target.exists()
        if fresh:
            fd = osprivacy.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | osprivacy.NOFOLLOW, 0o600)
            os.close(fd)
        else:
            if not target.stat().st_mode & 0o200:
                raise PermissionError("Database is read-only; refusing to change its write permission")
            target.chmod(0o600)
        conn = sqlite3.connect(target, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            migrate.validate(conn)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not fresh and tables and migrate.needs_upgrade(conn):
                compatibility.backup(conn, target)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA secure_delete=ON")
            if "scans" not in tables:
                conn.executescript(SCHEMA.read_text(encoding="utf-8"))
                conn.commit()
            migrate.apply(conn)
            target.chmod(0o600)
            return conn
        except BaseException:
            conn.close()
            raise


def scan_id(prefix: str, at: str) -> str:
    """A `scans.id` nothing can collide with, minted in ONE place.

    Trap: T12

    `scans.id` is a TEXT PRIMARY KEY and a timestamp is not a unique id. The
    defect was recorded on 2026-09-04 after `compute_deltas` collided on it —
    a launchd tick and a manual run landing in the same second is exactly what
    this estate produces — and it was fixed there and in `scan_events` by
    appending six random hex.

    It then came back a third time. `collectors/scan_sessions.py` was written
    on 2026-09-06 and minted `f"sessions-{now()}"`, with the insert inside a
    `try/finally` that has no `except`: two runs in one second would have
    raised `IntegrityError: UNIQUE constraint failed: scans.id` and killed the
    step. Measured 2026-09-08 — 105 sessions scans, every one in a distinct
    second, so it had not fired yet.

    Two fixes and a re-occurrence is the signature of a fix applied to
    instances rather than to the class. The clock stays with the caller —
    `at` is the collector's own `now()` — and the only job here is the part
    that got forgotten twice.
    """
    return f"{prefix}-{at}-{uuid.uuid4().hex[:6]}"


def latest_scan(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT id FROM scans WHERE finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


def embedding_contract(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT embedding_provider, embedding_model, dims FROM vec_meta LIMIT 1").fetchone()
    return dict(row) if row else {}
