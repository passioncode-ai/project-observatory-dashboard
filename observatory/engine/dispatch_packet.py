"""A session Fabric starts for a board request: its packet, what a reader sees of it, and its keys (OBS-323).

Fabric's specification of on-demand sessions (`docs/evidence/specs/2026-11-04-on-demand-sessions.md` §3.4 in
passioncode-ai/fabric, agreed with Observatory on 2026-11-04): a board request nobody answers starts a session, and
the dispatcher writes an Observatory workflow for it as `service:fabric`. Its checkpoint `artifacts` carry
references only, each `{kind: "other", ref}`:

    board-request:<uuid>      the request          (required for keys)
    board-thread:<uuid>       its thread
    capability:<name>         the capability it asks for
    fabric-project:<uuid>     Fabric's id of the project (required for keys)
    deadline:<ISO 8601 UTC>   the board's deadline   (required for keys)
    run-ends-at:<ISO 8601 UTC>  on each dispatch's own checkpoint: when that run must end

No message body and no absolute folder travels in the packet, and no secret: keys are reached by name through
`use_secret.py run` inside the session. The body's `credentials` (`[{project, env, name, purpose}]`, names only)
say which. Observatory gives a value only:

- to the holder of the workflow's lease (the run took it with `observatory_handoff_accept`);
- for a slot `credentials` names, and the request's environment;
- from the project `fabric-project` resolves to (its `fabric:<uuid>`, as Observatory pulled it from Fabric's hub);
- until the deadline, and never after the final checkpoint closed the workflow;

and none to a packet without the request, the project or the deadline, or without `credentials`. Each refusal is
`Refused` with a code (`REFUSALS`), never a value or a lease in its text.

`validate` runs inside `store.workflow.checkpoint_body`, so a malformed packet is refused on the way in; `view`
adds what a person needs to follow the request to `observatory_checkpoint_latest` and `observatory_workflow_list`.
"""
from __future__ import annotations

import hmac
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_UTC = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z"
#: Each reference the packet may carry, by its prefix, with the exact shape of its value.
REFS = {
    "board-request": re.compile(_UUID),
    "board-thread": re.compile(_UUID),
    "capability": re.compile(r"[a-z][a-z0-9._-]{1,127}"),
    "fabric-project": re.compile(_UUID),
    "deadline": re.compile(_UTC),
    "run-ends-at": re.compile(_UTC),
}
#: What keys need: without any of these a packet gets none (§3.4).
REQUIRED_FOR_KEYS = ("board-request", "fabric-project", "deadline")
#: The refs memory_redact keeps whole (a UUID inside them is no credential).
KEEP_PATTERN = r"(?:board-request|board-thread|fabric-project):" + _UUID
_ABSOLUTE = re.compile(r"^(?:/|~[\\/]|[A-Za-z]:[\\/]|\\\\|file:)", re.IGNORECASE)

REFUSALS = {
    "workflow-unknown": "no such workflow",
    "packet-incomplete": "the workflow's packet lacks the request, the project or the deadline, so it gets no keys",
    "no-credentials": "the packet names no credentials, so it gets none",
    "workflow-closed": "the workflow's final checkpoint closed it; nothing more is handed out",
    "deadline-passed": "the request's deadline has passed; nothing more is handed out",
    "not-lease-holder": "only the holder of the workflow's lease gets keys; take it with observatory_handoff_accept "
                        "and pass its leaseId in OBSERVATORY_WORKFLOW_LEASE",
    "slot-not-named": "the packet's credentials do not name this slot in this environment",
    "project-unresolved": "the packet's fabric-project does not resolve to one project of this registry",
    "project-mismatch": "the slot is not the project the packet's fabric-project resolves to",
}


class Refused(Exception):
    """A hand-out the packet does not allow: `code` is one of REFUSALS."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"refused: {code}: {REFUSALS[code]}" + (f" ({detail})" if detail else ""))
        self.code = code


class Invalid(ValueError):
    """A packet artifact that is not one of REFS in its exact shape."""


def _prefix(ref: Any) -> str | None:
    if not isinstance(ref, str) or ":" not in ref:
        return None
    head = ref.split(":", 1)[0]
    return head if head in REFS else None


def validate(artifacts: list[dict]) -> None:
    """Refuse a malformed packet (raises Invalid). A ref with a known prefix is `{kind: "other", ref}` and nothing
    else, its value in its exact shape, each prefix once; a body that carries one is a packet, and a packet holds
    no absolute path in any artifact. Other workflows are not touched."""
    seen: set[str] = set()
    for i, a in enumerate(artifacts):
        head = _prefix(a.get("ref"))
        if head is None:
            continue
        where = f"body.artifacts[{i}]"
        if a.get("kind") != "other" or set(a) != {"kind", "ref"}:
            raise Invalid(f"{where}: a {head}: reference is {{kind: \"other\", ref}} and nothing else; "
                          f"no note, path or text travels in the packet")
        if not REFS[head].fullmatch(a["ref"][len(head) + 1:]):
            raise Invalid(f"{where}.ref: {head}: takes "
                          + ("a lower-case UUID" if head in ("board-request", "board-thread", "fabric-project")
                             else "a capability name" if head == "capability" else "a UTC time, …T…Z"))
        if head in seen:
            raise Invalid(f"{where}.ref: {head}: is named twice; a packet carries one of each")
        seen.add(head)
    if not seen:
        return
    for i, a in enumerate(artifacts):
        for member in ("path", "ref"):
            if isinstance(a.get(member), str) and _ABSOLUTE.search(a[member]):
                raise Invalid(f"body.artifacts[{i}].{member}: a packet carries no absolute folder; the launch's "
                              f"working directory is the folder")


def refs_of(body: dict | None) -> dict[str, str]:
    """{prefix: value} of the packet refs a checkpoint body carries."""
    out = {}
    for a in (body or {}).get("artifacts") or []:
        head = _prefix(a.get("ref")) if isinstance(a, dict) else None
        if head and a.get("kind") == "other":
            out.setdefault(head, a["ref"][len(head) + 1:])
    return out


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _slot(c: dict) -> str:
    return f"{str(c['project']).split(':', 1)[-1]}/{c['env']}/{c['name']}"


def _window(status: str, refs: dict, credentials: list, now: datetime) -> dict:
    out: dict = {"slots": [_slot(c) for c in credentials]}
    if refs.get("deadline"):
        out["until"] = refs["deadline"]
    if status != "open":
        out.update(state="closed", reason="workflow-closed")
    elif any(k not in refs for k in REQUIRED_FOR_KEYS):
        out.update(state="closed", reason="packet-incomplete")
    elif now >= _parse(refs["deadline"]):
        out.update(state="closed", reason="deadline-passed")
    elif not credentials:
        out.update(state="closed", reason="no-credentials")
    else:
        out["state"] = "open"
    return out


def view(latest: dict, *, now: datetime | None = None) -> dict | None:
    """What a person needs to follow the request, from a `checkpoint_latest` answer, or None for a workflow that
    carries no packet: the board refs, the run's end and whether keys can be handed out (and why not)."""
    body = ((latest or {}).get("checkpoint") or {}).get("body") or {}
    refs = refs_of(body)
    if not refs:
        return None
    out = {"request": f"board-request:{refs['board-request']}" if "board-request" in refs else None,
           "thread": f"board-thread:{refs['board-thread']}" if "board-thread" in refs else None,
           "capability": refs.get("capability"),
           "fabricProject": f"fabric-project:{refs['fabric-project']}" if "fabric-project" in refs else None,
           "deadline": refs.get("deadline"), "runEndsAt": refs.get("run-ends-at"),
           "keys": _window(latest.get("status") or "open", refs, body.get("credentials") or [],
                           now or datetime.now(timezone.utc))}
    return {k: v for k, v in out.items() if v is not None}


def keys_state(conn: sqlite3.Connection, workflow_id: str, *, now: datetime | None = None) -> dict:
    """Whether keys can be handed out for `workflow_id` now, and why not. Reads only."""
    wf = conn.execute("SELECT status FROM workflows WHERE workflow_id = ?", (workflow_id,)).fetchone()
    if wf is None:
        return {"state": "closed", "reason": "workflow-unknown"}
    body = _body(conn, workflow_id)
    return _window(wf["status"], refs_of(body), body.get("credentials") or [], now or datetime.now(timezone.utc))


def _body(conn: sqlite3.Connection, workflow_id: str) -> dict:
    row = conn.execute("SELECT l.body_json FROM ledger l LEFT JOIN tombstones t ON t.memory_id = l.memory_id"
                       " WHERE l.memory_id = ? AND t.memory_id IS NULL ORDER BY l.revision DESC LIMIT 1",
                       (f"ckpt:{workflow_id}",)).fetchone()
    return json.loads(row["body_json"] or "{}") if row else {}


def resolve_fabric_project(uuid: str) -> tuple[str | None, str | None]:
    """(project id, None) for the one registry project recorded against `fabric:<uuid>`, else (None, why)."""
    try:
        import fabric_attribution
        owners = fabric_attribution.projects_for(f"fabric:{uuid}")
    except Exception as exc:  # noqa: BLE001: unresolved, said
        return None, f"Fabric's project ids could not be read: {type(exc).__name__}"
    if len(owners) == 1:
        return owners[0], None
    return None, ("several projects carry it" if owners else
                  "no project of this registry is recorded against it; `full fabric-attribution` asks again")


def project_of(text: str) -> str | None:
    """The registry project a vault folder or project id names (vault_project.resolve), or None."""
    import vault_project
    return vault_project.resolve(text).project_id


def authorize(conn: sqlite3.Connection, workflow_id: str, lease: str | None, *, project: str, env: str | None,
              names: list[str], now: datetime | None = None,
              resolve_fabric_project: Callable[[str], tuple[str | None, str | None]] = resolve_fabric_project,
              project_of: Callable[[str], str | None] = project_of) -> dict:
    """The hand-out of `names` from `project`/`env` to the presenter of `lease`, under `workflow_id`'s packet.
    Returns what was allowed (no value); raises Refused with its code. Checks in the order a person would fix
    them: the workflow, its packet, the deadline, the lease, the slots, the project."""
    now = now or datetime.now(timezone.utc)
    wf = conn.execute("SELECT status FROM workflows WHERE workflow_id = ?", (workflow_id,)).fetchone()
    if wf is None:
        raise Refused("workflow-unknown", workflow_id)
    body = _body(conn, workflow_id)
    refs = refs_of(body)
    if wf["status"] != "open":
        raise Refused("workflow-closed")
    missing = [k for k in REQUIRED_FOR_KEYS if k not in refs]
    if missing:
        raise Refused("packet-incomplete", "missing " + ", ".join(missing))
    if now >= _parse(refs["deadline"]):
        raise Refused("deadline-passed", refs["deadline"])
    active = conn.execute("SELECT token FROM workflow_leases WHERE workflow_id = ? AND state = 'active'",
                          (workflow_id,)).fetchone()
    if not lease or active is None or active["token"] is None or not hmac.compare_digest(active["token"], lease):
        raise Refused("not-lease-holder")
    credentials = body.get("credentials") or []
    if not credentials:
        raise Refused("no-credentials")
    pid, why = resolve_fabric_project(refs["fabric-project"])
    if pid is None:
        raise Refused("project-unresolved", why or "")
    if project_of(project) != pid:
        raise Refused("project-mismatch", f"{project} is not {pid}")
    allowed = []
    for name in names:
        match = [c for c in credentials if c["name"] == name and (env is None or c["env"] == env)
                 and project_of(str(c["project"])) == pid]
        if not match:
            raise Refused("slot-not-named", f"{name} in {env or 'any environment'}")
        allowed.append(_slot({**match[0], "project": project}))
    return {"workflowId": workflow_id, "request": f"board-request:{refs['board-request']}",
            "fabricProject": f"fabric-project:{refs['fabric-project']}", "project": pid,
            "slots": allowed, "until": refs["deadline"]}


__all__ = ["REFS", "REQUIRED_FOR_KEYS", "REFUSALS", "Refused", "Invalid", "validate", "refs_of", "view",
           "keys_state", "authorize"]
