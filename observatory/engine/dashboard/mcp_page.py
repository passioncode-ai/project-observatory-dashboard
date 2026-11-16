"""The supply-chain section of the MCP page (OBS-555, CP-09): which servers start risky commands, which pinned tools
changed, and whether third-party scanners may be sent tool definitions.

Rendered here, at build time, from `mcp_supply.summary()` (`payload["supply"]`), above the page's table of servers.
Nothing on it holds a command line, an argument, a header or an environment value: a row names the server, its agent
and the rule. The Accept buttons are built disabled; served by the local control server the page script enables them
and posts to its `mcp-accept` action (the same function as `observatory_mcp_accept`); opened as a file they stay
disabled and say why. No shell command text is shown on a row.

Every visible string is an English message id marked for the page script (L10N-01, L10N-05).
"""
from __future__ import annotations

from i18n import Translator
from machine_page import EMPTY, HEADING, NUM, _e, _table

HINT = ' class="machine-hint"'
NONE = "–"
#: The tool an agent accepts a changed tool with; named in each disabled button's tooltip.
TOOL = "observatory_mcp_accept"
DISABLED = ("This page only shows the changes: it was opened as a file or without the local control server. "
            "An agent accepts them with {tool}.")
RULE_LABELS = {
    "unpinned-runner": "Package runner without an exact version",
    "fetch-execute": "Fetches code and runs it",
    "outside-install-places": "Program outside the install places",
}
RULE_WHY = {
    "unpinned-runner": "It fetches the newest version each time, so what runs can change under the declaration.",
    "fetch-execute": "It downloads a program from a URL and runs it as it arrives.",
    "outside-install-places": "The executable sits in a temporary or download folder, or at a relative path.",
}
CHANGE_LABELS = {"changed": "changed", "added": "added", "removed": "removed"}


def _tiles(s: dict, t: Translator) -> str:
    scanners = s.get("scanners") or {}
    tiles = [(t.mark("Tools pinned"), t.number(s.get("pins") or 0),
              t.mark("on {n} servers", n=s.get("pinned_servers") or 0)),
             (t.mark("Risky commands"), t.number(len(s.get("risky") or [])), t.mark("judged from the declarations")),
             (t.mark("Changed tools"), t.number(len(s.get("changes") or [])), t.mark("waiting for your acceptance")),
             (t.mark("Remote scanners"), t.mark("switch@@on") if scanners.get("enabled") else t.mark("switch@@off"),
              t.mark("they are sent tool definitions"))]
    cells = "".join(f'<div class="tile">{k}<b>{v}</b>{sub}</div>' for k, v, sub in tiles)
    return f'<div class="tiles machine-tiles">{cells}</div>'


def _risky(s: dict, t: Translator) -> str:
    rows = []
    for r in s.get("risky") or []:
        for rule in r.get("rules") or []:
            rows.append([f'<span class="mono">{_e(r.get("server"))}</span>', _e(r.get("agent")),
                         t.mark(RULE_LABELS.get(rule, rule)), t.mark(RULE_WHY.get(rule, ""))])
    return _table(t, "Risky commands", [("Server", False), ("Agent", False), ("Rule", False), ("Why", False)], rows,
                  "No server command trips a rule.")


def _changed(s: dict, t: Translator) -> str:
    rows = s.get("changes") or []
    head = t.mark("Changed tools", tag="h2", attrs=HEADING)
    if not rows:
        return (f'<section class="card panel" id="supply-changes">{head}'
                f'{t.mark("No pinned tool has changed.", tag="p", attrs=EMPTY)}</section>')
    tip = t.attr("title", DISABLED, tool=TOOL)
    heads = (("Server", False), ("Tool", False), ("What changed", False), ("Seen", False), ("Accept", False))
    th = "".join(f'<th scope="col">{t.mark(h)}</th>' for h, _n in heads)
    body = []
    for r in rows:
        key = f'{r.get("agent")}:{r.get("server")}'
        button = (f'<button type="button" class="chip-btn" data-mcp-accept="{_e(key)}" '
                  f'data-mcp-tool="{_e(r.get("tool"))}" disabled{tip}>{t.mark("Accept")}</button>')
        cells = [f'<span class="mono">{_e(r.get("server"))}</span> ({_e(r.get("agent"))})',
                 f'<span class="mono">{_e(r.get("tool"))}</span>',
                 t.mark(CHANGE_LABELS.get(r.get("change"), "changed")), t.date(str(r.get("seen_at") or "")[:10]),
                 button]
        tds = "".join(f'<td{t.attr("data-label", h)}>{c}</td>' for (h, _n), c in zip(heads, cells))
        body.append(f"<tr>{tds}</tr>")
    everything = (f'<p><button type="button" class="chip-btn" data-mcp-accept="" data-mcp-tool="" disabled{tip}>'
                  f'{t.mark("Accept all")}</button> <span id="supply-said" class="machine-hint" role="status" '
                  f'aria-live="polite"></span></p>')
    return (f'<section class="card panel" id="supply-changes">{head}{everything}<table><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></section>')


def _scanners(s: dict, t: Translator) -> str:
    on = (s.get("scanners") or {}).get("enabled") is True
    head = t.mark("Third-party remote scanners", tag="h2", attrs=HEADING)
    state = (t.mark("On: tool definitions may be sent to a scanner you chose.", tag="p") if on else
             t.mark("Off: nothing is sent to a third-party scanner.", tag="p"))
    why = t.mark("A remote scanner receives each tool's name, description and schema, which is content from your "
                 "servers. It stays off until you turn on the setting features.mcp_remote_scanners; an agent "
                 "changes it with observatory_configure.", tag="p", attrs=HINT)
    return f'<section class="card panel">{head}{state}{why}</section>'


def _degraded(s: dict, t: Translator) -> str:
    items = [f'<li><span class="mono">{_e(d.get("source"))}</span> · {t.known(d.get("reason"))}</li>'
             for d in s.get("degraded") or [] if isinstance(d, dict)]
    if not items:
        return ""
    return f'<section class="card panel">{t.mark("Not measured", tag="h2", attrs=HEADING)}<ul>{"".join(items)}</ul></section>'


def mcp_html(payload: dict, t: Translator | None = None) -> str:
    t = t or Translator()
    s = payload.get("supply")
    head = t.mark("Supply chain", tag="h2", attrs=HEADING)
    if not isinstance(s, dict):
        return (f'<section id="supply" class="machine"><section class="card panel">{head}'
                f'{t.mark("The supply-chain watch has not been read.", tag="p", attrs=EMPTY)}</section></section>')
    note = t.mark("Commands are judged from the declarations and tools are pinned when a probe lists them on "
                  "request. No command line, argument or key is kept.", tag="p", attrs=HINT)
    return ('<section id="supply" class="machine">' f'<section class="card panel">{head}{note}</section>'
            + _tiles(s, t) + _risky(s, t) + _changed(s, t) + _scanners(s, t) + _degraded(s, t) + "</section>")


#: Values the page translates through a variable, named here so the catalog test sees them.
DYNAMIC = (tuple(RULE_LABELS.values()) + tuple(RULE_WHY.values()) + tuple(CHANGE_LABELS.values())
           + ("Server", "Agent", "Rule", "Why", "Tool", "What changed", "Seen", "Accept", "No server command trips a rule.",
              "Risky commands", DISABLED, "Not measured"))
