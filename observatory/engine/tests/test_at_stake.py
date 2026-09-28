#!/usr/bin/env python3
"""Ten rows ask "push or delete" without saying how much is at stake.

The biggest cluster on an operator's board was ten `clone.*` warnings about
work that exists only on one disk — `ahead`, `local-only-branch`,
`unpushed-and-remote-moved`, `diverged`. Each row read like this one:

    example-org/alpha-web holds commits that are on no remote
    the remote's own commit is an ancestor of this checkout, so this work
    survives only this disk (checked on a date, branch main).
    → push the branch

It names the branch and the date. It does not say **how many** commits are
unpushed or **when** the newest of them was made. One of these ten may hold a
single typo from an hour ago and another forty-four commits of a feature branch
from March, and the row treats them identically — so the operator is asked for
ten decisions with the size and age of each withheld.

**The collector already holds everything needed.** `sync_state` compares
`local_sha` and `remote_sha` with `merge-base --is-ancestor` inside the
checkout, so a count is one read-only `rev-list` in a function that already runs
git there, and `probe()` reaches the network per repository anyway — the local
walk is free beside it.

**Per state, the range that means "at stake":**

    ahead                       remote_sha..HEAD
    unpushed-and-remote-moved   the tracking ref..HEAD
    local-only-branch           HEAD --not --remotes, since no remote has it
    diverged                    the local side of remote_sha...HEAD

**Absent, never zero.** A count that cannot be obtained is omitted: zero
unpushed commits means "nothing at stake", which is the opposite of what every
one of these states asserts. Reporting an unknown as zero would turn the row
into a reassurance.
"""
from __future__ import annotations
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "collectors"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup                 # noqa: E402
portable_setup()
import tmp as tmpdir                                                  # noqa: E402

FAILURES: list[str] = []


def plant_checkouts(states: list[dict]) -> None:
    """Give the synthetic workspace's first repositories these `local` states,
    then rebuild its page, so the estate-level cases below have a subject
    instead of waiting for a real estate to hold one."""
    import json, paths
    f = paths.REGISTRY / "repositories.json"
    doc = json.loads(f.read_text(encoding="utf-8"))
    for row, state in zip(doc["repositories"], states):
        row["local"].update(state)
    f.write_text(json.dumps(doc), encoding="utf-8")
    py = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
    subprocess.run([py, "dashboard/build_dashboard.py"], cwd=ROOT, capture_output=True,
                   text=True, timeout=600, check=True)


plant_checkouts([
    {"sync": "ahead", "unpushed": 3, "unpushed_newest_on": "2026-01-02"},
    {"sync": "local-only-branch", "nothing_exclusive": True},
])


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def run(args, cwd) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, timeout=120).stdout.strip()


def repo_with(commits: int, *, branch: str = "main", push: int = 0) -> tuple[pathlib.Path, str]:
    """A checkout with `commits` commits, `push` of them in a bare 'remote'.

    Two real repositories rather than a stub: the measurement is `git rev-list`,
    and a fake would prove nothing about what git counts.
    """
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-stake-"))
    bare, work = d / "remote.git", d / "work"
    run(["init", "-q", "--bare", "-b", branch, str(bare)], d)
    run(["init", "-q", "-b", branch, str(work)], d)
    for k in ("user.email=t@t", "user.name=t", "commit.gpgsign=false"):
        run(["-C", str(work), "config", *k.split("=", 1)], d)
    run(["-C", str(work), "remote", "add", "origin", str(bare)], d)
    head_at_push = ""
    for i in range(commits):
        (work / "f").write_text(f"{i}\n", encoding="utf-8")
        run(["-C", str(work), "add", "f"], d)
        run(["-C", str(work), "commit", "-q", "-m", f"c{i}"], d)
        if i + 1 == push:
            run(["-C", str(work), "push", "-q", "origin", branch], d)
            head_at_push = run(["-C", str(work), "rev-parse", "HEAD"], d)
    run(["-C", str(work), "fetch", "-q", "origin"], d)
    return work, head_at_push


def at_stake(*args, **kw):
    import scan_remotes
    fn = getattr(scan_remotes, "at_stake", None)
    if fn is None:
        return None
    return fn(*args, **kw)


# ─────────── the count is what git counts ──────────────────────────────

def test_ahead_counts_the_unpushed_commits() -> None:
    work, pushed = repo_with(5, push=2)
    head = run(["rev-parse", "HEAD"], work)
    got = at_stake(work, "ahead", head, pushed, "main")
    if got is None:
        check("scan_remotes.at_stake exists", False,
              "ten rows ask push-or-delete with the size withheld")
        return
    check("three commits are unpushed", got.get("unpushed") == 3, str(got))
    check("and the newest of them carries a date",
          isinstance(got.get("newest_on"), str) and len(got["newest_on"]) == 10,
          str(got))


def test_a_local_only_branch_counts_what_no_remote_has() -> None:
    work, pushed = repo_with(4, push=1)
    run(["checkout", "-q", "-b", "side"], work)
    (work / "g").write_text("x\n", encoding="utf-8")
    run(["add", "g"], work)
    run(["commit", "-q", "-m", "side work"], work)
    head = run(["rev-parse", "HEAD"], work)
    got = at_stake(work, "local-only-branch", head, "", "side")
    if got is None:
        return
    # four commits on main of which one is pushed, plus one on the side branch:
    # four are reachable from HEAD and absent from every remote ref.
    check("it counts every commit no remote holds", got.get("unpushed") == 4, str(got))


def test_a_current_checkout_has_nothing_at_stake() -> None:
    work, pushed = repo_with(3, push=3)
    head = run(["rev-parse", "HEAD"], work)
    got = at_stake(work, "current", head, pushed, "main")
    if got is None:
        return
    check("a synchronised checkout reports nothing", got == {}, str(got))


# ─────────── absent is not zero ────────────────────────────────────────

def test_an_unanswerable_count_is_omitted() -> None:
    """Zero unpushed commits means "nothing at stake", the opposite of what every
    one of these states asserts. An unknown must not become a reassurance."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-stake-empty-"))
    got = at_stake(d, "ahead", "deadbeef", "cafebabe", "main")
    if got is None:
        return
    check("a path that is not a repository yields no keys", got == {}, str(got))
    check("and never a zero", got.get("unpushed") != 0, str(got))


def test_the_source_says_absent_is_not_zero() -> None:
    src = (ROOT / "collectors/scan_remotes.py").read_text(encoding="utf-8")
    i = src.find("def at_stake")
    body = src[i:src.find("\ndef ", i + 10)] if i != -1 else ""
    check("the reason is written where the function is",
          "zero" in body.lower() and ("opposite" in body.lower()
                                      or "reassur" in body.lower()),
          "a reader who does not know why an absence is kept will fill it in")


# ─────────── it reaches the registry and the row ───────────────────────

def test_the_registry_carries_it_for_the_live_estate() -> None:
    import json
    import paths
    f = paths.REGISTRY / "repositories.json"
    if not f.is_file():
        print("  SKIP  no registry here "
              "[covered: the git fixtures above measure the counting]")
        return
    repos = json.loads(f.read_text(encoding="utf-8"))["repositories"]
    risky = [r for r in repos
             if (r.get("local") or {}).get("sync") in
             ("ahead", "local-only-branch", "unpushed-and-remote-moved", "diverged")]
    if not risky:
        print("  SKIP  nothing on this estate holds unpushed work "
              "[covered: the git fixtures above]")
        return
    with_count = [r for r in risky if (r.get("local") or {}).get("unpushed") is not None]
    # A COUNT WHERE ONE IS MEASURABLE, and not everywhere. Two of six
    # `local-only-branch` checkouts have every commit reachable from some remote
    # ref: the branch NAME is unpublished, no work is exclusive, and omitting the
    # count is the honest answer — `build_findings` says so in the row rather
    # than printing a zero.
    exclusive = [r for r in risky
                 if (r.get("local") or {}).get("sync") != "local-only-branch"]
    missing = [r["id"] for r in exclusive if r not in with_count]
    check(f"every checkout whose state implies exclusive work carries a count "
          f"({len(exclusive)} of {len(risky)} at risk)",
          not missing,
          str(missing[:4]) + " — run `./observatory.py remotes`, `merge`, `emit`")
    check("and the estate's total is a number the operator can read",
          sum((r.get("local") or {}).get("unpushed") or 0 for r in risky) > 0,
          "a real estate carried over a hundred")


def test_the_finding_names_the_size_and_the_age() -> None:
    import build_findings as B
    rows = [f for f in B.collect()
            if f["type"] in ("clone.ahead", "clone.local-only-branch",
                             "clone.unpushed-and-remote-moved", "clone.diverged")]
    if not rows:
        print("  SKIP  no at-risk clone row on this estate "
              "[covered: the git fixtures above]")
        return
    import re
    without = [f for f in rows
               if not re.search(r"\d+ commit", f["detail"])
               and "No commit here is exclusive" not in f["detail"]]
    check("every at-risk row says how many commits are at stake, or that none is",
          not without, str([f["subject"] for f in without][:4]))
    # THE AGE OF THE WORK, which is what this case is named for. A bare date
    # anywhere in the sentence was a loose proxy, and it was being satisfied by
    # the date the REMOTE was asked — a different fact entirely, and one that
    # moved when that phrase gained a time of day. A row that
    # measured no exclusive work has no such age, and saying so is the point.
    dated = [f for f in rows
             if re.search(r"newest made \d{4}-\d{2}-\d{2}", f["detail"])
             or "No commit here is exclusive" in f["detail"]]
    check("and the rows carry the age of the work at stake", len(dated) == len(rows),
          f"{len(dated)} of {len(rows)}")


def test_measured_as_nothing_is_not_unmeasured() -> None:
    """The conflation this repository keeps removing, found in my own code from
    the previous iteration: `at_stake` returned `{}` both when a count could not
    be obtained and when it was obtained as ZERO, so the estate tile reported two
    measured checkouts as "not counted" and overstated its own uncertainty."""
    work, pushed = repo_with(2, push=2)
    run(["checkout", "-q", "-b", "side"], work)
    head = run(["rev-parse", "HEAD"], work)
    got = at_stake(work, "local-only-branch", head, "", "side")
    if got is None:
        return
    check("a branch whose commits are all on a remote says so",
          got.get("nothing_exclusive") is True, str(got))
    check("and carries no count, because there is nothing to count",
          "unpushed" not in got, str(got))
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-stake-none-"))
    unmeasured = at_stake(d, "ahead", "deadbeef", "cafebabe", "main")
    check("while an unanswerable one says nothing at all", unmeasured == {},
          str(unmeasured))


def test_the_page_carries_both_answers() -> None:
    import json as _json
    import re as _re
    import paths
    page = paths.DASHBOARD_HTML
    if not page.is_file():
        print("  SKIP  no built page here "
              "[covered: the git fixtures above measure both answers]")
        return
    m = _re.search(r"const D = (\{.*?\});\n", page.read_text(encoding="utf-8"), _re.S)
    if not m:
        check("the page carries a payload", False, "")
        return
    d = _json.loads(m.group(1))
    # The engine's tile is `unpushed_commits` ("commits on no remote").
    tile = {k: v for k, v in d["stats"].items() if k.startswith("unpushed")}
    check("the estate's total is a tile", bool(tile), str(sorted(d["stats"]))[:200])
    check("summing the planted count", tile.get("unpushed_commits") == 3, str(tile))
    risky = [r for row in d["rows"] for r in row.get("repos", [])
             if r.get("sync") in ("ahead", "local-only-branch",
                                  "unpushed-and-remote-moved", "diverged")]
    if not risky:
        print("  SKIP  nothing at risk on this estate "
              "[covered: the git fixtures above]")
        return
    silent = [r["nwo"] for r in risky
              if not r.get("unpushed") and not r.get("nothing_exclusive")]
    check("every at-risk repo on the page carries an answer", not silent,
          str(silent[:4]) + " — run remotes, merge, emit, dashboard")
    check("and the tile is a number or names its own uncertainty",
          isinstance(list(tile.values())[0], int)
          or any(w in str(list(tile.values())[0])
                 for w in ("не измерено", "не посчитаны", "нечего")),
          str(tile))


if __name__ == "__main__":
    print("at stake — ten decisions with the size and age withheld\n")
    for fn in (test_ahead_counts_the_unpushed_commits,
               test_a_local_only_branch_counts_what_no_remote_has,
               test_a_current_checkout_has_nothing_at_stake,
               test_an_unanswerable_count_is_omitted,
               test_the_source_says_absent_is_not_zero,
               test_the_registry_carries_it_for_the_live_estate,
               test_measured_as_nothing_is_not_unmeasured,
               test_the_page_carries_both_answers,
               test_the_finding_names_the_size_and_the_age):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mevery push-or-delete row says how much and how old\033[0m")
