"""The Installed tools page (OBS-552, CP-05): which coding agents, skills, plugins and hooks are installed here.

Rendered at build time from `tooling_view.build()`. The page reads and never acts: no button, so nothing here needs
an agent's twin (CP-18); an agent reads the same inventory with `observatory_machine(section="skills")`. One section
per coding agent: its version, then its plugins (they carry the updates; behind and finding rows first), its skills
collapsed per scope with their count (a scope opens by itself when one of its skills has a finding), its hooks. Each
harness finding sits beside the item it is about and links to its row on the Findings page. A scan that was not read,
or read in part, is named with its reason; a reason's command is cut, since a person types none (IS rules).
"""
from __future__ import annotations

import html
import re

from i18n import NO_VALUE, Translator

HEADING = ' class="machine-h"'
SUB = ' class="tools-h"'
EMPTY = ' class="empty"'
HINT = ' class="machine-hint"'
#: What an unknown cell shows: never a guess, never an em dash.
UNKNOWN = NO_VALUE
#: Severity to chip tone, as Home draws it.
TONE = {"critical": "danger", "warning": "warn", "info": ""}
#: The source tier the security scan weighs a skill by (docs/design/HARNESS.md H6).
TIERS = {1: "own code", 2: "pinned marketplace", 3: "third party"}
#: The agents whose plugins, skills and hooks the scan reads (tooling_view.READ_AGENTS).
READ_AGENTS = ("claude-code", "codex")


def _e(v) -> str:
    return html.escape("" if v is None else str(v))


def _stamp(value) -> str:
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.\d+)?Z?", str(value or ""))
    return f"{m.group(1)} {m.group(2)} UTC" if m else str(value or "")


def _anchor(fid) -> str:
    """The Findings page's row anchor, as `renderFindings` writes it."""
    return "f-" + re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(fid))


def _finding(f: dict, t: Translator) -> str:
    sev = f.get("severity") or ""
    word = t.mark(sev) if sev in TONE else _e(sev)
    tone = TONE.get(sev) or ""
    chip = f'<span class="chip{(" " + tone) if tone else ""}"><span class="dot"></span>{word}</span>'
    title = (t.mark(f["title_id"], **(f.get("title_args") or {})) if f.get("title_id")
             else _e(f.get("title") or f.get("id")))
    return f'<li>{chip} <a href="findings.html#{_e(_anchor(f.get("id")))}">{title}</a></li>'


def _findings(rows: list[dict], t: Translator) -> str:
    if not rows:
        return ""
    return '<ul class="tools-findings">' + "".join(_finding(f, t) for f in rows) + "</ul>"


def _table(t: Translator, caption: str, heads: list[tuple[str, bool]], rows: list[list[str]], empty: str) -> str:
    """A sub-heading and a table with labelled cells (narrow screens stack them), or the empty sentence."""
    head = t.mark(caption, tag="h3", attrs=SUB)
    if not rows:
        return head + t.mark(empty, tag="p", attrs=HINT)
    th = "".join(f'<th scope="col">{t.mark(h)}</th>' for h, _num in heads)
    body = "".join("<tr>" + "".join(f'<td{t.attr("data-label", h)}>{c}</td>' for (h, _n), c in zip(heads, r))
                   + "</tr>" for r in rows)
    return f'{head}<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>'


def _where(row: dict, t: Translator) -> str:
    scope = row.get("scope")
    word = t.mark(f"scope@@{scope}") if scope in ("user", "project", "local") else _e(scope or UNKNOWN)
    return word + (f' <span class="mono">{_e(row["project"])}</span>' if row.get("project") else "")


def _state(p: dict, t: Translator) -> str:
    enabled = p.get("enabled")
    out = (f'<span class="chip ok"><span class="dot"></span>{t.mark("enabled")}</span>' if enabled is True
           else f'<span class="chip"><span class="dot"></span>{t.mark("disabled")}</span>' if enabled is False
           else f'<span class="chip"><span class="dot"></span>{t.mark("not known")}</span>')
    if p.get("behind"):
        out += f'<span class="chip warn"><span class="dot"></span>{t.mark("behind")}</span>'
    return out


def _plugins(a: dict, t: Translator) -> str:
    rows = [[f'<span class="mono">{_e(p.get("name"))}</span>', _where(p, t), _e(p.get("version") or UNKNOWN),
             _e(p.get("latest") or UNKNOWN), _state(p, t), _findings(p["findings"], t)]
            for p in a.get("plugins") or []]
    return _table(t, "Plugins", [("Plugin", False), ("Installed for", False), ("Version", False),
                                 ("Newest known", False), ("State", False), ("Findings", False)],
                  rows, "No plugin recorded for this agent.")


def _tier(s: dict, t: Translator) -> str:
    return t.mark(TIERS[s["tier"]]) if s.get("tier") in TIERS else t.mark("not recorded")


def _skills(a: dict, t: Translator) -> str:
    parts = [t.mark("Skills", tag="h3", attrs=SUB)]
    scopes = a.get("skills") or {}
    if not scopes:
        return "".join(parts) + t.mark("No skill recorded for this agent.", tag="p", attrs=HINT)
    for scope, rows in scopes.items():
        label = {"user": "User skills", "project": "Project skills", "plugin": "Plugin skills"}.get(scope, "Other skills")
        cells = [[f'<span class="mono">{_e(s.get("name"))}</span>', _e(s.get("version") or UNKNOWN), _tier(s, t),
                  _findings(s["findings"], t)]
                 + ([f'<span class="mono">{_e(s.get("project") or s.get("plugin") or UNKNOWN)}</span>']
                    if scope in ("project", "plugin") else [])
                 for s in rows]
        if scope == "project":
            table = _rows(t, [("Skill", False), ("Version", False), ("Source", False), ("Findings", False),
                              ("Project", False)], cells)
        elif scope == "plugin":
            table = _rows(t, [("Skill", False), ("Version", False), ("Source", False), ("Findings", False),
                              ("Plugin", False)], cells)
        else:
            table = _rows(t, [("Skill", False), ("Version", False), ("Source", False), ("Findings", False)], cells)
        flagged = any(s["findings"] for s in rows)
        parts.append(f'<details class="tools-scope"{" open" if flagged else ""}><summary>{t.mark(label)}'
                     f' <span class="n">{t.number(len(rows))}</span></summary>{table}</details>')
    return "".join(parts)


def _rows(t: Translator, heads: list[tuple[str, bool]], rows: list[list[str]]) -> str:
    th = "".join(f'<th scope="col">{t.mark(h)}</th>' for h, _num in heads)
    body = "".join("<tr>" + "".join(f'<td{t.attr("data-label", h)}>{c}</td>' for (h, _n), c in zip(heads, r))
                   + "</tr>" for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


def _hooks(a: dict, t: Translator) -> str:
    rows = [[f'<span class="mono">{_e(h.get("event"))}</span>',
             f'<span class="mono">{_e(h["matcher"])}</span>' if h.get("matcher") else t.mark("any"),
             t.mark("your settings") if h.get("source") == "user"
             else f'<span class="mono">{_e(str(h.get("source") or "").removeprefix("plugin:"))}</span>',
             f'<span class="mono">{_e(h.get("program"))}</span>', _findings(h["findings"], t)]
            for h in a.get("hooks") or []]
    if a.get("id") != "claude-code":
        return t.mark("Hooks", tag="h3", attrs=SUB) + t.mark("Hooks are read for Claude Code only.", tag="p",
                                                              attrs=HINT)
    return _table(t, "Hooks", [("Event", False), ("For tools", False), ("From", False), ("Program", False),
                               ("Findings", False)], rows, "No hook recorded for this agent.")


def _agent(a: dict, t: Translator) -> str:
    aid = str(a.get("id") or "")
    head = f'<h2{HEADING}>{_e(a.get("name") or aid)}</h2>'
    version = (t.mark("version {version}", version=a["version"]) if a.get("version")
               else t.mark("version not read"))
    found = "" if a.get("installed") else " · " + t.mark("no launcher found; its records are read")
    parts = [f'<section class="card panel tools-agent" id="agent-{_e(aid)}">{head}'
             f'<p class="machine-at">{version}{found}</p>', _findings(a.get("findings") or [], t)]
    if aid in READ_AGENTS:
        parts += [_plugins(a, t), _skills(a, t), _hooks(a, t)]
    else:
        parts.append(t.mark("Its skills, plugins and hooks are not read yet: the scan reads those of Claude Code "
                            "and Codex.", tag="p", attrs=HINT))
    return "".join(parts) + "</section>"


def _plain(reason: str) -> str:
    """A reason without the command it ends with: a person reads it, an agent has the MCP tool."""
    at = reason.find("`")
    if at < 0:
        return reason
    cut = max(reason.rfind(";", 0, at), reason.rfind(",", 0, at))
    return (reason[:cut] if cut > 0 else reason[:at]).rstrip(" :(")


def _reason(d: dict, t: Translator) -> str:
    code, reason = d.get("code"), str(d.get("reason") or "")
    if d.get("source") == "harness.json" and code == "not-surveyed":
        if "predates" in reason:
            return t.mark("The last scan was made before the inventory existed; the next scan fills it.")
        return t.mark("The harness scan has not run here yet, or it is turned off.")
    if code == "stale":
        return t.mark("The last scan is more than a day old; the lists show that day.")
    if code == "unreadable" and d.get("error"):
        return t.mark("the record could not be read ({error})", error=d["error"])
    if code == "not-built":
        return t.mark("the findings were not built, so none is shown beside the items")
    return t.known(_plain(reason))


def _tiles(v: dict, t: Translator) -> str:
    c = v.get("counts") or {}
    tiles = [(t.mark("Coding agents"), t.number(c.get("agents", 0)), ""),
             (t.mark("Plugins"), t.number(c.get("plugins", 0)), t.mark("{n} behind", n=c.get("behind", 0))),
             (t.mark("Skills"), t.number(c.get("skills", 0)), ""),
             (t.mark("Hooks"), t.number(c.get("hooks", 0)), ""),
             (t.mark("Findings"), t.number(c.get("findings", 0)), "")]
    return '<div class="tiles machine-tiles">' + "".join(
        f'<div class="tile">{k}<b>{n}</b>{sub}</div>' for k, n, sub in tiles) + "</div>"


def tools_html(payload: dict, t: Translator | None = None) -> str:
    t = t or Translator()
    v = payload.get("tooling") or {}
    parts = ['<section id="tools" class="tools">']
    if v.get("measuredAt"):
        parts.append(t.mark("Harness scanned {at}", tag="p", attrs=' class="machine-at"', at=_stamp(v["measuredAt"])))
    agents = v.get("agents") or []
    degraded = [d for d in v.get("degraded") or [] if isinstance(d, dict)]
    if agents:
        parts.append(_tiles(v, t))
    if degraded:
        # First, so a partial list is never read as the whole one.
        items = "".join(f'<li><span class="mono">{_e(d.get("source"))}</span>: {_reason(d, t)}</li>' for d in degraded)
        parts.append(f'<section class="card panel tools-degraded">{t.mark("Not fully read", tag="h2", attrs=HEADING)}'
                     f'<ul>{items}</ul></section>')
    if not agents and not degraded:
        parts.append(f'<section class="card panel">{t.mark("No coding agent found here.", tag="p", attrs=EMPTY)}'
                     "</section>")
    if v.get("other"):
        parts.append(f'<section class="card panel">{t.mark("Findings about the setup as a whole", tag="h2", attrs=HEADING)}'
                     f'{_findings(v["other"], t)}</section>')
    parts += [_agent(a, t) for a in agents]
    if any(o.get("finding") == "organization-plugin-in-personal-space" for o in v.get("open") or []
           if isinstance(o, dict)):
        parts.append(t.mark("An organization's plugin enabled in your own space is not reported yet.", tag="p",
                            attrs=HINT))
    parts.append(t.mark("Agents read the same inventory with the MCP tool {tool}, section skills.", tag="p",
                        attrs=HINT, tool="observatory_machine"))
    parts.append("</section>")
    return "".join(parts)


#: Values the page translates through a variable, named so the catalogue test sees them (tests/test_i18n.py).
DYNAMIC = (*TIERS.values(), "scope@@user", "scope@@project", "scope@@local", "critical", "warning", "info",
           "User skills", "Project skills", "Plugin skills", "Other skills",
           # The skill tables' column heads, passed through `_rows`.
           "Skill", "Version", "Source", "Findings", "Project", "Plugin")
