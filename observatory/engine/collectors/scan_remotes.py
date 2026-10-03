#!/usr/bin/env python3
"""Ask every remote whether the local checkout is still current — no credential.

WHY THIS IS NOT A FORGE-SPECIFIC COLLECTOR. It was first reached for to fill
in repositories whose forge metadata is empty because no API token exists. But
the useful half of what SSH gives belongs to no single forge: across every
repository with a local clone, NONE recorded whether that clone had fallen
behind its remote. `local` held the checked-out branch, the commit count, the
last local commit and the uncommitted-file count — every one of them a fact
about the copy, none about the original. Building this under one forge's
collector would have hidden a machine-wide gap inside one forge's ticket.

WHAT IT COSTS AND WHAT IT REFUSES TO DO. One `git ls-remote` per repository,
run concurrently. It **never fetches**: a fetch would write objects into the
operator's repository, and an observatory that mutates what it observes is not
one. The price is that `behind` and `diverged` cannot always be told apart —
distinguishing them needs the remote commit locally.

**But the half of that question worth paying for is answerable anyway**, from
`refs/remotes/origin/<branch>` the clone already holds: whether there are
commits here that the remote did not have at the last fetch — work that would
die with the disk. So the ambiguous case splits into `stale` and
`unpushed-and-remote-moved`, and `behind-or-diverged` is kept for the one place
nothing local can answer it: no tracking ref to ask.

Not part of the thirty-minute tick: it is the only collector that makes a
network call per repository, and staleness does not change minute to minute.

    scan_remotes.py store/raw/remotes.json [--only <folder> ...]
"""
from __future__ import annotations
import concurrent.futures as cf
import json, os, pathlib, subprocess, sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths              
import safe_git          
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import local_scan              

WORKERS = 8
NET_TIMEOUT = 25


#: CREDENTIAL-FREE AND PROGRAM-FREE, through the engine's one git door
#: (`safe_git`). Credential-free means no credential STORE either: a remote that
#: answers 401 makes git ask every configured credential helper first — on macOS
#: that is `osxkeychain`, and from an unattended tick it can raise a Keychain
#: dialog in front of the operator. `safe_git` empties the helper list (system,
#: global, repository and URL-scoped alike), answers askpass with nothing, runs
#: ssh in BatchMode, and allows only the https, http, ssh, git and file
#: transports: a recorded origin such as `gcrypt::rsync://…` would otherwise
#: start `git-remote-gcrypt`, and through it gpg's pinentry. Such a remote is
#: reported unreachable with git's own "transport … not allowed".
#: `url.<base>.insteadOf` from the operator's configuration is still honoured,
#: so a remote rewritten to SSH is probed the way their own git reaches it.
def git(args: list[str], cwd: pathlib.Path | None = None, timeout: int = 10):
    try:
        r = safe_git.run(args, cwd=cwd, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except OSError as exc:
        return 127, "", f"git could not be run: {type(exc).__name__}: {exc}"


#: Every state this collector can put in `sync`. PUBLISHED, because both
#: consumers — the findings table and the dashboard's chip map — look the state
#: up and skip a miss in silence, so an unlisted state reaches neither surface
#: and nobody is told. `ahead` lived in that gap: eight clones held commits that
#: existed nowhere else and raised no finding. `unreachable` and
#: `unknown` are set by `merge.py` and `probe` rather than here, and are listed
#: because the consumers must cover them too.
STATES = frozenset({
    "current", "ahead", "behind", "behind-or-diverged", "diverged",
    "local-only-branch", "stale", "unpushed-and-remote-moved",
    "unreachable", "unknown",
})


def tracking_state(path: pathlib.Path, local_sha: str, branch: str) -> str | None:
    """Does this clone hold commits the remote did not have at the last fetch?

    Asked of `refs/remotes/origin/<branch>` — a ref the clone ALREADY holds, so
    no network call and, more to the point, no write into a repository this
    system only watches. That is what makes it askable at all: the caller's
    refusal to fetch is about mutating the subject, not about the question.

    Three answers, and the third is why this can return None: with no tracking
    ref the question genuinely needs a fetch, and the caller then keeps its old
    honest ambiguity instead of guessing a direction.

    What it does NOT claim: that the CURRENT remote lacks these commits. The ref
    is as old as the last fetch, and a force-push could have rewritten what came
    after it. The finding says "as of the last fetch" for that reason.
    """
    if not branch or branch == "HEAD":
        return None
    ref = f"refs/remotes/origin/{branch}"
    if git(["rev-parse", "--verify", "--quiet", ref], cwd=path)[0] != 0:
        return None
    if git(["merge-base", "--is-ancestor", local_sha, ref], cwd=path)[0] == 0:
        return "stale"
    return "unpushed-and-remote-moved"


def sync_state(path: pathlib.Path, local_sha: str, remote_sha: str,
               branch: str = "") -> str:
    """Direction, or an honest refusal to guess it."""
    if not remote_sha:
        return "local-only-branch"
    if local_sha == remote_sha:
        return "current"
    if git(["cat-file", "-e", remote_sha + "^{commit}"], cwd=path)[0] != 0:
        # The remote commit is not in this clone, and getting it would mean
        # writing to the operator's repository. But the half that MATTERS here —
        # is there work on this disk and nowhere else — is answerable from the
        # tracking ref without either. Ask that; refuse only what is left.
        return tracking_state(path, local_sha, branch) or "behind-or-diverged"
    if git(["merge-base", "--is-ancestor", remote_sha, local_sha], cwd=path)[0] == 0:
        return "ahead"
    if git(["merge-base", "--is-ancestor", local_sha, remote_sha], cwd=path)[0] == 0:
        return "behind"
    return "diverged"



#: The states where work exists on this disk and possibly nowhere else. Only
#: these are counted: `behind` and `current` have nothing at stake, and asking
#: git about them would be a walk for an answer nobody reads.
AT_RISK_STATES = ("ahead", "unpushed-and-remote-moved", "local-only-branch",
                  "diverged")


def at_stake(path, state: str, local_sha: str, remote_sha: str,
             branch: str = "") -> dict:
    """How many commits are at stake and when the newest was made.

    Ten `clone.*` rows asked the operator to push or delete a branch while
    withholding both — one may hold a single typo from an hour ago and another
    forty-four commits of a feature branch from March, and the rows read
    identically.

    The range per state:

        ahead                      remote_sha..HEAD
        unpushed-and-remote-moved  the tracking ref..HEAD
        local-only-branch          HEAD --not --remotes, since no remote has it
        diverged                   remote_sha..HEAD, the local side of the fork

    ABSENT, NEVER ZERO. A count that cannot be obtained is omitted, because zero
    unpushed commits means "nothing at stake" — the OPPOSITE of what every one of
    these states asserts — and an unknown reported as zero would turn the row
    into a reassurance. Same rule as every other measurement here: measured, not
    measured, or could not ask.
    """
    if state not in AT_RISK_STATES:
        return {}
    if state == "local-only-branch":
        args = ["rev-list", "--count", "HEAD", "--not", "--remotes"]
        log = ["log", "-1", "--format=%cs", "HEAD"]
    elif state == "unpushed-and-remote-moved":
        ref = f"refs/remotes/origin/{branch}" if branch else "refs/remotes/origin/HEAD"
        args = ["rev-list", "--count", f"{ref}..HEAD"]
        log = ["log", "-1", "--format=%cs", f"{ref}..HEAD"]
    else:
        if not remote_sha:
            return {}
        args = ["rev-list", "--count", f"{remote_sha}..HEAD"]
        log = ["log", "-1", "--format=%cs", f"{remote_sha}..HEAD"]
    code, out, _ = git(args, cwd=path)
    if code != 0 or not out.strip().isdigit():
        return {}
    n = int(out.strip())
    if n <= 0:
        # MEASURED AS NOTHING, which is not the same as unmeasured — and `{}`
        # for both was the conflation this repository removes wherever it finds
        # it. Two `local-only-branch` checkouts hold no commit that some remote
        # lacks: the branch NAME is unpublished, no work is. A reader that cannot
        # tell this from "the probe did not run" reports the second as the first
        #.
        return {"nothing_exclusive": True}
    got: dict = {"unpushed": n}
    code, when, _ = git(log, cwd=path)
    if code == 0 and len(when.strip()) == 10:
        got["newest_on"] = when.strip()
    return got


def probe(rec: dict) -> tuple[str, dict]:
    folder, path = rec["folder"], pathlib.Path(rec["path"])
    url, branch = rec.get("remote", ""), rec.get("branch", "")
    out: dict = {"remote": url, "checked_at": datetime.now(timezone.utc)
                 .strftime("%Y-%m-%dT%H:%M:%SZ")}
    if not url:
        out.update(reachable=False, reason="no origin remote")
        return folder, out

    refs = ["HEAD"] + ([f"refs/heads/{branch}"] if branch and branch != "HEAD" else [])
    code, stdout, stderr = git(["ls-remote", "--symref", url, *refs], timeout=NET_TIMEOUT)
    if code != 0:
        # A permission failure and a deleted repository are different facts and
        # the operator needs to tell them apart; git says which, so pass it on.
        out.update(reachable=False, reason=(stderr.splitlines() or ["unknown"])[-1][:200])
        return folder, out

    default_branch, remote_head, branch_sha = "", "", ""
    for line in stdout.splitlines():
        if line.startswith("ref: ") and line.endswith("HEAD"):
            default_branch = line.split()[1].removeprefix("refs/heads/")
        elif "\t" in line:
            sha, ref = line.split("\t", 1)
            if ref == "HEAD":
                remote_head = sha
            elif ref == f"refs/heads/{branch}":
                branch_sha = sha

    local_sha = git(["rev-parse", "HEAD"], cwd=path)[1]
    state = sync_state(path, local_sha, branch_sha, branch) if local_sha else "unknown"
    out.update(reachable=True, default_branch=default_branch, remote_head=remote_head,
               branch=branch, branch_remote_sha=branch_sha, sync=state)
    # HOW MUCH IS AT STAKE, for the states that assert work on this disk. The
    # state is already known here and the checkout is already open, so this is
    # one read-only walk beside a network round trip.
    out.update(at_stake(path, state, local_sha, branch_sha, branch))
    return folder, out


def main(argv: list[str]) -> int:
    import configuration
    if not configuration.enabled("git_remotes"):
        print("SKIP remote Git probes: integration git_remotes is disabled")
        return 0
    if len(argv) < 2:
        sys.exit(__doc__)
    dest = pathlib.Path(argv[1])
    only = set(argv[argv.index("--only") + 1:]) if "--only" in argv else None

    # `paths.SCRATCH`: see scan_bitbucket for the class this belongs to.
    src = paths.SCRATCH / "local.json"
    if not src.is_file():
        sys.exit(f"scan_remotes: no {src} — run scan_filesystem.py first")
    rows = [r for r in local_scan.folders(src)
            if r.get("is_git") and (only is None or r["folder"] in only)]
    if not rows:
        print("scan_remotes: no git checkout to probe")

    result: dict[str, dict] = {}
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for folder, info in pool.map(probe, rows):
            result[folder] = info

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps({
        "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repositories": result,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    tally: dict[str, int] = {}
    for info in result.values():
        key = info.get("sync") if info.get("reachable") else "unreachable"
        tally[key or "unknown"] = tally.get(key or "unknown", 0) + 1
    print(f"probed {len(result)} remote(s) -> {dest}")
    for k in sorted(tally, key=lambda k: -tally[k]):
        print(f"  {k:20s} {tally[k]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
