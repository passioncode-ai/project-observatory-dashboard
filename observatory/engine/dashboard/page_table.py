#!/usr/bin/env python3
"""The dashboard's page table: every page is declared once, here (OBS-572).

`shell.py` (the pages and their navigation), `build_dashboard.py` (the template's slots and the single
page's empty fills), `observatory.py` (the ignored build output of each page) and `tests/test_i18n.py` (the
modules whose message ids it reads) all derive their page lists from this file, so a new page is one
`Page` entry (plus one `BODIES` entry when a module of its own draws its body at build time).

It imports nothing from the dashboard so that `observatory.py` can read it without loading the shell.
"""
from __future__ import annotations
from typing import NamedTuple


class Page(NamedTuple):
    """One entry of the page table: the only place a page is declared (OBS-572).

    `kind` says how its body is made: `table` pages render one of the tab renderers into `#out`; the
    others show the shell's own sections (`BODIES` names the build-time ones). `question` is THE QUESTION
    THE PAGE ANSWERS, in the reader's words (IS-01/IS-02; backlog D-03): nine pages shared one `<title>`
    and one `<h1>` until 2026-09-14, so a browser tab, a bookmark and the history could not tell them
    apart. The sentence is the page's own heading; the measurement stamp follows it on every page.
    `group` and `nav` place it in the navigation: the group key and its rank inside the group. Titles
    and questions are English message ids; `dashboard/locales/*.json` translates them."""
    name: str
    title: str
    kind: str
    question: str
    group: str
    nav: int


#: The row order is the order of the page list, of the build and of Home's cards: Home first (OSS-88),
#: then what needs a person most often. A new page is one entry here (and one entry in `BODIES` when its
#: body is drawn at build time by a module of its own).
PAGE_TABLE: tuple[Page, ...] = (
    Page("index", "Home", "index", "Is everything fine, and what needs you first.", "work", 0),
    Page("findings", "Findings", "findings", "Problems, next actions and the history of silenced findings.",
         "work", 3),
    Page("projects", "Projects", "table", "Projects, their state, resources and latest changes.", "work", 1),
    Page("agents", "Agents", "agents", "Which agents are working, on what, and where their work moved.",
         "work", 2),
    Page("domains", "Domains", "table", "Domains, reachability, expiry and linked projects.",
         "infrastructure", 3),
    Page("heroku", "Heroku", "table", "Apps, state, deployments and cost.", "infrastructure", 0),
    Page("digitalocean", "DigitalOcean", "infra",
         "Droplets, apps, databases and volumes: state, project and cost.", "infrastructure", 1),
    Page("cloudflare", "Cloudflare", "infra", "Workers, Pages, R2, D1 and zones: state, project and cost.",
         "infrastructure", 2),
    Page("creds", "Keys", "table", "Keys, their purpose, state and the actions available.", "access", 0),
    Page("env", "ENV", "table", "Variables by project, compared with the running environments.", "access", 1),
    Page("mcp", "MCP", "table", "Agent servers, connections and reachability.", "access", 2),
    Page("traffic", "Traffic", "table", "Product audiences, data sources and linked projects.",
         "infrastructure", 4),
    Page("health", "Health", "health", "Observer state, data freshness and the agents' records.",
         "monitoring", 0),
    Page("machine", "Machine", "machine",
         "What runs on this machine, where memory and disk go, and what was cleaned.", "system", 0),
    Page("tools", "Installed tools", "tools",
         "Which coding agents, skills, plugins and hooks are installed here, and which are out of date.",
         "system", 1),
    Page("disk", "Disk", "disk",
         "How much space is free, what the guard and auto cleanup removed and keep, and how they are set.",
         "system", 2),
)
#: Navigation entries that are a narrowed view of a page, not a page: (name, title, address, group, rank).
#: Security and Availability are not pages of their own: each opens the Findings page narrowed to its group
#: (`finding_types.GROUPS`), so a finding has one home and two ways in.
NAV_VIEWS: tuple[tuple[str, str, str, str, int], ...] = (
    ("security", "Security", "findings.html?group=security", "monitoring", 1),
    ("availability", "Availability", "findings.html?group=availability", "monitoring", 2),
)
#: MONITORING FIRST (OBS-551, CP-17): what is wrong right now, then the work, then what it runs on.
NAV_GROUP_LABELS = (
    ("monitoring", "Monitoring"),
    ("work", "Work"),
    ("infrastructure", "Infrastructure"),
    ("access", "Access"),
    ("system", "System"),
)
#: What each page kind draws at build time: kind -> (template slot, dashboard module, function, takes the
#: page name). The slots are the `__NAME__` lines of the template (`SLOTS`, in this order); Home has its own
#: slot, filled where `page_html` puts it.
BODIES: dict[str, tuple[str, str, str, bool]] = {
    "machine": ("MACHINE", "machine_page", "machine_html", False),
    "tools": ("TOOLS", "tools_page", "tools_html", False),
    "disk": ("DISK", "disk_page", "disk_html", False),
    "agents": ("AGENTS", "agents_page", "agents_html", False),
    "infra": ("INFRA", "infra_page", "infra_html", True),
    # The Findings page's build-time section above its list: keys found in repositories (OBS-554).
    "findings": ("FINDINGS", "key_sweep_page", "key_sweep_html", False),
}
HOME_BODY = ("home_page", "home_html")
#: The slots a template carries and the single page empties (`build_dashboard.py`), in template order.
SLOTS = tuple(slot for slot, _m, _f, _p in BODIES.values())
#: The dashboard modules that draw a page body at build time (`test_i18n.py` reads their message ids).
PAGE_MODULES = tuple(m for _s, m, _f, _p in BODIES.values()) + (HOME_BODY[0],)

PAGES: tuple[tuple[str, str, str], ...] = tuple((p.name, p.title, p.kind) for p in PAGE_TABLE)
QUESTIONS: dict[str, str] = {p.name: p.question for p in PAGE_TABLE}

def _nav_groups() -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """(key, label, entry names) in navigation order, from the page table and the narrowed views."""
    entries = [(p.group, p.nav, p.name) for p in PAGE_TABLE] + [(g, r, n) for n, _t, _h, g, r in NAV_VIEWS]
    return tuple((key, label, tuple(n for g, _r, n in sorted((e for e in entries if e[0] == key),
                                                              key=lambda e: e[1])))
                 for key, label in NAV_GROUP_LABELS)


NAV_GROUPS = _nav_groups()
NAV_LINKS: dict[str, tuple[str, str]] = {name: (title, href) for name, title, href, _g, _r in NAV_VIEWS}
