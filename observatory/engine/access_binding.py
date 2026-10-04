"""access-binding/1: who may do what with agent memory, decided from the transport.

PB-137 N-007, plan §7.2 (Fabric ADR-0026 bindings). A binding is an immutable record of
what one principal may do: which projects, which scopes, the highest classification it
may see, the highest effect it may have, for how long, and how its channel proves it is
that principal. The full decision with its reasons is docs/design/ACCESS-BINDING.md; the
accepted cases are tests/access_binding_cases.json; the published shape is
defaults/access-binding.schema.json.

This module DECIDES and never serves. N-008 calls it at every business entry point.

The rules that carry the design:

- **Identity comes from the transport, never from arguments.** `resolve` reads the
  channel the server observed — stdio, or HTTP with a bearer whose SHA-256 the server
  computed — and nothing a caller typed. An `owner`, `principal` or `caller` argument is
  data; it never selects a binding.
- **stdio is explicit and is not the operator.** The process that speaks stdio was started
  by the operator's own agent configuration, so it is the operator's LOCAL AGENT: it reads
  every project and class, and its effect stops at `propose`. Operator acts (promote,
  reject, consent) still need a terminal (`tools/review.py`, `full embedding-policy`).
- **Everything else is denied by default.** HTTP needs a bearer bound to this server's
  audience, unexpired and unrevoked. No binding, no access.
- **A broken registry is refused whole**, like embedding-policy/1: `parse` raises
  `BindingError`, and a caller then serves stdio alone and denies every HTTP request.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

SCHEMA = "access-bindings/1"
REGISTRY_FILE = "access-bindings.json"
CLASSES = ("public", "project-internal", "confidential")
ALIASES = {"private": "confidential", "secret-adjacent": "confidential",
           "internal": "project-internal"}
SCOPES = ("memory.read", "memory.search", "memory.record", "memory.checkpoint",
          "memory.handoff")
#: `read` < `propose`. `operator` exists only to be refused: it is the authority a
#: terminal grants (review.py, embedding-policy), never a binding.
EFFECTS = ("read", "propose")
CHANNELS = ("stdio", "http")
VIAS = ("terminal", "fabric-session")
PROJECT_ID = re.compile(r"^project:[a-z0-9][a-z0-9._-]{0,127}$")
WORKFLOW_ID = re.compile(r"^wf_[0-9a-f]{16}$")
BINDING_ID = re.compile(r"^bnd_[a-z0-9]{8,32}$")
PRINCIPAL = re.compile(r"^(agent|service):[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
AUDIENCE = re.compile(r"^observatory:[a-z0-9][a-z0-9._-]{0,63}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$")
_FIELDS = {"bindingId", "principal", "channel", "audience", "credentialRef", "projects",
           "workflows", "scopes", "classCeiling", "effectCeiling", "issuedAt", "expiresAt",
           "revokedAt", "issuedBy", "via"}
_HTTP_REQUIRED = _FIELDS
_HINTS = {
    "no-credential": "send the bearer the operator issued for this server in Authorization",
    "no-binding": "ask the operator to issue a binding for this principal",
    "audience-mismatch": "this bearer was issued for another Observatory instance",
    "binding-revoked": "the operator revoked this binding; ask for a new one",
    "binding-expired": "the binding expired; ask the operator to renew it",
    "unknown-channel": "connect over stdio or the loopback HTTP endpoint",
    "project-not-bound": "this binding does not cover that project",
    "project-required": "name one project; a binding never reads across every project",
    "scope-not-bound": "this binding does not include that scope",
    "workflow-not-bound": "this binding covers other workflows",
    "workflow-required": "name the workflow this session was bound to",
    "effect-above-ceiling": "the operator's authority needs a terminal, never a binding",
    "invalid-request": "the request does not name a known scope, project and effect",
}


class BindingError(ValueError):
    """The bindings registry is not a valid access-bindings/1; none of it applies."""


@dataclass(frozen=True)
class Binding:
    binding_id: str
    principal: str
    channel: str
    audience: str | None
    digest: str | None
    projects: object            # tuple of project ids, or "all" (stdio only)
    workflows: tuple | None
    scopes: frozenset
    class_ceiling: str
    effect_ceiling: str
    expires_at: str | None
    revoked_at: str | None


#: The built-in binding of the stdio channel. Not configurable, not storable.
STDIO_LOCAL = Binding(binding_id="local:stdio", principal="local:stdio", channel="stdio",
                      audience=None, digest=None, projects="all", workflows=None,
                      scopes=frozenset(SCOPES), class_ceiling="confidential",
                      effect_ceiling="propose", expires_at=None, revoked_at=None)


@dataclass(frozen=True)
class Registry:
    revision: int
    bindings: tuple = ()


@dataclass(frozen=True)
class Resolution:
    binding: Binding | None
    reason: str


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str
    binding_id: str | None = None

    def envelope(self) -> dict:
        """The wire's typed refusal (the shape `_owner_error` already uses): no isError,
        no credential material, the reason code and what to do about it."""
        who = self.binding_id or "no binding"
        return {"error": "binding refused", "code": self.reason,
                "detail": f"{who}: {self.reason}",
                "hint": _HINTS.get(self.reason, "see docs/design/ACCESS-BINDING.md"),
                "degraded": []}


def empty() -> Registry:
    return Registry(revision=0, bindings=())


def bearer_digest(token: str) -> str:
    """What the server computes from a presented bearer; the registry stores only this."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def canonical_classification(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = ALIASES.get(value, value)
    return value if value in CLASSES else None


def _ts(value: Any, where: str, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not TIMESTAMP.match(value):
        raise BindingError(f"{where} must be an RFC 3339 timestamp with seconds and a zone")
    return value


def _when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _binding(raw: Any, k: int) -> Binding:
    where = f"bindings[{k}]"
    if not isinstance(raw, dict):
        raise BindingError(f"{where} must be an object")
    channel = raw.get("channel")
    if channel == "stdio":
        raise BindingError(f"{where}: stdio is built in (local:stdio) and is never configured")
    if channel != "http":
        raise BindingError(f"{where}.channel must be 'http'")
    extra, missing = set(raw) - _FIELDS, _HTTP_REQUIRED - set(raw)
    if extra:
        raise BindingError(f"{where} has unknown field(s) {sorted(extra)}")
    if missing:
        raise BindingError(f"{where} lacks field(s) {sorted(missing)}")
    if not isinstance(raw["bindingId"], str) or not BINDING_ID.match(raw["bindingId"]):
        raise BindingError(f"{where}.bindingId must look like bnd_<8-32 lowercase letters/digits>")
    if not isinstance(raw["principal"], str) or not PRINCIPAL.match(raw["principal"]):
        raise BindingError(f"{where}.principal must be agent:<name> or service:<name>")
    if not isinstance(raw["audience"], str) or not AUDIENCE.match(raw["audience"]):
        raise BindingError(f"{where}.audience must be observatory:<instance>")
    ref = raw["credentialRef"]
    if (not isinstance(ref, dict) or set(ref) != {"kind", "digest"}
            or ref.get("kind") != "bearer-sha256" or not isinstance(ref.get("digest"), str)
            or not DIGEST.match(ref["digest"])):
        raise BindingError(f"{where}.credentialRef must be {{kind: bearer-sha256, digest: <64 hex>}}; "
                           f"the registry holds a digest, never a token")
    projects = raw["projects"]
    if (not isinstance(projects, list) or not projects or len(set(projects)) != len(projects)
            or not all(isinstance(p, str) and PROJECT_ID.match(p) for p in projects)):
        raise BindingError(f"{where}.projects must list distinct project:<slug> ids; "
                           f"an HTTP binding never covers every project")
    workflows = raw["workflows"]
    if workflows is not None and (not isinstance(workflows, list) or not workflows or
                                  not all(isinstance(w, str) and WORKFLOW_ID.match(w) for w in workflows)):
        raise BindingError(f"{where}.workflows must be null or list wf_<16 hex> ids")
    scopes = raw["scopes"]
    if (not isinstance(scopes, list) or not scopes or len(set(scopes)) != len(scopes)
            or any(s not in SCOPES for s in scopes)):
        raise BindingError(f"{where}.scopes must list distinct scopes from {list(SCOPES)}")
    if raw["classCeiling"] not in CLASSES:
        raise BindingError(f"{where}.classCeiling must be one of {list(CLASSES)}")
    if raw["effectCeiling"] not in EFFECTS:
        raise BindingError(f"{where}.effectCeiling must be one of {list(EFFECTS)}; "
                           f"the operator's authority is never granted by a binding")
    _ts(raw["issuedAt"], f"{where}.issuedAt", True)
    expires = _ts(raw["expiresAt"], f"{where}.expiresAt", True)
    revoked = _ts(raw["revokedAt"], f"{where}.revokedAt", False)
    if not isinstance(raw["issuedBy"], str) or not (raw["issuedBy"] == "operator"
                                                    or PRINCIPAL.match(raw["issuedBy"])):
        raise BindingError(f"{where}.issuedBy must be operator or service:<name>")
    if raw["via"] not in VIAS:
        raise BindingError(f"{where}.via must be one of {list(VIAS)}")
    return Binding(binding_id=raw["bindingId"], principal=raw["principal"], channel="http",
                   audience=raw["audience"], digest=ref["digest"], projects=tuple(projects),
                   workflows=tuple(workflows) if workflows else None,
                   scopes=frozenset(scopes), class_ceiling=raw["classCeiling"],
                   effect_ceiling=raw["effectCeiling"], expires_at=expires, revoked_at=revoked)


def parse(doc: Any) -> Registry:
    if not isinstance(doc, dict) or set(doc) != {"schema", "revision", "bindings"}:
        raise BindingError("the registry must be {schema, revision, bindings}")
    if doc["schema"] != SCHEMA:
        raise BindingError(f"registry.schema must be {SCHEMA!r}")
    if type(doc["revision"]) is not int or doc["revision"] < 1:
        raise BindingError("registry.revision must be an integer of at least 1")
    if not isinstance(doc["bindings"], list):
        raise BindingError("registry.bindings must be a list")
    bindings = tuple(_binding(raw, k) for k, raw in enumerate(doc["bindings"]))
    ids = [b.binding_id for b in bindings]
    digests = [b.digest for b in bindings]
    if len(set(ids)) != len(ids):
        raise BindingError("two bindings share a bindingId")
    if len(set(digests)) != len(digests):
        raise BindingError("two bindings share one credential; a bearer must name one principal")
    return Registry(revision=doc["revision"], bindings=bindings)


def load(path: pathlib.Path) -> Registry:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return empty()
    except OSError as exc:
        raise BindingError(f"the registry cannot be read ({type(exc).__name__})") from None
    try:
        return parse(json.loads(raw))
    except ValueError as exc:
        if isinstance(exc, BindingError):
            raise
        raise BindingError("the registry is not JSON") from None


def resolve(registry: Registry, channel: Any, *, now: str) -> Resolution:
    """The binding the TRANSPORT proves, or a denial reason.

    `channel` is built by the server from what it observed — `{"channel": "stdio"}`, or
    `{"channel": "http", "audience": <this server>, "bearerDigest": <sha256 it computed>}`.
    Any other key (a principal, an owner) is ignored."""
    if not isinstance(channel, Mapping):
        return Resolution(None, "unknown-channel")
    kind = channel.get("channel")
    if kind == "stdio":
        return Resolution(STDIO_LOCAL, "stdio-local")
    if kind != "http":
        return Resolution(None, "unknown-channel")
    digest = channel.get("bearerDigest")
    if not isinstance(digest, str) or not DIGEST.match(digest):
        return Resolution(None, "no-credential")
    match = next((b for b in registry.bindings if b.digest == digest), None)
    if match is None:
        return Resolution(None, "no-binding")
    if channel.get("audience") != match.audience:
        return Resolution(None, "audience-mismatch")
    if match.revoked_at is not None:
        return Resolution(None, "binding-revoked")
    if _when(match.expires_at) <= _when(now):
        return Resolution(None, "binding-expired")
    return Resolution(match, "bearer-matched")


def authorize(binding: Binding | None, request: Any) -> Decision:
    """May this binding perform this request? Only `scope`, `projectId`, `workflowId` and
    `effect` are read; any identity the request claims is ignored."""
    if binding is None:
        return Decision(False, "no-binding")
    bid = binding.binding_id
    if not isinstance(request, Mapping):
        return Decision(False, "invalid-request", bid)
    scope, effect = request.get("scope"), request.get("effect")
    project, workflow = request.get("projectId"), request.get("workflowId")
    if not isinstance(scope, str) or not isinstance(effect, str) \
            or (project is not None and not isinstance(project, str)):
        return Decision(False, "invalid-request", bid)
    if effect not in EFFECTS:
        return Decision(False, "effect-above-ceiling", bid)
    if EFFECTS.index(effect) > EFFECTS.index(binding.effect_ceiling):
        return Decision(False, "effect-above-ceiling", bid)
    if scope not in binding.scopes:
        return Decision(False, "scope-not-bound", bid)
    if binding.projects != "all":
        if project is None:
            return Decision(False, "project-required", bid)
        if project not in binding.projects:
            return Decision(False, "project-not-bound", bid)
    if binding.workflows is not None:
        if workflow is None:
            return Decision(False, "workflow-required", bid)
        if workflow not in binding.workflows:
            return Decision(False, "workflow-not-bound", bid)
    return Decision(True, "allowed", bid)


def may_see(binding: Binding | None, classification: Any) -> bool:
    """Is a record of this classification visible to this binding? Unknown → never."""
    if binding is None:
        return False
    cls = canonical_classification(classification)
    return cls is not None and CLASSES.index(cls) <= CLASSES.index(binding.class_ceiling)


def query_context(binding: Binding | None, project_id: Any) -> dict:
    """The embedding-policy/1 query context this binding vouches for (N-002 rule 9).

    The query is classified at the binding's ceiling — the most it may read — so a
    binding that may see confidential text never exports its queries. A project outside
    the binding gets no trusted context at all."""
    covered = binding is not None and (binding.projects == "all" or project_id in binding.projects)
    if not covered or not isinstance(project_id, str):
        return {"project_id": project_id, "classification": None, "authority": "caller-argument"}
    return {"project_id": project_id, "classification": binding.class_ceiling,
            "authority": "binding"}
