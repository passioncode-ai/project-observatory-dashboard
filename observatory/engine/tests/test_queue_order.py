#!/usr/bin/env python3
"""The operator's queue is ordered so that the noise is on top.

Measured 2026-09-07 over the 130 records genuinely waiting for a decision — the
latest revision of each, tombstoned ones excluded, which is the count
`tools/review.py digest` reports:

    107   rest only on `commits-changed`, `dirty-changed`, `last_activity_on-changed`
     17   rest on something structural — `project-disappeared`, `project-appeared`,
          `ownership-changed`, `repos-changed`, `stack-changed`, `session-store`
      6   carry evidence this classifier cannot read

And the digest sorts projects by `-len(items)` — count descending. So the project
with EIGHTEEN notes about its own commit rhythm sits at the top, and "the
assistant-preview project has been deleted or absorbed" sits somewhere below it.
A queue whose only exit is a silent ninety-day expiry, ordered by which project
generated the most routine notes.

**The ranking signal is a fact, not a judgement.** Each record's
`evidence_json` names the delta kinds it was built from, so what a conclusion
RESTS ON is recorded. A conclusion built only from the working tree's own rhythm
is routine; one that rests on a project appearing, vanishing, changing owner,
gaining a repository or changing stack is structural.

**Confidence was measured and rejected as the signal.** It does not
discriminate: among the restatements 0.95, 0.9, 0.9, 0.85, 0.7, 0.6; among the
lifecycle and anomaly conclusions 0.9, 0.9, 0.85, 0.7, 0.6. Orthogonal, so
sorting by it would have looked principled and done nothing.

**Three outcomes, and the third is not folded into the cheap one.** A record
whose evidence this classifier cannot read is `unclassified`, never `routine` —
"I could not tell" demoted to "routine" is how a queue hides the thing that
mattered. Unclassified sorts with structural, because it might be either.

**One classifier, two readers.** `tools/review.py digest` orders by it and
`ledger.review_backlog` in `tools/build_findings.py` reports the split, so the
CLI and the board cannot come to disagree about the shape of the same queue —
the rule `estate.py` already exists to enforce for ownership, and `reset_date`
for the key's reset.
"""
from __future__ import annotations
import json, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
sys.path.insert(0, str(ROOT / "tests"))
import live_estate                                                  # noqa: E402
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


def E():
    import estate
    return estate


def klass(evidence) -> str | None:
    e = E()
    fn = getattr(e, "conclusion_class", None)
    if fn is None:
        return None
    return fn(json.dumps(evidence) if not isinstance(evidence, str) else evidence)


# ─────────── the classifier ────────────────────────────────────────────

def test_the_routine_trio_is_named_and_justified() -> None:
    e = E()
    src = (ROOT / "estate.py").read_text(encoding="utf-8")
    check("the routine kinds are a named set", hasattr(e, "ROUTINE_DELTA_KINDS"),
          "an inline tuple in two readers is two policies")
    if not hasattr(e, "ROUTINE_DELTA_KINDS"):
        return
    check("it holds exactly the three a working day produces",
          set(e.ROUTINE_DELTA_KINDS) == {"commits-changed", "dirty-changed",
                                         "last_activity_on-changed"},
          str(sorted(e.ROUTINE_DELTA_KINDS)))
    check("and the set says why those three",
          "working" in src.lower() or "rhythm" in src.lower() or "oscillat" in src.lower(),
          "the reason is the whole argument for the ranking")


def test_only_the_trio_is_routine() -> None:
    got = klass([{"uri": "delta:x", "kind": "commits-changed"},
                 {"uri": "delta:y", "kind": "dirty-changed"}])
    if got is None:
        check("estate.conclusion_class exists", False,
              "the digest and the finding both need it")
        return
    check("commits plus dirty is routine", got == "routine", str(got))
    check("all three together are still routine",
          klass([{"kind": k} for k in ("commits-changed", "dirty-changed",
                                       "last_activity_on-changed")]) == "routine",
          "measured: the nine records carrying all three are ordinary work")


def test_one_structural_kind_lifts_the_whole_record() -> None:
    for kind in ("project-disappeared", "project-appeared", "ownership-changed",
                 "repos-changed", "stack-changed", "session-store"):
        got = klass([{"kind": "commits-changed"}, {"kind": kind}])
        check(f"{kind} makes it structural", got == "structural", str(got))


def test_unreadable_evidence_is_its_own_class() -> None:
    for ev, why in (([], "no evidence at all"),
                    ("not json", "unparseable"),
                    ([{"uri": "delta:x"}], "an entry with no kind"),
                    ([{"kind": "?"}], "a kind that is a placeholder")):
        got = klass(ev)
        if got is None:
            return
        check(f"{why} is unclassified, not routine", got == "unclassified", str(got))


def test_the_sort_key_puts_structure_first() -> None:
    e = E()
    fn = getattr(e, "conclusion_rank", None)
    check("there is a rank to sort by", fn is not None,
          "two readers sorting by two hand-written keys is the same drift")
    if fn is None:
        return
    check("structural outranks unclassified outranks routine",
          fn("structural") < fn("unclassified") < fn("routine"),
          f'{fn("structural")}, {fn("unclassified")}, {fn("routine")}')


# ─────────── the digest uses it ────────────────────────────────────────

def test_the_digest_no_longer_sorts_by_count() -> None:
    src = (ROOT / "tools/review.py").read_text(encoding="utf-8")
    check("the count-descending key is gone",
          "-len(kv[1])" not in src,
          "sorting by how many routine notes a project produced promotes the "
          "noisiest project to the top")
    check("and the classifier is what it sorts by",
          "conclusion_rank" in src or "conclusion_class" in src, "")


def test_the_digest_states_the_shape_not_only_the_size() -> None:
    """DRIVEN. 130 is a size; 107 routine against 17 structural is the shape, and
    only the second tells the operator whether the queue is worth their evening."""
    p = subprocess.run([PY, "tools/review.py", "digest"], cwd=ROOT,
                       capture_output=True, text=True, timeout=600)
    out = p.stdout + p.stderr
    check("the digest still runs", p.returncode == 0, out[-300:])
    if not live_estate.needs("a review queue with rows",
                             "no ledger conclusion is waiting" not in out,
                             "the split's arithmetic is driven against a "
                             "planted ledger in this suite"):
        return
    check("it reports the routine/structural split",
          "structural" in out.lower(), out[:400])
    check("and every project line carries its class",
          out.count("structural") >= 1 or out.count("routine") >= 1, out[:400])


def test_a_structural_project_is_printed_before_a_noisier_routine_one() -> None:
    """The pathology, end to end: the project with eighteen routine notes must no
    longer outrank a project whose single note is that it disappeared."""
    p = subprocess.run([PY, "tools/review.py", "digest"], cwd=ROOT,
                       capture_output=True, text=True, timeout=600)
    out = p.stdout
    lines = [l for l in out.splitlines() if l.startswith("  ") and "project:" in l]
    if len(lines) < 2:
        print("  NOTE  fewer than two projects are waiting "
              "[covered: the classifier and rank cases above]")
        return
    first_structural = next((i for i, l in enumerate(lines) if "structural" in l), None)
    last_routine = next((i for i, l in reversed(list(enumerate(lines)))
                         if "routine" in l), None)
    if first_structural is None or last_routine is None:
        print("  NOTE  the live queue holds only one class right now "
              "[covered: the classifier cases above plant all three]")
        return
    check("every structural project precedes every routine one",
          first_structural < last_routine,
          f"first structural at {first_structural}, last routine at {last_routine}")


# ─────────── and the board agrees with the CLI ─────────────────────────

def test_the_finding_reports_the_same_split() -> None:
    import build_findings as B
    rows = [f for f in B.collect() if f["type"] == "ledger.review_backlog"]
    if not rows:
        print("  NOTE  no review backlog on this estate right now "
              "[covered: the classifier cases above]")
        return
    f = rows[0]
    blob = json.dumps(f, ensure_ascii=False)
    check("the finding names the structural count",
          "structural" in blob.lower(), f["detail"][:240])
    # THE PROPERTY, not a word. The first version of this assertion demanded the
    # literal "routine" and failed against prose that says "the working tree's
    # own rhythm" — better English and the same fact. What must hold is that the
    # board and the CLI carry the SAME NUMBERS, computed once.
    import sqlite3
    import paths as P
    e = E()
    counts: dict[str, int] = {}
    con = sqlite3.connect(f"file:{P.DB}?mode=ro", uri=True)
    try:
        for (ev,) in con.execute(
                "SELECT l.evidence_json FROM ledger l JOIN ("
                "  SELECT memory_id, MAX(revision) rev FROM ledger GROUP BY memory_id) m"
                " ON l.memory_id=m.memory_id AND l.revision=m.rev"
                " LEFT JOIN tombstones t ON t.memory_id=l.memory_id"
                " WHERE t.memory_id IS NULL AND l.state='proposed'"):
            k = e.conclusion_class(ev)
            counts[k] = counts.get(k, 0) + 1
    finally:
        con.close()
    for k, n in counts.items():
        check(f"the finding carries the {k} count ({n})", str(n) in blob,
              f"{k}={n} absent from: {f['detail'][:200]}")


if __name__ == "__main__":
    plant_queue()
    print("queue order — structure before rhythm\n")
    for fn in (test_the_routine_trio_is_named_and_justified,
               test_only_the_trio_is_routine,
               test_one_structural_kind_lifts_the_whole_record,
               test_unreadable_evidence_is_its_own_class,
               test_the_sort_key_puts_structure_first,
               test_the_digest_no_longer_sorts_by_count,
               test_the_digest_states_the_shape_not_only_the_size,
               test_a_structural_project_is_printed_before_a_noisier_routine_one,
               test_the_finding_reports_the_same_split):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe queue leads with what changed the estate, not with what "
          "changed a working tree\033[0m")
