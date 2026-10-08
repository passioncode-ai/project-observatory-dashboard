#!/usr/bin/env python3
"""Execute the probes declared in fabric-agent.json against the live server.

Reads the declarations rather than restating them: a probe that exists only in
this file would drift from the manifest a host actually reads. Output goes to
fabric/probe-receipts.json, and every assertion carries its own verdict — a
probe reported as passing while one of its assertions was never evaluated is
the failure mode this guards.
"""
from __future__ import annotations
import asyncio, atexit, argparse, hashlib, json, os, shutil, subprocess, sys, pathlib, tempfile
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import jsonschema                                                                  
from mcp import ClientSession, StdioServerParameters                               
from mcp.client.stdio import stdio_client                                          
import mcp.types as mtypes                                                         
import mcp.client.session as _mcp_session

# The SDK gives `server/discover` a fixed 10 s. A probe run starts one server per
# capability, each a fresh interpreter; on a loaded machine the first answer can
# take longer than an installed server ever does. The start gets its own budget;
# every later request keeps the SDK default.
_mcp_session.DISCOVER_TIMEOUT_SECONDS = max(
    _mcp_session.DISCOVER_TIMEOUT_SECONDS, float(os.environ.get("OBSERVATORY_MCP_STARTUP_BUDGET", "60")))
import atomic
import paths                                                                       

MANIFEST = json.loads((ROOT / "fabric-agent.json").read_text())
CAPS = MANIFEST["capabilities"]


def source_of(uri: str) -> pathlib.Path:
    """Resolve only this release's allowlisted bundled schemas and fixtures."""
    from publish_contract import resolve, staged
    relative = resolve(uri)
    if relative is None:
        raise AssertionError("Probe URI does not belong to a bundled release")
    if relative not in staged():
        raise AssertionError("Probe URI is outside the bundled allowlist")
    return ROOT / "fabric" / relative


def schema_for(cap: dict) -> dict:
    """Load the output schema a capability declares, by its own URI."""
    return json.loads(source_of(cap["outputSchema"]).read_text())


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_fixture(uri: str) -> dict:
    return json.loads(source_of(uri).read_text())


def payload(result) -> dict:
    if getattr(result, "structured_content", None):
        return result.structured_content
    for block in result.content:
        if isinstance(block, mtypes.TextContent):
            return json.loads(block.text)
    raise AssertionError("no JSON payload")


def registry_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in sorted(paths.REGISTRY.glob("*.json")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def ledger_max_revision() -> int:
    db = paths.DB
    if not db.exists():
        return -1
    import sqlite3
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT COALESCE(MAX(revision), 0) FROM ledger").fetchone()
        return row[0]
    finally:
        conn.close()


def side_effect_verdict(before, after, later) -> dict:
    """Whether the CALL wrote, or something else did while it ran.

    The side-effect ceiling of an `effect: none` capability was proved by two
    readings — before and after — and failed on any difference. The scheduled
    tick writes the ledger and commits the registry every thirty minutes, so a
    tick landing inside that window made a read-only capability look like it
    wrote. Observed 2026-09-08: `test-receipt` failed through
    `ledger_max_revision` minutes after the tick released its lease, and passed
    on the next run with nothing changed.

    That mattered more than the same class in `tests/test_traps.py`,
    because this verdict is published: `fabric/probe-receipts.json` is what a
    Fabric host reads to decide whether to trust this agent, so a false FAIL on
    a side-effect ceiling is a lie about the contract.

    THREE OUTCOMES, from three readings. Unchanged is a PASS. Changed and then
    settled is attributable to the call and still a FAIL. Changed and STILL
    moving with no call in between means something other than the call is
    writing, which proves nothing either way — so the verdict is INCONCLUSIVE
    and names the tick.

    A HEURISTIC, and the note says so: a tick write that both starts and
    finishes between the second and third readings would still be attributed to
    the call. That is strictly better than attributing every concurrent write to
    it, which is what two readings did.
    """
    assertion = ("registry git status and ledger max revision are "
                 "unchanged after the call")
    if before == after:
        return {"assertion": assertion, "verdict": "PASS",
                "note": f"before={before} after={after}"}
    if after != later:
        return {"assertion": assertion, "verdict": "INCONCLUSIVE",
                "note": (f"before={before} after={after} later={later} — the estate "
                         f"kept moving with no call in between, so this change is not "
                         f"shown to be the call's: the scheduled tick writes the "
                         f"ledger and commits the registry every thirty minutes. "
                         f"Heuristic: a tick write finishing between the second and "
                         f"third readings would still read as the call's.")}
    return {"assertion": assertion, "verdict": "FAIL",
            "note": (f"before={before} after={after} later={later} — the change "
                     f"settled, so it is attributed to the call")}


async def call_record(session, args: dict) -> dict:
    # Verbatim, for the reason above.
    return payload(await session.call_tool("observatory_record", dict(args)))


async def assess_record(session, args: dict, out_schema: dict,
                        scratch_db: pathlib.Path | None = None) -> list[dict]:
    """One verdict per declared assertion, in declaration order."""
    a: list[dict] = []

    def say(text: str, ok: bool, note: str = "") -> None:
        a.append({"assertion": text, "verdict": "PASS" if ok else "FAIL", "note": note})

    # THE LIVE STORE ON PURPOSE, not `paths.DB`. The probe runs against a scratch
    # store (the child gets its own OBSERVATORY_DB), and the assertion below is
    # that the OPERATOR's ledger did not grow. Reading `paths.DB` here would read
    # the scratch store and make the assertion vacuous — it looks like the bug
    # `tools/notify_findings.py` had, and it is the opposite.
    live_before = ledger_rows(paths.DB)
    accepted = await call_record(session, args)
    try:
        jsonschema.validate(accepted, out_schema)
        shape_ok, shape_note = True, ""
    except jsonschema.ValidationError as exc:
        shape_ok, shape_note = False, str(exc).splitlines()[0][:180]

    say("the probe runs against a scratch store; no row reaches the operator's ledger",
        ledger_rows(paths.DB) == live_before,
        f"live ledger {live_before} -> {ledger_rows(paths.DB)}")
    say("a well-formed write is accepted in state 'proposed', never higher",
        shape_ok and accepted.get("state") == "proposed", shape_note or str(accepted)[:140])

    # WHY it was refused, not merely that something failed. This asserted
    # `bool(res.is_error)` — true for a misspelled tool name, a crashed server
    # or any unrelated error — so the assertion could not distinguish "refused
    # because `owner` is missing" from "the call broke". In the artefact this
    # project publishes as its conformance evidence, that is a green nobody
    # earned. The refusal must NAME the field, and the ledger must be untouched
    # by this specific call: `is_error` says nothing about what was written.
    #
    # ONE assertion, with the manifest's declared wording verbatim — "refused
    # BEFORE THE LEDGER IS TOUCHED" already asserts both halves, and neither was
    # being checked. Splitting it into two would have made the probe evaluate
    # seven assertions against six declared, changing `fabric-agent.json`, its
    # contentHash and the published `assertions.md` — a revision bump and a
    # publish the operator owns, for a claim the manifest already makes.
    before_rows = ledger_rows(scratch_db)
    res = await session.call_tool("observatory_record", {"statement": "no owner"})
    text = " ".join(str(getattr(c, "text", c)) for c in (res.content or []))
    after_rows = ledger_rows(scratch_db)
    say("a write with no owner is refused before the ledger is touched",
        bool(res.is_error) and "owner" in text.lower() and after_rows == before_rows,
        f"is_error={res.is_error} rows {before_rows}->{after_rows} said {text[:120]!r}")

    stale = payload(await session.call_tool("observatory_record", {
        "owner": args["owner"], "statement": "stale", "memoryId": accepted.get("memoryId"),
        "expectedRevision": 99}))
    say("a stale expectedRevision returns RevisionConflict carrying the current revision",
        stale.get("error") == "RevisionConflict"
        and stale.get("currentRevision") == accepted.get("revision"), str(stale)[:160])

    other = payload(await session.call_tool("observatory_record", {
        "owner": "agent:someone-else", "statement": "rewrite",
        "memoryId": accepted.get("memoryId"),
        "expectedRevision": accepted.get("revision")}))
    say("another owner's record cannot be corrected: OwnerRefused",
        other.get("error") == "OwnerRefused", str(other)[:160])

    reg = paths.REGISTRY / "projects.json"
    before = reg.read_bytes()
    prop = payload(await session.call_tool("observatory_propose", {
        "owner": args["owner"], "targetId": "project:fabric-probe-scratch",
        "patch": {"description": "proposed, never applied"},
        "evidence": args.get("evidence") or []}))
    say("a registry proposal leaves registry/projects.json byte-identical",
        reg.read_bytes() == before and prop.get("status") == "proposed", str(prop)[:140])
    return a


def ledger_rows(db: pathlib.Path) -> int:
    if not db.exists():
        return -1
    import sqlite3
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT count(*) FROM ledger").fetchone()[0]
    except sqlite3.Error:
        return -1
    finally:
        conn.close()


async def call(session, args: dict) -> dict:
    # Verbatim, for the reason above: `observatory_status` takes the declared
    # `scope` object now, and unpacking it here would hide the day it stops.
    return payload(await session.call_tool("observatory_status", dict(args)))


def coverage(probe: dict, results: list[dict]) -> dict:
    """Compare the assertion TEXTS, not their count.

    Counting was the whole check until 2026-09-05, and it cannot see the failure
    it exists to catch: an assertion reworded in the manifest leaves the count
    at four while the runner goes on evaluating the sentence that used to be
    there. Measured that day — `degraded names bitbucket…` was rewritten, the
    receipt still read "4/4 assertions evaluated", and the stale predicate was
    the only reason anything failed at all.
    """
    declared = list(probe["assertions"])
    evaluated = [r["assertion"] for r in results]
    missing = [a for a in declared if a not in evaluated]
    extra = [a for a in evaluated if a not in declared]
    ok = (bool(results) and not missing and not extra
          and all(r["verdict"] == "PASS" for r in results))
    out = {"declaredAssertions": len(declared), "evaluatedAssertions": len(evaluated),
           "verdict": "PASS" if ok else "FAIL"}
    if missing:
        out["declaredButNotEvaluated"] = missing
    if extra:
        out["evaluatedButNotDeclared"] = extra
    return out


def assess_detail(data: dict, OUT_SCHEMA: dict, requested: dict | None = None) -> list[dict]:
    """`project.detail` — the capability that did not exist until rev-4.

    `observatory_project` was a REQUIRED FEATURE of `estate.survey`, whose
    published output schema it could not satisfy: measured 2026-09-07, the
    server's answer was rejected with `'project' was unexpected`. Three tools,
    one schema, and the two that were never probed were the two that failed —
    which is what an unprobed required feature is for.
    """
    a: list[dict] = []

    def say(text: str, ok: bool, note: str = "") -> None:
        a.append({"assertion": text, "verdict": "PASS" if ok else "FAIL", "note": note})

    clean = {k: v for k, v in data.items() if not k.startswith("_")}
    try:
        jsonschema.validate(clean, OUT_SCHEMA)
        schema_ok, schema_note = True, ""
    except jsonschema.ValidationError as exc:
        schema_ok, schema_note = False, str(exc).splitlines()[0][:180]
    say("result validates against project-detail-output.schema.json",
        schema_ok, schema_note)
    want = (requested or local_fixture(next(
        p["inputFixture"] for cap in CAPS for p in cap["profile"]["probes"]
        if p["id"] == "detail-carries-every-section")))["projectId"]
    say("the requested project is the one returned",
        (clean.get("project") or {}).get("id") == want,
        str((clean.get("project") or {}).get("id")))
    sections = ("activity", "measurements", "notes", "findings", "recentEvents",
                "evidence", "degraded")
    missing = [k for k in sections if k not in clean]
    say("every section is present, so an empty one means measured-and-empty",
        not missing, f"missing: {missing}")
    notes = clean.get("notes") or []
    say("a note carries the state that qualifies it, never a bare sentence",
        all(n.get("state") for n in notes),
        f"{len(notes)} note(s); stateless: "
        f"{[n.get('memoryId') for n in notes if not n.get('state')][:3]}")
    findings = clean.get("findings") or []
    say("a finding says whether it is about the project or one of its repositories",
        all("aboutThisProject" in f and f.get("subject") for f in findings),
        f"{len(findings)} finding(s)")
    return a


def assess_timeline(data: dict, OUT_SCHEMA: dict) -> list[dict]:
    a: list[dict] = []

    def say(text: str, ok: bool, note: str = "") -> None:
        a.append({"assertion": text, "verdict": "PASS" if ok else "FAIL", "note": note})

    clean = {k: v for k, v in data.items() if not k.startswith("_")}
    try:
        jsonschema.validate(clean, OUT_SCHEMA)
        schema_ok, schema_note = True, ""
    except jsonschema.ValidationError as exc:
        schema_ok, schema_note = False, str(exc).splitlines()[0][:180]
    say("result validates against project-timeline-output.schema.json",
        schema_ok, schema_note)
    events = clean.get("events") or []
    say("every event carries occurredAt, never the store's column name",
        bool(events) and all("occurredAt" in e and "occurred_at" not in e
                             for e in events),
        f"{len(events)} event(s); keys: {sorted(events[0]) if events else []}")
    say("the payload is an object, not a JSON string the caller must parse",
        all(isinstance(e.get("payload"), dict) for e in events),
        str([type(e.get("payload")).__name__ for e in events[:3]]))
    stamps = [e.get("occurredAt") or "" for e in events]
    say("the events are newest first", stamps == sorted(stamps, reverse=True),
        str(stamps[:3]))
    return a


#: The only fields an inventory server row may carry. A row with anything else
#: is carrying something the capability does not publish, which is how a URL or
#: a header value would travel.
MCP_SERVER_FIELDS = {"name", "declaredIn", "transport", "answers", "status", "checkedAt"}


def assess_mcp_inventory(data: dict, OUT_SCHEMA: dict) -> list[dict]:
    """`machine.mcp.inventory` — names and verdicts only, and coverage named."""
    a: list[dict] = []

    def say(text: str, ok: bool, note: str = "") -> None:
        a.append({"assertion": text, "verdict": "PASS" if ok else "FAIL", "note": note})

    clean = {k: v for k, v in data.items() if not k.startswith("_")}
    try:
        jsonschema.validate(clean, OUT_SCHEMA)
        schema_ok, schema_note = True, ""
    except jsonschema.ValidationError as exc:
        schema_ok, schema_note = False, str(exc).splitlines()[0][:180]
    say("result validates against mcp-inventory-output.schema.json", schema_ok, schema_note)
    say("degraded is present, even when empty", isinstance(clean.get("degraded"), list),
        str(type(clean.get("degraded")).__name__))
    servers = clean.get("servers") or []
    extra = sorted({k for s in servers for k in s} - MCP_SERVER_FIELDS)
    say("a server row carries only name, declaredIn, transport, answers, status and checkedAt",
        not extra, f"{len(servers)} server(s); unexpected fields: {extra}")
    say("an empty server list always comes with a degraded row saying why",
        bool(servers) or bool(clean.get("degraded")),
        f"{len(servers)} server(s), {len(clean.get('degraded') or [])} degraded row(s)")
    return a


def assess(pid: str, data: dict, OUT_SCHEMA: dict, requested: dict | None = None) -> list[dict]:
    """One verdict per declared assertion, in declaration order."""
    a: list[dict] = []

    def say(text: str, ok: bool, note: str = "") -> None:
        a.append({"assertion": text, "verdict": "PASS" if ok else "FAIL", "note": note})

    # Validate the payload the server returned, not the one this runner annotated.
    clean = {k: v for k, v in data.items() if not k.startswith("_")}
    try:
        jsonschema.validate(clean, OUT_SCHEMA)
        schema_ok, schema_note = True, ""
    except jsonschema.ValidationError as exc:
        schema_ok, schema_note = False, str(exc).splitlines()[0][:180]

    requested = requested or local_fixture(next(
        p["inputFixture"] for cap in CAPS for p in cap["profile"]["probes"] if p["id"] == pid))
    if pid == "survey-single-project":
        say("result validates against capability-output.schema.json", schema_ok, schema_note)
        want = requested["scope"]["value"]
        say("counts.projects == 1 and projects[0].id equals the requested id",
            data["counts"]["projects"] == 1 and data["projects"][0]["id"] == want,
            f"counts={data['counts']}")
        proj = data["projects"][0]
        rules = " \n".join(proj["membershipRules"])
        say("membershipRules has exactly one entry per repository",
            all(r["nameWithOwner"] in rules for r in proj["repositories"]),
            f"{len(proj['repositories'])} repos, {len(proj['membershipRules'])} rules")
        srcs = {s["id"] for s in json.loads((paths.REGISTRY / "sources.json").read_text())["sources"]}
        say("evidence is non-empty and every entry resolves to a registry source id",
            bool(data["evidence"]) and set(data["evidence"]) <= srcs,
            f"unresolved: {sorted(set(data['evidence']) - srcs)}")
        say("degraded is present, even when empty", "degraded" in data)
        say("registry git status and ledger max revision are unchanged after the call",
            data["_no_write"], data.get("_no_write_note", ""))

    elif pid == "survey-rejects-name-inference":
        say("result validates against capability-output.schema.json", schema_ok, schema_note)
        proj = data["projects"][0] if data["projects"] else {"repositories": [], "membershipRules": []}
        got = sorted(r["nameWithOwner"] for r in proj["repositories"])
        owners = {n.split("/", 1)[0] for n in got}
        selected_repositories = set(got)
        wanted = requested["scope"]["value"]
        owner_of = {}
        for rel in json.loads((paths.REGISTRY / "relations.json").read_text())["relations"]:
            if rel["type"] == "implemented_by":
                owner_of[rel["to"].split(":", 1)[1]] = rel["from"]
        say("every repository returned is one the project's notes name or a link verified "
            "by hand",
            bool(got) and all(owner_of.get(n) == wanted for n in got), f"got {got}")
        all_owner = {r["name_with_owner"] for r in
                     json.loads((paths.REGISTRY / "repositories.json").read_text())["repositories"]
                     if r["name_with_owner"].split("/", 1)[0] in owners}
        absent = sorted(all_owner - selected_repositories)
        homed = {n: owner_of.get(n) for n in absent}
        say("every repository of that owner which is absent is absent because it anchors "
            "its own project",
            bool(absent) and all(v and v != wanted for v in homed.values()),
            f"absent={homed}")
        rules = " \n".join(proj["membershipRules"])
        say("every returned repository is named in a membershipRules entry",
            all(n in rules for n in got), f"{len(got)} repos")

    elif pid == "survey-declares-degradation":
        sources = {d["source"] for d in data["degraded"]}
        say("with the event store unreachable the call still returns a typed result", schema_ok,
            schema_note)
        say("degraded names the store and its reason", "store" in sources, f"{sorted(sources)}")
        say("degraded names each bitbucket workspace whose listing was not read, "
            "carrying the collector's own reason; it is silent when every workspace "
            "was listed",
            # A source is now `bitbucket:<workspace>`: the notice must say WHICH
            # surface is dark, and a bare "bitbucket" cannot. Silence is correct
            # once a credential exists, so absence of a thin repository is a pass.
            any(x == "bitbucket" or x.startswith("bitbucket:") for x in sources)
            or not [r for pr in data["projects"] for r in pr["repositories"]
                    if r.get("discoveredBy") == "local-remote-only"],
            f"{sorted(sources)}")
        local_only = [r for p in data["projects"] for r in p["repositories"]
                      if r.get("discoveredBy") == "local-remote-only"]
        say("repositories known only from a local remote carry discoveredBy=local-remote-only",
            bool(local_only), f"{len(local_only)} such repositories")
    return a


def params_for(scratch: pathlib.Path | None) -> StdioServerParameters:
    env = {**os.environ}
    if scratch is not None:
        env["OBSERVATORY_DB"] = str(scratch)
    interpreter = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").is_file() else sys.executable
    return StdioServerParameters(command=interpreter,
                                 args=[str(ROOT / "mcp/server.py")], cwd=str(ROOT), env=env)


async def probe_read_capability(session, cap: dict) -> list[dict]:
    """The probes of one read capability, against the LIVE store and asserting no write.

    The registry fingerprint and the ledger's highest revision are taken around
    every call, because `effect: none` is a claim and a probe that does not
    check it is a claim repeated.

    ONE SERVER FOR EVERY READ CAPABILITY. Each used to start its own, and with six
    capabilities the conformance suite, which runs the probes several times,
    crossed the portable runner's 120 s budget on a loaded CI machine (measured
    2026-09-30). A read capability's probe needs no server of its own: it asserts
    what the call wrote, which a shared server does not change."""
    out_schema = schema_for(cap)
    receipts = []
    for probe in cap["profile"]["probes"]:
        args = local_fixture(probe["inputFixture"])
        before = (registry_fingerprint(), ledger_max_revision())
        if probe["id"] in JOB_PROBES:
            results = await JOB_PROBES[probe["id"]](session, cap, args, out_schema)
        else:
            data = await call_tool_for(session, cap, args)
        after = (registry_fingerprint(), ledger_max_revision())
        # A THIRD READING, with no call between it and the second. It is
        # what tells "the call wrote" from "the tick wrote while the call
        # ran" — see `side_effect_verdict`.
        later = (registry_fingerprint(), ledger_max_revision())
        if probe["id"] not in JOB_PROBES:
            assessor = READ_ASSESSORS[probe["id"]]
            results = (assessor(data, out_schema, args) if assessor is assess_detail
                       else assessor(data, out_schema))
        # THE MANIFEST'S OWN WORDING, verbatim. The first version
        # appended a sentence of its own — "the call wrote nothing:
        # `effect: none`" — which `coverage()` correctly reported as
        # evaluated-but-not-declared: a probe checking more than the
        # published prose says is a contract that understates itself,
        # and `tests/test_conformance_receipt.py` failed on it the same
        # minute. The assertion is declared beside the others now.
        results.append(side_effect_verdict(before, after, later))
        cov = coverage(probe, results)
        receipts.append({
            "probe": probe["id"], "capability": cap["name"],
            "inputFixture": probe["inputFixture"],
            "sideEffectCeiling": probe["sideEffectCeiling"],
            **cov, "assertions": results})
    return receipts


async def call_tool_for(session, cap: dict, args: dict) -> dict:
    """Call the ONE tool a capability declares, with the fixture's arguments.

    The tool name comes from `requiredFeatures` rather than from a literal here:
    a capability that declares one tool and is probed against another is a
    receipt about the wrong subject.
    """
    features = [f for f in cap["profile"]["requiredFeatures"] if f.startswith("tool:")]
    # A capability served under its own name (fabric-interop/0.1) may also
    # require the job tools; the tool it is PROBED through is the one of its name.
    if f"tool:{cap['name']}" in features:
        features = [f"tool:{cap['name']}"]
    if len(features) != 1:
        raise AssertionError(
            f"{cap['name']} declares {len(features)} tools; a read capability probed "
            f"this way must declare exactly one, or the receipt cannot say which "
            f"answer it validated")
    name = features[0].split(":", 1)[1]
    # THE FIXTURE, VERBATIM. This translated camelCase into the snake_case the
    # tools used to take, and stripped any `_`-prefixed key — so the receipt said
    # 31/31 about a payload no host would ever send, and could not see that a
    # host sending the DECLARED shape got the whole estate instead of one project
    #. The wire publishes the contract's own names now, so a probe
    # that still rewrote the input would be measuring this runner.
    res = await session.call_tool(name, dict(args))
    return json.loads(res.content[0].text)


async def probe_refresh_job(session, cap: dict, args: dict, out_schema: dict) -> list[dict]:
    """`machine.mcp.refresh`: the job-handle path of fabric-interop/0.1, end to end.

    The handle, `fabric.job.get` until the job is terminal (bounded by the
    probe's own `timeoutMs`), the result envelope and its output, and the
    refusal of an id this agent never issued."""
    import interop
    a: list[dict] = []

    def say(text: str, ok: bool, note: str = "") -> None:
        a.append({"assertion": text, "verdict": "PASS" if ok else "FAIL", "note": note})

    probe = cap["profile"]["probes"][0]
    res = await session.call_tool(cap["name"], dict(args))
    handle = (json.loads(res.content[0].text) if res.content else {}).get("job") or {}
    say("the call answers a job handle whose status is working",
        not res.is_error and handle.get("status") == "working" and bool(handle.get("id")), str(handle)[:160])
    job, deadline = {}, asyncio.get_running_loop().time() + probe["timeoutMs"] / 1000
    while handle.get("id"):
        got = await session.call_tool(interop.JOB_GET, {"id": handle["id"]})
        job = (json.loads(got.content[0].text) if got.content else {}).get("job") or {}
        if job.get("status") in ("completed", "failed", "cancelled") \
                or asyncio.get_running_loop().time() > deadline:
            break
        await asyncio.sleep(0.25)
    say("fabric.job.get reaches completed within the probe timeout", job.get("status") == "completed",
        f"status={job.get('status')} error={job.get('error')}")
    result = job.get("result") or {}
    problems = [f"job answer: {e.message[:140]}" for e in
                interop.contract_validator("interop-job.schema.json").iter_errors({"job": job})][:3]
    try:
        jsonschema.validate(result.get("output"), out_schema)
    except jsonschema.ValidationError as exc:
        problems.append(f"output: {str(exc).splitlines()[0][:140]}")
    say("the completed result is an envelope whose output validates against mcp-inventory-output.schema.json",
        bool(result) and not problems, "; ".join(problems))
    stranger = await session.call_tool(interop.JOB_GET, {"id": "job-" + "0" * 32})
    body = stranger.content[0].text if stranger.content else ""
    say("fabric.job.get for an id this agent never issued answers isError with unknown-job",
        bool(stranger.is_error) and "unknown-job" in body, body[:120])
    return a


#: probe id -> the job assessor for it (`probe_read_capability` dispatches on this first).
JOB_PROBES = {"mcp-refresh-runs-as-a-job": probe_refresh_job}


async def run_write_capability(cap: dict) -> tuple[list[dict], str]:
    """A `draft`-effect capability is probed against a scratch store, never the live one."""
    # REMOVED WHEN THE PROCESS ENDS. `mkdtemp` does not clean up, and this one
    # holds a whole store — every probe run left one behind. Measured 2026-09-07:
    # a `check` run consumed 290 MB of temp fixtures on a volume reading 100%
    # full, and the run before it died with `no space left on device`.
    scratch_dir = tempfile.mkdtemp(prefix="observatory-probe-")  # paths-check: allow — removed at exit by the atexit line below
    atexit.register(shutil.rmtree, scratch_dir, ignore_errors=True)
    scratch = pathlib.Path(scratch_dir) / "probe.db"
    out_schema = schema_for(cap)
    receipts, negotiated = [], ""
    async with stdio_client(params_for(scratch)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.discover()
            negotiated = session.protocol_version
            for probe in cap["profile"]["probes"]:
                args = local_fixture(probe["inputFixture"])
                results = await assess_record(session, args, out_schema,
                                              scratch_db=scratch)
                receipts.append({
                    "probe": probe["id"], "capability": cap["name"],
                    "inputFixture": probe["inputFixture"],
                    "sideEffectCeiling": probe["sideEffectCeiling"],
                    "scratchStore": str(scratch),
                    **coverage(probe, results),
                    "assertions": results,
                })
    return receipts, negotiated


#: probe id -> the assessor for it. A read capability's probes are dispatched
#: BY NAME rather than by the capability's position in the list, which is what
#: `CAPS[0]` / `CAPS[1:]` did until rev-4 split one capability into three: the
#: two new read capabilities would have been probed as WRITE capabilities,
#: against a scratch store and an assessor for a different contract.
READ_ASSESSORS = {
    "detail-carries-every-section": assess_detail,
    "timeline-answers-in-camel-case-with-parsed-payloads": assess_timeline,
    "mcp-inventory-names-and-degrades": assess_mcp_inventory,
}


async def run() -> dict:
    read_caps = [c for c in CAPS if c.get("effect") == "none"]
    write_caps = [c for c in CAPS if c.get("effect") != "none"]
    CAP = read_caps[0]
    OUT_SCHEMA = schema_for(CAP)
    params = params_for(None)
    receipts = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            disc = await session.discover()
            negotiated = session.protocol_version
            for probe in CAP["profile"]["probes"]:
                args = local_fixture(probe["inputFixture"])
                before = (registry_fingerprint(), ledger_max_revision())
                if probe["id"] == "survey-declares-degradation":
                    # A separate child receives a directory as its DB path;
                    # no live or fixture database is renamed or modified.
                    with tempfile.TemporaryDirectory(prefix="observatory-degraded-probe-") as blocked:
                        async with stdio_client(params_for(pathlib.Path(blocked))) as (dr, dw):
                            async with ClientSession(dr, dw) as degraded_session:
                                await degraded_session.discover()
                                data = await call(degraded_session, args)
                else:
                    data = await call(session, args)
                after = (registry_fingerprint(), ledger_max_revision())
                data["_no_write"] = before == after
                data["_no_write_note"] = f"before={before} after={after}"
                results = assess(probe["id"], data, OUT_SCHEMA, args)
                data.pop("_no_write", None), data.pop("_no_write_note", None)
                receipts.append({
                    "probe": probe["id"],
                    "capability": CAP["name"],
                    "inputFixture": probe["inputFixture"],
                    "sideEffectCeiling": probe["sideEffectCeiling"],
                    **coverage(probe, results),
                    "assertions": results,
                })
            for cap in read_caps[1:]:
                receipts.extend(await probe_read_capability(session, cap))
    for cap in write_caps:
        extra, _ = await run_write_capability(cap)
        receipts.extend(extra)
    # WHAT THIS WAS MEASURED AGAINST, in the receipt rather than only in the
    # prose beside it. `fabric/FABRIC-CONFORMANCE.md` says "Contract 0.1.0 at
    # 20a818e6…, Provider revision 4" and the receipt recorded neither — so a
    # host reading `probe-receipts.json` had to take the document's word for
    # which contract those assertions were measured under, and `--check`
    # compared only `manifestContentHash`: a receipt taken against a DIFFERENT
    # contract revision passed silently. Three fields, from the two files that
    # pin them, so the evidence can be tied to its subject.
    lock = json.loads((ROOT / "fabric-contract.lock.json").read_text(encoding="utf-8"))
    return {"ranAt": now(),
            "capabilities": [c["name"] for c in CAPS],
            "protocolNegotiated": negotiated,
            "protocolDeclared": CAP["profile"]["protocolRevision"],
            "offeredVersions": list(disc.supported_versions),
            "manifestContentHash": MANIFEST["provider"]["contentHash"],
            "contractVersion": MANIFEST.get("contractVersion"),
            "contractCommit": lock.get("commit"),
            "localProfile": lock.get("profile"),
            "schemaRelease": lock.get("schemaRelease"),
            "schemaReleases": [r.get("release") for r in lock.get("releases", [])]
                              or [lock.get("schemaRelease")],
            "externalHostAdmission": "unverified",
            "providerRevision": MANIFEST["provider"].get("revision"),
            "probes": receipts}


def select_fixture_home(base: pathlib.Path) -> None:
    """Explicit synthetic workspace, isolated from ambient path overrides."""
    import configuration
    import importlib
    import workspace
    base = base.expanduser().absolute()
    workspace.reject_symlinks(base)
    configuration.validate_workspace(base, required=True)
    settings = configuration.load(base)
    if settings.get("features", {}).get("probe_fixture") is not True:
        raise configuration.ConfigurationError("Probe workspace must explicitly enable feature probe_fixture")
    if any(settings.get("integrations", {}).values()) or settings.get("sources"):
        raise configuration.ConfigurationError("Probe workspace must not enable integrations or external sources")
    for name in list(os.environ):
        if name.startswith("OBSERVATORY_"):
            os.environ.pop(name)
    os.environ.update({"OBSERVATORY_HOME": str(base), "OBSERVATORY_DATA": str(base / "projects"),
                       "OBSERVATORY_VAULT": str(base / "wiki"),
                       "OBSERVATORY_VAULT_DIR": str(base / "secrets/projects")})
    importlib.reload(paths)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run bounded MCP probes in an explicitly synthetic workspace")
    parser.add_argument("--fixture-home", type=pathlib.Path, required=True)
    parser.add_argument("--check", action="store_true")
    options = parser.parse_args()
    try:
        select_fixture_home(options.fixture_home)
    except (RuntimeError, OSError):
        parser.error("An initialized, isolated synthetic fixture home is required")
    # `--check` runs every probe and writes NOTHING. The receipts are a dated,
    # published artefact — `fabric/FABRIC-CONFORMANCE.md` cites them — so
    # refreshing them is a deliberate act, not something a gate does on the way
    # past. Rewriting them on every run meant a clean tree could not survive its
    # own gate, and after that `git status` answers a question nobody asked.
    CHECK = options.check
    drift = []
    RECEIPTS = paths.STATE / "probe-receipts.json"
    report = asyncio.run(run())
    if CHECK:
        # A stale receipt is only a problem when it describes a different
        # SUBJECT. Everything else in it is re-measured by this very run — but
        # "the subject" is four things, not one: the manifest's content hash, the
        # contract version, the pinned contract commit and the provider's
        # revision. Comparing the hash alone let a receipt measured against
        # another contract revision pass, in the artefact whose entire purpose is
        # to say what these assertions were measured under.
        stored = json.loads(RECEIPTS.read_text(encoding="utf-8")) if RECEIPTS.exists() else {}
        drift[:] = [(k, stored.get(k), report.get(k))
                 for k in ("manifestContentHash", "contractVersion",
                           "contractCommit", "providerRevision")
                 if stored.get(k) != report.get(k)]
        # REPORTED, then the run is still shown. The first version raised here,
        # so a drifted receipt produced three lines about the receipt and NOT ONE
        # probe verdict — a check that hides its own measurement to complain
        # about the paperwork. The exit code is decided at the end, with
        # everything else.
        if drift:
            for key, was, now_ in drift:
                print(f"receipts describe {key}={was!r}, it is now {now_!r}",
                      file=sys.stderr)
            print("re-run `project-observatory full probes` to refresh them", file=sys.stderr)
    else:
        atomic.write_text(RECEIPTS,
                          json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(f"capabilities: {', '.join(report['capabilities'])}")
    print(f"protocol declared {report['protocolDeclared']} | negotiated {report['protocolNegotiated']}")
    ok = report["protocolDeclared"] == report["protocolNegotiated"]
    for p in report["probes"]:
        mark = "\033[32mPASS\033[0m" if p["verdict"] == "PASS" else "\033[31mFAIL\033[0m"
        print(f"\n{mark}  {p['probe']}  [{p.get('capability','?')}]  "
              f"({p['evaluatedAssertions']}/{p['declaredAssertions']} assertions evaluated)")
        for a in p["assertions"]:
            flag = " " if a["verdict"] == "PASS" else "!"
            print(f"   {flag} {a['verdict']}  {a['assertion']}")
            if a["verdict"] != "PASS" and a["note"]:
                print(f"          {a['note']}")
        ok = ok and p["verdict"] == "PASS"
    print("\nreceipts verified, not rewritten (--check)" if CHECK
          else "\nreceipts written in the selected private fixture workspace")
    raise SystemExit(0 if ok and not drift else 1)
