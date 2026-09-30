# Vendored from passioncode-ai/fabric-agent-adapter f31c2b2792f7 (fabric-agent-adapter 0.5.0),
# plugins/fabric-agent-adapter/skills/building-fabric-services/scripts/fabric_interop.py,
# upstream sha256 a9914847ed54bedd56a988320aad6cebe91d1a7477e362cca4540193b69a3537 (the bytes below this header).
# Do not edit here: update the kit upstream and copy it again (tests/test_fabric_service.py checks the digest).
#!/usr/bin/env python3
"""Reference kit for the fabric-interop/0.1 extension: how agents are called.

Standard library only, Python 3.9+. It sits beside fabric_service.py and uses it for
atomic, private writes. Every function implements one rule of the extension and names
it, so a service that calls these functions inherits the rule:

- C3.1 a capability is served as the MCP tool of its own name, annotations from its effect;
- C3.2 long work is a job: a stable id, fabric.job.get / fabric.job.cancel, a result envelope;
- C3.3 a question for a person is an elicitation (form mode, never a secret; URL mode for those);
- C3.4 every answer is a child span of the caller's traceparent, and so is every event.

Normative source: fabric-agent-contract docs/specification/interop.md (DEC-0016).
"""

# #region interop-kit — docs: plugins/fabric-agent-adapter/skills/building-fabric-services/references/interop.md#the-kit

from __future__ import annotations

import json
from pathlib import Path
import re
import secrets
import sys
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fabric_service as _fs  # noqa: E402

PROTOCOL = "fabric-interop/0.1"
EXTENSION_KEY = "https://fabric.passioncode.ai/agent-contract/extensions/interop/0.1"
MCP_REVISION = "2026-07-28"
JOB_STATES = ("working", "input_required", "completed", "failed", "cancelled")
TERMINAL = ("completed", "failed", "cancelled")

_TRACEPARENT = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_JOB_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SECRET_WORDS = re.compile(r"pass(word|phrase)|secret|api[\s_-]?key|access[\s_-]?key|private[\s_-]?key|"
                           r"(access|auth|bearer|refresh|session)[\s_-]?token|^token$|credential", re.IGNORECASE)


class InteropError(_fs.ServiceError):
    """A rule of fabric-interop/0.1 was violated; the message is one readable sentence."""


class UnknownJob(InteropError):
    def __init__(self, job_id: str):
        self.job_id = job_id
        super().__init__("unknown-job: no job %s here." % job_id)


# --- C3.4 trace context --------------------------------------------------------

def parse_traceparent(value: Any) -> Optional[Dict[str, str]]:
    """W3C Trace Context Level 1, lowercase hex; version ff and all-zero ids are invalid."""
    match = _TRACEPARENT.match(value) if isinstance(value, str) else None
    if not match:
        return None
    version, trace_id, span_id, flags = match.groups()
    if version == "ff" or set(trace_id) == {"0"} or set(span_id) == {"0"}:
        return None
    return {"version": version, "trace_id": trace_id, "span_id": span_id, "flags": flags}


def _span_id() -> str:
    while True:
        value = secrets.token_hex(8)
        if set(value) != {"0"}:
            return value


def child_traceparent(parent: Optional[str]) -> str:
    """A new span under `parent`; a missing or broken parent starts a new trace (W3C: restart)."""
    parsed = parse_traceparent(parent)
    trace_id = parsed["trace_id"] if parsed else secrets.token_hex(16)
    flags = parsed["flags"] if parsed else "01"
    return "00-%s-%s-%s" % (trace_id, _span_id(), flags)


def trace_ids(traceparent: Optional[str]) -> Dict[str, str]:
    """The `traceId`/`spanId` pair an event about this span carries (C3.4 c), or {}."""
    parsed = parse_traceparent(traceparent)
    return {"trace_id": parsed["trace_id"], "span_id": parsed["span_id"]} if parsed else {}


# --- C3.1 capabilities as tools --------------------------------------------------

def expected_annotations(effect: str, idempotency: str) -> Dict[str, bool]:
    """effect none -> readOnlyHint; delete|merge|deploy|change-policy -> destructiveHint; idempotency required -> idempotentHint."""
    hints: Dict[str, bool] = {}
    if effect == "none":
        hints["readOnlyHint"] = True
    if effect in ("delete", "merge", "deploy", "change-policy"):
        hints["destructiveHint"] = True
    if idempotency == "required":
        hints["idempotentHint"] = True
    return hints


def tool_for_capability(capability: Dict[str, Any], input_schema: Dict[str, Any], output_schema: Dict[str, Any],
                        title: Optional[str] = None) -> Dict[str, Any]:
    """The MCP tool for one manifest capability: its name, its two schemas as they are, derived annotations.

    A job-capable tool still serves the capability's outputSchema, though its
    structuredContent is the job handle (contract OQ-0006)."""
    tool: Dict[str, Any] = {
        "name": capability["name"],
        "inputSchema": input_schema,
        "outputSchema": output_schema,
        "annotations": expected_annotations(capability.get("effect", ""), capability.get("idempotency", "")),
    }
    if capability.get("description"):
        tool["description"] = capability["description"]
    if title:
        tool["title"] = title
    return tool


def tool_result(structured: Any, *, is_error: bool = False, traceparent: Optional[str] = None) -> Dict[str, Any]:
    """A tools/call result: structuredContent plus its JSON as text, for clients that read only text."""
    result: Dict[str, Any] = {
        "resultType": "complete",
        "content": [{"type": "text", "text": json.dumps(structured, ensure_ascii=False)}],
        "structuredContent": structured,
        "isError": is_error,
    }
    if traceparent:
        result["_meta"] = {"traceparent": traceparent}
    return result


def unknown_job_result(job_id: str, traceparent: Optional[str] = None) -> Dict[str, Any]:
    """C3.2: fabric.job.get for an unknown id answers isError with unknown-job, never a fresh job."""
    return tool_result({"error": {"code": "unknown-job", "message": "No job %s here." % job_id}},
                       is_error=True, traceparent=traceparent)


# --- C3.2 the result envelope ------------------------------------------------------

def _usage(usage: Any) -> Dict[str, Any]:
    if not isinstance(usage, dict):
        raise InteropError("usage must be an object.")
    for key in ("inputTokens", "outputTokens", "wallMs"):
        if key not in usage:
            raise InteropError("usage.%s is required." % key)
    for key, value in usage.items():
        if key not in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "costUsd", "wallMs"):
            raise InteropError("usage.%s is not a usage field." % key)
        numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
        if not numeric or value < 0 or (key != "costUsd" and not isinstance(value, int)):
            raise InteropError("usage.%s must be a non-negative %s." % (key, "number" if key == "costUsd" else "integer"))
    return dict(usage)


def result_envelope(*, done: List[Dict[str, Any]], proof: List[Dict[str, Any]], scope: Dict[str, Any],
                    not_verified: List[Dict[str, Any]], output: Any, usage: Dict[str, Any]) -> Dict[str, Any]:
    """DONE / PROOF / SCOPE / NOT VERIFIED (DEC-0011) plus the output and usage; the four collections are always present."""
    for label, value in (("done", done), ("proof", proof), ("notVerified", not_verified)):
        if not isinstance(value, list):
            raise InteropError("%s must be a list, even when empty." % label)
    if not isinstance(scope, dict):
        raise InteropError("scope must be an object.")
    return {"done": list(done), "proof": list(proof), "scope": dict(scope), "notVerified": list(not_verified),
            "output": output, "usage": _usage(usage)}


# --- C3.3 awaiting a choice ------------------------------------------------------------

def form_request(message: str, properties: Dict[str, Dict[str, Any]], required: Optional[List[str]] = None) -> Dict[str, Any]:
    """An elicitation in form mode: a flat object of primitive fields, and never a secret (FAC-SEM-018)."""
    for field, schema in properties.items():
        words = " ".join(str(schema.get(key, "")) for key in ("title", "description", "format"))
        if _SECRET_WORDS.search(field) or _SECRET_WORDS.search(words):
            raise InteropError("Form mode cannot ask for %s: a secret goes through url_request." % field)
        if schema.get("type") not in ("string", "number", "integer", "boolean", "array"):
            raise InteropError("Form field %s must be a primitive (string, number, integer, boolean or an enum array)." % field)
    requested: Dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        requested["required"] = list(required)
    return {"method": "elicitation/create", "params": {"mode": "form", "message": message, "requestedSchema": requested}}


def choice_request(message: str, field: str, options: List[Any], title: Optional[str] = None) -> Dict[str, Any]:
    """A titled single-select: oneOf [{const, title}] — what Fabric turns into an interaction point."""
    schema: Dict[str, Any] = {"type": "string", "oneOf": [{"const": value, "title": label} for value, label in options]}
    if title:
        schema["title"] = title
    return form_request(message, {field: schema}, [field])


def url_request(message: str, url: str) -> Dict[str, Any]:
    """URL mode: the person opens `url` out of band; for credentials and payments. Put nothing sensitive in the URL."""
    if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        raise InteropError("A URL-mode request needs an https URL (or this machine's loopback).")
    return {"method": "elicitation/create", "params": {"mode": "url", "message": message, "url": url}}


# --- C3.2 jobs ---------------------------------------------------------------------

class JobStore:
    """Durable jobs, one private JSON file each under `directory`, written atomically.

    A job id survives a restart of the agent, so fabric.job.get after a restart finds
    the same job. The file holds the public `job` object exactly as fabric.job.get
    returns it, and a `private` part (the capability, its input, the trace) that is
    never returned. The Node kit reads and writes the same format.
    """

    def __init__(self, directory: Path):
        self.dir = Path(directory)

    def _path(self, job_id: str) -> Path:
        if not _JOB_ID.match(job_id or ""):
            raise UnknownJob(str(job_id))
        return self.dir / ("%s.json" % job_id)

    def _load(self, job_id: str) -> Dict[str, Any]:
        try:
            return json.loads(self._path(job_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise UnknownJob(job_id) from None
        except ValueError:
            raise InteropError("Job %s is unreadable on disk." % job_id) from None

    def _save(self, record: Dict[str, Any]) -> Dict[str, Any]:
        record["job"]["updatedAt"] = _fs.now_iso()
        _fs.ensure_private_dir(self.dir)
        _fs.atomic_write(self._path(record["job"]["id"]), (json.dumps(record, ensure_ascii=False) + "\n").encode(), 0o600)
        return dict(record["job"])

    def create(self, capability: str, arguments: Any, traceparent: Optional[str] = None,
               poll_interval_ms: Optional[int] = None) -> Dict[str, Any]:
        """Start a job; returns the handle a job-capable tool puts in structuredContent."""
        job_id = "job_" + secrets.token_hex(12)
        job: Dict[str, Any] = {"id": job_id, "status": "working"}
        if poll_interval_ms:
            job["pollIntervalMs"] = int(poll_interval_ms)
        self._save({"job": job, "private": {"capability": capability, "input": arguments,
                                            "traceparent": traceparent if parse_traceparent(traceparent) else None}})
        return {"job": {"id": job_id, "status": "working"}}

    def get(self, job_id: str) -> Dict[str, Any]:
        return dict(self._load(job_id)["job"])

    def private(self, job_id: str) -> Dict[str, Any]:
        return dict(self._load(job_id)["private"])

    def traceparent(self, job_id: str) -> Optional[str]:
        return self._load(job_id)["private"].get("traceparent")

    def _transition(self, job_id: str, status: str, **fields: Any) -> Dict[str, Any]:
        record = self._load(job_id)
        job = record["job"]
        if job["status"] in TERMINAL:
            raise InteropError("Job %s is already %s; a terminal job does not change." % (job_id, job["status"]))
        for key in ("inputRequests", "result", "error", "statusMessage"):
            job.pop(key, None)
        job["status"] = status
        job.update({key: value for key, value in fields.items() if value is not None})
        return self._save(record)

    def working(self, job_id: str, message: Optional[str] = None) -> Dict[str, Any]:
        return self._transition(job_id, "working", statusMessage=message)

    def request_input(self, job_id: str, input_requests: Dict[str, Dict[str, Any]], message: Optional[str] = None) -> Dict[str, Any]:
        """input_required with MCP elicitation requests, keyed; build them with choice_request / form_request / url_request."""
        if not input_requests:
            raise InteropError("input_required needs at least one input request.")
        for key, request in input_requests.items():
            if not _KEY.match(key) or request.get("method") != "elicitation/create":
                raise InteropError("Input request %r must be a keyed elicitation/create request." % key)
        return self._transition(job_id, "input_required", inputRequests=input_requests, statusMessage=message)

    def answer(self, job_id: str, input_responses: Dict[str, Any]) -> Dict[str, Any]:
        """Apply inputResponses to an input_required job; unknown keys are ignored (MCP Tasks rule).

        Returns the responses that matched; when any did, the job is working again."""
        job = self.get(job_id)
        if job["status"] != "input_required":
            return {}
        matched = {key: value for key, value in (input_responses or {}).items()
                   if key in job.get("inputRequests", {}) and isinstance(value, dict)
                   and value.get("action") in ("accept", "decline", "cancel")}
        if matched:
            self.working(job_id)
        return matched

    def complete(self, job_id: str, envelope: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(envelope, dict) or not {"done", "proof", "scope", "notVerified", "output", "usage"} <= set(envelope):
            raise InteropError("A completed job carries the full result envelope; build it with result_envelope.")
        return self._transition(job_id, "completed", result=envelope)

    def fail(self, job_id: str, code: Any, message: str) -> Dict[str, Any]:
        return self._transition(job_id, "failed", error={"code": code, "message": message})

    def cancel(self, job_id: str) -> Dict[str, Any]:
        return self._transition(job_id, "cancelled")


# --- a minimal MCP tool server (streamable HTTP, JSON responses) ------------------------

class CallContext:
    """What a tool handler gets: the child span it runs in, and a way to start a job."""

    def __init__(self, server: "McpToolServer", tool: str, arguments: Any, traceparent: str):
        self.server = server
        self.tool = tool
        self.arguments = arguments
        self.traceparent = traceparent

    def start_job(self, poll_interval_ms: Optional[int] = None) -> Dict[str, Any]:
        if self.server.jobs is None:
            raise InteropError("This server has no job store.")
        handle = self.server.jobs.create(self.tool, self.arguments, self.traceparent, poll_interval_ms)
        self.server.job_started(handle["job"]["id"], self)
        return handle


Handler = Callable[[Any, CallContext], Any]
OnInput = Callable[[str, Dict[str, Any], CallContext], None]

JOB_REQUEST_SCHEMA = {"type": "object", "required": ["id"], "additionalProperties": False, "properties": {
    "id": {"type": "string", "minLength": 1, "maxLength": 128},
    "inputResponses": {"type": "object"}}}


class McpToolServer:
    """tools/list and tools/call for an MCP 2026-07-28 server, with fabric.job.get/cancel built in.

    `handle(message)` takes one JSON-RPC message and returns the response (None for a
    notification). Every result carries `_meta.traceparent`, a child span of the
    caller's. It is deliberately small: a service with a full MCP SDK should use that
    and keep only the helpers above.
    """

    def __init__(self, name: str, version: str, jobs: Optional[JobStore] = None,
                 on_job_started: Optional[Callable[[str, CallContext], None]] = None,
                 on_input: Optional[OnInput] = None):
        self.info = {"name": name, "version": version}
        self.jobs = jobs
        self.tools: List[Dict[str, Any]] = []
        self.handlers: Dict[str, Handler] = {}
        self._on_job_started = on_job_started
        self._on_input = on_input

    def add_tool(self, tool: Dict[str, Any], handler: Handler) -> None:
        if tool["name"] in self.handlers:
            raise InteropError("Tool %s is served twice." % tool["name"])
        self.tools.append(tool)
        self.handlers[tool["name"]] = handler

    def job_started(self, job_id: str, ctx: CallContext) -> None:
        if self._on_job_started:
            self._on_job_started(job_id, ctx)

    def _job_tools(self) -> List[Dict[str, Any]]:
        if self.jobs is None:
            return []
        return [
            {"name": "fabric.job.get", "description": "The state of a job this agent started; answer an input request with inputResponses.",
             "inputSchema": JOB_REQUEST_SCHEMA, "annotations": {"idempotentHint": True}},
            {"name": "fabric.job.cancel", "description": "Ask this agent to stop a job it started.",
             "inputSchema": {"type": "object", "required": ["id"], "additionalProperties": False, "properties": {"id": JOB_REQUEST_SCHEMA["properties"]["id"]}},
             "annotations": {"idempotentHint": True}},
        ]

    @staticmethod
    def _error(rid: Any, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

    def handle(self, message: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return self._error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid request.")
        if "id" not in message:
            return None  # a notification gets no response
        rid = message["id"]
        params = message.get("params") or {}
        meta = params.get("_meta") or {}
        span = child_traceparent(meta.get("traceparent"))
        method = message["method"]
        if method == "server/discover":
            return {"jsonrpc": "2.0", "id": rid, "result": {"resultType": "complete", "capabilities": {"tools": {}},
                                                             "serverInfo": self.info, "_meta": {"traceparent": span}}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": rid, "result": {"resultType": "complete", "tools": self.tools + self._job_tools(),
                                                             "_meta": {"traceparent": span}}}
        if method != "tools/call":
            return self._error(rid, -32601, "Method %s is not served here." % method)
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name in ("fabric.job.get", "fabric.job.cancel") and self.jobs is not None:
            return {"jsonrpc": "2.0", "id": rid, "result": self._job_call(name, arguments, span)}
        handler = self.handlers.get(name)
        if handler is None:
            return self._error(rid, -32602, "Unknown tool: %s." % name)
        try:
            structured = handler(arguments, CallContext(self, name, arguments, span))
        except InteropError as exc:
            return {"jsonrpc": "2.0", "id": rid, "result": tool_result({"error": {"code": "tool-error", "message": str(exc)}}, is_error=True, traceparent=span)}
        return {"jsonrpc": "2.0", "id": rid, "result": tool_result(structured, traceparent=span)}

    def _job_call(self, name: str, arguments: Dict[str, Any], span: str) -> Dict[str, Any]:
        job_id = str(arguments.get("id", ""))
        try:
            job_span = self.jobs.traceparent(job_id) or span
            if name == "fabric.job.cancel":
                state = self.jobs.get(job_id)
                if state["status"] not in TERMINAL:
                    state = self.jobs.cancel(job_id)
                return tool_result({"job": state}, traceparent=job_span)
            responses = arguments.get("inputResponses")
            if responses:
                matched = self.jobs.answer(job_id, responses)
                if matched and self._on_input:
                    private = self.jobs.private(job_id)
                    self._on_input(job_id, matched, CallContext(self, private["capability"], private["input"], job_span))
            return tool_result({"job": self.jobs.get(job_id)}, traceparent=job_span)
        except UnknownJob:
            return unknown_job_result(job_id, traceparent=span)


__all__ = [name for name in dir() if not name.startswith("_")]
# #endregion interop-kit
