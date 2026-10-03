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
* **Never pushes.** A commit is recoverable locally; a push is the operator's
  decision about what leaves the machine.
"""
from __future__ import annotations
import argparse, json, subprocess, sys, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from datetime import datetime, timezone
import atomic
import paths

# The projection is the configured wiki's `inventory` folder, addressed relative
# to the Git repository that holds the wiki; the lease uses the same name.
PROJECTION = f"{paths.VAULT.name}/inventory"
LEASE_KEY = PROJECTION


def git(*args: str, cwd: pathlib.Path) -> tuple[int, str]:
    # No signing, as in commit_registry: an unattended commit must not run a
    # signer that can raise a passphrase or Keychain dialog.
    p = subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=cwd,
                       capture_output=True, text=True, timeout=60)
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
        git("add", "--", PROJECTION, cwd=vault)
        code, out = git("commit", "-q", "-m", msg, cwd=vault)
        print(f"committed the projection: {summary}" if code == 0
              else f"commit failed: {out}")
        report("committed" if code == 0 else "commit-failed",
               summary if code == 0 else out, files=len(changed))
        print("  not pushed — what leaves the machine is the operator's decision")
    finally:
        if held and script:
            subprocess.run([sys.executable, str(script), "release", LEASE_KEY], cwd=vault,
                           capture_output=True, text=True, timeout=60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
