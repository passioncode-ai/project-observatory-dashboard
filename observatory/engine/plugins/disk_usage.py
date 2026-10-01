#!/usr/bin/env python3
"""How many bytes each project's working tree costs, as a metric over time.

The reference plugin. It exists to be copied, so it does the smallest honest
thing: walk each project's local folders, skip what is machine-generated, print
one JSON row per project on stdout, and write nothing anywhere.

`.git` is excluded on purpose. A repository's history is not what the working
tree costs, it does not shrink when files are deleted, and a project that
rewrote its history would look as though it had freed space it never used.
`node_modules`, `.venv` and their kin are excluded for the opposite reason: they
are reinstallable, so counting them measures the package manager rather than the
project. What they cost is reported separately, as `disk.reclaimable_bytes`.
"""
from __future__ import annotations
import json, os, pathlib, sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths                                                                    

#: Machine-generated and reinstallable. Counting them measures a tool, not a project.
SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", ".nuxt",
        "target", "build", "dist", ".gradle", "Pods", ".terraform", ".mypy_cache",
        ".pytest_cache", ".tox", ".DS_Store"}


def tree_bytes(root: pathlib.Path) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
        dirnames[:] = [d for d in dirnames if d not in SKIP]
        for name in filenames:
            if name in SKIP:
                continue
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue                                                       
            if not os.path.islink(os.path.join(dirpath, name)):
                total += st.st_size
    return total


def walk_all(folders, seen: set[str]) -> int:
    """Bytes across several folders, counting each place on disk once."""
    total = 0
    for folder in folders:
        path = paths.DATA / folder
        real = str(path.resolve()) if path.exists() else ""
        # A symlinked folder and its target are one place on disk. Counting both
        # would double a project's footprint for a convenience link.
        if not real or real in seen or not path.is_dir():
            continue
        seen.add(real)
        total += tree_bytes(path)
    return total


def worktrees_of(project_id: str, relations: list, repos: dict) -> list[str]:
    """The EXTRA checkouts of a project's repositories.

    `collectors/merge.py` already records them — a worktree never displaces a
    real checkout, and the ones it demotes land in `local.extra_clones`. Reading
    only `projects[].local_folders`, the PRIMARY checkout, would miss them, and
    a project with many worktrees can hold far more on disk than its primary
    checkout — so the footprint would under-report exactly where it matters, and
    `host.disk_low`'s "largest working trees" would name everything except the
    largest.
    """
    mine = {r["to"] for r in relations
            if r["type"] == "implemented_by" and r["from"] == project_id}
    out: list[str] = []
    for rid in sorted(mine):
        local = (repos.get(rid) or {}).get("local") or {}
        out += list(local.get("extra_clones") or [])
    return out


def subtree_bytes(root: str) -> int:
    """Every byte under `root`, pruning nothing. Used only for a SKIP subtree."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root, onerror=lambda _e: None):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass                                                           
    return total


def reclaimable_bytes(root: pathlib.Path) -> int:
    """Bytes inside the directories `tree_bytes` prunes — what a reinstall restores.

    **The question the footprint metric cannot answer.** `disk.bytes` excludes
    `node_modules`, `.venv`, `build` and their kin, correctly: counting them
    measures the package manager rather than the project. But
    `host.disk_low` — the critical finding that says the volume is nearly full —
    reads that metric to name who is responsible, and the footprint can account
    for a small fraction of what the project folders occupy while most of the
    rest is reclaimable. The finding would be asking "why is my disk full" while
    reading the answer to "what does this project cost".

    So this is its own metric, with its own meaning: generated dependency/build data, excluding Git history. This is an
    inventory estimate, not permission to remove a directory or a physical-space
    guarantee (hardlinks, clones and active work need separate checks).

    It descends into each pruned directory rather than counting only the files
    directly inside it: `node_modules` is deeply nested, and a shallow count
    measures the instrument instead of the disk.
    """
    total = 0
    for dirpath, dirnames, _filenames in os.walk(root, onerror=lambda _e: None):
        for d in [d for d in dirnames if d in SKIP and d != ".git"]:
            total += subtree_bytes(os.path.join(dirpath, d))
        dirnames[:] = [d for d in dirnames if d not in SKIP]
    return total


def main() -> int:
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")                     
    reg = lambda name, key: json.loads(                                          
        (paths.REGISTRY / name).read_text(encoding="utf-8"))[key]
    projects = reg("projects.json", "projects")
    relations = reg("relations.json", "relations")
    repos = {r["id"]: r for r in reg("repositories.json", "repositories")}
    for p in projects:
        folders = p.get("local_folders") or []
        if not folders:
            continue
        # ONE `seen` ACROSS BOTH, so a folder that is both a declared home and a
        # recorded extra clone is counted once.
        seen: set[str] = set()
        total = walk_all(folders, seen)
        extra = walk_all(worktrees_of(p["id"], relations, repos), seen)
        if total:
            # `disk.bytes` KEEPS ITS MEANING — the primary checkouts. Folding
            # the worktrees into it would silently break the series: yesterday's
            # value measured one thing and today's another, and a trend computed
            # across that boundary is a fiction. The extra checkouts are their
            # own metric, so both numbers stay comparable with their own past.
            print(json.dumps({"project_id": p["id"], "metric": "disk.bytes",
                              "at": at, "value": float(total)}))
        if extra:
            print(json.dumps({"project_id": p["id"], "metric": "disk.worktree_bytes",
                              "at": at, "value": float(extra)}))
        # RECLAIMABLE, over every checkout: the question "what can I free" does
        # not care which copy the bytes sit in. Measured across the whole estate
        # in 34s, well inside the runner's 300s ceiling.
        recl = 0
        reclaim_seen = set()
        for folder in list(folders) + worktrees_of(p["id"], relations, repos):
            path = paths.DATA / folder
            if path.is_dir() and str(path.resolve()) not in reclaim_seen:
                reclaim_seen.add(str(path.resolve()))
                recl += reclaimable_bytes(path)
        if recl:
            print(json.dumps({"project_id": p["id"],
                              "metric": "disk.reclaimable_bytes",
                              "at": at, "value": float(recl)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
