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

**It finishes inside its time, or keeps what it measured (2026-10-10).** On a ~100 GB
estate under heavy load it reached the runner's 300 s limit and lost everything, so the
period had no sample and the plugin ran again on every tick. Now each folder is walked
once for both numbers, the projects measured longest ago go first, and it stops between
projects at the deadline the runner hands it (`OBSERVATORY_STEP_DEADLINE`); the projects
it did not reach keep their previous measurement and go first next time.
"""
from __future__ import annotations
import json, os, pathlib, sqlite3, sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths                                                                    
import step_budget                                                              

#: Seconds kept before the deadline: a project's walk is not interrupted midway.
MARGIN_SECONDS = 20

#: Machine-generated and reinstallable. Counting them measures a tool, not a project.
SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", ".nuxt",
        "target", "build", "dist", ".gradle", "Pods", ".terraform", ".mypy_cache",
        ".pytest_cache", ".tox", ".DS_Store"}


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


def measure(root) -> tuple[int, int]:
    """(working-tree bytes, reclaimable bytes) for one folder, in ONE walk: the footprint
    prunes the reinstallable directories, and those pruned directories are the reclaimable
    part — the two numbers came from two full walks before.

    Reclaimable is its own metric because `host.disk_low` asks "why is my disk full" and
    the footprint, which rightly excludes `node_modules`, `.venv` and `build`, can account
    for a small fraction of what the folders occupy. It descends into each pruned
    directory: `node_modules` is deeply nested, and a shallow count measures the
    instrument instead of the disk."""
    footprint = reclaimable = 0
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
        keep = []
        for d in dirnames:
            if d in SKIP:
                reclaimable += subtree_bytes(os.path.join(dirpath, d))
            else:
                keep.append(d)
        dirnames[:] = keep
        for name in filenames:
            if name in SKIP:
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not os.path.islink(full):
                footprint += st.st_size
    return footprint, reclaimable


def last_measured() -> dict[str, str]:
    """When each project's footprint was last measured, read-only from the store; empty
    when the store cannot say, which only makes the order the registry's own."""
    try:
        conn = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
        try:
            return dict(conn.execute("SELECT project_id, MAX(at) FROM metrics "
                                     "WHERE metric = 'disk.bytes' GROUP BY project_id").fetchall())
        finally:
            conn.close()
    except sqlite3.Error:
        return {}


def oldest_first(projects: list, last: dict[str, str]) -> list:
    """Never measured first, then the oldest measurement: a run that stops early has spent
    its time where the numbers were most out of date."""
    return sorted(projects, key=lambda p: (p["id"] in last, last.get(p["id"], "")))


def folder_numbers(folders, seen: set[str], cache: dict) -> tuple[int, int]:
    """(footprint, reclaimable) across several folders, each place on disk counted once."""
    total = recl = 0
    for folder in folders:
        path = paths.DATA / folder
        real = str(path.resolve()) if path.exists() else ""
        # A symlinked folder and its target are one place on disk. Counting both
        # would double a project's footprint for a convenience link.
        if not real or real in seen or not path.is_dir():
            continue
        seen.add(real)
        if real not in cache:
            cache[real] = measure(path)
        total += cache[real][0]
        recl += cache[real][1]
    return total, recl


def main() -> int:
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")                     
    reg = lambda name, key: json.loads(                                          
        (paths.REGISTRY / name).read_text(encoding="utf-8"))[key]
    projects = reg("projects.json", "projects")
    relations = reg("relations.json", "relations")
    repos = {r["id"]: r for r in reg("repositories.json", "repositories")}
    measured, wanted, cache = 0, 0, {}
    for p in oldest_first([p for p in projects if p.get("local_folders")], last_measured()):
        wanted += 1
        if step_budget.remaining() < MARGIN_SECONDS:
            continue
        measured += 1
        folders = p.get("local_folders") or []
        # ONE `seen` ACROSS BOTH, so a folder that is both a declared home and a
        # recorded extra clone is counted once.
        seen: set[str] = set()
        total, recl_home = folder_numbers(folders, seen, cache)
        extra, recl_extra = folder_numbers(worktrees_of(p["id"], relations, repos), seen, cache)
        if total:
            # `disk.bytes` KEEPS ITS MEANING — the primary checkouts. Folding
            # the worktrees into it would silently break the series: yesterday's
            # value measured one thing and today's another, and a trend computed
            # across that boundary is a fiction. The extra checkouts are their
            # own metric, so both numbers stay comparable with their own past.
            print(json.dumps({"project_id": p["id"], "metric": "disk.bytes",
                              "at": at, "value": float(total)}), flush=True)
        if extra:
            print(json.dumps({"project_id": p["id"], "metric": "disk.worktree_bytes",
                              "at": at, "value": float(extra)}), flush=True)
        # RECLAIMABLE, over every checkout: the question "what can I free" does
        # not care which copy the bytes sit in.
        if recl_home + recl_extra:
            print(json.dumps({"project_id": p["id"],
                              "metric": "disk.reclaimable_bytes",
                              "at": at, "value": float(recl_home + recl_extra)}), flush=True)
    if measured < wanted:
        print(f"partial: measured {measured} of {wanted} project(s) before the deadline; the "
              f"rest keep their previous measurement and go first next time", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
