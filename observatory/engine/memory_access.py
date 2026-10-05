"""access-bindings/1 enforced at every business entry point (PB-137 N-008).

`access_binding.py` decides; this module is where the engine ASKS, once per call, in
the same way for every tool. The design and its rules: docs/design/ACCESS-BINDING.md,
section "Enforcement".

- **The transport sets the channel; nothing else may.** stdio serving sets the
  process default to the local agent (`serve_stdio`). An HTTP request sets its own
  channel for the duration of the request (`channel(...)`), built from what the server
  observed: its audience, the SHA-256 of the bearer, and Fabric's `X-Fabric-Projects`.
  A process that serves HTTP calls `serve_http()` first, which removes the default, so
  a request that forgot to set its channel is refused rather than served as stdio.
- **The memory family is the only surface a binding reaches.** `TOOLS` names each
  memory tool with its scope and effect. Every other tool and every resource is
  local-only: the stdio agent reaches it, a binding never does (`local_only`).
- **The target decides the project, not the caller.** A tool that acts on a workflow or
  a handoff resolves its project from the store before it asks (`target`), so a caller
  cannot name a project it is bound to and act on a workflow of another. A target that
  does not exist is refused to a binding exactly like a foreign one: a binding learns
  nothing about other projects from the difference.
- **A binding writes as its principal.** Over HTTP, `owner` must be the binding's
  principal, and the idempotency key is kept per binding (`idempotency_caller`), so a
  retry by another caller never receives the first caller's answer or lease token.
- **Fabric narrows, never widens** (F-015, fabric-agent-contract DEC-0023). When the
  channel carries `X-Fabric-Projects`, the binding's projects are intersected with it.
  Without the header the call is workspace-level and the binding applies as issued.
- **Revocation is immediate**: the registry is read on every HTTP call, and a revision
  lower than one this workspace already applied is refused whole, so a restored backup
  cannot reopen a revoked binding.
- **Every decision about a binding is journalled** (`store/logs/access.jsonl`, 0600):
  binding id, principal, tool, scope, effect, project, workflow, verdict, reason. Never
  a bearer and never its digest. The stdio agent's allowed calls are not journalled;
  its refusals are.
"""
from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import json
import os
import pathlib
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping

import access_binding as AB

#: The memory family: tool → (scope, effect). Everything not listed is local-only.
TOOLS: dict[str, tuple[str, str]] = {
    "observatory_search": ("memory.search", "read"),
    "observatory_recall": ("memory.read", "read"),
    "observatory_record": ("memory.record", "propose"),
    "observatory_workflow_list": ("memory.read", "read"),
    "observatory_checkpoint_write": ("memory.checkpoint", "propose"),
    "observatory_checkpoint_latest": ("memory.checkpoint", "read"),
    "observatory_handoff_create": ("memory.handoff", "propose"),
    "observatory_handoff_accept": ("memory.handoff", "propose"),
    "observatory_handoff_get": ("memory.handoff", "read"),
    # Reads back a receipt this caller's own search produced (PB-137 N-012).
    "observatory_explain": ("memory.search", "read"),
}
#: Tools that list workflows rather than act on one: a session binding lists its own.
LISTINGS = frozenset({"observatory_workflow_list"})
#: The scope a local-only tool is refused under, so the refusal names what is missing.
LOCAL_SCOPE = "estate.local"
SEEN_FILE = "access-bindings.seen.json"
JOURNAL = "access.jsonl"
#: Header Fabric's hub sets on the hop to Observatory: the caller's projects.
FABRIC_PROJECTS_HEADER = "X-Fabric-Projects"

_HINTS = {
    "registry-unreadable": "the operator must repair config/access-bindings.json "
                           "(`project-observatory full access-binding show` names the fault); "
                           "until then only the local stdio agent is served",
    "local-only": "this tool serves the local stdio agent only; a binding reaches the memory "
                  "tools (observatory_search, _explain, _recall, _record, _workflow_list, "
                  "_checkpoint_*, _handoff_*)",
    "owner-not-principal": "over HTTP, `owner` must be this binding's principal",
    "fabric-projects-invalid": "X-Fabric-Projects must be a comma list of project:<slug> ids",
    "class-ceiling-below-workflow": "workflow memory is project-internal; this binding sees "
                                    "only public records",
    "target-not-bound": "no such workflow or handoff within this binding: it does not exist, "
                        "or it belongs to a project or workflow the binding does not cover",
}

_CHANNEL: contextvars.ContextVar[Mapping | None] = contextvars.ContextVar(
    "observatory_access_channel", default=None)
#: What a call with no channel of its own runs as. None until a transport says.
_DEFAULT: dict | None = None


class Refused(Exception):
    """A typed refusal on its way to the wire; `envelope` is what the caller reads."""

    def __init__(self, envelope: dict) -> None:
        super().__init__(envelope.get("code"))
        self.envelope = envelope


@dataclasses.dataclass(frozen=True)
class Grant:
    """What an allowed call runs with."""
    binding: AB.Binding
    scope: str
    effect: str
    project_id: str | None
    workflow_id: str | None

    @property
    def local(self) -> bool:
        return self.binding.channel == "stdio"

    def idempotency_caller(self, owner: str) -> str:
        """The principal an idempotency record is kept under. The stdio agent keeps the
        owner it always used, so existing records still replay; a binding's records are
        its own, so another binding repeating the key never receives this answer."""
        return owner if self.local else f"{owner}@{self.binding.binding_id}"

    def may_see(self, classification: Any) -> bool:
        return AB.may_see(self.binding, classification)

    @property
    def hidden_kinds(self) -> tuple[str, ...]:
        """Record kinds a general reader (search) must not show this caller: workflow
        memory needs its own scope, as the workflow tools require. Empty for the local
        agent."""
        if self.local:
            return ()
        hidden = []
        if "memory.checkpoint" not in self.binding.scopes:
            hidden.append("checkpoint")
        if "memory.handoff" not in self.binding.scopes:
            hidden.append("handoff")
        return tuple(hidden)

    @property
    def visible_classes(self) -> tuple[str, ...] | None:
        """The stored classification values this caller may read, aliases included;
        None for the local agent, which reads every class."""
        if self.local:
            return None
        return tuple(sorted(v for v in (*AB.CLASSES, *AB.ALIASES)
                            if AB.may_see(self.binding, v)))


# ─────────────────────────────── the channel ────────────────────────────────

#: Set once a process serves HTTP. From then on `serve_stdio` cannot hand calls without a
#: channel back to the local agent — importing `mcp/server.py` after `serve_http()`
#: would otherwise have failed open (review 2026-10-05).
_HTTP_PROCESS = False


def serve_stdio() -> None:
    """This process serves stdio: a call with no channel of its own is the local agent.
    A no-op in a process that already serves HTTP."""
    global _DEFAULT
    if _HTTP_PROCESS:
        return
    _DEFAULT = {"channel": "stdio"}


def serve_http() -> None:
    """This process serves HTTP: every call must carry its own channel, and none
    inherits the local agent's — now or after any later `serve_stdio()`."""
    global _DEFAULT, _HTTP_PROCESS
    _HTTP_PROCESS = True
    _DEFAULT = None


def reset_for_tests() -> None:
    """Back to a stdio process. For test fixtures that serve HTTP and stdio in turn;
    nothing in the engine calls it."""
    global _DEFAULT, _HTTP_PROCESS
    _HTTP_PROCESS = False
    _DEFAULT = {"channel": "stdio"}


@contextlib.contextmanager
def channel(observed: Mapping) -> Iterator[None]:
    """Run the enclosed calls on the channel the transport observed."""
    token = _CHANNEL.set(dict(observed))
    try:
        yield
    finally:
        _CHANNEL.reset(token)


def own_audience() -> str:
    """This workspace's audience: a bearer issued for another instance opens nothing here."""
    import service_identity
    return f"observatory:{service_identity.instance()}"


def http_channel(*, bearer: str | None, audience: str | None = None,
                 fabric_projects: str | None = None) -> dict:
    """The channel of one HTTP request, built from what the server saw. The bearer
    is hashed here and goes no further. `audience` defaults to this workspace's."""
    out: dict[str, Any] = {"channel": "http",
                           "audience": audience if audience is not None else own_audience(),
                           "bearerDigest": AB.bearer_digest(bearer) if bearer else None}
    if fabric_projects is not None:
        out["fabricProjects"] = fabric_projects
    return out


def current() -> Mapping | None:
    observed = _CHANNEL.get()
    return observed if observed is not None else _DEFAULT


def is_local() -> bool:
    observed = current()
    return isinstance(observed, Mapping) and observed.get("channel") == "stdio"


# ─────────────────────────────── the registry ───────────────────────────────

def registry_path() -> pathlib.Path:
    import paths
    return paths.config_file(AB.REGISTRY_FILE)


def _seen_path() -> pathlib.Path:
    import paths
    return pathlib.Path(paths.STATE) / SEEN_FILE


def registry(path: pathlib.Path | None = None, seen: pathlib.Path | None = None) -> AB.Registry:
    """The bindings in force, or BindingError. A revision lower than the highest this
    workspace applied is refused: a restored backup must not reopen a revoked binding."""
    path = path or registry_path()
    seen = seen or _seen_path()
    reg = AB.load(path)
    try:
        last = int(json.loads(seen.read_text(encoding="utf-8")).get("revision", 0))
    except FileNotFoundError:
        last = 0
    except (OSError, ValueError, AttributeError, TypeError):
        raise AB.BindingError("the record of the last applied bindings revision is unreadable") \
            from None
    if reg.revision < last:
        raise AB.BindingError(f"bindings revision {reg.revision} is older than revision {last}, "
                              f"which this workspace already applied; refusing a rollback")
    if reg.revision > last:
        import atomic
        try:
            seen.parent.mkdir(parents=True, exist_ok=True)
            atomic.write_json(seen, {"revision": reg.revision})
        except (OSError, ValueError) as exc:
            raise AB.BindingError(f"the applied revision cannot be recorded "
                                  f"({type(exc).__name__}); no binding is served") from None
    return reg


def status(config_dir: pathlib.Path, state_dir: pathlib.Path) -> dict:
    """What `full doctor` says, READ-ONLY: whether the registry reads, how many bindings
    are in force, and for whom. Names, never digests. Nothing is recorded here."""
    label = f"config/{AB.REGISTRY_FILE}"
    path = config_dir / AB.REGISTRY_FILE
    if not path.exists():
        return {"state": "absent", "file": label, "bindings": 0, "inForce": 0,
                "note": "no bindings: only the local stdio agent is served"}
    try:
        reg = AB.load(path)
        try:
            last = int(json.loads((state_dir / SEEN_FILE).read_text(encoding="utf-8"))
                       .get("revision", 0))
        except FileNotFoundError:
            last = 0
        if reg.revision < last:
            raise AB.BindingError(f"bindings revision {reg.revision} is older than revision "
                                  f"{last}, which this workspace already applied")
    except (AB.BindingError, OSError, ValueError, AttributeError, TypeError) as exc:
        return {"state": "refused", "file": label, "reason": str(exc), "bindings": 0,
                "inForce": 0, "note": "every HTTP request is refused until it is repaired; "
                                      "the local stdio agent is still served"}
    now = _now()
    live = [b for b in reg.bindings
            if b.revoked_at is None and AB._when(b.expires_at) > AB._when(now)]
    return {"state": "ok", "file": label, "revision": reg.revision,
            "bindings": len(reg.bindings), "inForce": len(live),
            "principals": sorted({b.principal for b in live})}


# ─────────────────────────────── deciding ───────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def refusal(code: str, binding_id: str | None, detail: str | None = None) -> dict:
    if code in _HINTS:
        who = binding_id or "no binding"
        return {"error": "binding refused", "code": code,
                "detail": f"{who}: {detail or code}", "hint": _HINTS[code], "degraded": []}
    return AB.Decision(False, code, binding_id).envelope()


def _narrow(binding: AB.Binding, observed: Mapping) -> AB.Binding:
    """The binding as Fabric's hop narrows it: projects ∩ X-Fabric-Projects."""
    raw = observed.get("fabricProjects")
    if raw is None or binding.projects == "all":
        return binding
    names = [p.strip() for p in str(raw).split(",") if p.strip()]
    if not names or not all(AB.PROJECT_ID.match(p) for p in names):
        raise Refused(refusal("fabric-projects-invalid", binding.binding_id))
    narrowed = tuple(p for p in binding.projects if p in set(names))
    if not narrowed:
        raise Refused(refusal("project-not-bound", binding.binding_id,
                               "no project of this binding is among X-Fabric-Projects"))
    return dataclasses.replace(binding, projects=narrowed)


def _binding() -> AB.Binding:
    observed = current()
    if isinstance(observed, Mapping) and observed.get("channel") == "stdio":
        # The local agent is served even when the registry is broken (rule 11).
        return AB.STDIO_LOCAL
    if not isinstance(observed, Mapping) or observed.get("channel") != "http":
        raise Refused(refusal("unknown-channel", None))
    try:
        reg = registry()
    except AB.BindingError as exc:
        raise Refused(refusal("registry-unreadable", None, str(exc))) from None
    found = AB.resolve(reg, observed, now=_now())
    if found.binding is None:
        raise Refused(refusal(found.reason, None))
    return _narrow(found.binding, observed)


def caller() -> AB.Binding:
    """Who is calling, from the transport, before anything is authorized. Raises Refused
    when the channel proves no binding. Used where the target must be checked against the
    caller before its project may be named (an explain of someone else's receipt)."""
    try:
        return _binding()
    except Refused as exc:
        _journal(None, "caller", LOCAL_SCOPE, "read", None, None, False, exc.envelope.get("code"))
        raise


def authorize(tool: str, *, project_id: str | None = None, workflow_id: str | None = None,
              owner: str | None = None) -> Grant:
    """May the caller on this channel run `tool` on this target? Raises Refused."""
    try:
        scope, effect = TOOLS[tool]
    except KeyError:
        raise KeyError(f"{tool} is not a memory tool; gate it with local_only") from None
    binding: AB.Binding | None = None
    try:
        binding = _binding()
        if tool in LISTINGS and workflow_id is None and binding.workflows:
            # A listing names no workflow; a session binding may still list, and the
            # tool shows only its bound workflows (`Grant.binding.workflows`).
            workflow_id = binding.workflows[0]
        if owner is not None and binding.channel != "stdio" and owner != binding.principal:
            raise Refused(refusal("owner-not-principal", binding.binding_id,
                                   f"owner is not {binding.principal}"))
        decision = AB.authorize(binding, {"scope": scope, "effect": effect,
                                          "projectId": project_id, "workflowId": workflow_id})
        if not decision.allow:
            raise Refused(decision.envelope())
    except Refused as exc:
        _journal(binding, tool, scope, effect, project_id, workflow_id, False,
                 exc.envelope.get("code"))
        raise
    grant = Grant(binding, scope, effect, project_id, workflow_id)
    if not grant.local:
        _journal(binding, tool, scope, effect, project_id, workflow_id, True, "allowed")
    return grant


def local_only(what: str) -> dict | None:
    """None when the local stdio agent asks; a refusal for anything else."""
    if is_local():
        return None
    binding = None
    try:
        binding = _binding()
        envelope = refusal("local-only", binding.binding_id, f"{what} is local-only")
    except Refused as exc:
        envelope = exc.envelope
    _journal(binding, what, LOCAL_SCOPE, "read", None, None, False, envelope.get("code"))
    return envelope


def target(conn, *, workflow_id: str | None = None, handoff_id: str | None = None) \
        -> tuple[str | None, str | None, bool]:
    """(project, workflow, exists) of a workflow or handoff, read from the store — the
    project a call acts on is the target's, never the one the caller names."""
    if handoff_id is not None:
        row = conn.execute("SELECT workflow_id FROM workflow_leases WHERE handoff_id = ?",
                           (handoff_id,)).fetchone()
        if row is None:
            return None, None, False
        workflow_id = row[0]
    if workflow_id is None:
        return None, None, False
    row = conn.execute("SELECT project_id FROM workflows WHERE workflow_id = ?",
                       (workflow_id,)).fetchone()
    if row is None:
        return None, workflow_id, False
    return row[0], workflow_id, True


def authorize_target(tool: str, conn, *, workflow_id: str | None = None,
                     handoff_id: str | None = None, owner: str | None = None) -> Grant:
    """`authorize` on the target's own project.

    TO A BINDING, A TARGET OUTSIDE IT AND A TARGET THAT DOES NOT EXIST ARE ONE ANSWER,
    `target-not-bound`, given BEFORE owner, effect or scope are looked at. Checking those
    first made the refusal code differ by whether a foreign id existed (found in review,
    2026-10-05: `effect-above-ceiling` for a foreign handoff, `project-not-bound` for a
    made-up one). The local agent gets the store's own answer."""
    project, wid, exists = target(conn, workflow_id=workflow_id, handoff_id=handoff_id)
    if not is_local():
        binding = None
        try:
            binding = _binding()
            outside = (not exists
                       or (binding.projects != "all" and project not in binding.projects)
                       or (binding.workflows is not None and wid not in binding.workflows))
            envelope = refusal("target-not-bound", binding.binding_id) if outside else None
        except Refused as exc:
            envelope = exc.envelope
        if envelope is not None:
            scope, effect = TOOLS[tool]
            _journal(binding, tool, scope, effect, None, wid, False, envelope.get("code"))
            raise Refused(envelope)
    return authorize(tool, project_id=project, workflow_id=wid, owner=owner)


# ─────────────────────────────── the journal ────────────────────────────────

def _journal(binding: AB.Binding | None, tool: str, scope: str, effect: str,
             project_id: str | None, workflow_id: str | None, allowed: bool,
             reason: str | None) -> None:
    if binding is not None and binding.channel == "stdio" and allowed:
        return
    import paths
    row = {"at": _now(), "binding": None if binding is None else binding.binding_id,
           "principal": None if binding is None else binding.principal, "tool": tool,
           "scope": scope, "effect": effect, "projectId": project_id,
           "workflowId": workflow_id, "allowed": allowed, "reason": reason}
    target_file = pathlib.Path(paths.STATE) / "logs" / JOURNAL
    try:
        target_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        # The decision stands either way; a lost journal line is the smaller failure
        # than refusing a call because a log could not be written.
        pass
