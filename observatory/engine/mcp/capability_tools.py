#!/usr/bin/env python3
"""The MCP half of fabric-interop/0.1: capability tools with their published schemas.

`MCPServer` derives a tool's schemas from the Python signature, which is right for
the `observatory_*` tools and wrong for a capability: contract C3.1 (FAC-SEM-017)
requires the served `inputSchema` and `outputSchema` to BE the manifest's
published schemas, and a derived schema drifts from them the first time either
side changes. So `InteropServer` lists every capability tool from
`interop.tool_definitions()` — the bundled schema files themselves — and answers
calls to them here, before the SDK's own tool manager sees them:

1. arguments are validated against the published input schema; a violation is an
   `isError` answer naming the path, never a crash;
2. the request's `_meta.traceparent` becomes the span the work runs in, and the
   result's `_meta` carries that span back (C3.4);
3. the handler runs off the event loop;
4. the answer is validated against the published output schema before it leaves.
   An answer the schema does not describe — a refusal the v0.2.0 record schema
   has no word for, say — goes out as `isError` with its JSON as text, so a client
   that validates structured content never receives something it must reject.

A job capability returns the handle instead, and `fabric.job.get` /
`fabric.job.cancel` are served beside it (C3.2); their answers carry the JOB's
trace, the one of the request that started it.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Any, Callable

import anyio
import jsonschema
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from pydantic import ValidationError
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

import code_freshness
import configuration
import credential_shape
import interop
import jobs

#: The version file this server process was started from (lifecycle LC-10). A
#: call after an update replaced it answers `stale-server` instead of running
#: previous-release code against the new release's workspace.
FRESHNESS = code_freshness.Freshness(configuration.SOURCE / "configuration.py", configuration.VERSION)


def stale_answer() -> CallToolResult | None:
    stale = FRESHNESS.check()
    if stale is None:
        return None
    return _error("stale-server",
                  f"{stale['reason']}; this server still runs {stale['running']}. Restart the "
                  f"session (or reconnect this MCP server) so it starts from the installed code.")


@dataclass
class Answer:
    """A handler's answer: the structured output and any sentences for the text channel."""

    output: dict
    notes: list[str] = field(default_factory=list)


Handler = Callable[[dict, interop.Span], "Answer | dict"]

#: `fabric.job.get` / `fabric.job.cancel` arguments and answer, inline and
#: self-contained — the shapes of the contract's `interop-job-request.schema.json`
#: and `interop-job.schema.json` (tests validate real answers against those files).
JOB_ID = {"type": "string", "minLength": 1, "maxLength": 128, "pattern": "^[A-Za-z0-9._:-]+$"}
JOB_INPUT = {"type": "object", "additionalProperties": False, "required": ["id"],
             "properties": {"id": JOB_ID, "inputResponses": {"type": "object"}}}
JOB_OUTPUT = {
    "type": "object", "additionalProperties": False, "required": ["job"],
    "properties": {"job": {
        "type": "object", "additionalProperties": False, "required": ["id", "status", "updatedAt"],
        "properties": {"id": JOB_ID, "status": {"enum": list(jobs.STATES)},
                       "statusMessage": {"type": "string", "maxLength": 500},
                       "updatedAt": {"type": "string", "format": "date-time"},
                       "pollIntervalMs": {"type": "integer", "minimum": 100, "maximum": 3600000},
                       "inputRequests": {"type": "object"},
                       "result": {"type": "object", "required": list(interop.ENVELOPE_REQUIRED)},
                       "error": {"type": "object", "required": ["code", "message"],
                                 "properties": {"code": {"type": ["integer", "string"]},
                                                "message": {"type": "string", "minLength": 1},
                                                "data": True}}}}}}


def _text(value: Any) -> TextContent:
    return TextContent(type="text", text=json.dumps(value, ensure_ascii=False))


def _compact(result: Any) -> Any:
    """The SDK's text copy of a structured answer, without its indentation.

    The SDK serialises a returned dict with `indent=2` into the text channel and
    sends the same value again as `structuredContent`; the indentation alone made
    a survey a third larger (253,753 against 189,379 characters, measured). Hosts
    that read the text get the same JSON, compact; the structured copy is untouched."""
    if not isinstance(result, CallToolResult) or result.structured_content is None:
        return result
    out = []
    for block in result.content:
        if isinstance(block, TextContent):
            try:
                block = TextContent(type="text", text=json.dumps(
                    json.loads(block.text), ensure_ascii=False, separators=(",", ":")))
            except ValueError:
                pass
        out.append(block)
    result.content = out
    return result


def _error(code: str, detail: str, span: interop.Span | None = None) -> CallToolResult:
    return CallToolResult(content=[_text({"error": code, "detail": detail})], is_error=True,
                          _meta=span.meta() if span else None)


#: Pydantic error types whose message is built from the caller's own input (a
#: custom validator's sentence) rather than from the rule; only the rule is named then.
_VALUE_BEARING = ("value_error", "assertion_error")


def _invalid_arguments(exc: ValidationError) -> str:
    """A rejected argument as `field: fails `rule` — expectation`, never the value.

    The SDK's own text is pydantic's: a docs URL and `input_value=…`, which echoes
    whatever the caller put in the field — a pasted key included — into the
    caller's transcript. Pydantic's `msg` for a type or range rule states only the
    expectation ("Input should be a valid integer"), so it is kept; a message a
    custom validator wrote may quote the input, so for those only the rule is named."""
    parts = []
    for err in exc.errors(include_url=False, include_input=False, include_context=False)[:3]:
        where = ".".join(str(p) for p in err.get("loc") or ()) or "(root)"
        rule = str(err.get("type") or "invalid")
        msg = "" if rule.startswith(_VALUE_BEARING) else str(err.get("msg") or "")
        parts.append(f"{where}: fails `{rule}`" + (f" — {msg}" if msg else ""))
    return "; ".join(parts) or "(root): fails validation"


def _first_error(validator: jsonschema.protocols.Validator, value: Any) -> str | None:
    """The first violation as a path and the rule it broke — never the offending value."""
    errors = sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path))
    if not errors:
        return None
    e = errors[0]
    where = "/".join(str(p) for p in e.absolute_path) or "(root)"
    return f"{where}: fails `{e.validator}`"


def _accepted_names(tool: Any) -> set[str]:
    """Every argument name a tool takes: its fields and their declared aliases."""
    names: set[str] = set()
    for field_name, info in tool.fn_metadata.arg_model.model_fields.items():
        names.add(field_name)
        alias = info.validation_alias
        for choice in getattr(alias, "choices", None) or ([alias] if isinstance(alias, str) else []):
            if isinstance(choice, str):
                names.add(choice)
        if isinstance(info.alias, str):
            names.add(info.alias)
    return names


def _name_unknown_arguments(result: Any, tool: Any, arguments: dict) -> Any:
    """An argument the tool does not take is NAMED in `degraded`, never applied.

    The SDK drops unknown arguments silently, so `{"projectid": …}` (a typo)
    answered for the whole estate with an empty `degraded`, which asserts the
    question was understood. Refusing would break callers that send an
    optional field a later version added, which the compatibility policy keeps
    working, so the answer is kept and qualified. The NAME is quoted only when
    it is a plain identifier; the value never is."""
    unknown = sorted(k for k in arguments if k not in _accepted_names(tool))
    if not unknown or not isinstance(result, CallToolResult):
        return result
    structured = result.structured_content
    if not isinstance(structured, dict):
        return result
    structured = dict(structured)
    structured["degraded"] = list(structured.get("degraded") or []) + [{
        "source": "arguments", "code": "unknown-argument",
        "reason": f"ignored argument(s) this tool does not take: "
                  f"{', '.join(credential_shape.echo(k) for k in unknown)} — "
                  f"the answer is as if they were not sent"}]
    result.structured_content = structured
    result.content = [_text(structured), *[b for b in result.content[1:]]]
    return result


class InteropServer(MCPServer):
    """An MCPServer that also serves the manifest's capabilities as their own tools."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._definitions = {t["name"]: t for t in interop.tool_definitions()}
        self._caps = {c["name"]: c for c in interop.mcp_capabilities()}
        self._handlers: dict[str, Handler] = {}
        self._validators: dict[str, tuple[Any, Any]] = {
            name: (jsonschema.Draft202012Validator(d["inputSchema"]),
                   jsonschema.Draft202012Validator(d["outputSchema"]) if "outputSchema" in d else None)
            for name, d in self._definitions.items()}
        self._job_tools = any(interop.is_job(c) for c in self._caps.values())

    def capability(self, name: str) -> Callable[[Handler], Handler]:
        """Register the handler of a manifest capability (refused for any other name)."""
        if name not in self._definitions:
            raise KeyError(f"{name} is not an mcp capability of fabric-agent.json")

        def register(fn: Handler) -> Handler:
            self._handlers[name] = fn
            return fn
        return register

    def missing_handlers(self) -> list[str]:
        """Capabilities the manifest declares that nothing here answers. Must be empty."""
        return sorted(n for n, c in self._caps.items() if n not in self._handlers and not interop.is_job(c))

    # ── listing ──────────────────────────────────────────────────────────────
    def interop_tools(self) -> list[Tool]:
        tools = []
        for d in self._definitions.values():
            tools.append(Tool(name=d["name"], title=d["name"], description=d["description"],
                              input_schema=d["inputSchema"], output_schema=d.get("outputSchema"),
                              annotations=ToolAnnotations(**{
                                  {"readOnlyHint": "read_only_hint", "destructiveHint": "destructive_hint",
                                   "idempotentHint": "idempotent_hint"}[k]: v
                                  for k, v in d["annotations"].items()}),
                              _meta=d["meta"]))
        if self._job_tools:
            for name, verb in ((interop.JOB_GET, "Read"), (interop.JOB_CANCEL, "Cancel")):
                tools.append(Tool(
                    name=name, title=name,
                    description=(f"{verb} a job a capability of this agent started "
                                 f"(fabric-interop/0.1). An id this agent never issued answers "
                                 f"isError with `unknown-job`."),
                    input_schema=JOB_INPUT, output_schema=JOB_OUTPUT,
                    annotations=ToolAnnotations(read_only_hint=name == interop.JOB_GET,
                                                destructive_hint=False if name == interop.JOB_CANCEL else None,
                                                idempotent_hint=True),
                    _meta={interop.INTEROP_KEY: {"protocol": interop.PROTOCOL}}))
        return tools

    async def list_tools(self) -> list[Tool]:
        own = {t.name for t in self.interop_tools()}
        base = [t for t in await super().list_tools() if t.name not in own]
        return base + self.interop_tools()

    # ── calling ──────────────────────────────────────────────────────────────
    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        stale = stale_answer()
        if stale is not None:
            return stale
        is_job_tool = self._job_tools and name in (interop.JOB_GET, interop.JOB_CANCEL)
        is_assistant = name == "observatory_assistant_ask"
        if name not in self._definitions and not is_job_tool and not is_assistant:
            return await self._sdk_call(name, arguments, context)
        meta = None
        try:
            request = context.request_context if context is not None else None
            meta = getattr(request, "meta", None)
        except ValueError:
            meta = None
        span = interop.span_for(meta)
        if is_assistant:
            from agent import assistant
            try:
                out = await anyio.to_thread.run_sync(lambda: assistant.ask(arguments or {}, span.record()))
            except assistant.AssistantError as exc:
                return _error(str(exc), str(exc), span)
            except OSError as exc:
                code = "disk-full" if exc.errno == 28 else "workspace-write-failed"
                return _error(code, "Could not persist the request", span)
            except configuration.ConfigurationError:
                return _error("backend-configuration", "The workspace configuration is invalid", span)
            record = jobs.get(out["job"]["id"])
            job_span = interop.Span.from_record(record["trace"]) if record else span
            return CallToolResult(content=[_text(out)], structured_content=out, _meta=job_span.meta())
        if is_job_tool:
            return await anyio.to_thread.run_sync(self._job_call, name, arguments or {}, span)
        return await self._capability_call(name, arguments or {}, span)

    async def _sdk_call(self, name: str, arguments: dict[str, Any], context: Any) -> Any:
        """An SDK-described tool, with its refusals typed like the capability tools'.

        A malformed argument and an unknown tool were the SDK's raw text; now they
        are `invalid-input` and `unknown-tool`. Anything else the SDK raises keeps its
        own path: a crash still reaches the client as the SDK's bare "Error
        executing tool <name>" and is logged there."""
        tool = self._tool_manager.get_tool(name)
        if tool is None:
            return _error("unknown-tool", f"no tool named {credential_shape.echo(name)} is served "
                                          f"here; list the tools to see them")
        try:
            result = await super().call_tool(name, arguments, context)
            return _compact(_name_unknown_arguments(result, tool, arguments or {}))
        except ToolError as exc:
            if isinstance(exc, UnexpectedToolError) or not isinstance(exc.__cause__, ValidationError):
                raise
            return _error("invalid-input", _invalid_arguments(exc.__cause__))

    async def _capability_call(self, name: str, arguments: dict, span: interop.Span) -> CallToolResult:
        in_v, out_v = self._validators[name]
        bad = _first_error(in_v, arguments)
        if bad:
            return _error("invalid-input", bad, span)
        cap = self._caps[name]
        if interop.is_job(cap):
            doc, joined = await anyio.to_thread.run_sync(lambda: jobs.start(name, span.record()))
            handle = {"job": {"id": doc["id"], "status": doc["status"]}}
            if doc["status"] != "working":
                # The runner could not start: the handle would lie about a job that
                # is already over, so the answer is the job's own error.
                return _error((doc.get("error") or {}).get("code") or "job-failed",
                              (doc.get("error") or {}).get("message") or "the job did not start", span)
            bad = _first_error(out_v, handle) if out_v is not None else None
            if bad:
                return _error("output-schema-violation", bad, span)
            notes = (["a job of this capability was already running; this is its handle"]
                     if joined else [])
            job_span = interop.Span.from_record(doc["trace"]) if doc.get("trace") else span
            return CallToolResult(content=[_text(handle), *[TextContent(type="text", text=n) for n in notes]],
                                  structured_content=handle, _meta=job_span.meta())
        handler = self._handlers.get(name)
        if handler is None:
            return _error("not-served", f"{name} is declared but has no handler", span)
        answer = await anyio.to_thread.run_sync(handler, arguments, span)
        if not isinstance(answer, Answer):
            answer = Answer(answer)
        bad = _first_error(out_v, answer.output) if out_v is not None else None
        if bad:
            what = ("a refusal the published output schema does not describe"
                    if isinstance(answer.output, dict) and "error" in answer.output
                    else f"an answer that fails the published output schema at {bad}")
            return CallToolResult(content=[_text(answer.output), TextContent(type="text", text=what)],
                                  is_error=True, _meta=span.meta())
        return CallToolResult(content=[_text(answer.output),
                                       *[TextContent(type="text", text=n) for n in answer.notes]],
                              structured_content=answer.output, _meta=span.meta())

    def _job_call(self, name: str, arguments: dict, span: interop.Span) -> CallToolResult:
        bad = _first_error(jsonschema.Draft202012Validator(JOB_INPUT), arguments)
        if bad:
            return _error("invalid-input", bad, span)
        job_id = arguments["id"]
        doc = jobs.get(job_id) if name == interop.JOB_GET else jobs.cancel(job_id)
        if doc is None:
            return _error("unknown-job", "no job with this id was issued by this agent", span)
        job_span = interop.Span.from_record(doc["trace"]) if isinstance(doc.get("trace"), dict) else span
        out = {"job": jobs.view(doc)}
        return CallToolResult(content=[_text(out)], structured_content=out, _meta=job_span.meta())
