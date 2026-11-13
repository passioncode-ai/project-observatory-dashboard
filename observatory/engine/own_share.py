"""A peer share between one person's own two machines, finished by their agent (OBS-531, contract CT-298).

The recipient's "Accept" and the sender's comparison of the codes are the person's acts (DEC-0142, DEC-0147 PS-6).
When the person holds both machines and asked their agent to finish (the operator, 2026-11-12: «давай через SSH все
это делай, короче сам»), the contract admits three relayed acts of their own (OM-2), each under every rule of OM-1:

- `share.accept-own` (`full share accept ID --agent NAME --decision JSON`): the recipient's "Accept";
- `share.read-code` (`full share read-code ID --agent NAME --decision JSON`): the recipient's six digits, read on the
  recipient's machine after the exchange completed and before a confirm, abort or seal (OM-3);
- `share.confirm-own` (`full share confirm ID --agent NAME --decision JSON`, the digits on standard input): the
  sender's node compares them with its own code in constant time; equal seals, different is "They differ" (OM-4).

OM-1, all of which must hold, else the act stays the person's and the answer is `human-step-required` with today's
step (OM-2), never `decision-invalid`:

1. the node is in the personal context: enrolled in no organization; and on the sender's side the share's vault
   project belongs to no organization other than the default one;
2. the caller is local to this node (T0): a command run on this machine, never the relay, the hub or the mailbox;
3. the operator's decision (DEC-0121 AP-2) names this act and this share id as its `target`;
4. on the recipient's side, the link is pinned: it was in the operator's words unchanged (PS-4).

Each admitted act is journalled (OM-5) in the node's `shares/own-decisions.jsonl`, owner-only, with the share id, the
act, who relayed it and the decision; never the digits. "Share without comparing" is never relayed.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Optional

PROG = "project-observatory full" + " share"
JOURNAL = "own-decisions.jsonl"


class Outside(Exception):
    """The act is outside OM-1: it stays the person's, with the step they take."""

    def __init__(self, action: dict, step: str, why: str):
        super().__init__(why)
        self.answer = {"error": "human-step-required", "humanStep": action["humanStep"], "step": step, "why": why}


def now_stamp(clock=None) -> str:
    moment = clock() if clock else datetime.datetime.now(datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def personal_node(node: Path) -> bool:
    """Whether this node is in the personal context: enrolled in no organization (OM-1.1)."""
    from node import org
    return not org.enrolled_orgs(node)


def project_is_personal(project: str, registry: Optional[Path] = None) -> bool:
    """Whether a vault project belongs to no organization but the default one. A project the registry does not know
    is the person's own vault folder, so personal; a decided organization other than the default is not."""
    import paths
    try:
        doc = json.loads(((registry or paths.REGISTRY) / "projects.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    try:
        import organizations
        default = organizations.Assigner().default
    except Exception:  # noqa: BLE001 - no organizations configured: nothing is an organization's
        default = None
    for row in doc.get("projects") or []:
        names = {row.get("id", "").split(":", 1)[-1], *[Path(f).name for f in row.get("local_folders") or []]}
        if project in names:
            org = row.get("organization")
            if row.get("organization_source") in ("declared", "rule") and org and org != default:
                return False
    return True


def gate(own: dict, human: dict, *, agent: Any, decision: Any, share_id: str, node: Path, step: str,
         pinned: Optional[bool] = None, project: Optional[str] = None, clock=None) -> dict:
    """OM-1 for one act. Returns the record the act is journalled as; raises `Outside` with the person's step."""
    from node import actions
    if not isinstance(agent, str) or not agent.strip():
        raise Outside(human, step, "no agent named: the act is the person's")
    if not personal_node(node):
        raise Outside(human, step, "this machine is enrolled in an organization: its gates stay the person's (OM-1.1)")
    if project is not None and not project_is_personal(project):
        raise Outside(human, step, f"the vault project {project} belongs to an organization (OM-1.1)")
    if pinned is False:
        raise Outside(human, step, "the link was not in the operator's words unchanged, so it is not pinned (OM-1.4)")
    answer = actions.action_decision(action=own, tier="T0", caller="agent:" + agent.strip(), decision=decision,
                                     target=share_id, now=now_stamp(clock))
    if answer.get("ok") is not True:
        raise Outside(human, step, f"the operator's decision for {own['id']} on this share is missing or invalid "
                                   f"({answer.get('code') or answer.get('fault') or 'unreadable'}) (OM-1.3)")
    return answer["recordAs"]


def journal(node: Path, *, act: str, share_id: str, record_as: Any, decision: Any, outcome: str, clock=None) -> None:
    """OM-5: the act, the share, who relayed it and the decision; never the digits."""
    import private_io
    path = node / "shares" / JOURNAL
    with private_io.lock(node / "shares" / (JOURNAL + ".lock")):
        try:
            text = private_io.read(path)
        except FileNotFoundError:
            text = ""
        row = {"at": now_stamp(clock), "act": act, "shareId": share_id, "outcome": outcome, "recordAs": record_as,
               "decision": decision}
        private_io.write(path, text + json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def parse_decision(text: Optional[str]) -> Any:
    """The decision as the agent passed it: JSON, or None when absent or unreadable (both are outside OM-1.3)."""
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None
