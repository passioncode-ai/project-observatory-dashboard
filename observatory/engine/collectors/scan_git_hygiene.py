#!/usr/bin/env python3
"""Worktrees and branches that outlived their work, per registered checkout.

WHY. On 2026-09-27 one machine held 180 worktrees and 1,120 local branches.
Most were the residue of agents — a Codex or Claude Code worktree per task,
left behind when the task ended — and the disk was full. Sorting them into
"safe to drop" and "the only copy of somebody's work" took an afternoon of
hand-written scripts. This collector does that sorting on every tick, so the
cleanup (tools/cleanup.py) only ever acts on a classification it can show.

CLASSES, for a branch (never the default branch, never one checked out):
  merged        no commit that the default branch lacks
  patch-merged  every commit has an equivalent on the default branch (squash/rebase)
  pushed        identical to an upstream that still exists
  unique        commits that exist nowhere else — somebody's only copy
and for a worktree: `missing` (its directory is gone), `clean` or `dirty`, its
idle age, whether a process has its working directory inside it (`busy`), and
whether it is `locked`.

Idle age is the newest of the branch tip's commit and its reflog, because a
freshly created branch sits on an old commit and would otherwise read as stale.

    scan_git_hygiene.py OUT.json
    scan_git_hygiene.py REGISTRATION.json --registrations-only --budget-seconds 20

Registration is a separate, bounded metadata pass. It does not classify branches,
inspect working-tree changes or authorize cleanup. Its cursor survives an interrupted
pass and each carried repository keeps its original registration time and stale flag.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "collectors"))
from cleanup_safety import worktree_presence  # noqa: E402
import paths  # noqa: E402
import safe_git  # noqa: E402


def git(repo: str, *args: str, timeout: float = 60) -> str | None:
    """stdout, or None when git would not answer. A survey: through `safe_git`,
    which reads without taking optional locks and runs none of the programs the
    operator's or the repository's configuration names (signature display ran
    gpg here, and `status` in every worktree ran the fsmonitor)."""
    try:
        p = safe_git.run(args, repo=repo, timeout=timeout, extra_env={"LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    return p.stdout if p.returncode == 0 else None


def checkout_seeds(degraded: list[dict] | None = None) -> list[dict]:
    """Independent source reads: one corrupt registry never hides the other source.

    These are candidate anchors, not repository classifications. Git itself
    validates them when registration is requested; a linked worktree's .git file
    must not be rejected just because it is not a directory.
    """
    errors = degraded if degraded is not None else []

    def source(name: str, key: str) -> list:
        try:
            doc = json.loads((paths.REGISTRY / name).read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or not isinstance(doc.get(key), list):
                raise ValueError("invalid registry shape")
            return doc[key]
        except (OSError, ValueError):
            errors.append({"source": name, "reason": "registry could not be read"})
            return []

    found = []
    for row in source("repositories.json", "repositories"):
        if not isinstance(row, dict):
            errors.append({"source": "repositories.json", "reason": "invalid repository row"})
            continue
        local = row.get("local")
        path = local.get("path") if isinstance(local, dict) else None
        if isinstance(path, str) and path and isinstance(row.get("id"), str):
            found.append({"repository": row["id"], "path": os.path.realpath(path)})
    for row in source("projects.json", "projects"):
        if not isinstance(row, dict):
            errors.append({"source": "projects.json", "reason": "invalid project row"})
            continue
        folders = row.get("local_folders") or []
        if not isinstance(folders, list):
            errors.append({"source": "projects.json", "reason": "invalid project folders"})
            continue
        for folder in folders:
            if not isinstance(folder, str):
                errors.append({"source": "projects.json", "reason": "invalid project folder"})
                continue
            path = os.path.realpath(os.path.join(str(paths.DATA), folder))
            # A project's ordinary non-Git folder is not a failed Git source.
            if os.path.lexists(os.path.join(path, ".git")):
                found.append({"repository": f"local:{folder}", "path": path})
    unique = {}
    for row in found:
        unique.setdefault(row["path"], row)
    return list(unique.values())


def checkouts(degraded: list[dict] | None = None) -> list[dict]:
    """The full hygiene collector's validated anchors, including linked worktrees."""
    errors = degraded if degraded is not None else []
    out = []
    for row in checkout_seeds(errors):
        marker = os.path.join(row["path"], ".git")
        if os.path.isdir(marker) or (os.path.isfile(marker) and
                                    git(row["path"], "rev-parse", "--git-dir") is not None):
            out.append(row)
        else:
            errors.append({"source": row["repository"], "path": row["path"],
                           "reason": "registered checkout could not be read"})
    return out


def remote_branches(repo: str) -> list[str]:
    """`origin`'s branches as this checkout last fetched them, without `HEAD`."""
    out = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin") or ""
    return sorted(b.split("/", 1)[1] for b in out.split()
                  if "/" in b and b != "origin/HEAD" and b != "origin")


def default_branch(repo: str) -> str | None:
    """The branch every other branch is measured against, or None.

    In order: what `origin/HEAD` records; `main` or `master`; and, when the
    remote has exactly ONE branch, that branch. The third rule is what a checkout
    cloned without `origin/HEAD` needed: measured on one estate, a repository
    whose only branch is `develop` (and whose remote's own `HEAD` names
    `develop`) was reported as having no default branch at all, because only the
    first two rules existed. A remote with one branch has no other candidate, so
    nothing is guessed; with two or more and no `origin/HEAD`, None stands and
    `unresolved_reason` says how to record it.
    """
    head = (git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD") or "").strip()
    if head:
        return head.split("/", 1)[1]
    for name in ("main", "master"):
        if git(repo, "rev-parse", "--verify", "-q", "refs/heads/" + name) is not None:
            return name
    remote = remote_branches(repo)
    if len(remote) == 1:
        return remote[0]
    return None


def unresolved_reason(repo: str) -> str:
    """Why `default_branch` found nothing, and the one command that fixes it."""
    remote = remote_branches(repo)
    if not remote:
        return ("no default branch could be resolved: origin/HEAD is unset, there is no "
                "main or master, and no branch of origin has been fetched")
    return (f"no default branch could be resolved: origin/HEAD is unset and origin has "
            f"{len(remote)} branches ({', '.join(remote[:4])}{', …' if len(remote) > 4 else ''}), "
            f"none named main or master; `git remote set-head origin --auto` in the "
            f"checkout records the remote's own choice")


def worktree_records(repo: str, timeout: float = 60) -> list[dict] | None:
    """Git's registration metadata only; no working-tree status, log or branch walk."""
    text = git(repo, "worktree", "list", "--porcelain", timeout=timeout)
    if text is None:
        return None
    rows, cur = [], {}
    for line in text.splitlines() + [""]:
        if not line:
            if cur:
                if not isinstance(cur.get("worktree"), str) or not os.path.isabs(cur["worktree"]):
                    return None             # ambiguous/quoted path is not a usable registration
                rows.append(cur)
                cur = {}
            continue
        key, _, value = line.partition(" ")
        cur[key] = value or True
    return rows or None


def worktrees(repo: str, busy: set[str], now: float) -> list[dict] | None:
    rows = worktree_records(repo)
    if rows is None:
        return None
    out = []
    for w in rows[1:]:  # the first entry is the main checkout
        path = w["worktree"]
        entry = {"path": path, "branch": str(w.get("branch", "")).replace("refs/heads/", "") or None,
                 "head": w.get("HEAD"), "locked": "locked" in w}
        presence = worktree_presence(path)
        if presence == "absent":
            entry["state"] = "missing"
            out.append(entry)
            continue
        if presence == "unknown" or not os.path.isdir(path):
            # An unplugged drive, a renamed parent or a permission error is not a deleted
            # worktree: its commits may exist nowhere else. The pass names it and plans nothing.
            entry["state"] = "unreadable"
            entry["presence"] = "unknown"
            out.append(entry)
            continue
        status = git(path, "status", "--porcelain", "--untracked-files=normal", timeout=120)
        entry["dirty_files"] = len(status.splitlines()) if status is not None else None
        entry["state"] = "dirty" if entry["dirty_files"] else ("clean" if status is not None else "unreadable")
        ct = (git(path, "log", "-1", "--format=%ct") or "").strip()
        try:
            newest = max(os.path.getmtime(os.path.join(path, n)) for n in os.listdir(path))
        except (OSError, ValueError):
            newest = 0
        stamps = [t for t in (int(ct) if ct.isdigit() else 0, newest) if t]
        entry["idle_days"] = round((now - max(stamps)) / 86400, 1) if stamps else None
        entry["busy"] = any(b == path or b.startswith(path + "/") for b in busy)
        out.append(entry)
    return out


def branches(repo: str, default: str, in_worktree: set[str], now: float) -> list[dict] | None:
    fmt = "%(refname:short)\t%(committerdate:unix)\t%(ahead-behind:" + default + ")\t%(upstream:short)\t%(upstream:track)"
    out = []
    listed = git(repo, "for-each-ref", "--format=" + fmt, "refs/heads")
    if listed is None:
        return None
    for line in listed.splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        name, ct, ab, upstream, track = parts
        if name == default or name in in_worktree:
            continue
        ahead = int((ab.split() or ["0"])[0]) if ab else 0
        rl = (git(repo, "reflog", "show", "--format=%ct", "-n1", "refs/heads/" + name) or "").strip()
        last = max(int(ct or 0), int(rl) if rl.isdigit() else 0)
        if ahead == 0:
            cls = "merged"
        else:
            cherry = git(repo, "cherry", default, name) or ""
            if cherry and all(c.startswith("-") for c in cherry.splitlines()):
                cls = "patch-merged"
            elif upstream and "gone" not in track and \
                    (git(repo, "rev-list", "--count", f"{upstream}..{name}") or "1").strip() == "0":
                cls = "pushed"
            else:
                cls = "unique"
        out.append({"name": name, "class": cls, "ahead": ahead, "idle_days": round((now - last) / 86400, 1),
                    **({"upstream": upstream} if upstream else {}), **({"upstream_gone": True} if "gone" in track else {})})
    return out


#: Seconds kept back for writing the answer when the step's deadline nears (OBS-52).
RESERVE_S = 20.0


def _previous(out_path: pathlib.Path) -> dict[str, dict]:
    """The last answer's rows by checkout path: what an unreached checkout carries, and the order of the next pass."""
    try:
        doc = json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {r["path"]: r for r in doc.get("checkouts", []) if isinstance(r, dict) and r.get("path")}


def registrations_only(out_path: pathlib.Path, seconds: float) -> int:
    """Publish cheap registration independently of the expensive hygiene survey.

    The cursor is checkpointed before every Git call. A process killed at a slow
    anchor therefore starts after that anchor next time. Carried rows stay stale;
    registration says nothing about clean/dirty state or permission to delete.
    """
    import step_budget
    end = time.monotonic() + min(seconds, max(0.0, step_budget.remaining()))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        previous = json.loads(out_path.read_text(encoding="utf-8"))
        if not isinstance(previous, dict) or previous.get("kind") != "worktree-registration":
            previous = {}
    except (OSError, ValueError):
        previous = {}
    degraded: list[dict] = []
    seeds = sorted(checkout_seeds(degraded), key=lambda row: row["path"])
    prior_rows = previous.get("repositories", [])
    if not isinstance(prior_rows, list):
        degraded.append({"source": "previous-registration", "reason": "invalid prior repositories"})
        prior_rows = []
    rows = {}
    for row in prior_rows:
        if (not isinstance(row, dict) or not isinstance(row.get("path"), str)
                or not isinstance(row.get("worktrees"), list)
                or any(not isinstance(w, dict) or not isinstance(w.get("path"), str)
                       for w in row.get("worktrees", []))):
            degraded.append({"source": "previous-registration", "reason": "invalid prior repository row"})
            continue
        rows[row["path"]] = {**row, "stale": True}
    if not degraded:
        live = {row["path"] for row in seeds}
        rows = {key: row for key, row in rows.items() if key in live}
    for seed in seeds:
        rows.setdefault(seed["path"], {**seed, "worktrees": [], "registered_at": None, "stale": True})
    cursor = previous.get("cursor")
    last = cursor.get("last_attempted") if isinstance(cursor, dict) else None
    if isinstance(last, str):
        seeds = [row for row in seeds if row["path"] > last] + [row for row in seeds if row["path"] <= last]
    attempted = 0

    def write(in_progress: bool = False) -> dict:
        doc = {"schema_version": 1, "kind": "worktree-registration", "measured_at": stamp,
               "repositories": sorted(rows.values(), key=lambda row: row["path"]),
               "cursor": {"last_attempted": last}, "attempted": attempted,
               "partial": in_progress or bool(degraded) or any(row.get("stale") for row in rows.values()),
               "degraded": degraded}
        out_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = out_path.with_name(out_path.name + f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, out_path)
        return doc

    for seed in seeds:
        remaining = end - time.monotonic()
        if remaining <= 0:
            degraded.append({"source": "worktree-registration", "reason": "registration budget exhausted"})
            break
        key = seed["path"]
        last = key
        attempted += 1
        rows[key] = {**rows[key], **seed, "attempted_at": stamp, "stale": True}
        write(in_progress=True)
        remaining = end - time.monotonic()
        if remaining <= 0:
            degraded.append({"source": "worktree-registration", "reason": "registration budget exhausted"})
            break
        registered = worktree_records(key, timeout=min(5.0, remaining))
        if registered is None:
            degraded.append({"source": seed["repository"], "reason": "worktree registration unavailable", "path": key})
            continue
        rows[key] = {**seed, "attempted_at": stamp, "registered_at": stamp, "stale": False,
                     "worktrees": [{"path": row["worktree"], "head": row.get("HEAD"),
                                    "branch": str(row.get("branch", "")).removeprefix("refs/heads/") or None,
                                    "locked": "locked" in row, "prunable": "prunable" in row,
                                    "bare": "bare" in row} for row in registered]}
    doc = write()
    print(json.dumps({key: doc[key] for key in ("kind", "partial", "attempted", "cursor", "degraded")}))
    return 0


def main(argv: list[str]) -> int:
    if "--registrations-only" in argv:
        import argparse
        import math
        parser = argparse.ArgumentParser(description="Cheap Git worktree registration; no cleanup is authorized")
        parser.add_argument("output", type=pathlib.Path)
        parser.add_argument("--registrations-only", action="store_true")
        parser.add_argument("--budget-seconds", type=float, default=20)
        args = parser.parse_args(argv[1:])
        if not math.isfinite(args.budget_seconds) or args.budget_seconds < 0:
            parser.error("budget-seconds must be finite and non-negative")
        return registrations_only(args.output, args.budget_seconds)
    out_path = pathlib.Path(argv[1])
    from scan_machine import working_dirs
    import step_budget
    busy = set(working_dirs().values())
    now = time.time()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    before = _previous(out_path)
    # The least recently measured first, so a pass the deadline cuts short resumes where the last one stopped
    # instead of measuring the same checkouts every tick (OBS-52: stopped with nothing written 11 times in a day).
    degraded: list[dict] = []
    pending = sorted(checkouts(degraded), key=lambda c: (before.get(c["path"]) or {}).get("measured_at") or "")
    rows, carried_paths = [], set()
    carried = 0

    def carry(path: str) -> None:
        nonlocal carried
        old = before.get(path)
        if old and path not in carried_paths:
            rows.append({**old, "stale": True})
            carried_paths.add(path)
            carried += 1

    # Source failure is not evidence that a formerly observed checkout vanished.
    if degraded:
        discovered = {row["path"] for row in pending}
        for path in before:
            if path not in discovered:
                carry(path)
    for i, c in enumerate(pending):
        if step_budget.remaining() < RESERVE_S:
            for rest in pending[i:]:
                carry(rest["path"])
            never_measured = sum(1 for rest in pending[i:] if not before.get(rest["path"]))
            degraded.append({"source": "git-hygiene", "reason": (
                f"the step's time ran out after {i} of {len(pending)} checkouts; {carried} carry their last "
                f"measurement (marked stale) and {never_measured} were never measured; the next pass "
                f"starts with them")})
            break
        default = default_branch(c["path"])
        if not default:
            degraded.append({"source": c["repository"], "reason": unresolved_reason(c["path"])})
            carry(c["path"])
            continue
        wts = worktrees(c["path"], busy, now)
        if wts is None or any(w.get("state") == "unreadable" for w in wts):
            degraded.append({"source": c["repository"], "reason": "worktree classification unavailable"})
            carry(c["path"])
            continue
        held = {w["branch"] for w in wts if w.get("branch")}
        current = git(c["path"], "branch", "--show-current")
        if current is None:
            degraded.append({"source": c["repository"], "reason": "current branch unavailable"})
            carry(c["path"])
            continue
        held.add(current.strip())
        classified = branches(c["path"], default, held, now)
        if classified is None:
            degraded.append({"source": c["repository"], "reason": "branch classification unavailable"})
            carry(c["path"])
            continue
        rows.append({**c, "default_branch": default, "worktrees": wts, "measured_at": stamp,
                     "branches": classified})
    doc = {"schema_version": 1, "measured_at": stamp, "partial": bool(carried or degraded),
           "checkouts": rows, "degraded": degraded,
           "totals": {"checkouts": len(rows),
                      "worktrees": sum(len(r["worktrees"]) for r in rows),
                      "branches": sum(len(r["branches"]) for r in rows)}}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, out_path)
    t = doc["totals"]
    print(f"git hygiene: {t['checkouts']} checkouts, {t['worktrees']} worktrees, {t['branches']} branches"
          + (f"; {len(degraded)} degraded" if degraded else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
