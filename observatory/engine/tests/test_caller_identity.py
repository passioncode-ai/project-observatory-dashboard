#!/usr/bin/env python3
"""Who is allowed to say they are the operator.

`owner == "operator"` is the highest authority in this store, and it bought
three things: permanent exemption from retention
(`retention.json: owner_exempt`, in the workspace configuration), immunity from supersession by any other
writer (`store/ledger.py:_check_owner`), and — over the wire — no confidence
discount. `mcp/server.py` accepted `owner` as a free-form string of
`min_length=1`, so any stdio client could type the word and take all three.

Meanwhile `tools/review.py` refuses to write as the operator unless it is
running on a terminal, on its own stated grounds: *"minting that from a script
would let anything with shell access forge it."* Two doors to one authority,
with opposite standards.

**What this is not.** Not a security perimeter. stdio MCP has no channel
identity — whoever spawned the process IS the caller — and any local process
running as this user can write the SQLite file directly, bypassing the ledger
entirely. It is a CORRECTNESS boundary: an agent, including a well-behaved one,
must not be able to mint the human's authority, because the distinction between
"an agent proposed this" and "a person decided it" is what the whole review
queue rests on. The realistic failure is not an attacker but a model reasoning
"I will record this as the operator so it does not expire".
"""
from __future__ import annotations
import importlib, importlib.util, json, os, pathlib, sqlite3, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def fresh():
    """A real store, so the privileges under test are the store's own."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-identity-"))
    os.environ["OBSERVATORY_DB"] = str(d / "observatory.db")
    import paths
    importlib.reload(paths)
    from store import db as sdb
    importlib.reload(sdb)
    from store import ledger as L
    importlib.reload(L)
    return d, sdb.connect(), L


def load_server():
    """Registered in `sys.modules` before executing, and that is not optional.

    pydantic resolves a decorated function's annotations against
    `sys.modules[fn.__module__].__dict__`. A module built by
    `spec_from_file_location` is not there, so the lookup gets empty globals —
    and the moment the server gained resource functions returning
    `dict[str, Any]`, importing it here died with `NameError: name 'Any' is not
    defined`. The server was fine; this loader was lying about the environment
    it loaded into, and it did so silently for as long as nothing needed a
    name resolved.
    """
    spec = importlib.util.spec_from_file_location("srv_identity", ROOT / "mcp/server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["srv_identity"] = mod
    spec.loader.exec_module(mod)
    return mod


# ─────────────────── the privileges are real, not assumed ────────────────

def test_operator_ownership_actually_buys_something() -> None:
    """If it bought nothing, the rest of this file would be theatre."""
    # The policy lives in the workspace configuration, seeded from the shipped
    # defaults — the program tree holds no per-installation retention file.
    import paths
    importlib.reload(paths)
    policy = paths.config_file("retention.json")
    if not policy.is_file():
        policy = ROOT / "defaults" / "retention.json"
    cfg = json.loads(policy.read_text(encoding="utf-8"))["ledger"]
    # ENUMERATED, so a second exemption cannot be added silently — and one was
    # added deliberately on 2026-09-08: `agent:estate-history`, whose rows say
    # "this project was on this machine and its folder is gone", so their
    # subject no longer exists and nothing can re-derive them. What
    # this case is ABOUT is the operator's privilege, and that is asserted by
    # membership; the exact list is asserted beside it so the next addition is
    # a decision rather than a habit.
    exempt = cfg.get("owner_exempt") or []
    check("retention exempts operator-owned rows at any age",
          "operator" in exempt, str(exempt))
    check("and the exemption list holds only what has been decided",
          exempt == ["operator", "agent:estate-history"], str(exempt))
    check("and `observed` is NOT independently protected, so the exemption is the "
          "only thing saving such a row",
          "observed" not in (cfg.get("never") or []), str(cfg.get("never")))

    d, conn, L = fresh()
    r = L.append(conn, owner="agent:observer", statement="a claim", state="proposed",
                 confidence=0.5)
    promoted = L.transition(conn, r["memoryId"], to_state="observed", owner=L.OPERATOR,
                            expected_revision=r["revision"], why="the operator decided")
    check("a promotion leaves the current revision operator-owned",
          conn.execute("SELECT owner FROM ledger WHERE memory_id=? AND revision=?",
                       (promoted["memoryId"], promoted["revision"])).fetchone()[0] == "operator")
    refused = None
    try:
        L.append(conn, memory_id=promoted["memoryId"], expected_revision=promoted["revision"],
                 owner="agent:observer", statement="I disagree", state="proposed")
    except Exception as exc:
        refused = f"{type(exc).__name__}: {exc}"
    check("and no other writer may supersede it", refused is not None, "the write succeeded")
    conn.close()


# ─────────────────── the canonical write path ────────────────────────────

def test_a_brand_new_operator_row_is_refused() -> None:
    d, conn, L = fresh()
    refused = None
    try:
        L.append(conn, owner="operator", statement="I, the operator, declare this",
                 state="proposed", confidence=0.5)
    except Exception as exc:
        refused = f"{type(exc).__name__}: {exc}"
    check("the ledger refuses to mint a new operator-owned record",
          refused is not None, "a caller typed the word and got the highest authority")
    if refused:
        check("and names the legitimate path", "review.py" in refused, refused)
    check("nothing landed", conn.execute("SELECT count(*) FROM ledger").fetchone()[0] == 0,
          "a refused append must leave no row")
    check("and no outbox row either",
          conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0,
          "a refused append leaves no projection work")
    conn.close()


def test_promotion_still_works_because_it_has_a_prior() -> None:
    """The invariant is about ASSERTING authority, not exercising it."""
    d, conn, L = fresh()
    r = L.append(conn, owner="agent:observer", statement="a claim", state="proposed",
                 confidence=0.5)
    out = L.transition(conn, r["memoryId"], to_state="observed", owner=L.OPERATOR,
                       expected_revision=r["revision"], why="decided at a terminal")
    check("the operator may still promote an existing record", out["revision"] == 2,
          str(out))
    # From a FRESH proposal: `rejected` is reachable from `proposed`, not from
    # `observed` — the lifecycle's legal moves out of `observed` are `supported`
    # and `archived`, and asking for the wrong edge tested the state machine
    # rather than the ownership rule.
    other = L.append(conn, owner="agent:observer", statement="another claim",
                     state="proposed", confidence=0.5)
    check("and the rejection path too",
          L.transition(conn, other["memoryId"], to_state="rejected", owner=L.OPERATOR,
                       expected_revision=other["revision"], why="no")["revision"] == 2)
    conn.close()


# ─────────────────── the wire's own complete guard ───────────────────────

def test_the_wire_refuses_the_operators_claim_in_every_spelling() -> None:
    m = load_server()
    for claim in ("operator", "Operator", "OPERATOR", " operator ", "operator\t",
                  "", "admin", "human", "operator:admin"):
        check(f"{claim!r} is refused", m._owner_error(claim) is not None, "accepted")
    for good in ("agent:claude-code", "agent:observer", "service:tick",
                 "agent:Claude-Code", "agent:o11y_2"):
        check(f"{good!r} is accepted", m._owner_error(good) is None,
              "a legitimate identity was refused")


def test_the_refusal_happens_BEFORE_the_store_is_touched() -> None:
    """A guard that refuses after the write is a receipt, not a guard."""
    d, conn, L = fresh()
    conn.close()
    m = load_server()
    for call in (lambda: m.observatory_record(owner="operator", statement="mine now"),
                 lambda: m.observatory_propose(owner="operator", targetId="project:x",
                                               patch={"lifecycle": "archived"})):
        result = call()
        check("the tool returns a typed refusal rather than raising",
              isinstance(result, dict) and result.get("error") == "owner refused",
              str(result)[:200])
        check("with a remedy naming the terminal",
              "review.py" in str(result.get("hint", "")), str(result)[:200])
        check("and a degraded list, as every answer on this wire carries",
              isinstance(result.get("degraded"), list), str(result)[:200])

    c = sqlite3.connect(f"file:{d / 'observatory.db'}?mode=ro", uri=True)
    check("no ledger row was written", c.execute("SELECT count(*) FROM ledger").fetchone()[0] == 0)
    check("and no proposal either",
          c.execute("SELECT count(*) FROM proposals").fetchone()[0] == 0)
    c.close()


def test_a_legitimate_agent_write_still_reaches_the_store() -> None:
    """A guard that refuses everything is not a guard either."""
    d, conn, L = fresh()
    conn.close()
    m = load_server()
    out = m.observatory_record(owner="agent:claude-code",
                              statement="the estate has 156 projects",
                              why="counted from the registry")
    check("an agent-owned record is accepted", "memoryId" in out, str(out)[:200])
    c = sqlite3.connect(f"file:{d / 'observatory.db'}?mode=ro", uri=True)
    row = c.execute("SELECT owner, state, confidence FROM ledger").fetchone()
    check("owned by the identity that claimed it", row[0] == "agent:claude-code", str(row))
    check("in state proposed, as nothing on this wire may promote", row[1] == "proposed", str(row))
    check("with the confidence discount applied unconditionally", row[2] == 0.5, str(row))
    c.close()


def test_the_dead_branch_and_the_stale_hint_are_gone() -> None:
    src = (ROOT / "mcp/server.py").read_text(encoding="utf-8")
    check("the unreachable operator-confidence branch is removed",
          'confidence=None if owner == "operator"' not in src,
          "a condition whose true side cannot be reached is dead data")
    check("and the error catalogue no longer advertises `operator` as an owner",
          "'agent:claude-code' or 'operator'" not in src,
          "the wire invited exactly what it now refuses")
    check("the guard is a POSITIVE rule, not a blacklist", "CALLER_ID = re.compile" in src,
          "a blacklist of one word lets its spellings through")


if __name__ == "__main__":
    print("caller identity — who may say they are the operator\n")
    for fn in (test_operator_ownership_actually_buys_something,
               test_a_brand_new_operator_row_is_refused,
               test_promotion_still_works_because_it_has_a_prior,
               test_the_wire_refuses_the_operators_claim_in_every_spelling,
               test_the_refusal_happens_BEFORE_the_store_is_touched,
               test_a_legitimate_agent_write_still_reaches_the_store,
               test_the_dead_branch_and_the_stale_hint_are_gone):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe operator's authority is not claimable by typing it\033[0m")
