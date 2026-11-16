#!/usr/bin/env python3
"""What the key sweep owes the operator (OBS-554, CP-08, `tools/key_sweep.py`).

Reads `store/raw/key-sweep.json`. Silent until the sweep has run once and while it is switched off: no
answer is not a clean bill, and `observatory_key_sweep` says so in `degraded`.

- `secret.in_repository`: one row per open sighting (one value, however many places). Critical when the
  value is one a vault slot or an env file still holds (it works, and it is published); warning otherwise
  (a key-shaped match, or a value whose slot was rotated but is still in a working tree). The row names the
  slot when it is known, the places (repository, path, line, commit) and never the value, the matched text
  or the line. It closes by itself when the value is gone from every working tree and its slot is rotated;
  an unknown value closes when it is gone and a person settled it.

The subject is `secret:<folder>/key-sweep/<digest prefix>`, so `survey.finding_matcher` gives it to the
project whose folder holds the repository.
"""
from __future__ import annotations
import pathlib as _pathlib
import sys as _sys
_DASHBOARD = str(_pathlib.Path(__file__).resolve().parents[1] / "dashboard")
if _DASHBOARD not in _sys.path:
    _sys.path.append(_DASHBOARD)  # `finding_types.titled`: a title is a message id
from finding_types import titled  # noqa: E402

import json
import pathlib

EVIDENCE = ["store/raw/key-sweep.json"]
TYPES = ("secret.in_repository",)
LISTED = 6


def _place(p: dict) -> str:
    where = f"{p.get('repo')}/{p.get('path')}" + (f":{p['line']}" if p.get("line") else "")
    return where + (f" (commit {str(p['commit'])[:12]})" if p.get("commit") else " (working tree)")


def findings(doc: dict | None) -> list[dict]:
    if not isinstance(doc, dict) or doc.get("enabled") is False:
        return []
    out: list[dict] = []
    for s in doc.get("sightings") or []:
        if not isinstance(s, dict) or s.get("status") != "open":
            continue
        places = [p for p in s.get("places") or [] if isinstance(p, dict)]
        if not places:
            continue
        repo = places[0].get("repo") or "?"
        folder = pathlib.PurePosixPath(str(repo)).name
        fp = str(s.get("fingerprint") or "")
        slot = s.get("slot")
        state = s.get("slot_state")
        in_tree = bool(s.get("in_tree"))
        severity = "critical" if slot and state == "current" else "warning"
        rules = ", ".join(s.get("rules") or [])
        shown = "; ".join(_place(p) for p in places[:LISTED]) + (
            f" and {len(places) - LISTED} more place(s)" if len(places) > LISTED else "")
        state_text = {"current": f"{slot} still holds this value, so it works for whoever reads the repository",
                      "rotated": f"{slot} has been rotated since; this value no longer works there",
                      "gone": f"{slot} no longer exists on this machine; whether this value still works is not known",
                      None: "no slot on this machine holds it, so it is matched by its shape alone"}.get(state, "")
        detail = (f"Found by {rules} in {shown}. {state_text}. "
                  + ("It is in a working tree now. " if in_tree else "It is gone from every working tree, but the "
                     "history still holds it. ")
                  + "Only a keyed digest of the value is kept; the value and its line are not.")
        settle = f"`project-observatory full key-sweep --settle {fp} --why TEXT`"
        if slot and state == "current":
            action = (f"rotate {slot} at its issuer and in the vault (`vault.py rotate`, or the door that minted it), "
                      "then remove it from the working tree and commit; history keeps the old value, so the rotation "
                      "is what makes it harmless, and this row closes by itself")
        elif in_tree:
            action = ("remove it from the working tree and commit; revoke it where it was issued if it is a real key"
                      + (", and the row closes at the next sweep" if state == "rotated" else ", then settle it: " + settle))
        else:
            action = "revoke it where it was issued if it is a real key, then settle it: " + settle
        out.append({"type": "secret.in_repository", "subject": f"secret:{folder}/key-sweep/{fp[12:28]}",
                    "severity": severity,
                    **(titled("A value of {slot} is in the repository {repo}", slot=slot, repo=repo) if slot else
                       titled("A key-shaped value is in the repository {repo}", repo=repo)),
                    "detail": detail, "action": action, "evidence": EVIDENCE,
                    "project": next((r.get("project") for r in doc.get("repositories") or []
                                     if r.get("repo") == repo), None),
                    "fingerprint": fp, "slot": slot, "slot_state": state, "in_tree": in_tree,
                    "rules": list(s.get("rules") or []), "places": places[:LISTED]})
    return out


def from_file(path: pathlib.Path) -> list[dict]:
    try:
        return findings(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []
