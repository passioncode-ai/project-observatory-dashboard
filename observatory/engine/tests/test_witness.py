#!/usr/bin/env python3
"""A branch tidied up after a push is not work that vanished.

Four `work.unverifiable` rows once sat on the board, all in one repository,
each saying:

    <sha> exists in <owner>/<repo> but is not an ancestor of <branch>; the
    branch moved away from it

Every one of those commits was still there — `git cat-file -t` says `commit`,
with its date, author and subject — and each sat on
`remotes/origin/<branch>`, the very branch the row named. What had
actually happened is the ordinary thing: the branch was pushed and the LOCAL
copy deleted. So `merge-base --is-ancestor <sha> feat/attention-rail` could not
resolve the name, exited non-zero, and `check_session` read any non-zero as "the
branch moved away from it".

**Two outcomes collapsed into one, and it ends in deletion.** A refused row stays
`proposed`, and retention erases a `proposed` row at ninety days. So four records
of real, pushed work were queued for deletion because somebody tidied a branch —
and the finding told the operator the second witness "has nothing left to look
at" while the commit was one ref away.

**The same conflation sat one line above.** A missing local clone returned
`False` — the work cannot be placed — when nothing had been asked at all.

So the witness has three answers now: a witness confirms, a witness
CONTRADICTS, or there was none to ask. The middle one keeps its warning; the
third is `info`, because nothing is known to be wrong, and says plainly that the
absence of a clone rather than a judgement about the work is what would delete
it.
"""
from __future__ import annotations
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
# The board and the corroborator read a workspace; the synthetic estate is it.
from test_portable_mcp import setup as portable_setup                    # noqa: E402
portable_setup()
import tmp as tmpdir                                                  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def run(*args, cwd) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       timeout=300)
    return p.stdout.strip()


def fixture() -> tuple[pathlib.Path, dict]:
    """A clone whose branch was pushed and then deleted locally — the estate's
    own shape, built rather than described.

    A bare repository stands in for the remote, so `remotes/origin/<branch>` is
    a real remote-tracking ref rather than a hand-made one: the property under
    test is that git reaches the commit from SOME ref, and faking the ref would
    assert the fixture instead of the code.
    """
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-witness-"))
    remote, work = d / "remote.git", d / "work"
    run("init", "--bare", "-b", "main", str(remote), cwd=d)
    run("clone", str(remote), str(work), cwd=d)
    for k, v in (("user.email", "t@example.test"), ("user.name", "t")):
        run("config", k, v, cwd=work)
    (work / "a.txt").write_text("one\n", encoding="utf-8")
    run("add", "-A", cwd=work)
    run("commit", "-qm", "base", cwd=work)
    run("push", "-q", "origin", "main", cwd=work)
    base = run("rev-parse", "HEAD", cwd=work)

    run("checkout", "-qb", "feat/tidy", cwd=work)
    (work / "b.txt").write_text("two\n", encoding="utf-8")
    run("add", "-A", cwd=work)
    run("commit", "-qm", "the work", cwd=work)
    pushed = run("rev-parse", "HEAD", cwd=work)
    run("push", "-q", "origin", "feat/tidy", cwd=work)
    # THE TIDY-UP, which is the whole subject: back to main and delete the local
    # branch, exactly as a person does after pushing.
    run("checkout", "-q", "main", cwd=work)
    run("branch", "-qD", "feat/tidy", cwd=work)

    # A branch that EXISTS and has moved away from a commit: a real
    # contradiction, and the state the old message described.
    run("checkout", "-qb", "feat/moved", cwd=work)
    (work / "c.txt").write_text("three\n", encoding="utf-8")
    run("add", "-A", cwd=work)
    run("commit", "-qm", "orphan-to-be", cwd=work)
    orphan = run("rev-parse", "HEAD", cwd=work)
    run("reset", "-q", "--hard", "HEAD~1", cwd=work)

    return work, {"base": base, "pushed": pushed, "orphan": orphan}


def row(path, sha, branch):
    """A ledger row's shape, as `check_session` reads it."""
    return {"evidence_json": json.dumps(
        [{"uri": f"repo:repository:{path.name}", "head": sha, "branch": branch}])}


def witness(monkey_path, sha, branch):
    import corroborate as C
    C.clone_path = lambda nwo: monkey_path                       # noqa: ARG005
    return C.check_session(row(monkey_path, sha, branch))


# ─────────── the five states, each driven ──────────────────────────────

def test_a_branch_tidied_up_after_a_push_is_confirmed() -> None:
    """The four live rows. The local branch is gone; the commit is exactly where
    the row said, one ref away."""
    work, sha = fixture()
    ok, why = witness(work, sha["pushed"], "feat/tidy")
    check("the local branch really is gone",
          "feat/tidy" not in run("branch", cwd=work), run("branch", cwd=work))
    check("and the remote-tracking ref really holds the commit",
          "origin/feat/tidy" in run("branch", "-a", "--contains", sha["pushed"], cwd=work),
          run("branch", "-a", "--contains", sha["pushed"], cwd=work))
    check("the witness CONFIRMS", ok is True, f"{ok}: {why}")
    check("and names the ref that answered", "origin/feat/tidy" in why, why[:200])
    # THE PROPERTY, not the sentence: it must say the local branch is gone and
    # must not claim the branch moved away from the commit, which is the false
    # statement that put four records of pushed work on the board.
    check("and says the local branch is gone rather than that it moved",
          "gone" in why and "moved away" not in why, why[:200])


def test_a_branch_that_still_holds_the_commit_is_confirmed() -> None:
    work, sha = fixture()
    ok, why = witness(work, sha["base"], "main")
    check("the strongest claim is the named branch", ok is True, f"{ok}: {why}")
    check("and the message says so", "still an ancestor of main" in why, why[:200])


def test_a_commit_no_ref_reaches_is_contradicted() -> None:
    """The state the old message described and never actually met: the branch
    exists, has moved, and nothing else reaches the commit."""
    work, sha = fixture()
    ok, why = witness(work, sha["orphan"], "feat/moved")
    check("the commit is still an object",
          run("cat-file", "-t", sha["orphan"], cwd=work) == "commit",
          run("cat-file", "-t", sha["orphan"], cwd=work))
    check("no ref reaches it",
          not run("branch", "-a", "--contains", sha["orphan"], cwd=work).strip(),
          run("branch", "-a", "--contains", sha["orphan"], cwd=work))
    check("the witness CONTRADICTS", ok is False, f"{ok}: {why}")
    check("and says nothing else reaches it, so it will be collected",
          "no other ref reaches it" in why and "garbage collection" in why, why[:200])


def test_a_rewritten_commit_is_contradicted() -> None:
    work, _ = fixture()
    ok, why = witness(work, "0" * 40, "main")
    check("a sha that is not an object at all contradicts", ok is False, f"{ok}: {why}")
    check("and says the work was rewritten or the clone rebuilt",
          "no longer an object" in why, why[:200])


def test_a_missing_clone_is_unaskable_not_a_refusal() -> None:
    """The conflation one line above the one that reached the board: nothing was
    asked, so nothing can be concluded."""
    import corroborate as C
    C.clone_path = lambda nwo: None                              # noqa: ARG005
    ok, why = C.check_session(row(pathlib.Path("/nowhere/x"), "0" * 40, "main"))
    check("a missing clone is UNASKABLE", ok is None, f"{ok}: {why}")
    check("and the message refuses to read it as the work being gone",
          "not evidence that the work is gone" in why, why[:200])


def test_a_row_with_no_head_is_a_contradiction() -> None:
    """Not unaskable: the record was written to be verifiable and is short. The
    estate is measurable; the row is not."""
    import corroborate as C
    ok, why = C.check_session({"evidence_json": json.dumps([{"uri": "repo:repository:x"}])})
    check("a row carrying no head contradicts", ok is False, f"{ok}: {why}")


# ─────────── the board keeps the three apart ───────────────────────────

def test_the_board_reports_the_two_classes_differently() -> None:
    """Read from the source, because the rows are built inline in `collect()`
    and there is no function to call. The first version of this case probed for
    one with `getattr` and printed a NOTE when it was absent — a skip site
    announcing that a function nobody wrote does not exist, which is noise
    dressed as honesty. The live receipt is driven by the case below."""
    src = (ROOT / "tools/build_findings.py").read_text(encoding="utf-8")
    check("`work.unwitnessed` exists as its own class",
          '"work.unwitnessed"' in src,
          "an unaskable row folded into the refusals is what put four records of "
          "pushed work on the board as unplaceable")
    check("and it is info, not warning",
          '"type": "work.unwitnessed"' in src
          and '"severity": "info"' in src.split('"work.unwitnessed"')[1][:400],
          "nothing is known to be wrong when nobody could look")
    check("while the contradiction stays a warning",
          '"severity": "warning"' in src.split('"work.unverifiable"')[1][:400],
          "a witness that looked and disagreed is a different fact")
    check("and the refusal no longer claims the witness had nothing to look at",
          "has nothing left to look at" not in src,
          "that sentence was false of every row it was ever printed for")


def test_the_receipt_carries_all_four_counts() -> None:
    """Driven, not read: the corroborator runs over the synthetic estate and its
    receipt is inspected, so the case cannot pass by finding no receipt."""
    import os
    import paths
    p = subprocess.run([sys.executable, "tools/corroborate.py"], cwd=ROOT, env=dict(os.environ),
                       capture_output=True, text=True, timeout=300)
    check("the corroborator runs over the synthetic estate", p.returncode == 0,
          (p.stdout + p.stderr)[-300:])
    f = paths.SCRATCH / "corroboration.json"
    check("and writes its receipt", f.is_file(), str(f))
    if not f.is_file():
        return
    doc = json.loads(f.read_text(encoding="utf-8"))
    counts = doc.get("counts") or {}
    for k in ("proposed", "promoted", "refused", "unaskable", "needs_person"):
        check(f"the receipt counts `{k}`", k in counts, str(counts))
    check("and lists the unaskable rows rather than only counting them",
          isinstance(doc.get("unaskable"), list),
          "a count with no list is a number an operator cannot act on")



def test_the_row_says_how_long_it_has_before_retention_erases_it() -> None:
    """The number the decision turns on, and it was already recorded.

    Both rows tell the reader that retention erases a `proposed` row at ninety
    days and neither said how many that row had left — so a reader could not tell
    a decision due this week from one due next quarter. The row's own age and
    owner sat in the ledger, and `store/retention.days_left` is the one home for
    the arithmetic.
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import build_findings as B
    fresh = B._erasure_horizon({"created_at": "2026-09-08T00:00:00Z",
                                "owner": "agent:fixture", "state": "proposed"})
    check("a dated row says when it goes", "day(s)" in fresh, fresh)
    check("and quotes the day it was written", "2026-09-08" in fresh, fresh)
    kept = B._erasure_horizon({"created_at": "2026-09-08T00:00:00Z",
                               "owner": "operator", "state": "proposed"})
    check("a row retention never erases says so instead of a countdown",
          "never erases" in kept, kept)
    # ABSENT IS NOT "NEVER". A receipt written before the fields existed says
    # nothing, rather than implying the row is safe.
    old = B._erasure_horizon({"memory_id": "mem:x", "why": "no clone"})
    check("a receipt without the fields is silent", old == "", repr(old))


def test_the_receipt_dates_the_rows_it_asks_a_person_to_decide() -> None:
    """The finding can only say it if the corroborator records it."""
    src = (ROOT / "tools/corroborate.py").read_text(encoding="utf-8")
    for field in ("created_at", "owner", "state"):
        check(f"the receipt carries `{field}`",
              f'"{field}": ' in src.split('"unaskable": [')[1][:300],
              "the finding would have to go back to the store for it")


if __name__ == "__main__":
    print("the witness — a tidied branch is not vanished work\n")
    for fn in (test_a_branch_tidied_up_after_a_push_is_confirmed,
               test_a_branch_that_still_holds_the_commit_is_confirmed,
               test_a_commit_no_ref_reaches_is_contradicted,
               test_a_rewritten_commit_is_contradicted,
               test_a_missing_clone_is_unaskable_not_a_refusal,
               test_a_row_with_no_head_is_a_contradiction,
               test_the_board_reports_the_two_classes_differently,
               test_the_receipt_carries_all_four_counts,
               test_the_row_says_how_long_it_has_before_retention_erases_it,
               test_the_receipt_dates_the_rows_it_asks_a_person_to_decide):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mconfirmed, contradicted and unaskable are three answers\033[0m")
