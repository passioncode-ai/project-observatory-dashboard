#!/usr/bin/env python3
"""Two surfaces, one queue, and the operator acts on the number.

`tools/build_findings.py` rebuilds the board's figures on every tick;
`tools/review.py digest` recounts from the store at print time. Nothing had ever
compared them, and they had already disagreed once — a count that included
revisions where the CLI counted records.

Measured 2026-09-08 and they AGREE: computed at one instant, board and digest
both said 125, and the store's own query said 125 too. What looked like a
disagreement half an hour earlier — 125 against 127 — was the observer adding
rows between the tick that wrote the board and the moment the digest printed.

**So this suite is not a fix, it is the lock.** It recomputes both at one instant
and requires the same number, which is what would catch a real divergence: an
owner filter added to one query and not the other, a tombstone join dropped, a
`state` widened. All three have happened to one of these queries or the other.

**And the board now says why the digest will differ**, because I spent half an
iteration on that confusion myself. The stamp cannot go in the sentence:
`built_at` is carried forward when nothing moved, since "a committed file must be
a function of the FACTS, not of the clock", and a timestamp inside a finding
would make the document differ on every tick and defeat that rule.
"""
from __future__ import annotations
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
sys.path.insert(0, str(ROOT / "tests"))
# A synthetic workspace, whoever starts the suite: it writes to the store.
from test_portable_mcp import setup as portable_setup            # noqa: E402
portable_setup()
FAILURES: list[str] = []


def plant_queue() -> None:
    """Put a review queue into the synthetic store, so the driven cases assert.

    The suite runs over the portable synthetic estate (a private temporary
    workspace), whose ledger starts empty — and an empty queue would turn every
    driven case below into a NOTE. So the queue is planted with the shape that
    made the ordering matter: one project with many routine notes about its
    own commit rhythm, one whose single note rests on something structural, and
    one whose evidence the classifier cannot read. Enough rows to cross the
    board's backlog threshold, so the finding is raised as well.
    """
    from store import db as store_db, ledger as L
    import build_findings as B
    conn = store_db.connect()
    try:
        if conn.execute("SELECT COUNT(*) FROM ledger WHERE owner='agent:observer'").fetchone()[0]:
            return
        routine = [{"uri": "delta:fixture-commits", "kind": "commits-changed"},
                   {"uri": "delta:fixture-dirty", "kind": "dirty-changed"}]
        structural = [{"uri": "delta:fixture-gone", "kind": "project-disappeared"}]
        rows = ([("project:example-sample-1", routine)] * (B.REVIEW_BACKLOG + 2)
                + [("project:example-sample-2", structural)] * 2
                + [("project:example-sample-3", [{"uri": "delta:fixture-unknown"}])])
        for i, (pid, evidence) in enumerate(rows):
            L.append(conn, owner="agent:observer", kind="observation",
                     statement=f"synthetic conclusion {i} about {pid}", project_id=pid,
                     function="episodic", scope="project", state="proposed",
                     confidence=0.5, evidence=evidence)
        conn.commit()
    finally:
        conn.close()


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def digest_text() -> str:
    p = subprocess.run([PY, "tools/review.py", "digest"], cwd=ROOT,
                       capture_output=True, text=True, timeout=900)
    return p.stdout


# ─────────── the same number, at one instant ───────────────────────────

def test_the_board_and_the_digest_count_the_same_queue() -> None:
    """Recomputed, not read from the committed file: the point is the RULE, and
    a stale artefact would compare a rule against a clock."""
    import build_findings as B
    rows = [f for f in B.collect() if f["type"] == "ledger.review_backlog"]
    said = digest_text()
    dig = re.search(r"(\d+) conclusion\(s\) waiting", said)
    if not rows and dig is None:
        print("  NOTE  the queue is empty on this estate, so neither surface "
              "prints a figure [covered: nothing to compare without one]")
        return
    check("the board prints a figure", bool(rows), "")
    check("and so does the digest", dig is not None, said[:160])
    if not rows or dig is None:
        return
    board = int(re.search(r"(\d+) ledger records", rows[0]["title"]).group(1))
    check(f"the two agree ({board})", board == int(dig.group(1)),
          f"board {board}, digest {dig.group(1)} — recomputed at one instant, so "
          f"a difference is a rule divergence rather than staleness")


def test_the_class_split_agrees_too() -> None:
    """The total can match while the split does not: they are counted by
    different code — `estate.conclusion_class` on both sides, but summed here
    and printed there."""
    import build_findings as B
    rows = [f for f in B.collect() if f["type"] == "ledger.review_backlog"]
    said = digest_text()
    m = re.search(r"(\d+) rest on a structural change, (\d+) on the working "
                  r"tree's own rhythm", said)
    if not rows or m is None:
        print("  NOTE  no split to compare on this estate "
              "[covered: the total above is the half that must agree first]")
        return
    d = rows[0]["detail"]
    b = re.search(r"(\d+) rest on a structural change", d)
    br = re.search(r"(\d+) on the working tree's own rhythm", d)
    check("the structural count agrees",
          b is not None and b.group(1) == m.group(1),
          f"board {b and b.group(1)}, digest {m.group(1)}")
    check("and the routine count agrees",
          br is not None and br.group(1) == m.group(2),
          f"board {br and br.group(1)}, digest {m.group(2)}")


# ─────────── the row explains the staleness it cannot stamp ────────────

def test_the_row_warns_that_a_live_recount_will_differ() -> None:
    import build_findings as B
    rows = [f for f in B.collect() if f["type"] == "ledger.review_backlog"]
    if not rows:
        return
    d = rows[0]["detail"]
    check("the row says the count is as of the last change",
          "as of the last" in d, d[-200:])
    check("and that the digest recounts at print time",
          "recounts at print time" in d, d[-200:])
    check("and that a small difference is minutes rather than disagreement",
          "not disagreement" in d, d[-200:])


def test_the_stamp_is_not_smuggled_into_a_finding() -> None:
    """`built_at` is carried forward when nothing moved, so a timestamp inside a
    finding would make the document differ every tick — the churn the carry
    exists to prevent. This is the tripwire for a later reader who thinks the
    obvious fix is obvious."""
    sys.path.insert(0, str(ROOT / "tests"))
    import source_reader
    code = source_reader.code_keeping_strings(
        (ROOT / "tools/build_findings.py").read_text(encoding="utf-8"))
    i = code.find('"ledger.review_backlog"')
    end = code.find('"evidence"', i)
    block = code[i:end if end > i else i + 2500]
    check("no finding interpolates a clock into its own text",
          "built_at" not in block and "now_z" not in block,
          "a stamp here defeats the carry-forward rule two hundred lines down")


if __name__ == "__main__":
    plant_queue()
    print("two surfaces — one queue, counted twice\n")
    for fn in (test_the_board_and_the_digest_count_the_same_queue,
               test_the_class_split_agrees_too,
               test_the_row_warns_that_a_live_recount_will_differ,
               test_the_stamp_is_not_smuggled_into_a_finding):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mtwo readers of one queue, and they agree\033[0m")
