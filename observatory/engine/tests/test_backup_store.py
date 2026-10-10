#!/usr/bin/env python3
"""`tools/backup_store.py` — the one copy of what git cannot hold, tested at last.

The tool kept the ledger's only backups for a week with no test naming it
. Driven on a planted store: a
copy is taken through SQLite's backup API, the newest three are kept, SQLite's
own `-wal`/`-shm` companions are never mistaken for backups, a second run in
the same minute takes nothing, and an empty result is removed rather than
counted.
"""
from __future__ import annotations
import importlib.util
import os
import pathlib
import sqlite3
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def load():
    spec = importlib.util.spec_from_file_location("backup_store", ROOT / "tools/backup_store.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def planted() -> pathlib.Path:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-backup-"))
    db = d / "observatory.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    c.executemany("INSERT INTO t (v) VALUES (?)", [("a",), ("b",), ("c",)])
    c.commit(); c.close()
    return db


def run(db: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(ROOT / "tools/backup_store.py"), *args], cwd=ROOT,
                          env={**os.environ, "OBSERVATORY_DB": str(db)},
                          capture_output=True, text=True, timeout=120)


def test_a_copy_is_taken_and_reads_back_whole() -> None:
    bs = load()
    db = planted()
    dest = bs.take(db, "2026-09-14T1200Z")
    check("the backup is written beside the database with the prefix",
          dest.name == bs.PREFIX + "2026-09-14T1200Z" and dest.parent == db.parent, dest.name)
    c = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    check("and it holds every row", c.execute("SELECT count(*) FROM t").fetchone()[0] == 3)
    c.close()


def test_companions_are_never_counted_as_backups() -> None:
    bs = load()
    db = planted()
    store = db.parent
    for name in ("2026-09-11T1000Z", "2026-09-12T1000Z"):
        (store / f"{bs.PREFIX}{name}").write_bytes(b"x")
    (store / f"{bs.PREFIX}2026-09-12T1000Z-wal").write_bytes(b"x")
    (store / f"{bs.PREFIX}2026-09-12T1000Z-shm").write_bytes(b"x")
    rows = bs.existing(store)
    check("SQLite's -wal/-shm beside a copy are not backups",
          [r.name for r in rows] == [f"{bs.PREFIX}2026-09-12T1000Z", f"{bs.PREFIX}2026-09-11T1000Z"],
          str([r.name for r in rows]))


def test_the_newest_three_are_kept_and_the_rest_pruned_only_after_the_new_one_exists() -> None:
    bs = load()
    db = planted()
    store = db.parent
    for name in ("2026-09-10T1000Z", "2026-09-11T1000Z", "2026-09-12T1000Z"):
        (store / f"{bs.PREFIX}{name}").write_bytes(b"old")
    p = run(db)
    check("the run succeeds", p.returncode == 0, p.stderr[-200:])
    names = sorted(x.name for x in bs.existing(store))
    check("three remain, the oldest is gone, the new one is among them",
          len(names) == bs.KEEP and f"{bs.PREFIX}2026-09-10T1000Z" not in names
          and any(n > f"{bs.PREFIX}2026-09-12" for n in names), str(names))
    p2 = run(db)
    check("a second run inside the same minute takes nothing",
          p2.returncode == 0 and "already exists" in p2.stdout, p2.stdout[-120:])


def test_a_missing_store_is_not_a_failure_and_list_names_what_exists() -> None:
    bs = load()
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-backup-"))
    p = run(d / "observatory.db")
    check("no store to back up exits 0 — a fresh clone is not red", p.returncode == 0 and "no store" in p.stderr, p.stderr[-120:])
    db = planted()
    check("--list on an empty store says so", "no backup exists" in run(db, "--list").stdout)
    bs.take(db, "2026-09-14T1300Z")
    out = run(db, "--list").stdout
    check("and names each copy with its size", "2026-09-14T1300Z" in out and "MB" in out, out[-120:])


def backup_gate(src: str) -> tuple[str, list[str]]:
    """The `if` condition that encloses the tick's `step("backup", …)` call, and that block's
    statements, as source text. ("", []) when the step is not inside an `if` of its own.

    Audit A24: the earlier check asked only whether a daily age gate occurred anywhere in the
    tick, and it does — for four OTHER steps. The backup step is gated by
    `backup_store.py --due`, so a tick that ran the backup on every tick still passed."""
    import ast
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        body = [ast.unparse(s) for s in node.body]
        if any(b.startswith("step('backup'") for b in body):
            return ast.unparse(node.test), body
    return "", []


def test_the_tick_takes_one_a_day() -> None:
    src = (ROOT / "tools/tick.py").read_text(encoding="utf-8")
    condition, body = backup_gate(src)
    check("the tick's backup step runs only inside the `backup_store.py --due` question",
          "'tools/backup_store.py', '--due'" in condition and any(l.startswith("step('backup'") for l in body),
          condition or "the backup step is not inside an `if` of its own")
    check("and that block runs nothing else", len(body) == 2 and body[0].startswith("log("), str(body))
    # The question itself: due with no copy, not due once one exists, due again a day later.
    db = planted()
    home = db.parent / "home"
    env = {**os.environ, "OBSERVATORY_DB": str(db), "OBSERVATORY_HOME": str(home)}
    ask = lambda: subprocess.run([PY, str(ROOT / "tools/backup_store.py"), "--due"], cwd=ROOT, env=env,
                                 capture_output=True, text=True, timeout=120).returncode
    check("--due says due (exit 0) when no copy exists", ask() == 0)
    copy = load().take(db, "2026-09-14T1400Z")
    check("--due says not due (exit 1) once a copy younger than a day exists", ask() == 1)
    old = copy.stat().st_mtime - 25 * 3600
    os.utime(copy, (old, old))
    check("--due says due again when the newest copy is older than a day", ask() == 0)


if __name__ == "__main__":
    print("the store's backups — taken through SQLite, three kept, companions ignored\n")
    for fn in (test_a_copy_is_taken_and_reads_back_whole,
               test_companions_are_never_counted_as_backups,
               test_the_newest_three_are_kept_and_the_rest_pruned_only_after_the_new_one_exists,
               test_a_missing_store_is_not_a_failure_and_list_names_what_exists,
               test_the_tick_takes_one_a_day):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe one copy git cannot hold has a test that names it\033[0m")
