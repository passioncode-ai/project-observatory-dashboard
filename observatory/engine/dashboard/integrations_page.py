"""The Integrations page (OBS-557, CP-12, CP-13, CP-14): are the held provider keys still accepted, did a provider
change the answer a collector relies on, which providers does a project use that nothing here watches, and which
issues were filed.

Rendered here, at build time, from `PAYLOAD["integrations"]`: the stored answer of `tools/integration_health.py`
(`store/raw/integration-health.json`) and the issue filer's map (`store/state/issues.json`). Grouped by provider:
each provider's keys (valid, refused, not reachable), then the answers that lost a field, the untracked providers
by project, and the issues filed. A key is shown by its slot name, never by value; a package or variable by its
name. An unread or stale answer is named under "Not fully read", never shown as a clean page.

THE BANNER. While the issue filer is on and the person has not dismissed it, a short notice says issues are filed in
a project's own repository and how to turn that off. Its Dismiss button is built disabled; served by the local
control server (the key server) the page script enables it and posts the `issues-banner` action, which stores the
dismissal. The notice is also on Home (`home_page.issues_notice_html`). No command line is shown on a row.

Every visible string is an English message id marked for the page script (L10N-01, L10N-05).
"""
from __future__ import annotations

from i18n import Translator
from machine_page import EMPTY, HEADING, _table
from page_util import escape as _e

HINT = ' class="machine-hint"'
#: The tool an agent reads this with; named in the disabled control's tooltip.
TOOL = "observatory_issues"
#: A key's state in a reader's words, and the order a provider's keys are listed in.
STATE_WORDS = {"invalid": "refused", "unreachable": "not reachable", "unreadable": "value not readable",
               "valid": "accepted"}
STATE_ORDER = ("invalid", "unreachable", "unreadable", "valid")
KIND_WORDS = {"package": "package", "env": "variable"}
ISSUE_STATES = {"open": "open", "closed": "closed", "not-filed": "not filed"}


def banner_html(payload: dict, t: Translator) -> str:
    """The issue filer's one-time notice: on, where it writes, how to turn it off, and a Dismiss button."""
    issues = ((payload.get("integrations") or {}).get("issues") or {})
    if not (issues.get("banner") or {}).get("show"):
        return ""
    said = t.mark("Project Observatory opens issues in a project's own repository for dependency and integration "
                  "findings, and closes them when the finding closes. No key or value is ever written into one. "
                  "To turn it off, switch off github_issues in the settings, or one class of it.")
    button = ('<button type="button" class="chip-btn" data-issues-dismiss="1" disabled'
              + t.attr("title", "Dismissing needs the local control server; an agent uses observatory_issues")
              + f'>{t.mark("Dismiss")}</button>')
    return ('<section class="home-notice" id="issues-notice" aria-labelledby="issues-notice-h">'
            + t.mark("Issues are filed for findings", tag="h2", attrs=' class="home-h" id="issues-notice-h"')
            + f'<p>{said}</p><p>{button}</p></section>')


def _counts(view: dict) -> dict:
    keys = view.get("keys") or []
    return {"keys": len(keys), "invalid": sum(1 for k in keys if k.get("state") == "invalid"),
            "drift": len(view.get("drift") or []), "untracked": len(view.get("untracked") or []),
            "issues": sum(1 for i in (view.get("issues") or {}).get("issues") or [] if i.get("state") == "open")}


def tiles_html(view: dict, t: Translator) -> str:
    c = _counts(view)
    tiles = [(t.mark("Keys checked"), t.number(c["keys"]),
              t.mark("refused: {n}", n=c["invalid"])),
             (t.mark("Answers that changed"), t.number(c["drift"]), t.mark("a field a collector relies on is gone")),
             (t.mark("Providers nothing watches"), t.number(c["untracked"]), t.mark("by project")),
             (t.mark("Issues open"), t.number(c["issues"]), t.mark("filed in project repositories"))]
    cells = "".join(f'<div class="tile">{k}<b>{v}</b>{sub}</div>' for k, v, sub in tiles)
    return f'<div class="tiles machine-tiles">{cells}</div>'


def keys_html(view: dict, t: Translator) -> str:
    """Per provider, its keys: the slot name, the state, the day it was checked and, for a refused key, since when."""
    by: dict[str, list[dict]] = {}
    for k in view.get("keys") or []:
        by.setdefault(k.get("provider") or "", []).append(k)
    if not by:
        return (f'<section class="card panel">{t.mark("Keys by provider", tag="h2", attrs=HEADING)}'
                f'{t.mark("No held key was checked yet. Each key is asked once a day.", tag="p", attrs=EMPTY)}'
                "</section>")
    parts = []
    for provider in sorted(by):
        rows = sorted(by[provider], key=lambda k: (STATE_ORDER.index(k["state"]) if k.get("state") in STATE_ORDER
                                                   else len(STATE_ORDER), k.get("slot") or ""))
        table = [[f'<span class="mono">{_e(k.get("slot"))}</span>',
                  t.known(STATE_WORDS.get(k.get("state"), k.get("state"))),
                  t.number(k["status"]) if k.get("status") else "",
                  t.date(k.get("checked_on")) if k.get("checked_on") else "",
                  t.date(k["since"]) if k.get("since") else ""] for k in rows]
        heads = [("Key", False), ("State", False), ("Answer", True), ("Checked", False), ("Since", False)]
        parts.append(_provider_table(t, provider, heads, table))
    return "".join(parts)


def _provider_table(t: Translator, provider: str, heads, rows) -> str:
    th = "".join(f'<th scope="col">{t.mark(h)}</th>' for h, _n in heads)
    body = "".join("<tr>" + "".join(f'<td{t.attr("data-label", h)}>{c}</td>' for (h, _n), c in zip(heads, r))
                   + "</tr>" for r in rows)
    head = f'<h2{HEADING}>{_e(provider)}</h2>'
    return f'<section class="card panel">{head}<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></section>'


def drift_html(view: dict, t: Translator) -> str:
    rows = [[f'<span class="mono">{_e(d.get("collector"))}</span>', f'<span class="mono">{_e(d.get("field"))}</span>',
             t.date(d["last_seen"]) if d.get("last_seen") else t.mark("never seen"), t.number(d.get("rows") or 0)]
            for d in view.get("drift") or []]
    return _table(t, "Answers that changed shape", [("Collector", False), ("Field", False), ("Last seen", False),
                                                    ("Rows read", True)], rows,
                  "Every field a collector relies on is still in its stored answer.")


def untracked_html(view: dict, t: Translator, names: dict) -> str:
    rows = []
    for u in view.get("untracked") or []:
        what = ", ".join(e.get("name") or "" for e in u.get("evidence") or [])
        more = (u.get("evidence_total") or 0) - len(u.get("evidence") or [])
        rows.append([f'<a href="projects.html#{_e(u.get("project"))}">'
                     f'{_e(names.get(u.get("project")) or str(u.get("project")).removeprefix("project:"))}</a>',
                     _e(u.get("provider")), t.known(u.get("category")),
                     f'<span class="mono">{_e(what)}</span>' + (" " + t.mark("and {n} more", n=more) if more > 0 else "")])
    return _table(t, "Providers nothing watches", [("Project", False), ("Provider", False), ("Kind", False),
                                                    ("Seen as", False)], rows,
                  "Every provider a project uses has a door or a collector here.")


def issues_html(view: dict, t: Translator) -> str:
    issues = view.get("issues") or {}
    rows = []
    for i in issues.get("issues") or []:
        link = f'{_e(i.get("repo"))}#{_e(i.get("number"))}' if i.get("number") else _e(i.get("repo") or "")
        rows.append([f'<span class="mono">{link}</span>', t.known(ISSUE_STATES.get(i.get("state"), i.get("state"))),
                     f'<span class="mono">{_e(i.get("type"))}</span>',
                     t.date(i["filed_on"]) if i.get("filed_on") else "",
                     t.known(i.get("reason")) if i.get("reason") else ""])
    last = issues.get("lastRun") or {}
    note = ""
    if last:
        note = t.mark("Last run {date}: {created} filed, {closed} closed, {deferred} waiting for the next run.",
                      tag="p", attrs=HINT, date=last.get("date") or "", created=last.get("created") or 0,
                      closed=last.get("closed") or 0, deferred=last.get("deferred") or 0)
    table = _table(t, "Issues filed", [("Issue", False), ("State", False), ("Finding", False), ("Filed", False),
                                       ("Why not", False)], rows,
                   "No issue has been filed. A finding of an allowed class is filed once its project's "
                   "repository can be written to.")
    return table.replace("</section>", note + "</section>", 1) if note else table


def degraded_html(view: dict, t: Translator) -> str:
    rows = [d for d in (view.get("degraded") or []) + ((view.get("issues") or {}).get("degraded") or [])
            if isinstance(d, dict)]
    if not rows:
        return ""
    items = "".join(f"<li><span class=\"mono\">{_e(d.get('source'))}</span> {t.known(d.get('reason'))}</li>"
                    for d in rows)
    return (f'<section class="card panel">{t.mark("Not fully read", tag="h2", attrs=HEADING)}<ul>{items}</ul>'
            "</section>")


def integrations_html(payload: dict, t: Translator | None = None) -> str:
    t = t or Translator()
    view = payload.get("integrations") or {}
    names = {p.get("id"): p.get("name") for p in payload.get("rows") or [] if isinstance(p, dict)}
    when = view.get("checkedOn")
    parts = [banner_html(payload, t)]
    if when:
        parts.append(t.mark("Checked {date}", tag="p", attrs=' class="machine-at"', date=when))
    parts += [tiles_html(view, t), keys_html(view, t), drift_html(view, t), untracked_html(view, t, names),
              issues_html(view, t), degraded_html(view, t)]
    return f'<div id="integrations" class="machine">{"".join(parts)}</div>'


#: Values translated through a variable, so the catalogue test sees them (tests/test_i18n.py).
DYNAMIC = ("Key", "State", "Answer", "Checked", "Since", *STATE_WORDS.values(), *ISSUE_STATES.values(), "payments", "errors", "cloud", "analytics", "messaging", "ai")
