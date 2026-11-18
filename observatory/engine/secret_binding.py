"""Secret access bound to an agent's project (OBS-635).

An agent that runs `use_secret run` under an access binding (access-bindings/1) gets only the slots of the binding's
projects. The connection that starts the agent names the owner-only token file the operator's
`full access-binding issue ... --scope secrets.use` wrote, in `OBSERVATORY_BINDING_TOKEN_FILE`; the bearer is read
from that file at each run, never from an argument.

    check("alpha-web")  -> Bound(binding_id, project_id)     the slot's project is the binding's
    check("beta-api")   -> raises Refused("project-not-bound")
    check(...) with no token file -> None                     unbound: the caller journals it as such

A binding that cannot be read, is revoked or expired, lacks the `secrets.use` scope, or names a project the slot's
folder does not resolve to is a refusal, never a quiet fallback to the unbound path: an agent that was given a binding
and loses it gets nothing.

Not a sandbox. Agents that run as the same OS account can read each other's token files and the vault itself; the
binding narrows the accident and the journal names it. Separation between organizations that must hold against a
hostile agent is a separate OS account or space (OBS-200).
"""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass

SCOPE = "secrets.use"
TOKEN_FILE_ENV = "OBSERVATORY_BINDING_TOKEN_FILE"
TOKEN_FILE_MAX = 4096


class Refused(Exception):
    """A release the binding refused: `code` is the refusal code the audit row and the message carry."""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code


@dataclass(frozen=True)
class Bound:
    binding_id: str
    project_id: str


def _token_file_check(fd: int) -> None:
    import osprivacy
    if not osprivacy.private(fd):
        raise ValueError("readable by others")
    if not osprivacy.owned_by_me(fd):
        raise ValueError("owned by another account")


def _bearer(named: str) -> str:
    import osprivacy
    path = pathlib.Path(named)
    if not path.is_absolute():
        raise Refused("binding-unreadable", "the token file is not an absolute path")
    try:
        raw = osprivacy.read_regular(path, TOKEN_FILE_MAX, check=_token_file_check)
        bearer = raw.decode("utf-8").strip()
    except FileNotFoundError:
        raise Refused("binding-unreadable", "the token file does not exist") from None
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, UnicodeDecodeError) \
            else type(exc).__name__
        raise Refused("binding-unreadable", f"the token file cannot be read: {reason}") from None
    if not bearer or any(c.isspace() for c in bearer):
        raise Refused("binding-unreadable", "the token file holds no single bearer")
    return bearer


def check(project: str, environ=None, *, registry=None, project_of=None) -> Bound | None:
    """The binding this run is under and the project it releases, or None when the run names no binding.

    `project` is the folder or id `use_secret run` was given. `registry` and `project_of` (folder -> registry project
    id, or None) are for tests; omitted, the workspace's own are read."""
    env = os.environ if environ is None else environ
    named = env.get(TOKEN_FILE_ENV, "")
    if not named:
        return None
    import access_binding as AB
    import memory_access as MA
    digest = AB.bearer_digest(_bearer(named))
    try:
        reg = MA.registry() if registry is None else registry
    except AB.BindingError as exc:
        raise Refused("registry-unreadable", str(exc)) from None
    found = AB.resolve(reg, {"channel": "http", "audience": MA.own_audience(), "bearerDigest": digest},
                       now=MA._now())
    if found.binding is None:
        raise Refused("binding-refused", found.reason)
    binding = found.binding
    if project_of is None:
        import vault_project

        def project_of(text: str) -> str | None:
            r = vault_project.resolve(text)
            return r.project_id if r.how not in ("ambiguous", "unreadable") else None
    pid = project_of(project)
    if pid is None:
        raise Refused("project-unknown", f"{project!r} is not one registry project, so no binding can release it")
    decision = AB.authorize(binding, {"scope": SCOPE, "effect": "read", "projectId": pid})
    if not decision.allow:
        raise Refused(decision.reason, f"{binding.binding_id}: {decision.reason} for {pid}")
    return Bound(binding.binding_id, pid)
