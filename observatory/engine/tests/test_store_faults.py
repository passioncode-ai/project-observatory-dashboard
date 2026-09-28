#!/usr/bin/env python3
"""Four store failures in three days, and not one of them left evidence.

Read out of `store/logs/tick.log` on 2026-09-07 — five distinct error strings
across four incidents, each taking out between one and five tick steps:

    2026-09-05T11:19:12Z  OperationalError: disk I/O error            agent
    2026-09-06T17:59:08Z  DatabaseError: file is not a database       SEVEN steps:
                                                                     agent, corroborate,
                                                                     index, retention,
                                                                     findings, notify,
                                                                     projection
    2026-09-07T18:10:33Z  DatabaseError: database disk image is malformed   agent
    2026-09-07T20:26:39Z  OperationalError: disk I/O error            agent
    2026-09-07T20:26:41Z  DatabaseError: vtable constructor failed: search_notes   index

`tools/check_store.py` then reports `integrity_check` = `ok` every time, because
by the time anybody asks, the store is fine. So every post-mortem is a guess
about a machine nobody measured — including the guess this repository's own
operator was handed an hour before this file was written, which named a nearly
full volume as the cause on the strength of one correlation.

**What is missing is not resilience, it is evidence.** `agent/observe.py`'s
`note_fault` records `{project, kind, reason}` — no free space, no WAL size, no
holder count, not even its own timestamp.

An earlier draft of this docstring said three tick steps carry no sqlite guard,
and that this is why 09-06 killed five. Measured: `store/retention.py` carries
FOUR `except sqlite3` and died anyway; seven steps died, not five; and the
retention path the draft named does not exist. Guards at the query level do not
help when the file will not open at all — which is a better reason for what
follows than the one that was wrong. Resilience is deliberately NOT added here:
`file is not a database` means the file really is broken, and a step that
swallows it and carries on is worse than a step that stops. The system already
reports a stopped step (`tick.step_failed` fired correctly today).

So the instrument records and RE-RAISES. Diagnosis that changes behaviour is not
diagnosis, and a recorder that can raise is a second failure inside the first.

**No backfill.** The four incidents above stay in the ledger and in `tick.log`,
because free space at 11:19 on 2026-09-05 is unknowable now, and inventing it
beside measured rows is the one thing worse than having none.
"""
from __future__ import annotations
import json, pathlib, sqlite3, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def raised(sql: str = "SELECT * FROM nope") -> sqlite3.Error:
    """A REAL sqlite exception. `sqlite3.Error()` constructed by hand carries no
    `sqlite_errorname`; the module sets it when it raises, and that code is the
    whole point — it separates SQLITE_IOERR from SQLITE_CORRUPT from SQLITE_NOTADB,
    where the four incidents above are five different strings."""
    try:
        sqlite3.connect(":memory:").execute(sql)
    except sqlite3.Error as exc:
        return exc
    raise AssertionError("that sql did not fail")


def module():
    import store_faults
    return store_faults


def in_scratch(fn):
    """Run with the log redirected, so no test writes the live receipt."""
    sf = module()
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-faults-"))
    keep = sf.LOG
    sf.LOG = d / "store-faults.jsonl"
    try:
        return fn(sf)
    finally:
        sf.LOG = keep


# ─────────── it captures the machine, not just the message ─────────────

def test_the_record_carries_the_machine_state() -> None:
    def body(sf):
        return sf.record("index", raised())
    rec = in_scratch(body)
    if rec is None:
        check("store_faults.record returns the row it wrote", False,
              "the module must exist and report what it captured")
        return
    for field in ("at", "op", "error", "free_bytes", "db_bytes", "pid"):
        check(f"the record carries `{field}`", rec.get(field) is not None,
              json.dumps(sorted(rec), ensure_ascii=False))
    check("the sqlite error CODE is captured, not only its text",
          rec.get("sqlite_errorname") == "SQLITE_ERROR",
          str(rec.get("sqlite_errorname")))
    check("the operation is the caller's own word", rec.get("op") == "index", str(rec.get("op")))
    check("free space is measured on the STORE's volume",
          isinstance(rec.get("free_bytes"), int) and rec["free_bytes"] > 0,
          str(rec.get("free_bytes")))
    check("the holder count is an int or an explicit null",
          rec.get("holders") is None or isinstance(rec["holders"], int),
          str(rec.get("holders")))
    if rec.get("holders") is None:
        check("and when it is null the reason is recorded",
              bool(rec.get("holders_why")),
              "'nobody held it' and 'nobody could be asked' are different facts")


def test_it_appends_one_line_per_fault() -> None:
    def body(sf):
        sf.record("agent", raised())
        sf.record("retention", raised("SELECT bad syntax ("))
        return sf.LOG.read_text(encoding="utf-8")
    text = in_scratch(body)
    lines = [l for l in (text or "").splitlines() if l.strip()]
    check("two faults are two lines", len(lines) == 2, str(len(lines)))
    ok = True
    for l in lines:
        try:
            json.loads(l)
        except ValueError:
            ok = False
    check("and each line is a whole JSON document on its own", ok,
          "a half-written line would make the reader lose the rest of the file")


# ─────────── it cannot become a second failure ─────────────────────────

def test_it_never_raises_even_when_it_cannot_write() -> None:
    """A recorder that raises inside an exception handler replaces a diagnosable
    failure with an undiagnosable one."""
    sf = module()
    keep = sf.LOG
    sf.LOG = pathlib.Path("/proc/nonexistent/definitely/not/writable/x.jsonl")
    try:
        out = sf.record("agent", raised())
        check("an unwritable log returns None instead of raising", out is None, str(out))
    except Exception as exc:                                          # noqa: BLE001
        check("an unwritable log returns None instead of raising", False,
              f"{type(exc).__name__}: {exc}")
    finally:
        sf.LOG = keep


def test_it_accepts_a_plain_reason_too() -> None:
    def body(sf):
        return sf.record("tick", None, detail="the store did not open at all")
    rec = in_scratch(body)
    if rec is None:
        return
    check("a fault with no exception object is still recordable",
          "did not open" in rec.get("error", ""), str(rec.get("error")))
    check("and its sqlite code is an explicit null",
          rec.get("sqlite_errorname") is None, str(rec.get("sqlite_errorname")))


# ─────────── the reader is bounded and says so ─────────────────────────

def test_the_reader_names_its_own_limit() -> None:
    sf = module()
    src = (ROOT / "store_faults.py").read_text(encoding="utf-8")
    check("the tail the reader consults is a named constant",
          "READ_TAIL" in src, "a bare slice is a policy nobody can find")
    check("and `recent()` exists for callers", hasattr(sf, "recent"))

    def body(s):
        for i in range(5):
            s.record(f"op{i}", raised())
        return s.recent(days=30.0)
    rows = in_scratch(body)
    check("recent() returns what was recorded", isinstance(rows, list) and len(rows) == 5,
          str(len(rows) if isinstance(rows, list) else rows))


def test_a_finding_reports_the_pattern_not_the_event() -> None:
    """`tick.step_failed` already reports THIS tick's failure. This finding's
    subject is the store and its window is days, so the two do not double up on
    one incident — which is why a single fault is `info` and a recurrence is a
    warning."""
    import build_findings as B
    fn = getattr(B, "store_fault_findings", None)
    if fn is None:
        check("build_findings.store_fault_findings exists", False,
              "the log needs a reader or it is a measurement with none")
        return
    one = fn([{"at": "2026-09-07T20:26:39Z", "op": "agent", "error": "disk I/O error",
               "sqlite_errorname": "SQLITE_IOERR", "free_bytes": 12063109120,
               "db_bytes": 22831104, "wal_bytes": 0, "holders": 1, "pid": 1}])
    check("one fault is info, because the failed step already has a finding",
          all(f["severity"] == "info" for f in one), str([f["severity"] for f in one]))
    many = fn([{"at": f"2026-09-07T2{i}:00:00Z", "op": "agent",
                "error": "disk I/O error", "sqlite_errorname": "SQLITE_IOERR",
                "free_bytes": 12063109120, "db_bytes": 22831104, "wal_bytes": 0,
                "holders": 1, "pid": 1} for i in range(3)])
    check("three faults in the window is a warning, because that is a pattern",
          any(f["severity"] == "warning" for f in many), str([f["severity"] for f in many]))
    if many:
        blob = json.dumps(many, ensure_ascii=False)
        check("the finding names the sqlite code", "SQLITE_IOERR" in blob, blob[:200])
        check("and it names the conditions it measured",
              "GiB" in blob or "free" in blob.lower(),
              "the whole point of the log is that the conditions are recorded")
    check("no fault at all reports nothing", fn([]) == [], str(fn([])))


# ─────────── the caller records and RE-RAISES ──────────────────────────

def test_the_indexer_records_without_swallowing() -> None:
    src = (ROOT / "store/indexer.py").read_text(encoding="utf-8")
    check("the indexer records a store fault", "store_faults" in src,
          "line 142 propagated to EXIT 1 with nothing captured")
    # AT THE CALL SITE, not at the first mention. The first `store_faults` in the
    # file is now the import, and looking there found no `raise` — my own
    # assertion, wrong in the direction that would have passed a swallowed
    # exception once the import moved.
    call = src.find('store_faults.record("index"')
    check("the record call is in the indexer", call != -1, "the call site moved or was removed")
    if call == -1:
        return
    after = src[call:call + 200]
    check("and it re-raises rather than continuing", "raise" in after,
          "a swallowed `file is not a database` keeps a broken store in service: "
          + after[:120])


def test_the_indexers_branch_actually_fires() -> None:
    """DRIVEN, not asserted from the source. T18: a degradation nobody has
    watched work does not work, and the two assertions above only read the file.

    `index_batch` is replaced with one that raises a REAL sqlite exception, so
    the code that runs is the indexer's own `except`. First measured 2026-09-07:
    `op=index`, `SQLITE_ERROR`, free 11.07 GiB, holders 1, wal 0 — and the
    exception came back out, which is the property that keeps a broken store from
    staying in service.
    """
    sys.path.insert(0, str(ROOT / "store"))
    sys.path.insert(0, str(ROOT / "agent"))
    sf = module()
    import indexer                                                   # noqa: PLC0415
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-faults-e2e-"))
    keep_log, keep_batch = sf.LOG, indexer.index_batch
    sf.LOG = d / "store-faults.jsonl"

    def boom(*_a, **_k):
        raise raised("SELECT * FROM search_notes")

    indexer.index_batch = boom
    caught = None
    try:
        # A STORE OF ITS OWN, with one row waiting. This drove the LIVE store
        # read-only and reached `index_batch` only while the outbox happened to
        # hold something — and the tick drains it every 1800 seconds, so the
        # planted error fired or did not depending on when the suite ran.
        # Measured 2026-09-08 with 0 unconsumed rows: `cmd_index` returned at
        # `if not pending` and both assertions below read the absence of a
        # failure as a failure to record one.
        conn = sqlite3.connect(d / "store.db")
        conn.row_factory = sqlite3.Row
        conn.executescript((ROOT / "store/schema.sql").read_text(encoding="utf-8"))
        with conn:
            conn.execute(
                "INSERT INTO ledger (memory_id, revision, kind, function, scope,"
                " statement, state, owner, classification, supersedes_json,"
                " conflicts_with_json, provenance_json, evidence_json, created_at)"
                " VALUES ('mem:planted', 1, 'note', 'semantic', 'project',"
                " 'a statement to project', 'proposed', 'agent:observer', 'project-internal',"
                " '[]', '[]', '[]', '[]', '2026-09-08T00:00:00Z')")
            conn.execute(
                "INSERT INTO outbox (memory_id, revision, projection_version)"
                " VALUES ('mem:planted', 1, ?)", (indexer.PROJECTION_VERSION,))
        try:
            indexer.cmd_index(conn, 3)
        except BaseException as exc:                                  # noqa: BLE001
            caught = exc
    finally:
        indexer.index_batch = keep_batch
        log_text = sf.LOG.read_text(encoding="utf-8") if sf.LOG.is_file() else ""
        sf.LOG = keep_log

    check("the exception is re-raised, not swallowed",
          isinstance(caught, sqlite3.Error), type(caught).__name__ if caught else "nothing")
    lines = [l for l in log_text.splitlines() if l.strip()]
    check("and the fault was recorded on the way out", len(lines) >= 1, str(len(lines)))
    if not lines:
        return
    row = json.loads(lines[-1])
    check("the record names the indexer as the operation", row.get("op") == "index",
          str(row.get("op")))
    check("and carries the machine's state, which no past post-mortem had",
          isinstance(row.get("free_bytes"), int) and "wal_bytes" in row,
          json.dumps(sorted(row), ensure_ascii=False))


def test_the_agents_fault_note_records_the_machine() -> None:
    src = (ROOT / "agent/observe.py").read_text(encoding="utf-8")
    check("note_fault also writes the durable record",
          "store_faults" in src,
          "`agent.json#faults` is one run's report; the log is the history")


if __name__ == "__main__":
    print("store faults — four incidents, three days, no evidence\n")
    for fn in (test_the_record_carries_the_machine_state,
               test_it_appends_one_line_per_fault,
               test_it_never_raises_even_when_it_cannot_write,
               test_it_accepts_a_plain_reason_too,
               test_the_reader_names_its_own_limit,
               test_a_finding_reports_the_pattern_not_the_event,
               test_the_indexer_records_without_swallowing,
               test_the_indexers_branch_actually_fires,
               test_the_agents_fault_note_records_the_machine):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma store fault now leaves evidence about the machine it happened on\033[0m")
