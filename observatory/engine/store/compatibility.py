"""Local upgrade coordination and WAL-aware rollback snapshots (macOS/Linux)."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import sqlite3
import tempfile
import time


def readonly_uri(target: Path) -> str:
    """Avoid creating empty WAL/SHM files when a checkpointed store is inspected.

    A nonempty WAL must remain visible. Its read-only reader may reconstruct a
    shared-memory index, but never changes database or committed WAL content.
    """
    wal = Path(str(target) + "-wal")
    shm = Path(str(target) + "-shm")
    # IMMUTABLE ONLY WHEN NOTHING CAN BE WRITING. An open WAL connection always keeps a
    # `-shm` beside the database, and its writers empty the WAL on every checkpoint, so an
    # empty WAL says nothing about whether someone writes. An immutable reader takes no
    # locks and sees none of those writes: copying a store that session MCP servers wrote to
    # tore 12 copies out of 12 in an experiment, against 0 out of 12 with locks (seen live
    # on 2026-10-05; OBS-37). With a `-shm` present the reader takes the locks, and creates
    # no file: `-shm` already exists, and a read-only connection never creates a WAL.
    immutable = _wal_bytes(wal) == 0 and not shm.exists()
    return target.resolve().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")


def _wal_bytes(wal: Path) -> int:
    """The WAL's size, 0 when there is none. One `stat`, never `exists()` then `stat()`:
    the last connection to close deletes the WAL, and in between the two calls it
    vanished — a first open raced by three others failed with FileNotFoundError
    (1 open in 360 under load, the 0.18.0 release gate, 2026-10-06)."""
    try:
        return wal.stat().st_size
    except FileNotFoundError:
        return 0


def _footprint(target: Path) -> tuple:
    """What a writer cannot avoid changing: the database file's size and mtime, the
    WAL's size and whether a `-shm` exists (an open WAL connection keeps one)."""
    try:
        st = target.stat()
        head = (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        head = (None, None)
    return head + (_wal_bytes(Path(str(target) + "-wal")), Path(str(target) + "-shm").exists())


#: Reads of one store before a changing store is reported rather than read again.
CONSISTENT_READ_ATTEMPTS = 3


def read_consistently(target: Path, read, *, attempts: int = CONSISTENT_READ_ATTEMPTS):
    """`read(conn)` on a read-only connection whose view a writer did not overtake.

    `readonly_uri` chooses `immutable` from a check made BEFORE it connects, and an
    immutable reader takes no locks: a writer that opens after the check commits
    unseen, and the answer is whole but stale — no integrity check can tell (OBS-41).
    So an immutable read counts only if the store's footprint is the same after it
    as before; otherwise it is read again, with locks once that writer keeps its
    `-shm`. A locked read is kept as it is. `read` must be safe to repeat."""
    for _ in range(attempts):
        before = _footprint(target)
        uri = readonly_uri(target)
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        try:
            result = read(conn)
        finally:
            conn.close()
        if "immutable=1" not in uri or _footprint(target) == before:
            return result
    raise sqlite3.OperationalError(
        f"{target.name} changed during each of {attempts} unlocked reads; "
        f"a writer kept opening and closing it — try again when it is quieter")


def preflight(target: Path) -> None:
    """Reject newer or altered histories before opening the database for writes."""
    if not target.exists():
        return
    from store import migrate
    read_consistently(target, migrate.validate)


@contextmanager
def upgrade_lock(target: Path, timeout: float = 30):
    """Stable flock file: never unlink a lock another waiter may have opened."""
    lock_path = target.with_name(target.name + ".upgrade.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Another Observatory process is upgrading this database")
                time.sleep(0.05)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def verify_database(conn: sqlite3.Connection) -> None:
    """Check a copied store; load only the installed sqlite-vec package if needed."""
    import re
    schemas = [row[0] or "" for row in conn.execute("SELECT sql FROM sqlite_master WHERE type='table'")]
    if any(re.search(r"\bUSING\s+vec0\b", sql, re.IGNORECASE) for sql in schemas):
        try:
            import sqlite_vec
        except ImportError:
            raise RuntimeError("This snapshot contains vector tables; install the locked sqlite-vec dependency") from None
        if not callable(getattr(conn, "enable_load_extension", None)):
            raise RuntimeError("Vector snapshots require Python SQLite loadable extension support; use an extension-enabled Python build")
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    if len(rows) != 1 or rows[0][0] != "ok":
        raise RuntimeError("Database snapshot integrity verification failed")


def backup(conn: sqlite3.Connection, target: Path) -> Path:
    """Snapshot committed WAL content and verify it. `store/retention.py` keeps the newest
    two copies and any younger than 30 days (`migration_backups_*` in retention.json)."""
    directory = target.parent / "migration-backups"
    if directory.is_symlink():
        raise RuntimeError("Migration backup directory must not be a symbolic link")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    fd, name = tempfile.mkstemp(prefix=target.name + ".before-upgrade-", suffix=".db", dir=directory)
    os.close(fd)
    dest = Path(name)
    try:
        out = sqlite3.connect(dest)
        try:
            conn.backup(out)
            verify_database(out)
        finally:
            out.close()
        dest.chmod(0o600)
        return dest
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
