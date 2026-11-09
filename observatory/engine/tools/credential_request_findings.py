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


def findings(path: pathlib.Path, today: str) -> list[dict]:
    """The board's rows over one queue file. `today` is the build's day; a row's dates are the queue's own."""
    if not path.is_file():
        return []
    rows = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("schema") == cr.SCHEMA:
            rows.append(row)
    out = []
    for rid, entry in cr.fold(rows).items():
        request, slot = entry["request"], entry.get("slot") or "?"
        if entry["state"] == "open":
            failure = entry["attempts"][-1] if entry["attempts"] else None
            out.append({
                "type": "credential.request_waiting", "subject": f"credential:vault/{slot}", "severity": "warning",
                **titled("{agent} asks for a {provider} key for {slot}", agent=request["agentRef"],
                         provider=request["provider"], slot=slot),
                "detail": (f"Request {rid[:8]} ({request['preset']}, in {request['context']}, until "
                           f"{request['expiresAt'][:10]}) waits for the administrator's decision since "
                           f"{request['requestedAt'][:10]}: {request['reason']}"
                           + (f". The last issue failed: {failure.get('reason')}" if failure else "")),
                "action": (f"`{cr.COMMAND} issue {rid[:8]} [--account LABEL]` mints it through the door into the "
                           f"slot; `{cr.COMMAND} refuse {rid[:8]} --reason not-allowed` refuses it"),
            })
        elif entry["state"] in ("issued", "expired") and entry["attempts"] \
                and entry["attempts"][-1].get("stage") == "revoke":
            out.append({
                "type": "credential.revocation_refused", "subject": f"credential:vault/{slot}",
                "severity": "warning",
                **titled("Revoking {slot} at {provider} was refused", slot=slot, provider=request["provider"]),
                "detail": (f"Request {rid[:8]}'s credential still works at {request['provider']}: "
                           f"{entry['attempts'][-1].get('reason')}"),
                "action": f"`{cr.COMMAND} revoke {rid[:8]}` again once the door can reach the provider",
            })
        elif entry["state"] == "revoked" and (entry.get("revocation") or {}).get("proved") is False:
            out.append({
                "type": "credential.revocation_refused", "subject": f"credential:vault/{slot}",
                "severity": "warning",
                **titled("Revoking {slot} at {provider} was not proved", slot=slot, provider=request["provider"]),
                "detail": (f"Request {rid[:8]}'s credential was revoked through the door, and reading it back at "
                           f"{request['provider']} did not show it gone; it may still work there."),
                "action": (f"check the key at {request['provider']} with the door's `list` or `ping`, delete it there "
                           f"if it is still listed, then record it with `vault.py moved`"),
            })
    return out
