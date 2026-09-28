#!/usr/bin/env python3
"""`observatory_recall` — the tool a caller asks "what do you know", and its two lies.

The server's own `instructions` tell every client: *"Every result carries a
`degraded` list: an empty list asserts full coverage, and a non-empty one names
the sources that could not be read. Treat a missing `degraded` field as a bug
rather than as full coverage."*

**`observatory_recall` carried none.** Measured 2026-09-07 across all five read
tools: status, project, timeline and findings each returned one; recall did not —
the tool whose own docstring says "absence here is not proof of absence", which
is precisely what `degraded` exists to express.

**And it truncated silently.** 113 live records in the store, the default answer
carried 50, and `count: 50` reported the size of its own page rather than the
size of what it knows. No total, no cursor, nothing to distinguish a complete
answer from a third of one. The same silent-cap shape already closed for the
survey, in the tool most likely to be asked a whole-estate question.

**A suspicion that was wrong, recorded so nobody re-investigates it.** I expected
these read tools to mutate the store, because `store_db.connect()` opens
read-write and applies migrations — the defect `export_ledger --check` and
`review.py digest` both had. They do not: the database is byte-identical across
a `recall` and a `findings` call. The `-wal` and `-shm` sidecars that appear are
what any reader of a WAL database creates, not a write.
"""
from __future__ import annotations
import hashlib, importlib, importlib.util, json, os, pathlib, shutil, sqlite3
import subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def fixture(rows: int) -> tuple[pathlib.Path, object]:
    """A store with `rows` live records, all in the same SECOND on purpose.

    `created_at` is second-resolution, so a cursor keyed on the timestamp alone
    would be ambiguous here — which is the whole reason the key is the pair
    `(created_at, memory_id)`. A fixture that spread the rows over distinct
    seconds would pass with the broken key.
    """
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-recall-"))
    os.environ["OBSERVATORY_DB"] = str(d / "observatory.db")
    import paths
    importlib.reload(paths)
    from store import db as sdb
    importlib.reload(sdb)
    from store import ledger as L
    importlib.reload(L)
    conn = sdb.connect()
    for i in range(rows):
        conn.execute(
            "INSERT INTO ledger (memory_id, revision, kind, project_id, function, scope,"
            " statement, state, confidence, owner, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"mem:{i:04d}", 1, "observation", "project:alpha", "semantic", "project",
             f"a statement number {i}", "proposed", 0.5, "agent:test",
             "2026-09-07T00:00:00Z"))
    conn.commit()
    conn.close()
    return d, L


def load_server():
    spec = importlib.util.spec_from_file_location("srv_recall", ROOT / "mcp/server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["srv_recall"] = mod
    spec.loader.exec_module(mod)
    return mod


# ─────────────── the server's own rule, applied to itself ───────────────

def test_every_read_tool_carries_a_degraded_list() -> None:
    m = load_server()
    rule = [l for l in m.server.instructions.splitlines() if "degraded" in l]
    check("the server tells clients a missing `degraded` is a bug",
          any("as a bug" in l for l in rule), str(rule)[:160])
    calls = {
        "observatory_recall": lambda: m.observatory_recall(limit=1),
        "observatory_findings": lambda: m.observatory_findings(),
        "observatory_timeline": lambda: m.observatory_timeline(
            projectId="project:alpha", limit=1),
    }
    for name, fn in calls.items():
        try:
            r = fn()
        except Exception as exc:
            check(f"{name} answers", False, f"{type(exc).__name__}: {exc}")
            continue
        check(f"{name} carries `degraded`", isinstance(r.get("degraded"), list),
              str(sorted(r)))


def test_an_unreadable_store_is_a_typed_answer_not_a_raise() -> None:
    """A raise becomes UnexpectedToolError, which tells the caller the SERVER
    broke rather than that something could not be read."""
    m = load_server()
    import store.db as sdb
    original = sdb.connect
    m.store_db.connect = lambda *a, **k: (_ for _ in ()).throw(
        sqlite3.OperationalError("unable to open database file"))
    try:
        r = m.observatory_recall()
    finally:
        m.store_db.connect = original
    check("the answer is typed", isinstance(r, dict) and r.get("count") == 0, str(r)[:160])
    check("and names the store as the source that failed",
          any(d.get("source") == "store" for d in r.get("degraded", [])),
          str(r.get("degraded")))
    check("carrying the reason", "unable to open" in str(r.get("degraded")),
          str(r.get("degraded")))


# ─────────────── no silent cap ──────────────────────────────────────────

def test_the_answer_reports_the_scope_not_the_page() -> None:
    d, L = fixture(113)
    m = load_server()
    r = m.observatory_recall(limit=50)
    check("the page holds what was asked for", r["count"] == 50, str(r["count"]))
    check("and the total describes the SCOPE", r["total"] == 113, str(r.get("total")))
    check("with a cursor, because more remain", bool(r.get("nextCursor")),
          "a truncated answer with no cursor is a silent cap")


def test_the_walk_is_complete_and_never_repeats() -> None:
    """The property that matters, over rows sharing one timestamp."""
    d, L = fixture(113)
    m = load_server()
    seen, cursor, rounds = [], None, 0
    while rounds < 60:
        rounds += 1
        page = m.observatory_recall(limit=17, cursor=cursor)
        seen.extend(x["memory_id"] for x in page["records"])
        cursor = page.get("nextCursor")
        if not cursor:
            break
    check("the walk terminates", cursor is None, f"{rounds} rounds")
    check("it returns every record exactly once",
          sorted(seen) == sorted(f"mem:{i:04d}" for i in range(113)),
          f"{len(seen)} walked, {len(seen) - len(set(seen))} duplicated")


def test_a_complete_answer_offers_no_cursor() -> None:
    d, L = fixture(30)
    m = load_server()
    r = m.observatory_recall(limit=200)
    check("everything fits", r["count"] == 30 and r["total"] == 30, str(r["count"]))
    check("so no cursor is offered", "nextCursor" not in r,
          "a cursor at the end has the caller asking for nothing for ever")


def test_the_cursor_key_survives_a_shared_timestamp() -> None:
    """Second-resolution `created_at` is not unique — the trap that cost
    `compute_deltas.latest_two` a random ordering until `rowid` was added."""
    src = (ROOT / "store/ledger.py").read_text(encoding="utf-8")
    check("the cursor is keyed on the PAIR",
          "(l.created_at, l.memory_id) < (?, ?)" in src, "a timestamp alone is ambiguous")
    check("and the ordering matches the key",
          "ORDER BY l.created_at DESC, l.memory_id DESC" in src,
          "a cursor over an order it does not share skips or repeats rows")
    d, L = fixture(40)
    from store import db as sdb
    conn = sdb.connect()
    stamps = {r["created_at"] for r in L.live(conn, limit=100)}
    check("the fixture really shares one timestamp", len(stamps) == 1, str(stamps))
    conn.close()


# ─────────────── the suspicion that was wrong ───────────────────────────

def test_the_read_tools_do_not_write_to_the_store() -> None:
    """Expected to be a defect; measured and found sound. Kept as a check."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-ro-"))
    # The workspace's store (the synthetic estate's, under the portable runner),
    # located through `paths` rather than a path in the source tree.
    import paths
    importlib.reload(paths)
    source = pathlib.Path(os.environ.get("OBSERVATORY_HOME", "")) / "store/observatory.db"
    if not source.is_file():
        source = paths.DB
    if not source.is_file():
        d2, _L = fixture(5)
        source = d2 / "observatory.db"
    db = d / "observatory.db"
    shutil.copy(source, db)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    subprocess.run(
        [PY, "-c",
         "import sys,importlib.util as u; sys.path.insert(0,'.');"
         "s=u.spec_from_file_location('x','mcp/server.py'); m=u.module_from_spec(s);"
         "sys.modules['x']=m; s.loader.exec_module(m);"
         "m.observatory_recall(limit=1); m.observatory_findings()"],
        cwd=ROOT, env=dict(os.environ, OBSERVATORY_DB=str(db)),
        capture_output=True, text=True, timeout=300)
    after = hashlib.sha256(db.read_bytes()).hexdigest()
    check("two read tools leave the database byte-identical", before == after,
          f"{before[:12]} -> {after[:12]}")
    # `.upgrade.lock` is the store's stable flock file: every connect takes it
    # to coordinate schema upgrades between processes and never unlinks it (a
    # waiter may already hold it open). It carries no data.
    check("the sidecars they leave are a reader's, not a writer's",
          {f.name for f in d.iterdir()} <= {"observatory.db", "observatory.db-wal",
                                            "observatory.db-shm",
                                            "observatory.db.upgrade.lock"},
          str([f.name for f in d.iterdir()]))


if __name__ == "__main__":
    print("recall — what it knows, and what it admits\n")
    for fn in (test_every_read_tool_carries_a_degraded_list,
               test_an_unreadable_store_is_a_typed_answer_not_a_raise,
               test_the_answer_reports_the_scope_not_the_page,
               test_the_walk_is_complete_and_never_repeats,
               test_a_complete_answer_offers_no_cursor,
               test_the_cursor_key_survives_a_shared_timestamp,
               test_the_read_tools_do_not_write_to_the_store):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mrecall reports the scope, offers a cursor, and admits what it could not read\033[0m")
