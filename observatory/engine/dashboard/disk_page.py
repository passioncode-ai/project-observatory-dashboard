"""The Disk page (OBS-562, CP-16): how much space is free, what the disk guard and auto cleanup removed and keep,
and how they are set.

Rendered here, at build time, from the machine survey (`machine_view.summary()`) and the configuration door's
listing of the guard and cleanup keys (`configuration_door.listing()`, filtered by `settings_of`). The guard's last
pass, its largest files and the folders it keeps moved here from the Machine page with the cleanup tables; the
Machine page keeps one line and a link.

THE SETTINGS CHANGE ONLY THROUGH THE DOOR. Each control is built disabled. Served by the local control server (the
key server, which writes its token into the page), the page script enables them: a control posts to the server's
`configure` action, which asks the configuration door for a plan; the page shows the plan; Apply writes it as the
person at this page (`person:page`) and journals it; Undo restores it by the journal id. Opened as a file there is no
server to ask, so the controls stay disabled and say why, naming the agent's tool (`observatory_configure`) in their
tooltip. No command line is shown on a row.

Every visible string is an English message id marked for the page script, like the Machine page (L10N-01, L10N-05).
"""
from __future__ import annotations

from i18n import Translator
from machine_page import EMPTY, GUARD_CLASSES, HEADING, NUM, ROWS, _gigabytes, _table, _word
from page_util import escape as _e, stamp

HINT = ' class="machine-hint"'
#: A value nobody measured (an en dash, as a table's empty cell).
NONE = "\u2013"
#: The tool an agent changes these settings with; named in each disabled control's tooltip.
TOOL = "observatory_configure"
#: The keys this page shows (configuration_door.DISK_SCOPE): kept here as well so the page builds without the door.
SCOPE = ("features.auto_cleanup", "guard.", "cleanup.auto.")
#: The machine survey's sources this page reads; a gap in another one belongs to the Machine page.
SOURCES = ("machine.json", "disk-guard.json", "cleanup-plan.json")

#: Each setting's name for a reader. A key the door declares and this table lacks is shown by its key, and the page
#: test fails, so a new guard or cleanup key gets its words the day it ships.
SETTING_LABELS = {
    "features.auto_cleanup": "Auto cleanup",
    "guard.levels.low.free_gb": "Low space line, GB",
    "guard.levels.critical.free_gb": "Critical space line, GB",
    "guard.temp_days.normal": "Temporary files kept, days (normal)",
    "guard.temp_days.low": "Temporary files kept, days (low space)",
    "guard.temp_days.critical": "Temporary files kept, days (critical)",
    "guard.build_idle_days.normal": "Build output of an idle checkout kept, days (normal)",
    "guard.build_idle_days.low": "Build output of an idle checkout kept, days (low space)",
    "guard.build_idle_days.critical": "Build output of an idle checkout kept, days (critical)",
    "guard.trash_days.normal": "Trash kept, days (normal)",
    "guard.trash_days.low": "Trash kept, days (low space)",
    "guard.trash_days.critical": "Trash kept, days (critical)",
    "guard.clone_min_age_hours": "Abandoned browser clones kept, hours",
    "guard.user_temp_hours": "Entries of the temporary folder kept, hours",
    "guard.tmp_growth.entries_per_day": "Temporary folder growth worth a finding, new entries a day",
    "guard.tmp_growth.gb_per_day": "Temporary folder growth worth a finding, GB a day",
    "guard.vm_growth.gb_per_day": "Virtual machine disk growth worth a finding, GB a day",
    "guard.swap_growth.gb_per_day": "Swap growth worth a finding, GB a day",
    "cleanup.auto.merged_branch_idle_days": "Merged branches kept, idle days",
    "cleanup.auto.pushed_branch_idle_days": "Pushed branches kept, idle days",
    "cleanup.auto.clean_worktree_idle_days": "Clean worktrees kept, idle days",
    "cleanup.auto.build_artifacts_project_idle_days": "Build output of an idle project kept, days",
}
#: The cleanup's classes in a reader's words, with the tier and the setting that holds their idle days.
CLASSES = (
    ("branch-merged", "merged branches", "auto", "cleanup.auto.merged_branch_idle_days"),
    ("branch-pushed", "pushed branches", "auto", "cleanup.auto.pushed_branch_idle_days"),
    ("worktree-clean", "clean worktrees", "auto", "cleanup.auto.clean_worktree_idle_days"),
    ("build-artifacts", "build output of idle projects", "auto", "cleanup.auto.build_artifacts_project_idle_days"),
    ("worktree-missing", "worktree records whose folder is gone", "auto", None),
    ("branch-unique", "branches holding the only copy", "kept", None),
    ("worktree-dirty", "uncommitted worktrees", "kept", None),
)
DISABLED = "This page only shows the settings: it was opened as a file or without the local control server. An agent changes them with {tool}."


def in_scope(key) -> bool:
    return isinstance(key, str) and any(key == p or (p.endswith(".") and key.startswith(p)) for p in SCOPE)


def settings_of(listing: dict) -> dict:
    """The door's listing, cut to the keys this page shows, with its degraded sources."""
    return {"settings": [r for r in listing.get("settings") or [] if isinstance(r, dict) and in_scope(r.get("key"))],
            "degraded": list(listing.get("degraded") or [])}


def _switch(t: Translator, value) -> str:
    if value is True:
        return t.mark("switch@@on")
    if value is False:
        return t.mark("switch@@off")
    return t.number(value) if isinstance(value, (int, float)) else NONE


def _tiles(m: dict, auto, t: Translator) -> str:
    volume = (m.get("disk") or {}).get("volume") or {}
    guard = m.get("guard") or {}
    cleanup = m.get("cleanup") or {}
    totals = cleanup.get("guardTotals") or {}
    total = volume.get("total_gb")
    free_sub = (t.mark("{percent}% of {total} GB", percent=volume.get("free_percent", NONE), total=total)
                if isinstance(total, (int, float)) else t.mark("not surveyed"))
    if guard.get("at"):
        last, last_sub = t.date(str(guard.get("at"))[:10]), t.mark("free {before} → {after} GB",
                                                                  before=guard.get("freeGbBefore", NONE),
                                                                  after=guard.get("freeGbAfter", NONE))
    else:
        last, last_sub = NONE, t.mark("has not run yet")
    freed = (totals.get("freedMb") or 0) / 1024
    tiles = [(t.mark("Free disk"), _gigabytes(volume.get("free_gb"), t), free_sub),
             (t.mark("Last guard pass"), last, last_sub),
             (t.mark("Auto cleanup"), _switch(t, auto),
              t.mark("removed by the guard in 7 days: {n}", n=totals.get("removed") or 0)),
             (t.mark("Freed in 7 days"), _gigabytes(round(freed, 1), t), t.mark("by the disk's own count"))]
    cells = "".join(f'<div class="tile">{k}<b>{v}</b>{sub}</div>' for k, v, sub in tiles)
    return f'<div class="tiles machine-tiles">{cells}</div>'


#: The days the table lists; the line over them holds the whole series.
TREND_ROWS = 7
SPARK_W, SPARK_H, SPARK_PAD = 240, 48, 4


def _spark(rows: list[dict], t: Translator) -> str:
    """The lowest free space of each day as one line, zero at the bottom. Drawn only from two days on."""
    values = [r["min_gb"] for r in rows]
    top = max(values) or 1.0
    step = (SPARK_W - 2 * SPARK_PAD) / (len(values) - 1)
    points = " ".join(f"{SPARK_PAD + i * step:.1f},{SPARK_H - SPARK_PAD - v / top * (SPARK_H - 2 * SPARK_PAD):.1f}"
                      for i, v in enumerate(values))
    return (f'<svg class="free-spark" viewBox="0 0 {SPARK_W} {SPARK_H}" role="img" preserveAspectRatio="none"'
            f'{t.attr("aria-label", "Lowest free space of each day, {n} days", n=len(values))}>'
            f'<polyline points="{points}"/></svg>')


def trend_html(series, t: Translator) -> str:
    """The free space of the days before the last pass (OBS-572): a short line and the newest days as a table, kept
    by the guard (`tools/disk_free_series.py`). No series says so, and a single day draws no line."""
    rows = [r for r in series or [] if isinstance(r, dict) and r.get("day") and isinstance(r.get("min_gb"), (int, float))
            and isinstance(r.get("last_gb"), (int, float))]
    head = t.mark("Free space by day", tag="h2", attrs=HEADING)
    if not rows:
        return (f'<section class="card panel">{head}'
                f'{t.mark("No daily history yet. The guard records the free space each time it runs.", tag="p", attrs=EMPTY)}'
                "</section>")
    low = min(rows, key=lambda r: r["min_gb"])
    parts = [f'<section class="card panel">{head}']
    if len(rows) > 1:
        parts.append(_spark(rows, t))
        parts.append(t.mark("Lowest {low} GB on {date}, newest day {last} GB.", tag="p", attrs=HINT,
                            low=low["min_gb"], date=low["day"], last=rows[-1]["last_gb"]))
    table = [[t.date(r["day"]), _gigabytes(r["min_gb"], t), _gigabytes(r["last_gb"], t), t.number(r.get("samples") or 1)]
             for r in reversed(rows[-TREND_ROWS:])]
    heads = [("Day", False), ("Lowest", True), ("Last reading", True), ("Readings", True)]
    th = "".join(f'<th scope="col"{NUM if num else ""}>{t.mark(h)}</th>' for h, num in heads)
    body = "".join("<tr>" + "".join(f'<td{NUM if num else ""}{t.attr("data-label", h)}>{c}</td>'
                                    for (h, num), c in zip(heads, r)) + "</tr>" for r in table)
    parts.append(f'<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></section>')
    return "".join(parts)


def guard_html(g: dict | None, t: Translator) -> str:
    """The disk guard's last pass, what it planned at that pressure, the folders it keeps and the largest files."""
    if not g:
        return (f'<section class="card panel">{t.mark("Disk guard", tag="h2", attrs=HEADING)}'
                f'{t.mark("The disk guard has not run yet.", tag="p", attrs=EMPTY)}'
                f'{t.mark("It runs from the maintenance job when auto cleanup is on.", tag="p", attrs=HINT)}'
                "</section>")
    results, planned = g.get("results") or {}, g.get("planned") or {}
    ran = t.mark("Disk guard ran {at}: free {before} → {after} GB, {n} removed", at=stamp(g.get("at")),
                 before=g.get("freeGbBefore", NONE), after=g.get("freeGbAfter", NONE), n=results.get("removed", 0))
    parts = [f'<p class="machine-at">{ran} · {t.mark("pressure")} {_word(t, g.get("level"))}</p>']
    parts.append(_table(t, "Disk guard", [("What", False), ("Planned", True)],
                        [[_word(t, c), t.number(planned[c])] for c in GUARD_CLASSES if planned.get(c)],
                        "Nothing to free at this pressure."))
    kept = [k for k in g.get("kept") or [] if isinstance(k, dict) and k.get("target")][:ROWS]
    if kept:
        # Live state an agent service keeps under a cleanup root (OBS-203): shown, so a person
        # sees why a large folder there is never freed.
        parts.append(_table(t, "Kept by the guard", [("Folder", False), ("Why kept", False)],
                            [[f'<span class="mono">{_e(k["target"])}</span>', t.known(k.get("reason"))]
                             for k in kept], ""))
    parts.append(t.mark("From the maintenance job when auto cleanup is on: hourly while space is normal, every "
                        "15 minutes below the low line; never anything a process holds, maps, runs from or works "
                        "inside.", tag="p", attrs=' class="machine-hint"'))
    coverage = g.get("largeFiles") or {}
    files = (coverage.get("files") or [])[:ROWS]
    measured = coverage.get("complete") is True
    empty = "No file above the size line." if measured else "incomplete measurement" if coverage else "Not measured"
    if coverage and not measured:
        errors = coverage.get("errors")
        count = (sum(n for n in errors.values() if isinstance(n, int) and n > 0)
                 if isinstance(errors, dict) else None)
        shown = t.number(count) if count is not None else t.mark("Not measured")
        parts.append(f'<p class="machine-hint">{t.mark("incomplete measurement")} · '
                     f'{_e(stamp(coverage.get("measured_at")))} · {t.mark("unreadable")}: {shown}</p>')
    parts.append(_table(t, "Largest files",
                        [("File", False), ("Size", True), ("Modified", False)],
                        [[f'<span class="mono">{_e(f["path"])}</span>', _gigabytes(f.get("gb"), t),
                          t.date(f.get("modified"))] for f in files],
                        empty))
    return "".join(parts)


def _classes(cleanup: dict | None, values: dict, t: Translator) -> str:
    counts = (cleanup or {}).get("counts") or {}
    rows = []
    for cls, label, tier, key in CLASSES:
        days = values.get(key) if key else None
        if tier == "kept":
            keep = t.mark("never removed by itself; archived first, removed by hand")
        elif isinstance(days, int) and not isinstance(days, bool):
            keep = t.mark("{n} days", n=days)
        elif key:
            keep = NONE
        else:
            keep = t.mark("until the next pass")
        rows.append([t.mark(label), t.mark("auto") if tier == "auto" else t.mark("kept"),
                     t.number(counts.get(cls, 0)) if cleanup else NONE, keep])
    out = _table(t, "What auto cleanup removes and keeps",
                 [("What", False), ("Tier", False), ("Now", True), ("Kept for", False)], rows, "")
    return out + t.mark("Under disk pressure the guard lowers these days. A branch or worktree that holds the only copy "
                        "of someone's work is never removed by itself.", tag="p", attrs=' class="machine-hint"')


#: Why a VM's volumes could not be listed (disk_guard.vm_unused_volumes), in a reader's words.
VOLUME_UNREAD = {
    "docker-unanswered": "its Docker engine did not answer",
    "no-docker-cli": "no docker command is installed here",
    "vm-list-unknown": "the virtual machines could not be listed",
    "timeout": "the guard's time ran out",
}
VOLUME_HINT = ("Docker volumes no container uses, inside this machine's Lima or Colima virtual machines. A volume "
               "holds data, so the guard never removes a volume: their owner deletes the ones not needed.")


def volumes_html(g: dict | None, t: Translator) -> str:
    """The manual tier's unused Docker volumes (OBS-584): per VM each volume no container uses, the space it takes and
    the owner its labels name, largest first. Listed for the owner to delete; no command is printed on a row."""
    head = t.mark("Unused Docker volumes", tag="h2", attrs=HEADING)
    hint = t.mark(VOLUME_HINT, tag="p", attrs=HINT)
    found = (g or {}).get("vmVolumes")
    if not isinstance(found, dict):
        return (f'<section class="card panel" id="disk-volumes">{head}{hint}'
                f'{t.mark("Not listed yet. The guard lists them when it acts.", tag="p", attrs=EMPTY)}</section>')
    rows = [v for v in found.get("volumes") or [] if isinstance(v, dict) and v.get("name")][:ROWS]
    parts = [f'<section class="card panel" id="disk-volumes">{head}{hint}']
    if found.get("at"):
        parts.append(t.mark("Listed {at}", tag="p", attrs=' class="machine-at"', at=stamp(found["at"])))
    if rows:
        heads = [("Virtual machine", False), ("Volume", False), ("Owner", False), ("Size", True)]
        th = "".join(f'<th scope="col"{NUM if num else ""}>{t.mark(h)}</th>' for h, num in heads)
        body = []
        for v in rows:
            size = v.get("bytes")
            cells = [_e(v.get("instance") or NONE), f'<span class="mono">{_e(v["name"])}</span>',
                     _e(v["owner"]) if v.get("owner") else t.mark("no owner label"),
                     _gigabytes(round(size / 1e9, 2), t) if isinstance(size, int) and not isinstance(size, bool)
                     else NONE]
            body.append("<tr>" + "".join(f'<td{NUM if num else ""}{t.attr("data-label", h)}>{c}</td>'
                                         for (h, num), c in zip(heads, cells)) + "</tr>")
        parts.append(f'<table><thead><tr>{th}</tr></thead><tbody>{"".join(body)}</tbody></table>')
    else:
        parts.append(t.mark("No unused Docker volume.", tag="p", attrs=EMPTY))
    unread = [u for u in found.get("unread") or [] if isinstance(u, dict)]
    if unread:
        items = "".join(f'<li>{_e(u.get("instance") or NONE)} · '
                        f'{t.mark(VOLUME_UNREAD.get(u.get("reason_code"), "unreadable"))}</li>' for u in unread)
        parts.append(f'{t.mark("Not listed", tag="p", attrs=HINT)}<ul>{items}</ul>')
    parts.append("</section>")
    return "".join(parts)


#: The temporary folder's classes shown (busiest first).
TEMP_ROWS = 5
TEMP_HINT = ("The guard removes its entries older than {hours} hours that no process holds open; sockets, links, the "
             "system's own folders and another account's entries stay. A name class arriving faster than the setting "
             "below is a finding naming the program writing there.")


def temp_html(g: dict | None, t: Translator) -> str:
    """The user's temporary folder (OBS-585): its size, the name classes arriving fastest with the program writing
    there, and what the guard removed last. No command line on a row."""
    head = t.mark("Temporary folder", tag="h2", attrs=HEADING)
    temp = (g or {}).get("userTemp")
    if not isinstance(temp, dict) or not isinstance(temp.get("entries"), int):
        return (f'<section class="card panel" id="disk-temp">{head}'
                f'{t.mark("Not sampled yet. The guard samples it on every pass.", tag="p", attrs=EMPTY)}</section>')
    hours = temp.get("hours")
    parts = [f'<section class="card panel" id="disk-temp">{head}',
             t.mark("{entries} entries, {gb} GB, sampled {at}", tag="p", attrs=' class="machine-at"',
                    entries=temp["entries"], gb=temp.get("gb") if temp.get("gb") is not None else NONE,
                    at=stamp(temp.get("at"))),
             t.mark(TEMP_HINT, tag="p", attrs=HINT, hours=hours if isinstance(hours, (int, float)) else NONE)]
    growing = {gr.get("prefix"): gr for gr in temp.get("growing") or [] if isinstance(gr, dict)}
    if temp.get("window_hours") is None:
        parts.append(t.mark("Growth is measured from the next pass.", tag="p", attrs=HINT))
    classes = [c for c in temp.get("classes") or [] if isinstance(c, dict) and c.get("prefix")][:TEMP_ROWS]
    if classes:
        heads = [("Names", False), ("Entries", True), ("Size", True), ("New a day", True), ("Growth", False)]
        th = "".join(f'<th scope="col"{NUM if num else ""}>{t.mark(h)}</th>' for h, num in heads)
        body = []
        for c in classes:
            rate = c.get("entries_per_day")
            writers = (growing.get(c["prefix"]) or {}).get("writers") or []
            state = NONE
            if c["prefix"] in growing:
                state = t.mark("growing fast") + "".join(f' · <span class="mono">{_e(w)}</span>' for w in writers)
            cells = [f'<span class="mono">{_e(c["prefix"])}</span>', t.number(c.get("entries") or 0),
                     _gigabytes(c.get("gb"), t), t.number(rate) if isinstance(rate, int) else NONE, state]
            body.append("<tr>" + "".join(f'<td{NUM if num else ""}{t.attr("data-label", h)}>{cell}</td>'
                                         for (h, num), cell in zip(heads, cells)) + "</tr>")
        parts.append(f'<table><thead><tr>{th}</tr></thead><tbody>{"".join(body)}</tbody></table>')
    else:
        parts.append(t.mark("The folder is empty.", tag="p", attrs=EMPTY))
    last = temp.get("last_removed")
    if isinstance(last, dict) and isinstance(last.get("removed"), int) and last["removed"]:
        parts.append(t.mark("Last removed {at}: {count} entries, {gb} GB by the disk's own count", tag="p", attrs=HINT,
                            at=stamp(last.get("at")), count=last["removed"],
                            gb=last.get("freed_gb") if last.get("freed_gb") is not None else NONE))
    else:
        parts.append(t.mark("The guard has removed nothing here yet.", tag="p", attrs=HINT))
    parts.append("</section>")
    return "".join(parts)


VM_HINT = ("Each Lima or Colima virtual machine keeps its disk in a file on this disk; the file takes the space the "
           "machine wrote, and a log or database growing inside it shows here first. Observatory never changes a "
           "virtual machine: a disk growing faster than the setting below is a finding for its owner.")


def _growth(growth, t: Translator) -> str:
    if not isinstance(growth, dict) or not isinstance(growth.get("gb"), (int, float)):
        return t.mark("measured from the next day")
    return t.mark("{gb} GB in {hours} h", gb=growth["gb"], hours=growth.get("window_hours", NONE))


def vms_html(g: dict | None, t: Translator) -> str:
    """Each Lima or Colima VM disk file with the space it takes and its growth over the last day, and the swap on the
    same disk with the agent sessions holding the memory it writes (OBS-620). Nothing here acts on a VM."""
    head = t.mark("Virtual machine disks", tag="h2", attrs=HEADING)
    hint = t.mark(VM_HINT, tag="p", attrs=HINT)
    vms = (g or {}).get("vmDisks")
    parts = [f'<section class="card panel" id="disk-vms">{head}{hint}']
    if not isinstance(vms, list):
        parts.append(t.mark("Not measured yet. The guard measures them on every pass.", tag="p", attrs=EMPTY))
    elif not [v for v in vms if isinstance(v, dict)]:
        parts.append(t.mark("No Lima or Colima virtual machine disk on this machine.", tag="p", attrs=EMPTY))
    else:
        heads = [("Virtual machine", False), ("File", False), ("Space taken", True), ("Last day", True),
                 ("Growth", False)]
        th = "".join(f'<th scope="col"{NUM if num else ""}>{t.mark(h)}</th>' for h, num in heads)
        body = []
        for v in [v for v in vms if isinstance(v, dict)][:ROWS]:
            cells = [_e(v.get("vm") or NONE) + f' <span class="machine-hint">{_e(v.get("tool") or "")}</span>',
                     f'<span class="mono">{_e(v.get("file") or NONE)}</span>',
                     _gigabytes(v.get("allocated_gb"), t), _growth(v.get("growth"), t),
                     t.mark("growing fast") if v.get("growing") is True else NONE]
            body.append("<tr>" + "".join(f'<td{NUM if num else ""}{t.attr("data-label", h)}>{c}</td>'
                                         for (h, num), c in zip(heads, cells)) + "</tr>")
        parts.append(f'<table><thead><tr>{th}</tr></thead><tbody>{"".join(body)}</tbody></table>')
    parts.append(_swap(g, t))
    parts.append("</section>")
    return "".join(parts)


def _swap(g: dict | None, t: Translator) -> str:
    swap = (g or {}).get("swap")
    if not isinstance(swap, dict):
        return ""
    if not isinstance(swap.get("gb"), (int, float)):
        return t.mark("Swap: not measured.", tag="p", attrs=HINT)
    where = {True: "on the same disk", False: "on another disk"}.get(swap.get("same_container"), "disk not known")
    line = (f'<p class="machine-at">{t.mark("Swap holds {gb} GB", gb=swap["gb"])} · {t.mark(where)} · '
            f'{t.mark("last day")} {_growth(swap.get("growth"), t)}</p>')
    if swap.get("growing") is True:
        line += t.mark("Swap is growing fast.", tag="p", attrs=HINT)
    owner = swap.get("owner") if isinstance(swap.get("owner"), dict) else {}
    sessions, rss = owner.get("sessions"), owner.get("rss_gb")
    agents = [a for a in owner.get("agents") or [] if isinstance(a, dict) and a.get("agent")]
    named = "".join(f' · <span class="mono">{_e(a["agent"])}</span> '
                    f'{t.number(a["sessions"]) if isinstance(a.get("sessions"), int) else NONE}' for a in agents[:5])
    if isinstance(sessions, int):
        held = t.mark("Held by {count} agent sessions, their processes holding {gb} GB of memory", count=sessions,
                      gb=rss if isinstance(rss, (int, float)) else NONE)
        line += f'<p class="machine-hint">{held}{named}</p>'
    else:
        line += t.mark("The agent sessions holding its memory were not read.", tag="p", attrs=HINT)
    return line


def _runs(cleanup: dict | None, t: Translator) -> str:
    runs = (cleanup or {}).get("guardRuns") or []
    return _table(t, "Guard passes in 7 days",
                  [("When", False), ("Pressure", False), ("Removed", True), ("Freed", True)],
                  [[t.date(stamp(r.get("at"))), _word(t, r.get("level")), t.number(r.get("removed") or 0),
                    _gigabytes(round(max(0, r.get("freed_mb") or 0) / 1024, 1), t)] for r in runs[:ROWS]],
                  "The guard removed nothing in the last 7 days.")


def _journal(cleanup: dict | None, t: Translator) -> str:
    return _table(t, "Cleaned in the last 7 days",
                  [("When", False), ("What", False), ("Item", False), ("Result", False)],
                  [[t.date(stamp(r.get("at"))), _word(t, r.get("class")),
                    f'<span class="mono">{_e(r.get("target"))}</span>',
                    _word(t, r.get("result")) + (" · " + t.known(r["reason"]) if r.get("reason") else "")]
                   for r in ((cleanup or {}).get("journal") or [])[:ROWS]],
                  "Nothing was cleaned in the last 7 days.")


def _control(row: dict, n: int, t: Translator) -> str:
    """A disabled control the page script enables when the local control server serves the page."""
    key, value, kind = row.get("key"), row.get("value"), row.get("type")
    tip = t.attr("title", DISABLED, tool=TOOL)
    if kind == "bool":
        turn = "Turn off" if value is True else "Turn on"
        return (f'<button type="button" class="chip-btn" data-cfg-key="{_e(key)}" '
                f'data-cfg-value="{"false" if value is True else "true"}" disabled{tip}>{t.mark(turn)}</button>')
    if kind in ("int", "number"):
        step = "1" if kind == "int" else "any"
        bounds = "".join(f' {name}="{_e(row[name[:3] + "imum"])}"' for name in ("min", "max")
                         if row.get(name[:3] + "imum") is not None)
        current = f' value="{_e(value)}"' if isinstance(value, (int, float)) and not isinstance(value, bool) else ""
        return (f'<input type="number" id="cfg-{n}" class="mono" data-cfg-input="{_e(key)}" step="{step}"{bounds}'
                f'{current} aria-labelledby="cfg-label-{n}" disabled{tip}> '
                f'<button type="button" class="chip-btn" data-cfg-key="{_e(key)}" data-cfg-from="cfg-{n}" '
                f'disabled{tip}>{t.mark("Preview")}</button>')
    return ""


def settings_html(listing: dict | None, t: Translator) -> str:
    rows = (listing or {}).get("settings") or []
    head = t.mark("Settings", tag="h2", attrs=HEADING)
    live = t.mark("This page was opened as a file, so it only shows the settings. Served by the local control "
                  "server, each change is previewed before it applies and can be undone.", tag="p",
                  attrs=' class="machine-hint" id="disk-live"' + t.attr("title", DISABLED, tool=TOOL))
    if not rows:
        return (f'<section class="card panel" id="disk-settings">{head}'
                f'{t.mark("The settings could not be read.", tag="p", attrs=EMPTY)}</section>')
    heads = (("Setting", False), ("Value", True), ("Default", True), ("Source", False), ("Change", False))
    th = "".join(f'<th scope="col"{NUM if num else ""}>{t.mark(h)}</th>' for h, num in heads)
    body = []
    for n, row in enumerate(rows):
        key = row.get("key")
        label = SETTING_LABELS.get(key)
        name = (t.mark(label, attrs=f' id="cfg-label-{n}"') if label
                else f'<span id="cfg-label-{n}" class="mono">{_e(key)}</span>')
        if row.get("value") is None and row.get("type") != "path":
            value, source = t.mark("unreadable"), t.mark("unreadable")
        else:
            value = _switch(t, row.get("value"))
            # A file holding the default (the copy `init` made) is the default, not a choice.
            chosen = row.get("set") and row.get("value") != row.get("default")
            source = t.mark("set in {file}", file=row.get("file")) if chosen else t.mark("default")
        cells = [name + f'<br><span class="mono machine-hint">{_e(key)}</span>', value,
                 _switch(t, row.get("default")), source, _control(row, n, t)]
        tds = "".join(f'<td{NUM if num else ""}{t.attr("data-label", h)}>{c}</td>'
                      for (h, num), c in zip(heads, cells))
        body.append(f'<tr data-cfg-row="{_e(key)}">{tds}</tr>')
    plan = (f'<div id="disk-plan" class="card panel" role="region" aria-live="polite"'
            f'{t.attr("aria-label", "Change preview")} hidden></div>')
    return (f'<section class="card panel" id="disk-settings">{head}{live}<table><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></section>{plan}')


def _degraded(m: dict, listing: dict | None, t: Translator) -> str:
    items = []
    for d in [d for d in m.get("degraded") or [] if isinstance(d, dict) and d.get("source") in SOURCES] \
            + list((listing or {}).get("degraded") or []):
        if d.get("code") == "not-surveyed":
            why = t.mark("not surveyed yet")
        elif d.get("code") == "unreadable":
            why = t.mark("unreadable")
        else:
            why = t.known(d.get("reason"))
        items.append(f'<li><span class="mono">{_e(d.get("source"))}</span> · {why}</li>')
    if not items:
        return ""
    return f'<section class="card panel">{t.mark("Not measured", tag="h2", attrs=HEADING)}<ul>{"".join(items)}</ul></section>'


def disk_html(payload: dict, t: Translator | None = None) -> str:
    t = t or Translator()
    m = payload.get("machine") or {}
    listing = m.get("diskSettings")
    values = {r.get("key"): r.get("value") for r in (listing or {}).get("settings") or [] if isinstance(r, dict)}
    cleanup = m.get("cleanup")
    auto = values.get("features.auto_cleanup")
    if not isinstance(auto, bool):
        auto = (cleanup or {}).get("autoEnabled")
    parts = ['<section id="disk" class="machine">']
    if m.get("measuredAt"):
        parts.append(t.mark("Machine surveyed {at}", tag="p", attrs=' class="machine-at"', at=stamp(m["measuredAt"])))
    parts.append(_tiles(m, auto, t))
    if auto is True:
        parts.append(t.mark("Auto cleanup is on: the guard and the auto tier run from the maintenance job.", tag="p",
                            attrs=' class="machine-at"'))
    elif auto is False:
        parts.append(t.mark("Auto cleanup is off: nothing is removed by itself. Turn it on under Settings.", tag="p",
                            attrs=' class="machine-at"'))
    parts.append(trend_html(m.get("freeSeries"), t))
    parts.append(guard_html(m.get("guard"), t))
    parts.append(_runs(cleanup, t))
    parts.append(_classes(cleanup, values, t))
    parts.append(temp_html(m.get("guard"), t))
    parts.append(vms_html(m.get("guard"), t))
    parts.append(volumes_html(m.get("guard"), t))
    parts.append(_journal(cleanup, t))
    parts.append(settings_html(listing, t))
    parts.append(_degraded(m, listing, t))
    parts.append("</section>")
    return "".join(parts)


#: Values the page translates through a variable, named here so the catalog test sees them.
DYNAMIC = (tuple(SETTING_LABELS.values()) + tuple(c[1] for c in CLASSES)
           + ("Day", "Lowest", "Last reading", "Readings",      # the trend table's heads (`trend_html`)
              "Setting", "Value", "Default", "Source", "Change",  # the settings table's heads (`settings_html`)
              "Turn on", "Turn off", "Nothing to free at this pressure.", "No file above the size line.",
              "incomplete measurement", "Not measured", DISABLED,
              "Virtual machine", "Volume", "Owner", "Size",       # the volumes table's heads (`volumes_html`)
              "unreadable", VOLUME_HINT,
              "Names", "Entries", "New a day", "Growth", TEMP_HINT,   # the temporary folder's section (`temp_html`)
              "File", "Space taken", "Last day", VM_HINT,             # the VM disks' section (`vms_html`, OBS-620)
              "on the same disk", "on another disk", "disk not known", "last day")
           + tuple(VOLUME_UNREAD.values()))
