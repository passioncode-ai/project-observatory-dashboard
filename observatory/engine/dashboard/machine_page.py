"""The Machine page: what runs, where memory and disk go, what was cleaned.

Rendered here, at build time, from `machine_view.summary()` — the page reads and
never acts (OSS scenarios: no administrative mutation from the dashboard). Every
visible string is an English message id marked for the page script, and every
size and date a marked value, so a page built in one language reads in the
reader's (L10N-01, L10N-05) like the others. Tables reuse the
dashboard's own table, card and narrow-screen rules; nothing here adds a style.
"""
from __future__ import annotations

import json
import pathlib

from i18n import NO_VALUE, Translator
from page_util import escape as _e, reason_html, stamp

ROWS = 20
#: Attributes built outside the f-strings: Python 3.11 allows no backslash and no
#: reused quote inside an f-string expression, and the engine supports 3.11.
NUM = ' class="num"'
EMPTY = ' class="empty"'
HEADING = ' class="machine-h"'
TOOLS_LINK = ' href="tools.html"'
#: What an empty table says when its survey has not run. "Every worktree is
#: clean" over checkouts nobody looked at is a claim, not an empty state. Each
#: names its own survey: the git tables are filled by the cleanup survey,
#: not the machine survey (audit A42). The command that runs each is an agent's
#: to read (`degraded` in the JSON and MCP answers); a page shows none (OBS-629).
UNMEASURED = "Not measured yet: the machine survey has not run."
UNMEASURED_GIT = "Not measured yet: the cleanup survey has not run."


def _gb(mb):
    """Megabytes as gigabytes to one decimal, or None."""
    return None if mb is None else round(mb / 1024, 1)


def _gigabytes(gb, t: Translator) -> str:
    """A size in the one unit this page prints, marked: the unit and the
    decimal separator follow the reader's language (a Russian build that mixed
    the English and the Russian unit on one screen was the audit's A42)."""
    if gb is None or isinstance(gb, bool) or not isinstance(gb, (int, float)):
        return NO_VALUE
    return t.mark("{size} GB", size=gb)


def _size(mb, t: Translator) -> str:
    return _gigabytes(_gb(mb), t)


def _frees(row: dict, t: Translator) -> str:
    """What removing a place gives back, under its size, when APFS clones make `du` overstate it
    (OBS-66): `du` counted 19 browser clones as 13 GB, and removing them freed nothing."""
    if not row.get("clones_matter"):
        return ""
    hint = "<br>" + t.mark("removing it frees at least {size} GB; APFS clones share {shared} GB",
                           attrs=' class="machine-hint"', size=round(row["frees_at_least_gb"], 1),
                           shared=round(row["clone_shared_gb"], 1))
    # Figures carried from an earlier private walk say when that walk ran (ENG-006).
    walked = row.get("frees_measured_at")
    if walked and walked != row.get("measured_at"):
        hint += " " + t.mark("as of {date}", attrs=' class="machine-hint"', date=str(walked)[:10])
    return hint


def _word(t: Translator, msgid) -> str:
    """A value the engine writes as an English id (a disk kind, a worktree
    state, a cleanup class), marked so the language switch reaches it."""
    return t.known(msgid) if msgid else ""


def _project(pid, names: dict) -> str:
    """A project as its registry name, linked to its panel — never the raw
    `project:` id the survey records (audit A42; the Agents page does the same)."""
    if not pid:
        return ""
    return f'<a href="projects.html#{_e(pid)}">{_e(names.get(pid) or str(pid).removeprefix("project:"))}</a>'


def _table(t: Translator, caption: str, heads: list[tuple[str, bool]], rows: list[list[str]], empty: str) -> str:
    """One card: a heading, a table with labelled cells (narrow screens stack them)."""
    head = t.mark(caption, tag="h2", attrs=HEADING)
    if not rows:
        return f'<section class="card panel">{head}{t.mark(empty, tag="p", attrs=EMPTY)}</section>'
    th = "".join(f'<th scope="col"{NUM if num else ""}>{t.mark(h)}</th>' for h, num in heads)
    body = []
    for r in rows:
        # `t.attr`, so the page's language switch relabels a stacked cell too.
        cells = "".join(f'<td{NUM if num else ""}{t.attr("data-label", h)}>{c}</td>'
                        for (h, num), c in zip(heads, r))
        body.append(f"<tr>{cells}</tr>")
    return (f'<section class="card panel">{head}<table><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></section>')


def summary_html(m: dict, t: Translator) -> str:
    mem, disk = m.get("memory") or {}, (m.get("disk") or {}).get("volume") or {}
    procs = m.get("processes") or {}
    cleanup = m.get("cleanup") or {}
    done = [r for r in cleanup.get("journal") or [] if r.get("result") in ("removed", "pruned")
            and r.get("mode") != "guard"]
    totals = cleanup.get("guardTotals") or {}
    # The git tier's own rows plus the guard's run summaries (the disk's own count, `freed_mb`:
    # `du` counts an APFS clone in full, and removing one frees nothing).
    freed = (sum(r.get("kb") or 0 for r in done) / 1048576) + (totals.get("freedMb") or 0) / 1024
    done = done + [{}] * int(totals.get("removed") or 0)
    used = None
    if mem.get("total_mb") is not None and mem.get("free_mb") is not None:
        used = mem["total_mb"] - mem["free_mb"] - (mem.get("inactive_mb") or 0)
    auto = t.mark("auto cleanup on") if cleanup.get("autoEnabled") else t.mark("auto cleanup off")
    swap = _gb(mem.get("swap_used_mb"))
    total_gb = disk.get("total_gb")
    tiles = [
        (t.mark("Memory in use"),
         t.mark("{used} / {total} GB", used=_gb(used), total=_gb(mem.get("total_mb")))
         if used is not None and mem.get("total_mb") is not None else NO_VALUE,
         t.mark("swap {size} GB", size=swap) if swap is not None else t.mark("swap {size}", size=NO_VALUE)),
        (t.mark("Free disk"), _gigabytes(disk.get("free_gb"), t),
         t.mark("{percent}% of {total} GB", percent=disk.get("free_percent", NO_VALUE), total=total_gb)
         if isinstance(total_gb, (int, float)) else t.mark("{percent}% of {total}", percent=disk.get("free_percent", NO_VALUE),
                                                          total=NO_VALUE)),
        (t.mark("Processes"), _e(procs.get("count") if procs.get("count") is not None else NO_VALUE),
         t.mark("{n} origins", n=len(procs.get("groups") or []))),
        (t.mark("Cleaned in 7 days"), _e(len(done)),
         auto + (" · " + _gigabytes(round(freed, 1), t) if freed else "")),
    ]
    cells = "".join(f'<div class="tile">{k}<b>{v}</b>{sub}</div>' for k, v, sub in tiles)
    return f'<div class="tiles machine-tiles">{cells}</div>'


GUARD_CLASSES = ("cache-command", "cache-dir", "build-output", "user-temp", "temp", "trash", "browser-clone")


def disk_line(m: dict, t: Translator) -> str:
    """The disk guard and the cleanup moved to the Disk page (OBS-562): one line here, and the link."""
    guard, cleanup = m.get("guard") or {}, m.get("cleanup") or {}
    auto = t.mark("auto cleanup on") if cleanup.get("autoEnabled") else t.mark("auto cleanup off")
    last = (t.mark("the disk guard last ran {at}", at=stamp(guard.get("at"))) if guard.get("at")
            else t.mark("the disk guard has not run yet"))
    link = t.mark("Disk and cleanup", tag="a", attrs=' href="disk.html"')
    return f'<p class="machine-at">{link} · {auto} · {last}</p>'


def simulators_html(g: dict | None, t: Translator) -> str:
    """The simulators the guard found holding memory that swap writes to this disk, each with what brings it back."""
    sims = ((g or {}).get("memory") or {}).get("simulators") or []
    if not sims:
        return ""
    return _table(t, "Running simulators",
                  [("Device", False), ("Memory", True), ("How it comes back", False)],
                  [[_e(x["name"]), _size(x.get("rss_mb"), t), f'<span class="mono">{_e(x["command"])}</span>']
                   for x in sims[:ROWS]], "")


def machine_html(payload: dict, t: Translator | None = None) -> str:
    t = t or Translator()
    m = payload.get("machine") or {}
    parts = [f'<section id="machine" class="machine">']
    if m.get("measuredAt"):
        parts.append(t.mark("Machine surveyed {at}", tag="p", attrs=' class="machine-at"', at=stamp(m["measuredAt"])))
    parts.append(summary_html(m, t))
    # The coding agents' skills, plugins and hooks have their own page (OBS-552).
    parts.append(f'<p class="machine-hint">{t.mark("Coding agents, skills, plugins and hooks installed here:")} '
                 f'{t.mark("Installed tools", tag="a", attrs=TOOLS_LINK)}</p>')
    for section, label in (("memory", "Memory"), ("processes", "Processes")):
        sample = m.get(section) or {}
        if sample.get("source"):
            state = t.mark("stale") if sample.get("stale") is True else (
                t.mark("Not measured") if sample.get("stale") is None else "")
            if sample.get("degraded"):
                state += " · " + t.mark("incomplete measurement")
            parts.append(f'<p class="machine-at">{t.mark(label)} · '
                         f'{t.mark("as of {date}", date=stamp(sample.get("measured_at")))} · {state}</p>')
    procs = m.get("processes") or {}
    if procs.get("rss_semantics"):
        parts.append(t.mark("RSS totals may count shared pages more than once.", tag="p", attrs=' class="machine-hint"'))
    names = {r.get("id"): r.get("name") for r in payload.get("rows") or [] if isinstance(r, dict) and r.get("id")}
    parts.append(_table(t, "Memory by origin",
                        [("Origin", False), ("Processes", True), ("Sessions", True), ("Memory", True), ("CPU %", True)],
                        [[_e(g["origin"]), _e(g["processes"]), _e(g.get("sessions") if g.get("sessions") is not None else NO_VALUE), _size(g.get("rss_mb"), t),
                          t.number(g["cpu"]) if g.get("cpu") is not None else NO_VALUE]
                         for g in (procs.get("groups") or [])[:ROWS]],
                        "No process survey yet."))
    parts.append(_table(t, "Largest processes",
                        [("Process", False), ("Origin", False), ("Project", False), ("Memory", True), ("PID", True)],
                        [[_e(p["name"]) + (f'<br><span class="mono">{_e(p["script"])}</span>' if p.get("script") else ""),
                          _e(p["origin"]), _project(p.get("project"), names), _size(p["rss_mb"], t), _e(p["pid"])]
                         for p in (procs.get("top") or [])[:ROWS]],
                        "No process survey yet."))
    parts.append(t.mark("Why one of them runs: an agent asks observatory_machine with explainPid.",
                        tag="p", attrs=' class="machine-hint"'))
    parts.append(_table(t, "Memory by project",
                        [("Project", False), ("Processes", True), ("Memory", True)],
                        [[_project(p["project"], names), _e(p["processes"]), _size(p["rss_mb"], t)] for p in (procs.get("projects") or [])[:ROWS]],
                        # A sample that never split processes by project (the light memory snapshot,
                        # `projects_measured: False`) is unmeasured, never "none" (OSS-21).
                        "No process runs inside a project folder."
                        if m.get("processes") and procs.get("projects_measured") is not False else UNMEASURED))
    disk = m.get("disk") or {}
    parts.append(_table(t, "Where the disk goes",
                        [("Place", False), ("Kind", False), ("Size", True), ("How it comes back", False)],
                        [[_word(t, l["label"]) + f'<br><span class="mono">{_e(l["path"])}</span>', _word(t, l["kind"]),
                          _gigabytes(round(l["gb"], 1) if isinstance(l.get("gb"), (int, float)) else None, t)
                          + _frees(l, t),
                          _word(t, l.get("reclaim"))
                          + (f'<br><span class="mono">{_e(l["command"])}</span>' if l.get("command") else "")]
                         for l in disk.get("locations") or []],
                        "No disk location measured yet."))
    parts.append(disk_line(m, t))
    parts.append(simulators_html(m.get("guard"), t))
    git = m.get("git") or {}
    # A pass the deadline cut short carried some checkouts from an earlier one; their rows below
    # are that old, and they plan no automatic removal until a fresh pass (ENG-009).
    held = git.get("carried") or {}
    if git.get("partial") and held.get("checkouts"):
        parts.append(t.mark("partial survey: {n} checkouts carried from {date}", tag="p",
                            attrs=' class="machine-hint"', n=held["checkouts"],
                            date=str(held.get("measured_at") or "")[:10]))
    # Worth a look: a stale record, one git cannot read, or one nobody has touched
    # in a week. Uncommitted edits in a worktree someone works in today are work.
    idle = [w for w in git.get("worktrees") or []
            if w.get("state") in ("missing", "unreadable") or (w.get("idle_days") or 0) >= 7]
    parts.append(_table(t, "Worktrees to look at",
                        [("Repository", False), ("Worktree", False), ("Branch", False), ("State", False), ("Idle days", True)],
                        [[_e(w["repository"].split(":", 1)[-1]), f'<span class="mono">{_e(w["path"])}</span>',
                          _e(w.get("branch") or ""), _word(t, w["state"]) + (" · " + t.mark("in use") if w.get("busy") else ""),
                          _e(w.get("idle_days", ""))]
                         for w in sorted(idle, key=lambda w: -(w.get("idle_days") or 0))[:ROWS]],
                        "Every worktree is clean and in recent use." if git else UNMEASURED_GIT))
    parts.append(_table(t, "Branches holding the only copy",
                        [("Repository", False), ("Branch", False), ("Commits", True), ("Idle days", True)],
                        [[_e(b["repository"].split(":", 1)[-1]), _e(b["name"]), _e(b.get("ahead")), _e(b.get("idle_days"))]
                         for b in sorted(git.get("uniqueBranches") or [], key=lambda b: -(b.get("idle_days") or 0))[:ROWS]],
                        "No branch holds commits found nowhere else." if git else UNMEASURED_GIT))
    degraded = m.get("degraded") or []
    if degraded:
        # A reason with a `code` is a message id, so the Russian page reads Russian;
        # one without (a collector's own words) is translated when the catalog
        # holds it and shown as written otherwise (L10N-04). The command a survey's
        # reason names (machine_view._load) is for agents (OBS-629).
        items = "".join(f'<li><span class="mono">{_e(d.get("source"))}</span>: {reason_html(d, t)}</li>'
                        for d in degraded)
        parts.append(f'<section class="card panel">{t.mark("Not measured", tag="h2", attrs=HEADING)}<ul>{items}</ul></section>')
    parts.append("</section>")
    return "".join(parts)


#: Values the page translates through `t(...)` with a variable argument — named
#: here so the catalog test can see them (see DYNAMIC_IDS in tests/test_i18n.py).
DYNAMIC = ("cache", "toolchain", "build", "simulator", "vm", "worktrees", "user", "swap", "memory",
           "regenerable", "command", "history", "manual", "auto", "clean", "dirty", "missing", "unreadable",
           "branch-merged", "branch-pushed", "worktree-missing", "worktree-clean", "build-artifacts",
           "branch-unique", "worktree-dirty", "removed", "pruned", "skipped", "failed",
           # The manual tier's proposals (tools/cleanup.py, OBS-621).
           "repo-idle", "deps-stale", "checkout-merged",
           # The disk guard's classes and pressure levels (tools/disk_guard.py).
           "cache-command", "cache-dir", "build-output", "user-temp", "temp", "trash", "browser-clone", "normal", "low", "critical",
           # Empty states chosen by whether their survey ran (see UNMEASURED).
           UNMEASURED, UNMEASURED_GIT, "Every worktree is clean and in recent use.", "No branch holds commits found nowhere else.",
           "Nothing to clean.", "No process runs inside a project folder.")


def _location_labels() -> tuple[str, ...]:
    """The disk locations' names, from the shipped defaults, so the Russian page
    reads them in Russian (audit A42). A label a person added to their own
    configuration is not in the catalog and is shown as written."""
    try:
        doc = json.loads((pathlib.Path(__file__).resolve().parent.parent / "defaults" / "machine.json")
                         .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    found = []
    def walk(x):
        if isinstance(x, dict):
            if isinstance(x.get("label"), str):
                found.append(x["label"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(doc)
    return tuple(found)


#: The swap row's label, written by collectors/scan_machine.py itself.
SWAP_LABEL = "Swap files (memory written to disk)"
#: A VM disk file's row, which `machine_view.vm_location` adds from the disk guard's pass (OBS-620).
VM_DISK_LABEL = "Lima or Colima virtual machine disk"
DYNAMIC = DYNAMIC + (SWAP_LABEL, VM_DISK_LABEL) + _location_labels()
