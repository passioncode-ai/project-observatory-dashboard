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


def tool_definitions(doc: Mapping[str, Any] | None = None) -> list[dict]:
    """One MCP tool per `mcp` capability, with the capability's own schemas.

    A job capability's tool answers with a job handle, which its output schema
    does not describe, so its tool publishes no `outputSchema` and names where the
    RESULT's output is described instead — in the tool's `_meta`, under the
    interop key. A client that validates structured content against an
    `outputSchema` would otherwise reject every job handle."""
    tools = []
    for cap in mcp_capabilities(doc):
        tool: dict[str, Any] = {
            "name": cap["name"],
            "description": cap.get("description") or cap["name"],
            "inputSchema": bundled(cap["inputSchema"]),
            "annotations": annotations(cap),
            "meta": {INTEROP_KEY: {"capability": cap["id"], "effect": cap["effect"],
                                   **({"job": True, "resultOutputSchema": cap["outputSchema"]}
                                      if is_job(cap) else {})}},
        }
        if not is_job(cap):
            tool["outputSchema"] = bundled(cap["outputSchema"])
        tools.append(tool)
    return tools


# ─────────────────────────── C3.2 the result envelope ───────────────────────────

def producer(doc: Mapping[str, Any] | None = None) -> dict:
    """The DEC-0011 `revisionRef` of this provider: id, revision, content hash."""
    doc = doc or manifest()
    p = doc["provider"]
    return {"id": p["id"], "revision": p["revision"], "contentHash": p["contentHash"]}


#: The result envelope as this agent emits it: contract C3.2 with the DEC-0011
#: item shapes of the Fabric Agent Contract's `result.schema.json` (0.1.0) for
#: `done`, `proof` and `notVerified`. `scope` is narrowed to what a callee knows.
#: Kept here until the contract publishes its own interop schemas (AR-1.1).
ENVELOPE_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["done", "proof", "scope", "notVerified", "output", "usage"],
    "properties": {
        "done": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["claimId", "statement"],
            "properties": {"claimId": {"type": "string", "pattern": "^[A-Z][A-Z0-9_-]{1,63}$"},
                           "statement": {"type": "string", "minLength": 1}}}},
        "proof": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["claimIds", "kind", "uri", "producer", "capturedAt", "classification"],
            "properties": {
                "claimIds": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string"}},
                "kind": {"type": "string", "pattern": "^[a-z][a-z0-9./-]+$"},
                "uri": {"type": "string", "minLength": 1},
                "producer": {"type": "object", "additionalProperties": False,
                             "required": ["id", "revision", "contentHash"],
                             "properties": {"id": {"type": "string"}, "revision": {"type": "integer", "minimum": 1},
                                            "contentHash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}}},
                "capturedAt": {"type": "string"},
                "classification": {"enum": ["public", "project-internal", "confidential", "personal",
                                            "credential", "regulated"]}}}},
        "scope": {"type": "object", "additionalProperties": False, "required": ["writeScopes"],
                  "properties": {"writeScopes": {"type": "array", "uniqueItems": True,
                                                 "items": {"type": "string", "minLength": 1}}}},
        "notVerified": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["claim", "reason"],
            "properties": {"claim": {"type": "string", "minLength": 1},
                           "reason": {"type": "string", "minLength": 1}}}},
        "output": {},
        "usage": {"type": "object", "additionalProperties": False,
                  "required": ["inputTokens", "outputTokens", "wallMs"],
                  "properties": {k: {"type": "integer", "minimum": 0} for k in
                                 ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "wallMs")}
                  | {"costUsd": {"type": "number", "minimum": 0}}}}}


def envelope(*, output: Any, done: list[dict], proof: list[dict], not_verified: list[dict],
             write_scopes: list[str], wall_ms: int) -> dict:
    """The result envelope of C3.2.

    `scope` carries only `writeScopes`: the project, run, node and binding of the
    contract's result envelope are the HOST's identifiers, which a tool call does
    not hand the provider — inventing them would be a claim about someone else's
    run. No model runs in the observatory's jobs, so every token count is zero,
    stated rather than omitted."""
    return {"done": done, "proof": proof, "scope": {"writeScopes": write_scopes},
            "notVerified": not_verified, "output": output,
            "usage": {"inputTokens": 0, "outputTokens": 0, "wallMs": int(wall_ms)}}
