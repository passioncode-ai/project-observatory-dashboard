"""The Agents page: which agents are working, on what, and where their work moved.

Rendered from `agents_view.summary()` and `sessions_view`: at build time into `agents.html`,
and by the local server on request (`/agents`), so one renderer serves the static page and
its live refresh.

SESSIONS FIRST, THEN PROBLEMS (OBS-551, CP-02). The page opens with every coding session
running on this machine (the three counts of `sessions_view.facets`, then the list grouped
by agent and project: managed by the account manager or not, its account group, last turn,
state). Problems come next, one row per workflow with its worst state. A row offers a button,
never a command: a stalled workflow or a lapsed handoff is offered to another agent through
the key server's `workflow-handoff` action, the same store function as the agent's
`observatory_handoff_create`. Built disabled: the page script enables it only when the key
server serves the page with its token; opened as a file the tooltip names the agent's tool.
A key's value is typed by a person, so a key row links to the Keys page; kept steps link to
the Health page's review queue.

The lane view: one segment per executor that held the workflow, a dot per checkpoint
it wrote, and between segments the handoff with its reason. An offered handoff is a
dashed segment; a lapsed or replaced one is struck through. The same events are
listed in order underneath for a screen reader. Colour always comes with a word.
"""
from __future__ import annotations

import html

from i18n import NO_VALUE, Translator

HEADING = ' class="machine-h"'
NUM = ' class="num"'
EMPTY = ' class="empty"'
HINT = ' class="machine-hint"'


def _e(v) -> str:
    return html.escape("" if v is None else str(v))


def _project(pid, names: dict | None) -> str:
    """A project as its registry name, linked to its panel on the Projects page."""
    if not pid:
        return NO_VALUE
    name = (names or {}).get(pid) or pid
    return f'<a href="projects.html#{_e(pid)}">{_e(name)}</a>'


def _stamp(value) -> str:
    text = str(value or "")
    return text.replace("T", " ").replace("Z", " UTC") if text else NO_VALUE


def _age(t: Translator, seconds) -> str:
    """A duration as a marked element, so the page's language switch translates
    its unit too: a value passed as an argument would keep the build's language."""
    if seconds is None:
        return NO_VALUE
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
    return " · ".join(str(p) for p in parts if p) or NO_VALUE


#: A checkpoint dot's tooltip, one sentence per step status so each language
#: can agree its own words; a status no sentence knows reads the bare one.
DOT_TITLE = "step {step}, {at}"
DOT_TITLES = {"in_progress": "step {step} in progress, {at}", "done": "step {step} done, {at}",
              "blocked": "step {step} blocked, {at}", "failed": "step {step} failed, {at}"}


#: Words this page shows that other pages use in another sense ("missing" is a
#: deleted worktree on the Machine page), so each carries its context in its id.
def _m(t: Translator, prefix: str, value) -> str:
    """A context word as its own marked element (see `_age` for why)."""
    return t.mark(f"{prefix}@@{value}") if value else ""


def _chip(t: Translator, label: str, tone: str = "", **kw) -> str:
    cls = f"chip {tone}".strip()
    return f'<span class="{cls}"><span class="dot"></span>{t.mark(label, **kw)}</span>'


#: The agent's tool for the handoff a row's button makes; named in the button's tooltip when it cannot act.
HANDOFF_TOOL = "observatory_handoff_create"
DISABLED = "Opened as a file, this page cannot act. An agent offers the workflow with {tool}."
#: The reasons a handoff without the executor's lease may give (store/workflow.REASONS_WITHOUT_TOKEN).
HANDOFF_REASONS = ("crash", "limit", "restart")
#: A workflow's problems, worst first: the row shows the first, and names the rest.
WORST = ("stalled", "handoff-lapsed", "credential-missing", "credential-unknown", "credential-env-only",
         "kept-steps")
#: The chip of a problem row: its worst state in a reader's words.
NEED_WORDS = {"stalled": "need@@stalled", "handoff-lapsed": "need@@handoff lapsed",
              "credential-missing": "need@@key missing", "credential-unknown": "need@@key not checked",
              "credential-env-only": "need@@key in a .env", "kept-steps": "need@@steps to review"}


#: What a facet that was not read shows in place of its number: never 0 (CP-03).
NOT_READ = NO_VALUE


def _session_tiles(f: dict, t: Translator) -> list[tuple[str, object, str]]:
    """The three facets of one session count (CP-03, D1), each with its source and time:
    what runs, what the account manager manages, what recorded a turn in the last hour.
    `sessions_view.facets` is the one reader; Home's tile reads it too. A facet that was
    not read is a dash with its reason, never 0."""
    by = "".join(f'<li><span class="mono">{_e(a)}</span> {t.number(n) if isinstance(n, int) else _e(n)}</li>'
                 for a, n in (f.get("byAgent") or {}).items())
    sub = ' class="tile-sub"'            # the source on its own line, in sentence case (OBS-572)
    if f.get("processesFrom") == "manager":
        running = t.mark("processes the account manager lists, read {at}", tag="span", attrs=sub,
                         at=_stamp(f.get("processesAt")))
    elif f.get("processesFrom") == "machine-survey":
        running = t.mark("processes the machine survey counted, read {at}", tag="span", attrs=sub,
                         at=_stamp(f.get("processesAt")))
    else:
        running = t.mark("not read yet", tag="span", attrs=sub)
    if by:
        running += f'<ul class="tile-list">{by}</ul>'
    state = f.get("managerState")
    managed = (t.mark("by the account manager, read {at}", tag="span", attrs=sub, at=_stamp(f.get("managerAt"))) if state == "read"
               else t.mark("the account manager is not installed", tag="span", attrs=sub) if state == "not-installed"
               else t.mark("not read yet", tag="span", attrs=sub) if state is None
               else t.mark("the account manager did not answer", tag="span", attrs=sub))
    active = (t.mark("a turn recorded in the ledger", tag="span", attrs=sub) if f.get("activeHour") is not None
              else t.mark("the ledger could not be read", tag="span", attrs=sub))
    return [("Sessions running", f.get("running"), running), ("Managed", f.get("managed"), managed),
            ("Active in the last hour", f.get("activeHour"), active)]


def _tiles(tiles: list, t: Translator) -> str:
    cells = "".join(f'<div class="tile">{t.mark(k)}<b>{t.number(v) if v is not None else NOT_READ}</b>{sub}</div>'
                    for k, v, sub in tiles)
    return f'<div class="tiles agents-tiles">{cells}</div>'


def _counters(c: dict, t: Translator) -> str:
    """The workflow counts, above the workflows (the session counts head the sessions)."""
    return _tiles([("Workflows open", c.get("workflowsOpen", 0), t.mark("held by an executor", tag="span", attrs=' class="tile-sub"')),
                   ("Handoffs waiting", c.get("handoffsWaiting", 0), t.mark("to be accepted", tag="span", attrs=' class="tile-sub"')),
                   ("Stalled", c.get("stalled", 0), t.mark("silent 30 min", tag="span", attrs=' class="tile-sub"')),
                   ("Steps kept", c.get("keptSteps", 0), t.mark("for review", tag="span", attrs=' class="tile-sub"'))], t)


def _yes(t: Translator, value) -> str:
    return t.mark("answer@@yes") if value else t.mark("answer@@no")


def _machine_sessions(sessions: dict | None, t: Translator, names: dict | None = None) -> str:
    """Every coding session running here (sessions_view, CP-02): the three counts with their sources, then the
    list grouped by agent and project. A list the account manager did not give says why, never "none"."""
    import sessions_view
    doc = sessions if isinstance(sessions, dict) else {}
    f = sessions_view.facets(doc)
    parts = [f'<section class="card panel" id="agents-sessions">'
             f'{t.mark("Sessions on this machine", tag="h2", attrs=HEADING)}', _tiles(_session_tiles(f, t), t)]
    rows = [r for r in doc.get("sessions") or [] if isinstance(r, dict) and r.get("agent")]
    if not rows:
        if f["managerState"] == "read":
            parts.append(t.mark("No session is running: the account manager lists none.", tag="p", attrs=EMPTY))
        else:
            why = [d for d in doc.get("degraded") or [] if isinstance(d, dict)
                   and d.get("source") in ("manager", "sessions")]
            parts.append(t.mark("The session list comes from the account manager, and it was not read.",
                                tag="p", attrs=EMPTY))
            if why:
                parts.append(f'<p class="machine-hint">{t.known(why[0].get("reason"))}</p>')
        return "".join(parts) + "</section>"
    heads = ("Project", "Folder", "Managed", "Account group", "Last turn", "State")
    th = "".join(f'<th scope="col">{t.mark(h)}</th>' for h in heads)
    by_agent: dict[str, list[dict]] = {}
    for r in rows:
        by_agent.setdefault(str(r["agent"]), []).append(r)
    for agent in sorted(by_agent):
        group = sorted(by_agent[agent], key=lambda r: (r.get("project") is None,
                                                       str((names or {}).get(r.get("project")) or r.get("project") or ""),
                                                       str(r.get("folder_name") or ""), r.get("pid") or 0))
        parts.append(f'<h3 class="agents-project" data-agent="{_e(agent)}">{_e(agent)} · '
                     f'{t.mark("{n} sessions", n=len(group))}</h3>')
        body = []
        for r in group:
            quiet = r.get("state") == "quiet" or r.get("quiet") is True
            state = _chip(t, "session@@quiet") if quiet else _chip(t, "working", "ok")
            cells = (_project(r.get("project"), names) if r.get("project") else NOT_READ,
                     f'<span class="mono">{_e(r.get("folder_name") or NOT_READ)}</span>',
                     _yes(t, r.get("managed") is True),
                     f'<span class="mono">{_e(r.get("account_group"))}</span>' if r.get("account_group") else NOT_READ,
                     t.date(_stamp(r.get("last_turn_at"))) if r.get("last_turn_at") else NOT_READ,
                     state)
            tds = "".join(f'<td{t.attr("data-label", h)}>{c}</td>' for h, c in zip(heads, cells))
            body.append(f'<tr data-session="{_e(r.get("pid"))}">{tds}</tr>')
        parts.append(f'<table><thead><tr>{th}</tr></thead><tbody>{"".join(body)}</tbody></table>')
    return "".join(parts) + "</section>"


def _what(n: dict, t: Translator) -> str:
    """One problem of a workflow, as a sentence."""
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
        what = t.mark("key {name} ({env}) is only in a .env: move it to the vault",
                      name=n.get("name"), env=n.get("env"))
    return what


def providers(a: dict) -> list[str]:
    """Where a workflow may be offered: every provider an executor of a listed workflow ran on, else two defaults."""
    seen = set()
    for w in a.get("workflows") or []:
        for ex in [*(s.get("executor") for s in w.get("segments") or []), (w.get("lease") or {}).get("executor"),
                   *(o.get("to") for o in w.get("offers") or [])]:
            if isinstance(ex, dict) and isinstance(ex.get("provider"), str) and ex["provider"].strip():
                seen.add(ex["provider"].strip())
    return sorted(seen) or ["claude", "codex"]


def _handoff(wid: str, worst: str, options: list[str], t: Translator) -> str:
    """The offer: where to, why, and the button. Every control is built disabled (see the module's note)."""
    tip = t.attr("title", DISABLED, tool=HANDOFF_TOOL)
    pick = "".join(f'<option value="{_e(p)}">{_e(p)}</option>' for p in options)
    default = "limit" if worst == "handoff-lapsed" else "crash"
    why = "".join(f'<option value="{r}"{" selected" if r == default else ""}>{_m(t, "handoff", r)}</option>'
                  for r in HANDOFF_REASONS)
    return (f'<label>{t.mark("To")} <select data-wf-provider="{_e(wid)}" disabled{tip}>{pick}</select></label> '
            f'<label>{t.mark("Because of")} <select data-wf-reason="{_e(wid)}" disabled{tip}>{why}</select></label> '
            f'<button type="button" class="chip-btn" data-wf-act="handoff" data-wf="{_e(wid)}" disabled{tip}>'
            f'{t.mark("Offer to another agent")}</button>')


def _need(wid: str, items: list[dict], t: Translator, names: dict | None, options: list[str]) -> str:
    """One row per workflow (CP-02): its worst state, the rest named after it, and what to do."""
    items = sorted(items, key=lambda n: WORST.index(n["kind"]) if n["kind"] in WORST else len(WORST))
    worst = items[0]["kind"]
    tone = "danger" if worst in ("stalled", "handoff-lapsed", "credential-missing") else "warn"
    also = [_what(n, t) for n in items[1:]]
    head = (f'{_chip(t, NEED_WORDS.get(worst, "need@@stalled"), tone)} <span class="mono">{_e(wid)}</span> · '
            f'{_project(items[0].get("projectId"), names)}: {_what(items[0], t)}')
    if also:
        head += f'<br><span class="machine-hint">{t.mark("also:")} {"; ".join(also)}</span>'
    kinds = {n["kind"] for n in items}
    acts = []
    if kinds & {"stalled", "handoff-lapsed"}:
        acts.append(_handoff(wid, worst, options, t))
    if any(k.startswith("credential-") for k in kinds):
        acts.append(f'<a href="creds.html">{t.mark("Open the keys")}</a>')
    if "kept-steps" in kinds:
        acts.append(f'<a href="health.html#queue-s">{t.mark("Review the kept steps")}</a>')
    acts.append(f'<a href="#wf-{_e(wid)}">{t.mark("Open the workflow")}</a>')
    return (f'<li class="agents-need" data-wf="{_e(wid)}" data-worst="{_e(worst)}"><div>{head}</div>'
            f'<div class="agents-act">{" ".join(acts)}</div>'
            f'<p class="machine-hint" data-wf-said="{_e(wid)}" role="status" aria-live="polite"></p></li>')


def _needs(needs: list[dict], t: Translator, names: dict | None, options: list[str]) -> str:
    by_wf: dict[str, list[dict]] = {}
    for n in needs:
        by_wf.setdefault(str(n.get("workflowId")), []).append(n)
    rows = sorted(by_wf.items(), key=lambda kv: (min(WORST.index(n["kind"]) if n["kind"] in WORST else len(WORST)
                                                     for n in kv[1]), kv[0]))
    return f'<ul class="agents-needs">{"".join(_need(w, items, t, names, options) for w, items in rows)}</ul>'


def _lanes(w: dict, t: Translator) -> str:
    segs = []
    for i, s in enumerate(w.get("segments") or []):
        arrived = s.get("arrivedBy") or {}
        if i > 0 or arrived:
            reason = arrived.get("reason") or NO_VALUE
            segs.append(f'<span class="agents-arrow" aria-hidden="true">→ '
                        f'{_m(t, "handoff", reason) if reason != NO_VALUE else NO_VALUE}</span>')
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
                     f'({_m(t, "handoff", o.get("reason"))}): {state}</li>')
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
            f'{t.mark("step {step}", step=step.get("stepId") or NO_VALUE)} {_m(t, "step", step.get("status"))} · '
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
    is_open = " open" if w.get("stalled") or w.get("pendingHandoff") or w.get("credentialsMissing") else ""
    return f'<details class="card panel agents-wf" id="wf-{_e(wid)}"{is_open}>{head}{"".join(body)}</details>'


def _sessions(sessions: list[dict], t: Translator, names: dict | None = None) -> str:
    """The ledger's sessions with a recorded turn in the last day, and the workflow each worked on."""
    if not sessions:
        return (f'<section class="card panel">{t.mark("Recorded turns", tag="h2", attrs=HEADING)}'
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
            f'<td{t.attr("data-label", "Workflow")}><span class="mono">{_e(s.get("workflowId") or NO_VALUE)}</span></td>'
            "</tr>")
    return (f'<section class="card panel">{t.mark("Recorded turns", tag="h2", attrs=HEADING)}'
            f'<table><thead><tr>{th}</tr></thead><tbody>{"".join(rows)}</tbody></table></section>')


def signature(a: dict, sessions: dict | None = None) -> str:
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
    if sessions is not None:
        import sessions_view
        f = sessions_view.facets(sessions)
        said["sessionCount"] = (f["running"], f["managed"], f["activeHour"], f["managerState"])
        said["machineSessions"] = sorted((str(r.get("agent")), str(r.get("pid")), r.get("managed"), r.get("state"))
                                         for r in (sessions.get("sessions") or []) if isinstance(r, dict))
    return hashlib.sha256(json.dumps(said, sort_keys=True, default=str).encode()).hexdigest()[:16]


def agents_html(payload: dict, t: Translator | None = None, live: bool = False) -> str:
    """The page body. `live` marks a fragment served for the page's refresh."""
    t = t or Translator()
    a = payload.get("agents") or {}
    # The agents view names the projects it saw; a project only a session names, or one the view could only
    # name by its id, takes its name from the registry rows every page carries.
    names = dict(a.get("projectNames") or {})
    for r in payload.get("rows") or []:
        if isinstance(r, dict) and r.get("id") and r.get("name") and names.get(r["id"]) in (None, r["id"]):
            names[r["id"]] = r["name"]
    parts = [f'<section id="agents" class="agents" data-measured="{_e(a.get("measuredAt"))}"'
             f' data-signature="{signature(a, payload.get("sessions"))}"{" data-live" if live else ""}>']
    parts.append(t.mark("Read {at}", tag="p", attrs=' class="machine-at" id="agents-at"',
                        at=_stamp(a.get("measuredAt"))))
    parts.append(f'<p class="machine-hint" id="agents-live" role="status" aria-live="polite"></p>')
    parts.append(_machine_sessions(payload.get("sessions"), t, names))
    needs = a.get("needsYou") or []
    parts.append(f'<section class="card panel" id="agents-needs">{t.mark("Needs you", tag="h2", attrs=HEADING)}')
    if needs:
        parts.append(_needs(needs, t, names, providers(a)))
    else:
        parts.append(t.mark("Nothing is waiting for you.", tag="p", attrs=EMPTY))
    parts.append("</section>")
    workflows = a.get("workflows") or []
    parts.append(t.mark("Workflows", tag="h2", attrs=HEADING + ' id="agents-workflows"'))
    parts.append(_counters(a.get("counters") or {}, t))
    if workflows:
        by_project: dict[str, list[dict]] = {}
        for w in workflows:
            by_project.setdefault(w.get("projectId") or NO_VALUE, []).append(w)
        for pid, rows in sorted(by_project.items()):
            parts.append(f'<h3 class="agents-project">{_project(pid, names) if pid != NO_VALUE else NO_VALUE}</h3>')
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
        items = "".join(f'<li><span class="mono">{_e(d.get("source"))}</span>: {reason(d)}</li>'
                        for d in degraded)
        parts.append(f'<section class="card panel">{t.mark("Not read", tag="h2", attrs=HEADING)}<ul>{items}</ul></section>')
    parts.append(t.mark("Served by the local control server, a problem's button acts through it; opened as a file "
                        "the buttons are off and name the agent's tool. Opened through the local server the page "
                        "refreshes every 15 seconds.", tag="p", attrs=HINT))
    parts.append("</section>")
    return "".join(parts)


#: Values translated through `t(...)` with a variable argument (see DYNAMIC_IDS in
#: tests/test_i18n.py).
DYNAMIC = (DOT_TITLE, *DOT_TITLES.values(), "Sessions running", "Managed", "Active in the last hour",
           "Workflows open",
           "held by an executor", "Handoffs waiting", "to be accepted", "Stalled",
           "silent 30 min", "Steps kept", "for review", "Session", "Project", "Last turn",
           "Turns", "Workflow", "{n} s", "{n} min", "{n} h", "{n} d",
           # `_chip` takes its label as an argument, which the catalog scan does not see.
           "working", "session@@quiet", DISABLED, "answer@@yes", "answer@@no",
           *NEED_WORDS.values(),
           "Project", "Folder", "Managed", "Account group", "Last turn", "State",
           *(f"handoff@@{r}" for r in ("limit", "plan_route", "operator", "crash", "restart")),
           *(f"step@@{x}" for x in ("in_progress", "done", "blocked", "failed")),
           *(f"key-state@@{x}" for x in ("vault", "env-only", "missing", "unknown")),
           *(f"lease-end@@{x}" for x in ("handed-off", "closed", "lapsed", "superseded", "ended")),
           *(f"workflow@@{x}" for x in ("active", "stalled", "handoff waiting", "closed")))
