"""The local selection record of what goes to one organization, and its apply (OBS-417; organization onboarding
ORG-W5, ORG-W7, ORG-W8, ORG-R02, ORG-R03, ORG-R05, ORG-R11 to ORG-R16, ORG-R24, ORG-R25, ORG-R29).

WHY. Choosing which projects, keys and accounts go to an organization is a decision the person makes once and may
change later, and every later step (Fabric's adaptation plan, a credential request, a rotation, a new space) needs
to know exactly what was chosen, by whom and when. That decision is one local record, `org-selection/1`, the same to
the CLI (`full org select --org NAME plan|show|apply|undo|confirm-replacement`) and to an agent
(`observatory_org_selection`).

WHAT IT IS, AND WHAT IT IS NOT. The record is owner-only (`private_io`), lives in this space's store
(`store/org-selection/`), is never sent anywhere and is never signed by a device key (ORG-R11): it is the preview
and the journal of what Observatory does on this computer. Each revision names its predecessor's digest and its own,
so it verifies on every read. It holds names and decisions only, never a key's value: keys are vault slots by name.

WHAT APPLY DOES TODAY, AND WHAT IT DOES NOT. Apply writes the revision and a journal of steps per item. It moves
nothing it has no door for (ORG-R25: a step that has not happened is never shown as done):

* a project reaches an organization only as an `org-adaptation/1` item the person's Fabric sends with the person's
  credential (contract DEC-0138, Fabric TP-04, contract CT-169). Fabric has no door Observatory can hand items to
  yet, so each chosen project waits as `waiting` on `fabric-adaptation-plan`; the items are prepared here (a
  reference random per organization, a label only when the person typed one, ORG-R03) and nothing is sent;
* a key moves only by rotation (OBS-416, on top of the request queue of OBS-191 parts 2 and 3): a key handed over
  waits on `credential-requests`, and its revocation, the one step that cannot be undone, waits behind it. No value
  is read, copied or written by anything here;
* the layout's own step (a new space, OBS-362; personal kinds moved out, OBS-426; contexts on every item, OBS-359)
  waits on its row.

What does happen: an account recorded for the organization, and in the separate-space layout a key kept personal,
are `done` (there is nothing else to do for them; in this space or one space a kept key waits to move out or for
its context, OBS-426, OBS-359), the revision is written, and `show` reads the journal back with its counts.

DEFAULTS (ORG-R15, ORG-R16). Without a record: projects the organization's rules give it, and accounts a rule names
for it, are proposed and chosen; projects and accounts that need a decision are listed and not chosen; a key is
handed over (and the personal value revoked once the replacement works) only when its project and its issuing
account agree and its project is chosen. With a record: every item keeps the record's decision; a project or key
the rules propose that the record does not hold is listed as `new` and never chosen by itself (ORG-R16 names
projects and keys); anything else found since is listed and waits, unchosen.

THE GATES (contract DEC-0121, ORG-R23). Reading (`show`, `plan` without a change) needs nothing. Changing the plan,
`apply`, `undo` and `confirm-replacement` are relayed: from the CLI they are the person's own act, recorded as
such; from an agent they need the operator's words in DEC-0121's decision shape, checked by `node/actions.py`
(the contract's `actionDecision`), and the record keeps those words as who decided. The human steps (the passkey
signing in Fabric the estate revision that places the projects in the organization's context, R1-D12; the macOS
administrator password for a new OS account; the administrator's computer token) are named, never taken.
"""
from __future__ import annotations

import contextlib
import copy
import datetime
import getpass
import hashlib
import json
import os
import pathlib
import re
import secrets
import stat
import sys
from typing import Any

import paths
import private_io

SCHEMA = "org-selection/1"
VIEW_SCHEMA = "org-select/1"
DRAFT_SCHEMA = "org-selection-draft/1"
JOURNAL_SCHEMA = "org-selection-journal/1"
LAYOUTS = ("separate-space", "this-space", "one-space")
RECOMMENDED_LAYOUT = "separate-space"
KEY_CHOICES = ("hand-over-revoke", "hand-over-keep", "keep")
ITEM_CHOICES = ("include", "exclude")
GROUPS = ("proposed", "needs-you")
STEP_STATES = ("planned", "waiting", "done", "failed", "undone")
#: A label the organization will read (ORG-R03): typed by the person, one line, short.
LABEL_MAX = 80
#: A space's name as the person gives it (a macOS user, a VM): one line, short.
SPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")
#: Item ids this record takes: a project, a vault slot, a provider account (`org-accounts/1` ids).
ITEM_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}:[^\s]{1,300}")
#: DEC-0121 action ids of the relayed acts (`interop-common` requestKey).
ACTIONS = {"plan": "org.select.plan", "apply": "org.select.apply", "undo": "org.select.undo",
           "confirm-replacement": "org.select.confirm-replacement"}
TITLES = {"plan": "Change the selection", "apply": "Apply the selection", "undo": "Undo the selection",
          "confirm-replacement": "Confirm the replacement works"}
#: Gate per action (DEC-0121): `plan` is `none` when it changes nothing.
GATES = {"show": "none", "plan": "relayed", "apply": "relayed", "undo": "relayed", "confirm-replacement": "relayed"}
CMD = "project-observatory full org select --org {org}"

#: What a waiting step waits on: the missing door or decision, and the row or owner that builds it.
DEPENDS = {
    "fabric-adaptation-plan": {
        "what": "Fabric's adaptation plan has no door Observatory can hand project items to yet; the item is "
                "prepared here (a random reference, the label you typed) and nothing is sent",
        "owner": "Fabric TP-04, contract DEC-0138 and CT-169"},
    "fabric-entry-revision": {
        "what": "your passkey signs, in Fabric, the estate revision that places the project in the organization's "
                "context (R1-D12); it follows the hand-off to Fabric",
        "owner": "Fabric (a human step: your passkey)"},
    "space-export": {
        "what": "the project's curated entry is exported into the organization's space once that space exists",
        "owner": "OBS-364"},
    "credential-requests": {
        "what": "the space that will hold the organization's key asks the organization for its own (provider, "
                "scopes, environment, slot name; never a value); the queue exists (OBS-191 parts 2 and 3) but apply files "
                "no request into it yet, and the request reaching the organization is OBS-454",
        "owner": "OBS-454, OBS-416"},
    "rotation": {
        "what": "consumers switch to the organization's replacement, then the personal value is revoked at its "
                "provider with proof; the rotation door for a hand-over is not built",
        "owner": "OBS-416"},
    "replacement-confirmation": {
        "what": "in a separate space this space cannot read the organization's, so the revocation waits for your "
                "confirmation that the replacement works",
        "owner": "you: full org select --org NAME confirm-replacement KEY"},
    "space-create": {
        "what": "a new OS account or VM becomes a space bound to the organization and enrolls with its own computer "
                "token; the macOS administrator password is yours to type",
        "owner": "OBS-362, OBS-359, OBS-414"},
    "move-out": {
        "what": "every personal kind in this space is carried into your personal space and verified, or removed "
                "with its reason, before this space is bound",
        "owner": "OBS-426"},
    "context-binding": {
        "what": "every project and slot of this space carries its context, so the organization sees only its own",
        "owner": "OBS-359"},
}

#: The steps a person's body is needed for, named once each (ORG-R23); never taken by an agent.
HUMAN_STEPS = {
    "passkey-entry": "in Fabric, your passkey signs the estate revision that places the chosen projects in the "
                     "organization's context (R1-D12, Fabric ADR-0178 point 7)",
    "admin-password": "the macOS administrator password, once, when a new OS account is created for the "
                      "organization's space",
    "computer-token": "your administrator prepares a computer token for the organization's space in the hub "
                      "(Computers, Prepare a computer)",
}


class Refusal(ValueError):
    """A step this module does not take, with the reason and the command that changes it."""

    def __init__(self, code: str, message: str, command: str | None = None, **extra: Any):
        super().__init__(message)
        self.code = code
        self.command = command
        self.extra = extra

    def answer(self) -> dict:
        out = {"error": self.code, "message": str(self), "degraded": []}
        if self.command:
            out["command"] = self.command
        out.update(self.extra)
        return out


# ── time, digests, files ───────────────────────────────────────────────────────

#: The clock; tests replace it.
CLOCK = lambda: datetime.datetime.now(datetime.timezone.utc)  # noqa: E731


def _now() -> str:
    return CLOCK().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical(doc: Any) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _digest(doc: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(doc)).hexdigest()


def root() -> pathlib.Path:
    """Where every organization's selection lives in this space: owner-only, never sent."""
    return paths.STORE / "org-selection"


def _folder(org: str) -> pathlib.Path:
    # An organization's name is free text: the folder is named by its digest, and the record names the organization.
    return root() / hashlib.sha256(org.encode("utf-8")).hexdigest()[:16]


def _read(path: pathlib.Path) -> dict | None:
    """A JSON file of this record, or None when absent. Not owner-only, a symbolic link or not JSON: refused."""
    try:
        absent = not path.exists() and not path.is_symlink()
    except OSError as exc:                             # Python 3.11: a folder without search permission raises
        raise Refusal("record-unreadable", f"{path.name} of the selection cannot be reached ({type(exc).__name__})",
                      "project-observatory full doctor") from None
    if absent:
        return None
    try:
        doc = json.loads(private_io.read(path))
    except (OSError, RuntimeError, ValueError) as exc:
        raise Refusal("record-unreadable", f"{path.name} of the selection does not read ({type(exc).__name__}); "
                                           "nothing is decided from it until it does",
                      "project-observatory full doctor") from None
    if not isinstance(doc, dict):
        raise Refusal("record-unreadable", f"{path.name} of the selection is not one JSON object")
    return doc


def _write(path: pathlib.Path, doc: dict) -> None:
    try:
        private_io.write(path, json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    except UnicodeError:
        raise Refusal("record-unwritable", f"{path.name} of the selection cannot be written: it would hold text that "
                                           "is not valid Unicode; nothing was decided") from None
    except (OSError, RuntimeError) as exc:
        raise Refusal("record-unwritable", f"{path.name} of the selection cannot be written ({type(exc).__name__}: "
                                           "a symbolic link, a folder that is not this account's, or a full disk); "
                                           "nothing was decided", "project-observatory full doctor") from None


def _revisions(org: str) -> list[pathlib.Path]:
    folder = _folder(org) / "revisions"
    try:
        if not folder.is_dir() or folder.is_symlink():
            return []
        return sorted(p for p in folder.glob("[0-9]*.json") if p.stem.isdigit())
    except OSError as exc:
        raise Refusal("record-unreadable", f"the selection's revisions do not list ({type(exc).__name__}); nothing "
                                           "is decided from them", "project-observatory full doctor") from None


def _verified(doc: dict, org: str, where: str) -> dict:
    """A revision whose digest is its own body's and whose organization is the one asked (ORG-R11)."""
    body = {k: v for k, v in doc.items() if k != "digest"}
    if doc.get("schema") != SCHEMA or doc.get("organization") != org or doc.get("digest") != _digest(body):
        raise Refusal("record-unreadable", f"{where} does not verify against its digest; nothing is decided from it "
                                           "(it was changed by hand or is damaged)")
    return doc


def history(org: str) -> list[dict]:
    """Every revision, oldest first, each verified and each naming its predecessor's digest."""
    out: list[dict] = []
    for path in _revisions(org):
        doc = _verified(_read(path) or {}, org, f"revision {path.stem}")
        expected = out[-1]["digest"] if out else None
        if doc.get("predecessor") != expected or doc.get("revision") != len(out) + 1:
            raise Refusal("record-unreadable", f"revision {path.stem} does not name the revision before it; the "
                                               "chain is broken and nothing is decided from it")
        out.append(doc)
    return out


def current(org: str) -> dict | None:
    revs = history(org)
    return revs[-1] if revs else None


def _journal_path(org: str, revision: int) -> pathlib.Path:
    return _folder(org) / "journals" / f"{revision:06d}.json"


def recorded_projects(org: str) -> list[str]:
    """The projects the applied selection chose for `org` (for `full org keys`); none without a record."""
    rec = current(org) if _revisions(org) else None
    return sorted(i["id"] for i in (rec or {}).get("items") or []
                  if i.get("kind") == "project" and i.get("decision") == "include")


def journal(org: str, revision: int, digest: str | None = None) -> dict | None:
    """The journal of one revision; with `digest`, it must name that revision's digest (a journal left by another
    revision of the same number is not this one's)."""
    doc = _read(_journal_path(org, revision))
    if doc is not None and (doc.get("schema") != JOURNAL_SCHEMA or doc.get("revision") != revision
                            or not isinstance(doc.get("items"), list)
                            or (digest is not None and doc.get("recordDigest") != digest)):
        raise Refusal("record-unreadable", f"the journal of revision {revision} is not {JOURNAL_SCHEMA} for that "
                                           "revision; nothing is decided from it")
    return doc


def _draft(org: str, *, consumed_ok: bool = False) -> dict | None:
    """The person's pending changes, or None. A draft a revision already consumed (its name is that revision's
    `consumedDraft`, left behind by an interruption) is no draft."""
    try:
        doc = _read(_folder(org) / "draft.json")
    except Refusal as refusal:
        raise Refusal(refusal.code, f"the draft does not read: {refusal}", f"{CMD.format(org=_q(org))} plan --reset") \
            from None
    rows_ok = isinstance((doc or {}).get("items"), dict) and all(
        isinstance(r, dict) and r.get("kind") in ("project", "key", "account")
        and ((r.get("labelOnly") is True and "decision" not in r)
             or r.get("decision") in (ITEM_CHOICES if r.get("kind") != "key" else KEY_CHOICES))
        for r in (doc or {}).get("items", {}).values()) if doc is not None else True
    if doc is not None and (doc.get("schema") != DRAFT_SCHEMA or doc.get("organization") != org or not rows_ok
                            or doc.get("layout", None) not in (None, *LAYOUTS)
                            or not isinstance(doc.get("serves", ""), (str, type(None)))):
        raise Refusal("record-unreadable", f"the draft is not {DRAFT_SCHEMA}; drop it and start again",
                      f"{CMD.format(org=_q(org))} plan --reset")
    if doc is not None and not consumed_ok and doc.get("id"):
        revs = _revisions(org)
        if revs and (_read(revs[-1]) or {}).get("consumedDraft") == doc["id"]:
            return None                                # consumed by the last revision; only its removal was lost
    return doc


def _q(text: str) -> str:
    return text if re.fullmatch(r"[A-Za-z0-9._@+-]+", text) else "'" + text.replace("'", "'\\''") + "'"


def _space_here() -> dict:
    """This space: the workspace's digest (as in the launchd labels) and the OS account it runs as."""
    import service_identity
    try:
        account = getpass.getuser()
    except (OSError, KeyError, ImportError):          # Windows without user variables: ImportError (3.11, 3.12)
        account = None
    return {"workspace": service_identity.workspace_digest(), "account": account}


# ── who decided (DEC-0121) ─────────────────────────────────────────────────────

def _node_actions():
    """The engine's `node.actions` (the contract's `actionDecision`), whatever else this process imported as `node`:
    `tools/node.py` is a module of that name, and a door that put `tools/` first on the path shadows the package.
    Then the package is loaded under a private name from its own folder, so its relative imports still resolve."""
    import importlib
    import importlib.util
    engine = pathlib.Path(__file__).resolve().parent / "node"
    loaded = sys.modules.get("node")
    if loaded is not None and getattr(loaded, "__path__", None) and \
            pathlib.Path(list(loaded.__path__)[0]).resolve() == engine:
        from node import actions                       # the engine's package is the one imported
        return actions
    if loaded is None:
        spec = importlib.util.find_spec("node")
        if spec is not None and spec.submodule_search_locations and \
                pathlib.Path(list(spec.submodule_search_locations)[0]).resolve() == engine:
            from node import actions                   # importing `node` now finds the engine's package
            return actions
    # Otherwise never import the name `node` (that would leave tools/node.py behind for every later import):
    name = "_observatory_engine_node"
    if name not in sys.modules:
        folder = engine
        spec = importlib.util.spec_from_file_location(name, folder / "__init__.py",
                                                      submodule_search_locations=[str(folder)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[name] = package
        spec.loader.exec_module(package)
    return importlib.import_module(name + ".actions")


def decided(action: str, target: str, decision: Any = None, *, caller: str) -> dict:
    """Who decided a relayed act. No decision from the CLI: the person at their terminal. With one: the operator's
    words, checked as the contract's `actionDecision` checks them (this space's agent is T0), kept verbatim."""
    if decision is None:
        if caller == "cli":
            return {"by": "person", "via": "cli"}
        raise Refusal("operator-decision-required",
                      f"{TITLES[action].lower()} is the person's decision: ask them, then call again with their words "
                      "as `decision` (by operator, via relayed-instruction, action "
                      f"{ACTIONS[action]}, target {_echo(target)}, the words verbatim, when they said them)",
                      gate=GATES[action], action=ACTIONS[action])
    if not isinstance(decision, dict):
        # A string, a list or a number is not a decision (CLI and MCP refuse it alike, before anything is written).
        raise Refusal("decision-invalid", "the decision is not one JSON object in contract DEC-0121's shape",
                      gate=GATES[action], action=ACTIONS[action])
    try:
        # Kept verbatim in the record: words that are not valid Unicode (a lone surrogate) could never be written.
        json.dumps(decision, ensure_ascii=False).encode("utf-8")
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise Refusal("decision-invalid", "the decision is not valid Unicode text in contract DEC-0121's shape",
                      gate=GATES[action], action=ACTIONS[action]) from None
    actions = _node_actions()
    act = {"id": ACTIONS[action], "title": TITLES[action], "tool": "observatory.org.selection", "gate": "relayed"}
    answer = actions.action_decision(action=act, tier="T0", caller=caller, decision=decision, target=target,
                                     now=_now())
    import credential_shape
    kept = [decision.get("instruction"), decision.get("target"),
            *(((decision.get("source") or {}).values()) if isinstance(decision.get("source"), dict) else [])]
    # A selection's own digest (`sha256:` and 64 hex, as the plan's command quotes it) is not a credential: the
    # operator may repeat that command in their words.
    kept = [re.sub(r"\bsha256:[0-9a-f]{64}\b", "sha256:digest", v) if isinstance(v, str) else v for v in kept]
    if answer.get("ok") is True and any(credential_shape.shaped(v, uuids=False) for v in kept if isinstance(v, str)):
        # The words, the object and where they were said are kept verbatim and shown back: none may carry something
        # shaped like a credential (a session's id, a UUID, is not one).
        raise Refusal("decision-invalid", "the decision carries something shaped like a credential (in the words, the "
                                          "object or where they were said); send it without", gate=GATES[action],
                      action=ACTIONS[action])
    if answer.get("ok") is True and (not isinstance(decision, dict) or decision.get("target") != target):
        # The contract leaves `target` optional; here it is required, so words about one organization (or one key)
        # never decide another's selection.
        answer = {"ok": False, "code": "decision-invalid"}
    if answer.get("ok") is not True:
        raise Refusal(answer.get("code") or "decision-invalid",
                      "the decision is not the operator's for this action and object, or is older than 24 hours: "
                      "ask again and send their words as they said them", gate=GATES[action], action=ACTIONS[action])
    record = {k: copy.deepcopy(decision[k]) for k in ("by", "via", "action", "target", "instruction", "at", "source")
              if k in decision}
    record["caller"] = answer["recordAs"]["caller"]
    return record


# ── what can be chosen ─────────────────────────────────────────────────────────

def _context(org: str, chosen: list[str]) -> dict:
    """The organization's projects, keys and accounts, as `full org keys` and `full org accounts` read them."""
    import org_view
    try:
        kv = org_view.keys(org, chosen)
    except org_view.Refusal as refusal:
        raise Refusal(refusal.code, str(refusal), refusal.command) from None
    av = org_view.accounts()
    _doc, assigner = org_view.organization_checked(org)
    degraded: list[dict] = []
    pdoc = org_view._registry("projects.json", [], "")
    rows = org_view._project_rows(pdoc)
    projects = {p["id"]: p for p in rows}
    resolve = org_view._vault_resolver(rows)
    owner = {}
    slots = []
    for folder, env, name in org_view._slots(degraded):
        slot = f"{folder}/{env}/{name}"
        slots.append(slot)
        owner[slot] = resolve(folder)
    # The provider documents the views read quietly (absent is normal for a provider never connected): one that
    # exists and does not read is named here, so an account it would list reads unread, never gone.
    for name in PROVIDER_DOCS.values():
        found: list[dict] = []
        org_view._registry(name, found, "its accounts are not listed here")
        degraded.extend(d for d in found if d.get("code") != "missing")
    seen = {(d.get("source"), d.get("code"), d.get("reason")) for d in degraded}
    for d in kv["degraded"] + av["degraded"]:
        key = (d.get("source"), d.get("code"), d.get("reason"))
        if key not in seen:
            seen.add(key)
            degraded.append(d)
    return {"keys": kv, "accounts": av, "projects": projects, "slots": slots, "slotProject": owner,
            "default": assigner.default, "degraded": degraded, "label": kv["label"]}


def _candidates(org: str, ctx: dict) -> dict[str, dict]:
    """Every item the person may choose for `org`, with its default (ORG-R15) and why. Not here: projects and
    accounts that are somebody else's (Not ours) or the organization's not at all: they stay personal, counted."""
    out: dict[str, dict] = {}
    for p in ctx["keys"]["projects"]:
        group = "by-hand" if p.get("byChoice") else "proposed" if p["side"] == "organization" else "needs-you"
        out[p["id"]] = {"id": p["id"], "kind": "project", "group": group, "why": p["why"],
                        "default": "include" if group in ("proposed", "by-hand") else "exclude",
                        "name": (ctx["projects"].get(p["id"]) or {}).get("name")}
    for a in ctx["accounts"]["accounts"]:
        if a.get("organization") == org and a.get("source") == "rule":
            group = "proposed"                     # a rule names it, connected here or not
        elif a.get("source") == "conflict" and org in (a.get("claimedBy") or []):
            group = "needs-you"
        elif a.get("source") == "none":
            group = "needs-you"
        else:
            continue
        out[a["id"]] = {"id": a["id"], "kind": "account", "group": group, "why": a.get("why") or "",
                        "default": "include" if group == "proposed" else "exclude",
                        "provider": a.get("provider"), "name": a.get("name")}
    for k in ctx["keys"]["keys"]:
        out[k["id"]] = {"id": k["id"], "kind": "key", "group": "proposed" if k["selected"] else "needs-you",
                        "why": k["why"], "class": k["class"], "project": k["project"], "slot": k["slot"],
                        "default": "hand-over-revoke" if k["selected"] else "keep",
                        "environmentDeclared": k["environmentDeclared"]}
    return out


def _baseline(record: dict | None) -> dict[str, dict] | None:
    """The record's own decisions, which every later plan starts from; None before the first, or after an undo
    back to nothing (defaults apply again). Any other record, one with a layout and no item among them, is a
    baseline: what is found after it is new and never chosen by itself (ORG-R16)."""
    if not record:
        return None
    if not record.get("items") and record.get("kind") == "undo" and not record.get("layout"):
        return None
    return {i["id"]: i for i in record.get("items") or []}


def _effective(org: str, ctx: dict, draft: dict | None, record: dict | None) -> dict:
    """The selection as it stands: the defaults, or the record's decisions with anything new listed and not
    chosen (ORG-R16), then the person's changes in the draft."""
    cands = _candidates(org, ctx)
    base = _baseline(record)
    changes = (draft or {}).get("items") or {}
    # Projects the person chose in this draft that the record did not hold: their keys take the defaults, as they
    # would before any record (ORG-R15), rather than reading as found since.
    newly = {i for i, c in changes.items() if c.get("kind") == "project" and c.get("decision") == "include"
             and (base or {}).get(i, {}).get("decision") != "include"}
    items: dict[str, dict] = {}
    for ident, c in cands.items():
        row = {k: v for k, v in c.items() if k != "default"}
        follows = (c["kind"] == "key" and c.get("project") in newly
                   and (ident not in (base or {}) or base[ident].get("by") in ("rule", "default", "project")))
        if base is None or follows:
            # Before any record, or a key nobody chose by hand whose project is chosen now: the default (ORG-R15).
            row.update(decision=c["default"], by="rule" if c["default"] in ("include", "hand-over-revoke") else
                       "default", new=False)
        elif ident in base:
            # A row the record left out by default (it needed a decision then) that the rules propose now is found
            # since, as ORG-R16 means it: listed as new, never chosen by itself.
            again = (base[ident].get("by") == "default" and base[ident]["decision"] in ("exclude", "keep")
                     and c["group"] == "proposed" and c["kind"] in ("project", "key"))
            row.update(decision=base[ident]["decision"], by=base[ident].get("by", "record"), new=again)
            for k in ("label", "ref", "decidedBy", "labelledBy"):
                if base[ident].get(k):
                    row[k] = base[ident][k]
        else:
            # ORG-R16: found since the record. Only what the organization's rules propose (a project or a key) is
            # `new`, listed and never chosen; anything else that appeared waits for a person as needs-you always does.
            row.update(decision="keep" if c["kind"] == "key" else "exclude", by="default",
                       new=c["group"] == "proposed" and c["kind"] in ("project", "key"))
        items[ident] = row
    known = set(ctx["projects"]) | {f"vault:{x}" for x in ctx["slots"]} | {a["id"] for a in
                                                                          ctx["accounts"]["accounts"]}
    for ident, prior in (base or {}).items():
        if ident not in items:
            # In the record and no longer a candidate: kept with its decision. Not known here: `unread` when the
            # source that would list it did not read this time (the item may well exist), `notFound` only when its
            # source read and does not list it (a project gone from the registry, a slot deleted, an account gone).
            row = {k: v for k, v in prior.items() if k not in ("notFound", "unread")}
            row["new"] = False
            if ident not in known:
                source = _unread_source(prior.get("kind"), ident, ctx)
                row.update({"unread": source} if source else {"notFound": True})
            items[ident] = row
    # A key follows its project when nobody chose it by hand: a project not chosen hands no key over by default.
    chosen = {i for i, r in items.items() if r["kind"] == "project" and r["decision"] == "include"}
    for ident, change in changes.items():
        label_only = change.get("labelOnly") is True and "decision" not in change
        decided_now = not label_only and change.get("by", "hand") in ("hand", "group")
        row = items.get(ident)
        if row is None:
            row = {"id": ident, "kind": change["kind"], "group": "by-hand", "new": False,
                   "why": "you added it by hand" if decided_now else
                   "not proposed for this organization; only its label is set"}
            if label_only:
                row.update(decision="keep" if change["kind"] == "key" else "exclude", by="default")
        if not label_only:
            row.update(decision=change["decision"], by=change.get("by", "hand"),
                       new=row.get("new", False) and not decided_now)
        if decided_now and (row.get("group") == "by-hand" or ident not in cands):
            # Chosen or left out by hand, not proposed by a rule: the record says so, not an older reason.
            row.update(group="by-hand", why="you chose it by hand" if row["decision"] not in ("exclude", "keep")
                       else "you left it out by hand")
        if "label" in change:
            if change["label"]:
                row["label"] = change["label"]
            else:
                row.pop("label", None)
        if change.get("decidedBy") and decided_now:
            row["decidedBy"] = change["decidedBy"]
        if change.get("labelledBy"):
            row["labelledBy"] = change["labelledBy"]
        row.pop("unread", None)
        row.pop("notFound", None)
        if ident not in cands and ident not in known:
            # Chosen in the draft and gone since (or its source did not read): the same check as a recorded row.
            source = _unread_source(row["kind"], ident, ctx)
            row.update({"unread": source} if source else {"notFound": True})
        items[ident] = row
        if row["kind"] == "project":
            (chosen.add if row["decision"] == "include" else chosen.discard)(ident)
    for ident, row in items.items():
        if row["kind"] == "key" and ident not in changes and row.get("new") and row["decision"] == "keep" \
                and row.get("project") is not None and row["project"] not in chosen:
            # Found since the record, in a project the person left out: it follows that decision, it is not new.
            row.update(by="project", new=False)
        if row["kind"] == "key" and ident not in changes and row.get("by") in ("rule", "default", "project") \
                and row["decision"] != "keep" and row.get("project") is not None and row["project"] not in chosen:
            # `by: project`: it follows its project, a decision already made, so it is neither a default left for a
            # person nor something found since (ORG-R16).
            row.update(decision="keep", by="project", new=False,
                       why=row["why"] + "; its project is not chosen, so nothing is asked for it")
    # The form: what the record holds, what somebody decided in the draft, and the keys that follow a project chosen
    # in the draft. Everything else (found since and undecided, or a decision the rules leave to a person) is listed
    # beside the form and is neither in its digest nor written to the record, so an applied form reads applied until
    # a person decides something (ORG-R14, ORG-R16). Before any record the whole proposal is the form.
    for ident, row in items.items():
        row["inForm"] = (base is None or ident in base or ident in changes
                         or (row["kind"] == "key" and row.get("project") in newly))
    layout = (draft or {}).get("layout", (record or {}).get("layout"))
    serves = (draft or {}).get("serves", ((record or {}).get("serves") or {}).get("space"))
    # A space is named only for the separate-space layout; in the others the selection serves this space, and the
    # digest must not carry a name the record does not keep (ORG-R14: the applied form is the previewed one).
    if layout != "separate-space":
        serves = None
    return {"items": items, "layout": layout, "serves": serves}


#: The registry document each provider's accounts are listed from (beside its door's records and ledger).
PROVIDER_DOCS = {"digitalocean": "digitalocean-account.json", "cloudflare": "cloudflare-account.json",
                 "heroku": "heroku-apps.json"}


def _unread_source(kind: Any, ident: str, ctx: dict) -> str | None:
    """The source that did not read and would have listed this item, or None when every such source read."""
    for d in ctx["degraded"]:
        src = str(d.get("source") or "")
        if kind == "project" and src == "registry/projects.json":
            return src
        if kind == "key" and src == "vault":
            return src
        provider = ident.split(":", 1)[0]
        if kind == "account" and (src.split(":", 1)[0] == provider
                                  or src == f"registry/{PROVIDER_DOCS.get(provider, provider + '-account.json')}"
                                  or (provider in ("github", "bitbucket", "gitlab")
                                      and src in ("github", "registry/projects.json", "registry/repositories.json"))):
            return src
    return None


def selection_digest(org: str, sel: dict) -> str:
    """The digest of what is decided (the form the person previews, ORG-R14): the organization, the layout, the
    space it serves, and each item's decision and label. Reasons and counts are not in it: they explain, they do not
    decide."""
    # Only the form's rows (`_effective`, `inForm`): what the record holds and what somebody decided. A row listed
    # beside it (found since, undecided) is not in the record either, so the applied form reads applied.
    # Whether a person decided a row is part of the form too: excluding by hand what the record left out by default
    # is a decision the record must keep, or the row would read as found since forever (ORG-R16).
    return _digest({"organization": org, "layout": sel["layout"], "serves": sel["serves"],
                    "items": sorted([i["id"], i["kind"], i["decision"], i.get("label"),
                                     i.get("by") in ("hand", "group")]
                                    for i in sel["items"].values() if i.get("inForm", True))})


def _other_revisions(folder: pathlib.Path) -> list[pathlib.Path]:
    """Another organization's revisions, oldest first; none when it has no `revisions` folder yet. A folder that
    cannot be searched or listed, or a `revisions` entry that is a symbolic link or not a folder, is refused: it is
    never read as "no record", or the ORG-R05 check would pass over a project that record holds."""
    revdir = folder / "revisions"
    try:
        mode = os.lstat(revdir).st_mode
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise Refusal("record-unreadable", f"an organization's selection folder cannot be searched "
                                           f"({type(exc).__name__})") from None
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise Refusal("record-unreadable", "an organization's revisions are a symbolic link or not a folder; they "
                                           "are not read")
    try:
        names = os.listdir(revdir)
    except OSError as exc:
        raise Refusal("record-unreadable", f"an organization's revisions cannot be listed ({type(exc).__name__})") \
            from None
    return sorted(revdir / n for n in names if n.endswith(".json") and n[:-5].isdigit())


def _store_folders(base: pathlib.Path) -> list[pathlib.Path]:
    """The organizations' folders of the store; an unlistable store raises OSError for the caller to name."""
    return sorted(base.iterdir())


def _others_chosen(org: str) -> tuple[dict[str, str], list[dict]]:
    """project id -> another organization whose current record chose it (ORG-R05, until contract CT-141), and what
    could not be read. Each other record is verified as its own organization's history; one that does not verify is
    named, never read past: the conflict check then cannot say no project is held elsewhere."""
    out: dict[str, str] = {}
    unread: list[dict] = []
    base = root()
    if not base.is_dir() or base.is_symlink():
        return out, unread
    mine = _folder(org)
    effect = "whether another organization already holds a project is not known"
    try:
        entries = _store_folders(base)
    except OSError as exc:
        return out, [{"source": "store/org-selection", "code": "record-unreadable",
                      "reason": f"the selection store cannot be listed ({type(exc).__name__})", "effect": effect}]
    for link in (p for p in entries if p.is_symlink()):
        unread.append({"source": f"store/org-selection/{link.name}", "code": "record-unreadable",
                       "reason": "an organization's selection folder is a symbolic link; it is not read",
                       "effect": effect})
    for folder in (p for p in entries if p.is_dir() and not p.is_symlink() and p != mine):
        try:
            revs = _other_revisions(folder)
            if not revs:
                continue
            other = (_read(revs[-1]) or {}).get("organization")
            if not isinstance(other, str) or _folder(other) != folder:
                raise Refusal("record-unreadable", "a selection's folder does not match the organization it names")
            doc = current(other) or {}
        except Refusal as refusal:
            unread.append({"source": f"store/org-selection/{folder.name}", "code": refusal.code,
                           "reason": str(refusal)[:200],
                           "effect": "whether another organization already holds a project is not known"})
            continue
        for i in doc.get("items") or []:
            if isinstance(i, dict) and i.get("kind") == "project" and i.get("decision") == "include":
                out.setdefault(str(i.get("id")), other)
    return out, unread


def _elsewhere(org: str, ctx: dict, items: Any) -> list[dict]:
    """Chosen projects whose own answer is not this organization's (ORG-R05, ORG-R01): another organization's by a
    declaration or a rule (a project belongs to one context until contract CT-141), or Not ours (somebody else's
    code). Personal and undecided projects may be chosen by hand; these may not."""
    out = []
    default = ctx.get("default")
    for i in items:
        if i["kind"] != "project" or i["decision"] != "include":
            continue
        p = ctx["projects"].get(i["id"]) or {}
        src, other = p.get("organization_source"), p.get("organization")
        if src == "external" or other == "external":
            out.append({"project": i["id"], "organization": "external", "code": "not-ours",
                        "why": "Not ours: its repositories are somebody else's code, so it goes to no organization"})
        elif src in ("declared", "rule") and isinstance(other, str) and other not in (org, default):
            out.append({"project": i["id"], "organization": other, "code": "chosen-for-another",
                        "why": f"{other}'s by its {src}: a project belongs to one context until contract CT-141 "
                               "decides otherwise (ORG-R05)"})
    return out


def _clash(org: str, items: Any) -> None:
    """ORG-R05 before anything is written: refused for a project another organization's record holds, and refused
    when another record cannot be read (the check cannot be made)."""
    q = CMD.format(org=_q(org))
    items = list(items)
    if not any(i["kind"] == "project" and i["decision"] == "include" for i in items):
        return                                         # nothing chosen, nothing to clash: an undo to nothing works
    others, unread = _others_chosen(org)
    if unread:
        raise Refusal("conflict-unchecked", "another organization's selection does not read, so whether one of "
                                            "these projects is already held elsewhere cannot be checked: "
                                            + unread[0]["reason"], "project-observatory full doctor")
    clash = sorted(i["id"] for i in items if i["kind"] == "project" and i["decision"] == "include"
                   and i["id"] in others)
    if clash:
        raise Refusal("chosen-for-another", f"{clash[0]} is already chosen for {others[clash[0]]}; a project "
                                            "belongs to one context until contract CT-141 decides otherwise "
                                            "(ORG-R05)", f"{q} plan --exclude {clash[0]}")


# ── the preview ────────────────────────────────────────────────────────────────

def _preview(org: str, ctx: dict, sel: dict, refused: set[str] = frozenset()) -> dict:
    """ORG-W7 as data: what moves, where, what the organization will see, and the line that proves the rest.
    `refused`: the projects `conflicts` names (another organization's, or Not ours), which apply always refuses, so
    the organization is never told it receives them (ORG-R14)."""
    items = sel["items"].values()
    projects = [i for i in items if i["kind"] == "project" and i["decision"] == "include" and not i.get("notFound")
                and not i.get("unread")]
    chosen_projects = {i["id"] for i in projects}       # what stays personal leaves out everything chosen
    projects = [i for i in projects if i["id"] not in refused]
    keys_out = [i for i in items if i["kind"] == "key" and i["decision"] != "keep" and not i.get("notFound")
                and not i.get("unread")]
    revoked = [i for i in keys_out if i["decision"] == "hand-over-revoke"]
    accounts_in = [i for i in items if i["kind"] == "account" and i["decision"] == "include" and not i.get("notFound")
                   and not i.get("unread")]
    default = ctx.get("default")

    def personal(organization: Any) -> bool:
        # Stays personal: the default organization's, no organization yet (a conflict, nothing configured), or this
        # organization's that the person leaves out. Another organization's are not counted as personal.
        return organization in (None, default, org)
    stay_projects = sum(1 for pid, p in ctx["projects"].items()
                        if pid not in chosen_projects and p.get("organization_source") != "external"
                        and personal(p.get("organization")))
    out_keys = {i["id"] for i in keys_out}
    stay_keys = sum(1 for slot in ctx["slots"] if f"vault:{slot}" not in out_keys and (
        ctx["slotProject"].get(slot) is None or ctx["slotProject"][slot] in chosen_projects
        or personal((ctx["projects"].get(ctx["slotProject"][slot]) or {}).get("organization"))))
    stay_accounts = sum(1 for a in ctx["accounts"]["accounts"]
                        if a["id"] not in {i["id"] for i in accounts_in} and a.get("source") != "external"
                        and personal(a.get("organization")))
    human = ["passkey-entry"] if projects else []
    if sel["layout"] == "separate-space":
        human += ["admin-password", "computer-token"]
    return {
        "organization": {"projects": len(projects), "labelled": sum(1 for i in projects if i.get("label")),
                         "sees": "each project as a reference it cannot trace back, and a name only where you typed "
                                 "one; nothing else about your work"},
        "keysRequested": len(keys_out),
        "keysRevoked": len(revoked),
        "accounts": len(accounts_in),
        "staysPersonal": ({"projects": None, "keys": None, "accounts": None,
                           "line": "what stays personal is not known: " + (
                               "registry/projects.json" if any(d.get("source") == "registry/projects.json"
                                                               for d in ctx["degraded"]) else "the vault")
                           + " did not read"}
                          if any(d.get("source") in ("registry/projects.json", "vault") for d in ctx["degraded"])
                          else
                          {"projects": stay_projects, "keys": stay_keys, "accounts": stay_accounts,
                           "line": f"{stay_projects} projects and {stay_keys} keys stay personal and are never sent"}),
        "cannotBeUndone": [f"{i['id']}: the personal value is revoked at its provider once the organization's "
                           "replacement works" for i in revoked],
        "notMovable": ctx["keys"]["notMovable"],
        "humanSteps": [{"step": h, "what": HUMAN_STEPS[h]} for h in human],
        "sent": "nothing: this record never leaves this computer, and no project item is handed to Fabric yet",
        # ORG-W5's fourth group: when this space becomes the organization's, everything personal moves out first
        # (ORG-R27); the step is OBS-426's, named here so the preview says it before anything is applied.
        # Another organization's items move out too: a space bound to one context refuses the others' (ORG-R20).
        "movesOut": ({"projects": sum(1 for pid, p in ctx["projects"].items() if pid not in chosen_projects
                                      and p.get("organization_source") != "external"),
                      "keys": len(ctx["slots"]) - len(out_keys), "waitingOn": "move-out",
                      **DEPENDS["move-out"]} if sel["layout"] == "this-space" else None),
    }


def _items_out(sel: dict) -> list[dict]:
    return sorted(sel["items"].values(), key=lambda i: ({"project": 0, "key": 1, "account": 2}.get(i["kind"], 3),
                                                        i["id"]))


def plan(org: str, changes: dict | None = None, *, decision: Any = None, caller: str = "cli") -> dict:
    """The plan for `org`: the selection as it stands with its digest and preview. With `changes` the draft is
    changed first (a relayed act): `include`/`exclude` (item ids), `includeGroup`/`excludeGroup` (`proposed`,
    `needs-you`: every member listed now, each recorded one by one, ORG-R02), `labels` ({project id: text, or ""
    to clear}), `keys` ({key id: choice}), `layout`, `serves`, `reset`."""
    org_ok = _org_checked(org)
    changes = {k: v for k, v in (changes or {}).items()
               if v is not None and v is not False and not (isinstance(v, (list, dict)) and not v)}
    if "reset" in changes and changes["reset"] is not True:
        raise Refusal("invalid-change", "`reset` is true or absent")
    folder = _folder(org_ok)
    # The gate before anything is touched, the lock included; reading the plan writes nothing.
    who = decided("plan", org_ok, decision, caller=caller) if changes else None
    with (_locked(org_ok) if changes else contextlib.nullcontext()):
        record = current(org_ok)
        reset = bool(changes.pop("reset", False))
        if reset and changes:
            # Every other change is checked before the draft is dropped: a refused call changes nothing.
            _change(org_ok, None, record, changes, who)
        if reset:
            # Before the draft is read: a damaged draft is dropped by the command its refusal names.
            (folder / "draft.json").unlink(missing_ok=True)
        draft = _draft(org_ok)
        if changes:
            draft = _change(org_ok, draft, record, changes, who)
            _write(folder / "draft.json", draft)
        ctx = _context(org_ok, _chosen_projects(draft, record))
        sel = _effective(org_ok, ctx, draft, record)
        if changes and record and selection_digest(org_ok, sel) == record.get("selectionDigest"):
            # The change leaves exactly the applied form: there is nothing to apply, so no draft is kept.
            (folder / "draft.json").unlink(missing_ok=True)
            draft = None
    return _plan_answer(org_ok, ctx, sel, record, draft)


def _chosen_projects(draft: dict | None, record: dict | None) -> list[str]:
    """Projects chosen in the record or the draft, the draft winning: their keys join the plan (ORG-W6). A project
    the draft leaves out is not chosen, whatever the record said."""
    chosen = {i for i, r in (_baseline(record) or {}).items() if r["kind"] == "project" and r["decision"] == "include"}
    for i, c in ((draft or {}).get("items") or {}).items():
        if c.get("kind") == "project" and "decision" in c:          # a label alone decides nothing
            (chosen.add if c.get("decision") == "include" else chosen.discard)(i)
    return sorted(chosen)


def _plan_answer(org: str, ctx: dict, sel: dict, record: dict | None, draft: dict | None) -> dict:
    digest = selection_digest(org, sel)
    others, others_unread = _others_chosen(org)
    ctx["degraded"] = ctx["degraded"] + [d for d in others_unread if d not in ctx["degraded"]]
    conflicts = [{"project": i["id"], "organization": others[i["id"]], "code": "chosen-for-another",
                  "why": f"already chosen for {others[i['id']]}: a project belongs to one context until contract "
                         "CT-141 decides otherwise (ORG-R05)"}
                 for i in sel["items"].values()
                 if i["kind"] == "project" and i["decision"] == "include" and i["id"] in others]
    conflicts += [c for c in _elsewhere(org, ctx, sel["items"].values())
                  if c["project"] not in {x["project"] for x in conflicts}]
    # Only what is chosen counts: an excluded row or a key kept personal that is gone or unread moves nothing.
    unknown = [i["id"] for i in sel["items"].values() if i.get("notFound") and i["decision"] not in ("exclude", "keep")]
    unread = [i["id"] for i in sel["items"].values() if i.get("unread") and i["decision"] not in ("exclude", "keep")]
    applied = bool(record) and record.get("selectionDigest") == digest
    any_items = bool(sel["items"]) or bool(record)
    registry_unread = any(d.get("source") in ("registry/projects.json", "vault") for d in ctx["degraded"])
    state = ("partial" if registry_unread and not conflicts else "empty" if not any_items else "conflict"
             if conflicts else "partial" if ctx["degraded"] or unknown or unread else "complete")
    undecided = sorted(i["id"] for i in sel["items"].values() if i.get("new"))
    q = CMD.format(org=_q(org))
    if record and _unfinished(org, record):
        state = "waiting"                              # never complete while the last apply has not finished
        step, cmd = RESUME_STEP, f"{q} apply --digest {record['selectionDigest']}"
    elif registry_unread:
        step, cmd = ("registry/projects.json or the vault did not read, so what is proposed is not known; nothing is "
                     "applied until both read", "project-observatory full doctor")
    elif conflicts:
        step, cmd = "clear the projects another organization already holds", f"{q} plan --exclude PROJECT"
    elif others_unread and any(i["kind"] == "project" and i["decision"] == "include"
                               for i in sel["items"].values()):
        step, cmd = ("another organization's selection does not read, so whether a chosen project is already held "
                     "elsewhere cannot be checked; nothing is applied until it reads", "project-observatory full doctor")
    elif unknown:
        step, cmd = (f"{len(unknown)} chosen item(s) no longer found here: leave each out, or restore it, then "
                     "preview again", f"{q} plan --exclude {unknown[0]}")
    elif unread:
        step, cmd = (f"{len(unread)} chosen item(s) whose source did not read: nothing is applied until it reads",
                     "project-observatory full doctor")
    elif not any_items:
        step, cmd = ("no project, key or account here is proposed for this organization; add one by hand or a rule",
                     f"{q} plan --include PROJECT")
    elif sel["layout"] is None:
        step, cmd = ("choose where the organization's work lives first (a separate space is recommended)",
                     f"{q} plan --layout {RECOMMENDED_LAYOUT} --serves SPACE")
    elif sel["layout"] == "separate-space" and not sel["serves"]:
        step, cmd = "name the space this selection serves (the new OS account or VM)", f"{q} plan --serves SPACE"
    elif applied and undecided:
        step, cmd = (f"this is the applied selection; {len(undecided)} item(s) found since wait for you: include or "
                     "leave out each, then apply", f"{q} plan --include ID")
    elif applied:
        step, cmd = "this is the applied selection; nothing to apply", f"{q} show"
    else:
        step, cmd = "review the preview, then apply this exact form", f"{q} apply --digest {digest}"
    return {"schema": VIEW_SCHEMA, "answer": "plan", "generatedAt": _now(), "organization": org,
            "label": ctx["label"], "state": state, "digest": digest, "applied": applied,
            "layout": sel["layout"], "recommendedLayout": RECOMMENDED_LAYOUT, "serves": sel["serves"],
            "madeIn": _space_here(), "draft": draft is not None,
            "current": None if not record else {"revision": record["revision"], "digest": record["digest"],
                                                "selectionDigest": record["selectionDigest"]},
            "items": _items_out(sel), "new": sorted(i["id"] for i in sel["items"].values() if i.get("new")),
            "newSince": (record or {}).get("decidedAt"),
            "conflicts": conflicts, "unknown": sorted(unknown), "unread": sorted(unread), "preview": _preview(org, ctx, sel, {c["project"] for c in conflicts}),
            "nextStep": step, "command": cmd, "gates": GATES,
            "gate": "relayed" if draft is not None else "none", "degraded": ctx["degraded"]}


def _org_checked(org: Any, *, configured: bool = True) -> str:
    """A usable organization name; with `configured`, one `config/organizations.json` names and is not the default
    (the same refusals as `full org keys`), checked before any lock or file is touched. `show`, `undo` and
    `confirm-replacement` take a name only: a record outlives a rule removed later."""
    import credential_shape
    import org_view
    if not isinstance(org, str) or not org.strip() or len(org) > 200 or credential_shape.shaped(org):
        raise Refusal("invalid-organization", f"{credential_shape.echo(org)} is not an organization name",
                      "project-observatory full org accounts")
    if configured or not org_view.ORG_NAME.fullmatch(org):
        # A name only (show, undo, confirm) is taken when it is shaped like an organization's name; any other is
        # taken only when config/organizations.json names it, and is never echoed back otherwise.
        try:
            org_view.organization_checked(org)
        except org_view.Refusal as refusal:
            if configured:
                raise Refusal(refusal.code, str(refusal), refusal.command) from None
            raise Refusal("invalid-organization", f"{credential_shape.echo(org)} is not an organization name",
                          "project-observatory full org accounts") from None
    return org


class _locked:
    """One writer per organization's selection at a time (the CLI, the MCP server, a second terminal). With `every`,
    the lock of the whole store first, then the organization's: apply and undo check every other organization's
    record (ORG-R05) and must not race another organization's apply. The order is always the store's, then the
    organization's, so two writers never wait on each other in a circle."""

    def __init__(self, org: str, *, every: bool = False):
        self.paths = ([root() / ".lock"] if every else []) + [_folder(org) / ".lock"]
        self.stack: contextlib.ExitStack | None = None

    def __enter__(self):
        self.stack = contextlib.ExitStack()
        try:
            private_io.parent(root())                  # the store folder owner-only too, before any organization's
            for path in self.paths:
                private_io.parent(path.parent)
                self.stack.enter_context(private_io.lock(path))
        except (OSError, RuntimeError) as exc:
            self.stack.close()
            raise Refusal("record-unwritable", f"the selection's lock cannot be taken ({type(exc).__name__}: a "
                                               "symbolic link, a folder that is not this account's, or a full "
                                               "disk); nothing was decided", "project-observatory full doctor") from None
        return self

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)


def _echo(value: Any) -> str:
    """An item id as a refusal may quote it: never a credential's shape, never long."""
    import credential_shape
    return credential_shape.echo(value)


def _label_ok(text: Any) -> str:
    import credential_shape
    import unicodedata
    # One line of visible text: no control, format (bidirectional overrides among them) or line/paragraph separator.
    if not isinstance(text, str) or len(text) > LABEL_MAX \
            or any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp") for c in text) \
            or credential_shape.shaped(text):
        raise Refusal("invalid-label", f"a label is one line of at most {LABEL_MAX} characters the organization will "
                                       "read, never shaped like a credential")
    return text.strip()


def _change(org: str, draft: dict | None, record: dict | None, changes: dict, who: dict) -> dict:
    """The draft with one person's changes, each item recorded one by one with who decided (ORG-R02, ORG-R11)."""
    known = {"include", "exclude", "includeGroup", "excludeGroup", "labels", "keys", "layout", "serves"}
    extra = sorted(set(changes) - known)
    if extra:
        raise Refusal("invalid-change", f"unknown change {_echo(extra[0])}; the changes are: "
                                        + ", ".join(sorted(known)))
    for name, kind in (("include", list), ("exclude", list), ("labels", dict), ("keys", dict)):
        if name in changes and not isinstance(changes[name], kind):
            raise Refusal("invalid-change", f"`{name}` is a {'list of item ids' if kind is list else 'mapping'}")
    if "serves" in changes and changes.get("layout", (draft or {}).get("layout") or (record or {}).get("layout")) \
            not in (None, "separate-space"):
        raise Refusal("invalid-change", "a space is named only for the separate-space layout; in the others the "
                                        "selection serves this space")
    named: dict[str, str] = {}
    for verb in ("include", "exclude", "keys"):
        for ident in changes.get(verb) or []:
            if isinstance(ident, str) and named.setdefault(ident, verb) != verb:
                raise Refusal("invalid-change", f"{_echo(ident)} is named by both `{named[ident]}` and `{verb}`; "
                                                "decide each item once per change")
    if changes.get("includeGroup") is not None and changes.get("includeGroup") == changes.get("excludeGroup"):
        raise Refusal("invalid-change", "one group is both included and left out; decide it once per change")
    draft = copy.deepcopy(draft) if draft else {"schema": DRAFT_SCHEMA, "organization": org, "items": {}}
    if "layout" in changes:
        if changes["layout"] not in LAYOUTS:
            raise Refusal("invalid-layout", f"the layout is one of: {', '.join(LAYOUTS)}")
        draft["layout"] = changes["layout"]
    if "serves" in changes:
        import credential_shape
        if not isinstance(changes["serves"], str) or not SPACE.fullmatch(changes["serves"]) \
                or credential_shape.shaped(changes["serves"]):
            raise Refusal("invalid-space", "a space is named by up to 64 letters, digits, spaces, dots, dashes or "
                                           "underscores (the OS account or VM the organization's work lives in)")
        draft["serves"] = changes["serves"]
    hand = set(_chosen_projects(draft, record))
    for ident in changes.get("include") or []:
        if isinstance(ident, str) and ident.startswith("project:"):
            hand.add(ident)
    ctx = _context(org, sorted(hand))
    cands = _candidates(org, ctx)
    sel = _effective(org, ctx, draft, record)

    def kind_of(ident: Any) -> str:
        if isinstance(ident, str) and ident in sel["items"]:
            return sel["items"][ident]["kind"]         # an id this space listed, a free-text label's space included
        if not isinstance(ident, str) or not ITEM_ID.fullmatch(ident):
            raise Refusal("unknown-item", "an item is a project (project:ID), a key (vault:FOLDER/ENV/NAME) or an "
                                          "account as `full org accounts` names it", f"{CMD.format(org=_q(org))} plan")
        if ident in sel["items"]:
            return sel["items"][ident]["kind"]
        if ident in ctx["projects"]:
            return "project"
        raise Refusal("unknown-item", f"{_echo(ident)} is not a project, key or account this space knows for "
                                      f"{org}", f"{CMD.format(org=_q(org))} plan")

    def put(ident: str, decision: str | None, by: str | None = "hand", *, label_only: bool = False,
            **more: Any) -> None:
        row = dict(draft["items"].get(ident) or {})
        if label_only:
            # Typing a label is not deciding the item: the draft keeps the label and who typed it, and a decision only
            # when a person made one in this draft; otherwise the record or the defaults decide at plan time (ORG-R02,
            # ORG-R11).
            row.update(kind=kind_of(ident), labelledBy=who, **more)
            if "decision" not in row:
                row["labelOnly"] = True
        else:
            row.pop("labelOnly", None)
            row.update(kind=kind_of(ident), decision=decision, by=by, decidedBy=who, **more)
        draft["items"][ident] = row

    for verb in ("include", "exclude"):
        for ident in changes.get(verb) or []:
            kind = kind_of(ident)
            if kind == "key":
                put(ident, "hand-over-revoke" if verb == "include" else "keep")
            else:
                put(ident, verb)
    for verb, decision in (("includeGroup", "include"), ("excludeGroup", "exclude")):
        group = changes.get(verb)
        if group is None:
            continue
        if group not in GROUPS:
            raise Refusal("invalid-group", f"a group is one of: {', '.join(GROUPS)}")
        # The members as the plan lists them: an item found since the record (`new`) is shown apart and never
        # taken by a group choice (ORG-R16).
        members = sorted(i for i, c in cands.items() if c["group"] == group and c["kind"] != "key"
                         and not sel["items"].get(i, {}).get("new"))
        for ident in members:
            if named.get(ident) not in (None, verb.removesuffix("Group")):
                raise Refusal("invalid-change", f"{_echo(ident)} is named by `{named[ident]}` and is in the group "
                                                f"`{verb}` takes; decide each item once per change")
            put(ident, decision, by="group", group=group)
    for ident, choice in (changes.get("keys") or {}).items():
        if kind_of(ident) != "key":
            raise Refusal("unknown-item", f"{_echo(ident)} is not a key; a key's choice is one of "
                                          f"{', '.join(KEY_CHOICES)}")
        if choice not in KEY_CHOICES:
            raise Refusal("invalid-choice", f"a key's choice is one of: {', '.join(KEY_CHOICES)}")
        put(ident, choice)
    for ident, text in (changes.get("labels") or {}).items():
        if kind_of(ident) != "project":
            raise Refusal("unknown-item", f"{_echo(ident)} is not a project; only a project carries a label")
        # A label changes what the organization reads, never whether the project goes (ORG-R02): a project nobody
        # chose stays out, and an item found since stays new until a person decides it.
        put(ident, None, None, label_only=True, label="" if text == "" else _label_ok(text))
    draft["updatedAt"] = _now()
    # Each change is its own draft, named at random: a revision removes the draft it consumed by this name, never
    # one changed since (two changes can fall in one second).
    draft["id"] = secrets.token_hex(8)
    return draft


# ── apply, undo, confirm ───────────────────────────────────────────────────────

def _steps(item: dict, layout: str) -> list[dict]:
    """The journal's steps for one item (ORG-R12), in order; a step with nothing to wait on is `done`."""
    def waiting(step: str, on: str, **more: Any) -> dict:
        return {"step": step, "state": "waiting", "waitingOn": on, **more}

    if item.get("notFound"):
        return []                                      # this space no longer knows it: nothing to do for it here

    if item["kind"] == "project" and item["decision"] == "include":
        steps = [waiting("hand-to-fabric", "fabric-adaptation-plan"),
                 {"step": "entry-signed", "state": "planned", "waitingOn": "fabric-entry-revision",
                  "humanStep": "passkey-entry"}]
        if layout == "separate-space":
            steps.append(waiting("export-curated-entry", "space-export"))
        return steps
    if item["kind"] == "key" and item["decision"] != "keep":
        steps = [waiting("credential-request", "credential-requests"),
                 {"step": "switch-consumers", "state": "planned", "waitingOn": "rotation"}]
        if item["decision"] == "hand-over-revoke":
            steps.append({"step": "revoke-personal", "state": "planned", "cannotBeUndone": True,
                          "waitingOn": "replacement-confirmation" if layout == "separate-space" else "rotation"})
        return steps
    if item["kind"] == "key":
        if layout == "this-space":
            # The space becomes the organization's: a personal key stays personal by moving out first (OBS-426).
            return [{"step": "stays-personal", "state": "waiting", "waitingOn": "move-out"}]
        if layout == "one-space":
            # Both contexts in one space: a personal key stays personal once every slot carries its context (OBS-359).
            return [{"step": "stays-personal", "state": "waiting", "waitingOn": "context-binding"}]
        return [{"step": "stays-personal", "state": "done"}]
    if item["kind"] == "account" and item["decision"] == "include":
        return [{"step": "recorded", "state": "done"}]
    return []


def _layout_steps(layout: str) -> list[dict]:
    on = {"separate-space": ("create-space", "space-create"), "this-space": ("move-out", "move-out"),
          "one-space": ("bind-contexts", "context-binding")}[layout]
    return [{"step": on[0], "state": "waiting", "waitingOn": on[1]}]


def _journal_for(record: dict) -> dict:
    at = record["decidedAt"]
    items = []
    if record.get("layout"):
        items.append({"id": "layout:" + record["layout"], "kind": "layout",
                      "steps": [{**s, "at": at} for s in _layout_steps(record["layout"])]})
    for item in record["items"]:
        steps = _steps(item, record.get("layout"))
        if steps:
            items.append({"id": item["id"], "kind": item["kind"], "steps": [{**s, "at": at} for s in steps]})
    return {"schema": JOURNAL_SCHEMA, "organization": record["organization"], "revision": record["revision"],
            "recordDigest": record["digest"], "items": items}


def counts(jdoc: dict | None) -> dict:
    """Per kind, how many items are done, failed, waiting, planned or undone (ORG-R25): an item is as far as its
    least finished step."""
    out: dict[str, dict] = {}
    order = ("failed", "waiting", "planned", "undone", "done")
    for item in (jdoc or {}).get("items") or []:
        states = {s.get("state") for s in item.get("steps") or []}
        worst = next((s for s in order if s in states), "done")
        row = out.setdefault(item.get("kind", "other"), {s: 0 for s in STEP_STATES})
        row[worst] += 1
    return out


def _record(org: str, sel: dict, digest: str, prior: dict | None, who: dict, *, kind: str = "select",
            undoes: str | None = None, consumed: str | None = None) -> dict:
    refs = {}
    for rec in history(org):
        for i in rec.get("items") or []:
            if i.get("ref"):
                refs[i["id"]] = i["ref"]
    items = []
    for i in _items_out(sel):
        if not i.get("inForm", True):
            # Not in the form (found since and not decided by anyone, ORG-R16): left out of the record, so it stays
            # listed (and new, when the rules propose it) until a person decides it.
            continue
        row = {k: i[k] for k in ("id", "kind", "decision", "why", "by") if k in i}
        for k in ("label", "group", "decidedBy", "labelledBy", "project", "slot", "class", "provider", "name"):
            if i.get(k) is not None:
                row[k] = i[k]
        if row["kind"] == "project" and row["decision"] == "include":
            # ORG-R03: random per organization, never derived from the estate; the same project keeps its reference.
            row["ref"] = refs.get(row["id"]) or i.get("ref") or secrets.token_hex(16)
        items.append(row)
    space = _space_here()
    serves = ({"space": sel["serves"], "here": False} if sel["layout"] == "separate-space"
              else {"space": None, "here": True, **space})
    body = {"schema": SCHEMA, "organization": org, "revision": (prior or {}).get("revision", 0) + 1,
            "predecessor": (prior or {}).get("digest"), "kind": kind, "layout": sel["layout"], "madeIn": space,
            "serves": serves, "decidedAt": _now(), "decidedBy": who, "selectionDigest": digest, "items": items}
    if undoes:
        body["undoes"] = undoes
    if consumed:
        # The draft this revision consumed, by its name: only that draft is removed after the commit, never one the
        # person changed since (ORG-R12, an interrupted apply resumed later).
        body["consumedDraft"] = consumed
    body["digest"] = _digest(body)
    return body


#: What plan and show say while the last apply or undo has not finished writing (ORG-R12).
RESUME_STEP = ("the last apply or undo stopped before it finished (its journal, or the revision before it, is not "
               "settled): apply the record's own form to finish it; any apply or undo also finishes it first")

#: Steps that cannot be taken back once done: a project's entry signed in Fabric (ORG-R13) and a personal value revoked
#: at its provider (ORG-R08). A revision that would drop an item with one of them done is refused.
IRREVERSIBLE = ("entry-signed", "revoke-personal")


def _supersede(org: str, prior: dict | None, new: dict | None) -> None:
    """The prior revision's journal after a new revision (`new`, its journal): a step the new journal no longer has
    for the same item is `undone` (nothing of it left this computer); a step it has carries over there. An
    irreversible step that is done stays done: what happened is never rewritten."""
    if not prior:
        return
    jdoc = journal(org, prior["revision"], prior["digest"])
    if not jdoc:
        return
    now = {i["id"]: {st["step"]: st.get("waitingOn") for st in i["steps"]} for i in (new or {}).get("items") or []}
    changed = False
    for item in jdoc["items"]:
        kept = now.get(item["id"], {})
        for st in item["steps"]:
            if st["state"] not in ("planned", "waiting", "done"):
                continue
            # Carried as it was: the same step, waiting on the same thing.
            if st["step"] in kept and kept[st["step"]] == st.get("waitingOn"):
                continue
            if st["step"] in IRREVERSIBLE and st["state"] == "done":
                continue
            st["state"], st["at"], changed = "undone", _now(), True
    if changed:
        _write(_journal_path(org, prior["revision"]), jdoc)


def _irreversible(org: str, prior: dict | None, sel: dict) -> None:
    """Refuse a new revision in which an item whose irreversible step is done would lose that step (ORG-R13): a
    project already in the organization's context comes back only by a take-back (OBS-419), and a revoked personal
    value cannot be kept personal. Same decision and the same step in the new form: the step carries over."""
    if not prior:
        return
    jdoc = journal(org, prior["revision"], prior["digest"]) or {}
    q = CMD.format(org=_q(org))
    for item in jdoc.get("items") or []:
        done = [st["step"] for st in item["steps"] if st["step"] in IRREVERSIBLE and st["state"] == "done"]
        if not done:
            continue
        after = sel["items"].get(item["id"])
        steps = {st["step"] for st in _steps(after, sel.get("layout"))} if after else set()
        if all(d in steps for d in done):
            continue
        if "entry-signed" in done:
            raise Refusal("entry-signed", f"{item['id']} is already in the organization's context (your passkey "
                                          "signed its entry in Fabric); taking it back is a new estate revision "
                                          "your passkey signs (OBS-419)",
                          f"{q} show")
        raise Refusal("already-revoked", f"{item['id']}'s personal value is already revoked at its provider; the "
                                         "selection cannot keep it personal", f"{q} show")


def apply(org: str, digest: Any, *, decision: Any = None, caller: str = "cli") -> dict:
    """Write the selection as the next revision and start its journal, only in the form the person previewed
    (ORG-R14). The same form again changes nothing (ORG-R12)."""
    org_ok = _org_checked(org)
    q = CMD.format(org=_q(org_ok))
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise Refusal("not-previewed", "apply takes the digest the preview showed, so only the form the person saw is "
                                       "applied", f"{q} plan")
    # The gate first: nothing is written, not a lock nor a resume, for a call that is not the person's.
    who = decided("apply", org_ok, decision, caller=caller)
    with _locked(org_ok, every=True):
        record = current(org_ok)
        if record and _unfinished(org_ok, record):
            # ORG-R12: finish what the last apply or undo left before anything else. Its own form is all it takes.
            _resume(org_ok, record)
            if digest == record["selectionDigest"]:
                return _apply_answer(org_ok, record, "resumed", who)
        draft = _draft(org_ok)
        ctx = _context(org_ok, _chosen_projects(draft, record))
        sel = _effective(org_ok, ctx, draft, record)
        now_digest = selection_digest(org_ok, sel)
        if digest != now_digest:
            raise Refusal("changed-since-preview", "the selection changed since that preview (a change, a new rule, "
                                                   "a project or key found since): show the operator the plan again",
                          f"{q} plan")
        if record and record.get("selectionDigest") == now_digest:
            _resume(org_ok, record)
            return _apply_answer(org_ok, record, "unchanged", who)
        _complete(org_ok, ctx, sel)
        if sel["layout"] is None:
            raise Refusal("no-layout", "choose where the organization's work lives before anything is applied "
                                       "(ORG-R29)", f"{q} plan --layout {RECOMMENDED_LAYOUT} --serves SPACE")
        if sel["layout"] == "separate-space" and not sel["serves"]:
            raise Refusal("no-serving-space", "name the space this selection serves", f"{q} plan --serves SPACE")
        _clash(org_ok, sel["items"].values())
        foreign = _elsewhere(org_ok, ctx, sel["items"].values())
        if foreign:
            raise Refusal(foreign[0]["code"], f"{foreign[0]['project']} is {foreign[0]['why']}",
                          f"{q} plan --exclude {foreign[0]['project']}")
        _irreversible(org_ok, record, sel)
        _unused(org_ok, "apply", who)
        rec = _record(org_ok, sel, now_digest, record, who, consumed=(draft or {}).get("id"))
        _commit(org_ok, record, rec)
    return _apply_answer(org_ok, rec, "applied", who)


def _complete(org: str, ctx: dict, sel: dict) -> None:
    """Nothing is applied from a partial picture of what is chosen: the registry of projects must read (a project's
    identity, and which keys are its, come from it), and every chosen item must still be known here. A degraded
    provider read does not stop an apply; these two do, because they would change what the record says silently."""
    q = CMD.format(org=_q(org))
    if any(d.get("source") == "vault" for d in ctx["degraded"]):
        raise Refusal("vault-unreadable", "the vault's slots do not list, so which keys go is not known; nothing is "
                                          "applied until they list (no value is read either way)",
                      "project-observatory full doctor")
    if any(d.get("source") == "registry/projects.json" for d in ctx["degraded"]):
        raise Refusal("registry-unreadable", "registry/projects.json does not read, so which projects and keys are "
                                             "chosen is not known; nothing is applied until it reads",
                      "project-observatory full doctor")
    unread = sorted((i["id"], i["unread"]) for i in sel["items"].values() if i.get("unread")
                    and i["decision"] not in ("exclude", "keep"))
    if unread:
        raise Refusal("source-unreadable", f"{unread[0][0]} is chosen and its source ({unread[0][1]}) did not read this "
                                           "time, so whether it is still here is not known; nothing is applied until "
                                           "it reads (the choice stays)", "project-observatory full doctor")
    gone = sorted(i["id"] for i in sel["items"].values() if i.get("notFound")
                  and i["decision"] not in ("exclude", "keep"))
    if gone:
        raise Refusal("items-not-found", f"{gone[0]} is chosen and no longer found here (the registry or the vault "
                                         f"changed); leave it out, or restore it, then preview again",
                      f"{q} plan --exclude {gone[0]}")


def _commit(org: str, prior: dict | None, rec: dict) -> None:
    """Write a revision so that an interruption at any point resumes (ORG-R12): the journal first (a revision is
    never without its journal), then the revision (the commit point), then the prior journal's superseded steps and
    the consumed draft, which `_resume` finishes when the same form is applied again."""
    jdoc = _journal_for(rec)
    _carry(org, prior, jdoc)
    _write(_journal_path(org, rec["revision"]), jdoc)
    _write(_folder(org) / "revisions" / f"{rec['revision']:06d}.json", rec)
    _supersede(org, prior, jdoc)
    _drop_consumed(org, rec)


def _drop_consumed(org: str, rec: dict) -> None:
    """Remove the draft this revision consumed, and only that one."""
    try:
        draft = _draft(org, consumed_ok=True)
    except Refusal:
        return                                         # a damaged draft is the person's to drop (`plan --reset`)
    if draft is not None and rec.get("consumedDraft") and draft.get("id") == rec["consumedDraft"]:
        (_folder(org) / "draft.json").unlink(missing_ok=True)


def _resume(org: str, rec: dict) -> None:
    """Finish what an interrupted `_commit` left: a missing journal, the prior journal's superseded steps, the
    draft. Each part is idempotent."""
    revs = history(org)
    prior = revs[-2] if len(revs) >= 2 else None
    jdoc = journal(org, rec["revision"], rec["digest"])
    if jdoc is None:
        jdoc = _journal_for(rec)
        _carry(org, prior, jdoc)
        _write(_journal_path(org, rec["revision"]), jdoc)
    _supersede(org, prior, jdoc)
    _drop_consumed(org, rec)


def _unfinished(org: str, rec: dict | None) -> bool:
    """Whether the last apply stopped after its revision was written (ORG-R12): its journal missing, or a step of the
    prior journal neither carried nor undone. (A consumed draft left behind reads as the applied form with a draft:
    the plan's answer names that.)"""
    if not rec:
        return False
    jdoc = journal(org, rec["revision"], rec["digest"])
    if jdoc is None and (rec["items"] or rec.get("layout")):
        return True
    try:
        left = _draft(org, consumed_ok=True)
    except Refusal:
        left = None
    if left is not None and rec.get("consumedDraft") and left.get("id") == rec["consumedDraft"]:
        return True                                    # the draft the revision consumed is still there
    revs = history(org)
    prior = revs[-2] if len(revs) >= 2 else None
    old = journal(org, prior["revision"], prior["digest"]) if prior else None
    now = {i["id"]: {st["step"]: st.get("waitingOn") for st in i["steps"]} for i in (jdoc or {}).get("items") or []}

    def carried(item_id: str, st: dict) -> bool:
        kept = now.get(item_id, {})
        return st["step"] in kept and kept[st["step"]] == st.get("waitingOn")
    return any((st["state"] in ("planned", "waiting")
                or (st["state"] == "done" and st["step"] not in IRREVERSIBLE))
               and not carried(i["id"], st)
               for i in (old or {}).get("items") or [] for st in i["steps"])


def _carry(org: str, prior: dict | None, jdoc: dict) -> None:
    """What already happened carries into the new journal, step by step (a step done or failed, a confirmation), for
    each item the new revision still has with that step: a changed selection or layout never resets it. A step still
    planned or waiting takes the new revision's form, so what it waits on follows the new layout (a confirmation is
    asked only in a separate space, ORG-R08)."""
    if not prior:
        return
    old = {i["id"]: {st["step"]: st for st in i["steps"]}
           for i in (journal(org, prior["revision"], prior["digest"]) or {}).get("items") or []}
    for item in jdoc["items"]:
        before = old.get(item["id"]) or {}
        for new in item["steps"]:
            was = before.get(new["step"])
            if not was:
                continue
            if was.get("state") in ("done", "failed") and (
                    new["step"] in IRREVERSIBLE or was.get("waitingOn") == new.get("waitingOn")):
                # What happened carries when the new form has the same step waiting on the same thing; an
                # irreversible step always carries. A step that now waits on something else (a key kept personal
                # that must first move out of this space) starts again in the new form: never read as done.
                new.clear()
                new.update(copy.deepcopy(was))
            elif was.get("confirmedAt") and new.get("waitingOn") == "replacement-confirmation":
                new.update(confirmedAt=was["confirmedAt"], confirmedBy=was.get("confirmedBy"), waitingOn="rotation")


def _unused(org: str, action: str, who: dict) -> None:
    """The operator's words for one apply or undo decide that one act: the same words (their action, sentence and
    time) already recorded for a revision are not a decision for another form (ORG-R14)."""
    if who.get("via") != "relayed-instruction":
        return
    for rec in history(org):
        was = rec.get("decidedBy") or {}
        if (was.get("action"), was.get("instruction"), was.get("at")) == (who.get("action"), who.get("instruction"),
                                                                             who.get("at")):
            raise Refusal("decision-invalid", f"these words already decided revision {rec['revision']}; ask the "
                                              f"operator about this {action} and send their new words",
                          gate=GATES[action], action=ACTIONS[action])


def _apply_answer(org: str, rec: dict, outcome: str, who: dict) -> dict:
    jdoc = journal(org, rec["revision"], rec["digest"])
    c = counts(jdoc)
    waiting = any(r["waiting"] or r["planned"] for r in c.values())
    failed = any(r["failed"] for r in c.values())
    empty = not rec.get("items") and not rec.get("layout")
    state = "error" if failed else "waiting" if waiting else "empty" if empty else "complete"
    return {"schema": VIEW_SCHEMA, "answer": "apply", "organization": org, "outcome": outcome,
            "state": state, "revision": rec["revision"], "digest": rec["digest"],
            "selectionDigest": rec["selectionDigest"], "decidedBy": rec["decidedBy"], "calledBy": who,
            "counts": c, "waitingOn": _waiting_on(jdoc), "fabricItems": fabric_items(rec),
            "humanSteps": [{"step": h, "what": HUMAN_STEPS[h]} for h in _human(rec)],
            "nextStep": ("a step failed: `show` names it and its command" if failed else
                         "nothing is sent and nothing moved that has no door yet; each waiting step names what it "
                         "waits on" if waiting else "nothing is selected; the next plan starts from the defaults"
                         if empty else "every item is done"),
            "command": f"{CMD.format(org=_q(org))} show", "degraded": []}


def _human(rec: dict) -> list[str]:
    out = []
    if any(i["kind"] == "project" and i["decision"] == "include" for i in rec.get("items") or []):
        out.append("passkey-entry")
    if rec.get("layout") == "separate-space":
        out += ["admin-password", "computer-token"]
    return out


def _waiting_on(jdoc: dict | None) -> list[dict]:
    seen: dict[str, int] = {}
    for item in (jdoc or {}).get("items") or []:
        for s in item["steps"]:
            if s["state"] in ("waiting", "planned") and s.get("waitingOn"):
                seen[s["waitingOn"]] = seen.get(s["waitingOn"], 0) + 1
    return [{"on": k, "items": n, **DEPENDS.get(k, {})} for k, n in sorted(seen.items())]


def fabric_items(rec: dict | None) -> list[dict]:
    """The `org-adaptation/1` items this record would hand to Fabric (ORG-R03): a reference, the kind, and a label
    only where the person typed one. Nothing else about a project is in them, and nothing hands them over yet."""
    out = []
    for i in (rec or {}).get("items") or []:
        if i.get("kind") == "project" and i.get("decision") == "include" and i.get("ref"):
            item = {"ref": i["ref"], "kind": "project"}
            if i.get("label"):
                item["label"] = i["label"]
            out.append(item)
    return sorted(out, key=lambda x: x["ref"])


def undo(org: str, *, decision: Any = None, caller: str = "cli") -> dict:
    """Restore the revision before the current one, exactly, as a new revision (ORG-R13). Refused once a project's
    entry revision is signed in Fabric: a project then comes back only by a take-back (OBS-419)."""
    org_ok = _org_checked(org, configured=False)
    q = CMD.format(org=_q(org_ok))
    if not _revisions(org_ok):
        raise Refusal("nothing-to-undo", "no selection has been applied for this organization", f"{q} plan")
    who = decided("undo", org_ok, decision, caller=caller)         # the gate before anything is written
    with _locked(org_ok, every=True):
        revs = history(org_ok)
        if not revs:
            raise Refusal("nothing-to-undo", "no selection has been applied for this organization", f"{q} plan")
        if _unfinished(org_ok, revs[-1]):
            _resume(org_ok, revs[-1])                  # ORG-R12: the last apply or undo finishes first
        cur = revs[-1]
        jdoc = journal(org_ok, cur["revision"], cur["digest"])
        signed = [i["id"] for i in (jdoc or {}).get("items") or []
                  if any(s["step"] == "entry-signed" and s["state"] == "done" for s in i["steps"])]
        if signed:
            raise Refusal("entry-signed", f"{signed[0]} is already in the organization's context (your passkey signed "
                                          "its entry in Fabric); taking it back is a new estate revision your "
                                          "passkey signs (OBS-419, not built yet)", f"{q} show")
        if cur.get("kind") == "undo":
            # Undoing an undo restores what the undo removed: the revision it undid.
            restored = next((r for r in revs if r["digest"] == cur.get("undoes")), None)
        else:
            restored = revs[-2] if len(revs) >= 2 else None
        if restored is not None:
            sel = {"items": {i["id"]: dict(i) for i in restored["items"]}, "layout": restored.get("layout"),
                   "serves": (restored.get("serves") or {}).get("space")}
        else:
            sel = {"items": {}, "layout": None, "serves": None}
        # What is restored meets the same checks as an apply: a project another organization took meanwhile
        # (ORG-R05), a personal value already revoked.
        _clash(org_ok, sel["items"].values())
        try:
            ctx = _context(org_ok, sorted(i for i, r in sel["items"].items()
                                          if r["kind"] == "project" and r["decision"] == "include"))
        except Refusal as refusal:
            if refusal.code not in ("no-organizations", "unknown-organization", "default-organization"):
                raise                                  # a file that does not read is not "not configured"
            ctx = None                                 # not configured any more: a record outlives its rules
        foreign = _elsewhere(org_ok, ctx, sel["items"].values()) if ctx else []
        if foreign:
            raise Refusal(foreign[0]["code"], f"{foreign[0]['project']} is {foreign[0]['why']}", f"{q} show")
        if ctx is None:
            import org_view
            unread_registry: list = []
            listed = {p["id"] for p in org_view._project_rows(org_view._registry("projects.json", unread_registry, ""))}
            if unread_registry and any(r["kind"] == "project" and r["decision"] == "include"
                                       for r in sel["items"].values()):
                raise Refusal("registry-unreadable", "registry/projects.json does not read, so whether the projects "
                                                     "that would come back are still here is not known; nothing is "
                                                     "undone until it reads", "project-observatory full doctor")
            gone = sorted(i for i, r in sel["items"].items() if r["kind"] == "project" and r["decision"] == "include"
                          and i not in listed)
            if gone:
                raise Refusal("items-not-found", f"{gone[0]} would come back and is no longer in the registry",
                              f"{q} show")
        if ctx is not None and restored is not None:
            # The restored form as a plan would read it now (items found since listed apart), so the plan reads it
            # applied afterwards: apply and undo define the digest the same way.
            pseudo = {"items": restored["items"], "layout": restored.get("layout"), "serves": restored.get("serves")}
            sel = _effective(org_ok, ctx, None, pseudo)
            try:
                _complete(org_ok, ctx, sel)
            except Refusal as refusal:
                raise Refusal(refusal.code, f"undo cannot restore the revision before: {refusal}; apply a new "
                                            "selection instead", f"{q} plan") from None
        _irreversible(org_ok, cur, sel)
        _unused(org_ok, "undo", who)
        try:
            pending = (_draft(org_ok) or {}).get("id")
        except Refusal:
            pending = None
        # A pending draft was built on the revision being undone: undo drops it with that revision.
        rec = _record(org_ok, sel, selection_digest(org_ok, sel), cur, who, kind="undo", undoes=cur["digest"],
                      consumed=pending)
        _commit(org_ok, cur, rec)
    answer = _apply_answer(org_ok, rec, "undone", who)
    answer["answer"] = "undo"
    answer["draftDropped"] = bool(pending)
    answer["restored"] = None if restored is None else {"revision": restored["revision"], "digest": restored["digest"]}
    return answer


def confirm_replacement(org: str, key: Any, *, decision: Any = None, caller: str = "cli") -> dict:
    """Record the person's confirmation that the organization's replacement for `key` works (ORG-R08): in the
    separate-space layout the personal space cannot read the organization's, so the revocation waits for it. The
    revocation itself is OBS-416's rotation door, not built: the step then waits on `rotation`."""
    org_ok = _org_checked(org, configured=False)
    q = CMD.format(org=_q(org_ok))
    import credential_shape
    if not isinstance(key, str) or not key.strip() or credential_shape.shaped(key):
        raise Refusal("not-in-record", "confirm-replacement names the key by its slot: vault:FOLDER/ENV/NAME, never "
                                       "a value", f"{q} show")
    ident = key if key.startswith("vault:") else f"vault:{key}"
    if not _revisions(org_ok):
        raise Refusal("not-in-record", "no selection has been applied for this organization", f"{q} plan")
    # The object is the organization's key: words about the same slot in another organization's record do not count.
    who = decided("confirm-replacement", f"{org_ok}:{ident}", decision, caller=caller)
    with _locked(org_ok):
        rec = current(org_ok)
        item = next((i for i in (rec or {}).get("items") or [] if i["id"] == ident and i["kind"] == "key"), None)
        if item is None:
            raise Refusal("not-in-record", "that key is not in the applied selection", f"{q} show")
        if item["decision"] != "hand-over-revoke":
            raise Refusal("not-revoking", "the selection keeps that key's personal value, so there is no revocation to "
                                          "confirm", f"{q} plan --key {ident}=hand-over-revoke")
        if rec.get("layout") != "separate-space":
            raise Refusal("confirmation-not-needed", "in one space the journal's own signal (replacement received, "
                                                     "consumers switched) is the confirmation; nothing is asked of "
                                                     "you", f"{q} show")
        if journal(org_ok, rec["revision"], rec["digest"]) is None:
            _resume(org_ok, rec)                       # an interrupted apply first, with what it carries
        jdoc = journal(org_ok, rec["revision"], rec["digest"])
        entry = next((i for i in jdoc["items"] if i["id"] == ident), None)
        if entry is None or not any(st.get("step") == "revoke-personal" for st in entry.get("steps") or []):
            raise Refusal("record-unreadable", "the journal of the applied revision has no revocation step for this key "
                                               "(it was changed by hand)", "project-observatory full doctor")
        before = {st["step"]: st["state"] for st in entry["steps"]}
        if before.get("credential-request") != "done" or before.get("switch-consumers") != "done":
            # ORG-R08: a replacement that has not arrived, or consumers not switched to it, cannot be confirmed to
            # work; a confirmation now would release the revocation the moment the rotation door exists.
            raise Refusal("replacement-not-received", "the organization's replacement for this key has not arrived, "
                                                      "or its consumers have not switched to it (apply files no "
                                                      "request yet, the exchange with the organization is OBS-454 "
                                                      "and the rotation OBS-416); "
                                                      "nothing to confirm", f"{q} show")
        step = next(s for s in entry["steps"] if s["step"] == "revoke-personal")
        outcome = "already-confirmed" if step.get("confirmedAt") else "confirmed"
        if outcome == "confirmed":
            step.update(confirmedAt=_now(), confirmedBy=who, waitingOn="rotation")
            _write(_journal_path(org_ok, rec["revision"]), jdoc)
    return {"schema": VIEW_SCHEMA, "answer": "confirm-replacement", "organization": org_ok, "key": ident,
            "outcome": outcome, "state": "waiting", "step": step, "waitingOn": [{"on": "rotation", **DEPENDS["rotation"]}],
            "nextStep": "the personal value is revoked once the rotation door exists (OBS-416); it keeps working until "
                        "then", "command": f"{q} show", "degraded": []}


# ── show ───────────────────────────────────────────────────────────────────────

def show(org: str) -> dict:
    """The applied selection, its journal with counts per kind (ORG-W8, ORG-R25), its history, the items Fabric
    would receive, and what is new since it (ORG-R16). Read-only."""
    org_ok = _org_checked(org, configured=False)
    q = CMD.format(org=_q(org_ok))
    degraded: list[dict] = []
    try:
        revs = history(org_ok)
    except Refusal as refusal:
        return {"schema": VIEW_SCHEMA, "answer": "show", "organization": org_ok, "state": "error",
                "error": refusal.code, "message": str(refusal), "nextStep": "the selection's record does not read: "
                + str(refusal), "command": refusal.command or "project-observatory full doctor", "degraded": []}
    try:
        draft = _draft(org_ok)
    except Refusal as refusal:
        # A damaged draft is only the draft: the applied record stays readable beside it.
        draft = None
        degraded.append({"source": "store/org-selection draft", "code": refusal.code, "reason": str(refusal)[:200],
                         "effect": "the draft is ignored; drop it with " + (refusal.command or f"{q} plan --reset")})
    if not revs:
        return {"schema": VIEW_SCHEMA, "answer": "show", "organization": org_ok, "state": "empty", "record": None,
                "draft": draft is not None, "history": [], "nextStep": "no selection is applied for this organization",
                "command": f"{q} plan", "degraded": degraded}
    rec = revs[-1]
    try:
        jdoc = journal(org_ok, rec["revision"], rec["digest"])
    except Refusal as refusal:
        return {"schema": VIEW_SCHEMA, "answer": "show", "organization": org_ok, "state": "error",
                "error": refusal.code, "message": str(refusal), "nextStep": "the journal of the applied revision "
                "does not read: " + str(refusal), "command": "project-observatory full doctor", "degraded": []}
    new: list[str] = []
    try:
        hand = [i["id"] for i in rec["items"] if i["kind"] == "project" and i["decision"] == "include"]
        ctx = _context(org_ok, hand)
        sel = _effective(org_ok, ctx, None, rec)
        new = sorted(i["id"] for i in sel["items"].values() if i.get("new"))
        degraded = degraded + [d for d in ctx["degraded"] if d not in degraded]
    except Refusal as refusal:
        degraded.append({"source": "config/organizations.json", "code": refusal.code, "reason": str(refusal),
                         "effect": "what is new since the record is not listed"})
    c = counts(jdoc)
    failed = any(r["failed"] for r in c.values())
    waiting = any(r["waiting"] or r["planned"] for r in c.values())
    state = "error" if failed else "waiting" if waiting else "partial" if degraded else "complete"
    step = (f"{len(new)} item(s) found since this record: proposed, never chosen by themselves"
            if new else "each waiting step names what it waits on")
    command = f"{q} plan"
    if not rec["items"] and not rec.get("layout"):
        state = "empty"
        step = "nothing is selected; the next plan starts from the defaults"
    if _unfinished(org_ok, rec):
        # An apply or undo stopped after its revision was written (ORG-R12): never shown as done or empty. The
        # record's own digest finishes it whatever the plan reads now (`apply` resumes before anything else).
        state, step = "waiting", RESUME_STEP
        command = f"{q} apply --digest {rec['selectionDigest']}"
    return {"schema": VIEW_SCHEMA, "answer": "show", "organization": org_ok, "state": state, "record": rec,
            "journal": jdoc, "counts": c, "waitingOn": _waiting_on(jdoc), "fabricItems": fabric_items(rec),
            "humanSteps": [{"step": h, "what": HUMAN_STEPS[h]} for h in _human(rec)],
            "history": [{"revision": r["revision"], "kind": r["kind"], "digest": r["digest"],
                         "decidedAt": r["decidedAt"], "by": r["decidedBy"].get("by"), "via": r["decidedBy"].get("via")}
                        for r in revs],
            "new": new, "draft": draft is not None, "nextStep": step, "command": command, "degraded": degraded}


def _org_label(org: str) -> str:
    """The organization's label as config/organizations.json gives it, else its name."""
    try:
        import org_view
        _doc, assigner = org_view.organization_checked(org)
        return org_view._label(assigner.orgs.get(org)) or org
    except Exception:  # noqa: BLE001 - a label is cosmetic: the name always reads
        return org


def new_since_all() -> list[dict]:
    """Every organization with an applied selection and what was found since it (ORG-R16), for the findings board.
    An organization that cannot be read is skipped: the board names nothing it did not read."""
    out = []
    base = root()
    if not base.is_dir() or base.is_symlink():
        return out
    try:
        entries = _store_folders(base)
    except OSError:
        return out
    for folder in (p for p in entries if p.is_dir() and not p.is_symlink()):
        try:
            revs = _other_revisions(folder)
            if not revs:
                continue
            org = (_read(revs[-1]) or {}).get("organization")
            if not isinstance(org, str) or _folder(org) != folder:
                continue
            view = show(org)
        except Exception:  # noqa: BLE001 - one organization that cannot be read never costs the others their row
            continue
        if view.get("new"):
            out.append({"organization": org, "new": view["new"], "label": _org_label(org)})
    return out


# ── text ──────────────────────────────────────────────────────────────────────

def text(view: dict) -> str:
    """`full org select` without `--json`: ORG-W5 to ORG-W8 as lines."""
    a = view.get("answer")
    if a == "plan":
        p = view["preview"]
        lines = [f"What goes to {view['label']} ({view['state']})",
                 f"  layout: {view['layout'] or 'not chosen (recommended: ' + view['recommendedLayout'] + ')'}"
                 + (f", for the space {view['serves']}" if view.get("serves") else "")]
        for kind, title in (("project", "Projects"), ("key", "Keys"), ("account", "Accounts")):
            rows = [i for i in view["items"] if i["kind"] == kind]
            if not rows:
                continue
            lines.append(f"{title} ({len(rows)})")
            for i in rows:
                mark = "[x]" if i["decision"] not in ("exclude", "keep") else "[ ]"
                extra = f" -> {i['decision']}" if kind == "key" else ""
                tag = (f" (new: the record of {view['newSince']} does not choose it)" if view.get("newSince")
                       else " (new)") if i.get("new") else ""
                if i.get("notFound"):
                    tag += " (no longer found here)"
                if i.get("unread"):
                    tag += f" (its source did not read: {i['unread']})"
                label = f", label for the organization: {i['label']!r}" if i.get("label") else ""
                lines.append(f"  {mark} {i['id']}{extra}{tag}{label}: {i['why']}")
        for c in view["conflicts"]:
            lines.append(f"conflict: {c['project']}: {c['why']}")
        lines.append(f"The organization would receive {p['organization']['projects']} project item(s) "
                     f"({p['organization']['labelled']} with a name you typed); {p['keysRequested']} key(s) asked "
                     f"for, {p['keysRevoked']} personal value(s) revoked once the replacement works (cannot be undone)")
        lines.append(p["staysPersonal"]["line"] + ".")
        for h in p["humanSteps"]:
            lines.append(f"your step: {h['what']}")
        lines.append(f"digest: {view['digest']}")
        lines.append(f"next: {view.get('nextStep', '')}\n  {view.get('command', '')}")
    elif a == "show":
        lines = [f"Selection for {view['organization']} ({view['state']})"]
        if view.get("record"):
            r = view["record"]
            lines.append(f"  revision {r['revision']} ({r['kind']}), decided {r['decidedAt']} by "
                         f"{r['decidedBy'].get('by')} via {r['decidedBy'].get('via')}; layout {r.get('layout')}")
            for kind, row in sorted(view["counts"].items()):
                lines.append(f"  {kind}: " + ", ".join(f"{n} {s}" for s, n in row.items() if n))
            for w in view["waitingOn"]:
                lines.append(f"  not done ({w['items']}): {w['on']}: {w.get('what', '')} [{w.get('owner', '')}]")
        for n in view.get("new") or []:
            lines.append(f"  new since the record (not chosen): {n}")
        if view.get("error"):
            lines.append(f"  {view['error']}: {view['message']}")
        lines.append(f"next: {view.get('nextStep', '')}\n  {view.get('command', '')}")
    else:
        lines = [f"{a}: {view.get('outcome')} ({view.get('state')})"]
        if view.get("revision"):
            lines.append(f"  revision {view['revision']} {view['digest']}")
        for kind, row in sorted((view.get("counts") or {}).items()):
            lines.append(f"  {kind}: " + ", ".join(f"{n} {s}" for s, n in row.items() if n))
        for w in view.get("waitingOn") or []:
            lines.append(f"  not done ({w.get('items', 1)}): {w['on']}: {w.get('what', '')} [{w.get('owner', '')}]")
        lines.append(f"next: {view.get('nextStep', '')}\n  {view.get('command', '')}")
    for d in view.get("degraded") or []:
        lines.append(f"not read: {d.get('source')}: {d.get('reason')}")
    return "\n".join(lines)
