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
from machine_page import (EMPTY, GUARD_CLASSES, HEADING, NUM, ROWS, _e, _gigabytes, _table, _word, stamp)

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
    parts.append(guard_html(m.get("guard"), t))
    parts.append(_runs(cleanup, t))
    parts.append(_classes(cleanup, values, t))
    parts.append(_journal(cleanup, t))
    parts.append(settings_html(listing, t))
    parts.append(_degraded(m, listing, t))
    parts.append("</section>")
    return "".join(parts)


#: Values the page translates through a variable, named here so the catalog test sees them.
DYNAMIC = (tuple(SETTING_LABELS.values()) + tuple(c[1] for c in CLASSES)
           + ("Turn on", "Turn off", "Nothing to free at this pressure.", "No file above the size line.",
              "incomplete measurement", "Not measured", DISABLED))
