#!/usr/bin/env python3
"""Recovery of the store's history — drilled, not described.

The store holds the half of the system a fresh clone cannot rebuild: events read
out of commit histories, ledger revisions and the review decisions made against
them. A recovery path's defects are all discovered at the moment they cannot be
fixed, so the two properties it must have are driven here rather than trusted:

  * a snapshot taken while the database is being written restores the committed
    history, and only that, into an isolated database without touching the source;
  * a backup that fails is reported as failed and leaves the last good copy alone.

The recovery document these checks originally sat beside is an operational
document of the original installation and is not shipped with the engine; its
prose checks are therefore not part of this suite.
"""
from __future__ import annotations
import importlib.util
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def test_wal_snapshot_restores_history_into_an_isolated_database() -> None:
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("recovery_backup", ROOT / "tools/backup_store.py")
    backup = importlib.util.module_from_spec(spec); spec.loader.exec_module(backup)
    with tempfile.TemporaryDirectory(prefix="observatory-recovery-drill-") as td:
        root = pathlib.Path(td)
        live = root / "observatory.db"
        with sqlite3.connect(live) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE events (id TEXT PRIMARY KEY, kind TEXT)")
            writer.execute("CREATE TABLE ledger (id TEXT, revision INTEGER, decision TEXT)")
            writer.execute("INSERT INTO events VALUES ('fixture-event', 'work.completed')")
            writer.executemany("INSERT INTO ledger VALUES ('fixture-claim', ?, ?)",
                               [(1, 'proposed'), (2, 'accepted-by-fixture-operator')])
            writer.commit()
            check("drill source has a live WAL", live.with_name(live.name + "-wal").exists())
            writer.execute("INSERT INTO events VALUES ('uncommitted-event', 'interrupted-write')")
            snapshot = backup.take(live, "synthetic-drill")
            writer.rollback()
            writer.execute("DELETE FROM ledger"); writer.commit()
            restored = root / "restored.db"
            with sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True) as src:
                check("snapshot passes integrity check", src.execute("PRAGMA integrity_check").fetchone() == ('ok',))
                with sqlite3.connect(restored) as dst:
                    src.backup(dst)
            with sqlite3.connect(restored) as dst:
                check("restoration preserves the event", dst.execute("SELECT id FROM events").fetchall() == [('fixture-event',)])
                check("restoration preserves historical revisions and review decision",
                      dst.execute("SELECT revision, decision FROM ledger ORDER BY revision").fetchall()
                      == [(1, 'proposed'), (2, 'accepted-by-fixture-operator')])
            check("restore does not overwrite the source", writer.execute("SELECT count(*) FROM ledger").fetchone() == (0,))
        corrupt = root / "corrupt.db"
        corrupt.write_bytes(b"synthetic invalid sqlite data")
        try:
            with sqlite3.connect(corrupt.as_uri() + "?mode=ro", uri=True) as src:
                valid = src.execute("PRAGMA integrity_check").fetchone() == ('ok',)
        except sqlite3.DatabaseError:
            valid = False
        check("corrupt backup cannot pass the recovery check", not valid)


def test_failed_backup_keeps_last_good_snapshots() -> None:
    with tempfile.TemporaryDirectory(prefix="observatory-recovery-failure-") as td:
        root = pathlib.Path(td)
        db = root / 'observatory.db'
        old = root / 'observatory.db.backup-last-good'
        with sqlite3.connect(old) as c:
            c.execute("CREATE TABLE evidence (id INTEGER)")
            c.execute("INSERT INTO evidence VALUES (1)")
        before = old.read_bytes()
        db.write_bytes(b'synthetic corrupt live database')
        # A workspace of its own, so no passphrase or backups root of the
        # machine running the suite can be consulted.
        env = {**os.environ, 'OBSERVATORY_DB': str(db), 'OBSERVATORY_HOME': str(root / 'home'),
               'HOME': str(root)}
        p = subprocess.run([PY, str(ROOT / 'tools/backup_store.py')], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=30)
        check("failed backup is not reported successful", p.returncode != 0, (p.stdout + p.stderr)[-200:])
        check("failed backup retains the last good snapshot unchanged", old.read_bytes() == before)


if __name__ == "__main__":
    print("recovery — the store's history comes back, and a failed copy says so\n")
    for fn in (test_wal_snapshot_restores_history_into_an_isolated_database,
               test_failed_backup_keeps_last_good_snapshots):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe committed history survives, and no good copy is lost to a bad one\033[0m")
