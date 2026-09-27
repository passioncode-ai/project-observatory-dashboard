"""The Machine page: what runs, where memory and disk go, what was cleaned.

Rendered here, at build time, from `machine_view.summary()` — the page reads and
never acts (OSS scenarios: no administrative mutation from the dashboard). Every
visible string is an English message id marked for the page script, so the
EN/RU switch re-translates this page like the others. Tables reuse the
dashboard's own table, card and narrow-screen rules; nothing here adds a style.
"""
from __future__ import annotations

import html

from i18n import Translator

ROWS = 20
#: Attributes built outside the f-strings: Python 3.11 allows no backslash and no
#: reused quote inside an f-string expression, and the engine supports 3.11.
NUM = ' class="num"'
EMPTY = ' class="empty"'
HEADING = ' class="machine-h"'


def _e(v) -> str:
    return html.escape("" if v is None else str(v))


def _gb(mb) -> str:
    return "—" if mb is None else f"{mb / 1024:.1f} GB"


def _table(t: Translator, caption: str, heads: list[tuple[str, bool]], rows: list[list[str]], empty: str) -> str:
    """One card: a heading, a table with labelled cells (narrow screens stack them)."""
    head = t.mark(caption, tag="h2", attrs=HEADING)
    if not rows:
        return f'<section class="card panel">{head}{t.mark(empty, tag="p", attrs=EMPTY)}</section>'
    th = "".join(f'<th scope="col"{NUM if num else ""}>{t.mark(h)}</th>' for h, num in heads)
    body = []
    for r in rows:
        cells = "".join(f'<td{NUM if num else ""} data-label="{_e(t(h))}">{c}</td>'
                        for (h, num), c in zip(heads, r))
        body.append(f"<tr>{cells}</tr>")
    return (f'<section class="card panel">{head}<table><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></section>')


def summary_html(m: dict, t: Translator) -> str:
    mem, disk = m.get("memory") or {}, (m.get("disk") or {}).get("volume") or {}
    procs = m.get("processes") or {}
    cleanup = m.get("cleanup") or {}
    done = [r for r in cleanup.get("journal") or [] if r.get("result") in ("removed", "pruned")]
    freed = sum(r.get("kb") or 0 for r in done) / 1048576
    used = None
    if mem.get("total_mb") is not None and mem.get("free_mb") is not None:
        used = mem["total_mb"] - mem["free_mb"] - (mem.get("inactive_mb") or 0)
    auto = t.mark("auto cleanup on") if cleanup.get("autoEnabled") else t.mark("auto cleanup off")
    tiles = [
        (t.mark("Memory in use"), f"{_gb(used)} / {_gb(mem.get('total_mb'))}",
         t.mark("swap {size}", size=_gb(mem.get("swap_used_mb")))),
        (t.mark("Free disk"), f"{disk.get('free_gb', '—')} GB",
         t.mark("{percent}% of {total} GB", percent=disk.get("free_percent", "—"), total=disk.get("total_gb", "—"))),
        (t.mark("Processes"), _e(procs.get("count", "—")),
         t.mark("{n} origins", n=len(procs.get("groups") or []))),
        (t.mark("Cleaned in 7 days"), _e(len(done)), auto + (f" · {freed:.1f} GB" if freed else "")),
    ]
    cells = "".join(f'<div class="tile">{k}<b>{_e(v)}</b>{sub}</div>' for k, v, sub in tiles)
    return f'<div class="tiles machine-tiles">{cells}</div>'


def machine_html(payload: dict, t: Translator | None = None) -> str:
    t = t or Translator()
    m = payload.get("machine") or {}
    parts = [f'<section id="machine" class="machine">']
    if m.get("measuredAt"):
        parts.append(t.mark("Machine surveyed {at}", tag="p", attrs=' class="machine-at"', at=m["measuredAt"]))
    parts.append(summary_html(m, t))
    procs = m.get("processes") or {}
    parts.append(_table(t, "Memory by origin",
                        [("Origin", False), ("Processes", True), ("Sessions", True), ("Memory", True), ("CPU %", True)],
                        [[_e(g["origin"]), _e(g["processes"]), _e(g["sessions"]), _gb(g["rss_mb"]), _e(g["cpu"])]
                         for g in (procs.get("groups") or [])[:ROWS]],
                        "No process survey yet."))
    parts.append(_table(t, "Largest processes",
                        [("Process", False), ("Origin", False), ("Project", False), ("Memory", True), ("PID", True)],
                        [[_e(p["name"]) + (f'<br><span class="mono">{_e(p["script"])}</span>' if p.get("script") else ""),
                          _e(p["origin"]), _e(p.get("project", "")), _gb(p["rss_mb"]), _e(p["pid"])]
                         for p in (procs.get("top") or [])[:ROWS]],
                        "No process survey yet."))
    parts.append(t.mark("Why one of them runs: project-observatory full machine --explain PID",
                        tag="p", attrs=' class="machine-hint"'))
    parts.append(_table(t, "Memory by project",
                        [("Project", False), ("Processes", True), ("Memory", True)],
                        [[_e(p["project"]), _e(p["processes"]), _gb(p["rss_mb"])] for p in (procs.get("projects") or [])[:ROWS]],
                        "No process runs inside a project folder."))
    disk = m.get("disk") or {}
    parts.append(_table(t, "Where the disk goes",
                        [("Place", False), ("Kind", False), ("Size", True), ("How it comes back", False)],
                        [[_e(l["label"]) + f'<br><span class="mono">{_e(l["path"])}</span>', _e(t(l["kind"])),
                          f"{l['gb']:.1f} GB",
                          _e(t(l.get("reclaim", "")) if l.get("reclaim") else "")
                          + (f'<br><span class="mono">{_e(l["command"])}</span>' if l.get("command") else "")]
                         for l in disk.get("locations") or []],
                        "No disk location measured yet."))
    git = m.get("git") or {}
    # Worth a look: a stale record, one git cannot read, or one nobody has touched
    # in a week. Uncommitted edits in a worktree someone works in today are work.
    idle = [w for w in git.get("worktrees") or []
            if w.get("state") in ("missing", "unreadable") or (w.get("idle_days") or 0) >= 7]
    parts.append(_table(t, "Worktrees to look at",
                        [("Repository", False), ("Worktree", False), ("Branch", False), ("State", False), ("Idle days", True)],
                        [[_e(w["repository"].split(":", 1)[-1]), f'<span class="mono">{_e(w["path"])}</span>',
                          _e(w.get("branch") or ""), _e(t(w["state"])) + (" · " + _e(t("in use")) if w.get("busy") else ""),
                          _e(w.get("idle_days", ""))]
                         for w in sorted(idle, key=lambda w: -(w.get("idle_days") or 0))[:ROWS]],
                        "Every worktree is clean and in recent use."))
    parts.append(_table(t, "Branches holding the only copy",
                        [("Repository", False), ("Branch", False), ("Commits", True), ("Idle days", True)],
                        [[_e(b["repository"].split(":", 1)[-1]), _e(b["name"]), _e(b.get("ahead")), _e(b.get("idle_days"))]
                         for b in sorted(git.get("uniqueBranches") or [], key=lambda b: -(b.get("idle_days") or 0))[:ROWS]],
                        "No branch holds commits found nowhere else."))
    cleanup = m.get("cleanup") or {}
    counts = cleanup.get("counts") or {}
    parts.append(_table(t, "Cleanup plan",
                        [("What", False), ("Tier", False), ("Count", True)],
                        [[_e(t(k)), _e(t("auto") if k in ("branch-merged", "branch-pushed", "worktree-missing",
                                                          "worktree-clean", "build-artifacts") else t("manual")), _e(v)]
                         for k, v in counts.items() if v],
                        "Nothing to clean."))
    parts.append(t.mark("The auto tier loses nothing and runs on the tick when features.auto_cleanup is on; "
                        "the manual tier archives first: project-observatory full cleanup --apply --include manual",
                        tag="p", attrs=' class="machine-hint"'))
    parts.append(_table(t, "Cleaned in the last 7 days",
                        [("When", False), ("What", False), ("Item", False), ("Result", False)],
                        [[_e(r.get("at")), _e(t(r.get("class", ""))), f'<span class="mono">{_e(r.get("target"))}</span>',
                          _e(t(r.get("result", ""))) + (f' — {_e(r["reason"])}' if r.get("reason") else "")]
                         for r in (cleanup.get("journal") or [])[:ROWS]],
                        "Nothing was cleaned in the last 7 days."))
    degraded = m.get("degraded") or []
    if degraded:
        items = "".join(f'<li><span class="mono">{_e(d.get("source"))}</span> — {_e(d.get("reason"))}</li>' for d in degraded)
        parts.append(f'<section class="card panel">{t.mark("Not measured", tag="h2", attrs=HEADING)}<ul>{items}</ul></section>')
    parts.append("</section>")
    return "".join(parts)


#: Values the page translates through `t(...)` with a variable argument — named
#: here so the catalog test can see them (see DYNAMIC_IDS in tests/test_i18n.py).
DYNAMIC = ("cache", "toolchain", "build", "simulator", "vm", "worktrees", "user", "swap", "memory",
           "regenerable", "command", "history", "manual", "auto", "clean", "dirty", "missing", "unreadable",
           "branch-merged", "branch-pushed", "worktree-missing", "worktree-clean", "build-artifacts",
           "branch-unique", "worktree-dirty", "removed", "pruned", "skipped", "failed")
