#!/usr/bin/env python3
"""Project Observatory — stdio MCP server.

Protocol revision: 2026-07-28 (written down here and in fabric-agent.json; the
`mcp` SDK 2.x declares it as LATEST_PROTOCOL_VERSION — 1.x tops out at an older
revision and cannot satisfy the manifest, which pins the revision as a schema
`const`).

2026-07-28 is stateless and discovers through `server/discover` rather than an
`initialize` handshake; `sampling`, `roots` and `logging` are deprecated in it.
None of that is reconstructed here — the SDK owns the wire, and this file owns
the tools.

Eight tools read. Two write, and they write only PROPOSALS: `observatory_record`
appends to the append-only ledger in state `proposed`, and
`observatory_propose` queues a registry change without touching
`registry/*.json`.

Beside them, every capability of fabric-agent.json is served under its own name
with its published schemas (fabric-interop/0.1): `capability_tools.py` lists and
answers those, and the `@server.capability` handlers below translate their
arguments to the same code. Neither can approve its own proposal, and neither invents a
caller identity — `owner` is required and has no default, because the gateway
owns identity and a memory kernel that manufactures it has none.

The shebang points at the project venv on purpose: the declared tenancy is one
operator on one machine, and the manifest's `executableRef` names this file
directly. Run `./observatory.py setup` on a fresh clone to create it.
"""
from __future__ import annotations
import asyncio, json, re, sqlite3, sys, pathlib
from typing import Annotated, Any, Literal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def _home_argument(argv: list[str]) -> None:
    """`--home PATH` selects the workspace, for a host that starts this server from
    the per-install manifest: a manifest's stdio connection carries arguments and
    no environment, so `OBSERVATORY_HOME` cannot travel in it. Read before any
    engine module resolves its paths. `--stdio` is accepted and means the default."""
    if "--home" in argv:
        at = argv.index("--home")
        if at + 1 >= len(argv) or not argv[at + 1] or not pathlib.Path(argv[at + 1]).is_absolute():
            raise SystemExit("--home needs an absolute workspace path")
        import os
        os.environ["OBSERVATORY_HOME"] = argv[at + 1]


if __name__ == "__main__":
    _home_argument(sys.argv[1:])

from pydantic import AliasChoices, Field                                                        

# The capability tools live beside this file. `mcp/` is not a package — the SDK
# owns the name `mcp` — so the directory itself goes on the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from capability_tools import Answer, InteropServer                                 # noqa: E402

import paths                                                                      
import proposals                                                                  
import survey as survey_mod                                                       
from store import db as store_db                                                  
from store import ledger as L                                                     

PROTOCOL_REVISION = "2026-07-28"
import configuration
VERSION = configuration.VERSION

server = InteropServer(
    name="observatory",
    title="Project Observatory",
    version=VERSION,
    description="What is true of every project on this machine.",
    # An LLM client READS this to decide what the server can do, so a stale one is
    # not cosmetic. It said "Read-only" and named three tools while eight were
    # served and two of them write — and the gateway's own comment repeated the
    # same claim in Russian.
    instructions=(
        "Ten `observatory_*` tools read, two propose, and `observatory_assistant_ask` uses the configured model and writes dialogue, and every Fabric capability is also served under "
        "its own name (fabric-interop/0.1): `estate.survey`, `project.detail`, "
        "`project.timeline`, `project.record`, `machine.mcp.inventory`, and "
        "`machine.mcp.refresh`, which answers a job followed with `fabric.job.get` and "
        "stopped with `fabric.job.cancel`. Those tools take and return exactly the "
        "published schemas and echo `_meta.traceparent` as a child span.\n"
        "ASSISTANT: `observatory_assistant_status` reads readiness and saved conversation summaries; "
        "`observatory_assistant_conversation` reads a private dialogue; `observatory_assistant_ask` "
        "SPENDS using the configured model, sends bounded local evidence and persists a dialogue/job. "
        "It gives advice only. Follow or cancel its job with the Fabric job tools.\n"
        "READ: `observatory_status` surveys the current scope; a requested scan pin "
        "is reported as unsupported in degraded. `observatory_project` answers about "
        "one project; `observatory_timeline` returns its commit history; "
        "`observatory_findings` lists what needs a person; `observatory_recall` and "
        "`observatory_search` look over recorded narrative — SEARCH SPENDS: it "
        "embeds the query, charges the wallet, and falls back to the lexical half "
        "alone when a spend guardrail is reached, saying so in `degraded`; "
        "`observatory_credentials` names what a project can authenticate with and "
        "CANNOT return a value — use the `use` command it hands back instead of "
        "opening the file, because a transcript outlives the key it quotes; "
        "`observatory_machine` shows what runs by origin, memory, disk, idle worktrees "
        "and branches and the cleanup plan, and with `explainPid` why one process "
        "runs — never its environment or full command line. `machine.mcp.inventory` lists "
        "every MCP server the agent configs on this machine declare, by name, with where "
        "each is declared, its transport and whether it answered the last probe — never a "
        "URL, command line, header or environment value.\n"
        "WRITE: `observatory_record` and `observatory_propose` append to the ledger. "
        "Everything they write lands `proposed` with confidence below 1 and NOTHING "
        "here can promote it — that is the operator's act or a second independent "
        "corroboration.\n"
        "Every result carries a `degraded` list: an empty list asserts full coverage, "
        "and a non-empty one names the sources that could not be read. Treat a "
        "missing `degraded` field as a bug rather than as full coverage."
    ),
)


def _scope(kind: str, value: str | None) -> dict[str, Any]:
    scope: dict[str, Any] = {"kind": kind}
    if kind in ("owner", "project"):
        scope["value"] = value
    return scope


def _scope_error(kind: str, value: str | None) -> dict[str, Any] | None:
    """A bad scope is a typed answer, not an exception.

    Raising turns into UnexpectedToolError on the wire, which tells a caller
    that the server broke rather than that the argument was wrong."""
    if kind in ("owner", "project") and not value:
        return {"error": "missing value",
                "detail": f"scope kind '{kind}' requires a value",
                "hint": "owner takes an organisation login; project takes a 'project:<slug>' id",
                "degraded": []}
    return None


#: An identity a caller may claim over this wire. POSITIVE, not a blacklist:
#: a blacklist of `operator` lets "Operator", "operator " and "OPERATOR" through,
#: and a row that merely LOOKS operator-owned to a person reading the ledger is
#: the same forgery one layer down.
# The local part allows upper case: refusing a legitimate `agent:Claude-Code`
# is a worse failure than accepting a confusingly-named but UNPRIVILEGED
# identity, since nothing inside the namespace can reach the operator's
# three privileges.
CALLER_ID = re.compile(r"^(agent|service):[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


def _owner_error(owner: str) -> dict[str, Any] | None:
    """The wire cannot authenticate, so it cannot carry the operator's claim.

    stdio MCP has no channel identity — whoever spawned this process IS the
    caller — and a free-form `owner` string would let any client type
    `operator` and buy three privileges: permanent exemption from retention
    (`owner_exempt` in the retention config), immunity from supersession by any
    other writer, and no confidence discount. Meanwhile `tools/review.py`
    demands a TERMINAL before it will write as the operator, on the stated
    grounds that minting that from a script would let anything with shell access
    forge it. Two doors to one authority must not have opposite standards, so
    only `agent:` and `service:` identities are accepted here.

    **What this is not.** It is not a security perimeter: any local process
    running as this user can write the SQLite file directly and bypass the
    ledger entirely. It is a CORRECTNESS boundary — an agent, including a
    well-behaved one, must not be able to mint the human's authority, because
    the distinction between "an agent proposed this" and "a person decided it"
    is what the entire review queue rests on. The realistic failure is not an
    attacker but a model reasoning "I will record this as the operator so it
    does not expire".
    """
    if CALLER_ID.match(owner or ""):
        return None
    return {"error": "owner refused",
            "detail": f"{owner!r} is not an identity this wire accepts",
            "hint": "owner must be `agent:<name>` or `service:<name>`. The operator's "
                    "authority cannot be claimed over stdio, which has no caller "
                    "identity to check it against — promote or reject a record with "
                    "`review.py`, from a terminal.",
            "degraded": []}


@server.tool()
def observatory_assistant_status() -> dict:
    """Local assistant readiness and conversation list. Reads private workspace; no model spend."""
    from agent import assistant
    return assistant.status()


@server.tool()
def observatory_assistant_ask(question: str, request_id: str,
                              conversation_id: str | None = None,
                              project_id: str | None = None) -> dict:
    """Ask the configured model about bounded local project evidence. Sends the question
    and selected local facts to that provider and can incur configured model costs.
    Advisory only: no shell, deletion, approval or deployment. Persists private dialogue.
    Reuse request_id only for identical input. Returns job + conversation_id; follow
    with fabric.job.get, stop with fabric.job.cancel. A different active ask is busy.
    """
    from agent import assistant
    args={"question":question,"request_id":request_id}
    if conversation_id is not None:args["conversation_id"]=conversation_id
    if project_id is not None:args["project_id"]=project_id
    try:return assistant.ask(args)
    except assistant.AssistantError as exc:raise ValueError(str(exc)) from None


@server.tool()
def observatory_assistant_conversation(id: str) -> dict:
    """Read this workspace's private conversation by opaque chat id; no model spend."""
    from agent import assistant
    return assistant.get_conversation(id)


@server.tool()
def observatory_status(
    scope: Annotated[dict[str, Any] | None,
                     Field(description="The shape the published input schema declares: "
                                       "`{\"kind\": \"estate\"|\"owner\"|\"project\", "
                                       "\"value\": …}`. A host that compiled that schema "
                                       "sends this; `kind` and `value` below are the older "
                                       "flat spelling and still work.")] = None,
    kind: Annotated[Literal["estate", "owner", "project"],
                    Field(description="estate = the whole machine; owner = one GitHub org; "
                                      "project = one project: id")] = "estate",
    value: Annotated[str | None,
                     Field(description="Required for owner and project. An org login, or a "
                                       "'project:<slug>' id.")] = None,
    includeExternal: Annotated[bool,
                                Field(validation_alias=AliasChoices("includeExternal", "include_external"), description="Include third-party repositories merely "
                                                  "cloned here.")] = False,
    asOfScanId: Annotated[str | None,
                             Field(validation_alias=AliasChoices("asOfScanId", "as_of_scan_id"), description="The scan you want the answer compared "
                                               "against. It is RECORDED, not honoured — the "
                                               "registry is not versioned per scan, so the "
                                               "answer carries the current estate and says so "
                                               "in `degraded`. Omit it.")] = None,
    limit: Annotated[int | None, Field(ge=1, le=200,
                     description="Return at most this many projects. Omit for all of them — "
                                 "an estate survey is about 30,000 tokens, so pass a limit "
                                 "unless you want the whole thing.")] = None,
    cursor: Annotated[str | None,
                      Field(description="Continue after this project id, from a previous "
                                        "answer's `nextCursor`. The walk loses and repeats "
                                        "nothing, but a project appearing mid-walk still "
                                        "shifts it — no parameter freezes the estate.")] = None,
) -> dict[str, Any]:
    """Survey a scope: every project in it, with repositories, sites, stack and last activity.

    This is the `estate.survey` capability declared in fabric-agent.json. It is
    deterministic — no model participates — so the same scope over one registry
    returns the same answer. Over TWO registries it does not, and the tick
    rewrites the registry regularly.

    `asOfScanId` is spelled exactly as the input schema declares it (the snake
    case spelling is accepted as an alias), because a host that compiles the
    schema and constructs the call must not send a name this tool ignores. The
    pin is RECORDED, not honoured: the answer is built from the current registry,
    which is not versioned per scan, so `survey.py` names an unhonoured pin in
    `degraded`. Exposing the parameter is still right — a host that compiles the
    input schema will construct the call, and it must get a truthful answer
    rather than a silent one.
    """
    # THE CONTRACT'S SHAPE FIRST. `scope` is what the published input schema
    # declares; `kind`/`value` are the flat spelling this tool has always taken.
    # Until 2026-09-08 only the flat one existed, so a host sending the declared
    # object had it IGNORED and received the whole estate — 152 projects where it
    # asked for one, with no error.
    if isinstance(scope, dict) and scope.get("kind"):
        kind, value = scope["kind"], scope.get("value")
    # Checked AFTER the scope object is read. Checked before it, as until
    # 2026-09-30, `{"scope": {"kind": "owner"}}` passed the check on the flat
    # default `estate` and was surveyed with no owner at all.
    bad = _scope_error(kind, value)
    if bad:
        return bad
    return survey_mod.survey(_scope(kind, value), include_external=includeExternal,
                             as_of_scan_id=asOfScanId, limit=limit, cursor=cursor)


@server.tool()
def observatory_project(
    projectId: Annotated[str, Field(validation_alias=AliasChoices("projectId", "project_id"), description="A project identifier, for example project:example-project")],
    timelineLimit: Annotated[int, Field(validation_alias=AliasChoices("timelineLimit", "timeline_limit"), ge=0, le=200,
                              description="How many recent commits to include. 0 omits them.")] = 10,
) -> dict[str, Any]:
    """One project: identity, activity, measurements, conclusions, findings.

    Not identity alone — description, ownership, lifecycle, membership rules and
    repositories — because a host asked to render "this project" also needs its
    activity series, plugin measurements, the project's own findings and the
    conclusions the observatory has drawn about it. All four are in the store
    and on the dashboard, so the wire carries them too.

    `survey.project_detail` is the single place that assembles it, and
    `project.detail` is its own capability with its own published schema: the
    shape of this answer is not the shape of a survey, and declaring one schema
    for both would make the tools fail validation against the contract their
    own capability publishes.
    """
    return survey_mod.project_detail(projectId, timeline_limit=timelineLimit)


@server.tool()
def observatory_credentials(
    projectId: Annotated[str, Field(validation_alias=AliasChoices("projectId", "project_id", "project"),
                                    description="A 'project:<slug>' id, or the bare slug")],
) -> dict[str, Any]:
    """Which credentials a project holds, BY NAME — and how to USE one without seeing it.

    THIS TOOL CANNOT RETURN A VALUE, and that is its point rather than a
    limitation. An agent that needs a project's key does not need to read it: it
    needs to know the key exists, what it is called, and how to put it into a
    command. So this answers the first two, and `use` in the result answers the
    third — `tools/use_secret.py run <project> <NAME> -- <command>` places the
    value in that command's environment and removes it from everything the
    agent itself can see.

    Reading a `.env` "to check" a value is how credentials end up in session
    transcripts. A transcript outlives the key it quotes.

    `shared_with` is measured rather than declared: two projects carry it when
    their values are equal, which is what "what breaks if I rotate this" means.
    `available_in` is the opposite — a slot empty here whose name holds a live
    value elsewhere.
    """
    return survey_mod.credentials(projectId)


@server.tool()
def observatory_machine(
    explainPid: Annotated[int | None, Field(validation_alias=AliasChoices("explainPid", "explain_pid", "pid"),
                                            description="A process id to explain: its origin, ancestry, and "
                                                        "witr's service/port detail when witr is installed")] = None,
) -> dict[str, Any]:
    """The machine this estate runs on: processes grouped by ORIGIN (agent
    session, launchd job, simulator, app, detached), memory and swap, free disk
    and the largest cache/VM/history locations, worktrees and branches by class,
    and the cleanup plan with its journal.

    Read-only. It never kills a process or deletes a file: the auto tier of the
    cleanup runs in the tick when the operator enabled it, and anything that
    holds unique work only through `full cleanup --apply --include manual`.
    A process is reported by executable and script, never by its environment or
    full command line, because either can carry a credential.
    """
    import machine_view
    return machine_view.summary(explainPid)


@server.capability("machine.mcp.inventory")
def machine_mcp_inventory(arguments: dict[str, Any], span: Any) -> dict[str, Any]:
    """Every MCP server this machine's agent configs declare — by name, never by value.

    The `machine.mcp.inventory` capability of fabric-agent.json, served under its
    own name with its published schemas (`capability_tools.py`). It answers from
    the last inventory the observatory took — every tick takes one, and
    `machine.mcp.refresh` takes one on request: Claude Code (user and project
    scopes, plugins, claude.ai connectors), Cursor, OpenCode, Codex, Gemini CLI and
    Kiro. Each server says which configs declare it, its transport (stdio,
    streamable-http, sse) and whether it answered the last probe, with that
    probe's time. An absent or unreadable config, and an inventory older than the
    tick allows, are named in `degraded`.
    """
    import mcp_inventory
    return mcp_inventory.inventory()


@server.tool()
def observatory_timeline(
    projectId: Annotated[str, Field(validation_alias=AliasChoices("projectId", "project_id"), description="A 'project:<slug>' id")],
    since: Annotated[str | None, Field(description="ISO-8601 date or timestamp; "
                                                   "only events at or after it")] = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    "Commits and recorded events for one project, newest first."    
    return survey_mod.timeline(projectId, since=since, limit=limit)


@server.tool()
def observatory_search(
    query: Annotated[str, Field(min_length=1,
                     description="What to look for. Similarity where the vector index is "
                                 "available, and a lexical match always.")],
    project_id: Annotated[str | None, Field(description="Limit to one 'project:<slug>'")] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """Recall over recorded narrative — the one read where similarity is the right question.

    Conflicting records are returned **together** and unranked. `degraded` names
    every retrieval path that could not run, because absence from a result is not
    proof that a record does not exist.
    """
    return survey_mod.search(query, project_id=project_id, limit=limit)


@server.tool()
def observatory_recall(
    projectId: Annotated[str | None, Field(validation_alias=AliasChoices("projectId", "project_id"), description="Limit to one project, or omit for all")] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    cursor: Annotated[str | None,
                      Field(description="Continue after this point, from a previous "
                                        "answer's `nextCursor`.")] = None,
) -> dict[str, Any]:
    """Current ledger records — what has been recorded about projects, and why.

    Conflicting records are returned **together**: a `contested` record appears
    beside the `supported` one it disagrees with, and nothing here ranks them.
    Absence from this result is not proof that a record does not exist.
    """
    # A `degraded` list, because this server's own `instructions` tell a client
    # that a MISSING one is a bug: "an empty list asserts full coverage… Treat a
    # missing `degraded` field as a bug rather than as full coverage." Every
    # read tool carries one, and this one — the tool whose docstring says
    # "absence here is not proof of absence" — above all.
    # And an unreadable store must be a typed answer, not a raise: a raise
    # becomes UnexpectedToolError on the wire, which tells the caller the server
    # broke rather than that something could not be read.
    degraded: list[dict[str, str]] = []
    try:
        conn = store_db.connect()
    except Exception as exc:
        return {"projectId": projectId, "count": 0, "total": 0, "records": [],
                "contested": [], "note": "the store could not be opened",
                "degraded": [{"source": "store", "reason": f"unavailable: {exc}"}]}
    try:
        # ONE MORE than asked for, and the extra is the signal. Deciding whether
        # a further page exists by comparing `total` to the page size cannot
        # work once a cursor is in play — the caller's position is not in the
        # answer — and my first attempt peeked with a second connection opened
        # inside a condition and never closed. Over-fetching by one is exact,
        # needs no second query, and leaks nothing.
        fetched = L.live(conn, project_id=projectId, limit=limit + 1, cursor=cursor)
        rows, more = fetched[:limit], len(fetched) > limit
        total = L.live_count(conn, project_id=projectId)
    finally:
        conn.close()
    contested = [r["memory_id"] for r in rows if r["state"] == "contested"]
    # `total` is the SCOPE, `count` this page. Reporting only the page size
    # would tell the caller the size of its own page, not the size of what it
    # knows — a silent cap, in the tool a caller asks "what do you know".
    # `nextCursor` is keyed on `(created_at, memory_id)` because `created_at`
    # alone is not unique at second resolution.
    out = {"projectId": projectId, "count": len(rows), "total": total,
           "records": rows, "contested": contested,
           "note": "conflicting records are returned together and are not ranked; "
                   "absence here is not proof of absence",
           "degraded": degraded}
    # Only when a further page really exists. A cursor handed out at the end of
    # the walk would have a caller asking for nothing, for ever.
    if more and rows:
        out["nextCursor"] = L.live_cursor(rows[-1])
    return out


def _write_error(exc: Exception) -> dict[str, Any]:
    """Refusals are typed answers with a remedy, not stack traces."""
    remedy = {
        "OwnerRequired": "pass `owner` — an identity of the form `agent:<name>` or "
                         "`service:<name>`, e.g. 'agent:claude-code'. There is no default: a "
                         "write that could claim the operator's authority by omission is the "
                         "defect this refuses. `operator` is not claimable over this wire.",
        "OwnerRefused": "this record belongs to someone else. Only the operator overrides, and "
                        "an agent may correct only its own records.",
        "RevisionConflict": "re-read the record, merge onto the current revision, and retry with "
                            "that number as `expected_revision`. There is no last-write-wins.",
        "IllegalTransition": "the lifecycle has no edge from the current state to that one; the "
                             "message names the legal moves.",
        "LedgerError": "the write violates a ledger invariant; the message says which.",
    }.get(type(exc).__name__, "see the message")
    out: dict[str, Any] = {"error": type(exc).__name__, "detail": str(exc), "remedy": remedy}
    if isinstance(exc, L.RevisionConflict):
        out["currentRevision"] = exc.current
        out["expectedRevision"] = exc.expected
    return out


@server.tool()
def observatory_findings(
    severity: Annotated[Literal["all", "critical", "warning", "info"],
                        Field(description="Lowest severity to return; 'all' includes "
                                          "info.")] = "warning",
    include_acknowledged: Annotated[bool,
                                    Field(description="Include findings the operator has "
                                                      "silenced in finding_acks.json.")] = False,
) -> dict[str, Any]:
    """What in this estate needs a person: expiring domains, dark sites, clones that exist nowhere else.

    Read-only and derived: findings are REBUILT from the typed registry on every
    run, so one whose cause is gone disappears rather than lingering. Each carries
    evidence, a proposed action, and `first_seen`.

    NOT part of a declared Fabric capability. `estate.survey` answers what a
    project IS; this answers what is wrong with it, and folding the second into
    the first would make a survey's shape depend on the machine's health.
    """
    f = paths.REGISTRY / "findings.json"
    if not f.is_file():
        return {"findings": [], "counts": {},
                "degraded": [{"source": "findings",
                              "reason": "no findings have been built; run "
                                        "`./observatory.py findings`"}]}
    doc = json.loads(f.read_text(encoding="utf-8"))
    order = {"critical": 0, "warning": 1, "info": 2}
    floor = 3 if severity == "all" else order[severity]
    rows = [x for x in doc["findings"]
            if order.get(x["severity"], 3) <= (floor if severity != "all" else 2)
            and (include_acknowledged or not x.get("acked"))]
    # TWO dates, because they answer different questions and one of them used to
    # answer both badly. `builtAt` is when these findings last CHANGED — it stops
    # moving when nothing moves, which is what makes the registry committable
    # (see `tools/build_findings.py`). On its own that reads as staleness: a
    # caller seeing three days would assume the observer had stopped. `checkedAt`
    # is when the observer last completed a scan, from the store, where run
    # metadata belongs.
    degraded, checked_at = [], None
    try:
        c = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
        row = c.execute("SELECT finished_at FROM scans WHERE finished_at IS NOT NULL"
                        " ORDER BY finished_at DESC LIMIT 1").fetchone()
        c.close()
        checked_at = row[0] if row else None
    except sqlite3.Error as exc:
        degraded.append({"source": "store",
                         "reason": f"the last scan time is unreadable: {exc}; "
                                   f"builtAt alone cannot tell staleness from quiet"})
    return {"builtAt": doc.get("built_at"), "checkedAt": checked_at,
            "counts": doc.get("counts", {}),
            "resolvedSinceLastRun": doc.get("resolved_since_last_run", []),
            "findings": rows, "degraded": degraded}


@server.tool()
def observatory_record(
    owner: Annotated[str, Field(min_length=1,
                     description="Who is writing. Required, no default. `agent:<name>` or "
                                 "`service:<name>`, e.g. 'agent:claude-code'. `operator` is "
                                 "NOT accepted here: this wire cannot check a caller's "
                                 "identity, and the operator decides at a terminal.")],
    statement: Annotated[str, Field(min_length=1,
                         description="The minimal claim or episode. Not a transcript: a full log "
                                     "is evidence, and evidence is linked rather than stored.")],
    why: Annotated[str | None, Field(description="What it MEANT. This is the field no diff "
                                                 "contains and the only reason this tool exists.")] = None,
    projectId: Annotated[str | None, Field(validation_alias=AliasChoices("projectId", "project_id"), description="A 'project:<slug>' id")] = None,
    sessionId: Annotated[str | None, Field(validation_alias=AliasChoices("sessionId", "session_id"), description="The session this came out of")] = None,
    memoryId: Annotated[str | None, Field(validation_alias=AliasChoices("memoryId", "memory_id"), description="Correct an existing record. Requires "
                                                       "expected_revision.")] = None,
    expectedRevision: Annotated[int | None, Field(validation_alias=AliasChoices("expectedRevision", "expected_revision"), description="The revision you read. A mismatch "
                                                               "returns a conflict, never a "
                                                               "silent overwrite.")] = None,
    evidence: Annotated[list[dict[str, Any]] | None,
                        Field(description="Resolvable references: commit shas, file paths, URIs")] = None,
) -> dict[str, Any]:
    """Append a note to the ledger, in state `proposed`.

    It cannot be written as anything else: an automated writer proposes, and
    something else promotes. `owner` is required — this server does not invent
    caller identity.
    """
    bad = _owner_error(owner)
    if bad:
        return bad
    conn = store_db.connect()
    try:
        return L.append(conn, owner=owner, statement=statement, why=why,
                        project_id=projectId, session_id=sessionId,
                        memory_id=memoryId, expected_revision=expectedRevision,
                        evidence=evidence or [], function="episodic", scope="project",
                        state="proposed",
                        # 0.5 unconditionally. An operator-owned write would
                        # warrant no discount, but `_owner_error` above refuses
                        # that claim, so a branch for it could never be reached —
                        # and a condition whose true side is unreachable is dead
                        # data that misleads the next reader.
                        confidence=0.5)
    except Exception as exc:
        return _write_error(exc)
    finally:
        conn.close()


@server.tool()
def observatory_propose(
    owner: Annotated[str, Field(min_length=1,
                     description="Who is proposing. Required, no default. `agent:<name>` or "
                                 "`service:<name>`; `operator` is not accepted over this wire.")],
    targetId: Annotated[str, Field(validation_alias=AliasChoices("targetId", "target_id"), min_length=1,
                         description="What to change: 'project:<slug>', 'repository:<owner>/<name>' "
                                     "or 'domain:<fqdn>'")],
    patch: Annotated[dict[str, Any], Field(description="The fields to change, as an object. To report "
                                                       "something you created for a project — an analytics "
                                                       "property or tracker, a Firebase/Google Cloud project, a "
                                                       "server, a cloud or payment account, a Figma file — send "
                                                       "{\"resources\": [{\"kind\": \"ga4-property\", \"identifier\": "
                                                       "\"properties/123\", \"account\": \"accounts/456\", \"url\": …, "
                                                       "\"note\": …}]}; it is appended, never replacing what "
                                                       "is recorded. `organization` names whose accounts the "
                                                       "project uses.")],
    evidence: Annotated[list[dict[str, Any]] | None,
                        Field(description="What justifies it. A patch with no evidence is a guess.")] = None,
) -> dict[str, Any]:
    """Propose a change to the typed registry. It does NOT change the registry.

    The registry is written by collectors and by the operator. This queues a row
    awaiting a decision, because a writer that could edit the fact base directly
    would poison it one plausible sentence at a time.
    """
    bad = _owner_error(owner)
    if bad:
        return bad
    # REFUSED AT THE WIRE, not only at the decision. Measured 2026-09-07: a
    # proposal naming `project:also-not-real`, one patching the DERIVED
    # `activity_tier` along with the record's own `id` and `source_refs`, and one
    # with no evidence at all were each answered "proposed" — a receipt for
    # something no decision could ever apply. `proposals.refusal` derives the
    # appliable set from the curation files an accepted proposal lands in, so the
    # wire and the decider cannot disagree about it.
    why = proposals.refusal(targetId, patch, evidence or [])
    if why:
        return {"error": "unappliable-proposal", "detail": why,
                "appliable": {k: sorted(v) for k, v in proposals.appliable().items()}}
    try:
        registry_ids = {p["id"] for p in json.loads(
            (paths.REGISTRY / "projects.json").read_text(encoding="utf-8"))["projects"]}
        registry_ids |= {r["id"] for r in json.loads(
            (paths.REGISTRY / "repositories.json").read_text(encoding="utf-8"))["repositories"]}
    except (OSError, ValueError, KeyError) as exc:
        # The registry is unreadable, so the subject cannot be checked. The
        # proposal is still accepted and the gap is NAMED: refusing every write
        # because a file could not be read would make an unreadable registry an
        # outage rather than a degradation.
        registry_ids = set()
        unknown_subject = f"the registry could not be read to check the subject: {exc}"
    else:
        unknown_subject = ("" if targetId in registry_ids else
                           f"`{targetId}` is not in the registry. It may arrive on "
                           f"the next tick, so this is queued rather than refused — "
                           f"but if the id is a typo nothing will ever apply it")
    conn = store_db.connect()
    try:
        out = L.proposals_add(conn, target_id=targetId, patch=patch,
                              evidence=evidence or [], owner=owner)
        if unknown_subject:
            out["warning"] = unknown_subject
        return out
    except Exception as exc:
        return _write_error(exc)
    finally:
        conn.close()


# ─────────────────────────── capability tools ──────────────────────────────
# The four v0.2.0 capabilities under their own names (fabric-interop/0.1, C3.1),
# answering from the same code as the `observatory_*` tools above, which stay for
# every host that already calls them. Arguments arrive validated against the
# published input schema, so these only translate names.

@server.capability("estate.survey")
def capability_estate_survey(arguments: dict[str, Any], span: Any) -> dict[str, Any]:
    scope = arguments.get("scope") or {"kind": "estate"}
    return observatory_status(scope=scope, includeExternal=arguments.get("includeExternal", False),
                              asOfScanId=arguments.get("asOfScanId"), limit=arguments.get("limit"),
                              cursor=arguments.get("cursor"))


@server.capability("project.detail")
def capability_project_detail(arguments: dict[str, Any], span: Any) -> dict[str, Any]:
    return observatory_project(projectId=arguments["projectId"],
                               timelineLimit=arguments.get("timelineLimit", 10))


@server.capability("project.timeline")
def capability_project_timeline(arguments: dict[str, Any], span: Any) -> dict[str, Any]:
    return observatory_timeline(projectId=arguments["projectId"], since=arguments.get("since"),
                                limit=arguments.get("limit", 100))


@server.capability("project.record")
def capability_project_record(arguments: dict[str, Any], span: Any) -> Answer:
    """A note (`statement`) or a registry proposal (`targetId` + `patch`), both proposals.

    A proposal's `warning` — its subject is not in the registry yet — has no field
    in the published v0.2.0 output schema, so it travels as a sentence in the
    text channel instead of being dropped."""
    owner = arguments["owner"]
    if arguments.get("targetId") is not None or arguments.get("patch") is not None:
        if not (arguments.get("targetId") and isinstance(arguments.get("patch"), dict)):
            return Answer({"error": "LedgerError",
                           "detail": "a registry proposal needs both `targetId` and `patch`",
                           "remedy": "send `targetId` and `patch` together, or `statement` for a note"})
        out = observatory_propose(owner=owner, targetId=arguments["targetId"], patch=arguments["patch"],
                                  evidence=arguments.get("evidence"))
    elif arguments.get("statement"):
        out = observatory_record(owner=owner, statement=arguments["statement"], why=arguments.get("why"),
                                 projectId=arguments.get("projectId"), sessionId=arguments.get("sessionId"),
                                 memoryId=arguments.get("memoryId"),
                                 expectedRevision=arguments.get("expectedRevision"),
                                 evidence=arguments.get("evidence"))
    else:
        return Answer({"error": "LedgerError",
                       "detail": "nothing to write: neither `statement` nor `targetId` and `patch`",
                       "remedy": "send `statement` for a note, or `targetId` and `patch` for a proposal"})
    notes = []
    if isinstance(out, dict) and out.get("warning"):
        out = dict(out)
        notes.append(str(out.pop("warning")))
    return Answer(out, notes)


# ─────────────────────────── resources ──────────────────────────────────────
# WHY RESOURCES AND NOT A NEW CAPABILITY. The pinned Fabric contract defines no
# rendering capability, no resource concept and no pagination, so inventing an
# `estate.render` capability would be inventing contract surface — and a host
# compiling this manifest would find a capability the contract cannot describe.
# What the MCP protocol DOES define is resources with URI templates, which is
# exactly the addressable per-subject shape a renderer needs: one URI, one
# subject, a declared media type. `fabric/FABRIC-CONFORMANCE.md` records that
# these are protocol surface and NOT part of the pinned contract, because
# implying contract coverage would be the more expensive lie.

@server.resource("observatory://estate", mime_type="application/json",
                 title="The whole estate",
                 description="Every project with its repositories, sites, stack and last "
                             "activity. About 120 KB — a deliberate read, not a cheap one; "
                             "call the observatory_status tool with a `limit` to page it.")
def resource_estate() -> dict[str, Any]:
    return survey_mod.survey({"kind": "estate"})


@server.resource("observatory://project/{project_id}", mime_type="application/json",
                 title="One project",
                 description="Everything known about a single project — about 3 KB. This is "
                             "the address a renderer holds: one URI per project, stable "
                             "across scans.")
def resource_project(project_id: str) -> dict[str, Any]:
    result = survey_mod.survey(_scope("project", project_id), include_external=True)
    if not result["projects"]:
        return {"error": "unknown project", "projectId": project_id,
                "hint": "read observatory://estate, or call observatory_status, to list what "
                        "exists",
                "degraded": result["degraded"]}
    return {"surveyedAt": result["surveyedAt"], "scanId": result["scanId"],
            "project": result["projects"][0], "evidence": result["evidence"],
            "degraded": result["degraded"]}


@server.resource("observatory://dashboard", mime_type="text/html",
                 title="The dashboard, as built",
                 description="The generated HTML page, self-contained, roughly 450 KB. Meant "
                             "for a host that RENDERS it; reading it into a model's context "
                             "spends far more than the JSON resources beside it.")
def resource_dashboard() -> str:
    f = paths.DASHBOARD_HTML
    if not f.is_file():
        # A resource must not pretend. An empty string would render as a blank
        # page and read as "the estate has nothing to show".
        return ("<!doctype html><meta charset=\"utf-8\"><title>not built</title>"
                "<p>The dashboard has not been built on this machine. Run "
                "<code>./observatory.py dashboard</code>.</p>")
    return f.read_text(encoding="utf-8")


def main() -> int:
    configuration.validate_workspace(required=True)
    # A capability without its handler would be listed and then answer
    # `not-served`; refuse to start instead, so the gap is found at the first run.
    missing = server.missing_handlers()
    if missing:
        raise SystemExit(f"capabilities without a handler: {', '.join(missing)}")
    asyncio.run(server.run_stdio_async())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
