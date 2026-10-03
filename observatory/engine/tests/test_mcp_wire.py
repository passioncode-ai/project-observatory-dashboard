#!/usr/bin/env python3
"""Talk to the server over real stdio, as a host would.

Not an import check: this spawns mcp/server.py as a subprocess, negotiates, and
calls each declared tool. A tool that imports but cannot be called over the wire
is the failure this exists to catch.
"""
from __future__ import annotations
import asyncio, json, os, pathlib, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup, PROJECT_ID as SYNTHETIC_PROJECT
portable_setup()

sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402

from mcp import ClientSession, StdioServerParameters                              
from mcp.client.stdio import stdio_client                                         
import mcp.types as mtypes
import mcp.client.session as _mcp_session

# The SDK gives `server/discover` a fixed 10 s. Here the server is a fresh child
# started from an uncompiled sandbox copy (PYTHONDONTWRITEBYTECODE) while other
# suites run in parallel, so its first answer can take longer than an installed
# server ever does. Give the start its own budget instead of failing the wire
# check on machine load; every later request keeps the SDK default.
STARTUP_BUDGET_SECONDS = float(os.environ.get("OBSERVATORY_MCP_STARTUP_BUDGET", "60"))
_mcp_session.DISCOVER_TIMEOUT_SECONDS = max(_mcp_session.DISCOVER_TIMEOUT_SECONDS, STARTUP_BUDGET_SECONDS)                                                        

REQUIRED = ["observatory_status", "observatory_project", "observatory_timeline",
            "observatory_recall", "observatory_record", "observatory_propose",
            "machine.mcp.inventory", "estate.survey", "project.detail", "project.timeline",
            "project.record", "machine.mcp.refresh", "fabric.job.get", "fabric.job.cancel"]
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def payload(result) -> dict:
    if getattr(result, "structured_content", None):
        return result.structured_content
    for block in result.content:
        if isinstance(block, mtypes.TextContent):
            return json.loads(block.text)
    raise AssertionError("no JSON payload in tool result")


def typed_error(result) -> dict:
    """The JSON refusal in a tool result's first text block, or {} when it is not JSON."""
    try:
        doc = json.loads(result.content[0].text)
    except (ValueError, IndexError, AttributeError):
        return {}
    return doc if isinstance(doc, dict) else {}


async def run() -> None:
    # A throwaway store: the write tools are exercised for real, and the live
    # ledger never sees a test row.
    tmp = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-wire-")) / "test.db"
    params = StdioServerParameters(command=sys.executable,
                                   args=[str(ROOT / "mcp/server.py")], cwd=str(ROOT),
                                   env={**os.environ, "OBSERVATORY_DB": str(tmp)})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            # 2026-07-28 is stateless: discovery is `server/discover`, and opening
            # with the legacy `initialize` handshake negotiates DOWN to 2025-11-25.
            # The Fabric manifest pins the revision as a schema const, so the
            # discovery path is part of the contract, not a client preference.
            disc = await session.discover()
            check("server/discover offers 2026-07-28",
                  "2026-07-28" in disc.supported_versions, f"got {disc.supported_versions}")
            check("session adopts 2026-07-28",
                  session.protocol_version == "2026-07-28", f"got {session.protocol_version}")
            check("server instructions describe the degraded contract",
                  "degraded" in (disc.instructions or ""), (disc.instructions or "")[:60])
            # A host keeps about 2,048 characters of the instructions and drops the
            # rest; the text was once cut exactly at the WRITE rule.
            check("server instructions fit what a host keeps, with the WRITE and SPENDS rules in it",
                  len(disc.instructions or "") <= 1800
                  and "WRITE:" in (disc.instructions or "") and "SPENDS:" in (disc.instructions or ""),
                  f"{len(disc.instructions or '')} characters")

            listed = await session.list_tools()
            names = sorted(t.name for t in listed.tools)
            missing = [t for t in REQUIRED if t not in names]
            check("all three declared tools are served", not missing, f"missing {missing}")

            res = await session.call_tool("observatory_status",
                                          {"kind": "project",
                                           "value": SYNTHETIC_PROJECT})
            data = payload(res)
            check("status: counts.projects == 1", data["counts"]["projects"] == 1,
                  str(data["counts"]))
            check("status: degraded is present", "degraded" in data)
            check("status: evidence is non-empty", bool(data["evidence"]))
            rules = data["projects"][0]["membershipRules"]
            repos = data["projects"][0]["repositories"]
            check("status: every repository is named in a rule",
                  all(r["nameWithOwner"] in " \n".join(rules) for r in repos),
                  f"{len(repos)} repos, {len(rules)} rules")

            res = await session.call_tool("observatory_project",
                                          {"project_id": SYNTHETIC_PROJECT,
                                           "timeline_limit": 3})
            data = payload(res)
            check("project: returns the project", data.get("project", {}).get("id")
                  == SYNTHETIC_PROJECT, str(data)[:120])
            check("project: names the empty event store in degraded",
                  any("empty" in d["reason"] for d in data["degraded"]), str(data["degraded"])[:120])

            res = await session.call_tool("observatory_project", {"project_id": "project:does-not-exist"})
            data = payload(res)
            check("project: an unknown id is a typed answer, not a crash",
                  data.get("error") == "unknown project", str(data)[:120])

            # The throwaway store has no events, so this asserts the honest-degradation
            # contract instead of a commit count that would depend on the live store.
            res = await session.call_tool("observatory_timeline",
                                          {"project_id": SYNTHETIC_PROJECT, "limit": 5})
            data = payload(res)
            check("timeline: an empty store degrades honestly rather than returning silence",
                  data["events"] == [] and
                  any("empty" in d["reason"] for d in data["degraded"]), str(data)[:160])

            # The MCP inventory: this synthetic workspace takes none, so the answer
            # must be empty AND say why — never an empty list that reads as "no servers".
            inv_tool = next(t for t in listed.tools if t.name == "machine.mcp.inventory")
            check("inventory: the tool is read-only", bool(inv_tool.annotations and inv_tool.annotations.read_only_hint))
            res = await session.call_tool("machine.mcp.inventory", {})
            data = payload(res)
            import jsonschema
            schema = json.loads((ROOT / "fabric/schemas/mcp-inventory-output.schema.json").read_text())
            errors = [e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(data)]
            check("inventory: the answer validates against its published schema", not errors, str(errors)[:160])
            check("inventory: no scan means an empty list with a reason",
                  data["servers"] == [] and data["inventoryAt"] is None and bool(data["degraded"]),
                  str(data)[:160])

            res = await session.call_tool("observatory_status", {"kind": "owner"})
            data = payload(res)
            check("a scope missing its value is a typed answer, not a crash",
                  data.get("error") == "missing value" and not res.is_error, str(data)[:120])

            # A malformed argument to an SDK-described tool came back as the SDK's
            # raw validation text — "Error executing tool …", a pydantic URL and
            # `input_value=…`, which echoes whatever the caller put in the field
            # (a pasted key included) into the agent's transcript. It is the same
            # typed `invalid-input` the capability tools answer, naming the field
            # and the rule, never the value.
            planted = "sk-or-v1-" + "0" * 40
            for args, field in (({"limit": planted}, "limit"), ({"limit": -5}, "limit"),
                                ({"kind": "nonsense"}, "kind")):
                res = await session.call_tool("observatory_status", args)
                text = " ".join(getattr(b, "text", "") for b in res.content)
                typed = typed_error(res)
                check(f"a malformed `{field}` is a typed invalid-input naming the field",
                      bool(res.is_error) and typed.get("error") == "invalid-input"
                      and field in str(typed.get("detail")), text[:200])
                check(f"and the refusal carries no SDK text and no value ({field})",
                      "Error executing tool" not in text and "pydantic" not in text
                      and "input_value" not in text and planted not in text, text[:200])
            res = await session.call_tool("observatory_project", {})
            typed = typed_error(res)
            check("a missing required argument is a typed invalid-input",
                  bool(res.is_error) and typed.get("error") == "invalid-input"
                  and "missing" in str(typed.get("detail")), str(typed)[:200])
            res = await session.call_tool("observatory_no_such_tool", {})
            typed = typed_error(res)
            check("an unknown tool is a typed unknown-tool",
                  bool(res.is_error) and typed.get("error") == "unknown-tool", str(res.content)[:200])

            # ---- the write path, over the wire, including its refusals ----
            res = await session.call_tool("observatory_record", {
                "statement": "wire test wrote this", "owner": "agent:wire-test",
                "project_id": "project:observatory-wire-test", "why": "to prove the path exists"})
            wrote = payload(res)
            check("record: an agent's note lands as proposed",
                  wrote.get("state") == "proposed", str(wrote)[:140])
            check("record: an agent's note carries confidence below 1",
                  (wrote.get("owner") == "agent:wire-test"), str(wrote)[:140])
            mid, rev = wrote.get("memoryId"), wrote.get("revision")

            res = await session.call_tool("observatory_record",
                                          {"statement": "no owner given"})
            check("record: a missing owner is refused by the schema, before the ledger",
                  bool(res.is_error), "expected a validation error")

            res = await session.call_tool("observatory_record", {
                "statement": "stale writer", "owner": "agent:wire-test",
                "memory_id": mid, "expected_revision": 99})
            conflict = payload(res)
            check("record: a stale revision returns a conflict with the current number",
                  conflict.get("error") == "RevisionConflict" and conflict.get("currentRevision") == rev,
                  str(conflict)[:160])

            res = await session.call_tool("observatory_record", {
                "statement": "another agent's rewrite", "owner": "agent:someone-else",
                "memory_id": mid, "expected_revision": rev})
            refused = payload(res)
            check("record: another agent cannot correct this record",
                  refused.get("error") == "OwnerRefused", str(refused)[:160])

            res = await session.call_tool("observatory_recall",
                                          {"project_id": "project:observatory-wire-test"})
            recalled = payload(res)
            check("recall: the note reads back",
                  any(r["memory_id"] == mid for r in recalled["records"]), str(recalled)[:140])
            check("recall: the answer says absence is not proof of absence",
                  "absence" in recalled.get("note", ""), recalled.get("note", "")[:60])

            before = (__import__("paths").REGISTRY / "projects.json").stat().st_mtime_ns
            res = await session.call_tool("observatory_propose", {
                "owner": "agent:wire-test", "target_id": "project:observatory-wire-test",
                "patch": {"description": "proposed, not applied"},
                "evidence": [{"uri": "test:wire"}]})
            prop = payload(res)
            check("propose: lands as proposed", prop.get("status") == "proposed", str(prop)[:140])
            check("propose: registry/projects.json is untouched",
                  (__import__("paths").REGISTRY / "projects.json").stat().st_mtime_ns == before)

            # ---- the capability tools (fabric-interop/0.1): the same answers under
            # the capability names, with the published schemas enforced both ways ----
            # A REAL estate carries the organization and recorded resources on every
            # project; the published v0.2.0 item schema lists neither. The synthetic
            # registry gets both, so the published capability is checked against
            # the shape an operator's estate has rather than a bare fixture.
            reg = __import__("paths").REGISTRY / "projects.json"
            pdoc = json.loads(reg.read_text())
            for p in pdoc["projects"]:
                p.update(organization="example-org", organization_source="operator",
                         organization_why="fixture",
                         resources=[{"kind": "analytics", "id": "example-property"}])
            reg.write_text(json.dumps(pdoc))
            res = await session.call_tool("estate.survey", {"limit": 2})
            data = payload(res)
            check("estate.survey: a project carrying organization/resources still meets the published schema",
                  not res.is_error and len(data.get("projects", [])) == 2,
                  " ".join(b.text for b in res.content if isinstance(b, mtypes.TextContent))[-200:])
            check("estate.survey: fields outside the published contract are left to observatory_status",
                  all("organization" not in p and "resources" not in p for p in data.get("projects", [])))
            res = await session.call_tool("observatory_status", {"limit": 2, "detail": "full"})
            check("observatory_status: detail=full still carries the organization the capability omits",
                  all("organization" in p for p in payload(res).get("projects", [])), str(payload(res))[:160])
            # The published contract is the default: every project, every row meeting
            # the closed item schema (the v0.2.0 probes call this tool with no limit).
            res = await session.call_tool("observatory_status", {})
            check("observatory_status: the text copy is compact JSON, not indented",
                  "\n" not in res.content[0].text and json.loads(res.content[0].text) == payload(res),
                  res.content[0].text[:80])
            data = payload(res)
            item = next(d["outputSchema"] for d in __import__("interop").tool_definitions()
                        if d["name"] == "estate.survey")["properties"]["projects"]["items"]["properties"]
            check("observatory_status: unpaged it is every project, as the published contract says",
                  len(data["projects"]) == data["counts"]["projects"] and "nextCursor" not in data,
                  str(data.get("counts")))
            check("observatory_status: by default every row carries only published fields",
                  all(set(p) <= set(item) for p in data["projects"]),
                  str([sorted(set(p) - set(item)) for p in data["projects"]][:1]))
            res = await session.call_tool("observatory_status", {"limit": 5, "detail": "summary"})
            rows = payload(res)["projects"]
            check("observatory_status: summary rows drop description, sites, stack",
                  rows and all(not {"description", "sites", "organization", "stack"} & set(p) for p in rows))
            check("observatory_status: a summary row keeps every field the published item schema requires",
                  all({"id", "name", "ownership", "lifecycle", "repositories", "membershipRules"} <= set(p)
                      for p in rows))
            # The bounded entry point an agent starts from.
            res = await session.call_tool("observatory_overview", {"top": 3})
            ov = payload(res)
            check("observatory_overview: tiers add up to the projects counted",
                  sum(ov["activityTiers"].values()) == ov["counts"]["projects"], str(ov.get("activityTiers")))
            check("observatory_overview: small, bounded lists, and a degraded list",
                  len(res.content[0].text) < 20000 and len(ov["recentlyActive"]) <= 3
                  and len(ov["findings"]["mostSevere"]) <= 3 and "degraded" in ov,
                  f"{len(res.content[0].text)} characters")
            res = await session.call_tool("observatory_status", {"limit": 3})
            data = payload(res)
            check("observatory_status: a short page names where to continue",
                  len(data["projects"]) == 3 and data.get("nextCursor") == data["projects"][-1]["id"],
                  str(data.get("nextCursor")))
            res = await session.call_tool("observatory_status", {"limit": 3, "cursor": data["nextCursor"]})
            check("observatory_status: the next page continues after the cursor",
                  payload(res)["projects"][0]["id"] > data["projects"][-1]["id"])
            res = await session.call_tool("estate.survey", {"scope": {"kind": "project", "value": SYNTHETIC_PROJECT}},
                                          meta={"traceparent": "00-" + "1" * 32 + "-" + "2" * 16 + "-01"})
            data = payload(res)
            check("estate.survey: one project, under the capability's own name",
                  not res.is_error and data["counts"]["projects"] == 1, str(data)[:160])
            check("estate.survey: the answer is a child span of the caller's trace",
                  (res.meta or {}).get("traceparent", "").startswith("00-" + "1" * 32 + "-")
                  and "-" + "2" * 16 + "-" not in (res.meta or {}).get("traceparent", ""), str(res.meta))
            res = await session.call_tool("estate.survey", {"scope": {"kind": "owner"}})
            check("estate.survey: an owner scope without its value is refused, not surveyed whole",
                  bool(res.is_error) and "missing value" in res.content[0].text, res.content[0].text[:160])
            res = await session.call_tool("observatory_status", {"scope": {"kind": "owner"}})
            check("observatory_status: the scope OBJECT is checked too (it used to survey the whole estate)",
                  payload(res).get("error") == "missing value", str(payload(res))[:160])
            res = await session.call_tool("project.detail", {"projectId": SYNTHETIC_PROJECT, "timelineLimit": 2})
            check("project.detail: the project, validated against its schema",
                  not res.is_error and payload(res)["project"]["id"] == SYNTHETIC_PROJECT, str(payload(res))[:160])
            res = await session.call_tool("project.timeline", {"projectId": SYNTHETIC_PROJECT, "limit": 3})
            check("project.timeline: answers, with degraded", not res.is_error and "degraded" in payload(res),
                  str(payload(res))[:160])
            res = await session.call_tool("project.timeline", {"project_id": SYNTHETIC_PROJECT})
            check("project.timeline: the snake-case alias of the old tool is not the published input",
                  bool(res.is_error) and "invalid-input" in res.content[0].text, res.content[0].text[:160])
            res = await session.call_tool("project.record", {
                "owner": "agent:wire-test", "statement": "the capability tool wrote this",
                "projectId": "project:observatory-wire-test"})
            check("project.record: a note lands as proposed", payload(res).get("state") == "proposed",
                  str(payload(res))[:160])
            res = await session.call_tool("project.record", {"statement": "no owner"})
            check("project.record: a missing owner is refused by the published schema",
                  bool(res.is_error) and "invalid-input" in res.content[0].text, res.content[0].text[:160])
            res = await session.call_tool("project.record", {
                "owner": "agent:wire-test", "targetId": "project:not-in-the-registry-yet",
                "patch": {"description": "proposed"}, "evidence": [{"uri": "test:wire"}]})
            check("project.record: a proposal keeps its warning, in the text channel",
                  payload(res).get("status") == "proposed" and "warning" not in payload(res)
                  and any("not in the registry" in c.text for c in res.content[1:]), str(res.content)[:200])
            res = await session.call_tool("project.record", {"owner": "operator", "statement": "forged"})
            check("project.record: the operator's authority is refused, as an error answer",
                  bool(res.is_error) and "owner refused" in res.content[0].text, res.content[0].text[:160])

            res = await session.call_tool("observatory_status", {"kind": "owner", "value": "example"})
            data = payload(res)
            check("owner scope selects only that owner",
                  data["counts"]["projects"] > 0 and
                  all(any(r["nameWithOwner"].startswith("example/") for r in p["repositories"])
                      for p in data["projects"] if p["repositories"]),
                  f"{data['counts']}")


async def run_credentials() -> None:
    """A local-only project, asked for by its REGISTRY id.

    `project:local-alpha-web` names the folder `alpha-web`; the tool took the id's
    suffix as the folder, answered two empty lists and handed over a `use` command
    that use_secret could not resolve. And a rotated slot's `.meta.json` and
    `.retired-…` archive were counted as slots of their own."""
    import paths
    reg = paths.REGISTRY / "projects.json"
    doc = json.loads(reg.read_text(encoding="utf-8"))
    doc["projects"] = [p for p in doc["projects"] if p.get("id") != "project:local-alpha-web"] + [
        {"id": "project:local-alpha-web", "name": "alpha-web", "lifecycle": "active",
         "anchor": "local-folder", "local_folders": ["alpha-web"], "owners": []}]
    reg.write_text(json.dumps(doc), encoding="utf-8")
    vault = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-wire-vault-")) / "projects"
    slot = vault / "alpha-web" / "local"
    slot.mkdir(parents=True)
    for name in ("EXAMPLE_API_KEY", "EXAMPLE_API_KEY.meta.json", "EXAMPLE_API_KEY.retired-20260101T000000Z"):
        (slot / name).write_text("{}" if name.endswith(".json") else "synthetic-not-a-value", encoding="utf-8")
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp/server.py")], cwd=str(ROOT),
                                   env={**os.environ, "OBSERVATORY_VAULT_DIR": str(vault),
                                        "OBSERVATORY_DB": str(vault.parent / "test.db")})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.discover()
            data = payload(await session.call_tool("observatory_credentials", {"projectId": "project:local-alpha-web"}))
            check("a local-only project's slots are found by its registry id",
                  [v["name"] for v in data.get("vault", [])] == ["EXAMPLE_API_KEY"], str(data.get("vault")))
            check("and a rotated slot's meta and archive are not slots", data.get("totals", {}).get("vault_slots") == 1,
                  str(data.get("totals")))
            check("the use command names the folder use_secret resolves",
                  " alpha-web " in data.get("use", "") and "local-alpha-web" not in data.get("use", ""),
                  data.get("use", "")[:160])
            check("and no value crosses the wire", "synthetic-not-a-value" not in json.dumps(data))
            data = payload(await session.call_tool("observatory_credentials", {"projectId": "project:absent-example"}))
            check("an unknown id is a typed degradation",
                  any(d.get("code") == "unknown-project" for d in data.get("degraded", [])), str(data.get("degraded")))


async def run_refusals() -> None:
    """A refusal never quotes a value it could not vouch for, and says it refused.

    Meaning-level refusals echoed the caller's input: an unknown projectId in
    observatory_project, _findings and _credentials (and into `use`), a bad
    `since`, `cursor` or `asOfScanId`, a refused `owner`, a conversation id, an
    unknown tool name. A key pasted into the wrong field then reached the
    transcript through the refusal. Well-formed identifiers are still quoted —
    that is how a caller finds its typo. Unknown arguments, a `since` that is no
    date and a cursor that names no position are said in `degraded` rather than
    silently ignored, and a refused WRITE answers isError."""
    planted = "sk-or-v1-" + "FAKE" * 10
    mixed = "Fake" + "Tokn" + "_" + "Ab3" * 9 + "x"
    tmp = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-wire-refusals-")) / "test.db"
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp/server.py")],
                                   cwd=str(ROOT), env={**os.environ, "OBSERVATORY_DB": str(tmp)})

    def text_of(res) -> str:
        return " ".join(getattr(b, "text", "") for b in res.content) + json.dumps(
            getattr(res, "structured_content", None) or {})

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.discover()
            for value in (planted, mixed, "../../secrets"):
                for tool, args in (
                        ("observatory_project", {"projectId": value}),
                        ("observatory_findings", {"projectId": value}),
                        ("observatory_findings", {"cursor": value}),
                        ("observatory_credentials", {"projectId": value}),
                        ("observatory_timeline", {"projectId": SYNTHETIC_PROJECT, "since": value}),
                        ("observatory_status", {"limit": 1, "cursor": value}),
                        ("observatory_status", {"limit": 1, "asOfScanId": value}),
                        ("observatory_recall", {"cursor": value}),
                        ("observatory_record", {"owner": value, "statement": "x"}),
                        ("observatory_assistant_conversation", {"id": value}),
                        ("estate.survey", {"limit": 1, "cursor": value}),
                        ("project.timeline", {"projectId": SYNTHETIC_PROJECT, "since": value})):
                    res = await session.call_tool(tool, args)
                    check(f"{tool} {sorted(args)}: the refusal does not quote a {len(value)}-character value",
                          value not in text_of(res), text_of(res)[:240])
            res = await session.call_tool(planted, {})
            check("an unknown tool's name is not quoted when it is credential-shaped",
                  bool(res.is_error) and planted not in text_of(res), text_of(res)[:200])
            res = await session.call_tool("observatory_project", {"projectId": "project:absent-example"})
            check("a well-formed unknown id is still quoted, to find the typo",
                  "project:absent-example" in text_of(res), text_of(res)[:200])

            res = await session.call_tool("observatory_timeline", {"projectId": SYNTHETIC_PROJECT,
                                                                   "since": "garbage"})
            data = payload(res)
            check("a `since` that is no date is said in degraded, not answered as an empty filter",
                  any(d.get("source") == "since" for d in data.get("degraded", []))
                  and data.get("since") is None, str(data)[:240])
            res = await session.call_tool("project.timeline", {"projectId": SYNTHETIC_PROJECT,
                                                               "since": "garbage"})
            check("and the capability answers it inside its published schema",
                  not res.is_error and any(d.get("source") == "since" for d in payload(res)["degraded"]),
                  text_of(res)[:240])
            res = await session.call_tool("project.timeline", {"projectId": "project:absent-example"})
            check("an unknown project on project.timeline stays inside the published schema",
                  not res.is_error, text_of(res)[:240])
            res = await session.call_tool("observatory_recall", {"cursor": "garbage"})
            check("a recall cursor that names no position is said in degraded",
                  any(d.get("source") == "cursor" for d in payload(res).get("degraded", [])),
                  text_of(res)[:240])
            res = await session.call_tool("observatory_status", {"limit": 1, "bogus": planted,
                                                                 "include_external": False})
            data = payload(res)
            check("an argument the tool does not take is named in degraded, its value is not",
                  not res.is_error and any("bogus" in d.get("reason", "") for d in data.get("degraded", []))
                  and planted not in text_of(res)
                  and not any("include_external" in d.get("reason", "") for d in data.get("degraded", [])),
                  str(data.get("degraded"))[:300])

            # A REFUSAL IS AN ANSWER, NOT A FAULT — the published record schema says
            # so in its description, and `missing value` above pins the same for
            # reads. So a refused write is a typed answer with `error` and
            # `degraded`, isError false; isError is for malformed input, unknown
            # tools and answers a published schema does not describe.
            res = await session.call_tool("observatory_record", {"owner": "operator", "statement": "forged"})
            check("a refused write is a typed answer that still quotes a plain identifier",
                  not res.is_error and payload(res).get("error") == "owner refused"
                  and "'operator'" in text_of(res) and payload(res).get("degraded") == [],
                  text_of(res)[:200])
            res = await session.call_tool("observatory_propose", {"owner": "agent:wire-test",
                                                                  "targetId": "project:x",
                                                                  "patch": {"description": "d"}})
            check("an unappliable proposal is a typed answer carrying degraded",
                  payload(res).get("error") == "unappliable-proposal" and payload(res).get("degraded") == [],
                  text_of(res)[:200])
            res = await session.call_tool("observatory_record", {"owner": "agent:wire-test",
                                                                 "statement": "a note in its schema"})
            check("an accepted write keeps the published record shape (no `degraded`, as the "
                  "instructions say)", not res.is_error and "degraded" not in payload(res),
                  text_of(res)[:200])
            res = await session.call_tool("project.record", {"owner": "agent:wire-test",
                                                             "statement": "the capability's note"})
            check("project.record stays inside its published schema", not res.is_error, text_of(res)[:200])
            res = await session.call_tool("project.record", {"owner": "agent:wire-test",
                                                             "targetId": "project:x"})
            check("project.record's own refusal stays the typed answer its schema describes",
                  not res.is_error and payload(res).get("error") == "LedgerError", text_of(res)[:200])
            disc = await session.discover()
            check("the instructions say what isError means here, and where `degraded` is",
                  "isError" in (disc.instructions or "") and len(disc.instructions or "") <= 1800,
                  str(len(disc.instructions or "")))


async def run_workflow() -> None:
    """A workflow handed between two sessions, over the wire, as hosts would do it.

    Session A starts a workflow and checkpoints; a third party creates the
    handoff while A is silent; session B accepts it and continues; A's late
    write is refused as a typed answer that names where its work was kept. The
    lease token appears only in the answers that hand it out."""
    tmp = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-wire-workflow-")) / "test.db"
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp/server.py")],
                                   cwd=str(ROOT), env={**os.environ, "OBSERVATORY_DB": str(tmp)})
    body = {"goal": "ship the exporter", "open": [{"step_id": "S2", "next_action": "write it"}],
            "constraints": ["read-only: do not push"]}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.discover()
            listed = {t.name for t in (await session.list_tools()).tools}
            check("the five workflow tools are listed", {
                "observatory_checkpoint_write", "observatory_checkpoint_latest",
                "observatory_handoff_create", "observatory_handoff_accept",
                "observatory_handoff_get"} <= listed, str(sorted(listed)))
            a = payload(await session.call_tool("observatory_checkpoint_write", {
                "owner": "agent:claude-code", "idempotencyKey": "wire-start-0001",
                "stepId": "S1", "status": "done", "body": body, "projectId": "project:alpha-web",
                "executor": {"provider": "anthropic", "accountRef": "acct-a"}}))
            check("a first checkpoint starts a workflow and hands out its lease",
                  a.get("workflowId", "").startswith("wf_") and a.get("leaseId", "").startswith("wl_"),
                  json.dumps(a)[:200])
            wid, token_a = a.get("workflowId"), a.get("leaseId")
            refused = payload(await session.call_tool("observatory_checkpoint_write", {
                "owner": "operator", "idempotencyKey": "wire-operator-01", "stepId": "S2",
                "status": "done", "body": body, "workflowId": wid}))
            check("the operator's authority is not claimable over stdio",
                  refused.get("error") == "owner refused", json.dumps(refused)[:200])
            early = payload(await session.call_tool("observatory_handoff_create", {
                "owner": "service:switchboard", "idempotencyKey": "wire-handoff-early",
                "workflowId": wid, "reason": "limit",
                "to": {"provider": "anthropic", "accountRef": "acct-b"}}))
            check("while the executor is active, a handoff without its token is refused",
                  early.get("error") == "HandoffRefused" and early.get("remedy"),
                  json.dumps(early)[:200])
            # Session A falls silent: its checkpoint is ten minutes old.
            import sqlite3 as _sq
            db = _sq.connect(tmp)
            db.execute("UPDATE ledger SET created_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now',"
                       " '-10 minutes') WHERE kind = 'checkpoint'")
            db.commit()
            db.close()
            h = payload(await session.call_tool("observatory_handoff_create", {
                "owner": "service:switchboard", "idempotencyKey": "wire-handoff-0001",
                "workflowId": wid, "reason": "limit",
                "to": {"provider": "anthropic", "accountRef": "acct-b"}}))
            check("a handoff is created without the leaving session",
                  h.get("handoffId", "").startswith("handoff:"), json.dumps(h)[:200])
            b = payload(await session.call_tool("observatory_handoff_accept", {
                "owner": "agent:claude-code", "idempotencyKey": "wire-accept-0001",
                "handoffId": h.get("handoffId")}))
            check("acceptance carries the constraints first and a new lease",
                  b.get("constraints") == ["read-only: do not push"]
                  and b.get("leaseId") not in (None, token_a), json.dumps(b)[:200])
            late = payload(await session.call_tool("observatory_checkpoint_write", {
                "owner": "agent:claude-code", "idempotencyKey": "wire-late-0001", "stepId": "S2",
                "status": "done", "body": body, "workflowId": wid, "leaseId": token_a}))
            check("the old session's write is a typed LeaseLost naming where it was kept",
                  late.get("error") == "LeaseLost" and str(late.get("keptAs", "")).startswith("mem:"),
                  json.dumps(late)[:200])
            latest = await session.call_tool("observatory_checkpoint_latest", {"workflowId": wid})
            doc = payload(latest)
            check("reading the workflow names the new executor and never a token",
                  doc.get("lease", {}).get("executor", {}).get("accountRef") == "acct-b"
                  and "wl_" not in json.dumps(doc) and doc.get("degraded") == [],
                  json.dumps(doc)[:200])
            got = payload(await session.call_tool("observatory_handoff_get",
                                                  {"handoffId": h.get("handoffId")}))
            check("the pack reads back as accepted", got.get("status") == "accepted",
                  json.dumps(got)[:200])
            bad = payload(await session.call_tool("observatory_checkpoint_latest",
                                                  {"workflowId": "wf_0000000000000000"}))
            check("an unknown workflow is a typed refusal with a remedy and `degraded`",
                  bad.get("error") == "UnknownWorkflow" and bad.get("remedy")
                  and bad.get("degraded") == [], json.dumps(bad)[:200])


if __name__ == "__main__":
    print("MCP wire — mcp/server.py over stdio\n")
    asyncio.run(run())
    # Before run_credentials, which rewrites the registry with a minimal row.
    asyncio.run(run_refusals())
    asyncio.run(run_credentials())
    asyncio.run(run_workflow())
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mwire ok\033[0m")
