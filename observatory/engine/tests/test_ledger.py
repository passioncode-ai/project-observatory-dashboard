#!/usr/bin/env python3
"""The write path's invariants. Each one is a rule something else already paid for.

Runs against a throwaway database, never the live store.
"""
from __future__ import annotations
import pathlib, sqlite3, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup
portable_setup()
from store import ledger as L                                                      

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def fresh() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript((ROOT / "store/schema.sql").read_text(encoding="utf-8"))
    return conn


def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc as e:
        return True, str(e)
    except Exception as e:                                                                   
        return False, f"raised {type(e).__name__}: {e}"
    return False, "did not raise"


def supported(conn, statement: str, **kw):
    """A `supported` row, reached the only way there is: by walking.

    The operator may not create a record from nothing, so a fixture cannot mint
    one with `owner="operator", state="supported"`. It proposes, then promotes,
    which is the path real rows take anyway.
    """
    r = conn and L.append(conn, owner="agent:observer", statement=statement,
                          state="proposed", confidence=0.5, **kw)
    # Out of `proposed` through an independent witness (PB-137 N-014): an author may
    # not promote its own proposal.
    r = L.corroborate(conn, r["memoryId"], by="service:witness", check={"how": "fixture: an independent re-check"},
                      expected_revision=r["revision"])
    return L.transition(conn, r["memoryId"], to_state="supported", owner="agent:observer",
                        expected_revision=r["revision"])


def test_owner_required() -> None:
    """An unnamed writer must be refused, never defaulted to the operator."""
    conn = fresh()
    ok, why = raises(L.OwnerRequired, L.append, conn, owner="", statement="x")
    check("T7 an empty owner is refused, not defaulted", ok, why)
    ok, why = raises(L.OwnerRequired, L.append, conn, owner="   ", statement="x")
    check("T7 a whitespace owner is refused too", ok, why)


def test_operator_rows_are_sacred() -> None:
    conn = fresh()
    # Built the way an operator row ARISES: an agent proposes, the operator
    # promotes, and the new revision is theirs. A brand-new operator-owned
    # record is refused: the operator's authority applies to records that
    # already exist, and minting one from nothing would be a forgery any MCP
    # client could perform by typing the word. What this test checks, that such
    # a row is sacred once it exists, does not depend on how it was made.
    seed = L.append(conn, owner="agent:observer", statement="the paywall ships on Friday",
                    function="semantic", state="proposed", confidence=0.5,
                    valid_to="2099-01-01T00:00:00Z")
    r = L.transition(conn, seed["memoryId"], to_state="observed", owner=L.OPERATOR,
                     expected_revision=seed["revision"], why="the operator decided")
    ok, why = raises(L.OwnerRefused, L.append, conn, memory_id=r["memoryId"],
                     expected_revision=r["revision"], owner="agent:observer",
                     statement="rewritten by a robot")
    check("an automated writer may not supersede an operator row", ok, why)
    got = L.append(conn, memory_id=r["memoryId"], expected_revision=r["revision"],
                   owner="operator", statement="the paywall slipped to Monday",
                   function="semantic", state="supported")
    # Revision 3, not 2: the row is created `proposed`, promoted to `observed`,
    # and this correction is the third revision. Minting it `supported` in one
    # append is no longer possible for anyone — an agent may not create a
    # record already promoted (the rule below), and the operator may not create
    # one at all, so the lifecycle is now the only way in.
    check("the operator may supersede their own row", got["revision"] == 3, str(got))


def test_a_writer_may_only_correct_its_own() -> None:
    conn = fresh()
    r = L.append(conn, owner="agent:a", statement="a's note")
    ok, why = raises(L.OwnerRefused, L.append, conn, memory_id=r["memoryId"],
                     expected_revision=1, owner="agent:b", statement="b's rewrite")
    check("one agent may not correct another's record", ok, why)


def test_cas() -> None:
    conn = fresh()
    r = L.append(conn, owner="agent:a", statement="v1")
    L.append(conn, memory_id=r["memoryId"], expected_revision=1, owner="agent:a", statement="v2")
    ok, why = raises(L.RevisionConflict, L.append, conn, memory_id=r["memoryId"],
                     expected_revision=1, owner="agent:a", statement="stale writer")
    check("a stale expected_revision conflicts instead of winning", ok, why)
    ok, why = raises(L.RevisionConflict, L.append, conn, memory_id=r["memoryId"],
                     owner="agent:a", statement="no expectation at all")
    check("a correction with no expected_revision is refused", ok, why)
    try:
        L.append(conn, memory_id=r["memoryId"], expected_revision=1, owner="agent:a", statement="x")
    except L.RevisionConflict as e:
        check("the conflict carries the current revision so a caller can merge",
              e.current == 2 and e.expected == 1, f"expected={e.expected} current={e.current}")


def test_append_only() -> None:
    conn = fresh()
    r = L.append(conn, owner="agent:a", statement="first")
    L.append(conn, memory_id=r["memoryId"], expected_revision=1, owner="agent:a",
             statement="second")
    rows = L.history(conn, r["memoryId"])
    check("both revisions survive; nothing is rewritten",
          [x["statement"] for x in rows] == ["first", "second"], str([x["statement"] for x in rows]))
    import json
    check("the later revision names what it supersedes",
          json.loads(rows[1]["supersedes_json"]) == [f"{r['memoryId']}@1"],
          rows[1]["supersedes_json"])


def test_lifecycle_edges() -> None:
    conn = fresh()
    r = L.append(conn, owner="agent:a", statement="an episode")
    ok, why = raises(L.IllegalTransition, L.transition, conn, r["memoryId"],
                     to_state="supported", owner="agent:a", expected_revision=1)
    check("proposed cannot jump straight to supported", ok, why)
    ok, why = raises(L.IllegalTransition, L.transition, conn, r["memoryId"],
                     to_state="observed", owner="agent:a", expected_revision=1)
    check("an author may not promote its own proposal (N-014)", ok, why)
    a = L.corroborate(conn, r["memoryId"], by="service:witness", check={"how": "fixture: an independent re-check"},
                      expected_revision=1)
    b = L.transition(conn, r["memoryId"], to_state="supported", owner="agent:a",
                     expected_revision=a["revision"])
    check("proposed -> observed -> supported walks the diagram", b["state"] == "supported",
          str(b))
    c = L.transition(conn, r["memoryId"], to_state="contested", owner="agent:a",
                     expected_revision=b["revision"])
    check("supported -> contested is a legal edge", c["state"] == "contested", str(c))
    ok, why = raises(L.IllegalTransition, L.transition, conn, r["memoryId"],
                     to_state="proposed", owner="agent:a", expected_revision=c["revision"])
    check("nothing returns to proposed", ok, why)


def test_automated_writer_cannot_self_promote() -> None:
    conn = fresh()
    ok, why = raises(L.LedgerError, L.append, conn, owner="agent:observer",
                     statement="I declare this supported", state="supported")
    check("an agent cannot create a record already supported", ok, why)


def test_outbox_is_transactional() -> None:
    conn = fresh()
    r = L.append(conn, owner="agent:a", statement="x")
    pending = conn.execute("SELECT memory_id, revision FROM outbox WHERE consumed_at IS NULL").fetchall()
    check("every committed revision leaves exactly one outbox row",
          [(p["memory_id"], p["revision"]) for p in pending] == [(r["memoryId"], 1)], str(pending))
    check("the append returns a consistency cursor", isinstance(r["consistencyCursor"], int),
          str(r))
    before = conn.execute("SELECT count(*) FROM outbox").fetchone()[0]
    raises(L.LedgerError, L.append, conn, owner="agent:a", statement="bad", function="nonsense")
    after = conn.execute("SELECT count(*) FROM outbox").fetchone()[0]
    check("a refused append leaves no outbox row", before == after, f"{before} -> {after}")


def test_tombstone_keeps_the_row() -> None:
    """An erasure leaves a tombstone; the row itself is never deleted."""
    conn = fresh()
    r = L.append(conn, owner="agent:a", statement="to be erased")
    L.tombstone(conn, r["memoryId"], reason="retention: 90d", approved_by="operator")
    check("T6 the ledger row survives an erasure",
          len(L.history(conn, r["memoryId"])) == 1)
    check("T6 a tombstoned record leaves the live view",
          all(x["memory_id"] != r["memoryId"] for x in L.live(conn)))
    ok, why = raises(L.OwnerRequired, L.tombstone, conn, r["memoryId"], reason="x", approved_by="")
    check("T6 an erasure must name who approved it", ok, why)


def test_conflicts_return_together() -> None:
    """Two supported records that disagree come back together, unranked."""
    conn = fresh()
    a = supported(conn, "the build is green", project_id="project:x", function="semantic",
                  valid_to="2099-01-01T00:00:00Z")
    b = supported(conn, "the build is red", project_id="project:x", function="semantic",
                  valid_to="2099-01-01T00:00:00Z",
                  conflicts_with=[f"{a['memoryId']}@1"])
    L.transition(conn, a["memoryId"], to_state="contested", owner="agent:observer",
                 expected_revision=a["revision"])
    ids = {x["memory_id"] for x in L.live(conn, project_id="project:x")}
    check("T10 a contested record is returned beside the one it disagrees with",
          ids == {a["memoryId"], b["memoryId"]}, str(ids))
    states = {x["memory_id"]: x["state"] for x in L.live(conn, project_id="project:x")}
    check("T10 the disagreement is visible in the states, not resolved away",
          sorted(states.values()) == ["contested", "supported"], str(states))


def test_proposal_never_touches_the_registry() -> None:
    conn = fresh()
    reg = __import__("paths").REGISTRY / "projects.json"
    before = reg.stat().st_mtime_ns
    p = L.proposals_add(conn, target_id="project:x", patch={"description": "guessed"},
                        evidence=[{"uri": "commit:abc"}], owner="agent:observer")
    check("a proposal lands as proposed", p["status"] == "proposed", str(p))
    check("a proposal does not touch registry/projects.json",
          reg.stat().st_mtime_ns == before)
    ok, why = raises(L.OwnerRequired, L.proposals_add, conn, target_id="project:x",
                     patch={}, evidence=[], owner="")
    check("a proposal must declare its owner", ok, why)


def test_corroboration_needs_a_second_witness() -> None:
    """The promise in ARCHITECTURE.md:77, and the four ways it can be faked."""
    conn = fresh()
    r = L.append(conn, owner="agent:observer", statement="the branch was pushed")

    ok, why = raises(L.OwnerRefused, L.corroborate, conn, r["memoryId"],
                     by="agent:observer", check={"how": "I checked myself"},
                     expected_revision=1)
    check("a writer cannot corroborate its own row", ok, why)

    ok, why = raises(L.OwnerRefused, L.corroborate, conn, r["memoryId"],
                     by="operator", check={"how": "I approve"}, expected_revision=1)
    check("the operator may not approve through corroboration", ok, why)

    ok, why = raises(L.LedgerError, L.corroborate, conn, r["memoryId"],
                     by="agent:checker", check={}, expected_revision=1)
    check("a corroboration must say how it verified", ok, why)

    ok, why = raises(L.RevisionConflict, L.corroborate, conn, r["memoryId"],
                     by="agent:checker", check={"how": "git cat-file"},
                     expected_revision=99)
    check("a corroboration is subject to CAS like any write", ok, why)

    out = L.corroborate(conn, r["memoryId"], by="agent:checker",
                        check={"how": "sha 8e518d9 is an ancestor of master"},
                        expected_revision=1)
    check("an independent check promotes proposed -> observed",
          out["state"] == "observed", str(out))
    # The claim does not change hands. A corroborator who became the owner could
    # then edit the statement, which is the opposite of what a witness is for.
    check("the claim stays its author's", out["owner"] == "agent:observer", str(out))
    row = L.current(conn, r["memoryId"])
    check("the corroborator is recorded in provenance",
          "agent:checker" in row["provenance_json"], row["provenance_json"])
    check("the check itself is recorded as evidence",
          "ancestor of master" in row["evidence_json"], row["evidence_json"])
    check("revision 1 still says proposed, untouched",
          L.history(conn, r["memoryId"])[0]["state"] == "proposed")

    ok, why = raises(L.IllegalTransition, L.corroborate, conn, r["memoryId"],
                     by="agent:checker", check={"how": "again"}, expected_revision=2)
    check("an already-observed row is not corroborated twice", ok, why)


def test_export_writes_a_registry_outside_the_program() -> None:
    """The workspace registry is not under the engine; the export must still exit 0."""
    import os, subprocess
    work = pathlib.Path(tempfile.mkdtemp(prefix="observatory-ledger-export-")).resolve()
    (work / "registry").mkdir()
    env = {**os.environ, "OBSERVATORY_REGISTRY": str(work / "registry"),
           "OBSERVATORY_DB": str(work / "store/observatory.db")}
    p = subprocess.run([sys.executable, str(ROOT / "tools/export_ledger.py")], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    check("export to a registry outside the program exits 0", p.returncode == 0,
          (p.stdout + p.stderr)[-300:])
    check("and writes the ledger there", (work / "registry/ledger.jsonl").is_file())


if __name__ == "__main__":
    print("ledger invariants\n")
    for fn in (test_owner_required, test_operator_rows_are_sacred,
               test_a_writer_may_only_correct_its_own, test_cas, test_append_only,
               test_lifecycle_edges, test_automated_writer_cannot_self_promote,
               test_outbox_is_transactional, test_tombstone_keeps_the_row,
               test_conflicts_return_together, test_proposal_never_touches_the_registry,
               test_corroboration_needs_a_second_witness,
               test_export_writes_a_registry_outside_the_program):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mledger ok\033[0m")
