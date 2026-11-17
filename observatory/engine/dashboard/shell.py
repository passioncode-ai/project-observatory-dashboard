#!/usr/bin/env python3
"""The dashboard's shell: which pages exist, how they are linked, what each carries.

One page of 1.3 MB with seven tabs was the whole surface until 2026-09-14, and
the operator's verdict was that it could not be used: alerts above navigation,
one `main` for seven subjects, nothing addressable, everything loaded for
anything (plan v2, M1; scenario S12). The split keeps ONE template and ONE
script — every page is the same HTML with a `PAGE` constant, a navigation bar
in place of the tab strip, and only the DATA that page renders — so nothing is
rendered twice by two code paths, and a fix to a renderer reaches every page.

`docs/projects-dashboard.html` stays as the full single page (the tests, the
smoke harness and the design checks read it), and `docs/dashboard/*.html` are
the pages a person opens. Both come from `build_dashboard.py` in one run.
"""
from __future__ import annotations
import base64
import html
import importlib
import json
import re
from pathlib import Path

from i18n import LOCALE_NAMES, LOCALES, Translator
# The page table lives in page_table.py (OBS-572); the names are re-exported because every reader says `shell.X`.
from page_table import (BODIES, HOME_BODY, SECTIONS, NAV_GROUP_LABELS, NAV_GROUPS, NAV_LINKS, NAV_VIEWS,  # noqa: F401
                        PAGE_MODULES, PAGE_TABLE, PAGES, QUESTIONS, SLOTS, Page)

#: The product's name is never translated (docs/brand/locales/*.md).
TITLE_SUFFIX = "Project Observatory"
#: The family the product belongs to, and where it lives.
FAMILY_NAME = "PassionCode.ai"
FAMILY_URL = "https://passioncode.ai/"
#: The product glyph, inline: a page opened over file:// has no server to ask.
ICON = "data:image/svg+xml;base64," + base64.b64encode(
    (Path(__file__).with_name("brand") / "observatory-mark.svg").read_bytes()).decode("ascii")
NAMES = tuple(p[0] for p in PAGES)
TABLE_PAGES = tuple(p[0] for p in PAGES if p[2] == "table")
INFRA_PAGES = tuple(p[0] for p in PAGES if p[2] == "infra")
#: The pages whose overview card Home leaves out, because a tile or a Home group already says it (CP-01):
#: Needs you first speaks for Findings, the Estate group for Projects, the tiles for the rest.
HOME_COVERED = ("findings", "projects", "agents", "domains", "heroku", "digitalocean", "cloudflare", "creds",
                "machine", "disk")
OVERVIEW_FINDINGS_LIMIT = 8

#: What a lite project row keeps on pages that only look projects UP — the
#: domains page names a project, the creds page names an owner — but never
#: render the table. Everything else in a row is the projects page's business.
LITE_ROW_KEYS = ("id", "name", "anchor", "products", "tier", "lifecycle")


def brand_html(t: Translator) -> str:
    """The product glyph on its dark tile, the name, and the family it is part of."""
    family = t.mark("by {family}", attrs=' class="brand-family"', family=FAMILY_NAME)
    return ('<a class="brand" href="index.html">'
            f'<img class="brand-mark" src="{ICON}" width="32" height="32" alt="">'
            f'<span class="brand-name"><strong>{TITLE_SUFFIX}</strong>{family}</span></a>')


def _agents_line(payload: dict, t: Translator) -> str:
    """The overview's Agents card: what is open and what needs a person."""
    c = (payload.get("agents") or {}).get("counters") or {}
    if not c:
        return t.mark("no workflow yet")
    needs = len((payload.get("agents") or {}).get("needsYou") or [])
    return (t.mark("{n} workflows open", n=c.get("workflowsOpen", 0)) + " · "
            + t.mark("{n} need you", n=needs))


def _home(payload: dict, t: Translator) -> str:
    """Home's body, rendered at build time (dashboard/home_page.py, OSS-88)."""
    module, func = HOME_BODY
    return getattr(importlib.import_module(module), func)(payload, t)


def body_html(kind: str, page: str, payload: dict, t: Translator) -> str:
    """The body a page kind draws at build time (`BODIES`): the module's function, given the page name
    when it serves several pages (the infrastructure pages)."""
    _slot, module, func, takes_page = BODIES[kind]
    render = getattr(importlib.import_module(module), func)
    return render(page, payload, t) if takes_page else render(payload, t)


def _tools_line(payload: dict, t: Translator) -> str:
    """The overview's Installed tools card: agents found and plugins behind, or that nothing was read."""
    v = payload.get("tooling") or {}
    if not v.get("agents"):
        return t.mark("not scanned")
    c = v.get("counts") or {}
    return (t.mark("{n} coding agents", n=c.get("agents", 0)) + " · "
            + t.mark("{n} plugins behind", n=c.get("behind", 0)))


def locale_switch_html(t: Translator) -> str:
    """Language: System / English / Russian (L10N-01). "System" follows the
    system's preferred language; each language names itself in itself and is
    not translated. The page script presses the reader's choice and keeps it
    (see `LOCALE_KEY` in the template)."""
    system = t.mark("language@@System", tag="button",
                    attrs=' type="button" data-locale="system" aria-pressed="false"')
    buttons = "".join(
        f'<button type="button" data-locale="{code}" lang="{code}" aria-pressed="false">'
        f'{html.escape(LOCALE_NAMES[code])}</button>'
        for code in LOCALES)
    return f'<div class="locale-switch" role="group"{t.attr("aria-label", "Language")}>{system}{buttons}</div>'


def theme_switch_html(t: Translator) -> str:
    """Theme: Light / Dark (OBS-523). Light is the default; the page script
    presses the reader's choice, applies it at once and keeps it (see
    `THEME_KEY` in the template and the resolver before first paint)."""
    light = t.mark("theme@@Light", tag="button",
                   attrs=' type="button" data-theme-choice="light" aria-pressed="false"')
    dark = t.mark("theme@@Dark", tag="button",
                  attrs=' type="button" data-theme-choice="dark" aria-pressed="false"')
    buttons = light + dark
    return f'<div class="locale-switch theme-switch" role="group"{t.attr("aria-label", "Theme")}>{buttons}</div>'


def nav_html(page: str, counts: dict[str, int | str], t: Translator | None = None) -> str:
    """Grouped navigation keeps every route reachable without disclosure."""
    t = t or Translator()
    titles = {name: title for name, title, _kind in PAGES}
    groups = []
    for key, label, names in NAV_GROUPS:
        items = []
        for name in names:
            n = counts.get(name)
            badge = f' <span class="n">{n}</span>' if n not in (None, "") else ""
            if name in NAV_LINKS:
                view, href = NAV_LINKS[name]
                items.append(f'<a class="pg pg-view" href="{href}">{t.mark(view)}{badge}</a>')
                continue
            cur = ' aria-current="page"' if name == page else ""
            items.append(f'<a class="pg" href="{name}.html"{cur}>{t.mark(titles[name])}{badge}</a>')
        groups.append(f'<section class="nav-group" aria-labelledby="nav-{key}">'
                      + t.mark(label, tag="h2", attrs=f' class="nav-heading" id="nav-{key}"')
                      + "".join(items) + "</section>")
    return (t.mark("Skip to content", tag="a", attrs=' class="skip-link" href="#workspace"')
            + '<aside class="topbar nav-rail" id="topbar">'
            + brand_html(t)
            + f'<nav class="pages" id="pages"{t.attr("aria-label", "Sections")}>' + "".join(groups) + "</nav>"
            + locale_switch_html(t)
            + theme_switch_html(t)
            + f'<a class="family-link" href="{FAMILY_URL}" rel="noopener">{t.mark("Part of the PassionCode toolkit")}</a>'
            + "</aside>")


def domain_totals(domains: list[dict], zones: list[dict]) -> tuple[int, int]:
    """(rows, rows without a project) of the domains page: every registrar
    name, plus every Cloudflare zone no registrar lists — the same join
    `renderDomains` draws, so the overview tile, the badge and the table agree.
    A registrar name takes its projects from the registry's domain links; a
    zone-only row from the zone's own project."""
    names = {d.get("name") for d in domains}
    zone_only = [z for z in zones if z.get("name") not in names]
    unowned = (sum(1 for d in domains if not d.get("projects"))
               + sum(1 for z in zone_only if not z.get("project")))
    return len(domains) + len(zone_only), unowned


def env_default_count(env: dict) -> int:
    """How many variables the ENV page shows before any chip is pressed:
    live files only, secrets and anything whose value is set in another
    project — `envDefault` in the page script, counted the same way."""
    return sum(1 for f in env.get("files") or [] if f.get("kind") != "template"
               for v in f.get("variables") or []
               if v.get("class") == "secret" or v.get("available_in"))


def env_default_secrets(env: dict) -> int:
    """Secrets inside that same default view. The overview card once printed
    the view's size beside `totals.secrets`, which counts templates too — two
    populations in one sentence, and the second number could exceed the first
    (audit A40)."""
    return sum(1 for f in env.get("files") or [] if f.get("kind") != "template"
               for v in f.get("variables") or []
               if v.get("class") == "secret")


def _infra_count(doc: dict | None) -> int | str:
    """A DigitalOcean or Cloudflare badge: the rows its page lists, or nothing when the
    page lists none because nothing was read (never "0" for an unread provider)."""
    if not doc:
        return ""
    rows = doc.get("resources") or []
    return len(rows) if rows or not doc.get("degraded") else ""


def _infra_line(doc: dict | None, t: Translator, on: bool = True) -> str:
    """The overview card of an infrastructure page: what it lists and what it says it costs."""
    if not doc:
        return t.mark("not connected")
    if not on:
        return t.mark("switched off") + " · " + t.mark("last read {date}", date=str(doc.get("scanned_on") or ""))
    tt = doc.get("totals") or {}
    if not doc.get("resources") and doc.get("degraded"):
        return t.mark("not read")
    by = tt.get("by_state") or {}
    bad = (by.get("off") or 0) + (by.get("down") or 0)
    line = t.mark("{n} resources", n=tt.get("resources", 0))
    if bad:
        line += " · " + t.mark("off or down: {n}", n=bad)
    cost = tt.get("monthly_cost")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost:
        line += " · " + (t.mark("stated at least ${amount}/mo", amount=round(cost, 2))
                         if tt.get("monthly_cost_partial") else t.mark("stated ${amount}/mo", amount=round(cost, 2)))
    return line


def _unread(doc: dict) -> bool:
    """A scan that read no application and recorded why (`degraded`)."""
    return not doc.get("apps") and bool(doc.get("degraded"))


def counts_of(payload: dict) -> dict[str, int | str]:
    """The badge beside each page name — computed once, carried by every page,
    so a page that holds no rows of a kind still says how many exist."""
    f = payload.get("findings") or {}
    c = (f.get("counts") or {}) if f else {}
    open_findings = sum(v for k, v in c.items() if k in ("critical", "warning", "info"))
    heroku = payload.get("heroku") or {}
    mcp = payload.get("mcp") or {}
    env = payload.get("env") or {}
    creds = payload.get("creds") or {}
    # Navigation badges count what the destination shows when it opens — the
    # number its own "Showing N of …" line starts from — so a badge and the
    # page it names never count two different units. Home and Health
    # are summaries, not inventories; severity and review totals belong in
    # their labelled content rather than beside an unrelated route name.
    import finding_types
    grouped = {"security": 0, "availability": 0}
    for item in (f.get("items") or []) if f else []:
        g = finding_types.group_of(item.get("type"))
        if g in grouped and item.get("severity") in ("critical", "warning", "info"):
            grouped[g] += 1
    return {
        "index": "",
        "health": "",
        "findings": open_findings or "",
        # What the narrowed Findings page shows when it opens; no findings built is no count.
        "security": (grouped["security"] or "") if f else "",
        "availability": (grouped["availability"] or "") if f else "",
        "projects": len(payload.get("rows") or []),
        "domains": domain_totals(payload.get("domains") or [], payload.get("zones") or [])[0],
        # A scan that read no application is not a count of zero (audit A09).
        "heroku": len(heroku.get("apps") or []) if heroku and not _unread(heroku) else "",
        "digitalocean": _infra_count(payload.get("digitalocean")),
        "cloudflare": _infra_count(payload.get("cloudflare")),
        "creds": len(creds.get("credentials") or []) if creds else "",
        "env": (env_default_count(env) if env else "") or "",
        # Declarations, as the MCP page lists them (one row per agent's entry).
        "mcp": (len(mcp.get("servers") or []) or (mcp.get("totals") or {}).get("declarations", "")) if mcp else "",
        "traffic": ((payload.get("google") or {}).get("totals") or {}).get("properties", "") or "",
    }


def _observer(health: dict, t: Translator) -> str:
    """The three states the health row spells apart, in three words: -1 is off
    (a choice, not a fault), a fresh receipt is alive, an old one is silence."""
    age = health.get("server_age_s")
    if not isinstance(age, (int, float)):
        return t.mark("observer not measured")
    if age == -1:
        return t.mark("the observer was not running when measured")
    if age < (health.get("server_silent_after_s") or 90):
        return t.mark("the observer was running when measured")
    return t.mark("not answering for {n} min when measured", n=int(age // 60))


def _traffic_line(payload: dict, t: Translator) -> str:
    """A resource sum, with unknown coverage kept distinct from measured zero."""
    g = payload.get("google") or {}
    tt = g.get("totals") or {}
    if not g:
        return t.mark("analytics not scanned")
    users = tt.get("users_30d")
    loose = tt.get("users_30d_unclaimed")
    unknown = tt.get("unknown_properties") or 0
    measured = tt.get("measured_properties")
    parts = [t.mark("audience not measured") if users is None else
             t.mark("{n} users / 30 d · sum across properties", n=users)]
    if unknown:
        if users is not None:
            parts.append(t.mark("partial"))
        if measured is not None:
            parts.append(t.mark("properties measured: {n}", n=measured))
        parts.append(t.mark("not measured: {n}", n=unknown))
    if loose is not None:
        parts.append(t.mark("{n} without a project", n=loose))
    return " · ".join(parts)


def _machine_line(payload: dict, t: Translator) -> str:
    """The overview's Machine card: free disk and process count, or that the
    machine has not been surveyed — never a blank card."""
    m = payload.get("machine") or {}
    volume = (m.get("disk") or {}).get("volume") or {}
    if not m.get("measuredAt") or volume.get("free_gb") is None:
        return t.mark("not surveyed")
    return (t.mark("{gb} GB free on disk", gb=volume.get("free_gb")) + " · "
            + t.mark("{n} processes", n=(m.get("processes") or {}).get("count") or 0))


def _disk_line(payload: dict, t: Translator) -> str:
    """The overview's Disk card: free space and whether auto cleanup is on (OBS-562)."""
    m = payload.get("machine") or {}
    free = ((m.get("disk") or {}).get("volume") or {}).get("free_gb")
    auto = (m.get("cleanup") or {}).get("autoEnabled")
    line = t.mark("{gb} GB free on disk", gb=free) if free is not None else t.mark("not surveyed")
    if auto is not None:
        line += " · " + (t.mark("auto cleanup on") if auto else t.mark("auto cleanup off"))
    return line


def _integrations_line(payload: dict, t: Translator) -> str:
    """The overview's Integrations card: keys refused and answers that changed, or that nothing was checked."""
    v = payload.get("integrations") or {}
    if not v.get("checkedOn"):
        return t.mark("not checked")
    keys = v.get("keys") or []
    return (t.mark("{n} keys refused", n=sum(1 for k in keys if k.get("state") == "invalid")) + " · "
            + t.mark("{n} answers changed", n=len(v.get("drift") or [])))


def _assistant_line(payload: dict, t: Translator) -> str:
    """The overview's Assistant card: the doctor's fixes still undoable, or that it has not run (OBS-560)."""
    doctor = payload.get("doctor") or {}
    if not doctor.get("at"):
        return t.mark("Ask about the estate") + " · " + t.mark("the doctor has not run")
    return t.mark("Ask about the estate") + " · " + t.mark("doctor fixes to review: {n}", n=len(doctor.get("undoable") or []))


def cards_html(payload: dict, counts: dict, t: Translator | None = None, skip: tuple[str, ...] = ()) -> str:
    """The index page's module cards: one sentence each, the number, the link. Home passes `skip`
    (HOME_COVERED), the pages a tile or a Home group already speaks for (CP-01)."""
    t = t or Translator()
    stats = payload.get("stats") or {}
    health = payload.get("health") or {}
    lines = {
        "projects": t.mark("{n} projects", n=counts["projects"]) + " · "
                    + t.mark("{n} active this week", n=stats.get("projects_active_7d", 0)),
        "findings": t.mark("{n} open", n=counts["findings"] or 0),
        "domains": t.mark("{n} names", n=counts["domains"]) + " · "
                   + t.mark("Cloudflare zones: {n}", n=len(payload.get("zones") or [])),
        "heroku": t.mark("{n} apps", n=counts["heroku"] or 0),
        "digitalocean": _infra_line(payload.get("digitalocean"), t,
                                    (payload.get("infra_integrations") or {}).get("digitalocean", True)),
        "cloudflare": _infra_line(payload.get("cloudflare"), t,
                                  (payload.get("infra_integrations") or {}).get("cloudflare", True)),
        "creds": t.mark("{n} entries", n=counts["creds"] or 0),
        # The badge's number with its unit, then the population it is drawn from.
        "env": t.mark("{n} variables", n=counts["env"] or 0) + " · "
               + t.mark("{n} read as a secret", n=env_default_secrets(payload.get("env") or {})),
        "mcp": t.mark("{n} declarations", n=counts["mcp"] or 0) + " · "
               + t.mark("{n} servers", n=((payload.get("mcp") or {}).get("totals") or {}).get("distinct_servers", 0)),
        "traffic": _traffic_line(payload, t),
        "machine": _machine_line(payload, t),
        "tools": _tools_line(payload, t),
        "disk": _disk_line(payload, t),
        "integrations": _integrations_line(payload, t),
        "agents": _agents_line(payload, t),
        "assistant": _assistant_line(payload, t),
        # The observer's state alone: the agents' unconfirmed records ask nothing of a
        # person (operator, 2026-10-06), so they are no reason to open the page.
        "health": _observer(health, t),
    }
    # A page nobody scanned says so on its card, instead of "0 apps".
    for name, key in (("heroku", "heroku"), ("creds", "creds"), ("env", "env"), ("mcp", "mcp")):
        if payload.get(key) is None:
            lines[name] = t.mark("not scanned")
    if payload.get("heroku") is not None and _unread(payload["heroku"]):
        lines["heroku"] = t.mark("not read")
    cards = []
    for name, title, _kind in PAGES:
        if name == "index" or name in skip:
            continue
        cards.append(f'<a class="card mod" href="{name}.html">{t.mark(title, tag="b")}'
                     f'<span class="anchor">{lines.get(name, "")}</span></a>')
    return (f'<section class="mods" id="mods"{t.attr("aria-label", "Sections")}>'
            + "".join(cards) + "</section>")


def slice_for(page: str, payload: dict) -> dict:
    """Everything small, and only the HEAVY document a page renders.

    The first cut kept only what each page draws and every page but two threw
    at load: the script reads `D.dups`, `D.events` and friends unconditionally
    before any renderer runs. So the slice is the whole payload minus the
    heavy parts — full project rows on the projects page only (lite rows
    elsewhere, for lookups), and each provider document only on its page.
    """
    # `google` is the whole property inventory and belongs to its own page;
    # `traffic` is the per-project summary — a number and a link or two — and is
    # small enough to ride everywhere, which is what the projects column and the
    # project panel read.
    heavy = {"env": "env", "heroku": "heroku", "creds": "creds", "mcp": "mcp", "supply": "mcp",
             "google": "traffic", "machine": "machine", "agents": "agents", "sessions": "agents",
             "digitalocean": "digitalocean", "cloudflare": "cloudflare"}
    out = dict(payload)
    # WHAT PRODUCTION HOLDS is read by exactly the two pages that can say
    # something about it: the Heroku row and the ENV row.
    if page not in ("heroku", "env"):
        out["remote"] = None
    # The per-project key map feeds the project PANEL, which opens on the
    # projects page alone; 319 rows of names rode onto every page and pushed the
    # index to 133 KB before this line.
    if page != "projects":
        out["keys"] = None
    if page != "projects":
        out["rows"] = [{k: r.get(k) for k in LITE_ROW_KEYS} for r in payload.get("rows") or []]
    for key, owner in heavy.items():
        if page != owner:
            out[key] = None
    # The installed tools are rendered into their page (dashboard/tools_page.py) and no script reads them: a
    # machine holds a thousand skills, so they ride on no page's data.
    out["tooling"] = None
    if page != "integrations":
        out["integrations"] = None                  # the Integrations page renders it at build time (OBS-557)
    out["key_sweep"] = None                         # rendered into findings.html at build time (OBS-554)
    out["doctor"] = None                            # rendered into assistant.html at build time (OBS-560)
    if page == "disk":
        out["machine"] = payload.get("machine")     # the Disk page reads the machine survey too (OBS-562)
    if page != "domains":
        out["zones"] = None
        out["domains"] = []
    if page not in ("index", "findings"):
        out["findings"] = None
    if page != "health":
        out["queue"] = []
        # `health.wallet` is 85 KB of provider and model listings that only the
        # health page draws; every other page reads the wallet's scalars.
        h = dict(payload.get("health") or {})
        w = h.get("wallet")
        if isinstance(w, dict):
            h["wallet"] = {k: v for k, v in w.items() if not isinstance(v, (list, dict))}
            h["wallet"]["_on_health_page"] = True
        out["health"] = h
    if page == "index" and payload.get("findings"):
        # The preview is bounded, but totals describe the whole findings list.
        # Warnings remain visible when there are no criticals. Clear inherited
        # folding: a preview must not conceal rows behind nonexistent controls.
        f = dict(payload["findings"])
        items = f.get("items") or []
        rank = {"critical": 0, "warning": 1, "info": 2}
        ranked = sorted(items, key=lambda item: rank.get(item.get("severity"), 3))
        f["items"] = [{**x, "detail": "", "action": "", "folded": False}
                      for x in ranked[:OVERVIEW_FINDINGS_LIMIT]]
        f["folded_by_type"] = {}
        f["elsewhere"] = len(items) - len(f["items"])
        out["findings"] = f
    return out


#: Where the split pages keep the style and the script they all share. The
#: single page keeps both inline — it is read by `design` and `smoke` as one
#: file, and a self-contained page is the thing those checks check.
ASSET_CSS = "app.css"
ASSET_JS = "app.js"
#: The line in the template after which everything is shared. Above it live
#: `PAGE`, `TABLE_PAGES` and `D` — the three things that differ per page.
SHARED_FROM = "// __SHARED_BELOW__"


def split_template(template: str) -> tuple[str, str, str]:
    """(page template, the shared CSS, the shared JS).

    WHY THE SPLIT. Inlined, the style and the script are 114 KB of the 154 KB
    a page weighs, and nine pages carried nine copies — re-parsed on every
    navigation, which is the cost the split was supposed to remove. As two
    sibling files they are fetched once and served from cache thereafter, and
    an index that holds only its own data drops to ~45 KB (plan v2, T-02 DoD).
    Both are classic (non-module) subresources next to the page, so `file://`
    loads them like any browser loads a stylesheet — no fetch, no CORS.
    """
    o, c = template.index("<style>"), template.index("</style>")
    css = template[o + len("<style>"):c]
    page = (template[:o] + f'<link rel="stylesheet" href="{ASSET_CSS}">'
            + template[c + len("</style>"):])
    o = page.rindex("<script>") + len("<script>")
    c = page.rindex("</script>")
    body = page[o:c]
    cut = body.index(SHARED_FROM)
    page = (page[:o] + body[:cut] + '</script>\n'
            + f'<script src="{ASSET_JS}"></script>'
            + page[c + len("</script>"):])
    return page, css, body[cut:]


def page_html(template: str, page: str, payload: dict, locale: str = "en") -> str:
    """One page in the build locale. `template` is already localized (static
    marks filled by `i18n.localize_markup`); this fills the per-page parts."""
    t = Translator(locale)
    counts = counts_of(payload)
    data = json.dumps(slice_for(page, payload), ensure_ascii=False)
    title = dict((n, tt) for n, tt, _k in PAGES)[page]
    # D-20: the single page's tab strip is dead markup on a split
    # page — CSS hid it, the DOM still carried six buttons and six counters, and
    # `drawTab` wrote into them on every render. Stripped at build; the script
    # asks before writing a counter (`setN`), so the single page keeps its strip.
    template = re.sub(r'<nav class="tabs"[^>]*>.*?</nav>\s*', "", template, count=1, flags=re.S)
    if page == "index":
        # HOME (OSS-88, OBS-261): the state band, the three problems to do first
        # and the projects at a glance, server-rendered right under the heading,
        # so reading and keyboard order are the visual order. The script's
        # inventory counters and findings preview stay in the DOM for the single
        # page and are hidden here (workspace.css); activity follows Home.
        template = template.replace('<div class="tiles" id="tiles">',
                                    "__HOME__\n" + '<div class="tiles" id="tiles">', 1)
        # The activity tiles move into Home's Work group (OBS-551, CP-01): home_page draws their holder there,
        # so the template's own heading and holder go, and the page script fills the one `#work` left. A fresh
        # install's Home has no Work group, and the template keeps them.
        if payload.get("rows"):
            template = re.sub(r'\s*<h3 id="work-h"[^>]*>[^<]*</h3>\s*<div class="tiles work" id="work"></div>', "",
                              template, count=1)
    # The table renderer owns #out, not the document's main landmark. Home,
    # Findings and Health hide #out, so their primary content needs a shared
    # visible main as well. Scripts stay outside the content landmark.
    template = re.sub(r'<main(\s+id="out"[^>]*)></main>', r'<div\1></div>', template, count=1)
    template = template.replace("__NAV__", '__NAV__\n<main id="workspace" class="workspace" tabindex="-1">', 1)
    template = template.replace("</footer>", "</footer>\n</main>", 1)
    # ONE PASS. Each placeholder is filled exactly once and no inserted text is scanned
    # again: with chained `str.replace`, a project or finding title holding the literal
    # `__DATA__` (which `html.escape` leaves alone) was replaced by the raw payload JSON
    # in body context, a live tag on the page (review of #377). Unknown `__NAME__` tokens
    # (a comment in the script, a placeholder another builder fills) are left as they are.
    fills = {
        "PAGE": lambda: page,
        "TITLE": lambda: html.escape(f"{t(title)} · {TITLE_SUFFIX}"),
        "PAGE_TITLE": lambda: html.escape(title),
        "H1": lambda: t.mark(title, tag="h1"),
        "SUB": lambda: t.mark(QUESTIONS[page]) if page in QUESTIONS else "",
        "NAV": lambda: nav_html(page, counts, t),
        "HOME": lambda: _home(payload, t) if page == "index" else "",
        "CARDS": lambda: cards_html(payload, counts, t, HOME_COVERED) if page == "index" else "",
        "DATA": lambda: data.replace("</", "<\\/"),
    }
    kind = next(p.kind for p in PAGE_TABLE if p.name == page)
    for body_kind, (slot, *_rest) in BODIES.items():
        fills[slot] = (lambda k=body_kind: body_html(k, page, payload, t)) if kind == body_kind else (lambda: "")
    for section_page, (slot, module, func) in SECTIONS.items():
        fills[slot] = ((lambda m=module, f=func: getattr(importlib.import_module(m), f)(payload, t))
                       if page == section_page else (lambda: ""))
    return fill_placeholders(template, fills)


#: `__NAME__` in a page template: capitals, digits and inner underscores, shortest match.
PLACEHOLDER = re.compile(r"__([A-Z][A-Z0-9_]*?)__")


def fill_placeholders(template: str, fills: dict) -> str:
    """Every `__NAME__` with a fill, in one left-to-right pass; the text a fill returns is
    never scanned for placeholders, so data cannot name one. Each fill runs once."""
    done: dict[str, str] = {}

    def one(m: re.Match) -> str:
        name = m.group(1)
        if name not in fills:
            return m.group(0)
        if name not in done:
            done[name] = fills[name]()
        return done[name]
    return PLACEHOLDER.sub(one, template)
