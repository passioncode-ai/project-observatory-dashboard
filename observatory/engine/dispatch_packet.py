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

THE PACKET IS FROZEN WHEN THE DISPATCHER WRITES IT. Every checkpoint stores its body whole, and the run the packet
constrains writes the next one: read from the latest revision, the packet would be whatever the run last wrote, so
it could name itself another slot and a later deadline, and an honest step checkpoint that leaves the refs out would
shut its own keys. So the refs above (but `run-ends-at`) and `credentials` are read from the FIRST revision that
carries a packet (`packet_of`), the dispatcher's, written before any handoff. A later checkpoint may leave them out,
and the frozen packet stands; one that names a frozen ref with another value, a ref the packet did not carry, or
another set of credentials is refused (`check_write`, `packet-frozen`). The deadline the dispatcher writes is at
most seven days after that write (`deadline-too-far`), the board's own limit on a request. `run-ends-at` stays per
dispatch: the last one written is shown, and nothing reads it as a permission, so it cannot widen the deadline.

`validate` runs inside `store.workflow.checkpoint_body`, so a malformed packet is refused on the way in; `view`
adds what a person needs to follow the request to `observatory_checkpoint_latest` and `observatory_workflow_list`.
"""
from __future__ import annotations

import hmac
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
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
#: The refs frozen with the packet at the dispatcher's write; `run-ends-at` belongs to each dispatch's own checkpoint.
FROZEN = ("board-request", "board-thread", "capability", "fabric-project", "deadline")
#: How far after the write that sets it a packet's deadline may be: the board's own limit on a request (7 days).
MAX_HORIZON = timedelta(days=7)
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
    "deadline-too-far": "the packet's deadline is more than 7 days after the dispatcher wrote it, the board's limit "
                        "on a request; it gets no keys",
    "packet-frozen": "the packet is frozen at the dispatcher's write; a later checkpoint may leave its refs and "
                     "credentials out, never change them",
    "not-lease-holder": "only the holder of the workflow's lease gets keys; take it with observatory_handoff_accept "
                        "and present its leaseId in OBSERVATORY_WORKFLOW_LEASE or a private "
                        "file named in OBSERVATORY_WORKFLOW_LEASE_FILE",
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


def _time(value: Any) -> datetime | None:
    """A packet time in its exact shape, or None: a row written before refs were validated may hold anything, and
    an unreadable deadline is a packet without one, never an exception in a reader or the gate."""
    if not isinstance(value, str) or not REFS["deadline"].fullmatch(value):
        return None
    try:
        return _parse(value)
    except ValueError:
        return None


def _slot(c: dict) -> str:
    return f"{str(c['project']).split(':', 1)[-1]}/{c['env']}/{c['name']}"


def _slots(credentials: list) -> set[tuple[str, str, str]]:
    return {(str(c.get("project")), str(c.get("env")), str(c.get("name"))) for c in credentials or []
            if isinstance(c, dict)}


def _rows(conn: sqlite3.Connection, workflow_id: str, needles: tuple[str, ...], order: str):
    """The checkpoint revisions of `workflow_id` whose body mentions one of `needles`, in `order` of revision. The
    text filter only narrows the read; `refs_of` decides."""
    where = " OR ".join("instr(l.body_json, ?) > 0" for _ in needles)
    return conn.execute("SELECT l.revision, l.body_json, l.created_at FROM ledger l LEFT JOIN tombstones t"
                        " ON t.memory_id = l.memory_id WHERE l.memory_id = ? AND t.memory_id IS NULL"
                        f" AND ({where}) ORDER BY l.revision {order}",
                        (f"ckpt:{workflow_id}", *(f"{n}:" for n in needles)))


def packet_of(conn: sqlite3.Connection, workflow_id: str) -> dict | None:
    """The frozen packet of `workflow_id`, or None for a workflow that never carried one: `refs` (the FROZEN refs) and
    `credentials` of the first revision that carries a packet, that revision and when it was written, and the
    `run-ends-at` of the last revision that names one. Reads only."""
    packet = None
    for row in _rows(conn, workflow_id, FROZEN, "ASC"):
        body = json.loads(row["body_json"] or "{}")
        refs = refs_of(body)
        if any(k in refs for k in FROZEN):
            packet = {"refs": {k: refs[k] for k in FROZEN if k in refs},
                      "credentials": body.get("credentials") or [],
                      "revision": row["revision"], "writtenAt": row["created_at"]}
            break
    if packet is None:
        return None
    for row in _rows(conn, workflow_id, ("run-ends-at",), "DESC"):
        ends = refs_of(json.loads(row["body_json"] or "{}")).get("run-ends-at")
        if ends:
            packet["runEndsAt"] = ends
            break
    return packet


def check_write(packet: dict | None, body: dict, now: datetime) -> None:
    """Refuse a checkpoint body that would change the frozen `packet` (raises Refused `packet-frozen`), or that sets
    a packet whose deadline is more than MAX_HORIZON after `now` (`deadline-too-far`). Omitting the refs or the
    credentials keeps the packet; a body of a workflow that carries no packet is not touched."""
    refs = refs_of(body)
    if packet is None:
        if any(k in refs for k in FROZEN):
            deadline = _time(refs.get("deadline"))
            if deadline is not None and deadline > now + MAX_HORIZON:
                raise Refused("deadline-too-far", refs["deadline"])
        return
    changed = [k for k in FROZEN if k in refs and refs[k] != packet["refs"].get(k)]
    if changed:
        raise Refused("packet-frozen", "changes " + ", ".join(changed))
    credentials = body.get("credentials") or []
    if credentials and _slots(credentials) != _slots(packet["credentials"]):
        raise Refused("packet-frozen", "changes credentials")


def _shut(status: str, packet: dict | None, now: datetime) -> tuple[str, str] | None:
    """Why no key can be handed out under this packet whoever asks, as (code, detail), or None."""
    if status != "open":
        return "workflow-closed", ""
    refs = (packet or {}).get("refs") or {}
    missing = [k for k in REQUIRED_FOR_KEYS if k not in refs]
    if missing:
        return "packet-incomplete", "missing " + ", ".join(missing)
    deadline = _time(refs["deadline"])
    if deadline is None:
        return "packet-incomplete", "the deadline does not read as a UTC time"
    written = _time(packet.get("writtenAt"))
    if written is None:
        return "packet-incomplete", "the packet's write time does not read"
    if deadline > written + MAX_HORIZON:
        return "deadline-too-far", refs["deadline"]
    if now >= deadline:
        return "deadline-passed", refs["deadline"]
    return None


def _window(status: str, packet: dict | None, now: datetime) -> dict:
    credentials = (packet or {}).get("credentials") or []
    out: dict = {"slots": [_slot(c) for c in credentials]}
    deadline = ((packet or {}).get("refs") or {}).get("deadline")
    if deadline:
        out["until"] = deadline
    shut = _shut(status, packet, now)
    if shut:
        out.update(state="closed", reason=shut[0])
    elif not credentials:
        out.update(state="closed", reason="no-credentials")
    else:
        out["state"] = "open"
    return out


def view(packet: dict | None, status: str, *, now: datetime | None = None) -> dict | None:
    """What a person needs to follow the request, from the frozen `packet` (`packet_of`), or None for a workflow that
    carries none: the board refs, the run's end and whether keys can be handed out (and why not)."""
    if packet is None:
        return None
    refs = packet["refs"]
    out = {"request": f"board-request:{refs['board-request']}" if "board-request" in refs else None,
           "thread": f"board-thread:{refs['board-thread']}" if "board-thread" in refs else None,
           "capability": refs.get("capability"),
           "fabricProject": f"fabric-project:{refs['fabric-project']}" if "fabric-project" in refs else None,
           "deadline": refs.get("deadline"), "runEndsAt": packet.get("runEndsAt"),
           "keys": _window(status or "open", packet, now or datetime.now(timezone.utc))}
    return {k: v for k, v in out.items() if v is not None}


def keys_state(conn: sqlite3.Connection, workflow_id: str, *, now: datetime | None = None) -> dict:
    """Whether keys can be handed out for `workflow_id` now, and why not. Reads only."""
    wf = conn.execute("SELECT status FROM workflows WHERE workflow_id = ?", (workflow_id,)).fetchone()
    if wf is None:
        return {"state": "closed", "reason": "workflow-unknown"}
    return _window(wf["status"], packet_of(conn, workflow_id), now or datetime.now(timezone.utc))


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
    packet = packet_of(conn, workflow_id)
    shut = _shut(wf["status"], packet, now)
    if shut:
        raise Refused(*shut)
    refs = packet["refs"]
    active = conn.execute("SELECT token FROM workflow_leases WHERE workflow_id = ? AND state = 'active'",
                          (workflow_id,)).fetchone()
    if not lease or active is None or active["token"] is None or not hmac.compare_digest(active["token"], lease):
        raise Refused("not-lease-holder")
    credentials = packet["credentials"]
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


__all__ = ["REFS", "FROZEN", "MAX_HORIZON", "REQUIRED_FOR_KEYS", "REFUSALS", "Refused", "Invalid", "validate",
           "refs_of", "packet_of", "check_write", "view", "keys_state", "authorize"]
