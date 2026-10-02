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
import json
import re
from pathlib import Path

from i18n import LOCALE_NAMES, LOCALES, Translator

#: (name, title, kind). `table` pages render one of the tab renderers into
#: `#out`; the three others show the shell's own sections. Order is the nav
#: order: the overview first, then what needs a person most often.
#: Titles are English message ids; `dashboard/locales/*.json` translates them.
PAGES: tuple[tuple[str, str, str], ...] = (
    ("index",    "Overview",  "index"),
    ("findings", "Findings",  "findings"),
    ("projects", "Projects",  "table"),
    ("domains",  "Domains",   "table"),
    ("heroku",   "Heroku",    "table"),
    ("creds",    "Keys",      "table"),
    ("env",      "ENV",       "table"),
    ("mcp",      "MCP",       "table"),
    ("traffic",  "Traffic",   "table"),
    ("health",   "Health",    "health"),
    ("machine",  "Machine",   "machine"),
)
#: THE QUESTION EACH PAGE ANSWERS, in the reader's words (IS-01/IS-02; backlog
#: D-03). Nine pages shared one `<title>` and one `<h1>` until 2026-09-14: a
#: browser tab, a bookmark and the history could not tell them apart, and no
#: page said what it showed or over what scope. The sentence is the page's own
#: heading; the measurement stamp follows it on every page.
QUESTIONS: dict[str, str] = {
    "index":    "What needs attention and what changed across your projects.",
    "findings": "Problems, next actions and the history of silenced findings.",
    "projects": "Projects, their state, resources and latest changes.",
    "domains":  "Domains, reachability, expiry and linked projects.",
    "heroku":   "Apps, state, deployments and cost.",
    "creds":    "Keys, their purpose, state and the actions available.",
    "env":      "Variables by project, compared with the running environments.",
    "mcp":      "Agent servers, connections and reachability.",
    "traffic":  "Product audiences, data sources and linked projects.",
    "health":   "Observer state, data freshness and the decision queue.",
    "machine":  "What runs on this machine, where memory and disk go, and what was cleaned.",
}
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
NAV_GROUPS = (
    ("work", "Work", ("index", "projects", "findings")),
    ("infrastructure", "Infrastructure", ("heroku", "domains", "traffic")),
    ("access", "Access", ("creds", "env", "mcp")),
    ("system", "System", ("health", "machine")),
)
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


def _machine(payload: dict, t: Translator) -> str:
    """The Machine page's body, rendered at build time (dashboard/machine_page.py)."""
    import machine_page
    return machine_page.machine_html(payload, t)


def locale_switch_html(t: Translator) -> str:
    """EN/RU: the reader's language. Each button names its language in itself,
    and the page script keeps the choice (see `LOCALE_KEY` in the template)."""
    buttons = "".join(
        f'<button type="button" data-locale="{code}" lang="{code}" aria-pressed="false"'
        f' title="{html.escape(LOCALE_NAMES[code])}">{code.upper()}</button>'
        for code in LOCALES)
    return f'<div class="locale-switch" role="group"{t.attr("aria-label", "Language")}>{buttons}</div>'


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
    # page it names never count two different units. Overview and Health
    # are summaries, not inventories; severity and review totals belong in
    # their labelled content rather than beside an unrelated route name.
    return {
        "index": "",
        "health": "",
        "findings": open_findings or "",
        "projects": len(payload.get("rows") or []),
        "domains": domain_totals(payload.get("domains") or [], payload.get("zones") or [])[0],
        "heroku": len(heroku.get("apps") or []) if heroku else "",
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
    if age < 90:
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


def cards_html(payload: dict, counts: dict, t: Translator | None = None) -> str:
    """The index page's module cards: one sentence each, the number, the link."""
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
        "creds": t.mark("{n} entries", n=counts["creds"] or 0),
        # The badge's number with its unit, then the population it is drawn from.
        "env": t.mark("{n} variables", n=counts["env"] or 0) + " · "
               + t.mark("{n} read as a secret", n=((payload.get("env") or {}).get("totals") or {}).get("secrets", 0)),
        "mcp": t.mark("{n} declarations", n=counts["mcp"] or 0) + " · "
               + t.mark("{n} servers", n=((payload.get("mcp") or {}).get("totals") or {}).get("distinct_servers", 0)),
        "traffic": _traffic_line(payload, t),
        "machine": _machine_line(payload, t),
        # TWO NUMBERS, because one of them is the reason to open the page: the
        # observer's state, and how many rows are waiting for a person (S4/F9).
        "health": (_observer(health, t) + " · "
                   + t.mark("awaiting a decision: {n}", n=health.get("proposed", 0))),
    }
    # A page nobody scanned says so on its card, instead of "0 apps".
    for name, key in (("heroku", "heroku"), ("creds", "creds"), ("env", "env"), ("mcp", "mcp")):
        if payload.get(key) is None:
            lines[name] = t.mark("not scanned")
    cards = []
    for name, title, _kind in PAGES:
        if name == "index":
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
    heavy = {"env": "env", "heroku": "heroku", "creds": "creds", "mcp": "mcp",
             "google": "traffic", "machine": "machine"}
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
        # Reading and keyboard order must match the visual attention-first
        # layout. CSS order alone would leave assistive navigation behind the
        # less important inventory and activity counters.
        attention = '<section id="findings"></section>'
        template = template.replace(attention, "", 1)
        template = template.replace('<div class="tiles" id="tiles">',
                                    attention + '\n<div class="tiles" id="tiles">', 1)
    # The table renderer owns #out, not the document's main landmark. Overview,
    # Findings and Health hide #out, so their primary content needs a shared
    # visible main as well. Scripts stay outside the content landmark.
    template = re.sub(r'<main(\s+id="out"[^>]*)></main>', r'<div\1></div>', template, count=1)
    template = template.replace("__NAV__", '__NAV__\n<main id="workspace" class="workspace" tabindex="-1">', 1)
    template = template.replace("</footer>", "</footer>\n</main>", 1)
    return (template.replace("__PAGE__", page)
            .replace("__TITLE__", html.escape(f"{t(title)} — {TITLE_SUFFIX}"))
            .replace("__PAGE_TITLE__", html.escape(title))
            .replace("__H1__", t.mark(title, tag="h1"))
            .replace("__SUB__", t.mark(QUESTIONS[page]) if page in QUESTIONS else "")
            .replace("__NAV__", nav_html(page, counts, t))
            .replace("__CARDS__", cards_html(payload, counts, t) if page == "index" else "")
            .replace("__MACHINE__", _machine(payload, t) if page == "machine" else "")
            .replace("__DATA__", data.replace("</", "<\\/")))
