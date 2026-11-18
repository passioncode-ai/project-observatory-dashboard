"""The Assistant page (OBS-560, CP-19): the built-in agent panel on the live dashboard.

Three parts, one panel. The read-only assistant's chat; the daily doctor's last run (its fixes, each with an undo, and
what it only reports); and the security watch (the open findings of the security group, by area, with a link to them).

Rendered here, at build time: the doctor's view (`PAYLOAD["doctor"]`, `daily_doctor.agent_view`, what
`observatory_doctor` answers) and the open findings (`PAYLOAD["findings"]`). Neither rides on a page's data: the page
script reads nothing of them.

THE CHAT IS LIVE ONLY. Its conversations are private to the workspace, so none is written into a built page. Served by
the local control server (the key server) with its token, the page script asks the key server's `assistant` action:
`status` (whether a question can be asked now, and why not, with today's spend against the ceiling), `ask`, `job`,
`cancel` and `conversation`, the functions the assistant's MCP tools call. The Undo beside a fix posts the `doctor`
action, `observatory_doctor`'s undo. Opened as a file, every control is built disabled and the page says why. No command
line is shown on a row; the doctor's degraded reasons, which can name one, are shown by their source alone.

Every visible string is an English message id marked for the page script (L10N-01, L10N-05).
"""
from __future__ import annotations

from i18n import Translator
from machine_page import EMPTY, HEADING, _table
from page_util import escape as _e

HINT = ' class="machine-hint"'
#: The tools an agent uses for what each control does; named in the disabled controls' tooltips.
ASK_TOOL = "observatory_assistant_ask"
UNDO_TOOL = "observatory_doctor"
#: Each check of the doctor's catalogue in a reader's words (`daily_doctor.CATALOGUE`); an unknown id is shown as is.
CHECK_WORDS = {
    "temp_orphans": "Temporary files left by an interrupted write",
    "receipt_unreadable": "Scan receipts that do not parse",
    "backup_stale": "The workspace backup is older than 48 hours",
    "step_failing": "A maintenance step failing pass after pass",
    "vault_meta": "Vault metadata files that cannot be read",
    "scan_fresh": "No scan for three hours",
    "mcp_doors": "Engine servers running replaced code",
    "plugin_behind": "Installed plugins behind their marketplace",
}
STATE_WORDS = {"fixed": "fixed", "finding": "reported", "fix-failed": "fix failed", "unknown": "not read",
               "ok": "fine", "would-fix": "would fix"}
#: The checks a person reads: a fix, a fix that failed, a finding and an unread source. An `ok` check is a count.
LISTED = ("fixed", "fix-failed", "finding", "unknown")
#: The security watch's areas, in order, and the finding type prefixes each takes (`finding_types.GROUPS`, security).
AREAS = ("keys", "supply", "dependencies", "integrations")
AREA_WORDS = {"keys": "Keys and secrets", "supply": "Supply chain", "dependencies": "Dependencies",
              "integrations": "Integrations"}
_AREA_PREFIXES = (("mcp.risky_command", "supply"), ("mcp.tool_changed", "supply"), ("mcp.unpinned_server", "supply"),
                  ("harness.hook_risk", "supply"),
                  ("harness.skill_risk", "supply"), ("dep.", "dependencies"), ("integration.", "integrations"))
SEVERITIES = ("critical", "warning", "info")


def area_of(kind: str) -> str:
    """The security watch's area of a security finding type: supply chain, dependencies or integrations by name, and
    every other security type (keys, secrets, env files, SSH keys, a key in an MCP address or a plaintext secret in an
    agent's configuration) under keys and secrets."""
    for prefix, area in _AREA_PREFIXES:
        if kind == prefix or (prefix.endswith(".") and kind.startswith(prefix)):
            return area
    return "keys"


def chat_html(t: Translator) -> str:
    """The chat: conversations, the transcript, the question and the ask button, all disabled until the page is live."""
    off = t.attr("title", "Asking needs the local control server; an agent uses {tool}", tool=ASK_TOOL)
    new = (f'<button type="button" class="chip-btn" id="assistant-new" disabled{off}>'
           f'{t.mark("New conversation")}</button>')
    blank = t.mark("New conversation", tag="option", attrs=' value=""')
    pick = (f'<label for="assistant-conv">{t.mark("Conversation")}</label> '
            f'<select id="assistant-conv" disabled{off}>{blank}</select>')
    form = (f'<form id="assistant-form" class="assistant-form">'
            f'<label for="assistant-q">{t.mark("Your question")}</label>'
            f'<textarea id="assistant-q" rows="3" maxlength="6000" disabled{off}></textarea>'
            f'<p class="assistant-actions"><button type="submit" class="chip-btn" id="assistant-send" disabled{off}>'
            f'{t.mark("Ask")}</button> '
            f'<button type="button" class="chip-btn" id="assistant-stop" hidden>{t.mark("Stop")}</button></p></form>')
    cost = t.mark("Each question sends it and a bounded set of local facts to the configured model, and is paid "
                  "from the engine's model budget. The assistant reads; it changes nothing.", tag="p",
                  attrs=HINT + ' id="assistant-cost"')
    live = t.mark("This page was opened as a file: the assistant answers when the local control server serves it.",
                  tag="p", attrs=' id="assistant-live" class="machine-at"')
    return ('<section class="card panel" id="assistant-chat" aria-labelledby="assistant-chat-h">'
            + t.mark("Ask about the estate", tag="h2", attrs=HEADING + ' id="assistant-chat-h"')
            + live + f'<p class="assistant-convs">{pick} {new}</p>'
            + '<div id="assistant-transcript" class="assistant-transcript" aria-live="polite"></div>'
            + form + cost + '<p id="assistant-said" role="status"></p></section>')


def _check_name(c: dict, t: Translator) -> str:
    words = CHECK_WORDS.get(c.get("id"))
    return t.mark(words) if words else _e(c.get("title") or c.get("id"))


def doctor_html(view: dict | None, t: Translator) -> str:
    """The doctor's last run: its summary, the fixes an undo can still reverse, and the checks a person reads."""
    head = t.mark("The daily doctor", tag="h2", attrs=HEADING)
    view = view or {}
    if not view.get("at"):
        return (f'<section class="card panel" id="assistant-doctor">{head}'
                + t.mark("The daily doctor has not run yet. The maintenance job runs it once a day.",
                         tag="p", attrs=EMPTY) + "</section>")
    parts = [f'<p class="machine-at">{t.mark("Last run")} {t.date(view["at"])}</p>']
    summary = view.get("summary") or {}
    if summary.get("state") == "written" and summary.get("text"):
        parts.append(f'<p class="assistant-summary">{_e(summary["text"])}</p>')
    checks = view.get("checks") or []
    fine = sum(1 for c in checks if c.get("state") == "ok")
    if checks:
        parts.append(t.mark("Checks that found nothing to do: {n} of {total}", tag="p", attrs=HINT, n=fine,
                            total=len(checks)))
    undo_off = t.attr("title", "Undoing needs the local control server; an agent uses {tool}", tool=UNDO_TOOL)
    rows = []
    for fix in view.get("undoable") or []:
        jid = str(fix.get("journal_id") or "")
        check = {"id": fix.get("check")}
        rows.append([_check_name(check, t), t.number(len(fix.get("files") or [])),
                     t.date(fix["at"]) if fix.get("at") else "",
                     f'<button type="button" class="chip-btn" data-doctor-undo="{_e(jid)}" disabled{undo_off}>'
                     f'{t.mark("Undo")}</button> <span data-doctor-said="{_e(jid)}"></span>'])
    fixes = _table(t, "Fixes that can be undone", [("Check", False), ("Files moved aside", True), ("When", False),
                                                    ("Action", False)], rows,
                   "No fix waits for an undo.")
    parts.append(_inner(fixes))
    listed = []
    for c in checks:
        if c.get("state") not in LISTED or c.get("state") == "fixed":
            continue
        where = (t.mark("already raised by another rule") if c.get("covered_by")
                 else t.mark("a finding of its own") if c.get("finding") else "")
        listed.append([_check_name(c, t), t.known(STATE_WORDS.get(c.get("state"), c.get("state"))),
                       t.number(c.get("count") or 0), where])
    reported = _table(t, "What it only reports", [("Check", False), ("State", False), ("Count", True),
                                                   ("Where", False)], listed,
                      "Every check either found nothing or fixed it.")
    parts.append(_inner(reported))
    gaps = sorted({str(d.get("source")) for d in view.get("degraded") or [] if isinstance(d, dict) and d.get("source")})
    if gaps:
        parts.append(t.mark("Not read: {sources}", tag="p", attrs=HINT, sources=", ".join(gaps)))
    parts.append('<p id="doctor-said" role="status"></p>')
    return f'<section class="card panel" id="assistant-doctor">{head}{"".join(parts)}</section>'


def _inner(section: str) -> str:
    """A `_table` card without its own card: the doctor's two lists sit inside the doctor's card."""
    return (section.replace('<section class="card panel">', '<div class="assistant-sub">', 1)
            .replace('class="machine-h"', 'class="assistant-h3"', 1).replace("<h2", "<h3", 1)
            .replace("</h2>", "</h3>", 1).removesuffix("</section>") + "</div>")


def security_html(findings: dict | None, t: Translator) -> str:
    """The security watch: how many security findings are open, by area and severity, and the link to them."""
    import finding_types
    open_ = [f for f in (findings or {}).get("items") or []
             if isinstance(f, dict) and finding_types.group_of(f.get("type")) == "security"]
    head = t.mark("The security watch", tag="h2", attrs=HEADING)
    link = (f'<p><a href="findings.html?group=security">{t.mark("Open the security findings")}</a></p>')
    if not open_:
        return (f'<section class="card panel" id="assistant-security">{head}'
                + t.mark("No security finding is open: keys, the supply chain of agent servers and skills, "
                         "dependencies and integrations were read clean.", tag="p", attrs=EMPTY)
                + link + "</section>")
    by_area = {a: sum(1 for f in open_ if area_of(f.get("type") or "") == a) for a in AREAS}
    by_sev = [(s, sum(1 for f in open_ if f.get("severity") == s)) for s in SEVERITIES]
    lead = t.mark("Open security findings: {n}", tag="p", attrs=' class="assistant-lead"', n=len(open_))
    sev = " · ".join(t.mark(s + ": {n}", n=n) for s, n in by_sev if n)
    rows = [[t.mark(AREA_WORDS[a]), t.number(n)] for a, n in by_area.items()]
    table = _inner(_table(t, "By area", [("Area", False), ("Open", True)], rows, "Nothing is open."))
    return (f'<section class="card panel" id="assistant-security">{head}{lead}<p{HINT}>{sev}</p>{table}{link}'
            "</section>")


def agent_panel_html(payload: dict, t: Translator | None = None) -> str:
    t = t or Translator()
    return (f'<div id="assistant" class="machine assistant">{chat_html(t)}{doctor_html(payload.get("doctor"), t)}'
            f'{security_html(payload.get("findings"), t)}</div>')


#: Values translated through a variable, so the catalogue test sees them (tests/test_i18n.py).
DYNAMIC = (*CHECK_WORDS.values(), *STATE_WORDS.values(), *AREA_WORDS.values(), "Check", "Files moved aside", "When",
           "Action", "State", "Count", "Where", "Area", "Open", "critical: {n}", "warning: {n}", "info: {n}")
