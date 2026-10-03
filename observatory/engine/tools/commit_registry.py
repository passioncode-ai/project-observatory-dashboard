#!/usr/bin/env python3
"""Commit the generated registry, or explain why not.

`tools/commit_projection.py` solved this for the wiki on 2026-09-04
and the identical problem sat unnamed in this repository: the tick rewrites
`registry/*.json` every thirty minutes and nothing committed them, so the tree
was dirty within half an hour of any commit and a real abandoned edit was
invisible among the generated churn.

The sibling's rule was "refuse if anything outside the projection is modified".
That is right for the wiki, where the whole repository is the projection's home
and a dirty file there is probably an abandoned edit. It is wrong here: this
repository is under active development, so it is nearly always dirty, and a
committer that refuses on any dirt never runs.

What actually has to be true is narrower and testable — **the commit must
capture nothing but the registry**:

* only paths under `registry/` are staged, with an explicit pathspec;
* the index must already be clean, because `git commit` writes the whole index
  and a file somebody staged by hand would be swept in;
* no merge, rebase or cherry-pick may be in progress, since committing into one
  rewrites what it means;
* it never pushes — a commit is recoverable locally, a push is the operator's
  decision about what leaves the machine.

It is a writer of a guarded path like any other, so it asks agent-sync's own
guard first wherever the project declares coordination.
"""
from __future__ import annotations
import re
import argparse, json, pathlib, subprocess, sys

TOOLS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import tick_lease
sys.path.insert(0, str(TOOLS.parent))
import paths
import configuration
import safe_git

DEFAULT_ROOT = paths.HOME
REGISTRY = "registry"
IN_PROGRESS = ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")


def git(*args: str, cwd: pathlib.Path) -> tuple[int, str]:
    # Through `safe_git`, the engine's one git door. No signing: an unattended
    # commit that runs gpg's pinentry or an SSH signer can put a passphrase or
    # Keychain dialog in front of the operator. No hooks, no fsmonitor, and no
    # clean filter either: `git add` ran the operator's filter on every staged
    # file while the hook and signing overrides held. The author is the one the
    # operator's own git would use in this repository (`safe_git.identity`).
    p = safe_git.run(args, cwd=cwd, write=True, timeout=60)
    return p.returncode, (p.stdout + p.stderr).strip()


def may_write(root: pathlib.Path) -> tuple[bool, str]:
    """agent-sync's own answer, and only where the project declares coordination.

    A project with no `.claude/agent-sync.json` is not coordinated, and asking
    the guard there returns a denial about a config that does not exist — which
    would make this tool refuse in every checkout but one."""
    if not (root / ".claude/agent-sync.json").exists():
        return True, "coordination is not declared in this checkout"
    script = tick_lease.agent_sync_script()
    if script is None:
        return True, "agent-sync is not installed here"
    p = subprocess.run([sys.executable, str(script), "guard", f"{REGISTRY}/projects.json"],
                       cwd=root, capture_output=True, text=True, timeout=60)
    if p.returncode == 0:
        return True, (p.stdout.strip() or "allowed")
    return False, (p.stderr.strip() or p.stdout.strip() or "denied by agent-sync")


#: Fields whose movement is not worth a line in a commit subject. They are real
#: measurements asked only in the PRESENT tense — "how much uncommitted work sits
#: in this checkout right now" — and nothing keeps their history because nothing
#: needs it.
#:
#: Verified rather than assumed, because the first version of this comment
#: claimed their history lived in `events` and `project_week` and that was false:
#: `project_week` holds `commits`, `active_days` and `authors`, and `events` holds
#: `commit` and `finding.notified`. Which is exactly why `commits` is NOT in this
#: set — its history IS kept, per-event and per-week, so a change in it is a fact
#: about the estate rather than a reading of the moment.
#: `extra_checkouts` joins them for the same reason `extra_clones` was here, one
#: level deeper: an agent committing inside a worktree moves `commits`, `branch`
#: and `dirty` in that list, and a background committer that treats every such
#: move as a reason to commit would write a registry commit per agent turn. The
#: worktrees themselves appear and vanish within the hour.
VOLATILE = {"uncommitted_files", "files", "mtime", "extra_clones",
            "extra_checkouts"}


def describe_change(root: pathlib.Path, changed: list[str]) -> str:
    """What MOVED, in one line — not how big the registry is.

    The subject line used to be `Registry refresh: 156 projects, 172
    repositories, 200 relations`, which is the SIZE of the registry and changes
    almost never. Every commit the tick made therefore carried an identical
    subject: 48 a day, none of them searchable, and no way to tell a counter
    bump from a project appearing without opening the diff.

    Field names are read out of the staged diff. A JSON diff rewrites
    neighbouring lines when a list element moves, so a name appearing here is
    evidence that something in its neighbourhood changed rather than proof about
    that exact field — which is why the summary counts LINES per field and says
    so by ordering them, instead of claiming "field X changed".
    """
    code, diff = git("diff", "--cached", "--unified=0", "--", REGISTRY, cwd=root)
    if code != 0 or not diff.strip():
        code, diff = git("diff", "--unified=0", "--", REGISTRY, cwd=root)
    fields: dict[str, int] = {}
    ledger_rows = 0
    for line in diff.splitlines():
        if line[:1] not in "+-" or line[:3] in ("+++", "---"):
            continue
        if '"memory_id"' in line and line.startswith("+"):
            ledger_rows += 1
            continue
        m = re.match(r"[+-]\s*\"([a-z_]+)\":", line)
        if m:
            fields[m.group(1)] = fields.get(m.group(1), 0) + 1
    parts = []
    if ledger_rows:
        parts.append(f"{ledger_rows} ledger row(s)")
    named = [(n, c) for n, c in sorted(fields.items(), key=lambda kv: -kv[1])
             if n not in VOLATILE]
    volatile = sum(c for n, c in fields.items() if n in VOLATILE)
    parts += [f"{n} x{c}" for n, c in named[:4]]
    if len(named) > 4:
        parts.append(f"+{len(named) - 4} more field(s)")
    if volatile:
        parts.append(f"counters x{volatile}")
    if not parts:
        parts.append(f"{len(changed)} file(s)")
    return ", ".join(parts)


def private_workspace(root: pathlib.Path) -> tuple[bool, str]:
    """Never let scheduled commits stage runtime facts into source history."""
    root = root.resolve()
    source = paths.ROOT.resolve()
    if root == source or root in source.parents or source in root.parents:
        return False, "registry history must be separate from the program checkout"
    if (root / "observatory.py").exists() or (root / "store/schema.sql").exists():
        return False, "this directory contains program sources"
    if (root / REGISTRY).is_symlink():
        return False, "registry directory cannot be a symbolic link"
    if paths.REGISTRY.resolve() != root / REGISTRY:
        return False, "the Git root does not contain this workspace's configured registry"
    if (root / ".git").exists():
        code, top = git("rev-parse", "--show-toplevel", cwd=root)
        if code or pathlib.Path(top).resolve() != root:
            return False, "the requested workspace is not its own Git root"
    return True, "explicit private registry history; this tool never pushes"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    root = pathlib.Path(a.root).resolve()
    if not configuration.enabled("registry_history", "features"):
        print("Registry history disabled: enable features.registry_history for a private workspace Git repository")
        return 0
    allowed, why = private_workspace(root)
    if not allowed:
        print(f"NOT committing: {why}", file=sys.stderr)
        return 1

    if not (root / ".git").exists():
        print("not a git repository — nothing to commit")
        return 0

    code, out = git("status", "--porcelain", "--", REGISTRY, cwd=root)
    if code != 0:
        print(f"git status failed: {out}", file=sys.stderr)
        return 0
    # `XY path`, and a rename is `R  old -> new`; the status letters vary in width
    # across git versions, so the last whitespace-separated field is the only
    # slice that is right for every shape.
    changed = [l.split()[-1] for l in out.splitlines() if l.strip()]
    if not changed:
        print("the registry is clean — nothing to commit")
        return 0

    inflight = [n for n in IN_PROGRESS if (root / ".git" / n).exists()]
    if inflight:
        print(f"NOT committing: {inflight[0]} exists, so a merge or rebase is in progress. "
              "Committing into one rewrites what it means.", file=sys.stderr)
        return 0

    code, _ = git("diff", "--cached", "--quiet", cwd=root)
    if code != 0:
        _, staged = git("diff", "--cached", "--name-only", cwd=root)
        names = staged.split()
        print(f"NOT committing: {len(names)} path(s) are already staged — "
              f"{', '.join(names[:4])}"
              + (f" and {len(names) - 4} more" if len(names) > 4 else "")
              + ". `git commit` writes the whole index, so this would sweep up staged work. "
                "Commit that by hand first.", file=sys.stderr)
        return 0

    if a.dry_run:
        print(f"would commit {len(changed)} registry file(s): {', '.join(changed)}")
        print(f"  subject: Registry: {describe_change(root, changed)}")
        return 0

    allowed, why = may_write(root)
    if not allowed:
        print(f"NOT committing: {why}", file=sys.stderr)
        return 0

    summary = describe_change(root, changed)
    msg = (f"Registry: {summary}\n\n"
           f"Written by the collectors and committed by the tick that ran them, under the "
           f"`{tick_lease.REGISTRY_KEY}` lease. Only paths under {REGISTRY}/ are staged, and "
           f"the index was clean before this ran — a scheduled committer must not sweep up "
           f"work in progress.\n\n"
           f"Automated because the alternative, left in place since 2026-09-03, was a tree "
           f"dirty within half an hour of every commit — which makes a real abandoned edit "
           f"in the registry invisible among the generated churn; the wiki had the same "
           f"failure.")
    code, out = git("add", "--", REGISTRY, cwd=root)
    if code != 0:
        print("Could not stage the private registry", file=sys.stderr)
        return 1
    code, out = git("commit", "-q", "-m", msg, cwd=root)
    print(f"committed the registry: {summary} ({len(changed)} file(s))" if code == 0
          else f"commit failed: {out}")
    print("  not pushed — what leaves the machine is the operator's decision")
    return 0 if code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
