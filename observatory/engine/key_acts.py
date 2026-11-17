"""The key page's acts for an agent (OBS-559 part 2, CP-18): the MCP tools `observatory_key_act` and
`observatory_credential_mark`.

The key server (`tools/keyserver.py`) serves eight acts to the person at the page: mint, limit, disable, enable,
rotate-key and revoke of an OpenRouter key, marking a vault slot leaked and signing a credential. An agent reaches the
SAME functions here, in process: `keyserver.ACTIONS[name](body)`, so every check, every journal line and every door
call is the page's. Nothing below mints, caps, revokes, marks or signs by itself.

What this module adds is what stdio cannot prove. The key server takes an agent's call over HTTP only with a Fabric
delegation token for the act's scope (`keyserver.ACTION_SCOPES`); a stdio MCP server has no caller identity to check.
So an act that spends money or cannot be undone (`mint`; `limit` above the ceiling the ledger records, or one it
cannot compare; `rotate`, which mints a successor and revokes the old key; `revoke`) needs the operator's words in
contract DEC-0121's shape (`decision`), checked as `node/actions.py` checks them, and the key server's journal row
keeps that decision beside the caller `agent:mcp`. Disable and enable are reversible at the provider; a leak mark and
a signature are drafts of a record (`ACTION_SCOPES` effect `draft`): none of them needs the words.

A key value never crosses: the door writes a minted value to its destination, and nothing here reads one. Every
string argument shaped like a credential is refused by type and never echoed, and every refusal is scrubbed.
Design: docs/design/control-plane/key-tools.md.
"""
from __future__ import annotations

import datetime
import math
import pathlib
import sys
import urllib.error
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import credential_shape  # noqa: E402

#: The tool's action and the key server's act it runs.
KEY_ACTS = {"mint": "mint", "limit": "limit", "disable": "disable", "enable": "enable", "rotate": "rotate-key",
            "revoke": "revoke"}
RECORD_ACTS = {"leak": "leak", "annotate": "annotate"}
#: The decision's `action` per gated act: the scope the key server names for it (`keyserver.ACTION_SCOPES`).
DECISION_ACTIONS = {"mint": ("keys.mint", "Mint an OpenRouter key"),
                    "limit": ("keys.limit", "Raise an OpenRouter key's spending ceiling"),
                    "rotate": ("keys.rotate", "Rotate an OpenRouter key (mint its successor, revoke it)"),
                    "revoke": ("keys.revoke", "Revoke an OpenRouter key")}
#: Which arguments each action takes; any other non-empty argument is refused rather than dropped.
KEY_ARGS = {"mint": {"destination", "name", "limit", "limitReset", "account", "project"},
            "limit": {"name", "limit", "limitReset"},
            "disable": {"name"}, "enable": {"name"}, "rotate": {"name"}, "revoke": {"name"}}
RECORD_ARGS = {"leak": {"project", "env", "name", "where"},
               "annotate": {"id", "purpose", "owner", "evidence", "rotationDays", "tags"}}
CALLER = "agent:mcp"


class Refusal(ValueError):
    """An act not taken, by type. Its message never carries the caller's refused value."""

    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.extra = extra

    def answer(self) -> dict:
        return {"error": {"code": self.code, "message": credential_shape.redact(str(self)), **self.extra},
                "degraded": []}


def _keyserver():
    sys.path.insert(0, str(ROOT / "tools"))
    import keyserver
    return keyserver


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _check_arguments(action: str, taken: dict[str, set], given: dict[str, Any]) -> None:
    if action not in taken:
        raise Refusal("invalid-argument", f"action is one of {', '.join(taken)}")
    stray = sorted(k for k, v in given.items() if v not in (None, "", [], {}) and k not in taken[action])
    if stray:
        raise Refusal("invalid-argument", f"`{stray[0]}` belongs to another action, not to {action}")
    for key, value in given.items():
        values = value if isinstance(value, list) else [value]
        if any(isinstance(v, str) and credential_shape.shaped(v, uuids=False) for v in values):
            raise Refusal("invalid-argument", f"`{key}` is shaped like a credential; it is refused, not echoed. "
                                              "A key value never travels through this tool")


def _decided(action: str, target: str, decision: Any) -> dict:
    """The operator's words for this act and object (DEC-0121 AP-2, checked by `node/actions.py`), or a refusal."""
    act, title = DECISION_ACTIONS[action]
    return decided(act, title, "observatory.key_act", target, decision, label=action)


def decided(act: str, title: str, tool: str, target: str, decision: Any, *, label: str,
            why: str = "spends money or cannot be undone") -> dict:
    """The operator's words for the act `act` on `target`, checked by the engine's one decision check
    (`node/actions.decided`, OBS-611) as every relayed act is; the record the journal keeps, or a refusal. Shared with
    `ops_acts.py` (OBS-559, the operations verbs)."""
    import engine_node
    actions = engine_node.module("actions")
    answer = actions.decided(
        action={"id": act, "title": title, "tool": tool, "gate": "relayed"}, target=target, caller=CALLER,
        decision=decision, now=_now())
    if answer.get("ok") is True:
        return answer["record"]
    reason = answer.get("why")
    if reason == "absent":
        raise Refusal(answer["code"],
                      f"{label} {why}: ask the person, then call again with their words "
                      f"as `decision` (by operator, via relayed-instruction, action {act}, target {target}, the words "
                      "verbatim, when they said them)", action=act, target=target)
    raise Refusal(answer["code"], actions.REASONS.get(reason, actions.REASONS["contract"]), action=act,
                  **({"target": target} if reason in ("contract", "target") else {}))


def _raises_ceiling(ks, name: str, limit: float) -> bool:
    """True unless the ledger records a ceiling for `name` at or above `limit`: a ceiling it cannot compare (a
    provider label, an unknown name, an unreadable ledger) is read as a raise."""
    try:
        rec = (ks._door().ledger().get("issued") or {}).get(name) or {}
    except Exception:                                              # noqa: BLE001  any unreadable ledger is a raise
        return True
    before = rec.get("limit_usd")
    return not (isinstance(before, (int, float)) and math.isfinite(before) and limit <= before)


def _run(ks_action: str, body: dict, record: dict | None) -> dict:
    """The key server's own act, with this caller and the relayed decision in its journal row; its exceptions as the
    key server's HTTP route maps them, by type."""
    ks = _keyserver()
    caller, relayed = ks._CALLER.set(CALLER), ks._RELAYED.set(record)
    try:
        return ks.ACTIONS[ks_action](body)
    except Exception as exc:                                       # noqa: BLE001  typed by the route's own mapping
        # The code is the key server's (`keyserver.refusal`, OBS-601), so the page's route and this tool name one
        # refusal the same way: the consumer guard's own code (OBS-594), invalid-argument, held, provider-refused,
        # not-found, unconfirmed, failed.
        _status, code = ks.refusal(exc)
        if isinstance(exc, ks.key_consumers.RevokeRefused):
            raise Refusal(code, str(exc)[:500], consumers=exc.consumers, unread=exc.unread) from None
        if isinstance(exc, urllib.error.HTTPError):
            raise Refusal(code, f"the provider refused: HTTP {exc.code}") from None
        if isinstance(exc, (ValueError, LookupError, RuntimeError)):
            raise Refusal(code, str(exc)[:500]) from None
        raise Refusal(code, f"the act failed ({type(exc).__name__}); inspect the local service "
                            "configuration") from None
    finally:
        ks._RELAYED.reset(relayed)
        ks._CALLER.reset(caller)


def key_act(action: str, *, name: str | None = None, destination: str | None = None, limit: float | None = None,
            limitReset: str | None = None, account: str | None = None, project: str | None = None,
            decision: Any = None) -> dict:
    """One act on an OpenRouter key, the key page's button: mint, limit, disable, enable, rotate or revoke."""
    given = {"name": name, "destination": destination, "limit": limit, "limitReset": limitReset,
             "account": account, "project": project}
    try:
        _check_arguments(action, KEY_ARGS, given)
        ks = _keyserver()
        record = None
        if action == "mint":
            try:
                target = ks.check_destination(destination)
            except ValueError as exc:
                raise Refusal("invalid-argument", str(exc)[:500]) from None
            if limit is None or not isinstance(limit, (int, float)) or not math.isfinite(limit) or limit <= 0:
                raise Refusal("invalid-argument", "a mint without a limit is a key with no ceiling; give one")
            record = _decided(action, target, decision)
            body = {"destination": target, "name": name, "limit": limit, "limit_reset": limitReset,
                    "account": account, "project": project}
        else:
            if not isinstance(name, str) or not name.strip():
                raise Refusal("invalid-argument", "name is required: the key's name in the ledger, or its label")
            name = name.strip()
            body = {"name": name}
            if action == "limit":
                if limit is None or not isinstance(limit, (int, float)) or not math.isfinite(limit) or limit <= 0:
                    raise Refusal("invalid-argument", "a positive limit is required")
                body.update({"limit": limit, "limit_reset": limitReset})
                if _raises_ceiling(ks, name, float(limit)):
                    record = _decided(action, name, decision)
            elif action in DECISION_ACTIONS:
                record = _decided(action, name, decision)
        out = {"action": action, **_run(KEY_ACTS[action], {k: v for k, v in body.items() if v is not None}, record)}
        if record:
            out["recordedAs"] = {"by": record["by"], "via": record["via"], "caller": CALLER}
        return out
    except Refusal as refusal:
        return refusal.answer()


def credential_mark(action: str, *, project: str | None = None, env: str | None = None, name: str | None = None,
                    where: str | None = None, id: str | None = None, purpose: str | None = None,
                    owner: str | None = None, evidence: str | None = None, rotationDays: int | None = None,
                    tags: list[str] | None = None) -> dict:
    """Mark a vault slot leaked (`leak`), or sign a credential with its purpose and evidence (`annotate`)."""
    given = {"project": project, "env": env, "name": name, "where": where, "id": id, "purpose": purpose,
             "owner": owner, "evidence": evidence, "rotationDays": rotationDays, "tags": tags}
    try:
        _check_arguments(action, RECORD_ARGS, given)
        if action == "leak":
            body = {"project": project, "env": env, "name": name, "where": where}
        else:
            body = {"id": id, "purpose": purpose, "evidence": evidence, "owner": owner,
                    "rotation_days": rotationDays, "tags": tags}
            body = {k: v for k, v in body.items() if v is not None}
        return {"action": action, **_run(RECORD_ACTS[action], body, None)}
    except Refusal as refusal:
        return refusal.answer()
