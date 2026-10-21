#!/usr/bin/env python3
"""`tools/backup_store.py` — the one copy of what git cannot hold, tested at last.

The tool kept the ledger's only backups for a week with no test naming it
. Driven on a planted store: a
copy is taken through SQLite's backup API, the newest three are kept, SQLite's
own `-wal`/`-shm` companions are never mistaken for backups, a second run in
the same minute takes nothing, the next minute takes one and prunes to three
(both driven by `--stamp`, never the wall clock), and an empty result is
removed rather than counted.
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
    """Driven through `--stamp`, so the minute is the test's and not the wall clock's
    (ENG-008): the earlier version retried when the minute turned between two runs,
    and the turned-minute path itself was never asserted."""
    bs = load()
    db = planted()
    store = db.parent
    for name in ("2026-09-10T1000Z", "2026-09-11T1000Z", "2026-09-12T1000Z"):
        p = run(db, "--stamp", name)
        check(f"--stamp {name} takes that copy", p.returncode == 0 and f"backup {bs.PREFIX}{name}" in p.stdout,
              (p.stdout + p.stderr)[-200:])
    check("three copies exist before the fourth", len(bs.existing(store)) == bs.KEEP,
          str([x.name for x in bs.existing(store)]))
    again = run(db, "--stamp", "2026-09-12T1000Z")
    check("a second run inside the same minute takes nothing",
          again.returncode == 0 and "already exists" in again.stdout, (again.stdout + again.stderr)[-200:])
    check("and prunes nothing", len(bs.existing(store)) == bs.KEEP)
    turned = run(db, "--stamp", "2026-09-12T1001Z")
    check("the next minute takes a copy", turned.returncode == 0 and "pruned 1" in turned.stdout,
          (turned.stdout + turned.stderr)[-200:])
    names = [x.name for x in bs.existing(store)]
    check("exactly KEEP remain: the oldest is gone, the new one is among them",
          names == [f"{bs.PREFIX}2026-09-12T1001Z", f"{bs.PREFIX}2026-09-12T1000Z", f"{bs.PREFIX}2026-09-11T1000Z"],
          str(names))


def test_a_stamp_that_is_not_a_past_minute_is_refused() -> None:
    """`--stamp` names the minute a copy is taken in. A malformed one, or one in the
    future, would sort a copy after every real one and keep it out of the rotation for
    as long as that future lasts, so both are refused before anything is copied."""
    bs = load()
    db = planted()
    for bad, why in (("2026-09-12 10:00", "malformed"), ("2026-09-12T10:00Z", "malformed"),
                     ("2026-13-40T1000Z", "not a date"), ("2999-01-01T0000Z", "in the future")):
        p = run(db, "--stamp", bad)
        check(f"--stamp {bad!r} ({why}) is refused with exit 2 and a reason",
              p.returncode == 2 and "--stamp" in p.stderr, (p.stdout + p.stderr)[-200:])
    check("and nothing was copied", bs.existing(db.parent) == [], str(bs.existing(db.parent)))
    missing = run(db, "--stamp")
    check("--stamp with no value is refused", missing.returncode == 2, (missing.stdout + missing.stderr)[-200:])


def test_a_stamp_older_than_the_kept_window_is_refused() -> None:
    """A past `--stamp` older than every copy kept would take a copy and prune it in the
    same run while printing success (round-1 seam finding F-1 on #250): the copy that
    proves the rotation would be the one rotated away. It is refused before anything is
    copied, and the kept copies stay as they were."""
    bs = load()
    db = planted()
    for name in ("2026-09-10T1000Z", "2026-09-11T1000Z", "2026-09-12T1000Z"):
        run(db, "--stamp", name)
    before = [x.name for x in bs.existing(db.parent)]
    stale = run(db, "--stamp", "2026-09-01T1000Z")
    check("a stamp older than the kept window is refused with exit 2 and a reason",
          stale.returncode == 2 and "older than" in stale.stderr, (stale.stdout + stale.stderr)[-200:])
    check("and the kept copies are untouched", [x.name for x in bs.existing(db.parent)] == before,
          str([x.name for x in bs.existing(db.parent)]))
    between = run(db, "--stamp", "2026-09-11T1200Z")
    check("a past stamp inside the window is still taken", between.returncode == 0
          and f"backup {bs.PREFIX}2026-09-11T1200Z" in between.stdout, (between.stdout + between.stderr)[-200:])


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
    check("--due says not due (exit 3, its own code) once a copy younger than a day exists", ask() == 3)
    old = copy.stat().st_mtime - 25 * 3600
    os.utime(copy, (old, old))
    check("--due says due again when the newest copy is older than a day", ask() == 0)



def test_a_crashing_due_question_is_a_failure_not_a_no() -> None:
    """Review of #328, M3: an uncaught exception exits 1, which was the "not due" answer, so a broken
    `--due` read as "a copy exists" for ever. "Not due" is exit 3 (NOT_DUE), and anything that goes
    wrong while answering is exit 2 with the exception's type on stderr."""
    import contextlib, io
    mod = load()
    check("not due has an exit of its own, not the one a crash gives", mod.NOT_DUE not in (0, 1))

    def broken(*args, **kwargs):
        raise RuntimeError("synthetic")
    mod.due = broken
    db = planted()
    os.environ["OBSERVATORY_DB"] = str(db)
    importlib.reload(mod.paths)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = mod.main(["backup_store.py", "--due"])
    check("a crash while answering exits 2", code == 2, str(code))
    check("and says what raised", "RuntimeError" in err.getvalue(), err.getvalue())


if __name__ == "__main__":
    print("the store's backups — taken through SQLite, three kept, companions ignored\n")
    for fn in (test_a_copy_is_taken_and_reads_back_whole,
               test_companions_are_never_counted_as_backups,
               test_the_newest_three_are_kept_and_the_rest_pruned_only_after_the_new_one_exists,
               test_a_stamp_that_is_not_a_past_minute_is_refused,
               test_a_stamp_older_than_the_kept_window_is_refused,
               test_a_missing_store_is_not_a_failure_and_list_names_what_exists,
               test_the_tick_takes_one_a_day,
               test_a_crashing_due_question_is_a_failure_not_a_no):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe one copy git cannot hold has a test that names it\033[0m")
