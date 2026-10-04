#!/usr/bin/env python3
"""The indexer under a queue bigger than one run, and under a provider that fails.

`store/indexer.py` announces a degradation — *"embedding unavailable; writing the
lexical index only"* — and until 2026-09-07 it had only ever been driven against
a two-row fixture. Against a real queue, three of its claims were tested and one
held.

* **Completeness across runs HELD.** 1,200 queued, `limit=500`: three runs drain
  it exactly, with no duplicate and no loss.

* **The cap was silent.** The run printed `indexed 500 revision(s) … checkpoint
  500` and said nothing about the 700 still queued. A caller reading that line
  would reasonably conclude the queue was drained — the "no silent caps" rule,
  broken in the one place where load makes it matter.

* **The degradation became PERMANENT.** A provider failure was caught, the
  lexical index was written, and the outbox rows were consumed anyway. Measured:
  a 200-row queue with the provider failing from the second batch left 200
  lexical rows, 64 vectors, an EMPTY outbox, and a summary claiming "indexed 200
  revision(s)". Nothing would ever have re-embedded those 136 — the vector half
  of search was gone for them until a full `reindex`, and no output said so.

The distinction that decides the fix: a batch whose vector half FAILED keeps its
rows queued, while a machine with no sqlite-vec at all consumes them. The second
is a property of the machine, announced at the top of every run; holding those
rows would grow the queue for ever and keep `projection.lagging` lit.
"""
from __future__ import annotations
import importlib, importlib.util, os, pathlib, sqlite3, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402
sys.path.insert(0, str(ROOT / "agent"))

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def fixture(rows: int):
    """A queue bigger than one run, over the real schema."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-idxload-"))
    os.environ["OBSERVATORY_DB"] = str(d / "observatory.db")
    import paths
    importlib.reload(paths)
    from store import db as sdb
    importlib.reload(sdb)
    from store import ledger as L
    importlib.reload(L)
    conn = sdb.connect()
    for i in range(rows):
        L.append(conn, owner="agent:observer", statement=f"a measured statement {i}",
                 why="because the estate was scanned", state="proposed",
                 confidence=0.5, project_id="project:alpha")
    conn.commit()
    import embedding_consent
    embedding_consent.grant("project:alpha")
    return d, conn


def load_indexer(embed):
    spec = importlib.util.spec_from_file_location("ix_load_t", ROOT / "store/indexer.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ix_load_t"] = mod
    spec.loader.exec_module(mod)
    mod.providers.embed = embed
    return mod


def stub(texts, log=print, **_kw):
    return {"vectors": [[0.01] * 1536 for _ in texts], "cost": 0.0,
            "tokens": len(texts), "model": "stub", "cost_is_estimate": True}


def counts(conn) -> tuple[int, int, int]:
    lex = conn.execute("SELECT count(*) FROM search_notes").fetchone()[0]
    try:
        vec = conn.execute("SELECT count(*) FROM vec_notes").fetchone()[0]
    except sqlite3.Error:
        vec = -1
    queued = conn.execute(
        "SELECT count(*) FROM outbox WHERE consumed_at IS NULL").fetchone()[0]
    return lex, vec, queued


def test_repeated_runs_drain_the_queue_exactly() -> None:
    """The one claim that held on first contact with load."""
    d, conn = fixture(1200)
    ix = load_indexer(stub)
    seen = []
    for _ in range(5):
        ix.cmd_index(conn, limit=500)
        lex, vec, queued = counts(conn)
        seen.append((lex, queued))
        if queued == 0:
            break
    check("the queue drains", seen[-1][1] == 0, str(seen))
    check("in three runs of 500 over 1,200 rows", len(seen) == 3, str(seen))
    lex, vec, _ = counts(conn)
    check("every revision reached the lexical index", lex == 1200, str(lex))
    if vec >= 0:
        check("and the vector one", vec == 1200, str(vec))
    dup = conn.execute("SELECT count(*) FROM (SELECT memory_id, revision"
                       " FROM search_notes GROUP BY 1,2 HAVING count(*) > 1)").fetchone()[0]
    check("nothing was indexed twice", dup == 0, str(dup))
    conn.close()


def test_a_capped_run_says_what_it_left(capsys=None) -> None:
    """`indexed 500` with 700 queued and no word about it is a silent cap."""
    d, conn = fixture(700)
    ix = load_indexer(stub)
    import io
    import contextlib
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ix.cmd_index(conn, limit=500)
    said = err.getvalue() + out.getvalue()
    check("the run reports the remainder", "200 revision(s) still queued" in said,
          said[-200:])
    check("and names the cap that caused it", "capped at 500" in said, said[-200:])
    check("and how to finish", "index` again" in said, said[-200:])

    # And it must NOT say that when the queue really is drained.
    err2 = io.StringIO()
    with contextlib.redirect_stderr(err2):
        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2):
            ix.cmd_index(conn, limit=500)
    said2 = err2.getvalue() + out2.getvalue()
    check("a complete run claims no remainder", "still queued" not in said2, said2[-200:])
    conn.close()


def test_a_failed_vector_half_keeps_its_rows_queued() -> None:
    """The permanent-degradation defect, driven end to end."""
    d, conn = fixture(200)
    calls = {"n": 0}
    import providers

    def flaky(texts, log=print, **_kw):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise providers.Retryable("the provider dropped the connection")
        return stub(texts)

    ix = load_indexer(flaky)
    ix.cmd_index(conn, limit=500)
    lex, vec, queued = counts(conn)
    if vec < 0:
        print("  SKIP  sqlite-vec is not loadable here, so there is no vector half to lose")
        conn.close()
        return
    check("the lexical index has everything", lex == 200, str(lex))
    check("the vector index has only the first batch", 0 < vec < 200, str(vec))
    check("and the rest is STILL QUEUED rather than consumed", queued == lex - vec,
          f"{queued} queued, {lex - vec} missing a vector")

    # The provider returns; the next run must finish the job.
    ix.providers.embed = stub
    ix.cmd_index(conn, limit=500)
    lex2, vec2, queued2 = counts(conn)
    check("a later run embeds what was held", vec2 == 200, str(vec2))
    check("the queue is then empty", queued2 == 0, str(queued2))
    dup = conn.execute("SELECT count(*) FROM (SELECT memory_id, revision"
                       " FROM search_notes GROUP BY 1,2 HAVING count(*) > 1)").fetchone()[0]
    check("and nothing was written twice", dup == 0, str(dup))
    conn.close()


def test_the_summary_counts_both_halves() -> None:
    """One number for two indexes hid the half a caller could not see."""
    d, conn = fixture(200)
    calls = {"n": 0}
    import providers

    def flaky(texts, log=print, **_kw):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise providers.Retryable("down")
        return stub(texts)

    ix = load_indexer(flaky)
    import io
    import contextlib
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        ix.cmd_index(conn, limit=500)
    said = out.getvalue() + err.getvalue()
    _, vec, _ = counts(conn)
    if vec < 0:
        print("  SKIP  no vector half here — sqlite-vec is not loadable, "
              "so `vec_notes` does not exist")
        conn.close()
        return
    check("the summary does not claim 200 into both indexes",
          "indexed 200 revision(s) into both" not in said, said[:200])
    check("it names the lexical-only remainder", "lexical-only" in said, said[:300])
    check("and the queue explains itself", "kept in the queue" in said, err.getvalue()[:200])
    conn.close()


def test_a_machine_without_sqlite_vec_still_drains() -> None:
    """The distinction the fix rests on: a missing extension is not a failure.

    Holding rows for a machine that will never have a vector index would grow
    the queue without bound and keep `projection.lagging` lit for ever.
    """
    d, conn = fixture(120)
    ix = load_indexer(stub)
    ix.load_vec = lambda conn: False          # as if the extension were absent
    ix.cmd_index(conn, limit=500)
    _, _, queued = counts(conn)
    check("the outbox is consumed even with no vector index", queued == 0, str(queued))
    import io, contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        ix.cmd_index(conn, limit=500)
    check("and the summary does not claim two indexes on a one-index machine",
          "both indexes" not in out.getvalue(), out.getvalue()[:160])
    lex = conn.execute("SELECT count(*) FROM search_notes").fetchone()[0]
    check("and the lexical index is complete", lex == 120, str(lex))
    conn.close()


def test_the_lag_finding_now_means_what_it_says() -> None:
    """`projection.lagging` is raised by AGE; until this change a provider
    failure emptied the queue, so the finding could not fire for the very
    failure that most deserved it."""
    src = (ROOT / "tools/build_findings.py").read_text(encoding="utf-8")
    check("the finding exists", "projection.lagging" in src)
    check("and blames the right cause",
          "embedding provider refusing or a reached spend ceiling" in src,
          "the detail text must survive this change, since it is now literally true")
    ix_src = (ROOT / "store/indexer.py").read_text(encoding="utf-8")
    check("the indexer keeps rows queued on a vector failure",
          "held_seqs" in ix_src and "done_seqs" in ix_src)
    check("and consumes only what was fully indexed",
          "[(now(), s) for s in done_seqs]" in ix_src,
          "consuming `seqs` was what made the degradation permanent")


if __name__ == "__main__":
    print("the indexer under load — and a degradation that must not become permanent\n")
    for fn in (test_repeated_runs_drain_the_queue_exactly,
               test_a_capped_run_says_what_it_left,
               test_a_failed_vector_half_keeps_its_rows_queued,
               test_the_summary_counts_both_halves,
               test_a_machine_without_sqlite_vec_still_drains,
               test_the_lag_finding_now_means_what_it_says):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe queue drains, the cap speaks, and a lost vector comes back\033[0m")
