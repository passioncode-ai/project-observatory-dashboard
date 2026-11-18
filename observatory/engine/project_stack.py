#!/usr/bin/env python3
"""A project's stack, read from its dependency inventory, and the packages its panel lists (OBS-603, CP-06).

The stack used to be a guess from marker files at the top of each folder (`collectors/scan_filesystem.MARKERS`):
a `package.json` said `node`, a `next.config.js` said `nextjs`. The dependency scan (`collectors/scan_dependencies.py`)
already reads every lockfile a project resolves, at any depth the walk allows, so the part of the stack a package
manager knows follows from it:

    an ecosystem with a lockfile read      npm node, PyPI python, Go go, crates.io rust, RubyGems ruby, Packagist php
    a DIRECT dependency that is a framework next nextjs, astro astro

A marker stands in only where no readable lockfile of its ecosystem was found, and the answer says so in
`degraded` (`marker-fallback`, or `markers-only` when the inventory read nothing for the project). Markers that
describe no package (a Dockerfile, an Xcode project, a skill, a deploy target) are kept as they are and labelled
`from: marker`: no lockfile could confirm or deny them, so nothing is guessed about them.

`panel` is what the project panel draws: each package with its version, ecosystem, whether it is direct, transitive
or local, and how many advisories the vulnerability watch matched (`None` when the watch has not run, never 0).
"""
from __future__ import annotations

#: Ecosystem (as the dependency scan spells it) -> the stack kind the marker files used for it.
ECOSYSTEM_KIND = {"npm": "node", "PyPI": "python", "Go": "go", "crates.io": "rust", "RubyGems": "ruby",
                  "Packagist": "php"}
#: (ecosystem, direct dependency) -> framework kind; the vocabulary the markers already used.
FRAMEWORKS = {("npm", "next"): "nextjs", ("npm", "astro"): "astro"}
#: Kinds a lockfile can show, and the ecosystem whose lockfile would.
INVENTORY_KINDS = {**{kind: eco for eco, kind in ECOSYSTEM_KIND.items()},
                   **{kind: eco for (eco, _name), kind in FRAMEWORKS.items()}}
#: Rows the project panel shows; the rest are counted, and `observatory_dependencies` pages them all.
PANEL_ROWS = 200


def _readable(inventory: dict | None) -> list[dict]:
    """The lockfiles of a project's inventory that were read, never one that failed to parse."""
    return [r for r in (inventory or {}).get("read_from") or [] if isinstance(r, dict) and not r.get("error")]


def derive(markers, inventory: dict | None) -> dict:
    """{stack, source, evidence, degraded} for one project.

    `markers` are the kinds the folder scan found (`project["kinds"]`); `inventory` is the project's row of
    `registry/dependencies.json` (`dependencies_registry.project_row`), or None when the scan holds none.
    `source` is `dependencies` (every kind a lockfile can show was shown by one), `dependencies+markers` (some were
    marker fallbacks), `markers` (no lockfile was read) or `none` (no kind at all)."""
    markers = sorted({str(m) for m in markers or [] if m})
    read = _readable(inventory)
    ecosystems = {r.get("ecosystem") for r in read if r.get("ecosystem")}
    evidence: dict[str, dict] = {}
    if read:
        for eco in sorted(ecosystems):
            kind = ECOSYSTEM_KIND.get(eco)
            if kind:
                evidence[kind] = {"kind": kind, "from": "dependencies", "ecosystem": eco}
        for row in (inventory or {}).get("packages") or []:
            kind = FRAMEWORKS.get((row.get("ecosystem"), row.get("name")))
            if kind and row.get("direct") is True and kind not in evidence:
                evidence[kind] = {"kind": kind, "from": "dependencies", "ecosystem": row["ecosystem"],
                                  "package": row["name"]}
    fallback = []
    for kind in markers:
        if kind in evidence:
            continue
        evidence[kind] = {"kind": kind, "from": "marker"}
        if read and kind in INVENTORY_KINDS:
            fallback.append(kind)
    stack = sorted(evidence)
    degraded = []
    if not read and stack:
        source = "markers"
        degraded.append({"source": "stack", "code": "markers-only",
                         "reason": "no lockfile or manifest of this project was read, so its stack ("
                                   + ", ".join(stack) + ") is read from marker files"})
    elif fallback:
        source = "dependencies+markers"
        degraded.append({"source": "stack", "code": "marker-fallback",
                         "reason": "no lockfile shows " + ", ".join(fallback)
                                   + "; read from marker files beside the lockfiles that were read"})
    else:
        source = "dependencies" if read else "none"
    return {"stack": stack, "source": source, "evidence": [evidence[k] for k in stack], "degraded": degraded}


def registry_fields(derived: dict) -> dict:
    """The fields of a `registry/projects.json` row: `stack` as before, its `stack_source` and `stack_evidence`, and
    `stack_degraded` only when there is something to say (an absent field means nothing was guessed)."""
    out = {"stack": list(derived["stack"]), "stack_source": derived["source"],
           "stack_evidence": [dict(e) for e in derived["evidence"]]}
    if derived["degraded"]:
        out["stack_degraded"] = [dict(d) for d in derived["degraded"]]
    return out


def detail_fields(row: dict | None) -> dict:
    """`stackSource` and `stackEvidence` for `project.detail`, from a registry row; nothing for a row written before
    the stack had a source (absent, never a guessed value)."""
    if not isinstance(row, dict) or not row.get("stack_source"):
        return {}
    return {"stackSource": row["stack_source"],
            "stackEvidence": [dict(e) for e in row.get("stack_evidence") or [] if isinstance(e, dict)]}


def survey_degraded(rows, one_project: bool) -> list[dict]:
    """The survey's `degraded` entries for stacks read from markers: each one's reason when the scope is one project,
    one count for a wider scope (a list of every such project would bury the rest). `{source, reason}` only: the
    published degraded item is closed."""
    guessed = [r for r in rows or [] if isinstance(r, dict) and r.get("stack_degraded")]
    if not guessed:
        return []
    if one_project:
        return [{"source": "stack", "reason": str(d.get("reason") or "")}
                for r in guessed for d in r["stack_degraded"] if isinstance(d, dict)]
    return [{"source": "stack", "reason": f"{len(guessed)} project(s) have stack kinds read from marker files "
                                          f"because no lockfile of that ecosystem was read; `stackSource` on "
                                          f"each project says which"}]


def inventory_by_project(scan: dict | None) -> dict[str, dict]:
    """{project id: its registry row} from the raw scan document (`store/raw/dependencies.json`)."""
    if not isinstance(scan, dict):
        return {}
    import dependencies_registry
    return {p["project"]: dependencies_registry.project_row(p)
            for p in scan.get("projects") or [] if isinstance(p, dict) and p.get("project")}


def panel(registry_doc: dict | None, vulns_doc: dict | None, rows: int = PANEL_ROWS) -> dict:
    """{projects: {id: {rows, total, shown, direct, transitive, local, unknown, vulnerable}}, scannedOn, checkedOn,
    degraded}. Rows: the vulnerable first (most advisories first), then direct, then by ecosystem and name."""
    degraded: list[dict] = []
    if not registry_doc:
        degraded.append({"source": "dependencies", "code": "not-built",
                         "reason": "no dependency inventory has been built yet"})
    else:
        degraded += [d for d in registry_doc.get("degraded") or [] if isinstance(d, dict)]
    watched = isinstance(vulns_doc, dict) and vulns_doc.get("enabled") is not False
    if not isinstance(vulns_doc, dict):
        degraded.append({"source": "osv", "code": "not-run",
                         "reason": "the vulnerability watch has not run; advisory counts are unknown"})
    elif not watched:
        degraded.append({"source": "osv", "code": "off",
                         "reason": "the vulnerability watch is switched off; advisory counts are unknown"})
    else:
        degraded += [d for d in vulns_doc.get("degraded") or [] if isinstance(d, dict)]
    matches = {(m.get("ecosystem"), m.get("name"), str(m.get("version"))): len(m.get("advisories") or [])
               for m in (vulns_doc or {}).get("matches") or [] if isinstance(m, dict)}
    out: dict[str, dict] = {}
    for p in (registry_doc or {}).get("projects") or []:
        if not isinstance(p, dict) or not p.get("project"):
            continue
        found = []
        for r in p.get("packages") or []:
            local = bool(r.get("local"))
            count = (0 if local else matches.get((r.get("ecosystem"), r.get("name"), str(r.get("version"))), 0)) \
                if watched else None
            found.append({"name": r.get("name"), "version": r.get("version"), "ecosystem": r.get("ecosystem"),
                          "direct": r.get("direct"), "local": local, "advisories": count})
        found.sort(key=lambda r: (-(r["advisories"] or 0), r["direct"] is not True, str(r["ecosystem"]),
                                  str(r["name"]), str(r["version"])))
        nonlocal_rows = [r for r in found if not r["local"]]
        out[p["project"]] = {
            "rows": found[:rows], "total": len(found), "shown": min(len(found), rows),
            "direct": sum(1 for r in nonlocal_rows if r["direct"] is True),
            "transitive": sum(1 for r in nonlocal_rows if r["direct"] is False),
            "unknown": sum(1 for r in nonlocal_rows if r["direct"] is None),
            "local": len(found) - len(nonlocal_rows),
            "vulnerable": sum(1 for r in found if r["advisories"]),
            "ecosystems": sorted({r["ecosystem"] for r in found if r["ecosystem"]})}
    return {"projects": out, "scannedOn": (registry_doc or {}).get("updated_on"),
            "checkedOn": (vulns_doc or {}).get("checked_on") if isinstance(vulns_doc, dict) else None,
            "degraded": degraded}
