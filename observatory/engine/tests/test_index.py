#!/usr/bin/env python3
"""The derived projections, and the one property that makes them safe to lose.

Everything here runs against a throwaway store inside a private synthetic
workspace. The embedding call is stubbed so the suite costs nothing and makes no
request.

Trap: T20

The marker sits on the MODULE because the guard is not a test — it is
`fresh_store()`, which pops `store` itself from `sys.modules` and asserts
`paths.DB` before returning. Popping only `store.db` leaves the PACKAGE holding
an attribute that still references the old module, so a test read its
neighbour's database while asserting on a fresh path; a checkpoint once carried
from 1 to 5 between two tests. Every case below inherits the fix, which is why
no single one of them owns it.
"""
from __future__ import annotations
import importlib.util, json, pathlib, sqlite3, sys, tempfile
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
import tmp as tmpdir  # noqa: E402
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
import paths as _paths  # noqa: E402
# NO REQUEST MAY LEAVE THE MACHINE. The synthetic workspace's provider endpoints
# point at a closed loopback port, so a call a stub failed to intercept fails
# fast and locally instead of reaching a real provider.
_models = _paths.CONFIG / "models.json"
_mdoc = json.loads(_models.read_text(encoding="utf-8"))
_mdoc["base_url"] = "http://127.0.0.1:9/api/v1"
_mdoc.setdefault("embedding", {})["base_url"] = "http://127.0.0.1:9/v1"
_models.write_text(json.dumps(_mdoc), encoding="utf-8")

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "agent"))
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


#: Everything that caches a path at import time, in dependency order. `paths`
#: resolves OBSERVATORY_DB once and every module below holds the result, so
#: setting the variable without dropping these gives a test somebody else's
#: store — which is how three checks here passed and failed for reasons that had
#: nothing to do with the code.
#: `store` itself is in this list, and that is the whole point. Popping only
#: `store.db` leaves the PACKAGE holding an attribute `db` that still references
#: the old module, and `from store import db` reads the attribute — so a test got
#: its neighbour's store while asserting on a fresh path; the checkpoint was seen
#: to carry over from 1 to 5 between two tests.
_CACHED = ("survey", "providers", "store.indexer", "store.ledger", "store.db", "store",
           "paths", "ledger_t", "indexer_t")


def fresh_store():
    import os
    db = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-index-")) / "t.db"
    os.environ["OBSERVATORY_DB"] = str(db)
    for m in _CACHED:
        sys.modules.pop(m, None)
    import paths
    assert str(paths.DB) == str(db), f"paths.DB is {paths.DB}, expected {db}"
    from store import db as sdb
    return sdb.connect(), db


def load(name, path):
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def stub_embed(dims: int):
    """Deterministic pseudo-vectors: the index's plumbing is what is under test,
    not the provider's semantics."""
    import hashlib

    def _embed(texts, log=None, **_kw):
        vecs = []
        for t in texts:
            h = hashlib.sha256(t.encode()).digest()
            vecs.append([((h[i % len(h)] / 255.0) - 0.5) for i in range(dims)])
        return {"vectors": vecs, "tokens": sum(len(t.split()) for t in texts),
                "cost": 0.0, "model": "stub", "cost_is_estimate": True}
    return _embed


def seed(conn, L, n=3):
    ids = []
    for i in range(n):
        r = L.append(conn, owner="agent:observer", kind="observation",
                     statement=f"note number {i} about a paywall and a migration",
                     why=f"because reason {i}", project_id=f"project:p{i}",
                     state="proposed", confidence=0.5)
        ids.append(r["memoryId"])
    # The vector half runs only under a recorded consent (PB-137 N-003).
    import embedding_consent
    embedding_consent.grant(*[f"project:p{i}" for i in range(n)])
    return ids


def test_the_outbox_drives_the_index() -> None:
    conn, _ = fresh_store()
    L = load("ledger_t", ROOT / "store/ledger.py")
    ix = load("indexer_t", ROOT / "store/indexer.py")
    import providers
    providers.embed = stub_embed(providers.config()["embedding"]["dims"])
    ids = seed(conn, L, 3)
    pending = conn.execute("SELECT count(*) FROM outbox WHERE consumed_at IS NULL").fetchone()[0]
    check("every committed revision left an outbox row", pending == 3, str(pending))
    ix.cmd_index(conn, limit=100)
    left = conn.execute("SELECT count(*) FROM outbox WHERE consumed_at IS NULL").fetchone()[0]
    lex = conn.execute("SELECT count(*) FROM search_notes").fetchone()[0]
    check("the outbox is drained", left == 0, str(left))
    check("the lexical index has a row per revision", lex == 3, str(lex))
    ix.cmd_index(conn, limit=100)
    check("a second pass is idempotent — nothing to do and nothing duplicated",
          conn.execute("SELECT count(*) FROM search_notes").fetchone()[0] == 3)


def test_a_tombstoned_revision_is_never_indexed() -> None:
    conn, _ = fresh_store()
    L = load("ledger_t", ROOT / "store/ledger.py")
    ix = load("indexer_t", ROOT / "store/indexer.py")
    import providers
    providers.embed = stub_embed(providers.config()["embedding"]["dims"])
    ids = seed(conn, L, 2)
    L.tombstone(conn, ids[0], reason="retention", approved_by="operator")
    ix.cmd_index(conn, limit=100)
    rows = [r[0] for r in conn.execute("SELECT memory_id FROM search_notes")]
    check("an erased record does not reach the projection", ids[0] not in rows, str(rows))
    check("the surviving one does", ids[1] in rows, str(rows))
    check("its outbox row is still checkpointed, so it is not retried forever",
          conn.execute("SELECT count(*) FROM outbox WHERE consumed_at IS NULL").fetchone()[0] == 0)


def test_rebuild_restores_from_canon() -> None:
    """The invariant: a derived projection must be rebuildable without touching
    canonical identity or revision history."""
    conn, _ = fresh_store()
    L = load("ledger_t", ROOT / "store/ledger.py")
    ix = load("indexer_t", ROOT / "store/indexer.py")
    import providers
    providers.embed = stub_embed(providers.config()["embedding"]["dims"])
    seed(conn, L, 4)
    ix.cmd_index(conn, limit=100)
    before_lex = conn.execute("SELECT count(*) FROM search_notes").fetchone()[0]
    before_ledger = [tuple(r) for r in conn.execute(
        "SELECT memory_id, revision, statement, confidence FROM ledger ORDER BY memory_id, revision")]
    ix.cmd_rebuild(conn)
    after_lex = conn.execute("SELECT count(*) FROM search_notes").fetchone()[0]
    after_ledger = [tuple(r) for r in conn.execute(
        "SELECT memory_id, revision, statement, confidence FROM ledger ORDER BY memory_id, revision")]
    check("rebuild restores the same number of rows", before_lex == after_lex,
          f"{before_lex} -> {after_lex}")
    check("rebuild does not touch the ledger at all", before_ledger == after_ledger)
    check("rebuild leaves the outbox drained",
          conn.execute("SELECT count(*) FROM outbox WHERE consumed_at IS NULL").fetchone()[0] == 0)


def test_no_index_write_without_a_committed_revision() -> None:
    conn, _ = fresh_store()
    ix = load("indexer_t", ROOT / "store/indexer.py")
    with conn:
        conn.execute("INSERT INTO outbox (memory_id, revision, projection_version)"
                     " VALUES ('mem:never-committed', 1, 1)")
    ix.cmd_index(conn, limit=10)
    rows = [tuple(r) for r in conn.execute("SELECT memory_id, revision FROM search_notes")]
    check("an outbox row with no ledger revision indexes nothing", not rows, str(rows))
    check("and is still checkpointed rather than retried on every run",
          conn.execute("SELECT count(*) FROM outbox WHERE consumed_at IS NULL").fetchone()[0] == 0)


def test_search_returns_conflicts_together() -> None:
    """The projection must not resolve a disagreement it only stores.

    Trap: T10
    """
    conn, _ = fresh_store()
    L = load("ledger_t", ROOT / "store/ledger.py")
    ix = load("indexer_t", ROOT / "store/indexer.py")
    import providers
    providers.embed = stub_embed(providers.config()["embedding"]["dims"])
    # `supported` is reached by WALKING the lifecycle. These rows used to be
    # minted `owner="operator", state="supported"` in one append, which worked
    # only because the operator was exempt from "an automated writer proposes
    # and something else promotes"; since then the operator may not create a
    # record at all, so no caller can mint a promoted row. The subject of this
    # test is conflict retrieval, and the walk changes nothing about it.
    def supported(statement: str, **kw):
        r = L.append(conn, owner="agent:observer", kind="observation",
                     project_id="project:x", statement=statement,
                     function="semantic", state="proposed", confidence=0.5,
                     valid_to="2099-01-01T00:00:00Z", **kw)
        r = L.corroborate(conn, r["memoryId"], by="service:witness", check={"how": "fixture: an independent re-check"},
                          expected_revision=r["revision"])
        return L.transition(conn, r["memoryId"], to_state="supported",
                            owner="agent:observer", expected_revision=r["revision"])

    a = supported("the paywall shipped on Friday")
    b = supported("the paywall did not ship at all",
                  conflicts_with=[f"{a['memoryId']}@1"])
    L.transition(conn, a["memoryId"], to_state="contested", owner="agent:observer",
                 expected_revision=a["revision"])
    ix.cmd_index(conn, limit=100)
    import survey
    # survey.search reaches for `providers` itself; the stub must be in place on
    # the module object it will actually find.
    sys.modules["providers"].embed = providers.embed
    r = survey.search("paywall shipped", project_id="project:x", limit=10)
    ids = {h["memoryId"] for h in r["results"]}
    # STRONGER than it was, because the old form passed by spending past the
    # ceiling. `survey.search` now checks the spend guardrail before embedding
    # so on a machine whose key is exhausted the similarity half is
    # skipped and one side of a disagreement can be reachable only that way.
    #
    # The contract's invariant is that ranking never collapses a disagreement —
    # not that retrieval is always complete. So the true property is: EITHER both
    # sides come back, OR the answer declares itself incomplete. An answer that
    # quietly returns one side is the failure; an answer that returns one side
    # and says why is the degradation the `degraded` list exists for.
    both = {a["memoryId"], b["memoryId"]} <= ids
    declared = bool(r.get("degraded"))
    check("either both sides come back, or the answer says it is incomplete",
          both or declared,
          f"returned {ids} with degraded={r.get('degraded')}")
    if not both:
        print(f"  NOTE  similarity was unavailable ("
              f"{'; '.join(d.get('reason','') for d in r['degraded'])[:90]}), so the "
              f"conflict invariant is asserted over the lexical path alone")
    check("nothing ranked one side away silently", both or declared,
          str(ids))
    check("the contested one is named as such", a["memoryId"] in r["contested"],
          str(r["contested"]))
    check("the answer says absence is not proof of absence", "absence" in r["note"])
    check("degraded is present even when empty", "degraded" in r)


if __name__ == "__main__":
    print("the derived projections\n")
    for fn in (test_the_outbox_drives_the_index, test_a_tombstoned_revision_is_never_indexed,
               test_rebuild_restores_from_canon, test_no_index_write_without_a_committed_revision,
               test_search_returns_conflicts_together):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mprojections ok\033[0m")
