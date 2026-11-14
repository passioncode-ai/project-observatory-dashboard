#!/usr/bin/env python3
"""Which dependencies have a published vulnerability, from public data, once a day (OBS-553, CP-07).

Reads the inventory `collectors/scan_dependencies.py` measured (`registry/dependencies.json`), asks
OSV's batch API which advisories match each public package at its version, reads each advisory's
record (cached under `store/state/osv-cache/` and fetched again only when OSV lists a newer
`modified`), and joins the CVE aliases to CISA's known-exploited catalogue, fetched once a day.
What it writes is `store/raw/vulnerabilities.json`; `tools/dependency_findings.py` turns it into
`dep.vulnerable` findings and `observatory_dependencies` serves it to agents.

**Private names never leave the machine (decision D2).** Only rows with `local: false` and a pinned
version are queried: a path, a workspace member, a link, a git URL, a private registry or a name the
person listed in `config/private_packages.json` is never in a request. A declared range is not a
version anything runs, so it is not queried either.

**Daily, and honest when it cannot ask.** A run is due when the last complete one was on another
UTC day or the inventory changed since (`vuln-watch.json` in the state folder). Switched off
(`integrations.vulnerabilities: false`) or offline (`OBSERVATORY_OFFLINE=1`) it asks nothing. OSV
unreachable: the last answer is kept with the date it was measured, and `degraded` says so. An
advisory published after a project was scanned is matched on the next daily pass, because every
pass asks about every package, not about what changed.

**osv-scanner is not run.** It reads lockfiles itself and would send what this file deliberately
keeps back (local and private names); the standard-library path is the one path.

    vuln_watch.py           run when due
    vuln_watch.py --force   run now
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCHEMA = "vulnerabilities/1"
OSV = "https://api.osv.dev"
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
#: OSV answers up to 1,000 queries per batch request.
BATCH = 1000
#: Pages of one query followed at most; a package with more advisories than this is named.
MAX_PAGES = 20
TIMEOUT = 30
#: Ecosystems whose names OSV knows (research.md, "Vulnerability data").
ECOSYSTEMS = frozenset({"npm", "PyPI", "Go", "crates.io", "RubyGems", "Packagist", "Maven", "NuGet"})
#: Severity words an advisory database writes, to one scale.
DB_SEVERITY = {"CRITICAL": "critical", "HIGH": "high", "MODERATE": "medium", "MEDIUM": "medium", "LOW": "low"}
LEVEL_RANK = {"critical": 4, "high": 3, "unknown": 2.5, "medium": 2, "low": 1}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def http(method: str, url: str, body: bytes | None = None, timeout: float = TIMEOUT) -> bytes:
    """The default fetcher: urllib with a timeout. Raises OSError (URLError is one) on any failure."""
    import urllib.request
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Content-Type": "application/json", "User-Agent": "project-observatory"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https origins above)
        return resp.read()


def switched_on(settings: dict | None = None, environ=None) -> bool:
    """On by default; off when `integrations.vulnerabilities` is false or the process is offline."""
    import configuration
    return configuration.enabled("vulnerabilities", settings=settings, environ=environ)


# ---- inventory -----------------------------------------------------------------------------------------

def inventory(registry_doc: dict | None) -> list[tuple[str, str, str]]:
    """(ecosystem, name, version) of every public, pinned package, once across projects."""
    out = set()
    for p in (registry_doc or {}).get("projects") or []:
        for r in p.get("packages") or []:
            if r.get("local") or r.get("pinned") is False or not r.get("version") or not r.get("name"):
                continue
            if r.get("ecosystem") in ECOSYSTEMS:
                out.add((r["ecosystem"], r["name"], str(r["version"])))
    return sorted(out)


def digest(queries) -> str:
    return hashlib.sha256("\n".join("\t".join(q) for q in sorted(queries)).encode()).hexdigest()


def due(state: dict | None, queries, today: str) -> bool:
    state = state or {}
    return state.get("last_complete_on") != today or state.get("inventory") != digest(queries)


# ---- OSV ------------------------------------------------------------------------------------------------

def query_batch(fetch, queries) -> tuple[dict, list[dict]]:
    """{(eco, name, version): {id: modified}} and the degraded rows. Raises OSError/ValueError when OSV
    cannot be read at all."""
    found: dict[tuple, dict[str, str]] = {q: {} for q in queries}
    pending: list[tuple[tuple, str | None]] = [(q, None) for q in queries]
    degraded: list[dict] = []
    pages: dict[tuple, int] = {}
    while pending:
        chunk, pending = pending[:BATCH], pending[BATCH:]
        body = {"queries": [{"package": {"ecosystem": q[0], "name": q[1]}, "version": q[2],
                             **({"page_token": token} if token else {})} for q, token in chunk]}
        answer = json.loads(fetch("POST", f"{OSV}/v1/querybatch", json.dumps(body).encode(), TIMEOUT))
        results = answer.get("results")
        if not isinstance(results, list) or len(results) != len(chunk):
            raise ValueError("the batch answer does not hold one result per query")
        for (q, _token), result in zip(chunk, results):
            for v in (result or {}).get("vulns") or []:
                if isinstance(v, dict) and v.get("id"):
                    found[q][v["id"]] = v.get("modified") or ""
            token = (result or {}).get("next_page_token")
            if token:
                pages[q] = pages.get(q, 1) + 1
                if pages[q] > MAX_PAGES:
                    degraded.append({"source": "osv", "reason": f"{q[0]} {q[1]} {q[2]}: more than {MAX_PAGES} "
                                                                f"pages of advisories; the rest were not read"})
                else:
                    pending.append((q, token))
    return found, degraded


def _cache_file(cache_dir: pathlib.Path, vid: str) -> pathlib.Path:
    return cache_dir / (re.sub(r"[^A-Za-z0-9._-]", "_", vid) + ".json")


def details(fetch, wanted: dict[str, str], cache_dir: pathlib.Path) -> tuple[dict[str, dict], list[dict]]:
    """Each advisory's record, from the cache unless OSV lists a newer `modified` than the cached one."""
    import atomic
    out, degraded = {}, []
    for vid, modified in sorted(wanted.items()):
        path = _cache_file(cache_dir, vid)
        cached = None
        if path.is_file():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cached = None
        if cached and str(cached.get("modified") or "") >= str(modified or ""):
            out[vid] = cached
            continue
        try:
            rec = json.loads(fetch("GET", f"{OSV}/v1/vulns/{vid}", None, TIMEOUT))
        except (OSError, ValueError) as exc:
            if cached:
                out[vid] = cached
            degraded.append({"source": "osv", "reason": f"the record of {vid} could not be read "
                                                        f"({type(exc).__name__})"
                                                        + ("; the cached one is used" if cached else "")})
            continue
        if isinstance(rec, dict) and rec.get("id"):
            out[vid] = rec
            cache_dir.mkdir(parents=True, exist_ok=True)
            atomic.write_json(path, rec)
    return out, degraded


def known_exploited(fetch, cache_dir: pathlib.Path, today: str) -> tuple[set[str], str | None, list[dict]]:
    """(CVE ids, the day the catalogue was read, degraded). Read at most once a UTC day."""
    import atomic
    path = cache_dir / "kev.json"
    cached = None
    if path.is_file():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
    if cached and cached.get("fetched_on") == today:
        return set(cached.get("cves") or []), today, []
    try:
        doc = json.loads(fetch("GET", KEV_URL, None, TIMEOUT))
        cves = sorted({v["cveID"] for v in doc.get("vulnerabilities") or [] if isinstance(v, dict) and v.get("cveID")})
    except (OSError, ValueError, AttributeError) as exc:
        reason = f"the known-exploited catalogue could not be read ({type(exc).__name__})"
        if cached:
            return (set(cached.get("cves") or []), cached.get("fetched_on"),
                    [{"source": "kev", "reason": f"{reason}; the copy of {cached.get('fetched_on')} is used"}])
        return set(), None, [{"source": "kev", "reason": f"{reason}; no advisory is marked exploited"}]
    cache_dir.mkdir(parents=True, exist_ok=True)
    atomic.write_json(path, {"fetched_on": today, "cves": cves})
    return set(cves), today, []


# ---- severity and fixed versions -------------------------------------------------------------------------

_V3 = {"AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}, "AC": {"L": 0.77, "H": 0.44},
       "UI": {"N": 0.85, "R": 0.62}, "C": {"H": 0.56, "L": 0.22, "N": 0.0}}


def _roundup(x: float) -> float:
    i = round(x * 100000)
    return i / 100000.0 if i % 10000 == 0 else (i // 10000 + 1) / 10.0


def cvss3_score(vector: str) -> float | None:
    """The CVSS v3.x base score of a vector (FIRST's specification, section 7.1); None when unreadable."""
    try:
        m = dict(part.split(":", 1) for part in vector.split("/")[1:])
        scope = m["S"]
        pr = {"N": 0.85, "L": 0.68 if scope == "C" else 0.62, "H": 0.5 if scope == "C" else 0.27}[m["PR"]]
        iss = 1 - (1 - _V3["C"][m["C"]]) * (1 - _V3["C"][m["I"]]) * (1 - _V3["C"][m["A"]])
        impact = 6.42 * iss if scope == "U" else 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15
        exploit = 8.22 * _V3["AV"][m["AV"]] * _V3["AC"][m["AC"]] * pr * _V3["UI"][m["UI"]]
    except (KeyError, ValueError, TypeError):
        return None
    if impact <= 0:
        return 0.0
    return _roundup(min(impact + exploit, 10) if scope == "U" else min(1.08 * (impact + exploit), 10))


def _rating(score: float) -> str:
    return "critical" if score >= 9 else "high" if score >= 7 else "medium" if score >= 4 else "low"


def severity_of(rec: dict) -> tuple[str, str]:
    """(critical|high|medium|low|unknown, where it came from): the advisory database's own word first,
    then a CVSS v3 vector's base score. A CVSS v4 vector alone is not scored here: `unknown`."""
    word = ((rec.get("database_specific") or {}).get("severity") or "").upper()
    if word in DB_SEVERITY:
        return DB_SEVERITY[word], "database"
    for aff in rec.get("affected") or []:
        word = (((aff or {}).get("database_specific") or {}).get("severity")
                or ((aff or {}).get("ecosystem_specific") or {}).get("severity") or "")
        if str(word).upper() in DB_SEVERITY:
            return DB_SEVERITY[str(word).upper()], "database"
    for sev in rec.get("severity") or []:
        if (sev or {}).get("type") in ("CVSS_V3", "CVSS_V31", "CVSS_V30"):
            score = cvss3_score(str(sev.get("score") or ""))
            if score is not None:
                return _rating(score), f"cvss {score}"
    return "unknown", "none"


def _vkey(v: str) -> tuple:
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"[^A-Za-z0-9]+", v) if p)


def fixed_for(fixed: list[str], current: str) -> str | None:
    """The lowest fixed version above `current`, by a plain numeric comparison (each ecosystem's own
    ordering is not applied); None when no listed fix is above it."""
    above = [f for f in fixed if _vkey(f) > _vkey(current)]
    return min(above, key=_vkey) if above else None


def summarize(rec: dict, cves: set[str]) -> dict:
    fixed: dict[str, list[str]] = {}
    for aff in rec.get("affected") or []:
        pkg = (aff or {}).get("package") or {}
        key = f"{pkg.get('ecosystem')}/{pkg.get('name')}"
        for rng in aff.get("ranges") or []:
            for ev in rng.get("events") or []:
                if (ev or {}).get("fixed") and rng.get("type") != "GIT":
                    fixed.setdefault(key, [])
                    if ev["fixed"] not in fixed[key]:
                        fixed[key].append(ev["fixed"])
    level, origin = severity_of(rec)
    aliases = sorted({a for a in rec.get("aliases") or [] if isinstance(a, str)})
    ids = {rec.get("id")} | set(aliases)
    return {"id": rec.get("id"), "aliases": aliases, "summary": str(rec.get("summary") or "")[:300],
            "severity": level, "severity_from": origin, "modified": rec.get("modified"),
            "published": rec.get("published"), "fixed": fixed,
            "exploited": bool(ids & cves), "withdrawn": rec.get("withdrawn")}


# ---- one pass ---------------------------------------------------------------------------------------------

def carried(previous: dict | None, reason: str) -> dict:
    """The last answer, kept with the day it was measured, when OSV cannot be asked now."""
    prev = previous or {}
    when = prev.get("checked_on")
    said = (f"OSV could not be asked ({reason}); the answer of {when} is kept" if when
            else f"OSV could not be asked ({reason}) and no earlier answer exists, so no package is known vulnerable")
    return {"schema": SCHEMA, "checked_at": prev.get("checked_at"), "checked_on": when, "enabled": True,
            "complete": False, "queried": prev.get("queried", 0), "matches": prev.get("matches") or [],
            "advisories": prev.get("advisories") or {}, "kev": prev.get("kev") or {},
            "degraded": [{"source": "osv", "reason": said}]}


def run(queries, fetch, cache_dir: pathlib.Path, today: str, previous: dict | None = None) -> dict:
    """One pass over `queries`: the matches, each advisory summarized, and what could not be read."""
    try:
        found, degraded = query_batch(fetch, queries)
    except (OSError, ValueError, AttributeError) as exc:
        return carried(previous, type(exc).__name__)
    wanted: dict[str, str] = {}
    for ids in found.values():
        for vid, modified in ids.items():
            wanted[vid] = max(wanted.get(vid, ""), str(modified))
    records, bad = details(fetch, wanted, cache_dir)
    degraded += bad
    cves, kev_on, bad = known_exploited(fetch, cache_dir, today) if wanted else (set(), None, [])
    degraded += bad
    advisories = {vid: summarize(rec, cves) for vid, rec in records.items()}
    matches = [{"ecosystem": q[0], "name": q[1], "version": q[2],
                "advisories": sorted(vid for vid in ids if not (advisories.get(vid) or {}).get("withdrawn"))}
               for q, ids in sorted(found.items()) if ids]
    matches = [m for m in matches if m["advisories"]]
    return {"schema": SCHEMA, "checked_at": now(), "checked_on": today, "enabled": True,
            "complete": not degraded, "queried": len(queries), "matches": matches,
            "advisories": {k: advisories[k] for k in sorted(advisories)},
            "kev": {"fetched_on": kev_on, "count": len(cves)}, "degraded": degraded}


# ---- the agent's view (observatory_dependencies) -----------------------------------------------------------

_READ = object()


def _read(path: pathlib.Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def agent_view(project_id: str | None = None, vulnerable_only: bool = False, limit: int = 50,
               cursor: str | None = None, *, registry_doc=_READ, vulns_doc=_READ, known_projects=None) -> dict:
    """Every package row of the inventory, the vulnerable first, paged; each with its advisories."""
    if registry_doc is _READ or vulns_doc is _READ or (project_id and known_projects is None):
        import paths
        if registry_doc is _READ:
            registry_doc = _read(paths.REGISTRY / "dependencies.json")
        if vulns_doc is _READ:
            vulns_doc = _read(paths.SCRATCH / "vulnerabilities.json")
        if project_id and known_projects is None:
            known_projects = {p.get("id") for p in ((_read(paths.REGISTRY / "projects.json") or {}).get("projects") or [])}
    if project_id and known_projects is not None and project_id not in known_projects:
        return {"error": "unknown project", "projectId": project_id[:120],
                "hint": "call observatory_status to list what exists", "degraded": []}
    degraded: list[dict] = []
    if not registry_doc:
        degraded.append({"source": "dependencies", "reason": "no dependency inventory has been built; run "
                                                             "`project-observatory full scan-dependencies` and `emit`"})
    else:
        degraded += list(registry_doc.get("degraded") or [])
    if not vulns_doc:
        degraded.append({"source": "osv", "reason": "the vulnerability watch has not run; no package is known "
                                                    "vulnerable or not"})
    else:
        degraded += list(vulns_doc.get("degraded") or [])
        if vulns_doc.get("enabled") is False:
            degraded.append({"source": "osv", "reason": "the watch is switched off (integrations.vulnerabilities)"})
    matches = {(m["ecosystem"], m["name"], m["version"]): m["advisories"]
               for m in (vulns_doc or {}).get("matches") or []}
    advisories = (vulns_doc or {}).get("advisories") or {}
    rows = []
    for p in (registry_doc or {}).get("projects") or []:
        if project_id and p.get("project") != project_id:
            continue
        if project_id:
            degraded += list(p.get("degraded") or [])
        for r in p.get("packages") or []:
            ids = [] if r.get("local") else matches.get((r.get("ecosystem"), r.get("name"), str(r.get("version"))), [])
            found = []
            for vid in ids:
                a = advisories.get(vid) or {"id": vid}
                key = f"{r.get('ecosystem')}/{r.get('name')}"
                found.append({"id": vid, "aliases": a.get("aliases") or [], "severity": a.get("severity", "unknown"),
                              "exploited": bool(a.get("exploited")), "summary": a.get("summary"),
                              "fixed": fixed_for((a.get("fixed") or {}).get(key) or [], str(r.get("version")))})
            if vulnerable_only and not found:
                continue
            rows.append({"project": p.get("project"), **{k: r.get(k) for k in ("name", "version", "ecosystem",
                                                                               "direct", "local", "file")},
                         **({"source": r["source"]} if r.get("source") else {}), "advisories": found})
    worst = lambda r: max([LEVEL_RANK.get(a["severity"], 2.5) + (1 if a["exploited"] else 0)  # noqa: E731
                           for a in r["advisories"]] or [0])
    rows.sort(key=lambda r: (-worst(r), r["project"] or "", r["ecosystem"] or "", r["name"] or "",
                             str(r["version"]), r["file"] or ""))
    key = lambda r: f"{r['project']}|{r['ecosystem']}|{r['name']}|{r['version']}|{r['file']}"  # noqa: E731
    total = len(rows)
    if cursor:
        at = next((i for i, r in enumerate(rows) if key(r) == cursor), None)
        if at is None:
            return {"error": "unknown cursor", "cursor": cursor[:120],
                    "hint": "start again without `cursor`; the inventory is rebuilt every tick", "degraded": []}
        rows = rows[at + 1:]
    limit = max(1, min(int(limit), 500))
    page = rows[:limit]
    out = {"scannedOn": (registry_doc or {}).get("updated_on"), "checkedOn": (vulns_doc or {}).get("checked_on"),
           "total": total, "packages": page, "degraded": degraded}
    if len(rows) > limit and page:
        out["nextCursor"] = key(page[-1])
    return out


# ---- the step ----------------------------------------------------------------------------------------------

def main(argv: list[str], fetch=http) -> int:
    import atomic
    import paths
    out = paths.SCRATCH / "vulnerabilities.json"
    state_file = paths.STATE / "vuln-watch.json"
    cache_dir = paths.STATE / "state" / "osv-cache"
    today = datetime.now(timezone.utc).date().isoformat()
    previous = _read(out)
    if not switched_on():
        doc = {"schema": SCHEMA, "checked_at": now(), "checked_on": None, "enabled": False, "complete": False,
               "queried": 0, "matches": [], "advisories": {}, "kev": {},
               "degraded": [{"source": "osv", "reason": "switched off (integrations.vulnerabilities) or offline; "
                                                        "nothing was asked"}]}
        atomic.write_json(out, doc)
        print("vulnerabilities: switched off or offline; nothing asked")
        return 0
    registry_doc = _read(paths.REGISTRY / "dependencies.json")
    if registry_doc is None:
        print("vulnerabilities: no dependency inventory yet (registry/dependencies.json); nothing asked")
        return 0
    queries = inventory(registry_doc)
    state = _read(state_file) or {}
    if "--force" not in argv and not due(state, queries, today) and previous:
        print(f"vulnerabilities: not due (checked {state.get('last_complete_on')}, inventory unchanged)")
        return 0
    doc = run(queries, fetch, cache_dir, today, previous)
    atomic.write_json(out, doc)
    state.update({"last_run_on": today, "inventory": digest(queries) if doc["complete"] else state.get("inventory")})
    if doc["complete"]:
        state["last_complete_on"] = today
    atomic.write_json(state_file, state)
    print(f"vulnerabilities: {len(queries)} package(s) asked, {len(doc['matches'])} with advisories, "
          f"{sum(1 for a in doc['advisories'].values() if a.get('exploited'))} advisory(ies) exploited; "
          f"{len(doc['degraded'])} degraded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
