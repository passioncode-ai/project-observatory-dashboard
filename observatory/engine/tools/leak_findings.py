#!/usr/bin/env python3
"""A value of ours, seen where it does not belong.

`tools/scan_leaks.py` measures; this decides what reaches the board. The split
matters here more than anywhere else on it: the scanner is allowed to be noisy —
a sighting costs a line — and `leaks.jsonl`, the operator's debt register, is
not. So nothing here writes that register. A hit leaves as a finding carrying the
exact `tools/vault.py leak` command, and a person confirms it.

TWO SEVERITIES, because a text match cannot tell them apart and a name can.
A Cloudflare account id is thirty-two hex characters — shape-identical to a
secret, and public by design. A `*_SECRET`, `*_TOKEN`, `*_KEY` or `*_PASSWORD`
is not. Both are reported; only the second is critical, and the row says why the
first is there at all rather than hiding it.
"""
from __future__ import annotations
import re
import pathlib as _pathlib
import sys as _sys
_DASHBOARD = str(_pathlib.Path(__file__).resolve().parents[1] / "dashboard")
if _DASHBOARD not in _sys.path:
    _sys.path.append(_DASHBOARD)  # `finding_types.titled`: a title is a message id
from finding_types import titled  # noqa: E402

LISTED = 6

#: Names that identify rather than authenticate. An account id, a project id, a
#: SID, a public key: holding one proves nothing, so seeing one proves little.
IDENTIFIER = re.compile(r"(_ID$|_SID$|ACCOUNT_ID|PROJECT_ID|CLIENT_ID|_PUBLIC|"
                        r"PUBLIC_|_URL$|_HOST$|_REGION$|_BUCKET$)", re.I)


def _where(path: str) -> str:
    """A place a reader can recognise, without the machine's full path."""
    if "/.claude/projects/" in path:
        rest = path.split("/.claude/projects/", 1)[1]
        return "session transcript " + rest
    if "/store/logs/" in path:
        return "this project's log " + path.split("/store/logs/", 1)[1]
    return path


def _name_of(secret: str) -> str:
    return secret.split("/")[-1]


def sightings(doc: dict | None) -> list[dict]:
    hits = (doc or {}).get("hits") or []
    if not hits:
        return []
    creds = [h for h in hits if not IDENTIFIER.search(_name_of(h["secret"]))]
    ids = [h for h in hits if IDENTIFIER.search(_name_of(h["secret"]))]
    out = []
    if creds:
        subjects = sorted({h["secret"] for h in creds})
        out.append({
            "type": "secret.seen_outside_its_home",
            "subject": "estate:leak-sightings",
            "severity": "critical",
            **titled("{n} credentials appear in files they do not live in", n=len(subjects)),
            "detail": ("Measured by searching for the values this estate actually "
                       "holds, not for anything key-shaped — so each of these is the "
                       "value itself, sitting somewhere else: "
                       + "; ".join(f"{h['secret']} in {_where(h['where'])} "
                                   f"×{h['occurrences']}" for h in creds[:LISTED])
                       + (f" and {len(creds) - LISTED} more sighting(s)"
                          if len(creds) > LISTED else "")
                       + ". A session transcript outlives the key it quotes, is "
                         "copied by every backup, and is read back by tools that "
                         "summarise it."),
            "action": ("confirm each, then `python \"$(project-observatory full-path)/tools/vault.py\" leak <project> <env> "
                       "<NAME> --where \"<the file>\"` to put it on the register; "
                       "rotating is the decision that clears it"),
        })
    if ids:
        subjects = sorted({h["secret"] for h in ids})
        out.append({
            "type": "secret.identifier_seen",
            "subject": "estate:leak-identifiers",
            "severity": "info",
            **titled("{n} account identifiers appear outside their files", n=len(subjects)),
            "detail": ("These matched the same search and are probably harmless: an "
                       "account id is thirty-two hex characters, which no shape test "
                       "can tell from a secret, and it is public by design. They are "
                       "listed rather than filtered because the judgement is the "
                       "operator's: "
                       + "; ".join(f"{h['secret']} in {_where(h['where'])}"
                                   for h in ids[:LISTED])
                       + (f" and {len(ids) - LISTED} more" if len(ids) > LISTED else "")
                       + "."),
            "action": ("if one of these really does authenticate, treat it as the "
                       "row above; otherwise nothing to do"),
        })
    return out


def suppression(doc: dict | None) -> list[dict]:
    """Sightings the operator accepted, and suppressions that stopped applying (PB-032)."""
    out = []
    gone = (doc or {}).get("suppressed") or []
    if gone:
        soonest = min(g["expires_on"] for g in gone)
        out.append({
            "type": "secret.sighting_suppressed", "subject": "estate:leak-suppressions",
            "severity": "info",
            **titled("{n} sightings suppressed by a recorded decision", n=len(gone)),
            "detail": ("Still seen, not reported as leaks, because a suppression with a reason "
                       "covers them: " + "; ".join(f"{g['secret']} in {_where(g['where'])} — {g['reason']}"
                                                  for g in gone[:LISTED])
                       + f". The first suppression expires on {soonest}."),
            "action": "renew a suppression only if its reason still holds; otherwise let it expire",
        })
    bad = (doc or {}).get("suppression_problems") or []
    if bad:
        out.append({
            "type": "secret.suppression_not_applied", "subject": "estate:leak-suppressions",
            "severity": "warning",
            **titled("{n} suppression rules not applied", n=len(bad)),
            "detail": "; ".join(f"rule {b.get('rule')}: {b['problem']}" for b in bad[:LISTED]),
            "action": "fix or remove the rule in config/leak_suppressions.json; an expired one "
                      "means its sightings are reported again",
        })
    return out


def unscanned(doc: dict | None) -> list[dict]:
    """What the scanner could not read. Absent is not clean."""
    notes = (doc or {}).get("not_scanned") or []
    if not notes:
        return []
    unreadable = [n for n in notes if n.get("unreadable")]
    return [{
        "type": "secret.leak_scan_blind",
        "subject": "estate:leak-scan-coverage",
        # A file that could not be opened is a hole in the measurement, not a
        # choice of window: warning, not info.
        "severity": "warning" if unreadable else "info",
        **(titled("the leak scan could not read {n} sources", n=len(unreadable)) if unreadable else
                  titled("the leak scan did not read {n} sources", n=len(notes))),
        "detail": ("A clean scan is only as wide as what it opened: "
                   + "; ".join(f"{n['what']} — {n['why']}" for n in notes[:LISTED])
                   + "."),
        "action": ("`python \"$(project-observatory full-path)/tools/scan_leaks.py\" --full --days 60` widens the window; a "
                   "source named here as unreadable needs its own reader"),
    }]


def findings(doc: dict | None) -> list[dict]:
    if not doc:
        return []
    if doc.get("degraded"):
        return [{
            "type": "secret.leak_scan_unmeasured",
            "subject": "estate:leak-scan-coverage",
            "severity": "warning",
            **titled("the leak scan had nothing to look for"),
            "detail": ("It ran and found nothing because it knows no values on this "
                       "machine — no env scan, no vault, no installed key. A clean "
                       "report from it means UNMEASURED, and the difference is the "
                       "whole point of saying so."),
            "action": "project-observatory full env, then project-observatory full leaks",
        }]
    return sightings(doc) + suppression(doc) + unscanned(doc)
