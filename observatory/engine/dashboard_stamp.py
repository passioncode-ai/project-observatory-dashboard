"""Which registry the dashboard pages were built from, and whether the registry has moved since.

WHY (OBS-504, 2026-11-11). On one loaded machine three ticks of one night stopped the dashboard step
at its limit, after the merge and the emit had rewritten the registry. The pages kept the tick
before's build for hours and said nothing: they showed 301 projects while the registry held 302.
A static page cannot know that the registry moved after it was written, so the build records the
registry it read (`record`, `built-from.json` beside the pages), and the two readers that can ask
now compare it with the registry as it stands (`compare`):

* the tick, after its dashboard step, writes the answer into its report (`tick.json`:
  `dashboard_current`, and `dashboard_note` when the pages are behind);
* the local server answers `/dashboard-freshness`, and a page opened through it shows a notice
  when the registry it was built from is not the registry now.

A registry is named by a fingerprint of the files the emit writes and the pages are drawn from
(`CORE`), never by a clock: a rebuilt but unchanged registry is the same registry. Standard library
only; nothing here raises on a missing or damaged file, which reads as unknown (`current: None`).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import atomic
import paths

#: The registry files a page's projects, repositories, relations and domains come from. The findings
#: are not among them: they are rebuilt every tick (and again after a blank page), and a page built
#: before a recheck is not built from an older registry.
CORE = ("projects.json", "repositories.json", "relations.json", "domains.json")
#: The record beside the pages.
STAMP_NAME = "built-from.json"
SCHEMA = 1


def registry_stamp(registry: Path | None = None) -> dict:
    """{fingerprint, updated_on, projects} of the registry in `registry` (paths.REGISTRY by default).

    `fingerprint` hashes the bytes of every CORE file (an absent one hashes as absent), so any change
    to what the pages draw changes it. `updated_on` and `projects` are read from projects.json for a
    person; None when it cannot be read."""
    registry = paths.REGISTRY if registry is None else Path(registry)
    digest = hashlib.sha256()
    projects_doc = None
    for name in CORE:
        try:
            data = (registry / name).read_bytes()
        except OSError:
            data = None
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(b"absent" if data is None else hashlib.sha256(data).digest())
        if name == "projects.json" and data is not None:
            try:
                projects_doc = json.loads(data)
            except ValueError:
                projects_doc = None
    rows = projects_doc.get("projects") if isinstance(projects_doc, dict) else None
    updated = projects_doc.get("updated_on") if isinstance(projects_doc, dict) else None
    return {"fingerprint": digest.hexdigest()[:16],
            "updated_on": updated if isinstance(updated, str) else None,
            "projects": len(rows) if isinstance(rows, list) else None}


def record(stamp: dict, built_at: str | None = None, directory: Path | None = None) -> Path:
    """Write `built-from.json` beside the pages: the registry `stamp` they were built from and when."""
    directory = paths.DASHBOARD_DIR if directory is None else Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    built_at = built_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    target = directory / STAMP_NAME
    atomic.write_json(target, {"schema": SCHEMA, "built_at": built_at, "registry": dict(stamp)})
    return target


def read(directory: Path | None = None) -> dict | None:
    """The record beside the pages, or None when there is none or it cannot be read."""
    directory = paths.DASHBOARD_DIR if directory is None else Path(directory)
    try:
        doc = json.loads((directory / STAMP_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("registry"), dict):
        return None
    if not isinstance(doc["registry"].get("fingerprint"), str):
        return None
    return doc


def compare(directory: Path | None = None, registry: Path | None = None) -> dict:
    """{current, built_at, built_from, registry}: whether the pages were built from the registry now.

    `current` is None when the pages carry no record (never built, or built by an engine before
    OBS-504): unknown, never "current"."""
    now = registry_stamp(registry)
    doc = read(directory)
    if doc is None:
        return {"current": None, "built_at": None, "built_from": None, "registry": now}
    built_from = doc["registry"]
    return {"current": built_from.get("fingerprint") == now["fingerprint"],
            "built_at": doc.get("built_at") if isinstance(doc.get("built_at"), str) else None,
            "built_from": built_from, "registry": now}


def note(answer: dict) -> str:
    """One line for a log or a report when the pages are behind; "" otherwise."""
    if answer.get("current") is not False:
        return ""
    was = (answer.get("built_from") or {}).get("projects")
    now = (answer.get("registry") or {}).get("projects")
    counts = (f" ({was} projects then, {now} now)" if isinstance(was, int) and isinstance(now, int)
              and was != now else "")
    return (f"the dashboard pages were built from an older registry{counts}, at "
            f"{answer.get('built_at') or 'an unrecorded time'}; the next dashboard build replaces them")
