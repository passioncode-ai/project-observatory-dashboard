"""The DigitalOcean and Cloudflare pages: one shape beside Heroku (OBS-235, OBS-271).

Rendered here, at build time, from `registry/digitalocean-account.json` and
`registry/cloudflare-account.json` (`collectors/infra_registry.py`). Every page has the
same columns, resource, project, account, state, cost and findings, so the three
infrastructure pages read as one product (PERSON-SURFACES.md, Infrastructure; IS-18).

The page reads and never acts. Its four states (PERSON-SURFACES.md, "Measured and not
measured are different states"):

  not connected  never scanned: what switching it on and connecting an account takes,
                 with the door's own commands;
  failed         the scan named what it could not read: each reason, above whatever it
                 did read, with the command that scans again; a scan that read nothing is
                 never "no resources";
  empty          read, and the accounts hold nothing: said in words, never a row of zeros;
  full           the tiles, the accounts and the resource table.

Loading is the static page plus the live server's refresh, as on every other page.
Every visible string is an English message id marked for the page script (L10N-01).
"""
from __future__ import annotations

import html

from i18n import Translator

NUM = ' class="num"'
EMPTY = ' class="empty"'
HEADING = ' class="machine-h"'
CHIP = ' class="chip"'
DOOR = 'python3 "$(project-observatory full-path)/tools/{door}.py"'

#: Per provider: the page's own words and the commands that connect it.
PROVIDERS = {
    "digitalocean": {
        "name": "DigitalOcean",
        "off": "DigitalOcean is not connected. Switch it on, then connect each team with a read-only token:",
        "switched_off": "DigitalOcean is switched off. Below is the last read, from {date}; switch it on to read again:",
        "unread": "Part of DigitalOcean could not be read:",
        "integration": "project-observatory full configure integrations digitalocean true",
        "scan": "project-observatory full scan-digitalocean",
        "connect": (DOOR.format(door="digitalocean") + " issue --preset observatory-read --team <team>",
                    DOOR.format(door="digitalocean") + " install --preset observatory-read --team <team> < token-file"),
    },
    "cloudflare": {
        "name": "Cloudflare",
        "off": "Cloudflare's account inventory is not connected. Switch it on, then issue a read-only reader for each account:",
        "switched_off": "Cloudflare is switched off. Below is the last read, from {date}; switch it on to read again:",
        "unread": "Part of Cloudflare could not be read:",
        "integration": "project-observatory full configure integrations cloudflare true",
        "scan": "project-observatory full scan-cloudflare-account",
        "connect": (DOOR.format(door="cloudflare") + " stash < admin-token-file",
                    DOOR.format(door="cloudflare") + " issue --preset inventory-read --account <account>"),
    },
}
#: The reader's word for each kind of resource.
KINDS = {"droplet": "Droplet", "app": "App Platform app", "database": "Managed database",
         "volume": "Volume", "worker": "Worker", "pages": "Pages project", "r2": "R2 bucket",
         "d1": "D1 database", "zone": "Zone"}
#: The state vocabulary of `infra_registry.STATES`, as the page reads it.
STATE_WORDS = {s: f"infra-state@@{s}" for s in ("running", "off", "down", "pending", "idle", "listed")}
STATE_CLASS = {"running": "ok", "off": "warn", "down": "danger", "pending": "", "idle": "", "listed": ""}
#: Engine-written sentences a row carries (`infra_registry`), translated through `t.known`.
REASONS = ("the provider names no repository it deploys from",
           "it deploys from a repository this registry does not tie to a project",
           "the provider records nothing that ties it to a repository or a site",
           "no project's site is on this zone",
           "the provider's listing states no price for this resource",
           "the plan states no monthly USD price")


def _e(v) -> str:
    return html.escape("" if v is None else str(v))


def _command(text: str, t: Translator) -> str:
    return (f'<p><button class="chip-btn mono" type="button" data-copy="{_e(text)}"'
            f'{t.attr("title", "copy the command")}>{_e(text)}</button></p>')


def _money(value, t: Translator) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return t.mark("not stated")
    return "$" + t.number(round(float(value), 2))


def _cost_total(tt: dict, t: Translator) -> str:
    """A provider's stated monthly total: "not stated" when nothing states a price, "at
    least $X" when some rows state none (the sum is a floor), else the sum."""
    cost = tt.get("monthly_cost")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        return t.mark("not stated")
    if tt.get("monthly_cost_partial"):
        return t.mark("at least ${amount}", amount=round(float(cost), 2))
    return _money(cost, t)


def _table(t: Translator, caption: str, heads: list[tuple[str, bool]], rows: list[list[str]], empty: str) -> str:
    head = t.mark(caption, tag="h2", attrs=HEADING)
    if not rows:
        return f'<section class="card panel">{head}{t.mark(empty, tag="p", attrs=EMPTY)}</section>'
    th = "".join(f'<th scope="col"{NUM if num else ""}>{t.mark(h)}</th>' for h, num in heads)
    body = []
    for r in rows:
        cells = "".join(f'<td{NUM if num else ""}{t.attr("data-label", h)}>{c}</td>'
                        for (h, num), c in zip(heads, r))
        body.append(f"<tr>{cells}</tr>")
    return (f'<section class="card panel">{head}<table><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></section>')


def _findings(payload: dict, provider: str) -> list[dict]:
    """Open findings that concern this provider: a subject in its namespace, a type of its
    rules, or a credential of its door (the analytics and inventory tokens)."""
    items = ((payload.get("findings") or {}).get("items")) or []
    out = []
    for f in items:
        subject, kind = str(f.get("subject") or ""), str(f.get("type") or "")
        if (subject.startswith(provider + ":") or kind.startswith(provider + ".")
                or subject.startswith(f"credential:{provider}/") or subject.startswith(f"estate:{provider}")
                or subject.startswith(("domain:", "zone:")) and provider == "cloudflare"):
            out.append(f)
    return out


def _title(f: dict, t: Translator) -> str:
    if f.get("title_id"):
        return t.mark(f["title_id"], **(f.get("title_args") or {}))
    return _e(f.get("title") or f.get("type"))


def _finding_link(f: dict, t: Translator) -> str:
    sev = f.get("severity") or "info"
    cls = {"critical": "danger", "warning": "warn"}.get(sev, "")
    return (f'<a class="chip {cls}" href="findings.html#{_e(f.get("id"))}">{_title(f, t)}</a>')


def _not_connected(spec: dict, integration_on: bool, t: Translator) -> str:
    if integration_on:
        lines = [t.mark("Not measured yet. Run the scan, then rebuild the registry and the pages:", tag="p"),
                 _command(spec["scan"], t), _command("project-observatory full local", t)]
    else:
        lines = [t.mark(spec["off"], tag="p"), _command(spec["integration"], t)]
        lines += [_command(c, t) for c in spec["connect"]]
        lines.append(_command(spec["scan"], t))
    return f'<section class="card panel empty not-scanned">{"".join(lines)}</section>'


def _degraded(doc: dict, spec: dict, t: Translator) -> str:
    gaps = [d for d in doc.get("degraded") or [] if isinstance(d, dict) and d.get("reason")]
    if not gaps:
        return ""
    items = "".join(f'<li><span class="mono">{_e(d.get("source"))}</span>: {t.known(d.get("reason"))}</li>'
                    for d in gaps)
    unread = not (doc.get("resources") or [])
    lead = t.mark("Nothing was read. What stopped it:", tag="p") if unread else t.mark(spec["unread"], tag="p")
    connect = ("".join(_command(c, t) for c in spec["connect"]) if unread else "")
    return (f'<section class="card panel empty not-scanned degraded-note">{lead}<ul>{items}</ul>'
            f'{t.mark("Connect an account, or scan again:", tag="p") if unread else t.mark("Scan again:", tag="p")}'
            f'{connect}{_command(spec["scan"], t)}</section>')


def _tiles(doc: dict, provider: str, t: Translator) -> str:
    tt = doc.get("totals") or {}
    by = tt.get("by_state") or {}
    tiles = [
        (t.mark("Resources"), t.number(tt.get("resources", 0)),
         t.mark("linked to a project: {n}", n=tt.get("linked_to_a_project", 0))),
        (t.mark("Running"), t.number(by.get("running", 0)),
         t.mark("listed only: {n}", n=by.get("listed", 0))),
        (t.mark("Off or down"), t.number((by.get("off") or 0) + (by.get("down") or 0)),
         t.mark("pending or idle: {n}", n=(by.get("pending") or 0) + (by.get("idle") or 0))),
        (t.mark("Stated monthly cost"), _cost_total(tt, t),
         t.mark("without a stated price: {n}", n=tt.get("cost_unknown", 0))),
    ]
    if provider == "digitalocean":
        tiles.append((t.mark("Month to date"), _money(tt.get("month_to_date_usage"), t),
                      t.mark("from the teams' balance")))
    cells = "".join(f'<div class="tile">{k}<b>{v}</b>{sub}</div>' for k, v, sub in tiles)
    return f'<div class="tiles machine-tiles">{cells}</div>'


def _accounts(doc: dict, provider: str, t: Translator) -> str:
    rows = []
    for a in doc.get("accounts") or []:
        unread = _e(", ".join(a.get("unread") or [])) or t.mark("none")
        if provider == "digitalocean":
            extra = _money(a.get("month_to_date_usage"), t)
            reader = _e(a.get("token_kind")) or t.mark("none")
        else:
            extra = t.date(a.get("token_expires_at")) if a.get("token_expires_at") else t.mark("not stated")
            reader = "inventory-read"
        rows.append([_e(a.get("name") or a.get("label")), f'<span class="mono">{reader}</span>', extra,
                     f'<span class="mono">{unread}</span>'])
    third = "Month to date" if provider == "digitalocean" else "Reader expires"
    return _table(t, "Accounts", [("Account", False), ("Reader", False), (third, provider == "digitalocean"),
                                  ("Not read", False)], rows, "No account is connected.")


def _detail(r: dict, t: Translator) -> str:
    """The one line under a resource's name: what a reader asks of that kind."""
    bits = []
    if r.get("kind") == "zone":
        if r.get("plan"):
            bits.append(_e(r["plan"]))
        if isinstance(r.get("dns_records"), int):
            bits.append(t.mark("DNS records: {n}", n=r["dns_records"]))
        if r.get("certificate_expires_on"):
            bits.append(t.mark("certificate expires {date}", date=r["certificate_expires_on"]))
    for key in ("region", "size", "engine", "host", "location"):
        if r.get(key):
            bits.append(f'<span class="mono">{_e(r[key])}</span>')
    if not bits:
        return ""
    return '<div class="machine-hint">' + " · ".join(bits) + "</div>"


def _resources(doc: dict, findings: list[dict], names: dict, t: Translator) -> str:
    by_subject: dict[str, list[dict]] = {}
    for f in findings:
        by_subject.setdefault(str(f.get("subject")), []).append(f)
    rows = []
    for r in doc.get("resources") or []:
        mine = [f for s in r.get("subjects") or [r.get("id")] for f in by_subject.get(s, [])]
        state = r.get("state")
        word = t.mark(STATE_WORDS.get(state, "infra-state@@listed"))
        provider_state = (f' <span class="mono">{_e(r.get("provider_state"))}</span>'
                          if r.get("provider_state") and r.get("provider_state") != state else "")
        if r.get("project"):
            pid = r["project"]
            project = (f'<a href="projects.html#{_e(pid)}">{_e(names.get(pid) or pid.removeprefix("project:"))}</a>'
                       f' <span class="mono">{_e(r.get("link_rule"))}</span>')
        else:
            project = t.mark("no project") + f'<div class="machine-hint">{t.known(r.get("unlinked_reason"))}</div>'
        cost = (_money(r["monthly_cost"], t) if isinstance(r.get("monthly_cost"), (int, float))
                else t.mark("not stated") + f'<div class="machine-hint">{t.known(r.get("cost_note"))}</div>')
        rows.append([
            f'<b id="{_e(r.get("id"))}">{_e(r.get("name"))}</b> {t.mark(KINDS.get(r.get("kind"), "Resource"), attrs=CHIP)}'
            + _detail(r, t),
            project, _e(r.get("account")),
            f'<span class="chip {STATE_CLASS.get(state, "")}">{word}</span>{provider_state}',
            cost,
            " ".join(_finding_link(f, t) for f in mine) or t.mark("no findings"),
        ])
    return _table(t, "Resources",
                  [("Resource", False), ("Project", False), ("Account", False), ("State", False),
                   ("Cost", True), ("Findings", False)],
                  rows, "No resources in the connected accounts.")


def _other_findings(findings: list[dict], doc: dict, t: Translator) -> str:
    named = {s for r in doc.get("resources") or [] for s in r.get("subjects") or [r.get("id")]}
    rest = [f for f in findings if str(f.get("subject")) not in named
            and not str(f.get("subject")).startswith(("domain:", "zone:"))]
    if not rest:
        return ""
    return _table(t, "Findings that name no resource on this page", [("Finding", False)],
                  [[_finding_link(f, t)] for f in rest], "")


def infra_html(page: str, payload: dict, t: Translator | None = None) -> str:
    """The body of the DigitalOcean or the Cloudflare page."""
    t = t or Translator()
    spec = PROVIDERS[page]
    doc = payload.get(page)
    names = {r.get("id"): r.get("name") for r in payload.get("rows") or []}
    if not doc:
        on = bool((payload.get("infra_integrations") or {}).get(page))
        return f'<section id="infra" class="infra">{_not_connected(spec, on, t)}</section>'
    findings = _findings(payload, page)
    parts = [_degraded(doc, spec, t)]
    if not (payload.get("infra_integrations") or {}).get(page):
        # Switched off after a read: the last read stays, never shown as current.
        parts.insert(0, '<section class="card panel empty not-scanned">'
                     + t.mark(spec["switched_off"], tag="p", date=str(doc.get("scanned_on") or ""))
                     + _command(spec["integration"], t) + "</section>")
    unread = [k for a in doc.get("accounts") or [] for k in a.get("unread") or []]
    if not doc.get("resources") and unread:
        # Connected but not read (a refused token, a deadline before the first listing): the
        # accounts say what was not read; tiles of zeros and "no resources" would claim a read.
        parts += [_accounts(doc, page, t), _other_findings(findings, doc, t)]
    elif doc.get("resources") or doc.get("accounts"):
        parts += [_tiles(doc, page, t), _accounts(doc, page, t), _resources(doc, findings, names, t),
                  _other_findings(findings, doc, t)]
    stamp = t.mark("Scanned {date}", date=str(doc.get("scanned_on") or ""), tag="p", attrs=' class="machine-at"')
    parts.append(stamp)
    parts.append(t.mark("Connect another account:", tag="p", attrs=' class="machine-hint"')
                 + "".join(_command(c, t) for c in spec["connect"]))
    return f'<section id="infra" class="infra">{"".join(parts)}</section>'


#: Ids reached through a variable, so the catalog test can see them (tests/test_i18n.py).
DYNAMIC = (tuple(KINDS.values()) + tuple(STATE_WORDS.values()) + REASONS
           + tuple(p[k] for p in PROVIDERS.values() for k in ("off", "switched_off", "unread"))
           + ("Resource", "Month to date", "Reader expires"))
