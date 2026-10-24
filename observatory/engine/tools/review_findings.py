#!/usr/bin/env python3
"""What the review queue owes a person, so it is never forgotten (OBS-227, docs/design/REVIEW-POLICY.md).

Reads `review_policy.summary()`: the items that wait for a decision only a person can make, after the
standing policy took what it may. One row, `review.queue_waiting`, a warning, when their number
reaches the policy's `remind.count` or the oldest is `remind.age_days` old; silent below both, and
silent on an empty queue. It names the three commands that resolve the queue in one launch.

`ledger.review_backlog` and `ledger.review_expiring` stay: they count every proposed conclusion and
what retention is about to erase. This row counts what needs a PERSON across both queues, the
registry proposals included, which had no finding at all.

NO CLOCK IN THE TEXT beyond the oldest item's own date: the same queue gives the same row.
"""
from __future__ import annotations

import pathlib as _pathlib
import sys as _sys

_DASHBOARD = str(_pathlib.Path(__file__).resolve().parents[1] / "dashboard")
if _DASHBOARD not in _sys.path:
    _sys.path.append(_DASHBOARD)  # `finding_types.titled`: a title is a message id
from finding_types import titled  # noqa: E402

TYPES = ("review.queue_waiting",)
CMD = "project-observatory full review"
STATE_WORDS = {
    "on": "The standing review policy is on and decides what it may every tick; these are what it leaves",
    "off": f"The standing review policy is off: `{CMD} policy show` lists what its rules would decide, and "
           f"`{CMD} policy enable` at a terminal turns it on",
    "changed": "The standing review policy's rules changed after it was enabled, so it decides nothing until "
               f"`{CMD} policy enable` is run again at a terminal",
    "invalid": "The standing review policy file is refused, so it decides nothing",
}


def findings(summary: dict | None) -> list[dict]:
    if not isinstance(summary, dict):
        return []
    waiting = summary.get("waiting") or {}
    total = int(waiting.get("total") or 0)
    if total <= 0:
        return []
    import review_policy
    remind = {**review_policy.REMIND_DEFAULT, **(summary.get("remind") or {})}
    age = review_policy.age_days(waiting.get("oldest"))
    if total < remind["count"] and (age is None or age < remind["age_days"]):
        return []
    state = (summary.get("policy") or {}).get("state") or "off"
    oldest = str(waiting.get("oldest") or "")[:10]
    return [{
        "type": "review.queue_waiting", "subject": "review", "severity": "warning",
        **titled("{n} proposals wait for a person", n=total),
        "detail": (f"{int(waiting.get('registry') or 0)} registry proposal(s) and "
                   f"{int(waiting.get('ledger') or 0)} ledger conclusion(s) wait for a decision only a person "
                   f"can make" + (f"; the oldest since {oldest}" if oldest else "") + ". "
                   + STATE_WORDS.get(state, STATE_WORDS["off"]) + ". This row appears at "
                   f"{remind['count']} waiting or once the oldest is {remind['age_days']} days old "
                   f"(`remind` in config/review-policy.json)."),
        "action": (f"{CMD} policy show; then {CMD} batch prepare, which writes every waiting item with a "
                   f"recommendation an agent may edit, and at a terminal {CMD} batch apply FILE, which asks "
                   "once and applies them all"),
        "evidence": ["store:proposals", "store:ledger", "store/raw/review-policy.json"]}]
