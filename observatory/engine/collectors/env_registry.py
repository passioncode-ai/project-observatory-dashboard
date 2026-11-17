#!/usr/bin/env python3
"""The env inventory as a registry document: names, states, and who shares what.

`collectors/scan_env.py` measures; this decides what of that measurement is a
FACT the estate keeps. The division is the same one `heroku_registry.py` draws,
and it exists because the scan holds one thing the registry may never receive:
the salted fingerprint under every value. That fingerprint answers "are these
two the same secret", and the answer, not the fingerprint, is what belongs in
the registry.

THREE QUESTIONS THIS DOCUMENT EXISTS TO ANSWER.

  what does this project hold      per file, per variable, by class
  what would break if I rotate it  `shared_with`, measured rather than curated
  where can I get one              `available_in`, for a slot still empty here

The second is the one that could not be answered before. `collectors/
credential_owners.json` asks an operator to DECLARE which projects share an
account, and the honest answer was that nobody remembers. Two files holding one
value is that declaration, measured, and the finding rule says so, pointing at
the curation file rather than replacing it: a fingerprint proves two values are
equal, not that they are the same ACCOUNT, and an operator still owns that step.
"""
from __future__ import annotations
from collections import defaultdict

#: Classes a still-empty slot can be filled FROM. Configuration counts here
#: (another project's cache URL is a usable default) even though only secrets
#: are grouped by value below.
FILLABLE_FROM = ("secret", "config")

#: Listed by name up to this many in a summary, as everywhere else here.
LISTED = 8


def sites(files: list[dict]) -> dict[str, list[dict]]:
    """fingerprint -> every place that exact value sits."""
    out: dict[str, list[dict]] = defaultdict(list)
    for f in files:
        for v in f["variables"]:
            fp = v.get("fingerprint")
            if fp:
                out[fp].append({"project": f["project"], "path": f["path"],
                                "name": v["name"], "class": v["class"],
                                "kind": f["kind"]})
    return out


def live_names(files: list[dict]) -> dict[str, set[str]]:
    """variable name -> projects where it holds a real value.

    REAL EXCLUDES A TEMPLATE, and that is the whole point of the distinction: a
    `.env.example` declaring a payment secret key is a project ASKING for one, and
    offering it back as a place to get one would be a circle.
    """
    out: dict[str, set[str]] = defaultdict(set)
    for f in files:
        if f["kind"] != "env":
            continue
        for v in f["variables"]:
            if v["class"] in FILLABLE_FROM:
                out[v["name"]].add(f["project"])
    return out


def enrich(files: list[dict]) -> list[dict]:
    """The registry's copy of each file: no fingerprint leaves this function."""
    by_fp = sites(files)
    live = live_names(files)
    out = []
    for f in files:
        variables = []
        for v in f["variables"]:
            row = {"name": v["name"], "class": v["class"]}
            fp = v.get("fingerprint")
            if fp:
                group = by_fp[fp]
                others = sorted({s["project"] for s in group} - {f["project"]})
                if others:
                    row["shared_with"] = others
                if len(group) > 1:
                    row["copies"] = len(group)
            elif v["class"] in ("empty", "placeholder") and f["kind"] == "env":
                # THE REUSE AFFORDANCE, and only for a live file. An unfilled
                # slot whose name holds a real value somewhere else is the
                # question "where do I get one" already answered — but a
                # `.env.example` is a DECLARATION, not a slot, and offering it
                # somewhere to fill from would put the affordance on 168 files
                # that never run.
                where = sorted(live.get(v["name"], set()) - {f["project"]})
                if where:
                    row["available_in"] = where[:LISTED]
            variables.append(row)
        counts = {c: sum(1 for v in f["variables"] if v["class"] == c)
                  for c in ("secret", "config", "placeholder", "empty")}
        out.append({
            "id": "env:" + f["path"],
            "path": f["path"],
            "project": f["project"],
            "kind": f["kind"],
            "git": f["git"],
            "mode": f["mode"],
            "modified_on": f["modified_on"],
            "unparsed_lines": f["unparsed_lines"],
            "counts": {k: v for k, v in counts.items() if v},
            "variables": variables,
        })
    return out


def shared_groups(files: list[dict]) -> list[dict]:
    """One row per value that more than one PROJECT holds.

    Within one project a repeated value is ordinary: a backend and a frontend
    reading one database. Across projects it is a rotation blast radius, and that
    is the only version worth a row.
    """
    groups = []
    for fp, group in sites(files).items():
        projects = sorted({s["project"] for s in group})
        if len(projects) < 2:
            continue
        names = sorted({s["name"] for s in group})
        groups.append({
            "projects": projects,
            "names": names,
            "sites": sorted(f"{s['path']}:{s['name']}" for s in group),
            "class": "secret" if any(s["class"] == "secret" for s in group) else "config",
            "in_templates": sum(1 for s in group if s["kind"] == "template"),
        })
    # Widest blast radius first, then the secrets, then stably by name.
    groups.sort(key=lambda g: (-len(g["projects"]), g["class"] != "secret",
                               g["names"][0] if g["names"] else ""))
    return groups


def document(scan: dict, obs_date: str) -> dict:
    files = enrich(scan.get("files") or [])
    groups = shared_groups(scan.get("files") or [])
    real = [f for f in files if f["kind"] == "env"]
    n_vars = sum(len(f["variables"]) for f in files)
    return {
        "schema_version": 1,
        "updated_on": obs_date,
        "note": ("Every `.env` under the estate, measured by collectors/scan_env.py "
                 "as NAMES and never as values. `class` is decided from the value "
                 "and the value is then dropped: secret, config, placeholder or "
                 "empty. `shared_with` is measured, not declared: two variables "
                 "carry it when a salted fingerprint says their values are equal, "
                 "and the salt is machine-local and gitignored so the fingerprint "
                 "itself never reaches this file. `available_in` answers the "
                 "opposite question for a slot still empty here. A `template` is a "
                 "`.env.example` and holds no live value by construction, so it is "
                 "listed apart rather than counted as an inventory."),
        "source_refs": ["SRC-0015"],
        "scanned_on": (scan.get("scanned_at") or "")[:10],
        "root": scan.get("root"),
        "totals": {
            "files": len(files),
            "env_files": len(real),
            "templates": len(files) - len(real),
            "projects": len({f["project"] for f in files if f["project"]}),
            "variables": n_vars,
            "secrets": sum(1 for f in files for v in f["variables"]
                           if v["class"] == "secret"),
            "unfilled": sum(1 for f in real for v in f["variables"]
                            if v["class"] in ("empty", "placeholder")),
            "shared_across_projects": len(groups),
            "tracked_in_git": sum(1 for f in real if f["git"] == "tracked"),
            "unignored": sum(1 for f in real if f["git"] == "loose"),
            "world_readable": sum(1 for f in real if int(f["mode"][-1]) & 0o4
                                  or int(f["mode"][-2]) & 0o4),
        },
        "files": files,
        "shared": groups,
        "degraded": scan.get("degraded") or [],
    }
