#!/usr/bin/env python3
"""What the credential inventory owes the operator, on the board rather than a tab.

A TAB ANSWERS A QUESTION SOMEBODY ASKED. These arrive at somebody who did not:
a key whose cap is a lifetime rather than a month dies one day for no visible
reason, and an account nothing claims is one nobody will dare rotate.

`secret.leaked_unrotated` is NOT here: `tools/build_findings.py` already raises
it from the leak register, and two rules over one subject is how a board gets
two rows that mean one thing. This file adds what the register cannot see,
because it reads the register's neighbours instead:

`credential.lifetime_cap`   a limit with no reset. The key works until the
                            total is reached and then stops, long after anyone
                            remembers setting it.
`credential.unclaimed`      nothing says which projects use it. Aggregated:
                            the remedy is one curation pass, not one decision
                            per row.
`credential.untracked`      known only because it leaked: the estate has no
                            record of the credential itself, so rotating it
                            cannot be verified afterwards.
`credential.shared_rotation` one credential, several projects. Not a defect but
                            a fact with a consequence, and the consequence is
                            that rotating it breaks every project at once
                            unless they are deployed together.

NO TIMESTAMP INSIDE A FINDING: every date quoted is a date the registry
already holds, so the same inputs always produce the same rows.
"""
from __future__ import annotations
import pathlib as _pathlib
import sys as _sys
_DASHBOARD = str(_pathlib.Path(__file__).resolve().parents[1] / "dashboard")
if _DASHBOARD not in _sys.path:
    _sys.path.append(_DASHBOARD)  # `finding_types.titled`: a title is a message id
from finding_types import titled  # noqa: E402


def _config_label(name: str) -> str:
    """The workspace-relative name of a configuration file.

    Imported late: these rule modules are loaded by `tools/build_findings.py`,
    which has already resolved the workspace, and importing `paths` at module
    load would resolve it again for every test that imports a rule alone."""
    import paths
    return paths.config_label(name)

#: Listed by name up to this many, as everywhere else on this board.
LISTED = 6


def _listed(names: list[str]) -> str:
    shown = sorted(names)[:LISTED]
    rest = len(names) - len(shown)
    return ", ".join(shown) + (f" and {rest} more" if rest > 0 else "")


def _short(cred: dict) -> str:
    return cred.get("name") or cred["id"].split("/")[-1]


def lifetime_cap(creds: list[dict]) -> list[dict]:
    """A cap with no reset — one row each, because each is one edit to fix."""
    out = []
    for c in creds:
        if c.get("limit") in (None, 0) or c.get("limit_reset"):
            continue
        out.append({
            "type": "credential.lifetime_cap",
            "subject": c["id"],
            "severity": "warning",
            **titled("{key} is capped for its lifetime, not per month", key=_short(c)),
            "detail": (f"Its limit is {c['limit']} with no reset, so it spends down "
                       f"once and then stops — months later, and with nothing on the "
                       f"day it happens to say why. A monthly budget behaves the same "
                       f"until it does not."
                       + (f" {c['usage']} of it is already spent."
                          if c.get("usage") else "")),
            "action": ("set `limit_reset` to monthly on the provider, or say out loud "
                       "that a lifetime cap is what this key is for"),
        })
    return out


def unclaimed(creds: list[dict]) -> list[dict]:
    """Credentials no project claims. ONE row: the remedy is a curation pass."""
    rows = [c for c in creds if not (c.get("used_by") or [])]
    if not rows:
        return []
    return [{
        "type": "credential.unclaimed",
        "subject": "estate:credentials-unclaimed",
        "severity": "info",
        **titled("{n} credentials belong to no project here", n=len(rows)),
        "detail": (f"Nothing measured says who uses them and nothing curated claims "
                   f"them: {_listed([_short(c) for c in rows])}. An unclaimed "
                   f"credential is one nobody will dare rotate, because the blast "
                   f"radius is unknown — which is the state a shared account drifts "
                   f"into on its own."),
        "action": (f"add a row to {_config_label('credential_owners.json')} with the evidence "
                   "that proves who uses it; a row without evidence is refused"),
    }]


def untracked(creds: list[dict]) -> list[dict]:
    """Known only from a leak — the worst shape, and one row each."""
    out = []
    for c in creds:
        if c.get("kind") != "leaked-untracked":
            continue
        out.append({
            "type": "credential.untracked",
            "subject": c["id"],
            "severity": "warning",
            **titled("{key} is known only because it leaked", key=_short(c)),
            "detail": (f"It was recorded where it was SEEN"
                       + (f" on {c['leaked_on']}" if c.get("leaked_on") else "")
                       + ", and the estate holds no record of the credential itself — "
                         "so after a rotation nothing here can tell whether the value "
                         "in service is the new one."),
            "action": (f"tools/vault.py put {c.get('vault_project') or '<project>'} "
                       f"{c.get('env') or '<env>'} {_short(c)} — value on stdin — then "
                       f"rotate it at its issuer"),
        })
    return out


def shared_rotation(creds: list[dict]) -> list[dict]:
    """One credential, several projects: a fact whose consequence is a decision."""
    rows = [c for c in creds if len(c.get("used_by") or []) > 1]
    if not rows:
        return []
    return [{
        "type": "credential.shared_rotation",
        "subject": "estate:credentials-shared",
        "severity": "info",
        **titled("{n} credentials are used by more than one project", n=len(rows)),
        "detail": ("Rotating one of these changes what every project sharing it must "
                   "hold, at the same moment: "
                   + "; ".join(f"{_short(c)} → {len(c['used_by'])} projects"
                               for c in rows[:LISTED])
                   + ". That is not a defect — it is the reason the edge is recorded "
                     "at all, and the reason a rotation needs a plan rather than a "
                     "command."),
        "action": ("before rotating, list the projects on the Keys page and deploy "
                   "them together, or split the account so each holds its own"),
    }]


def unsigned(creds: list[dict]) -> list[dict]:
    """A credential nobody has said what it is FOR.

    ONE AGGREGATE ROW, because the remedy is one sentence for every one of them
    and a row per credential would repeat it. What the row carries instead is
    the count and the worst class: a credential with no purpose cannot be
    retired, cannot be delegated, and cannot be judged when it leaks. "Is this
    still needed" has no answer, so it is kept forever.
    """
    bare = sorted(c["id"] for c in creds if not (c.get("signature") or {}).get("purpose"))
    if not bare:
        return []
    return [{
        "type": "credential.unsigned",
        "subject": "estate:credentials",
        "severity": "info",
        **titled("{n} credentials carry no statement of what they are for", n=len(bare)),
        "detail": (f"{_listed([b.split(':', 1)[-1] for b in bare])}. A credential "
                   f"with no purpose cannot be retired, delegated or judged when "
                   f"it leaks — «is this still needed» has no answer, so it is "
                   f"kept forever and rotated never."),
        "action": ("`tools/sign_credential.py set <id> --purpose \"…\" --evidence "
                   "\"…\"`, or the “sign…” button on the Keys page"),
    }]


def rotation_due(creds: list[dict], today: str) -> list[dict]:
    """Only where a rotation policy was ASKED FOR.

    A global age policy would put every key on the board on the day it was
    introduced, and a board that opens with rows nobody chose is a board nobody
    reads. `rotation_days` is opt-in per credential, set in the signature, so
    the row exists because somebody decided it should.
    """
    import datetime
    out = []
    for c in creds:
        days = (c.get("signature") or {}).get("rotation_days")
        if not days:
            continue
        since = c.get("rotated_on") or c.get("created_on") or c.get("installed_on")
        if not since:
            continue
        try:
            age = (datetime.date.fromisoformat(today)
                   - datetime.date.fromisoformat(str(since)[:10])).days
        except ValueError:
            continue
        if age <= days:
            continue
        out.append({
            "type": "credential.rotation_due",
            "subject": c["id"],
            "severity": "warning",
            **titled("{key} was last set {n} days ago and its own policy says every {days}", key=_short(c), n=age, days=days),
            "detail": (f"The policy is this credential's own — `rotation_days` in "
                       f"its signature — and it is {age - days} day(s) past. "
                       f"{'It was rotated' if c.get('rotated_on') else 'It has never been rotated; the date is when it was created'} "
                       f"on {str(since)[:10]}."),
            "action": ("rotate it through its door, then `tools/vault.py moved` "
                       "if the value went anywhere else"),
        })
    return out


def project_file_exposed(creds: list[dict]) -> list[dict]:
    """A key file beside the code that git tracks, or that everyone can read.

    Service-account keys, signing keys and wallet seeds kept as files next to a
    project are invisible to an inventory of `.env` files. This rule exists so
    such a file is caught the day it lands rather than the day it leaks.

    TRACKED IS CRITICAL AND UNIGNORED IS A WARNING, the same two verdicts
    `env.tracked_in_git` and `env.unignored` already carry for `.env` files, for
    the same reason: history is not a place you delete from, and a file one
    `git add -A` away from it looks identical to a safe one in every listing.
    """
    files = [c for c in creds if c.get("kind") == "project-secret-file"]
    if not files:
        return []
    out = []
    tracked = sorted(f"{c['in_project']}/{c['path']}" for c in files if c.get("git") == "tracked")
    if tracked:
        out.append({
            "type": "credential.project_file_in_git",
            "subject": "estate:project-secrets",
            "severity": "critical",
            **titled("{n} secret files beside the code are tracked by git", n=len(tracked)),
            "detail": (f"{_listed(tracked)}. The value is in the repository's history and "
                       f"on every clone of it; removing the file from the working tree "
                       f"does not remove it from the history, so rotation is the remedy "
                       f"and `git rm --cached` plus a .gitignore line is the part that "
                       f"stops it happening twice."),
            "action": "rotate what it holds, then `git rm --cached` it and ignore the path",
        })
    loose = sorted(f"{c['in_project']}/{c['path']}" for c in files if c.get("git") == "loose")
    if loose:
        out.append({
            "type": "credential.project_file_unignored",
            "subject": "estate:project-secrets",
            "severity": "warning",
            **titled("{n} secret files beside the code are neither tracked nor ignored", n=len(loose)),
            "detail": (f"{_listed(loose)}. One `git add -A` from the row above, and "
                       f"indistinguishable from a safe file in every listing that does "
                       f"not ask git the second question."),
            "action": "add the path to that repository's .gitignore",
        })
    open_mode = sorted(f"{c['in_project']}/{c['path']} ({c['mode']})" for c in files
                       if str(c.get("mode", "")).endswith(("4", "5", "6", "7"))
                       and str(c.get("mode", ""))[-2:] != "00")
    if open_mode:
        out.append({
            "type": "credential.project_file_readable",
            "subject": "estate:project-secrets",
            "severity": "warning",
            **titled("{n} secret files beside the code are readable by more than their owner", n=len(open_mode)),
            "detail": f"{_listed(open_mode)}. One `chmod 600` loop, not N decisions.",
            "action": "`chmod 600` them",
        })
    return out


def findings(doc: dict | None) -> list[dict]:
    """Every credential rule. Empty when nothing was scanned — absent is not clean,
    and the tab says so where a reader is already asking about credentials."""
    if not doc:
        return []
    creds = doc.get("credentials") or []
    if not creds:
        return []
    # THE DATE THE REGISTRY ALREADY HOLDS, never a clock of this rule's own
    #: the document says when it was measured, and every age here is
    # measured against that.
    today = (doc.get("scanned_on") or doc.get("updated_on") or "")[:10]
    broken = doc.get("registers_unreadable") or []
    # `unsigned` is WITHHELD when the annotations register could not be read:
    # every credential would look unsigned at once, and the board would say
    # "nobody signed anything" about a file somebody could not open.
    annotations_ok = not any("annotations" in str(b.get("register", "")) for b in broken)
    return (register_unreadable(broken) + lifetime_cap(creds) + unclaimed(creds)
            + untracked(creds) + shared_rotation(creds)
            + (unsigned(creds) if annotations_ok else [])
            + project_file_exposed(creds)
            + (rotation_due(creds, today) if today else []))


def register_unreadable(broken: list[dict]) -> list[dict]:
    """A curated register that exists and would not read: one row per file.

    Reading such a file as empty would be the opposite of the truth: for the
    annotations register every signature would vanish and `credential.unsigned`
    would light on every row; for the owners register every curated membership
    would vanish and `credential.unclaimed` would count them. A register that
    cannot be read is reported, never read as empty.
    """
    out = []
    for b in broken:
        out.append({
            "type": "credential.register_unreadable",
            "subject": f"register:{b.get('register')}",
            "severity": "warning",
            **titled("the curated register {register} exists and could not be read", register=b.get("register")),
            "detail": (f"{b.get('problem')}. Everything in it — signatures, curated "
                       f"memberships — is absent from this build, and the rules that "
                       f"would have named that absence are withheld so the board does "
                       f"not report a file's syntax as an estate of unsigned keys."),
            "action": (f"open {b.get('path')} by hand; `tools/sign_credential.py` refuses "
                       f"to write over a file it cannot read, so nothing was lost by a "
                       f"later write"),
        })
    return out
