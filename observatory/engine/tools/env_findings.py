#!/usr/bin/env python3
"""What the env inventory owes the operator, on the board rather than in a tab.

A TAB ANSWERS A QUESTION SOMEBODY ASKED. These four arrive at somebody who did
not, and each one is invisible from inside the project it belongs to:

`env.tracked_in_git`   a real `.env` that git tracks. Not a risk — an event: the
                       value is in the history, on every clone, and rotating is
                       the only remedy because history is not a place you delete
                       from. Templates are excluded by construction; a
                       `.env.example` is SUPPOSED to be committed, and a rule
                       that cannot tell the two apart cries wolf 168 times.
`env.unignored`        untracked AND unignored — one `git add -A` from the row
                       above. It looks identical to a safe file in every listing
                       that does not ask git the second question.
`env.world_readable`   mode grants group or other read. One row for all of them:
                       the remedy is one `chmod` loop, not twenty-six decisions.
`env.shared_secret`    one value, several projects, sometimes under different
                       names. The blast radius of a rotation, MEASURED — and the
                       rule points at `collectors/credential_owners.json` rather
                       than replacing it, because equal values prove equality,
                       not that two projects were meant to share an account.
`env.reusable_slot`    a secret slot still empty here whose name holds a live
                       value in another project. The one rule on this board that
                       reports an opportunity rather than a debt.

NO TIMESTAMP INSIDE A FINDING: every date quoted is one the registry
already holds.
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


def _real(doc: dict) -> list[dict]:
    """Files that hold live values. A template is not one of them."""
    return [f for f in doc.get("files", []) if f.get("kind") == "env"]


def tracked_in_git(doc: dict) -> list[dict]:
    """One row per file, because each is its own rotation."""
    out = []
    for f in _real(doc):
        if f.get("git") != "tracked":
            continue
        secrets = [v["name"] for v in f.get("variables", [])
                   if v.get("class") == "secret"]
        out.append({
            "type": "env.tracked_in_git",
            "subject": f["id"],
            "severity": "critical" if secrets else "warning",
            **titled("{path} is committed to git", path=f["path"]),
            "detail": (f"It is a live env file, not a template, and git tracks it — "
                       f"so its contents are in the repository's history and on "
                       f"every clone of it."
                       + (f" {len(secrets)} of its variables read as credentials: "
                          f"{_listed(secrets)}." if secrets else
                          " None of its variables reads as a credential, which is "
                          "why this is a warning rather than an incident.")
                       + " Removing the file from the working tree does not remove "
                         "it from the history."),
            "action": ("rotate what it holds, then `git rm --cached` it and add the "
                       "path to .gitignore; record the exposure with "
                       "`tools/vault.py leak --where` so the debt stays visible"),
        })
    return out


def unignored(doc: dict) -> list[dict]:
    """Untracked and unignored — one `git add -A` from the rule above."""
    rows = [f for f in _real(doc) if f.get("git") == "loose"]
    if not rows:
        return []
    return [{
        "type": "env.unignored",
        "subject": "estate:env-unignored",
        "severity": "warning",
        **titled("{n} env files are untracked but not ignored", n=len(rows)),
        "detail": ("git does not carry them today and nothing stops it carrying them "
                   f"tomorrow: {_listed([f['path'] for f in rows])}. In `git status` "
                   "they are indistinguishable from a file that is safely ignored, "
                   "and `git add -A` does not ask."),
        "action": "add each path to its repository's .gitignore",
    }]


def world_readable(doc: dict) -> list[dict]:
    """One row: the remedy is one loop, not twenty-six decisions."""
    rows = [f for f in _real(doc)
            if any(v.get("class") == "secret" for v in f.get("variables", []))
            and f.get("mode", "0600")[-2:] != "00"]
    if not rows:
        return []
    return [{
        "type": "env.world_readable",
        "subject": "estate:env-modes",
        "severity": "warning",
        **titled("{n} env files holding credentials are readable beyond their owner", n=len(rows)),
        "detail": ("Their mode grants group or other read, so any process on this "
                   "machine that is not this user can read the values: "
                   + _listed([f"{f['path']} ({f['mode']})" for f in rows])
                   + ". A backup agent, a sync client and a second account all "
                     "qualify, and none of them announces itself."),
        "action": "chmod 600 each of them; nothing that reads an env file needs more",
    }]


def shared_secret(doc: dict) -> list[dict]:
    """One value in several projects — the measured blast radius of a rotation."""
    groups = [g for g in doc.get("shared", []) if g.get("class") == "secret"]
    if not groups:
        return []
    widest = max(groups, key=lambda g: len(g["projects"]))
    return [{
        "type": "env.shared_secret",
        "subject": "estate:env-shared",
        "severity": "info",
        **titled("{n} credentials are held by more than one project", n=len(groups)),
        "detail": ("Measured by value, not by name, which is why "
                   + ", ".join(widest["names"][:3])
                   + (" are one credential under " + str(len(widest["names"]))
                      + " names across " + str(len(widest["projects"]))
                      + " projects" if len(widest["names"]) > 1 else
                      " is one credential across " + str(len(widest["projects"]))
                      + " projects")
                   + ". Rotating any of these changes what every project sharing it "
                     "must hold, at the same moment. The widest: "
                   + _listed(widest["projects"]) + "."),
        "action": ("open the ENV tab, and where a group is genuinely one account, "
                   f"record it in {_config_label('credential_owners.json')} with these sites "
                   "as the evidence — equal values prove equality, not intent"),
    }]


def reusable_slot(doc: dict) -> list[dict]:
    """An empty slot whose name holds a live value somewhere else."""
    rows = [(f["path"], v["name"], v["available_in"])
            for f in _real(doc) for v in f.get("variables", [])
            if v.get("available_in")]
    if not rows:
        return []
    return [{
        "type": "env.reusable_slot",
        "subject": "estate:env-reusable",
        "severity": "info",
        **titled("{n} empty credential slots have a value in another project", n=len(rows)),
        "detail": ("Each of these is a variable this project declares and leaves "
                   "blank while another project here holds a live one under the same "
                   "name: "
                   + _listed([f"{p}:{n} (in {w[0]})" for p, n, w in rows])
                   + ". Same name is not same account — a staging key and a "
                     "production key share a name by design — so this is where to "
                     "look, not what to copy."),
        "action": ("open the ENV tab and reveal the value in the project that has "
                   "one; the reveal is audited and the value never reaches this page"),
    }]


def findings(doc: dict | None) -> list[dict]:
    """Every env rule. Empty when nothing was scanned — absent is not clean, and
    the tab says so where a reader is already asking about env files."""
    if not doc or not doc.get("files"):
        return []
    return (tracked_in_git(doc) + unignored(doc) + world_readable(doc)
            + shared_secret(doc) + reusable_slot(doc))
