#!/usr/bin/env python3
"""What the vulnerability watch owes the operator (OBS-553, `tools/vuln_watch.py`).

Reads `registry/dependencies.json` (what each project resolves) and `store/raw/vulnerabilities.json`
(which public packages OSV matched). Silent until the watch has run once, and silent while it is
switched off: no answer is not a clean bill, and `observatory_dependencies` says so in `degraded`.

- `dep.vulnerable`: one row per project, package and version, carrying every advisory that matches
  it. Grouped per package rather than per advisory because the remedy is one upgrade of one package
  in one lockfile, and an acknowledgement should silence that one decision. Severity follows the
  worst advisory (critical: critical; high or not stated: warning; medium or low: info) and is
  raised one level when an advisory's CVE is in the known-exploited catalogue.
- `dep.watch_stale`: the last complete check is older than STALE_DAYS, so new advisories are not
  being matched.

NO TIMESTAMP INSIDE A FINDING beyond the day the watch checked.
"""
from __future__ import annotations
import pathlib as _pathlib
import sys as _sys
_DASHBOARD = str(_pathlib.Path(__file__).resolve().parents[1] / "dashboard")
if _DASHBOARD not in _sys.path:
    _sys.path.append(_DASHBOARD)  # `finding_types.titled`: a title is a message id
from finding_types import titled  # noqa: E402

import datetime
import json
import pathlib

EVIDENCE = ["registry:dependencies.json", "store/raw/vulnerabilities.json"]
TYPES = ("dep.vulnerable", "dep.watch_stale")
#: Days without a complete check before the watch is reported as stale (the watch runs daily).
STALE_DAYS = 3
LEVEL_RANK = {"critical": 4, "high": 3, "unknown": 2.5, "medium": 2, "low": 1}
#: An advisory's level -> a finding's severity, before the exploited boost.
SEVERITY = {"critical": "critical", "high": "warning", "unknown": "warning", "medium": "info", "low": "info"}
RAISED = {"info": "warning", "warning": "critical", "critical": "critical"}


def _vkey(v: str) -> tuple:
    import re
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"[^A-Za-z0-9]+", v) if p)


def _fixed(fixed: list[str], current: str) -> str | None:
    above = [f for f in fixed if _vkey(f) > _vkey(current)]
    return min(above, key=_vkey) if above else None


def _where(row: dict, read_from: list[dict]) -> tuple[str, str]:
    """(project folder, lockfile path within it) of a package row."""
    file = str(row.get("file") or "")
    for r in read_from:
        if r.get("file") == file and r.get("folder"):
            return r["folder"], r.get("path") or file[len(r["folder"]) + 1:]
    folder, _, rest = file.partition("/")
    return folder, rest


def _age_days(day: str | None, today: str) -> int | None:
    try:
        return (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(str(day))).days
    except ValueError:
        return None


def findings(deps: dict | None, vulns: dict | None, today: str | None = None) -> list[dict]:
    if not isinstance(deps, dict) or not isinstance(vulns, dict) or vulns.get("enabled") is False:
        return []
    today = today or datetime.date.today().isoformat()
    out: list[dict] = []
    age = _age_days(vulns.get("checked_on"), today)
    if age is None or age > STALE_DAYS:
        when = vulns.get("checked_on") or "never"
        out.append({"type": "dep.watch_stale", "subject": "dependencies", "severity": "warning",
                    **titled("Vulnerability watch last checked {day}", day=when),
                    "detail": f"The last complete OSV check was {when}; advisories published since are not "
                              f"matched to the estate's packages. "
                              + "; ".join(d.get("reason", "") for d in vulns.get("degraded") or [])[:400],
                    "action": "let the tick reach the network, or run `project-observatory full vulnerabilities "
                              "--force`; `observatory_dependencies` names what was not read",
                    "evidence": EVIDENCE})
    matches = {(m.get("ecosystem"), m.get("name"), str(m.get("version"))): m.get("advisories") or []
               for m in vulns.get("matches") or []}
    advisories = vulns.get("advisories") or {}
    seen: set[str] = set()
    for p in deps.get("projects") or []:
        project = p.get("project")
        for row in p.get("packages") or []:
            if row.get("local"):
                continue
            eco, name, version = row.get("ecosystem"), row.get("name"), str(row.get("version"))
            ids = matches.get((eco, name, version))
            if not ids or not project:
                continue
            folder, lockfile = _where(row, p.get("read_from") or [])
            subject = f"dep:{pathlib.PurePosixPath(folder).name}/{eco}/{name}@{version}"
            if subject in seen:
                continue
            seen.add(subject)
            found = [advisories.get(i) or {"id": i, "severity": "unknown"} for i in ids]
            found.sort(key=lambda a: (-(LEVEL_RANK.get(a.get("severity"), 2.5) + (1 if a.get("exploited") else 0)),
                                      a.get("id") or ""))
            main = found[0]
            exploited = any(a.get("exploited") for a in found)
            severity = SEVERITY.get(main.get("severity"), "warning")
            if exploited:
                severity = RAISED[severity]
            fixes = [_fixed((a.get("fixed") or {}).get(f"{eco}/{name}") or [], version) for a in found]
            fixed = max((f for f in fixes if f), key=_vkey, default=None)
            aliases = sorted({x for a in found for x in a.get("aliases") or []})
            listed = ", ".join(a.get("id", "?") for a in found[:5]) + (f" and {len(found) - 5} more" if len(found) > 5 else "")
            out.append({
                "type": "dep.vulnerable", "subject": subject, "severity": severity,
                **titled("{package} {version} has {n} known vulnerabilities", package=name, version=version,
                         n=len(found)),
                "detail": (f"{name} {version} ({eco}, {'direct' if row.get('direct') else 'transitive' if row.get('direct') is False else 'direct or transitive not known'}) "
                           f"in {project}'s {folder}/{lockfile} matches {listed}. The worst is "
                           f"{main.get('id')}, {main.get('severity', 'unknown')}"
                           + (f": {main['summary']}" if main.get("summary") else "")
                           + (". Its CVE is in the known-exploited catalogue." if exploited else ".")
                           + f" Checked against OSV on {vulns.get('checked_on')}."),
                "action": (f"upgrade {name} to {fixed} or later in {lockfile} and commit the lockfile"
                           if fixed else f"no fixed version above {version} is published: read {main.get('id')} for "
                                         f"a workaround, or replace {name}"),
                "evidence": EVIDENCE,
                "project": project, "package": name, "version": version, "ecosystem": eco,
                "advisory": main.get("id"), "advisories": [a.get("id") for a in found], "aliases": aliases,
                "fixed": fixed, "exploited": exploited, "lockfile": lockfile, "folder": folder,
            })
    return out


def from_files(deps_path: pathlib.Path, vulns_path: pathlib.Path, today: str | None = None) -> list[dict]:
    def read(path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    return findings(read(deps_path), read(vulns_path), today)
