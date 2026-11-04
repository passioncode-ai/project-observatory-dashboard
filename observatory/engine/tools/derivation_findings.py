#!/usr/bin/env python3
"""What the recorded copies and moves owe the operator (OBS-321, `derivations.py`).

Reads `registry/derivations.json`, which the emit rebuilds from `config/derivations.json`,
each copy's tracked `.copy/upstream.json` and the source's local clone. Silent when the
document is absent: no curated relation is not a clean bill, and `derivations.load` says
so to the readers. One row per relation, because each is one decision about one copy.

- `copy.behind_source`: the source branch has commits after `sync_base` (outside the
  copy's deny paths, when it lists them), or the count could not be measured here.
- `copy.relation_unrecorded`: a copy says it is a copy and nothing here records its source;
  or a recorded copy does not say so, or says it in a record that cannot be read.
- `copy.record_differs`: the curated entry and the copy's own record disagree; the curated
  one is used.
- `copy.moved_source_open`: a move whose old repository is not archived, so a session whose
  clone still points at it would fork the history without a word.

NO TIMESTAMP INSIDE A FINDING beyond the document's own and the day it is judged on.
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

EVIDENCE = ["registry:derivations.json"]
CONFIG = "config/derivations.json"
#: Days an unsynced source commit may wait before being behind is a warning, not news.
WARN_AFTER_DAYS = 7
TYPES = ("copy.behind_source", "copy.relation_unrecorded", "copy.record_differs", "copy.moved_source_open")


def _age_days(day: str | None, today: str) -> int | None:
    try:
        return (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(str(day))).days
    except ValueError:
        return None


def _behind(rel: dict, today: str) -> dict | None:
    copy, source, behind = rel["copy"], rel["source"], rel.get("behind") or {}
    if rel.get("kind") != "copy" or not behind:
        return None
    since = behind.get("since") or "sync_base"
    if behind.get("state") != "measured":
        return {"type": "copy.behind_source", "subject": copy, "severity": "info",
                **titled("{copy} may be behind its source: not measured", copy=copy),
                "detail": f"{copy} is recorded as a copy of {source}, and how many commits {source} has after "
                          f"its {since} is unknown here: {behind.get('reason')}.",
                "action": "clone or fetch the source on this machine and run `project-observatory full emit`; "
                          "nothing is fetched by Observatory itself",
                "evidence": EVIDENCE}
    commits, outside = behind.get("commits") or 0, behind.get("outside_deny")
    counted = commits if outside is None else outside
    if counted <= 0:
        return None
    age = _age_days(behind.get("oldest_on"), today)
    severity = "warning" if age is not None and age > WARN_AFTER_DAYS else "info"
    detail = (f"{source} has {commits} commit(s) on {behind.get('ref')} (head {behind.get('head')}) after "
              f"{rel['copy']}'s {since} {((rel.get(since) or {}).get('source_commit'))}"
              + (f", {outside} of them outside the copy's deny paths" if outside is not None else
                 "; the copy lists no deny paths, so every commit counts")
              + (f"; the oldest is from {behind['oldest_on']}" if behind.get("oldest_on") else "")
              + ". Counted in the source's local clone as of its last fetch.")
    if behind.get("on_branch") is False:
        detail += (f" The recorded commit is not on {behind.get('ref')}: the copy was taken from another "
                   "branch, so the count includes work that may never have been meant for it.")
    return {"type": "copy.behind_source", "subject": copy, "severity": severity,
            **titled("{copy} is {n} commits behind its source", copy=copy, n=counted),
            "detail": detail,
            "action": f"carry the changes into the copy through its own builder and a reviewed pull request, "
                      f"then record the new sync_base in {CONFIG}; "
                      f"`project-observatory full copies plan {copy}` prints the steps",
            "evidence": EVIDENCE}


def findings(doc: dict | None, today: str | None = None) -> list[dict]:
    if not isinstance(doc, dict) or not isinstance(doc.get("relations"), list):
        return []
    today = today or datetime.date.today().isoformat()
    out: list[dict] = []
    for rel in doc["relations"]:
        if not isinstance(rel, dict) or not rel.get("copy") or not rel.get("source"):
            continue
        copy, source = rel["copy"], rel["source"]
        row = _behind(rel, today)
        if row:
            out.append(row)
        record = rel.get("copy_record") or {}
        if rel.get("kind") == "copy" and record.get("state") == "absent":
            out.append({"type": "copy.relation_unrecorded", "subject": copy, "severity": "info",
                        **titled("{copy} is recorded as a copy but does not say so", copy=copy),
                        "detail": f"{CONFIG} records {copy} as a copy of {source}, and the copy's clone tracks "
                                  f"no .copy/upstream.json at HEAD, so its own readers cannot tell it is a copy "
                                  f"or what it was last synced to.",
                        "action": "commit a .copy/upstream.json in the copy (kind rewritten or export, copy_point, sync_base, deny; "
                                  "never the source's name) and a README line saying it is carried forward",
                        "evidence": EVIDENCE})
        elif record.get("state") == "unreadable" or record.get("problems"):
            why = record.get("reason") or "; ".join(record.get("problems") or [])
            out.append({"type": "copy.relation_unrecorded", "subject": copy, "severity": "warning",
                        **titled("{copy} has a copy record that cannot be read", copy=copy),
                        "detail": f"{copy}'s .copy/upstream.json could not be used: {why}. The values in "
                                  f"{CONFIG} are used alone.",
                        "action": "fix .copy/upstream.json in the copy",
                        "evidence": EVIDENCE})
        differs = record.get("differs") or []
        if differs:
            said = "; ".join(f"{d.get('field')}: {CONFIG} says {d.get('curated')}, the copy says {d.get('copy')}"
                             for d in differs)
            out.append({"type": "copy.record_differs", "subject": copy, "severity": "warning",
                        **titled("{copy} and its copy record disagree", copy=copy),
                        "detail": f"{said}. The curated value is used; one of the two records is wrong.",
                        "action": f"find which is right (the copy's sync history), then correct {CONFIG} or the "
                                  f"copy's .copy/upstream.json",
                        "evidence": EVIDENCE})
        if rel.get("kind") == "move" and rel.get("source_archived") is not True:
            out.append({"type": "copy.moved_source_open", "subject": source, "severity": "warning",
                        **titled("{source} was moved but is not archived", source=source),
                        "detail": f"{CONFIG} records {source} as moved to {copy}, and the registry says "
                                  f"{source} is not archived. A session whose clone still points at it can push "
                                  f"there and fork the history without a word.",
                        "action": f"archive {source} at its host once nothing pushes to it, and point remaining "
                                  f"clones at {copy}",
                        "evidence": EVIDENCE})
    for row in doc.get("unrecorded") or []:
        if not isinstance(row, dict) or not row.get("copy"):
            continue
        copy = row["copy"]
        out.append({"type": "copy.relation_unrecorded", "subject": copy, "severity": "warning",
                    **titled("{copy} says it is a copy, but its source is not recorded", copy=copy),
                    "detail": f"{copy} tracks a .copy/upstream.json, and no accepted entry of {CONFIG} names "
                              f"its source, so nothing measures how far behind it is"
                              + (" (an entry naming it was refused; see `degraded`)." if row.get("refused")
                                 else "."),
                    "action": f"add an entry for {copy} to {CONFIG}: copy, source, kind, copy_point, sync_base",
                    "evidence": EVIDENCE})
    return out


def from_file(path: pathlib.Path, today: str | None = None) -> list[dict]:
    try:
        return findings(json.loads(path.read_text(encoding="utf-8")), today)
    except (OSError, ValueError):
        return []
