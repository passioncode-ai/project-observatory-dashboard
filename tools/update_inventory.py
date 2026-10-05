#!/usr/bin/env python3
"""Keep observatory/engine/SOURCE-INVENTORY.json in step with the engine tree.

The public repository is the engine's upstream: a change made here updates the
inventory in the same commit. ``--check`` (CI) exits 1 when the inventory
disagrees with the tree; without it the inventory is rewritten in place.

Each row keeps ``source_sha256`` as the provenance of the original sanitized
extraction. A file changed or added upstream gets ``upstream_modified: true``;
its ``export_sha256`` and ``bytes`` always describe the bytes that ship.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "observatory" / "engine"
INVENTORY = ENGINE / "SOURCE-INVENTORY.json"


def _safe_git():
    """The engine's one git door, loaded by path from this checkout (not from
    whatever copy the interpreter has installed), so listing the tree runs none
    of the programs the maintainer's git configuration names."""
    spec = importlib.util.spec_from_file_location("observatory_inventory_safe_git",
                                                  ENGINE / "safe_git.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Unmerged(Exception):
    """The index still holds a conflict; an inventory of it would describe no tree."""


def tracked_engine_files(root: Path = ROOT) -> list[str]:
    """Engine files Git tracks, relative to the engine directory, each once.

    `git ls-files` lists an unmerged path once per stage, which once wrote the same
    `path` two or three times (issue #97); while a conflict is open this refuses."""
    git = _safe_git()
    unmerged = git.run(["ls-files", "-u", "-z", "--", "observatory/engine"], repo=root,
                       timeout=None, check=True, text=False).stdout.decode("utf-8")
    if unmerged.strip("\0"):
        paths = sorted({row.split("\t", 1)[-1] for row in unmerged.split("\0") if row})
        raise Unmerged(f"{len(paths)} unmerged path(s), e.g. {paths[0]}: resolve and "
                       f"`git add` them, then regenerate")
    out = git.run(["ls-files", "-z", "--", "observatory/engine"], repo=root, timeout=None,
                  check=True, text=False).stdout.decode("utf-8")
    prefix = "observatory/engine/"
    return sorted({p[len(prefix):] for p in out.split("\0")
                   if p.startswith(prefix) and p != prefix + "SOURCE-INVENTORY.json"})


def refreshed(doc: dict, engine: Path, files: list[str]) -> tuple[dict, list[str]]:
    """Return (new inventory, human-readable differences)."""
    rows = {r["path"]: r for r in doc["files"]}
    changes: list[str] = []
    seen: set[str] = set()
    for r in doc["files"]:
        if r["path"] in seen:
            changes.append(f"duplicate: {r['path']}")
        seen.add(r["path"])
    new_rows = []
    for path in files:
        data = (engine / path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        row = dict(rows.get(path) or {"path": path, "source_sha256": None})
        if path not in rows:
            changes.append(f"added: {path}")
            row["upstream_modified"] = True
        elif row.get("export_sha256") != digest or row.get("bytes") != len(data):
            changes.append(f"changed: {path}")
            row["upstream_modified"] = True
        row["export_sha256"] = digest
        row["bytes"] = len(data)
        new_rows.append(row)
    for path in sorted(set(rows) - set(files)):
        changes.append(f"removed: {path}")
    out = dict(doc)
    out["files"] = new_rows
    out["file_count"] = len(new_rows)
    return out, changes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="exit 1 on drift, write nothing")
    args = ap.parse_args(argv)
    try:
        doc = json.loads(INVENTORY.read_text(encoding="utf-8"))
        files = tracked_engine_files()
    except (OSError, ValueError, subprocess.CalledProcessError, Unmerged) as exc:
        print(json.dumps({"gate": "source-inventory", "passed": False, "error": str(exc)}))
        return 2
    new, changes = refreshed(doc, ENGINE, files)
    if args.check:
        print(json.dumps({"gate": "source-inventory", "passed": not changes,
                          "differences": changes}, indent=2))
        return 1 if changes else 0
    if changes:
        INVENTORY.write_text(json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"updated": bool(changes), "differences": changes}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
