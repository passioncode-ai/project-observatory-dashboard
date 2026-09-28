#!/usr/bin/env python3
"""A third of the operator's queue was residue from a policy already corrected.

On a real estate, a third of the observer records waiting for review came from
(project, day) groups holding more than one record — all of them from the days
before `agent/observe.py` gained "ONE record per project per DAY, corrected —
not a new one per tick", and none from after. So this is not a live defect; it
is what the fix left behind.

**The key is the policy's own, read rather than inferred:**

    project_id = ? AND kind = 'observation' AND owner = ?
      AND substr(created_at, 1, 10) = ?
      AND memory_id NOT IN (SELECT memory_id FROM tombstones)

Four parts. A fold on a different key would merge what the policy keeps apart.

**`superseded` is unreachable and that decided the design.** `store/ledger.py`'s
transition table lets `proposed` go only to `observed` or `rejected`;
`superseded` is reachable from `supported` and `contested` alone. So the residue
cannot be folded as supersession — the only honest exit is `rejected`.

**Which is the operator's authority, and it is not minted here.**
`require_terminal` refuses any write without a terminal because everything
`review.py` writes is owned by `operator`, "exempt from retention, and
unsupersedable by any agent". A batch reject does not weaken that: the guard is
about WHERE the command runs, not how many rows it touches. So the operator still
runs it, once per group instead of once per record.
The assertion that matters most in this file is that the guard still refuses.

**What the fold loses, stated rather than hidden.** Each record of a day is a
different moment — the tree went dirty, the work was committed, it went dirty
again — and keeping only the latest loses the intermediate readings. That is
exactly what the current policy does every day: it corrects one record rather
than appending, so only the latest reading survives. Reproducing that outcome is
the point, not a side effect.
"""
from __future__ import annotations
import json, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup, PROJECT_ID  # noqa: E402
portable_setup()
FAILURES: list[str] = []


def plant_queue() -> int:
    """A review queue in the synthetic workspace store, with residue in it.

    Enough proposed observer readings to cross the backlog threshold, three of
    them sharing one (project, day) — the shape the one-per-day policy no
    longer creates — so the digest and the board have a residue to report.
    Returns how many records the fold would reject.
    """
    import sqlite3
    import paths
    from store import db as sdb
    from store import ledger as L
    import build_findings as B
    conn = sdb.connect()
    ids = []
    for i in range(B.REVIEW_BACKLOG + 3):
        r = L.append(conn, owner="agent:observer", statement=f"synthetic reading {i}",
                     state="proposed", kind="observation", project_id=PROJECT_ID)
        ids.append(r["memoryId"])
    with conn:
        for n, mid in enumerate(ids):
            day = "2026-01-05" if n < 3 else f"2026-01-{10 + n:02d}"
            conn.execute("UPDATE ledger SET created_at = ? WHERE memory_id = ?",
                         (f"{day}T{10 + n % 10:02d}:00:00Z", mid))
    conn.close()
    return 2


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def E():
    import estate
    return estate


PLANTED: dict[str, int] = {}


def rec(mid, project, day, *, kind="observation", owner="agent:observer", rev=1):
    return {"memory_id": mid, "project_id": project, "kind": kind, "owner": owner,
            "created_at": f"{day}T12:00:00Z", "revision": rev}


# ─────────── the key is the policy's ───────────────────────────────────

def test_the_key_has_all_four_parts() -> None:
    e = E()
    fn = getattr(e, "residue_key", None)
    if fn is None:
        check("estate.residue_key exists", False,
              "a fold on a key of its own would merge what the policy keeps apart")
        return
    a = rec("m1", "project:x", "2026-09-05")
    check("the same project, kind, owner and day is one key",
          fn(a) == fn(rec("m2", "project:x", "2026-09-05")), str(fn(a)))
    for field, other in (("project", rec("m2", "project:y", "2026-09-05")),
                         ("day", rec("m2", "project:x", "2026-09-06")),
                         ("kind", rec("m2", "project:x", "2026-09-05", kind="session")),
                         ("owner", rec("m2", "project:x", "2026-09-05",
                                       owner="agent:claude-code"))):
        check(f"a different {field} is a different key", fn(a) != fn(other),
              f"{fn(a)} vs {fn(other)}")


def test_the_key_reads_the_day_not_the_moment() -> None:
    e = E()
    fn = getattr(e, "residue_key", None)
    if fn is None:
        return
    morning = dict(rec("m1", "project:x", "2026-09-05"), created_at="2026-09-05T01:02:03Z")
    night = dict(rec("m2", "project:x", "2026-09-05"), created_at="2026-09-05T23:59:59Z")
    check("two moments of one day share a key", fn(morning) == fn(night), str(fn(morning)))
    check("an unreadable stamp does not collapse into the day of another record",
          fn(dict(morning, created_at=None)) != fn(morning),
          "a missing date must not join a group it cannot be shown to belong to")


# ─────────── the groups, and what survives ─────────────────────────────

def test_a_group_keeps_its_latest_reading() -> None:
    e = E()
    fn = getattr(e, "fold_groups", None)
    if fn is None:
        check("estate.fold_groups exists", False, "the digest and the batch command share it")
        return
    rows = [dict(rec("m1", "project:x", "2026-09-05"), created_at="2026-09-05T01:00:00Z"),
            dict(rec("m2", "project:x", "2026-09-05"), created_at="2026-09-05T09:00:00Z"),
            dict(rec("m3", "project:x", "2026-09-05"), created_at="2026-09-05T20:00:00Z"),
            rec("m4", "project:y", "2026-09-05")]
    groups = fn(rows)
    check("only the over-full group is returned", len(groups) == 1, str(groups))
    if not groups:
        return
    g = groups[0]
    check("the latest reading survives", g["keep"] == "m3", str(g["keep"]))
    check("and the earlier ones are named for rejection",
          sorted(g["fold"]) == ["m1", "m2"], str(g["fold"]))
    check("a single-record group is not a group at all",
          all("m4" not in gg["fold"] for gg in groups), str(groups))


def test_the_count_is_what_the_policy_would_not_have_created() -> None:
    e = E()
    fn = getattr(e, "fold_groups", None)
    if fn is None:
        return
    rows = ([dict(rec(f"a{i}", "project:x", "2026-09-05"),
                  created_at=f"2026-09-05T0{i}:00:00Z") for i in range(1, 4)]
            + [dict(rec(f"b{i}", "project:y", "2026-09-06"),
                    created_at=f"2026-09-06T0{i}:00:00Z") for i in range(1, 3)])
    groups = fn(rows)
    extra = sum(len(g["fold"]) for g in groups)
    check("three plus two collapses to two, so three are extra", extra == 3, str(extra))


# ─────────── the operator's authority is not minted ────────────────────

def test_only_a_one_per_day_writer_is_folded() -> None:
    """The bug this file caught in its own iteration. The first `fold_groups` had
    no writer scope and folded 2 `session` records and 1 `estate-history` record
    along with the observer's — and `tools/record_turn.py` keeps one record per
    SESSION, not per day, so two sessions in one day are two
    legitimate records. The four-part key protected the day; nothing protected
    the policy."""
    e = E()
    fn = getattr(e, "fold_groups", None)
    if fn is None:
        return
    check("the scope is a named set", hasattr(e, "ONE_PER_DAY_WRITERS"),
          "a filter in one caller is a mistake the next caller repeats")
    if not hasattr(e, "ONE_PER_DAY_WRITERS"):
        return
    check("and it holds only the observer today",
          set(e.ONE_PER_DAY_WRITERS) == {("observation", "agent:observer")},
          str(sorted(e.ONE_PER_DAY_WRITERS)))
    sessions = [dict(rec(f"s{i}", "project:x", "2026-09-05", kind="session",
                         owner="agent:claude-code"),
                     created_at=f"2026-09-05T0{i}:00:00Z") for i in (1, 2)]
    check("two sessions on one day are not a group", fn(sessions) == [],
          str(fn(sessions)))
    hist = [dict(rec(f"h{i}", "project:x", "2026-09-05", kind="estate-history",
                     owner="agent:estate-history"),
                 created_at=f"2026-09-05T0{i}:00:00Z") for i in (1, 2)]
    check("a writer whose policy was never read is not folded either",
          fn(hist) == [], str(fn(hist)))
    obs = [dict(rec(f"o{i}", "project:x", "2026-09-05"),
                created_at=f"2026-09-05T0{i}:00:00Z") for i in (1, 2)]
    check("and the observer still is", len(fn(obs)) == 1, str(fn(obs)))


def test_the_batch_reject_still_refuses_without_a_terminal() -> None:
    """THE ASSERTION THIS FILE EXISTS FOR. A batch path is exactly where a guard
    gets weakened for convenience, and everything `review.py` writes is owned by
    `operator` — minting that from a script would let anything with shell access
    forge it. There is no `--yes`, and adding one row-count does not buy one."""
    p = subprocess.run([PY, "tools/review.py", "reject-group", "project:x", "2026-09-05"],
                       cwd=ROOT, stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=300)
    out = p.stdout + p.stderr
    if "invalid choice" in out or "unrecognized arguments" in out:
        check("review.py has a reject-group command", False, out[-200:])
        return
    check("it refuses with no terminal", p.returncode != 0, out[-200:])
    check("and says why, naming the ownership",
          "operator" in out and "terminal" in out, out[-260:])
    # THE FLAG, not prose about it. The first version searched for the string
    # `--yes` and failed on `require_terminal`'s own message — "there is no
    # --yes" — and on this file's docstring saying the same. The same distinction
    # `tools/check_docs.py` rule 5 had to learn: check the declaration, not the
    # sentence about it.
    review_src = (ROOT / "tools/review.py").read_text(encoding="utf-8")
    check("no command declares a --yes flag",
          'add_argument("--yes"' not in review_src
          and "add_argument('--yes'" not in review_src,
          "the guard is the whole reason this command is one decision per group "
          "rather than an automatic sweep")


def test_the_command_is_declared_with_both_arguments() -> None:
    src = (ROOT / "tools/review.py").read_text(encoding="utf-8")
    check("reject-group is a declared subcommand", '"reject-group"' in src, "")
    check("it takes a project and a day",
          "reject-group" in src and src.count("reject-group") >= 1, "")


# ─────────── the surfaces report it ────────────────────────────────────

def test_the_digest_names_the_residue() -> None:
    PLANTED["residue"] = plant_queue()
    p = subprocess.run([PY, "tools/review.py", "digest"], cwd=ROOT,
                       capture_output=True, text=True, timeout=600)
    out = p.stdout + p.stderr
    check("the digest still runs", p.returncode == 0, out[-300:])
    check("over a planted queue", "no ledger conclusion is waiting" not in out, out[-300:])
    check("it says how much of the queue the policy would not have created",
          "one-per-day" in out or "per day" in out or "residue" in out.lower(),
          out[:500])


def test_the_finding_carries_the_same_number() -> None:
    import build_findings as B
    rows = [f for f in B.collect() if f["type"] == "ledger.review_backlog"]
    check("the planted backlog raises the finding", bool(rows), "")
    if not rows:
        return
    blob = json.dumps(rows[0], ensure_ascii=False)
    e = E()
    import sqlite3
    import paths as P
    con = sqlite3.connect(f"file:{P.DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        recs = [dict(r) for r in con.execute(
            "SELECT l.memory_id, l.project_id, l.kind, l.owner, l.created_at, l.revision"
            " FROM ledger l JOIN (SELECT memory_id, MAX(revision) rev FROM ledger"
            "   GROUP BY memory_id) m ON l.memory_id=m.memory_id AND l.revision=m.rev"
            " LEFT JOIN tombstones t ON t.memory_id=l.memory_id"
            " WHERE t.memory_id IS NULL AND l.state='proposed'")]
    finally:
        con.close()
    extra = sum(len(g["fold"]) for g in e.fold_groups(recs))
    check("the planted residue is what the fold finds", extra == PLANTED.get("residue"),
          f"{extra} vs {PLANTED.get('residue')}")
    if not extra:
        return
    check(f"the finding names the {extra} record(s) the policy would not have made",
          str(extra) in blob, rows[0]["detail"][:300])


if __name__ == "__main__":
    print("policy residue — a third of the queue, from a rule already fixed\n")
    for fn in (test_the_key_has_all_four_parts,
               test_the_key_reads_the_day_not_the_moment,
               test_a_group_keeps_its_latest_reading,
               test_the_count_is_what_the_policy_would_not_have_created,
               test_only_a_one_per_day_writer_is_folded,
               test_the_batch_reject_still_refuses_without_a_terminal,
               test_the_command_is_declared_with_both_arguments,
               test_the_digest_names_the_residue,
               test_the_finding_carries_the_same_number):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe residue is one decision per group, and still the operator's\033[0m")
