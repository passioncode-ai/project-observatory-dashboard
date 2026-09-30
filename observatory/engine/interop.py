#!/usr/bin/env python3
"""`fabric-interop/0.1` as the observatory speaks it: capabilities as tools, trace, envelope.

The Fabric agent-registry contracts (locked 2026-09-29, section C3) say how one
agent calls another through Fabric's hub. For a provider the rules are few, and
each has its home here:

- **C3.1 Capabilities.** Every manifest capability whose profile is `mcp` is served
  as an MCP tool of the SAME name, whose `inputSchema` and `outputSchema` are the
  capability's own published schemas — byte for byte the bundled files, never a
  schema re-derived from a function signature (FAC-SEM-017). `tool_definitions`
  builds them from the manifest, so a capability cannot exist without its tool nor
  a tool drift from its capability. Annotations derive from the declared effect.
- **C3.2 Jobs.** A capability that declares `"job": true` in its interop block
  answers with a job handle; `jobs.py` keeps the jobs and `envelope` shapes a
  finished one's result.
- **C3.4 Trace.** Every request may carry `_meta.traceparent` (W3C Trace Context).
  The work it starts is a CHILD span — same trace id, a new span id, the caller's
  span as parent — and the result carries that child's `traceparent` back. A
  request without a valid one starts a new trace and says so (`parentSpanId`
  null), rather than inventing a parent.

This module holds no MCP SDK import, so the job runner and the tests use it
without starting a server. `mcp/interop.py` is the half that serves it.
"""
from __future__ import annotations
import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "fabric-agent.json"
#: The extension key, spelled as contract C3 locks it — the same host and path
#: shape as the contract's `service/0.1` key.
INTEROP_KEY = "https://fabric.passioncode.ai/agent-contract/extensions/interop/0.1"
PROTOCOL = "fabric-interop/0.1"
#: The tools every job-capable agent serves beside its capabilities (C3.2).
JOB_GET, JOB_CANCEL = "fabric.job.get", "fabric.job.cancel"

# ─────────────────────────── C3.4 trace context ────────────────────────────────

#: W3C Trace Context Level 1, `traceparent`: version-traceid-parentid-flags, lower
#: case hex. Version ff is invalid; a version above 00 may append fields, which a
#: version-00 parser ignores; all-zero ids are invalid.
_TRACEPARENT = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})(-.*)?$")
#: `tracestate` is passed on untouched when it looks like one: at most 512
#: characters of list members. Anything else is dropped rather than propagated.
_TRACESTATE = re.compile(r"^[\x20-\x7e]{1,512}$")


def parse_traceparent(value: Any) -> tuple[str, str, str] | None:
    """(trace id, parent span id, flags), or None when `value` is not a valid traceparent."""
    if not isinstance(value, str):
        return None
    m = _TRACEPARENT.match(value)
    if not m:
        return None
    version, trace_id, parent_id, flags, rest = m.groups()
    if version == "ff" or (version == "00" and rest):
        return None
    if trace_id == "0" * 32 or parent_id == "0" * 16:
        return None
    return trace_id, parent_id, flags


def _new_id(nbytes: int) -> str:
    while True:
        value = secrets.token_hex(nbytes)
        if value != "0" * (2 * nbytes):
            return value


@dataclass(frozen=True)
class Span:
    """One span of this agent's work. `parent_span_id` None means a new trace."""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    flags: str = "01"
    tracestate: str | None = None

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.flags}"

    def child(self) -> "Span":
        """The span for a call this work makes: same trace, this span as parent (C3.4 b)."""
        return Span(self.trace_id, _new_id(8), self.span_id, self.flags, self.tracestate)

    def meta(self) -> dict[str, str]:
        """The `_meta` keys a result carries back."""
        out = {"traceparent": self.traceparent}
        if self.tracestate:
            out["tracestate"] = self.tracestate
        return out

    def env(self) -> dict[str, str]:
        """The environment-variable carrier for a child process (TRACEPARENT/TRACESTATE)."""
        out = {"TRACEPARENT": self.traceparent}
        if self.tracestate:
            out["TRACESTATE"] = self.tracestate
        return out

    def record(self) -> dict[str, Any]:
        return {"traceId": self.trace_id, "spanId": self.span_id,
                "parentSpanId": self.parent_span_id, "flags": self.flags,
                "tracestate": self.tracestate}

    @classmethod
    def from_record(cls, doc: Mapping[str, Any]) -> "Span":
        return cls(str(doc["traceId"]), str(doc["spanId"]), doc.get("parentSpanId"),
                   str(doc.get("flags") or "01"), doc.get("tracestate"))


def span_for(meta: Mapping[str, Any] | None) -> Span:
    """The span a request's work runs in: a child of `_meta.traceparent`, or a new trace."""
    meta = meta or {}
    parsed = parse_traceparent(meta.get("traceparent"))
    state = meta.get("tracestate")
    state = state if isinstance(state, str) and _TRACESTATE.match(state) else None
    if parsed is None:
        return Span(_new_id(16), _new_id(8), None, "01", None)
    trace_id, parent_id, flags = parsed
    return Span(trace_id, _new_id(8), parent_id, flags, state)


# ─────────────────────────── C3.1 capabilities as tools ─────────────────────────

#: Effects the contract counts as destructive (C3.1).
DESTRUCTIVE = {"delete", "merge", "deploy", "change-policy"}


def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def interop_block(capability: Mapping[str, Any]) -> dict:
    block = (capability.get("extensions") or {}).get(INTEROP_KEY)
    return block if isinstance(block, dict) else {}


def is_job(capability: Mapping[str, Any]) -> bool:
    return interop_block(capability).get("job") is True


def annotations(capability: Mapping[str, Any]) -> dict[str, bool]:
    """C3.1: `effect: none` → readOnlyHint; delete/merge/deploy/change-policy →
    destructiveHint; `idempotency: required` → idempotentHint.

    A write that is not destructive says `destructiveHint: false` explicitly: MCP
    defaults the hint to TRUE for any tool that is not read-only, so leaving it
    out would announce a proposal-only `draft` write as destructive."""
    effect = capability.get("effect")
    out: dict[str, bool] = {}
    if effect == "none":
        out["readOnlyHint"] = True
    else:
        out["readOnlyHint"] = False
        out["destructiveHint"] = effect in DESTRUCTIVE
    if capability.get("idempotency") == "required":
        out["idempotentHint"] = True
    return out


def bundled(uri: str) -> dict:
    """The bundled bytes a pinned schema or fixture URI names (never fetched)."""
    import sys
    sys.path.insert(0, str(ROOT / "tools"))
    from publish_contract import resolve
    name = resolve(uri)
    if name is None:
        raise KeyError(f"no bundled release publishes {uri}")
    return json.loads((ROOT / "fabric" / name).read_text(encoding="utf-8"))


def mcp_capabilities(doc: Mapping[str, Any] | None = None) -> list[dict]:
    doc = doc or manifest()
    return [c for c in doc.get("capabilities", []) if (c.get("profile") or {}).get("kind") == "mcp"]


def object_rooted(schema: dict) -> dict:
    """The schema with `"type": "object"` at its root, added when it is missing.

    MCP clients require a tool's `outputSchema` to be an object schema at the root:
    Claude Code 2.1.285 refuses the whole `tools/list` answer ("tools fetch failed —
    Handler returned an invalid result") when one tool's schema is rooted in `oneOf`,
    and so every tool of the server becomes unreachable (measured 2026-09-30 on 0.9.0).
    Two served schemas are such unions: `project.record`'s published v0.2.0 output and
    the DEC-0017 job-tool union. Every branch of both is an object, so the added
    keyword changes no value they accept; it is the one difference from the published
    bytes, stated in docs/design/FABRIC-INTEROP.md and put to the contract."""
    return schema if schema.get("type") == "object" else {"type": "object", **schema}


def tool_definitions(doc: Mapping[str, Any] | None = None) -> list[dict]:
    """One MCP tool per `mcp` capability, with the capability's own schemas.

    A job capability's tool answers with a job handle (or, had it finished at once,
    the result envelope), so its `outputSchema` is the contract's union around the
    capability's output schema (DEC-0017, OQ-0006): what it returns always conforms."""
    tools = []
    for cap in mcp_capabilities(doc):
        output = bundled(cap["outputSchema"])
        tools.append({
            "name": cap["name"],
            "description": cap.get("description") or cap["name"],
            "inputSchema": bundled(cap["inputSchema"]),
            "outputSchema": object_rooted(job_tool_output_schema(output) if is_job(cap) else output),
            "annotations": annotations(cap),
            "meta": {INTEROP_KEY: {"capability": cap["id"], "effect": cap["effect"],
                                   **({"job": True} if is_job(cap) else {})}},
        })
    return tools


# ─────────────────────────── C3.2 the result envelope ───────────────────────────

CONTRACT_VERSION = "0.1.0"
#: The vendored contract schemas (fabric-agent-contract, DEC-0017), for validation.
CONTRACT_SCHEMAS = ROOT / "fabric" / "interop-schemas"
#: What the contract's result envelope requires of a job result (DEC-0017, OQ-0002):
#: the full `result.schema.json` plus `output` and `usage`.
ENVELOPE_REQUIRED = ["id", "contractVersion", "outcome", "done", "proof", "scope", "notVerified",
                     "artifacts", "createdAt", "producer", "output", "usage"]
#: The job handle, inline — the shape of the contract's `interop-job-handle.schema.json`.
JOB_HANDLE_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["job"], "additionalProperties": False,
    "properties": {"job": {"type": "object", "required": ["id", "status"], "additionalProperties": False,
                           "properties": {"id": {"type": "string", "minLength": 1, "maxLength": 128,
                                                 "pattern": "^[A-Za-z0-9._:-]+$"},
                                          "status": {"const": "working"}}}}}


def job_tool_output_schema(output: Any) -> dict:
    """DEC-0017 (OQ-0006): a job-backed tool's `outputSchema` is the self-contained union
    `oneOf[result envelope, job handle]`, so its structured content always conforms. The
    exact shape of the contract's `jobToolOutputSchema(output)`; the manifest's capability
    keeps the pure output schema."""
    return {"oneOf": [{"type": "object", "required": list(ENVELOPE_REQUIRED), "properties": {"output": output}},
                      JOB_HANDLE_SCHEMA]}


def contract_validator(name: str):
    """A validator for one vendored contract schema, its `$ref`s resolved locally (never fetched)."""
    import jsonschema
    from referencing import Registry, Resource
    docs = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in CONTRACT_SCHEMAS.glob("*.schema.json")}
    registry = Registry().with_resources([(d["$id"], Resource.from_contents(d)) for d in docs.values()])
    return jsonschema.Draft202012Validator(docs[name], registry=registry,
                                           format_checker=jsonschema.FormatChecker())


def producer(doc: Mapping[str, Any] | None = None) -> dict:
    """The DEC-0011 `revisionRef` of this provider: id, revision, content hash."""
    doc = doc or manifest()
    p = doc["provider"]
    return {"id": p["id"], "revision": p["revision"], "contentHash": p["contentHash"]}


def envelope(*, job_id: str, capability: str, span: Span, output: Any, done: list[dict], proof: list[dict],
             not_verified: list[dict], write_scopes: list[str], wall_ms: int, created_at: str) -> dict:
    """The full result envelope a job result is (C3.2; DEC-0017 OQ-0002, OQ-0003).

    `trace` is the job's span and is authoritative for the stored result: every
    answer about the job carries the same traceparent in `_meta` (FAC-SEM-022).

    `scope` names the observatory's own identifiers. The contract requires the
    project, run, node and binding, but those are the HOST's, and a tool call does
    not hand them to the provider; so the envelope says what the observatory knows
    — the subject it observed (this machine), this job as the run, the capability
    as the node, this provider revision as the binding — each under a URN a host
    cannot mistake for one of its own. `outcome` is `partial` when something is not
    verified (a degraded source), `succeeded` otherwise. No model runs in these
    jobs, so every token count is zero, stated rather than omitted."""
    me = producer()
    body: dict[str, Any] = {
        "id": f"urn:observatory:result:{job_id}",
        "contractVersion": CONTRACT_VERSION,
        "outcome": "partial" if not_verified else "succeeded",
        "done": done, "proof": proof,
        "scope": {"project": "urn:observatory:subject:machine", "run": f"urn:observatory:job:{job_id}",
                  "node": f"urn:observatory:capability:{capability}", "binding": me,
                  "writeScopes": write_scopes},
        "notVerified": not_verified, "artifacts": [], "createdAt": created_at, "producer": me,
        "output": output,
        "usage": {"inputTokens": 0, "outputTokens": 0, "wallMs": int(wall_ms)},
        "trace": {"traceparent": span.traceparent, **({"tracestate": span.tracestate} if span.tracestate else {})},
    }
    return body
