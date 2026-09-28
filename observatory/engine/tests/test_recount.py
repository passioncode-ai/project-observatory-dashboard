#!/usr/bin/env python3
"""Two clocks behind one number, and the row printed one date for both.

Measured once against a repository whose truth could be checked
independently:

    board:  47 commit(s) are on no remote … (checked <today>, branch main)
    git:    61

Fourteen commits of the operator's own work missing from the single number that
says how much work exists solely on this disk, printed beside a date that reads
as today. Nothing was broken: `collectors/scan_remotes.py` runs at most every
six hours because `git ls-remote` is about ninety network round trips, and the
count was simply that old. `remote_head` had not moved at all — only the cheap
half was stale.

**The two halves have different costs and deserve different cadences.** What the
remote HAS is a network question and its six-hour gate is right. How far ahead
this checkout is NOW is `git rev-list` inside the clone and costs nothing, so
`collectors/merge.py` recounts it every tick — by calling
`scan_remotes.at_stake`, not by reimplementing the ranges it already knows.

**The row now names both clocks**: when the remote was last asked, with the age
in hours, and whether the count beside it was taken just now or inherited from
that scan. The remote's instant is quoted from the scan's own report rather than
stored per repository — a second-resolution stamp on 174 rows is what made
`git diff` never empty, which is why `remote_checked_on` is a date.
"""
from __future__ import annotations
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "collectors"))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def run(args, cwd) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, timeout=300).stdout.strip()


def repo_ahead(total: int, pushed: int) -> tuple[pathlib.Path, str]:
    """A checkout `total - pushed` commits ahead of a real bare remote."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-recount-"))
    bare, work = d / "remote.git", d / "work"
    run(["init", "-q", "--bare", "-b", "main", str(bare)], d)
    run(["init", "-q", "-b", "main", str(work)], d)
    for k in ("user.email=t@t", "user.name=t", "commit.gpgsign=false"):
        run(["-C", str(work), "config", *k.split("=", 1)], d)
    run(["-C", str(work), "remote", "add", "origin", str(bare)], d)
    at_push = ""
    for i in range(total):
        (work / "f").write_text(f"{i}\n", encoding="utf-8")
        run(["-C", str(work), "add", "f"], d)
        run(["-C", str(work), "commit", "-q", "-m", f"c{i}"], d)
        if i + 1 == pushed:
            run(["-C", str(work), "push", "-q", "origin", "main"], d)
            at_push = run(["-C", str(work), "rev-parse", "HEAD"], d)
    run(["-C", str(work), "fetch", "-q", "origin"], d)
    return work, at_push


# ─────────── the recount happens, and it is the same implementation ────

def test_merge_recounts_the_local_half_every_tick() -> None:
    """The defect, driven: the raw scan's figure is stale and the clone knows
    better.

    **NOT by importing merge.** `collectors/merge.py` has no
    `if __name__ == "__main__"` guard: it does its work at module level, so
    `import merge` performs a full merge against whatever estate is configured.
    The first version of this case imported it and reloaded it — two merges
    from a test — which the gate's purity verdict would have reported as this
    suite writing `store/raw/model.json`, so no suite imports it.

    So the recount is driven through the collector that OWNS the ranges, and
    merge's use of it is read from the source.
    """
    import scan_remotes
    work, remote_sha = repo_ahead(9, 4)          # five commits ahead, truly
    fresh = scan_remotes.at_stake(work, "ahead", "", remote_sha, "main")
    check("the clone really is five ahead", fresh.get("unpushed") == 5, str(fresh))
    sys.path.insert(0, str(ROOT / "tests"))
    import source_reader
    # `code_keeping_strings`: prose removed, literals KEPT. The first version
    # read the RAW source and matched `rev-list` inside the comment I had just
    # written explaining why merge does not run one — the ninth assertion this
    # session to test what the code says instead of what it does.
    code = source_reader.code_keeping_strings(
        (ROOT / "collectors/merge.py").read_text(encoding="utf-8"))
    check("merge calls the collector that owns the ranges",
          "scan_remotes.at_stake(" in code,
          "a second implementation of the ranges is the drift this avoids")
    check("and runs no `rev-list` of its own",
          "rev-list" not in code, "the ranges per state belong to one file")
    check("gated on the at-risk states, so it is ~17 calls and not 174",
          "AT_RISK_STATES" in code, "")


def test_an_unrecountable_clone_keeps_the_scans_figure() -> None:
    """Absent stays absent. An empty answer means the count could not be taken
    now — the remote sha may be unknown to this clone — and replacing a measured
    older number with silence loses the only figure there is."""
    import scan_remotes
    work, _ = repo_ahead(3, 0)
    # A sha this clone has never heard of: `rev-list` cannot resolve it.
    fresh = scan_remotes.at_stake(work, "ahead", "", "0" * 40, "main")
    check("the recount is empty, not zero", fresh == {}, str(fresh))
    src = (ROOT / "collectors/merge.py").read_text(encoding="utf-8")
    check("merge only overwrites when the recount answered",
          "if fresh:" in src, "an empty answer must not erase the scan's number")
    check("and marks the figure it holds",
          '"unpushed_recounted"' in src or "unpushed_recounted" in src,
          "otherwise a reader cannot tell which clock a number came from")


def test_the_emitter_carries_the_provenance() -> None:
    """The enumerated list stops a key added upstream unless it is named — which
    is how `unpushed` itself reached `store/raw/model.json` and none of the
    registry."""
    src = (ROOT / "collectors/emit_registry.py").read_text(encoding="utf-8")
    check("`unpushed_recounted` is named in the emitted keys",
          "unpushed_recounted" in src,
          "an unnamed key is dropped silently at this boundary")


# ─────────── the row says which clock ──────────────────────────────────

def test_the_remote_clock_is_quoted_with_its_age() -> None:
    import build_findings as B
    fn = getattr(B, "_remote_asked", None)
    if fn is None:
        check("build_findings._remote_asked exists", False,
              "the row printed one date for two different clocks")
        return
    said = fn()
    check("the phrase is not empty", bool(said), said)
    scan = pathlib.Path(B.paths.SCRATCH / "remotes.json")
    if scan.is_file():
        check("it carries a time of day", "Z" in said, said)
        check("and the age, so a reader need not subtract",
              "ago" in said, said)
    else:
        check("with no scan it refuses to invent one",
              "unrecorded" in said, said)


def test_the_phrase_degrades_rather_than_inventing_a_date() -> None:
    """A missing timestamp rendered as today's date is the defect this
    replaces, so an absent or unreadable report must say so."""
    import importlib
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-noscan-"))
    previous = os.environ.get("OBSERVATORY_SCRATCH")
    os.environ["OBSERVATORY_SCRATCH"] = str(d)
    for m in ("paths", "build_findings"):
        sys.modules.pop(m, None)
    import build_findings as B
    check("an absent report yields an honest phrase",
          "unrecorded" in B._remote_asked(), B._remote_asked())
    (d / "remotes.json").write_text("{not json", encoding="utf-8")
    sys.modules.pop("build_findings", None)
    import build_findings as B2
    check("and so does an unreadable one",
          "unrecorded" in B2._remote_asked(), B2._remote_asked())
    (d / "remotes.json").write_text(
        json.dumps({"scanned_at": "2026-09-08T03:01:54Z", "repositories": []}),
        encoding="utf-8")
    sys.modules.pop("build_findings", None)
    import build_findings as B3
    said = B3._remote_asked()
    check("a real stamp is quoted at minute resolution", "03:01Z" in said, said)
    # RESTORE, because the estate cases below read the configured workspace and
    # a leaked redirect makes them skip while looking like honest degradation.
    if previous is None:
        os.environ.pop("OBSERVATORY_SCRATCH", None)
    else:
        os.environ["OBSERVATORY_SCRATCH"] = previous
    for m in ("paths", "build_findings"):
        sys.modules.pop(m, None)
    importlib.invalidate_caches()


def test_the_live_rows_say_which_clock_each_number_came_from() -> None:
    import build_findings as B
    rows = [f for f in B.collect() if f["type"].startswith("clone.")]
    if not rows:
        print("  NOTE  no at-risk clone on this estate right now "
              "[covered: the fixture cases above drive the recount itself]")
        return
    blob = " ".join(f["detail"] for f in rows)
    check("the remote's clock is named with its age",
          "the remote was last asked" in blob, blob[:200])
    counted = [f for f in rows if "counted just now" in f["detail"]]
    inherited = [f for f in rows if "not since" in f["detail"]]
    check("and every figure declares which clock it came from",
          all("counted just now" in f["detail"] or "not since" in f["detail"]
              or "commit(s) are on no remote" not in f["detail"] for f in rows),
          f"{len(counted)} recounted, {len(inherited)} inherited, {len(rows)} rows")
    print(f"  NOTE  measured now: {len(counted)} row(s) recounted this tick, "
          f"{len(inherited)} carrying the scan's figure "
          f"[covered: both branches are driven by the fixture cases above]")


def test_the_recount_agrees_with_git_at_one_instant() -> None:
    """Ground truth taken at ONE instant, not two.

    The first version compared the registry's STORED figure with git's count
    now, and went red within the hour: the recount happens at merge time, so a
    single commit between the merge and the test breaks it. A test comparing
    stored state to live git is a test with a clock inside it — the staleness of
    the stored figure is bounded by the tick BY DESIGN, and what the row now
    carries is the provenance that says so. So this asserts the property that
    has no clock: the recount, taken now, is what git counts now — on a planted
    checkout whose remote is a real bare repository.
    """
    import scan_remotes
    work, sha = repo_ahead(7, 2)
    fresh = scan_remotes.at_stake(work, "ahead", "", sha, "main")
    truth = subprocess.run(["git", "rev-list", "--count", f"{sha}..HEAD"], cwd=work,
                           capture_output=True, text=True, timeout=300).stdout.strip()
    check("the recount is what git counts, both taken now",
          truth.isdigit() and fresh.get("unpushed") == int(truth) == 5,
          f"recount {fresh.get('unpushed')} vs git {truth!r}")


if __name__ == "__main__":
    print("the recount — two clocks behind one number\n")
    for fn in (test_merge_recounts_the_local_half_every_tick,
               test_an_unrecountable_clone_keeps_the_scans_figure,
               test_the_emitter_carries_the_provenance,
               test_the_remote_clock_is_quoted_with_its_age,
               test_the_phrase_degrades_rather_than_inventing_a_date,
               test_the_live_rows_say_which_clock_each_number_came_from,
               test_the_recount_agrees_with_git_at_one_instant):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe cheap half is counted now, and the row says so\033[0m")
