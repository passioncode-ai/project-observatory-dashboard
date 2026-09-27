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
import paths  # noqa: E402


def git(repo: str, *args: str, timeout: int = 60) -> str | None:
    try:
        p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def checkouts() -> list[dict]:
    """Every registered local checkout, by real path, once."""
    try:
        repos = json.loads((paths.REGISTRY / "repositories.json").read_text(encoding="utf-8"))["repositories"]
    except (OSError, ValueError, KeyError):
        return []
    seen, out = set(), []
    for r in repos:
        path = (r.get("local") or {}).get("path")
        if not path or not os.path.isdir(os.path.join(path, ".git")):
            continue
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        out.append({"repository": r["id"], "path": real})
    return out


def default_branch(repo: str) -> str | None:
    head = (git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD") or "").strip()
    if head:
        return head.split("/", 1)[1]
    for name in ("main", "master"):
        if git(repo, "rev-parse", "--verify", "-q", "refs/heads/" + name) is not None:
            return name
    return None


def worktrees(repo: str, busy: set[str], now: float) -> list[dict]:
    rows, cur = [], {}
    for line in (git(repo, "worktree", "list", "--porcelain") or "").splitlines() + [""]:
        if not line:
            if cur:
                rows.append(cur)
                cur = {}
            continue
        key, _, value = line.partition(" ")
        cur[key] = value or True
    out = []
    for w in rows[1:]:  # the first entry is the main checkout
        path = w["worktree"]
        entry = {"path": path, "branch": str(w.get("branch", "")).replace("refs/heads/", "") or None,
                 "head": w.get("HEAD"), "locked": "locked" in w}
        if not os.path.isdir(path):
            entry["state"] = "missing"
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


def branches(repo: str, default: str, in_worktree: set[str], now: float) -> list[dict]:
    fmt = "%(refname:short)\t%(committerdate:unix)\t%(ahead-behind:" + default + ")\t%(upstream:short)\t%(upstream:track)"
    out = []
    for line in (git(repo, "for-each-ref", "--format=" + fmt, "refs/heads") or "").splitlines():
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


def main(argv: list[str]) -> int:
    out_path = pathlib.Path(argv[1])
    from scan_machine import working_dirs
    busy = set(working_dirs().values())
    now = time.time()
    rows, degraded = [], []
    for c in checkouts():
        default = default_branch(c["path"])
        if not default:
            degraded.append({"source": c["repository"], "reason": "no default branch could be resolved"})
            continue
        wts = worktrees(c["path"], busy, now)
        held = {w["branch"] for w in wts if w.get("branch")}
        current = (git(c["path"], "branch", "--show-current") or "").strip()
        held.add(current)
        rows.append({**c, "default_branch": default, "worktrees": wts,
                     "branches": branches(c["path"], default, held, now)})
    doc = {"schema_version": 1, "measured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
