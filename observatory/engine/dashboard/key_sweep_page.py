"""Keys found in repositories: the key sweep's section on the Findings page (OBS-554, CP-08).

Rendered at build time from `store/raw/key-sweep.json` (`tools/key_sweep.py`), above the findings list. One row
per repository the sweep reads: its project, the open sightings and their count per rule, how far the history is
read and the day the working tree was read. Counts and places only: the sweep keeps no value, and this section
shows not even a digest. Each open sighting is a `secret.in_repository` row of the findings list below it, which
carries its action. The section reads and never acts (no button, so nothing here needs an agent's twin, CP-18); an
agent reads the same with `observatory_key_sweep`. A sweep that has not run, is switched off, or read a repository
in part says so; a reason's command is never shown (IS rules).
"""
from __future__ import annotations

import html
import re

from i18n import NO_VALUE, Translator

HEADING = ' class="machine-h"'
HINT = ' class="machine-hint"'
UNKNOWN = NO_VALUE
#: Message ids passed through a variable (the catalogue test reads them here).
DYNAMIC = ("Repository", "Project", "Open", "By rule", "History", "Working tree", "Read to the last commit", "Read in part", "Not read yet", "the key sweep has not run yet",
           "the key sweep is switched off")


def _e(v) -> str:
    return html.escape("" if v is None else str(v))


def summary(doc) -> dict:
    """What the section needs from a sweep document: counts per repository, never a place or a digest."""
    if not isinstance(doc, dict):
        return {"state": "not-run"}
    if doc.get("enabled") is False:
        return {"state": "off"}
    rows = []
    for r in doc.get("repositories") or []:
        if not isinstance(r, dict):
            continue
        rows.append({"repo": r.get("repo"), "project": r.get("project"), "open": int(r.get("open") or 0),
                     "by_rule": dict(r.get("sightings_by_rule") or {}),
                     "history": ("done" if r.get("history_complete") else "part" if r.get("history_through")
                                 else "none"),
                     "tree_on": r.get("tree_scanned_on"),
                     "degraded": [str(d.get("reason") or "") for d in r.get("degraded") or [] if isinstance(d, dict)]})
    rows.sort(key=lambda x: (-x["open"], str(x["repo"])))
    scanners = doc.get("scanners") or {}
    return {"state": "ran", "at": doc.get("at"), "complete": bool(doc.get("complete")), "rows": rows,
            "open": sum(1 for s in doc.get("sightings") or [] if isinstance(s, dict) and s.get("status") == "open"),
            "external": scanners.get("external"),
            "degraded": [str(d.get("reason") or "") for d in doc.get("degraded") or [] if isinstance(d, dict)]}


#: A reason a collector wrote may end in the command that fixes it; a page shows a person no command.
COMMAND = re.compile(r"\s*[;:(]?\s*`[^`]*`.*$")


def _stamp(value) -> str:
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?Z?", str(value or ""))
    return f"{m.group(1)} {m.group(2)} UTC" if m else str(value or UNKNOWN)


def _reason(text: str) -> str:
    return _e(COMMAND.sub("", text).strip())


def key_sweep_html(payload: dict, t: Translator) -> str:
    v = payload.get("key_sweep") or {"state": "not-run"}
    head = t.mark("Keys found in repositories", tag="h2", attrs=HEADING)
    if v.get("state") == "off":
        return f'<section id="key-sweep">{head}{t.mark("the key sweep is switched off", tag="p", attrs=HINT)}</section>'
    if v.get("state") != "ran":
        return f'<section id="key-sweep">{head}{t.mark("the key sweep has not run yet", tag="p", attrs=HINT)}</section>'
    lead = t.mark("Swept {at}: our own values and key-shaped text in working trees and Git history. Only places "
                  "and keyed digests are kept, never a value.", tag="p", attrs=HINT, at=_stamp(v.get("at")))
    scanner = (t.mark("External scanner: {name}, with redaction.", tag="p", attrs=HINT, name=v["external"])
               if v.get("external") else
               t.mark("External scanner: not installed; our own values are still looked for.", tag="p", attrs=HINT))
    heads = ("Repository", "Project", "Open", "By rule", "History", "Working tree")
    th = "".join(f'<th scope="col">{t.mark(h)}</th>' for h in heads)
    body = []
    for r in v.get("rows") or []:
        rules = ", ".join(f"{_e(k)} {int(n)}" for k, n in sorted(r["by_rule"].items())) or UNKNOWN
        history = {"done": "Read to the last commit", "part": "Read in part", "none": "Not read yet"}[r["history"]]
        cells = [f'<span class="mono">{_e(r["repo"])}</span>', _e(r.get("project") or UNKNOWN),
                 str(r["open"]), rules, t.mark(history), _e(r.get("tree_on") or UNKNOWN)]
        body.append("<tr>" + "".join(f'<td{t.attr("data-label", h)}>{c}</td>' for h, c in zip(heads, cells))
                    + "</tr>")
    table = (f'<table><thead><tr>{th}</tr></thead><tbody>{"".join(body)}</tbody></table>' if body
             else t.mark("No repository with a local clone to sweep.", tag="p", attrs=HINT))
    notes = [_reason(x) for x in v.get("degraded") or []]
    notes += [f'<span class="mono">{_e(r["repo"])}</span>: {_reason(x)}' for r in v.get("rows") or []
              for x in r["degraded"]]
    partial = ""
    if notes:
        partial = (t.mark("Not fully read", tag="h3", attrs=' class="tools-h"')
                   + "<ul>" + "".join(f"<li>{n}</li>" for n in notes[:20] if n) + "</ul>")
    return f'<section id="key-sweep">{head}{lead}{scanner}{table}{partial}</section>'
