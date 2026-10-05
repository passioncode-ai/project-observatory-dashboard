"""memory/0.1 served under its own names (PB-137 N-025; fabric-agent-contract DEC-0023).

The nine `memory.*` capabilities are the contract's names for tools this server already
serves as `observatory_*`. This module adds no behaviour of its own: each capability
validates its input against the contract's schema, calls the SAME function the
`observatory_*` tool runs (so authorization, redaction, receipts and refusals are one
code path), and checks the answer against the contract's output or refusal shape.

- **The schema is the contract's, not the engine's.** `fabric/memory-schemas/` holds
  `memory-capability.schema.json` copied byte for byte from fabric-agent-contract at the
  commit named in its README, with its sha256; `tests/test_memory_wire.py` checks both.
  To change a shape, change the contract and copy it again.
- **Unknown fields are refused, not ignored.** Every input schema is closed; a field the
  contract does not define answers `invalid-input` naming the field's path, never its
  value. A capability name the contract does not list is `unknown-tool`, and the three
  reserved names (`memory.learning.propose`, `memory.explain`, `memory.forget`) are not
  served under these names yet.
- **The `observatory_*` tools stay** for every client that already calls them; the
  compatibility map in `fabric/memory-provider.json` says which is which.
"""
from __future__ import annotations

import copy
import hashlib
import json
import pathlib
from typing import Any, Callable

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "fabric" / "memory-schemas"
SCHEMA_FILE = SCHEMA_DIR / "memory-capability.schema.json"
DECLARATION_FILE = ROOT / "fabric" / "memory-provider.json"
FAMILY = "memory/0.1"

#: Arguments whose contract name differs from the observatory tool's (contract → tool).
RENAMES: dict[str, dict[str, str]] = {"memory.search": {"projectId": "project_id"}}


def _compatibility() -> dict[str, tuple[str, dict[str, str]]]:
    import memory_access
    return {cap: (tool, RENAMES.get(cap, {}))
            for cap, tool in memory_access.MEMORY_CAPABILITIES.items()}


#: capability → (the observatory tool it is, argument renames contract → tool).
COMPATIBILITY = _compatibility()

_DESCRIPTIONS = {
    "memory.checkpoint.write": "Write a workflow's checkpoint after every step, or start a workflow. Keep the leaseId.",
    "memory.checkpoint.latest": "A workflow's latest checkpoint, executor and pending handoff. Never a lease token.",
    "memory.handoff.create": "Offer a workflow to another executor with an immutable handoff pack.",
    "memory.handoff.accept": "Take a workflow: the new leaseId, the constraints in force, the checkpoint and the pack.",
    "memory.handoff.get": "A handoff pack and its status. Never a lease token.",
    "memory.workflow.list": "Workflows, newest first, with where each stands.",
    "memory.record": "Append a note as a proposal; it is never promoted by this call.",
    "memory.search": "Search recorded memory by word forms within your scope; the answer names its receipt.",
    "memory.recall": "Current records, paged; conflicting records come back together.",
}


def schema() -> dict:
    return json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))


def schema_digest() -> str:
    return hashlib.sha256(SCHEMA_FILE.read_bytes()).hexdigest()


COMMON_FILE = SCHEMA_DIR / "common.schema.json"


def _common() -> dict:
    return json.loads(COMMON_FILE.read_text(encoding="utf-8"))


def _resolve(ref: str, defs: dict) -> Any:
    """The definition a `$ref` names: local (`#/$defs/...`) or the contract's common
    schema (`common.schema.json#/$defs/...`), nothing else."""
    if ref.startswith("#/$defs/"):
        target, path = defs, ref[len("#/$defs/"):]
    elif ref.startswith("common.schema.json#/$defs/"):
        target, path = _common()["$defs"], ref[len("common.schema.json#/$defs/"):]
    else:
        raise ValueError(f"memory/0.1 schema refers outside its vendored files: {ref}")
    for part in path.split("/"):
        target = target[part]
    return copy.deepcopy(target)


def _inline(node: Any, defs: dict, depth: int = 0) -> Any:
    """A self-contained schema: every `$ref` replaced by its definition. An MCP client
    does not fetch `$ref`s, so the listed input schema must stand alone."""
    if depth > 40:
        raise ValueError("schema $ref nesting is too deep")
    if isinstance(node, dict):
        if set(node) == {"$ref"} and isinstance(node["$ref"], str):
            return _inline(_resolve(node["$ref"], defs), defs, depth + 1)
        return {k: _inline(v, defs, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline(v, defs, depth + 1) for v in node]
    return node


def _registry():
    """The two vendored files under their contract `$id`s, for the validators."""
    from referencing import Registry, Resource
    resources = []
    for doc in (schema(), _common()):
        resources.append((doc["$id"], Resource.from_contents(doc)))
    return Registry().with_resources(resources)


def definitions() -> dict[str, dict]:
    """Per capability: its listed input schema, and validators for input and answer."""
    import jsonschema
    s = schema()
    defs = s["$defs"]
    registry = _registry()
    base = s["$id"]
    out = {}
    for cap in defs["capability"]["enum"]:
        in_ref = {"$ref": f"{base}#/$defs/input/{cap}"}
        out_ref = {"oneOf": [{"$ref": f"{base}#/$defs/output/{cap}"},
                             {"$ref": f"{base}#/$defs/refusal"}]}
        out[cap] = {"inputSchema": _inline(defs["input"][cap], defs),
                    "description": _DESCRIPTIONS[cap] + f" ({FAMILY}; same code as "
                                                        f"{COMPATIBILITY[cap][0]}.)",
                    "input": jsonschema.Draft202012Validator(in_ref, registry=registry),
                    "output": jsonschema.Draft202012Validator(out_ref, registry=registry)}
    return out


def declaration() -> dict:
    """This provider's declaration, as `fabric/memory-provider.json` ships it."""
    return {"kind": "memory-provider", "family": FAMILY, "provider": "project-observatory",
            "capabilities": list(COMPATIBILITY),
            "compatibility": {cap: tool for cap, (tool, _r) in COMPATIBILITY.items()}}


def call(capability: str, arguments: dict, tools: dict[str, Callable[..., dict]]) -> dict:
    """Run the observatory tool behind `capability` with the contract's arguments."""
    tool, renames = COMPATIBILITY[capability]
    args = {renames.get(k, k): v for k, v in arguments.items()}
    return tools[tool](**args)
