#!/usr/bin/env python3
"""How many local branches each project's checkouts hold.

**Written from `plugins/README.md` alone, as an experiment, and the first draft
inlined two canonical paths because the document never said otherwise.** It
resolved the estate root as `Path.home() / "DATA"` and the registry as the
relative string `"registry/projects.json"` — both of which the repository has
resolvers for, both of which `tools/check_paths.py` missed, and neither of which
the README mentions. The document and the gate were fixed in the same change
.
"""
from __future__ import annotations
import json
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import paths                                                                      
import safe_git                                                                   


def branches(folder: pathlib.Path) -> int | None:
    """Local branches in one checkout, or None when git will not answer."""
    try:
        p = safe_git.run(["branch", "--list"], repo=folder, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    return len([l for l in p.stdout.splitlines() if l.strip()])


def main() -> int:
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    projects = json.loads(
        (paths.REGISTRY / "projects.json").read_text(encoding="utf-8"))["projects"]
    for pr in projects:
        total, seen = 0, False
        for folder in pr.get("local_folders") or []:
            n = branches(paths.DATA / folder)
            if n is not None:
                total += n
                seen = True
        # ABSENT, NOT ZERO: a project with no readable checkout has no branch
        # count, and printing 0 would put it beside a project that genuinely has
        # none.
        if seen:
            print(json.dumps({"project_id": pr["id"], "metric": "git.branches",
                              "at": at, "value": float(total)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
