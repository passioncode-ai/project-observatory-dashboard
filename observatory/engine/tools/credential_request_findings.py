#!/usr/bin/env python3
"""What waits for the administrator in the credential request queue, on the board (OBS-191 parts 2 and 3).

`credential.request_waiting`     a request nobody decided yet (open, maybe marked pending, maybe after an
                                 issue attempt that failed, named in the detail): `warning`, since an agent
                                 waits for it.
`credential.revocation_refused`  an issued credential whose last revocation the provider refused, or one
                                 revoked whose removal at the provider was not proved: it may still work
                                 there, so it stays on the board until a revocation stands.

Read from `credential-requests.jsonl` beside the vault (`tools/credential_requests.py`), names only: the queue
holds no value. An absent queue gives no rows.
"""
from __future__ import annotations

import json
import pathlib
import sys as _sys

_HERE = pathlib.Path(__file__).resolve().parent
_DASHBOARD = str(_HERE.parent / "dashboard")
if _DASHBOARD not in _sys.path:
    _sys.path.append(_DASHBOARD)  # `finding_types.titled`: a title is a message id
if str(_HERE) not in _sys.path:
    _sys.path.append(str(_HERE))
from finding_types import titled  # noqa: E402
import credential_requests as cr  # noqa: E402

#: The queue's file name beside the vault, named here so the findings build cites it without spelling it
#: (that build is held to calling no network library, and is read for the text `requests.`).
QUEUE_FILE = "credential-requests.jsonl"


def findings(path: pathlib.Path, today: str) -> list[dict]:
    """The board's rows over one queue file. `today` is the build's day; a row's dates are the queue's own.
    Each row's subject carries the request's id after the slot, so two requests for one slot are two rows."""
    if not path.exists():
        return []
    try:
        rows, degraded = cr.parse_rows(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        rows, degraded = [], [{"reason": f"the file could not be read ({type(exc).__name__})"}]
    out = []
    if degraded:
        # HONEST DEGRADATION: a queue that cannot be read whole is said, never shown as an empty one.
        out.append({
            "type": "credential.request_queue_unreadable", "subject": "credential:request-queue",
            "severity": "warning",
            **titled("Credential request queue partly unreadable"),
            "detail": (f"{len(degraded)} line(s) of the credential request queue could not be read "
                       + (f"(line {degraded[0]['line']} first)" if degraded[0].get("line") else
                          f"({degraded[0]['reason']})")
                       + "; the requests on them are missing from this board, which is not the same as none."),
            "action": f"`{cr.COMMAND} list --state all` names the lines; nothing rewrites them",
        })
    for rid, entry in cr.fold(rows).items():
        try:
            out.extend(_rows(rid, entry))
        except (KeyError, TypeError, AttributeError):
            continue                                   # one damaged record never stops the board's build
    return out


def _rows(rid: str, entry: dict) -> list[dict]:
    request, slot = entry["request"], entry.get("slot") or "?"
    subject = f"credential:vault/{slot}#{rid[:8]}"
    out = []
    if entry["state"] == "open":
        failure = entry["attempts"][-1] if entry["attempts"] else None
        out.append({
            "type": "credential.request_waiting", "subject": subject, "severity": "warning",
            **titled("{agent} asks for a {provider} key for {slot}", agent=request["agentRef"],
                     provider=request["provider"], slot=slot),
            "detail": (f"Request {rid[:8]} ({request['preset']}, in {request['context']}, until "
                       f"{request['expiresAt'][:10]}) waits for the administrator's decision since "
                       f"{request['requestedAt'][:10]}: {request['reason']}"
                       + (f". An issue started at {entry['inFlight']} was interrupted; `issue` says what is left"
                          if entry.get("inFlight") else "")
                       + (f". The last issue failed: {failure.get('reason')}" if failure else "")),
            "action": (f"`{cr.COMMAND} issue {rid[:8]} [--account LABEL]` mints it through the door into the "
                       f"slot; `{cr.COMMAND} refuse {rid[:8]} --reason not-allowed` refuses it"),
        })
    revocation = entry.get("revocation") or {}
    attempts = entry["attempts"]
    if entry["state"] in ("issued", "expired") and attempts and attempts[-1].get("stage") == "revoke":
        out.append({
            "type": "credential.revocation_refused", "subject": subject, "severity": "warning",
            **titled("Revoking {slot} at {provider} was refused", slot=slot, provider=request["provider"]),
            "detail": f"Request {rid[:8]}'s credential still works at {request['provider']}: {attempts[-1].get('reason')}",
            "action": f"`{cr.COMMAND} revoke {rid[:8]}` again once the door can reach the provider",
        })
    elif entry["state"] == "revoked" and revocation.get("proved") is False:
        out.append({
            "type": "credential.revocation_refused", "subject": subject, "severity": "warning",
            **titled("Revoking {slot} at {provider} was not proved", slot=slot, provider=request["provider"]),
            "detail": (f"Request {rid[:8]}'s credential was not shown gone at {request['provider']}"
                       + (f" ({revocation['why']})" if revocation.get("why") else "") + "; it may still work there."),
            "action": (f"`{cr.COMMAND} revoke {rid[:8]}` again, or check it at {request['provider']} (the door's "
                       f"`list`), delete it there if it is still listed, then `{cr.COMMAND} revoke {rid[:8]} "
                       f"--settle \"how it was checked\"`"),
        })
    if entry.get("undoUnsettled"):
        undo = next((a for a in reversed(attempts) if a.get("stage") == "undo"), {})
        out.append({
            "type": "credential.revocation_refused", "subject": subject + "#undo", "severity": "warning",
            **titled("Revoking {slot} at {provider} was refused", slot=slot, provider=request["provider"]),
            "detail": (f"A credential minted for request {rid[:8]} did not meet it and could not be revoked again: "
                       f"{undo.get('reason')}"),
            "action": (f"delete it at {request['provider']} through the door (its `list` names it), then "
                       f"`{cr.COMMAND} revoke {rid[:8]} --settle \"how it was checked\"`"),
        })
    return out
