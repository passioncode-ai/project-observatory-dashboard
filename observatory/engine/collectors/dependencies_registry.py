#!/usr/bin/env python3
"""The dependency inventory as a registry document (OBS-553, contract `registry/dependencies.json`).

`collectors/scan_dependencies.py` measures, with the sizes and times it caches by; this keeps
what is a fact about each project: which packages, at which versions, in which ecosystems, direct
or not, local or not, read from which lockfiles, and which of those could not be read. A package
resolved by two lockfiles of one project is one row, carrying the first lockfile that names it.
"""
from __future__ import annotations

SCHEMA_VERSION = 1


def project_row(p: dict) -> dict:
    seen: dict[tuple, dict] = {}
    read_from, degraded = [], list(p.get("degraded") or [])
    for f in p.get("files") or []:
        where = f"{f.get('folder')}/{f.get('path')}"
        read_from.append({"file": where, "folder": f.get("folder"), "path": f.get("path"), "format": f.get("format"), "ecosystem": f.get("ecosystem"),
                          "packages": len(f.get("packages") or []), **({"error": f["error"]} if f.get("error") else {})})
        for r in f.get("packages") or []:
            key = (r.get("ecosystem"), r.get("name"), r.get("version"), r.get("local"))
            row = seen.get(key)
            if row is None:
                row = {"name": r.get("name"), "version": r.get("version"), "ecosystem": r.get("ecosystem"),
                       "direct": r.get("direct"), "local": bool(r.get("local")), "file": where}
                if r.get("local"):
                    row["source"] = r.get("source")
                if r.get("dev"):
                    row["dev"] = True
                if r.get("pinned") is False:
                    row["pinned"] = False
                seen[key] = row
            elif r.get("direct") and not row.get("direct"):
                row["direct"] = True
    packages = sorted(seen.values(), key=lambda r: (r["ecosystem"] or "", r["name"] or "", r["version"] or ""))
    return {"project": p.get("project"),
            "ecosystems": sorted({r["ecosystem"] for r in packages if r["ecosystem"]}
                                 | {x["ecosystem"] for x in read_from if x.get("ecosystem")}),
            "packages": packages, "read_from": read_from, "degraded": degraded}


def document(scan: dict, observed_on: str) -> dict:
    projects = sorted((project_row(p) for p in scan.get("projects") or [] if p.get("project")),
                      key=lambda r: r["project"])
    rows = [r for p in projects for r in p["packages"]]
    return {
        "schema_version": SCHEMA_VERSION, "updated_on": observed_on,
        "note": ("What each project's lockfiles resolve (collectors/scan_dependencies.py). A lockfile "
                 "that could not be parsed is named in its project's `degraded`: its dependencies are "
                 "unknown, not absent. `local` packages (a path, a workspace member, a link, git, a "
                 "private registry) are never sent to a vulnerability service."),
        # No scan time here: the registry is committed when its CONTENT moves, and a time that moves
        # every tick would make it differ from itself every tick (`updated_on` is the measured day).
        "complete": scan.get("complete", True),
        "projects": projects,
        "totals": {"projects": len(projects), "packages": len(rows),
                   "local": sum(1 for r in rows if r["local"]),
                   "unreadable_files": sum(1 for p in projects for x in p["read_from"] if x.get("error"))},
        "degraded": list(scan.get("degraded") or []),
    }
