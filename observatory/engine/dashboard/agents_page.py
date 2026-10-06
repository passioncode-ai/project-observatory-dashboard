"""The Agents page: which agents are working, on what, and where their work moved.

Rendered from `agents_view.summary()` — at build time into `agents.html`, and by the
local server on request (`/agents`), so one renderer serves the static page and its
live refresh. It reads and never acts: every action is a command to copy (OSS
scenarios: no administrative mutation from the dashboard).

The lane view: one segment per executor that held the workflow, a dot per checkpoint
it wrote, and between segments the handoff with its reason. An offered handoff is a
dashed segment; a lapsed or replaced one is struck through. The same events are
listed in order underneath for a screen reader. Colour always comes with a word.
"""
from __future__ import annotations

import html

from i18n import Translator

HEADING = ' class="machine-h"'
NUM = ' class="num"'
EMPTY = ' class="empty"'
HINT = ' class="machine-hint"'


def _e(v) -> str:
    return html.escape("" if v is None else str(v))


def _project(pid, names: dict | None) -> str:
    """A project as its registry name, linked to its panel on the Projects page."""
    if not pid:
        return "—"
    name = (names or {}).get(pid) or pid
    return f'<a href="projects.html#{_e(pid)}">{_e(name)}</a>'


def _stamp(value) -> str:
    text = str(value or "")
    return text.replace("T", " ").replace("Z", " UTC") if text else "—"


def _age(t: Translator, seconds) -> str:
    """A duration as a marked element, so the page's language switch translates
    its unit too: a value passed as an argument would keep the build's language."""
    if seconds is None:
        return "—"
    if seconds < 120:
        return t.mark("{n} s", n=seconds)
    if seconds < 7200:
        return t.mark("{n} min", n=seconds // 60)
    if seconds < 172800:
        return t.mark("{n} h", n=seconds // 3600)
    return t.mark("{n} d", n=seconds // 86400)


def _who(executor: dict | None) -> str:
    """Provider · model · account handle, as plain text: escaped where it is
    placed into markup, passed raw where `t.mark` escapes it."""
    executor = executor or {}
    parts = [executor.get("provider"), executor.get("model"), executor.get("accountRef")]
    return " · ".join(str(p) for p in parts if p) or "—"


#: A checkpoint dot's tooltip, one sentence per step status so each language
#: can agree its own words; a status no sentence knows reads the bare one.
DOT_TITLE = "step {step}, {at}"
DOT_TITLES = {"in_progress": "step {step} in progress, {at}", "done": "step {step} done, {at}",
              "blocked": "step {step} blocked, {at}"}


#: Words this page shows that other pages use in another sense ("missing" is a
#: deleted worktree on the Machine page), so each carries its context in its id.
def _m(t: Translator, prefix: str, value) -> str:
    """A context word as its own marked element (see `_age` for why)."""
    return t.mark(f"{prefix}@@{value}") if value else ""


def _chip(t: Translator, label: str, tone: str = "", **kw) -> str:
    cls = f"chip {tone}".strip()
    return f'<span class="{cls}"><span class="dot"></span>{t.mark(label, **kw)}</span>'


def _cmd(text: str) -> str:
    return f'<code class="mono agents-cmd">{_e(text)}</code>'


def _counters(c: dict, t: Translator) -> str:
    tiles = [("Sessions working", c.get("sessionsActive", 0), "turn in the last hour"),
             ("Workflows open", c.get("workflowsOpen", 0), "held by an executor"),
             ("Handoffs waiting", c.get("handoffsWaiting", 0), "to be accepted"),
             ("Stalled", c.get("stalled", 0), "silent 30 min"),
             ("Steps kept", c.get("keptSteps", 0), "for review")]
    cells = "".join(f'<div class="tile">{t.mark(k)}<b>{t.number(v)}</b>{t.mark(sub, tag="span")}</div>'
                    for k, v, sub in tiles)
    return f'<div class="tiles agents-tiles">{cells}</div>'


def _need(n: dict, t: Translator, names: dict | None = None) -> str:
    wf = f'<span class="mono">{_e(n["workflowId"])}</span>'
    kind = n["kind"]
    if kind == "stalled":
        what = t.mark("stalled: no checkpoint and no session turn for") + " " + _age(t, n.get("seconds"))
    elif kind == "handoff-lapsed":
        what = t.mark("a handoff lapsed before any session accepted it")
    elif kind == "kept-steps":
        what = t.mark("{n} step(s) kept after a lost lease wait for review", n=n.get("count", 0))
    elif kind == "credential-missing":
        what = t.mark("key {name} ({env}) is not in the vault", name=n.get("name"), env=n.get("env"))
    elif kind == "credential-unknown":
        what = t.mark("key {name} ({env}) could not be checked: the vault could not be read",
                      name=n.get("name"), env=n.get("env"))
    else:
        what = t.mark("key {name} ({env}) is only in a .env — move it to the vault",
                      name=n.get("name"), env=n.get("env"))
    return (f'<li class="agents-need"><div>{wf} · {_project(n.get("projectId"), names)} — {what}</div>'
            f'{_cmd(n["command"]) if n.get("command") else ""}</li>')


def _lanes(w: dict, t: Translator) -> str:
    segs = []
    for i, s in enumerate(w.get("segments") or []):
        arrived = s.get("arrivedBy") or {}
        if i > 0 or arrived:
            reason = arrived.get("reason") or "—"
            segs.append(f'<span class="agents-arrow" aria-hidden="true">→ '
                        f'{_m(t, "handoff", reason) if reason != "—" else "—"}</span>')
        dots = "".join(
            f'<span class="agents-dot st-{_e(c.get("status") or "unknown")}"'
            f'{t.attr("title", DOT_TITLES.get(c.get("status"), DOT_TITLE), step=c["stepId"], at=_stamp(c["at"]))}>'
            f'{_e(c["stepId"])}</span>' for c in s.get("checkpoints") or [])
        ended = ""
        if s.get("state") == "ended" and s.get("endedReason"):
            ended = f'<span class="agents-ended">{_m(t, "lease-end", s["endedReason"])}</span>'
        segs.append(f'<span class="agents-seg">{_e(_who(s.get("executor")))}'
                    f'<span class="agents-dots">{dots or t.mark("no checkpoint", tag="i")}</span>'
                    f'{ended}</span>')
    for o in w.get("offers") or []:
        cls = "agents-seg offered" if o["state"] == "offered" else "agents-seg lapsed"
        segs.append(f'<span class="agents-arrow" aria-hidden="true">⇢ {_m(t, "handoff", o.get("reason"))}</span>'
                    f'<span class="{cls}">{_e(_who(o.get("to")))} '
                    f'{t.mark("offered until {at}", at=_stamp(o.get("expiresAt"))) if o["state"] == "offered" else _m(t, "lease-end", o["state"])}'
                    f'</span>')
    return f'<div class="agents-lane" aria-hidden="true">{"".join(segs)}</div>'


def _events(w: dict, t: Translator) -> str:
    """The lanes as an ordered list: what a screen reader reads instead. Each
    changing word is its own marked element, so the language switch reaches it."""
    items = []
    for s in w.get("segments") or []:
        arrived = (s.get("arrivedBy") or {}).get("reason")
        items.append(f'<li>{t.date(_stamp(s.get("from")))}: {_e(_who(s.get("executor")))} '
                     f'{t.mark("took the workflow")} '
                     f'({_m(t, "handoff", arrived) if arrived else t.mark("started it")})</li>')
        for c in s.get("checkpoints") or []:
            items.append(f'<li>{t.date(_stamp(c["at"]))}: {t.mark("step {step}", step=c["stepId"])} '
                         f'{_m(t, "step", c.get("status"))}</li>')
    for o in w.get("offers") or []:
        state = t.mark("offered") if o["state"] == "offered" else _m(t, "lease-end", o["state"])
        items.append(f'<li>{t.date(_stamp(o.get("at")))}: {t.mark("handoff to")} {_e(_who(o.get("to")))} '
                     f'({_m(t, "handoff", o.get("reason"))}) — {state}</li>')
    return f'<ol class="agents-events visually-hidden-list">{"".join(items)}</ol>'


def _workflow(w: dict, t: Translator, names: dict | None = None) -> str:
    step = w.get("step") or {}
    state = ("closed" if w["status"] == "closed" else "stalled" if w.get("stalled")
             else "handoff waiting" if w.get("pendingHandoff") else "active")
    tone = {"closed": "", "stalled": "danger", "handoff waiting": "warn", "active": "ok"}[state]
    keys = "".join(
        f'<span class="chip {({"vault": "ok", "env-only": "warn", "missing": "danger"}).get(c["state"], "")}">'
        f'<span class="dot"></span><span class="mono">{_e(c["name"])}</span>&nbsp;'
        f'{_m(t, "key-state", c["state"])}</span>' for c in w.get("credentials") or [])
    consts = "".join(f"<li>{_e(c)}</li>" for c in w.get("constraints") or [])
    nexts = "".join(f'<li><span class="mono">{_e(n.get("step_id"))}</span> {_e(n.get("next_action"))}</li>'
                    for n in w.get("next") or [])
    git = "".join(f'<li class="mono">{_e(a.get("path"))}{" @ " + _e(a.get("branch")) if a.get("branch") else ""}</li>'
                  for a in w.get("artifacts") or [])
    lease = w.get("lease") or {}
    head = (f'<summary><span class="mono">{_e(w["workflowId"])}</span> '
            f'{_chip(t, "workflow@@" + state, tone)} <b>{_e(w.get("goal"))}</b>'
            f'<span class="agents-meta">{_e((names or {}).get(w.get("projectId")) or w.get("projectId"))} · '
            f'{t.mark("step {step}", step=step.get("stepId") or "—")} {_m(t, "step", step.get("status"))} · '
            f'{t.mark("last checkpoint")} {_age(t, w.get("silentSeconds"))} {t.mark("ago")} · '
            f'{_e(_who(lease.get("executor")))} · {t.mark("{n} handoff(s)", n=w.get("handoffs", 0))}</span></summary>')
    body = [_lanes(w, t), _events(w, t)]
    if consts:
        body.append(f'{t.mark("Constraints", tag="h3")}<ul class="agents-constraints">{consts}</ul>')
    if nexts:
        body.append(f'{t.mark("Next", tag="h3")}<ul>{nexts}</ul>')
    if keys:
        body.append(f'{t.mark("Keys", tag="h3")}<p>{keys}</p>')
    if git:
        body.append(f'{t.mark("Checkouts", tag="h3")}<ul>{git}</ul>')
    wid = w["workflowId"]
    body.append(f'<p class="machine-hint">{_cmd(f"project-observatory full workflow show {wid}")}</p>')
    is_open = " open" if w.get("stalled") or w.get("pendingHandoff") or w.get("credentialsMissing") else ""
    return f'<details class="card panel agents-wf"{is_open}>{head}{"".join(body)}</details>'


def _sessions(sessions: list[dict], t: Translator, names: dict | None = None) -> str:
    if not sessions:
        return (f'<section class="card panel">{t.mark("Sessions", tag="h2", attrs=HEADING)}'
                f'{t.mark("No agent session recorded a turn in the last day.", tag="p", attrs=EMPTY)}</section>')
    th = "".join(f'<th scope="col"{NUM if h == "Turns" else ""}>{t.mark(h)}</th>'
                 for h in ("Session", "Project", "Last turn", "Turns", "Workflow"))
    rows = []
    for s in sessions:
        live = _chip(t, "working", "ok") if s.get("active") else ""
        rows.append(
            "<tr>"
            f'<td{t.attr("data-label", "Session")}><span class="mono">{_e((s.get("sessionId") or "")[:8])}</span> {live}</td>'
            f'<td{t.attr("data-label", "Project")}>{_project(s.get("projectId"), names)}</td>'
            f'<td{t.attr("data-label", "Last turn")}>{t.date(_stamp(s.get("lastTurnAt")))}</td>'
            f'<td{t.attr("data-label", "Turns")} class="num">{t.number(s.get("turns"))}</td>'
            f'<td{t.attr("data-label", "Workflow")}><span class="mono">{_e(s.get("workflowId") or "—")}</span></td>'
            "</tr>")
    return (f'<section class="card panel">{t.mark("Sessions", tag="h2", attrs=HEADING)}'
            f'<table><thead><tr>{th}</tr></thead><tbody>{"".join(rows)}</tbody></table></section>')


def signature(a: dict) -> str:
    """What the page SAYS, without its clocks: the live refresh announces a change to
    a screen reader only when this differs (audit A27), not every 15 seconds."""
    import hashlib
    import json
    said = {"counters": a.get("counters"),
            "needs": [(n.get("kind"), n.get("workflowId"), n.get("name")) for n in a.get("needsYou") or []],
            "workflows": [(w.get("workflowId"), w.get("status"), w.get("stalled"),
                           w.get("step"),
                           w.get("handoffs")) for w in a.get("workflows") or []],
            "sessions": [(s.get("sessionId"), s.get("turns"), s.get("active")) for s in a.get("sessions") or []],
            "degraded": [d.get("code") or d.get("reason") for d in a.get("degraded") or []]}
    return hashlib.sha256(json.dumps(said, sort_keys=True, default=str).encode()).hexdigest()[:16]


def agents_html(payload: dict, t: Translator | None = None, live: bool = False) -> str:
    """The page body. `live` marks a fragment served for the page's refresh."""
    t = t or Translator()
    a = payload.get("agents") or {}
    names = a.get("projectNames") or {}
    parts = [f'<section id="agents" class="agents" data-measured="{_e(a.get("measuredAt"))}"'
             f' data-signature="{signature(a)}"{" data-live" if live else ""}>']
    parts.append(t.mark("Read {at}", tag="p", attrs=' class="machine-at" id="agents-at"',
                        at=_stamp(a.get("measuredAt"))))
    parts.append(f'<p class="machine-hint" id="agents-live" role="status" aria-live="polite"></p>')
    parts.append(_counters(a.get("counters") or {}, t))
    needs = a.get("needsYou") or []
    parts.append(f'<section class="card panel">{t.mark("Needs you", tag="h2", attrs=HEADING)}')
    if needs:
        parts.append(f'<ul class="agents-needs">{"".join(_need(n, t, names) for n in needs)}</ul>')
    else:
        parts.append(t.mark("Nothing is waiting for you.", tag="p", attrs=EMPTY))
    parts.append("</section>")
    workflows = a.get("workflows") or []
    parts.append(t.mark("Workflows", tag="h2", attrs=HEADING))
    if workflows:
        by_project: dict[str, list[dict]] = {}
        for w in workflows:
            by_project.setdefault(w.get("projectId") or "—", []).append(w)
        for pid, rows in sorted(by_project.items()):
            parts.append(f'<h3 class="agents-project">{_project(pid, names) if pid != "—" else "—"}</h3>')
            parts += [_workflow(w, t, names) for w in rows]
    elif not any(d.get("code") in ("no-store", "unreadable", "before-agent-memory")
                 for d in a.get("degraded") or []):
        parts.append(f'<section class="card panel">'
                     f'{t.mark("No workflow yet. An agent starts one by writing its first checkpoint: observatory_checkpoint_write.", tag="p", attrs=EMPTY)}'
                     f'</section>')
    parts.append(_sessions(a.get("sessions") or [], t, names))
    degraded = a.get("degraded") or []
    if degraded:
        def reason(d: dict) -> str:
            code = d.get("code")
            if code == "no-store":
                return t.mark("the store does not exist yet")
            if code == "before-agent-memory":
                return t.mark("the store predates agent memory; the next engine start migrates it")
            if code == "unreadable":
                return t.mark("unreadable: {error}", error=d.get("reason", ""))
            return t.known(d.get("reason"))
        items = "".join(f'<li><span class="mono">{_e(d.get("source"))}</span> — {reason(d)}</li>'
                        for d in degraded)
        parts.append(f'<section class="card panel">{t.mark("Not read", tag="h2", attrs=HEADING)}<ul>{items}</ul></section>')
    parts.append(t.mark("The page shows and hands over commands; it changes nothing. "
                        "Opened through the local server it refreshes every 15 seconds.",
                        tag="p", attrs=HINT))
    parts.append("</section>")
    return "".join(parts)


#: Values translated through `t(...)` with a variable argument (see DYNAMIC_IDS in
#: tests/test_i18n.py).
DYNAMIC = (DOT_TITLE, *DOT_TITLES.values(), "Sessions working", "turn in the last hour", "Workflows open",
           "held by an executor", "Handoffs waiting", "to be accepted", "Stalled",
           "silent 30 min", "Steps kept", "for review", "Session", "Project", "Last turn",
           "Turns", "Workflow", "{n} s", "{n} min", "{n} h", "{n} d",
           # `_chip` takes its label as an argument, which the catalog scan does not see.
           "working",
           *(f"handoff@@{r}" for r in ("limit", "plan_route", "operator", "crash", "restart")),
           *(f"step@@{x}" for x in ("in_progress", "done", "blocked")),
           *(f"key-state@@{x}" for x in ("vault", "env-only", "missing", "unknown")),
           *(f"lease-end@@{x}" for x in ("handed-off", "closed", "lapsed", "superseded", "ended")),
           *(f"workflow@@{x}" for x in ("active", "stalled", "handoff waiting", "closed")))
