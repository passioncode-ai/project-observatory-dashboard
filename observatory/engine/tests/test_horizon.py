#!/usr/bin/env python3
"""One horizon, three readers, and only one of them applied the rule.

The workspace's `config/retention.json` declares `owner_exempt`, and the
exemptions are structural, not a flag at the call site — an optional guard is
one that is eventually forgotten. `ledger_candidates` honours that: the owners
are inlined into every query. The other two readers once did not:

    tools/review.py       `left = horizon - (now - made).days`, every row
    tools/build_findings  the `soon` query: `state='proposed' AND created_at < ?`

So a row retention will never touch was shown with a countdown in the digest and
could be announced on the board as "will be erased unreviewed within 14 days" —
a deadline on the operator's attention that does not exist.

`agent:estate-history` is exempt, and its rows are the reason: they say "this
project was on this machine and its folder is gone", so their subject no longer
exists and nothing can re-derive them. Erasing one for want of a decision
destroys the last evidence of work this machine did — the opposite of what a
horizon is for.

**The exemption removes the deadline, not the review.** The rows stay `proposed`,
keep their place in the queue, and the digest marks them `kept`.

So the rule has one home — `retention.days_left`, which answers `None` for "this
is never erased" — and the readers ask it instead of subtracting.
"""
from __future__ import annotations
import json
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup                 # noqa: E402
portable_setup()
import tmp as tmpdir                                                  # noqa: E402

FAILURES: list[str] = []
_CACHED = ("paths", "store.db", "store.ledger", "store.retention", "store")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def cfg() -> dict:
    """The selected workspace's retention policy, initialized from the defaults."""
    import paths
    return json.loads(paths.config_file("retention.json").read_text(encoding="utf-8"))["ledger"]


def fresh():
    """A store of its own, redirected exactly as `tests/test_retention.py` does —
    connection AND scratch, because `cmd_apply` writes a receipt the board
    reads."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-horizon-"))
    os.environ["OBSERVATORY_DB"] = str(d / "t.db")
    (d / "scratch").mkdir(exist_ok=True)
    os.environ["OBSERVATORY_SCRATCH"] = str(d / "scratch")
    for m in _CACHED:
        sys.modules.pop(m, None)
    import paths
    assert str(paths.DB) == str(d / "t.db")
    from store import db as sdb
    from store import ledger as L
    from store import retention as R
    return sdb.connect(), L, R


def live() -> None:
    """Undo the redirection, so a case about the workspace's own store reads it.

    `fresh()` sets `OBSERVATORY_DB` and never put it back, and the first version
    of the backlog case below therefore ran `build_findings.collect()` against
    the fixture store — found no backlog, printed a NOTE, and skipped the
    assertion while looking like an honest degradation — a redirection leaking
    into a case that was about something else.
    """
    for var in ("OBSERVATORY_DB", "OBSERVATORY_SCRATCH"):
        os.environ.pop(var, None)
    for m in _CACHED:
        sys.modules.pop(m, None)
    sys.modules.pop("build_findings", None)


def age(conn, memory_id: str, days: int) -> None:
    when = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        conn.execute("UPDATE ledger SET created_at = ? WHERE memory_id = ?", (when, memory_id))


# ─────────── the rule, in one place ────────────────────────────────────

def test_days_left_answers_none_where_there_is_no_deadline() -> None:
    from store import retention as R
    old = (datetime.now(timezone.utc) - timedelta(days=80)).strftime("%Y-%m-%dT%H:%M:%SZ")
    check("a normal proposed row has days left",
          R.days_left("agent:observer", "proposed", old) == 10,
          str(R.days_left("agent:observer", "proposed", old)))
    for owner in R.exempt_owners():
        check(f"an exempt owner has none — {owner}",
              R.days_left(owner, "proposed", old) is None,
              str(R.days_left(owner, "proposed", old)))
    for state in R.never_states():
        check(f"a `{state}` row has none",
              R.days_left("agent:observer", state, old) is None,
              str(R.days_left("agent:observer", state, old)))
    check("a state the policy does not mention invents no deadline",
          R.days_left("agent:observer", "invented", old) is None,
          "silence is the honest answer where the policy is silent")
    check("and an unparseable date does not become a deadline either",
          R.days_left("agent:observer", "proposed", "not a date") is None, "")


def test_the_estate_history_writer_is_exempt_and_says_why() -> None:
    """The policy file shipped as a default carries no prose note; the reason
    for the exemption is recorded in this suite's docstring instead."""
    c = cfg()
    check("`agent:estate-history` is exempt",
          "agent:estate-history" in (c.get("owner_exempt") or []),
          str(c.get("owner_exempt")))
    check("and so is the operator, whose decisions are never erased",
          "operator" in (c.get("owner_exempt") or []), str(c.get("owner_exempt")))
    from store import retention as R
    check("the policy module reads the same list",
          set(R.exempt_owners()) == set(c.get("owner_exempt") or []),
          f"{R.exempt_owners()} vs {c.get('owner_exempt')}")


# ─────────── the writer that enforces it ───────────────────────────────

def test_an_exempt_row_is_never_a_candidate_however_old() -> None:
    conn, L, R = fresh()
    for owner in ("agent:observer", "agent:estate-history"):
        r = L.append(conn, owner=owner, statement=f"a vanished project, per {owner}",
                     state="proposed", kind="estate-history", scope="global")
        age(conn, r["memoryId"], 400)
    cands = {c["owner"] for c in R.ledger_candidates(conn)}
    check("the ordinary writer's row is a candidate at 400 days",
          "agent:observer" in cands, str(cands))
    check("and the estate-history row is not",
          "agent:estate-history" not in cands, str(cands))
    conn.close()


# ─────────── the readers ───────────────────────────────────────────────

def test_the_digest_marks_an_exempt_row_kept_rather_than_counting_down() -> None:
    src = (ROOT / "tools/review.py").read_text(encoding="utf-8")
    check("the digest asks retention instead of subtracting",
          "days_left(" in src,
          "`horizon - (now - made).days` cannot know about an exemption")
    check("and prints `kept` for a row with no deadline", '"{kept} kept"' in src
          or "kept}" in src, "a countdown that does not apply is a false deadline")
    check("the header names the exempt owners",
          "exempt_owners()" in src,
          "a reader told the horizon must be told the exceptions")
    # BOTH IN ONE GROUP, which the first version dropped: it printed the age
    # window and lost the `kept` count whenever any row still had a deadline.
    check("and a group with both shows both",
          'window += f", {kept} kept"' in src, src[:0])


def test_the_board_excludes_exempt_owners_from_the_expiry_alarm() -> None:
    sys.path.insert(0, str(ROOT / "tests"))
    import source_reader
    code = source_reader.code_keeping_strings(
        (ROOT / "tools/build_findings.py").read_text(encoding="utf-8"))
    i = code.find("will be erased unreviewed within 14 days")
    check("the expiry finding exists", i != -1, "")
    check("and the query behind it excludes the exempt owners",
          "EXEMPT_OWNERS" in code,
          "the alarm could otherwise name a row that is never erased")
    check("read from the file that enforces it, not repeated",
          "retention.json" in code and "owner_exempt" in code,
          "a second copy of the list is a second thing to keep true")


def test_the_backlog_row_no_longer_claims_every_row_expires() -> None:
    """Planted rather than hoped for: a synthetic backlog above the threshold,
    so the finding exists and its wording can be read."""
    live()
    import paths
    from store import db as sdb
    from store import ledger as L
    import build_findings as B
    conn = sdb.connect()
    for i in range(B.REVIEW_BACKLOG + 2):
        r = L.append(conn, owner="agent:observer", statement=f"a synthetic reading {i}",
                     state="proposed", kind="observation", scope="global")
        age(conn, r["memoryId"], 30)
    conn.close()
    rows = [f for f in B.collect() if f["type"] == "ledger.review_backlog"]
    check("a planted backlog raises the backlog finding", bool(rows), str(paths.DB))
    if not rows:
        return
    d = rows[0]["detail"]
    check("the detail names the exemption", "never erases" in d, d[-260:])
    check("and names who is exempt", "estate-history" in d, d[-260:])


# ─────────── the column nobody reads ───────────────────────────────────

def test_the_retention_policy_column_is_gone() -> None:
    """`retention_policy` was written on every ledger row by a literal
    `"default"` in `append` — the only writer — exported into the ledger
    export, and read by nothing. A per-row policy is set at INSERT time, which
    is the "flag at the call site" the retention policy argues against, and the
    owner exemption settled it by measurement: it protected rows written BEFORE
    it existed, which a column could not."""
    sys.path.insert(0, str(ROOT / "tests"))
    import source_reader
    for rel in ("store/schema.sql", "store/ledger.py", "tools/export_ledger.py"):
        code = (source_reader.code_keeping_strings(
            (ROOT / rel).read_text(encoding="utf-8"))
            if rel.endswith(".py") else (ROOT / rel).read_text(encoding="utf-8"))
        # The schema keeps a comment saying the column stood there and why it
        # went; what must not survive is a DECLARATION.
        declared = ("retention_policy  TEXT" in code
                    if rel.endswith(".sql") else "retention_policy" in code)
        check(f"{rel} no longer declares or writes it", not declared,
              "a column nothing reads is a promise the schema does not keep")
    from store import migrate
    ids = [m[0] for m in migrate.MIGRATIONS]
    check("a migration removes it from stores that have it",
          "0007-drop-inert-retention-policy" in ids, str(ids))
    conn, L, R = fresh()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(ledger)")]
    check("a store built from the schema has no such column",
          "retention_policy" not in cols, str(cols))
    r = L.append(conn, owner="agent:observer", statement="after the drop",
                 state="proposed")
    check("and a row still appends", r["revision"] == 1, str(r))
    conn.close()
    live()

if __name__ == "__main__":
    print("the horizon — one rule, and two readers ignored it\n")
    for fn in (test_days_left_answers_none_where_there_is_no_deadline,
               test_the_estate_history_writer_is_exempt_and_says_why,
               test_an_exempt_row_is_never_a_candidate_however_old,
               test_the_digest_marks_an_exempt_row_kept_rather_than_counting_down,
               test_the_board_excludes_exempt_owners_from_the_expiry_alarm,
               test_the_backlog_row_no_longer_claims_every_row_expires,
               test_the_retention_policy_column_is_gone):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma row with no deadline is not shown one\033[0m")
