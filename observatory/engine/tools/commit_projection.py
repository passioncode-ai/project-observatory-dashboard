#!/usr/bin/env python3
"""Commit the generated projection in the wiki, under a lease, or explain why not.

The tick rewrites a few files in the wiki on every run and nothing committed
them, so the repository was dirty soon after any commit and the coordination
tool's `finish` step reported it — correctly, because it cannot tell a
generated refresh from an abandoned edit.

Three rules keep a scheduled committer from being worse than the problem:

* **Only the projection.** If anything outside `inventory/` is modified, this
  stops and touches nothing: a scheduled job must never sweep up a human's work
  in progress.
* **Under a lease.** `inventory/` is a guarded path in that repository, and a
  background job is exactly the second writer the lease exists for.
* **On origin, then pushed** (OBS-516). The wiki's source is its `origin`: a
  commit that stays on one machine splits the wiki, and on 2026-11-11 an
  unpushed projection commit left the operator's checkout two ahead and two
  behind. So the commit is built in a temporary index on top of the upstream
  branch, holding only the projection, and pushed, again on a fresh base if
  another writer won the race. The local branch then follows only by a fast
  forward that overwrites nothing local; a branch with no upstream keeps the
  local commit, as before.

A checkout marked as a read mirror (`wiki_checkout.MIRROR_KEY`) is not written.
"""
from __future__ import annotations
import argparse, json, subprocess, sys, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from datetime import datetime, timezone
import os, tempfile
import atomic
import paths
import safe_git
import wiki_checkout

# The projection is the configured wiki's `inventory` folder, addressed relative
# to the Git repository that holds the wiki; the lease uses the same name.
PROJECTION = f"{paths.VAULT.name}/inventory"
LEASE_KEY = PROJECTION


def git(*args: str, cwd: pathlib.Path) -> tuple[int, str]:
    # Through `safe_git`, as in commit_registry: an unattended commit must not
    # run a signer that can raise a passphrase or Keychain dialog, nor the
    # operator's hooks, fsmonitor or clean filters. Signing was the only thing
    # turned off here, and one tick ran pre-commit, prepare-commit-msg,
    # commit-msg, post-commit and reference-transaction hooks from the
    # operator's global hooks directory.
    p = safe_git.run(args, cwd=cwd, write=True, timeout=60)
    return p.returncode, (p.stdout + p.stderr).strip()


def agent_sync() -> pathlib.Path | None:
    """One resolver, shared with the registry committer.

    This had its own copy that sorted the version directories as STRINGS, which
    puts 1.100.0 below 1.19.2 and silently runs an old copy of the coordination
    tool. `tick_lease.agent_sync_script` compares them as numbers."""
    sys.path.insert(0, str(ROOT / "tools"))
    import tick_lease
    return tick_lease.agent_sync_script()


def report(outcome: str, detail: str = "", **extra) -> None:
    """The run's outcome as a fact, on EVERY path.

    Every refusal here returns 0 deliberately — a scheduled job must not fail
    because the wiki is dirty — and a refusal that is only a line on stdout
    lets the projection go uncommitted for a week because one stray file sat
    in the wiki, while the tick's `step` sees exit 0 and nobody learns. So the
    outcome is recorded as a fact, the same early-return shape the digest, the
    findings, the notifications and the plugin runner already use.

    `last_commit` is read from the wiki itself, so a reader can tell a refusal
    that happened once from one that has been standing for days.
    """
    last = ""
    try:
        code, out = git("log", "-1", "--format=%cI", cwd=paths.VAULT.parent)
        if code == 0:
            last = out.strip()
    except Exception:
        pass
    try:
        paths.SCRATCH.mkdir(parents=True, exist_ok=True)
        atomic.write_json(paths.SCRATCH / "commit-projection.json", {
            "ran_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "outcome": outcome, "detail": detail[:300],
            "wiki_last_commit": last, **extra})
    except OSError as exc:
        print(f"could not write the commit report: {exc}", file=sys.stderr)


class PushError(Exception):
    """A step of the push failed; the remote branch is left as it was."""


def out(*args: str, cwd: pathlib.Path, env: dict | None = None, input: str | None = None) -> str:
    p = safe_git.run(args, cwd=cwd, write=True, timeout=120, extra_env=env, input=input)
    if p.returncode != 0:
        raise PushError(f"git {args[0]}: {(p.stderr or '').strip()[:300] or p.returncode}")
    return p.stdout.strip()


def upstream(vault: pathlib.Path) -> tuple[str, str] | None:
    """(remote, remote branch) the checked-out branch tracks, read from its configuration."""
    code, branch = git("symbolic-ref", "--short", "-q", "HEAD", cwd=vault)
    if code != 0 or not branch:
        return None
    code, remote = git("config", f"branch.{branch}.remote", cwd=vault)
    code2, merge = git("config", f"branch.{branch}.merge", cwd=vault)
    if code or code2 or not remote or remote == "." or not merge.startswith("refs/heads/"):
        return None
    return remote, merge[len("refs/heads/"):]


def projection_tree(top: pathlib.Path, base: str, prefix: str) -> str:
    """The tree of `base` with the projection folder as the working copy holds it, built in a
    temporary index: nothing outside the projection, staged or not, reaches the commit."""
    folder = prefix + PROJECTION
    with tempfile.TemporaryDirectory(prefix="observatory-projection-") as tmp:
        env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        out("read-tree", base, cwd=top, env=env)
        # Drop the folder as base has it, then add it as it is on disk: a file the projection
        # no longer writes leaves the commit too.
        out("rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", folder, cwd=top, env=env)
        if (top / folder).is_dir():
            out("add", "-f", "--", folder, cwd=top, env=env)
        return out("write-tree", cwd=top, env=env)


def push_on_origin(vault: pathlib.Path, branch: tuple[str, str], msg: str) -> tuple[str, str]:
    """Commit the projection on the upstream branch and push it; three tries against a racing writer."""
    remote, remote_branch = branch
    top = pathlib.Path(out("rev-parse", "--show-toplevel", cwd=vault))
    prefix = out("rev-parse", "--show-prefix", cwd=vault)
    tracking = f"refs/remotes/{remote}/{remote_branch}"
    ident = safe_git.identity(top)
    last = ""
    for _ in range(3):
        out("fetch", "-q", remote, cwd=top)
        base = out("rev-parse", "--verify", tracking, cwd=top)
        tree = projection_tree(top, base, prefix)
        if tree == out("rev-parse", f"{base}^{{tree}}", cwd=top):
            return "clean", follow(top, base, prefix + PROJECTION)
        commit = out("commit-tree", tree, "-p", base, "-m", msg, cwd=top, env=ident)
        p = safe_git.run(["push", "-q", remote, f"{commit}:refs/heads/{remote_branch}"],
                         cwd=top, write=True, timeout=120)
        if p.returncode == 0:
            safe_git.run(["fetch", "-q", remote], cwd=top, write=True, timeout=120)
            return "pushed", follow(top, commit, prefix + PROJECTION)
        last = (p.stderr or "").strip()[:300]
    raise PushError(last or "the push was refused three times")


def follow(top: pathlib.Path, commit: str, folder: str) -> str:
    """Move the local branch to `commit` when that is a fast forward that overwrites no local edit.

    After a successful push a failure here only leaves the branch where it was, and says why."""
    try:
        head = out("rev-parse", "HEAD", cwd=top)
        if head == commit:
            return ""
        ahead = out("rev-list", "--count", f"{commit}..{head}", cwd=top)
        if ahead != "0":
            return f"; the local branch stays: {ahead} local commit(s) are not on origin"
        if out("ls-files", "-u", cwd=top):
            return "; the local branch stays: a merge is unfinished"
        # A two-way read-tree updates only the files that differ between head and commit, and
        # refuses when such a file has a local edit. The projection on disk equals `commit`, so
        # its index entries are staged first, and put back as they were if the read-tree refuses.
        saved = out("ls-files", "-s", "--", folder, cwd=top)
        if saved or (top / folder).exists():
            out("add", "-A", "-f", "--", folder, cwd=top)
        r = safe_git.run(["read-tree", "-m", "-u", head, commit], cwd=top, write=True, timeout=120)
        if r.returncode != 0:
            safe_git.run(["reset", "-q", "--", folder], cwd=top, write=True, timeout=120)
            if saved:
                safe_git.run(["update-index", "--index-info"], cwd=top, write=True, timeout=120,
                             input=saved + "\n")
            busy = "index.lock" in (r.stderr or "")
            return ("; the local branch stays: "
                    + ("the index is in use" if busy else "following would overwrite a local edit"))
        ref = out("symbolic-ref", "HEAD", cwd=top)
        out("update-ref", "-m", "projection: follow origin", ref, commit, head, cwd=top)
        return ""
    except PushError as exc:
        return f"; the local branch stays: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    vault = paths.VAULT.parent
    if not (vault / ".git").exists():
        print("the wiki is not a git repository — nothing to commit")
        report("no-git", "the wiki is not a git repository")
        return 0
    refused = wiki_checkout.mirror(vault)
    if refused:
        print(f"not committing: {refused}")
        report("mirror", refused)
        return 0

    code, out = git("status", "--porcelain", cwd=vault)
    if code != 0:
        print(f"git status failed: {out}", file=sys.stderr)
        report("status-failed", out)
        return 0
    # `XY path`, but a rename is `R  old -> new` and the status letters vary in
    # width across git versions. Taking the last whitespace-separated field is
    # right for every shape, and slicing a fixed offset was not — it ate a
    # character off the first path.
    changed = [l.split()[-1] for l in out.splitlines() if l.strip()]
    if not changed:
        print("the wiki is clean — nothing to commit")
        report("clean", "nothing to commit")
        return 0
    outside = [c for c in changed if not c.startswith(PROJECTION)]
    if outside:
        print(f"NOT committing: {len(outside)} change(s) outside the projection — "
              f"{', '.join(outside[:4])}"
              + (f" and {len(outside) - 4} more" if len(outside) > 4 else "")
              + "\n  A scheduled job must not sweep up work in progress. Commit by hand, "
                "or re-run once the tree holds only the projection.", file=sys.stderr)
        report("refused-foreign-changes",
               f"{len(outside)} change(s) outside the projection: "
               f"{', '.join(outside[:6])}", outside=outside[:20])
        return 0
    if a.dry_run:
        print(f"would commit {len(changed)} projection file(s): {', '.join(changed)}")
        # A dry run reports nothing: it is a question, and answering it must
        # not overwrite the record of what the last real run did.
        return 0

    script = agent_sync()
    held = False
    if script:
        code, out = subprocess.run(
            [sys.executable, str(script), "acquire", LEASE_KEY], cwd=vault,
            capture_output=True, text=True, timeout=60).returncode, ""
        held = code == 0
        if not held:
            print("could not take the lease on the projection — someone else is holding it. "
                  "Leaving the tree dirty is the right answer here.", file=sys.stderr)
            report("lease-held-elsewhere",
                   "another run holds the projection lease")
            return 0
    try:
        counts = {}
        for name in ("projects", "repositories", "relations"):
            f = paths.REGISTRY / f"{name}.json"
            if f.exists():
                d = json.loads(f.read_text(encoding="utf-8"))
                counts[name] = len(d.get(name, []))
        summary = ", ".join(f"{v} {k}" for k, v in counts.items())
        msg = (f"Projection refresh: {summary}\n\n"
               f"Generated by project-observatory/tools/project_into_vault.py and committed "
               f"by its tick. Nothing here is a source: every file carries _generated and "
               f"_do_not_edit, and the canonical registry is in that repository.\n\n"
               f"Committed automatically because the alternative — leaving three files dirty "
               f"every thirty minutes — makes a real abandoned edit in this directory "
               f"invisible among the generated ones.")
        branch = upstream(vault)
        if branch is None:
            git("add", "--", PROJECTION, cwd=vault)
            code, out = git("commit", "-q", "-m", msg, cwd=vault)
            print(f"committed the projection: {summary}" if code == 0
                  else f"commit failed: {out}")
            report("committed" if code == 0 else "commit-failed",
                   summary if code == 0 else out, files=len(changed), pushed=False)
            print("  not pushed: the wiki's branch has no upstream")
        else:
            try:
                outcome, said = push_on_origin(vault, branch, msg)
            except PushError as exc:
                print(f"the projection was not pushed: {exc}", file=sys.stderr)
                report("push-failed", str(exc), files=len(changed), pushed=False)
            else:
                print(f"{outcome}: {summary}{said}")
                report(outcome, summary + said, files=len(changed),
                       pushed=outcome == "pushed")
    finally:
        if held and script:
            subprocess.run([sys.executable, str(script), "release", LEASE_KEY], cwd=vault,
                           capture_output=True, text=True, timeout=60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
