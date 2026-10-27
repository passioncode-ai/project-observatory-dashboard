#!/usr/bin/env python3
"""Home: is everything fine, and what needs the person first (OSS-88, OBS-261).

The overview it replaces opened on a wall of findings (eight critical rows above
the counters) and answered "what is wrong" while hiding "is anything fine"
(docs/design/PERSON-SURFACES.md, decision D1). Home answers both in the first
screenful, top to bottom:

* the STATE BAND: six tiles (sites, apps, disk and memory, keys, agents, personal
  cloud), each with the period or time it was measured. A source that did not run
  says "not measured" with its reason; it is never green and never 0;
* NEEDS YOU FIRST: the three findings to do first, one row per kind with its count,
  and "All problems (N)" to the findings page;
* PROJECTS AT A GLANCE, grouped by organization (by label once OBS-270 lands).

Activity (the store's commit and project windows) and the section cards follow it,
drawn by the page script and `shell.cards_html` as before.

It reads ONLY what the pages already read: the build's payload. Nothing here opens
a file, asks a provider or runs a command; a command it shows is copied, never run.
"""
from __future__ import annotations

import html
import re

from i18n import Translator

#: The tile keys, in the order the band shows them.
TILES = ("sites", "apps", "disk", "keys", "agents", "cloud")
#: Severity order for "which first", and each severity's chip class.
SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}
SEVERITY_CHIP = {"critical": "danger", "warning": "warn", "info": ""}
#: How many problems Home names. A limit, not a sample: the ranking makes these
#: the ones to do first, and "All problems (N)" is one press away (D1).
NEEDS_LIMIT = 3
#: Free-disk thresholds when the disk guard has not run (tools/disk_guard.py defaults).
DISK_CRITICAL_GB, DISK_LOW_GB = 10, 25


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _shell_arg(value: str) -> str:
    """The page script's `shellArg`: always single-quoted, a quote escaped."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def full_command(payload: dict, *args: str) -> str:
    """The command a person types, as the page script spells it (`fullCommand`)."""
    home = (payload.get("runtime") or {}).get("home")
    return (f"OBSERVATORY_HOME={_shell_arg(home)} " if home else "") + "project-observatory full " + " ".join(args)


def _copy(command: str, t: Translator) -> str:
    return (f'<p><button class="chip-btn mono" type="button" data-copy="{_e(command)}"'
            f'{t.attr("title", "copy the command")}>{_e(command)}</button></p>')


def _reason(degraded: object) -> str:
    """The first recorded reason a source gave for not being read, as written."""
    for d in degraded or []:
        if isinstance(d, str) and d:
            return d
        if isinstance(d, dict):
            text = d.get("reason") or d.get("text") or d.get("code")
            if text:
                return str(text)
    return ""


def _stamp(value: object) -> str:
    """An ISO time as the header writes it: `2026-01-02 03:04 UTC`; a date stays a date."""
    s = "" if value is None else str(value)
    if "T" in s:
        return s[:16].replace("T", " ") + " UTC"
    return s


# --- the state band ---------------------------------------------------------

def _tile(key: str, name: str, state: str, value: str, sub: str, at: str,
          href: str | None, t: Translator) -> dict:
    return {"key": key, "name": name, "state": state, "value": value, "sub": sub, "at": at, "href": href}


def _unmeasured(key: str, name: str, at: str, reason: str, href: str | None, t: Translator) -> dict:
    return _tile(key, name, "unmeasured", t.mark("not measured"), t.known(reason) if reason else "", at, href, t)


def _site_hosts(payload: dict) -> dict[str, dict | None]:
    """Every host the Domains page and the project rows measured, once: the registrar
    names with their liveness, and each project's site hosts."""
    hosts: dict[str, dict | None] = {}
    for d in payload.get("domains") or []:
        if d.get("name"):
            hosts[d["name"]] = d.get("live") or hosts.get(d["name"])
    for r in payload.get("rows") or []:
        for s in r.get("sites") or []:
            if s.get("host"):
                hosts[s["host"]] = s.get("live") or hosts.get(s["host"])
    return hosts


def sites_tile(payload: dict, t: Translator) -> dict:
    name = t.mark("Sites")
    hosts = _site_hosts(payload)
    measured = [v for v in hosts.values() if isinstance(v, dict)]
    if not measured:
        return _unmeasured("sites", name, t.mark("never measured"), "", "domains.html", t)
    up = sum(1 for v in measured if v.get("resolves") and isinstance(v.get("http"), int)
             and not isinstance(v.get("http"), bool) and 0 < v["http"] < 400)
    down = len(measured) - up
    checked = max((str(v.get("on") or "") for v in measured), default="")
    return _tile("sites", name, "bad" if down else "ok", t.mark("home@@{n} up", n=up),
                 t.mark("home@@{n} down", n=down),
                 t.mark("checked {date}", date=checked) if checked else "", "domains.html", t)


#: The infrastructure pages beside Heroku (OBS-235, OBS-271), each a line on the apps tile.
INFRA = (("digitalocean", "DigitalOcean"), ("cloudflare", "Cloudflare"))


def infra_lines(payload: dict, t: Translator) -> tuple[list[str], str]:
    """One line per infrastructure provider that was read or is switched on, in the words its
    section card uses (`shell._infra_line`), and the worst state among them: `bad` when a
    resource is down, `warn` when one is off. A provider never connected adds no line."""
    import shell
    lines, worst = [], "ok"
    switches = payload.get("infra_integrations") or {}
    for key, label in INFRA:
        doc = payload.get(key)
        if not doc and not switches.get(key):
            continue
        on = switches.get(key, True) if doc else True
        lines.append(f"{label}: " + shell._infra_line(doc, t, on))
        by = ((doc or {}).get("totals") or {}).get("by_state") or {}
        if on and by.get("down"):
            worst = "bad"
        elif on and by.get("off") and worst == "ok":
            worst = "warn"
    return lines, worst


def apps_tile(payload: dict, t: Translator) -> dict:
    tile = _apps_heroku(payload, t)
    lines, worst = infra_lines(payload, t)
    tile["more"] = lines
    if tile["state"] != "unmeasured":
        rank = ("ok", "warn", "bad")
        tile["state"] = max(tile["state"], worst, key=rank.index)
    return tile


def _apps_heroku(payload: dict, t: Translator) -> dict:
    name = t.mark("Apps")
    doc = payload.get("heroku")
    if doc is None:
        return _unmeasured("apps", name, t.mark("never measured"), "", "heroku.html", t)
    apps = doc.get("apps") or []
    if not apps and doc.get("degraded"):
        at = (t.mark("not measured since {date}", date=str(doc["scanned_on"]))
              if doc.get("scanned_on") else "")
        return _unmeasured("apps", name, at, _reason(doc.get("degraded")), "heroku.html", t)
    down = sum(1 for a in apps if a.get("state") in ("down", "suspended") or a.get("crashed"))
    up = sum(1 for a in apps if a.get("state") == "running" and not a.get("crashed"))
    at = t.mark("read {date}", date=str(doc.get("scanned_on"))) if doc.get("scanned_on") else ""
    return _tile("apps", name, "bad" if down else "ok", t.mark("home@@{n} up", n=up),
                 t.mark("home@@{n} down", n=down) + " · Heroku", at,
                 "heroku.html?f=down" if down else "heroku.html", t)


LEVELS = ("normal", "low", "critical")


def disk_level(m: dict, free: float) -> str:
    """The worse of two readings: the level the measured free space falls in (the guard's
    own lines, `diskLevels` from the build, else its defaults) and the guard's last level,
    counted only when the guard ran no earlier than the measurement. A guard run from
    weeks ago at `normal` never turns a fresh 4 GB green (review of #377)."""
    lines = m.get("diskLevels") or {}
    critical = lines.get("critical") if isinstance(lines.get("critical"), (int, float)) else DISK_CRITICAL_GB
    low = lines.get("low") if isinstance(lines.get("low"), (int, float)) else DISK_LOW_GB
    level = "critical" if free < critical else "low" if free < low else "normal"
    guard = m.get("guard") or {}
    at, measured = str(guard.get("at") or ""), str(m.get("measuredAt") or "")
    if guard.get("level") in LEVELS and at and measured and at[:19] >= measured[:19]:
        level = max(level, guard["level"], key=LEVELS.index)
    return level


def disk_tile(payload: dict, t: Translator) -> dict:
    name = t.mark("Disk and memory")
    m = payload.get("machine") or {}
    volume = (m.get("disk") or {}).get("volume") or {}
    free = volume.get("free_gb")
    if not m.get("measuredAt") or not isinstance(free, (int, float)) or isinstance(free, bool):
        return _unmeasured("disk", name, t.mark("never measured"), _reason(m.get("degraded")), "machine.html", t)
    level = disk_level(m, free)
    mem = m.get("memory") or {}
    used = None
    if isinstance(mem.get("total_mb"), (int, float)) and isinstance(mem.get("free_mb"), (int, float)):
        used = mem["total_mb"] - mem["free_mb"] - (mem.get("inactive_mb") or 0)
    memory = (t.mark("memory {used} of {total} GB", used=round(used / 1024, 1), total=round(mem["total_mb"] / 1024, 1))
              if used is not None else t.mark("memory not measured"))
    state = {"critical": "bad", "low": "warn"}.get(level, "ok")
    return _tile("disk", name, state, t.mark("{gb} GB free", gb=free), memory,
                 t.mark("measured {date}", date=_stamp(m.get("measuredAt"))), "machine.html", t)


def keys_tile(payload: dict, t: Translator) -> dict:
    name = t.mark("Keys")
    doc = payload.get("creds")
    if doc is None:
        return _unmeasured("keys", name, t.mark("never measured"), "", "creds.html", t)
    totals = doc.get("totals") or {}
    leaked = int(totals.get("leaked_unrotated") or 0)
    unowned = int(totals.get("unclaimed") or 0)
    at = t.mark("read {date}", date=str(doc.get("scanned_on"))) if doc.get("scanned_on") else ""
    return _tile("keys", name, "bad" if leaked else "ok", t.mark("home@@{n} leaked, not rotated", n=leaked),
                 t.mark("home@@{n} without a project", n=unowned), at,
                 "creds.html?f=c-leaked" if leaked else "creds.html", t)


def agents_tile(payload: dict, t: Translator) -> dict:
    name = t.mark("Agents")
    a = payload.get("agents") or {}
    counters = a.get("counters") or {}
    if not counters:
        return _unmeasured("agents", name, t.mark("never measured"), _reason(a.get("degraded")), "agents.html", t)
    stalled = int(counters.get("stalled") or 0)
    # Each number names its own period: sessions with a turn in the last hour
    # (agents_view.ACTIVE_SECONDS) and workflows stalled now. Both are read from the store
    # when the page is built, so the time is the header's "Measured", not a clock here
    # (two builds a minute apart would differ).
    at = t.mark("as of this page's measurement")
    return _tile("agents", name, "warn" if stalled else "ok",
                 t.mark("home@@{n} sessions active in the last hour", n=int(counters.get("sessionsActive") or 0)),
                 t.mark("home@@{n} workflows stalled now", n=stalled), at, "agents.html", t)


def cloud_tile(payload: dict, t: Translator) -> dict:
    """The personal cloud (OBS-263) has no connection record yet: the tile says so
    and is no link, because there is no page to open."""
    name = t.mark("Personal cloud")
    cloud = payload.get("cloud")
    if not isinstance(cloud, dict) or not cloud.get("connected"):
        return _tile("cloud", name, "unmeasured", t.mark("not connected"),
                     t.mark("Not set up on this machine yet."), "", None, t)
    return _tile("cloud", name, "ok", t.mark("connected"), "",
                 t.mark("checked {date}", date=_stamp(cloud.get("checkedAt"))) if cloud.get("checkedAt") else "",
                 None, t)


def tiles(payload: dict, t: Translator) -> list[dict]:
    return [sites_tile(payload, t), apps_tile(payload, t), disk_tile(payload, t),
            keys_tile(payload, t), agents_tile(payload, t), cloud_tile(payload, t)]


def band_html(payload: dict, t: Translator) -> str:
    cells = []
    for tl in tiles(payload, t):
        inner = (f'<span class="home-k">{tl["name"]}</span><b>{tl["value"]}</b>'
                 + (f'<span class="home-sub">{tl["sub"]}</span>' if tl["sub"] else "")
                 + "".join(f'<span class="home-sub">{line}</span>' for line in tl.get("more") or [])
                 + (f'<small class="home-at">{tl["at"]}</small>' if tl["at"] else ""))
        cls = f'tile home-tile home-{tl["state"]}'
        attrs = f' class="{cls}" data-tile="{tl["key"]}" data-state="{tl["state"]}"'
        cells.append(f'<a{attrs} href="{tl["href"]}">{inner}</a>' if tl["href"] else f"<div{attrs}>{inner}</div>")
    return (f'<section class="home-state" aria-labelledby="home-state-h">'
            + t.mark("State of the system", tag="h2", attrs=' class="home-h visually-hidden-list" id="home-state-h"')
            + f'<div class="home-band">{"".join(cells)}</div></section>')


# --- needs you first --------------------------------------------------------

def _projects_of(item: dict) -> set[str]:
    """The projects a finding touches: its subject when it is one, and the project row
    its subject resolved to (`subject_links`)."""
    out = set()
    subject = str(item.get("subject") or "")
    if subject.startswith("project:"):
        out.add(subject)
    href = str(item.get("href") or "")
    if href.startswith("projects.html#project:"):
        out.add(href.split("#", 1)[1])
    return out


def first_problems(findings: dict | None, limit: int = NEEDS_LIMIT) -> list[dict]:
    """The kinds to do first. Findings of one type collapse into one group; groups are
    ranked by severity, then whether the person can act now (the rule names an action),
    then how long the kind has been open (oldest first), then how many projects it
    touches. Each group carries its count and its most urgent finding."""
    groups: dict[str, dict] = {}
    for f in (findings or {}).get("items") or []:
        g = groups.setdefault(f.get("type") or "", {"type": f.get("type") or "", "items": [], "projects": set()})
        g["items"].append(f)
        g["projects"] |= _projects_of(f)
    ranked = []
    for g in groups.values():
        items = g["items"]
        severity = min(SEVERITY_RANK.get(f.get("severity"), 3) for f in items)
        actionable = any(str(f.get("action") or "").strip() for f in items)
        seen = [str(f["first_seen"]) for f in items if f.get("first_seen")]
        oldest = min(seen) if seen else "9999-99-99"
        lead = sorted(items, key=lambda f: (SEVERITY_RANK.get(f.get("severity"), 3),
                                            0 if str(f.get("action") or "").strip() else 1,
                                            str(f.get("first_seen") or "9999-99-99"), str(f.get("id") or "")))[0]
        ranked.append({"type": g["type"], "count": len(items), "lead": lead, "projects": len(g["projects"]),
                       "key": (severity, 0 if actionable else 1, oldest, -len(g["projects"]), g["type"])})
    ranked.sort(key=lambda g: g["key"])
    return ranked[:limit]


_TOOL = re.compile(r'\bpython "\$\(project-observatory full-path\)/([^"]+)"')


def runnable(text: str, payload: dict) -> str:
    """The page script's `runnable`: a rule names a tool as `python "$(project-observatory
    full-path)/tools/..."`; with this engine's interpreter and path known it becomes them."""
    rt = payload.get("runtime") or {}
    if not rt.get("python"):
        return text
    engine = str(rt.get("engine") or ".").rstrip("/")
    return _TOOL.sub(lambda m: _shell_arg(rt["python"]) + " " + _shell_arg(engine + "/" + m.group(1)), text)


def _anchor(fid: object) -> str:
    """The findings page's row anchor, as `renderFindings` writes it."""
    return "f-" + re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(fid))


def _title(item: dict, t: Translator) -> str:
    if item.get("title_id"):
        return t.mark(item["title_id"], **(item.get("title_args") or {}))
    return _e(item.get("title") or item.get("id") or "")


def needs_html(payload: dict, t: Translator) -> str:
    f = payload.get("findings")
    head = t.mark("Needs you first", tag="h2", attrs=' class="home-h" id="home-needs-h"')
    if not f:
        body = (f'<div class="empty not-scanned"><p>{t.mark("Problems not checked yet. Measure this machine:")}</p>'
                + _copy(full_command(payload, "local"), t) + "</div>")
        return f'<section class="home-needs" aria-labelledby="home-needs-h">{head}{body}</section>'
    counts = f.get("counts") or {}
    items = f.get("items") or []
    total = (sum(int(counts.get(k) or 0) for k in ("critical", "warning", "info"))
             if counts else len(items))
    when = _stamp(f.get("built_at"))
    groups = first_problems(f)
    if not total or not groups:
        body = (f'<p class="home-calm">{t.mark("Nothing needs you")}'
                + (f' <span class="home-at">{t.mark("last checked {date}", date=when)}</span>' if when else "")
                + "</p>")
        return f'<section class="home-needs" aria-labelledby="home-needs-h">{head}{body}</section>'
    labels = f.get("type_labels") or {}
    rows = []
    for g in groups:
        lead = g["lead"]
        sev = lead.get("severity") or ""
        word = t.mark(sev) if sev in SEVERITY_CHIP else _e(sev)
        chip = f'<span class="chip{(" " + SEVERITY_CHIP[sev]) if SEVERITY_CHIP.get(sev) else ""}"><span class="dot"></span>{word}</span>'
        label = t.mark(labels[g["type"]]) if labels.get(g["type"]) else _e(g["type"])
        count = (f' <span class="home-count">{t.mark("open: {n}", n=g["count"])}</span>' if g["count"] > 1 else "")
        # A page subject's label is a message id (`subject_links.PAGE_SUBJECTS`); a row's is a name.
        target = (t.mark(lead["href_label"]) if lead.get("href_page") else _e(lead.get("href_label") or ""))
        subject = (f' <a class="plink fsubj" href="{_e(lead["href"])}">→ {target}</a>'
                   if lead.get("href") and lead.get("href_label") else "")
        # The action the rule names, as the findings page writes it (English, L10N: a
        # finding's detail and action stay as the rule wrote them; `runnable` as on the page).
        action = str(lead.get("action") or "").strip()
        act = f'<span class="act home-act">{t.known(runnable(action, payload))}</span>' if action else ""
        review = f'<a class="fdetail" href="findings.html#{_e(_anchor(lead.get("id")))}">{t.mark("Review →")}</a>'
        rows.append(f'<li class="f" data-type="{_e(g["type"])}" data-sev="{_e(sev)}">{chip}'
                    f'<span class="t"><span class="ftl">{label}{count}</span>{_title(lead, t)}{subject}</span>'
                    f'{act}{review}</li>')
    more = f'<p class="home-more"><a href="findings.html">{t.mark("All problems ({n})", n=total)}</a></p>'
    return (f'<section class="home-needs" aria-labelledby="home-needs-h">{head}'
            f'<ol class="flist home-list">{"".join(rows)}</ol>{more}</section>')


# --- projects at a glance ---------------------------------------------------

def _need_you_projects(findings: dict | None) -> set[str]:
    """Projects with an open critical or warning finding."""
    out = set()
    for f in (findings or {}).get("items") or []:
        if f.get("severity") in ("critical", "warning"):
            out |= _projects_of(f)
    return out


def project_groups(payload: dict) -> list[dict]:
    """One group per organization: its projects, how many need the person, and the
    latest activity. A project with no organization is grouped as such, never dropped."""
    need = _need_you_projects(payload.get("findings"))
    groups: dict[str, dict] = {}
    for r in payload.get("rows") or []:
        org = r.get("organization") or ""
        g = groups.setdefault(org, {"organization": org, "projects": 0, "need": 0, "last": ""})
        g["projects"] += 1
        g["need"] += 1 if r.get("id") in need else 0
        g["last"] = max(g["last"], str(r.get("last") or ""))
    return sorted(groups.values(), key=lambda g: (g["organization"] == "", -g["projects"], g["organization"]))


def projects_html(payload: dict, t: Translator) -> str:
    groups = project_groups(payload)
    items = []
    for g in groups:
        org = g["organization"]
        name = (t.mark("No organization") if not org else t.mark("Others' code") if org == "external" else _e(org))
        last = (t.mark("last activity {date}", date=g["last"]) if g["last"] else t.mark("no activity recorded"))
        need = t.mark("{n} need you", n=g["need"])
        items.append(f'<li><a class="home-org" href="projects.html"><b>{name}</b>'
                     f' <span class="home-n">{t.mark("tile@@{n} projects", n=g["projects"])}</span></a>'
                     f'<span class="home-sub">{need} · {last}</span></li>')
    return (f'<section class="home-projects" aria-labelledby="home-projects-h">'
            + t.mark("Projects at a glance", tag="h2", attrs=' class="home-h" id="home-projects-h"')
            + f'<ul class="home-orgs">{"".join(items)}</ul></section>')


# --- the page ---------------------------------------------------------------

def empty_html(payload: dict, t: Translator) -> str:
    """A fresh install: what to connect first, each with its command, never a row of zeros."""
    configured = (payload.get("runtime") or {}).get("projects_configured")
    steps = []
    if configured is False:
        steps.append((t.mark("Name the folder that holds your project checkouts:"),
                      full_command(payload, "configure sources projects", "PATH")))
    steps += [(t.mark("Measure this machine's projects:"), full_command(payload, "local")),
              (t.mark("See which providers can connect:"), full_command(payload, "doctor")),
              (t.mark("Connect your coding agent:"), full_command(payload, "agent install"))]
    body = "".join(f"<p>{said}</p>{_copy(cmd, t)}" for said, cmd in steps)
    return (f'<section class="home-empty" aria-labelledby="home-empty-h">'
            + t.mark("Connect your first source", tag="h2", attrs=' class="home-h" id="home-empty-h"')
            + f'<div class="empty not-scanned">{body}</div></section>')


def home_html(payload: dict, t: Translator | None = None) -> str:
    """Home's body, rendered at build time into the index page."""
    t = t or Translator()
    if not payload.get("rows"):
        return f'<div id="home" class="home">{empty_html(payload, t)}</div>'
    return (f'<div id="home" class="home">{band_html(payload, t)}{needs_html(payload, t)}'
            f'{projects_html(payload, t)}</div>')
