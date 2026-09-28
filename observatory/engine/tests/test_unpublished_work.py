#!/usr/bin/env python3
"""Work that exists on no remote, in the two places the estate could not see it.

An open question asked where a worktree belongs. Driving it produced a second
and worse answer beside it.

**Three git repositories on one machine had no remote at all**, and their own
history said they were committed to that same day:

    alpha-agent                 167 commits  plan/0016-product-program        2026-09-07
    beta-pilot                   47 commits  codex/product-readiness-plan-…   2026-09-07
    gamma-preview                 5 commits  codex/team-preview               2026-09-07  +8 dirty

The estate reported all three as `activity_tier: "unknown"` with
`last_activity_on: ""` — the three most recently worked-on projects on the
machine, in a system whose central question is which projects are active and
where work happened. `local.json` measured `last_commit` for every one of them;
`merge.py` put `mtime` in `local_only` and not the commit date, and `mtime` is
`null` for all three. The date was measured and then dropped one function later.

**And 219 commits existed on no remote with nothing saying so.** A repository with
no remote is the strongest form of "this survives only this disk" — stronger than
`clone.ahead`, which at least has somewhere to push — and it raised no finding,
because findings iterate `repositories.json` and a checkout with no remote has no
`owner/name` to be keyed by.

**The worktrees, which is what the question was about.** `merge.py` already
detects them (`.git` is a file reading `gitdir: …/worktrees/<name>`) and already
refuses to let one displace a real checkout — it demotes them into
`local.extra_clones`. But that list holds NAMES, so every measurement about a
worktree is discarded at the merge: ten `codex/*` branches of one project,
each existing on no remote, with nothing able to report them. `extra_clones` stays a name list because `plugins/disk_usage.py` resolves
folder names through it and a plugin must not change for a core need; the state
travels beside it in `extra_checkouts`.
"""
from __future__ import annotations
import importlib, json, os, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import subprocess
import tmp as tmpdir

# A private workspace of this suite's own, so the board reads the shipped
# default configuration rather than a machine's; each fixture below redirects
# the registry, scratch and store to its own planted copies.
for _name in ("OBSERVATORY_REGISTRY", "OBSERVATORY_DB", "OBSERVATORY_STATE", "OBSERVATORY_SCRATCH"):
    os.environ.pop(_name, None)
os.environ["OBSERVATORY_HOME"] = str(pathlib.Path(tmpdir.mkdtemp(prefix="observatory-unpub-home-")).resolve() / "home")
subprocess.run([sys.executable, str(ROOT / "observatory.py"), "init"], cwd=ROOT,
               capture_output=True, timeout=120, check=True)

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


# ─────────── the findings, from a planted registry ─────────────────────

def findings_for(repositories: list[dict], projects: list[dict] | None = None,
                 prefix: str = "") -> list[dict]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-unpub-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "registry/projects.json").write_text(json.dumps({"projects": projects or []}))
    (d / "registry/repositories.json").write_text(json.dumps(
        {"repositories": repositories}))
    (d / "registry/relations.json").write_text('{"relations": []}')
    os.environ.update(OBSERVATORY_REGISTRY=str(d / "registry"),
                      OBSERVATORY_SCRATCH=str(d / "scratch"),
                      OBSERVATORY_DB=str(d / "absent.db"))
    import paths
    importlib.reload(paths)
    import build_findings as B
    importlib.reload(B)
    try:
        return [f for f in B.collect() if f["type"].startswith(prefix)] if prefix \
            else B.collect()
    finally:
        for k in ("OBSERVATORY_REGISTRY", "OBSERVATORY_SCRATCH", "OBSERVATORY_DB"):
            os.environ.pop(k, None)
        importlib.reload(paths)


def repo_with(checkouts: list[dict], sync: str = "current") -> list[dict]:
    return [{"id": "repository:o/parent", "local": {
        "folder": "parent", "path": "/x/parent", "checked_out_branch": "main",
        "sync": sync, "remote_checked_on": "2026-09-07",
        "extra_clones": [c["folder"] for c in checkouts],
        "extra_checkouts": checkouts}}]


def wt(folder: str, branch: str, sync: str = "local-only-branch") -> dict:
    return {"folder": folder, "path": f"/x/{folder}", "worktree_of": "parent",
            "branch": branch, "sync": sync, "commits": 12, "dirty": 0}


def test_a_worktree_branch_on_no_remote_is_reported() -> None:
    got = findings_for(repo_with([wt("parent-a", "codex/a")]), prefix="worktree.")
    check("it fires", len(got) == 1, str(got)[:220])
    if not got:
        return
    f = got[0]
    check("as a warning — the branch exists nowhere else",
          f["severity"] == "warning", f["severity"])
    check("against the repository that owns the worktree",
          f["subject"] == "repository:o/parent", f["subject"])
    check("naming the branch", "codex/a" in f["detail"], f["detail"][:200])
    check("and the directory it is checked out in",
          "parent-a" in f["detail"], f["detail"][:200])
    check("the action offers both real choices",
          "push" in f["action"] and "worktree remove" in f["action"], f["action"])


def test_the_risk_is_stated_precisely_not_dramatically() -> None:
    """A worktree's commits live in the PARENT's object database, so deleting the
    directory does not lose them. Saying otherwise would send someone to rescue
    work that was never in danger — and the real danger, the disk, would read as
    the same alarm."""
    got = findings_for(repo_with([wt("parent-a", "codex/a")]), prefix="worktree.")
    if not got:
        check("a finding to inspect", False, "nothing fired")
        return
    d = got[0]["detail"]
    check("it says removing the worktree keeps the commits",
          "object database" in d or "parent's store" in d, d[:240])
    check("and that no remote has them", "no remote" in d, d[:240])


def test_ten_worktrees_of_one_project_are_one_finding() -> None:
    """The measured shape: ten `codex/*` worktrees of one project, left by one
    agent. Ten rows for one cause is how a findings list stops being read — the
    same rule the board applies to a full disk."""
    many = [wt(f"parent-{i}", f"codex/task-{i}") for i in range(10)]
    got = findings_for(repo_with(many), prefix="worktree.")
    check("one finding, not ten", len(got) == 1, str([f["title"] for f in got])[:200])
    if got:
        check("and it counts them", "10" in got[0]["title"], got[0]["title"])
        check("listing every branch",
              all(f"codex/task-{i}" in got[0]["detail"] for i in range(10)),
              got[0]["detail"][:300])


def test_a_worktree_that_is_current_raises_nothing() -> None:
    got = findings_for(repo_with([wt("parent-a", "main", sync="current")]),
                       prefix="worktree.")
    check("a worktree with nothing of its own is silent", got == [], str(got)[:200])


def test_an_at_risk_worktree_of_any_state_is_caught() -> None:
    """`local-only-branch` is not the only way a worktree holds unique work:
    `ahead` and `unpushed-and-remote-moved` mean the same thing."""
    for sync in ("ahead", "unpushed-and-remote-moved", "diverged"):
        got = findings_for(repo_with([wt("parent-a", "codex/a", sync=sync)]),
                           prefix="worktree.")
        check(f"a worktree that is {sync} is reported", len(got) == 1,
              f"{sync}: {str(got)[:140]}")
        if got:
            check(f"and the state is named for {sync}",
                  sync in got[0]["detail"], got[0]["detail"][:200])


def test_a_repository_with_no_extra_checkouts_is_unaffected() -> None:
    got = findings_for([{"id": "repository:o/p", "local": {
        "folder": "p", "path": "/x/p", "checked_out_branch": "main",
        "sync": "current", "remote_checked_on": "2026-09-07"}}], prefix="worktree.")
    check("no worktrees, no finding", got == [], str(got)[:160])


# ─────────── a git repository with no remote at all ────────────────────

def local_only(folder: str, commits: int, branch: str = "codex/x",
               dirty: int = 0, last: str = "2026-09-07") -> dict:
    return {"id": f"project:local-{folder}", "name": folder,
            "anchor": "local-folder", "ownership": "local-only",
            "lifecycle": "active", "last_activity_on": last,
            "membership_rules": [f"{folder}: git repository with no remote, local only"],
            "local_only": {"folder": folder, "path": f"/srv/projects/{folder}",
                           "kinds": ["node"], "unpublished": True,
                           "commits": commits, "branch": branch,
                           "last_commit": last, "dirty": dirty,
                           "readme": "", "files": 0, "mtime": ""}}


def test_a_remoteless_repository_is_reported_with_what_is_at_stake() -> None:
    got = findings_for([], [local_only("alpha-agent", 167,
                                       "plan/0016-product-program")], prefix="repo.")
    check("it fires", len(got) == 1, str(got)[:220])
    if not got:
        return
    f = got[0]
    check("as a warning", f["severity"] == "warning", f["severity"])
    check("against the project, since there is no repository id to use",
          f["subject"] == "project:local-alpha-agent", f["subject"])
    check("counting the commits", "167" in f["detail"], f["detail"][:200])
    check("naming the branch", "plan/0016-product-program" in f["detail"],
          f["detail"][:220])
    check("and saying there is no remote at all, not merely an unpushed branch",
          "no remote at all" in f["detail"], f["detail"][:240])
    check("the action does not invent a destination",
          "remote" in f["action"] and "deliberate" in f["action"], f["action"])


def test_uncommitted_files_are_named_beside_the_unpushed_history() -> None:
    """Eight files in `gamma-preview` are not even committed
    locally: a different loss, one `git push` would not have prevented."""
    got = findings_for([], [local_only("gamma-preview", 5,
                                       "codex/team-preview", dirty=8)], prefix="repo.")
    check("the finding fires", len(got) == 1, str(got)[:200])
    if got:
        check("and the uncommitted files are counted separately",
              "8" in got[0]["detail"] and "uncommitted" in got[0]["detail"],
              got[0]["detail"][:260])


def test_an_empty_git_init_is_not_lost_work() -> None:
    got = findings_for([], [local_only("scratch-thing", 0)], prefix="repo.")
    check("a repository with no commits raises nothing", got == [],
          "an empty `git init` is not work that could be lost")


def test_a_published_project_is_not_called_remoteless() -> None:
    p = local_only("published", 5)
    p["local_only"]["unpublished"] = False
    check("only `unpublished` folders qualify",
          findings_for([], [p], prefix="repo.") == [],
          "the flag is what merge.py sets from `is_git and no remote`")


# ─────────── the merge carries what the finding needs ──────────────────

def test_the_registry_carries_the_worktree_state_and_the_names() -> None:
    """`extra_clones` stays a list of NAMES: `plugins/disk_usage.py` resolves
    folders through it, and `plugins/README.md` promises a plugin need not change
    when the core does — so the state travels in a sibling field rather than by
    changing that shape underneath it."""
    src = (ROOT / "collectors/emit_registry.py").read_text(encoding="utf-8")
    check("the emitter writes both views", "extra_checkouts" in src
          and "extra_clones" in src, "")
    plug = (ROOT / "plugins/disk_usage.py").read_text(encoding="utf-8")
    check("and the disk plugin still reads the name list",
          'get("extra_clones")' in plug,
          "changing that shape would have made a core need edit a plugin")
    vol = (ROOT / "tools/commit_registry.py").read_text(encoding="utf-8")
    check("the new field is volatile, or the registry churns every tick",
          "extra_checkouts" in vol,
          "an agent committing in a worktree must not produce a registry commit")


def test_every_at_risk_state_is_a_state_the_collector_emits() -> None:
    """`AT_RISK` is spelled out rather than derived from severity, so a typo in
    it would be a member that silently never matches — a filter that quietly
    passes nothing."""
    sys.path.insert(0, str(ROOT / "collectors"))
    import scan_remotes as S
    import build_findings as B
    importlib.reload(S)
    importlib.reload(B)
    unknown = sorted(B.AT_RISK - S.STATES)
    check("no at-risk state is a name the collector never produces",
          not unknown, f"not real states: {unknown}")
    check("and every one of them is a warning in the clone table",
          all(B.SYNC_FINDINGS[s][0] == "warning" for s in B.AT_RISK),
          str({s: B.SYNC_FINDINGS[s][0] for s in sorted(B.AT_RISK)}))
    safe = sorted(S.STATES - B.AT_RISK - {"current"})
    check("the states left out are the ones with nothing unique here",
          safe == ["behind", "behind-or-diverged", "stale", "unknown",
                   "unreachable"], str(safe))


def test_a_local_only_project_reaches_activity_through_its_own_commit() -> None:
    """The measured failure: `last_commit` was read for all three and never
    carried, so `mtime` — `null` for every one of them — was the only candidate
    and the answer came out `unknown`."""
    src = (ROOT / "collectors/merge.py").read_text(encoding="utf-8")
    check("`local_only` carries the commit date",
          '"last_commit"' in src.split("local_only")[1][:400]
          if "local_only" in src else False,
          "the field merge puts in `local_only`")
    check("and the activity computation uses it",
          'local_only"]["last_commit"' in src or "local_only'][\"last_commit\"" in src
          or 'local_only"].get("last_commit")' in src,
          "line 392 previously offered only `mtime`")
    check("the reason is recorded where the fix is",
          "activity_tier" in src or "unknown" in src, "")


if __name__ == "__main__":
    print("unpublished work — 219 commits and ten branches on no remote\n")
    for fn in (test_a_worktree_branch_on_no_remote_is_reported,
               test_the_risk_is_stated_precisely_not_dramatically,
               test_ten_worktrees_of_one_project_are_one_finding,
               test_a_worktree_that_is_current_raises_nothing,
               test_an_at_risk_worktree_of_any_state_is_caught,
               test_a_repository_with_no_extra_checkouts_is_unaffected,
               test_a_remoteless_repository_is_reported_with_what_is_at_stake,
               test_uncommitted_files_are_named_beside_the_unpushed_history,
               test_an_empty_git_init_is_not_lost_work,
               test_a_published_project_is_not_called_remoteless,
               test_the_registry_carries_the_worktree_state_and_the_names,
               test_every_at_risk_state_is_a_state_the_collector_emits,
               test_a_local_only_project_reaches_activity_through_its_own_commit):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mno unique work reaches a surface and vanishes\033[0m")
