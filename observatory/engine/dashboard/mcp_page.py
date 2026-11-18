"""The supply-chain section of the MCP page (OBS-555, CP-09): which servers start risky commands, which pinned tools
changed, and what each remote and plugin server is pinned to (OBS-606). No third-party scanner exists, so none is
offered.

Rendered here, at build time, from `mcp_supply.summary()` (`payload["supply"]`), above the page's table of servers.
Nothing on it holds a command line, an argument, a header or an environment value: a row names the server, its agent
and the rule. The Accept buttons are built disabled; served by the local control server the page script enables them
and posts to its `mcp-accept` action (the same function as `observatory_mcp_accept`); opened as a file they stay
disabled and say why. No shell command text is shown on a row.

Every visible string is an English message id marked for the page script (L10N-01, L10N-05).
"""
from __future__ import annotations

from i18n import NO_VALUE, Translator
from machine_page import EMPTY, HEADING, NUM, _table
from page_util import escape as _e, reason_html

HINT = ' class="machine-hint"'
NONE = NO_VALUE
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
#: OBS-606: a remote or plugin server, how its key travels, and what it is not pinned to.
KIND_LABELS = {"remote": "Remote", "plugin": "Plugin"}
AUTH_LABELS = {"url": "In the URL", "header": "In a header", "oauth": "Sign-in (OAuth)", "none-declared": "None declared"}
UNPINNED_LABELS = {
    "remote-endpoint": "Remote: only its endpoint is recorded",
    "plain-http": "Plain HTTP to another host",
    "image-tag": "Container image named by a tag",
    "plugin-unversioned": "Plugin without a recorded version",
}


def _tiles(s: dict, t: Translator) -> str:
    tiles = [(t.mark("Tools pinned"), t.number(s.get("pins") or 0),
              t.mark("on {n} servers", n=s.get("pinned_servers") or 0)),
             (t.mark("Risky commands"), t.number(len(s.get("risky") or [])), t.mark("judged from the declarations")),
             (t.mark("Changed tools"), t.number(len(s.get("changes") or [])), t.mark("waiting for your acceptance")),
             (t.mark("Unpinned servers"), t.number(len({(u.get("agent"), u.get("server"))
                                                        for u in s.get("unpinned") or []})),
              t.mark("remote, plugin and container servers"))]
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


def _judged(s: dict, t: Translator) -> str:
    """Each remote and plugin server: where it is (a host, or a plugin and its version), its transport, where its key
    travels, and what it is pinned to or why it is not (OBS-606). Never a URL's query, a key or an argument."""
    rows = []
    for j in s.get("judged") or []:
        where = " · ".join(
            ([f'<span class="mono">{_e(j.get("plugin"))}</span> {_e(j.get("plugin_version") or NONE)}']
             if j.get("plugin") else []) + ([_e(j.get("host"))] if j.get("host") else [])) or NONE
        codes = [c for c in j.get("codes") or [] if c in UNPINNED_LABELS]
        pinned = (t.mark("Pinned") if j.get("pinned") else
                  "<br>".join(t.mark(UNPINNED_LABELS[c]) for c in codes) or t.mark("Not pinned"))
        rows.append([f'<span class="mono">{_e(j.get("server"))}</span>', _e(j.get("agent")),
                     t.mark(KIND_LABELS.get(j.get("kind"), "Remote")), where, _e(j.get("transport") or NONE),
                     t.mark(AUTH_LABELS[j["auth"]]) if j.get("auth") in AUTH_LABELS else NONE, pinned])
    table = _table(t, "Remote and plugin servers",
                   [("Server", False), ("Agent", False), ("Kind", False), ("Where", False), ("Transport", False),
                    ("Key", False), ("Pinned", False)], rows, "No remote or plugin server is declared.")
    note = t.mark("A remote server runs what its provider deploys, so only its endpoint is recorded. A plugin's server "
                  "is pinned to the plugin's version. No URL query, key or argument is kept.", tag="p", attrs=HINT)
    return table[: -len("</section>")] + note + "</section>"


def _degraded(s: dict, t: Translator) -> str:
    items = [f'<li><span class="mono">{_e(d.get("source"))}</span> · {reason_html(d, t)}</li>'
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
            + _tiles(s, t) + _risky(s, t) + _changed(s, t) + _judged(s, t) + _degraded(s, t) + "</section>")


#: Values the page translates through a variable, named here so the catalog test sees them.
DYNAMIC = (tuple(RULE_LABELS.values()) + tuple(RULE_WHY.values()) + tuple(CHANGE_LABELS.values())
           + tuple(KIND_LABELS.values()) + tuple(AUTH_LABELS.values()) + tuple(UNPINNED_LABELS.values())
           + ("Server", "Agent", "Rule", "Why", "Tool", "What changed", "Seen", "Accept", "No server command trips a rule.",
              "Risky commands", DISABLED, "Not measured", "Remote and plugin servers", "Kind", "Where", "Transport",
              "Key", "Pinned", "No remote or plugin server is declared."))
