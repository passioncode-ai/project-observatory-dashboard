#!/usr/bin/env python3
"""A control plane for credentials that never carries one.

    T="$(project-observatory full-path)/tools"   # the installed engine's tools
    python "$T/keyserver.py"            serve on 127.0.0.1, print the URL, stay up
    python "$T/keyserver.py" --port N   a different port
    python "$T/keyserver.py" --once     answer one request and exit (for tests)

WHY THIS EXISTS AND WHY IT IS NARROW. The dashboard is a file read from
`file://`: it can show what is true and it cannot change anything. Minting,
capping and revoking a key are one command each, and a person reading a row
about a key that is about to die should not have to go and assemble that command
by hand. So the page is served here instead, same-origin with a small API.

**THE VALUE NEVER PASSES THROUGH THE BROWSER, and that is the whole design.**

  minting        the provider generates the key, this process writes it straight
                 to its destination file at mode 600. The browser learns a name
                 and a limit and never sees a character of it.
  capping        a number and a reset word.
  revoking       a label — the provider's truncated display form of the key,
                 which is not the key.
  marking a leak free text saying where it was seen.
  a setting      the Disk page's guard and cleanup settings (OBS-562): a key and a
                 number or a switch, planned, applied and undone through the
                 configuration door, which refuses anything shaped like a key.

  revealing      THE ONE EXCEPTION, and it is an exception to "the value never
                 passes through the browser" rather than to any of the rules
                 below. One named variable out of one env file, resolved against
                 `store/raw/env.json` rather than against the path in the
                 request — so this is a reader of a measured inventory, not a
                 file reader that happens to hold a token — audited before the
                 file is opened, with the path, the name and the class in the log
                 and never the value. It exists because the operator owns the
                 machine and the secret; the honest way to offer it is to make it
                 narrow and loud rather than to refuse it and leave them grepping.

  putting or rotating a VAULT secret is REFUSED here. `tools/vault.py`'s
  contract is that a value travels on stdin and nowhere else — not argv, not a
  chat message, and not an HTTP body a browser tab holds in memory and a
  devtools panel replays. The page hands over the command instead. A control
  plane that broke that rule to be convenient would be the largest hole in the
  system it was built to protect.

WHAT GUARDS THE API:

  bound to 127.0.0.1  never a routable address, and refused if asked for one
  the peer's account on a host with several OS accounts, another account's
                     connection is answered 403 and closed before its request is
                     read (`peer_check.py`, OBS-355)
  a token            in a file at mode 600, required on every API call in a
                     custom header — which also forces a CORS preflight, so a
                     page on another origin cannot post to it blind
  an Origin check    the browser's own statement of who is calling
  an audit line      every action, with what it acted on, appended before the
                     action is attempted — a log written afterwards loses the
                     one case that matters

Only the explicit, authenticated reveal route returns a credential value.
The state route serves metadata from `registry/credentials.json`; mutation
routes return a name, a label and an outcome.
"""

from __future__ import annotations
import argparse
import contextvars
import hmac
import html as html_module
import math
import socket
import http.server
import json
import os
import pathlib
import re
import secrets
import socketserver
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import osprivacy  # noqa: E402
import delegation  # noqa: E402
sys.path.insert(0, str(ROOT / "collectors"))
import credential_shape
import paths              
import peer_check  # noqa: E402  OBS-355: another OS account's connection refused before it is read

import stat
from runtime_identity import IdentityError, load as load_identity
sys.path.insert(0, str(ROOT / "tools"))
import key_consumers  # noqa: E402  OBS-594: the one consumer guard every revoke runs

TOKEN_FILE = paths.STATE / ".keyserver-token"
#: UNDER `STATE`, NOT `STORE` — the same knob `tools/use_secret.py` honours, and
#: until 2026-09-14 this one did not: every test that started a server wrote its
#: `reveal` rows into the LIVE journal, so the security record could not tell a
#: gate run from a person (16 rows on 2026-09-12 were most likely the suite).
AUDIT = paths.STATE / "logs" / "keyserver.jsonl"
HEADER = "X-Observatory-Token"
#: WHO IS ASKING. The token says the caller is on this machine and read a 600
#: file; it does not say which of the nine agent sessions on the machine it was.
#: Measured 2026-09-14: 33 reveals of one variable between 00:27Z and 06:29Z,
#: attributable only by correlating session files' mtimes. A caller names
#: itself in this header — `page:<name>` from the dashboard, a session id from
#: an agent — and a caller that does not is recorded as `unnamed`, which the
#: board's `secret.reveal_burst` rule then counts against it.
CALLER_HEADER = "X-Observatory-Caller"
_CALLER: contextvars.ContextVar[str] = contextvars.ContextVar("caller", default="unnamed")
#: WHO IS ASKING, PROVEN (OBS-62, Fabric ADR-0144). An agent presents a Fabric delegation token in
#: `Authorization: Bearer` or `Fabric-Delegation`, aud `project-observatory`, with the scope of the action
#: it calls; then the journal names the token's outermost actor as the caller, `sub` as the principal, the
#: chain and the grant, and `caller_verified` is true. The self-named header stays a label. The scopes and
#: effects are Fabric's (fabric-b4, 2026-10-11). `reveal` is never served to a token: a value must not enter
#: an agent's context, and `use_secret.py run` puts it in a child's environment instead.
ACTION_SCOPES = {"mint": ("keys.mint", "charge"), "limit": ("keys.limit", "change-policy"),
                 "disable": ("keys.disable", "change-policy"), "enable": ("keys.enable", "change-policy"),
                 "rotate-key": ("keys.rotate", "change-policy"), "revoke": ("keys.revoke", "delete"),
                 "leak": ("ledger.leak", "draft"), "annotate": ("ledger.annotate", "draft")}
#: The contract's HTTP status per refusal code (DL-19); every other code is 403.
REFUSAL_STATUS = {"unauthenticated": 401, "token-expired": 401, "principal-unproven": 401,
                  "issuer-unavailable": 503, "unknown-state": 503}
_IDENTITY: contextvars.ContextVar[dict | None] = contextvars.ContextVar("identity", default=None)
#: THE OPERATOR'S WORDS AN AGENT RELAYED (OBS-559, contract DEC-0121). The MCP tools `observatory_key_act` and
#: `observatory_credential_mark` call the acts below in process (`key_acts.py`); stdio proves no caller, so an act that
#: spends or cannot be undone carries the operator's decision instead of a delegation token, and the journal row keeps
#: it. Empty for the page and for an HTTP caller.
_RELAYED: contextvars.ContextVar[dict | None] = contextvars.ContextVar("relayed", default=None)
#: AN ACT'S EXCEPTION, BY TYPE (OBS-601). Every refusal this server answers is `{"error": {"code", "message"}}`, the
#: shape the MCP tools answer; `refusal` names the code for an act's exception, and the MCP key acts (`key_acts.py`)
#: take their codes from it too, so one refusal reads the same on the page's route and on the agent's tool. An act
#: that raises a ValueError or a LookupError whose whole text is a code (`agent-disabled`, `unknown-job`) is answered
#: with that code.
BARE_CODE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+")


def refusal(exc: BaseException) -> tuple[int, str]:
    """(HTTP status, code) for an act's exception."""
    if isinstance(exc, key_consumers.RevokeRefused):
        return 400, exc.code
    if isinstance(exc, urllib.error.HTTPError):
        return 502, "provider-refused"
    text = str(exc)
    if isinstance(exc, ValueError):
        return 400, text if BARE_CODE.fullmatch(text) else "invalid-argument"
    if isinstance(exc, LookupError):
        if BARE_CODE.fullmatch(text):
            return 404, text
        return 404, ("held" if text.startswith("held:") else
                     "provider-refused" if text.startswith("the provider refused") else "not-found")
    if isinstance(exc, RuntimeError):
        return 500, "unconfirmed"
    return 500, "failed"


def caller_name(raw: str | None) -> str:
    """The caller's own name, or `unnamed`. Printable, short, one token: a header
    is attacker-shaped input and this lands in a journal people grep."""
    if not raw:
        return "unnamed"
    s = re.sub(r"[^A-Za-z0-9._:@/+-]", "", raw.strip())[:80]
    return s or "unnamed"

#: The destinations `tools/install_key.py` owns. A mint may only land in one of
#: these: an arbitrary path from a request body would make this an arbitrary
#: file writer that happens to hold a provisioning key.
DESTINATIONS = {"observatory", "claude-mem"}

#: THE SECOND SHAPE OF DESTINATION: a slot in this estate's vault (plan v2,
#: T-27). The door has taken `--to vault:<project>/<env>/<NAME>` since it
#: existed and this server could not ask for one, so a key for a project could
#: only be issued from a terminal. The pattern is the whole guard, and it is
#: deliberately narrow: three segments, a lowercase project and env, and an
#: UPPER_SNAKE variable name — the shape `tools/vault.py` itself enforces. The
#: project must also be one this estate KNOWS, because a typo in a project name
#: does not fail here, it quietly creates a slot nothing will ever read.
VAULT_DEST = re.compile(r"^vault:(?P<project>[a-z0-9][a-z0-9._-]{0,63})"
                        r"/(?P<env>[a-z][a-z0-9-]{0,15})"
                        r"/(?P<name>[A-Z][A-Z0-9_]{0,63})$")


def known_projects() -> set[str]:
    """Project slugs this estate holds — from the registry, plus whatever the
    vault already carries, so a project that has a slot keeps working even if
    the registry has not caught up."""
    names: set[str] = set()
    doc = paths.REGISTRY / "projects.json"
    try:
        for row in json.loads(doc.read_text(encoding="utf-8")).get("projects", []):
            if row.get("name"):
                names.add(str(row["name"]).lower())
    except (OSError, ValueError):
        pass
    store = pathlib.Path(os.environ.get(
        "OBSERVATORY_VAULT_DIR", paths.source_path("secret_store", paths.SECRETS) / "projects"))
    try:
        names |= {d.name for d in store.iterdir() if d.is_dir()}
    except OSError:
        pass
    return names


def check_destination(dest: str) -> str:
    """The destination, normalised, or a refusal that says what a good one
    looks like.

    THE SAME PROJECT RULE AS THE VAULT. This refused `vault:local-beta-api/…`
    (the registry id's slug) while `vault.py put local-beta-api …` accepted it,
    so one project had two spellings and two directories. Now the project goes
    through `vault.project_folder`: a registry id or slug is normalised to the
    project's folder, a name two projects claim is refused with both named, and
    a name nothing claims is refused unless the vault already holds a folder of
    that name (an organisation's key has one)."""
    dest = dest if isinstance(dest, str) else ""
    if dest in DESTINATIONS:
        return dest
    m = VAULT_DEST.match(dest or "")
    if not m:
        raise ValueError(
            f"destination must be one of {sorted(DESTINATIONS)} or a vault slot "
            f"`vault:<project>/<env>/<NAME>` — project and env lowercase, the "
            f"variable UPPER_SNAKE; got {credential_shape.echo(dest)}")
    sys.path.insert(0, str(ROOT / "tools"))
    import vault
    import vault_project
    folder, _ = vault.project_folder(m.group("project"), m.group("env"), m.group("name"))
    if vault_project.resolve(folder).project_id is None:
        known = known_projects()
        if known and folder not in known:
            raise ValueError(
                f"no project named {credential_shape.echo(folder)} on this machine — a slot "
                f"under a misspelt project is one nothing will ever read. PROJECT is the "
                f"project's folder name (or its registry id); known names are in "
                f"registry/projects.json")
    vault.validate_names(folder, m.group("env"), m.group("name"))
    return f"vault:{folder}/{m.group('env')}/{m.group('name')}"


def token() -> str:
    """The API token, read from its owner-only state file; a read never creates one."""
    return load_identity(TOKEN_FILE, 'keyserver-token')


def audit(action: str, subject: str, detail: dict) -> None:
    """Written BEFORE the action, because a log written after loses the crash."""
    AUDIT.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    row = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "action": action, "subject": subject,
           "caller": _CALLER.get(), "by": os.environ.get("USER", "unknown"), **detail,
           # `caller` is what the client declared; the server authenticated
           # only possession of this workspace's token. The row says so, after
           # `detail` so no action can overwrite it, and a later reader never
           # mistakes the label for a verified identity.
           "caller_verified": False, "principal": "local-token-holder"}
    relayed = _RELAYED.get()
    if relayed:
        row["decision"] = relayed
    proven = _IDENTITY.get()
    if proven:
        # A verified delegation token: the caller is who the hub's signed chain says (OBS-62).
        row.update({"label": row["caller"], "caller": proven["caller"], "caller_verified": True,
                    "principal": proven["principal"], "chain": proven["chain"], "grant": proven["grant"]})
    # Free-text fields are not journalled. Scrub credential shapes in labels
    # and subjects too; a user can paste a credential into any textbox by
    # mistake. The shapes are `credential_shape`'s, the ones every door uses —
    # this scrub once caught only `sk-…` and 40+ runs, so a 37-character token
    # sent as an annotate id sat in the journal whole. Each string is scrubbed
    # on its own (never the encoded JSON, where a match could span a quote),
    # and the caller keeps a UUID: an agent's session id is one, and naming the
    # caller is what the journal is for.
    def scrub(key, value):
        if isinstance(value, str):
            return credential_shape.redact(value, uuids=key != "caller")
        if isinstance(value, list):
            return [scrub(key, v) for v in value]
        if isinstance(value, dict):
            return {k: scrub(k, v) for k, v in value.items()}
        return value
    encoded = json.dumps({k: scrub(k, v) for k, v in row.items()}, ensure_ascii=False)
    fd = osprivacy.open(AUDIT, os.O_WRONLY | os.O_APPEND | os.O_CREAT |
                 osprivacy.NOFOLLOW | osprivacy.NONBLOCK, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise ValueError("audit destination must be a regular file")
        os.fchmod(fh.fileno(), 0o600)
        fh.write(encoded + "\n")


def _door():
    """The OpenRouter door, imported when a route first asks for it.

    Every live mint, limit, disable, enable and rotate route goes through this
    one function. `tests/test_keyserver.py` drives a route THROUGH a stubbed
    door, because a suite that stops at the refusal layer above it cannot
    notice that the door itself is broken.
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import openrouter
    return openrouter


def act_mint(body: dict) -> dict:
    """Mint through the door and deliver, without the value crossing this
    process's edge: the door writes it straight to the destination."""
    dest = check_destination(body.get("destination"))
    name = (body.get("name") or f"observatory-{dest}").strip()
    limit = float(body.get("limit") or 0)
    if not math.isfinite(limit) or limit <= 0:
        raise ValueError("a mint without a limit is a key with no ceiling; give one")
    reset = body.get("limit_reset") or "monthly"
    audit("mint", name, {"destination": dest, "limit": limit, "limit_reset": reset})
    try:
        r = _door().issue_key(name, limit, body.get("account"), dest, body.get("project"), reset)
    except (ValueError, RuntimeError) as exc:
        raise LookupError(str(exc)) from None
    return {"ok": True, **r, "destination": dest}


def act_limit(body: dict) -> dict:
    label = body.get("label") or body.get("name") or ""
    limit = float(body.get("limit") or 0)
    # None keeps the key's own period: a daily ceiling stays daily (the door decides).
    reset = body.get("limit_reset") or None
    if not label or not math.isfinite(limit) or limit <= 0:
        raise ValueError("a label and a positive limit are both required")
    audit("limit", label, {"limit": limit, "limit_reset": reset})
    try:
        r = _door().set_limit(label, limit, reset)
    except (ValueError, LookupError, RuntimeError) as exc:
        raise LookupError(str(exc)) from None
    return {"ok": True, **r}


def act_revoke(body: dict) -> dict:
    label = body.get("label") or body.get("name") or ""
    if not label:
        raise ValueError("a label is required")
    # REFUSED FOR A KEY IN SERVICE, AND WHEN WHO READS IT CANNOT BE READ: the
    # one consumer guard (OBS-594) the door runs too, run here first so a
    # button answers instantly, without a provider round-trip. A missing scan
    # refuses; it never skips the check.
    key_consumers.revoke_guard(label, {"labels": {label}, "names": {label}},
                               [key_consumers.OpenRouterScanSource()])
    audit("revoke", label, {})
    try:
        r = _door().revoke_key(label)
    except key_consumers.RevokeRefused:
        raise
    except (ValueError, LookupError, RuntimeError) as exc:
        raise LookupError(str(exc)) from None
    return {"ok": True, **r}


#: The vault's door. A module constant so a test can stand a silent program in
#: its place and prove an exit code alone is not taken as a receipt.
VAULT = ROOT / "tools" / "vault.py"
#: What `vault.py leak` prints when, and only when, it appended the row.
_RECEIPT = re.compile(r"^recorded: (leak:\S+)$", re.M)


def act_leak(body: dict) -> dict:
    """Mark a credential leaked. No value crosses — only where it was seen.

    THE BODY IS DATA, NEVER OPTIONS. Its fields were handed to `vault.py` as
    positional argv, so `{"project": "--help"}` printed the help, exited 0 and
    this route answered 200 `marked` with nothing recorded, and `"where":
    "--force"` came back as usage text. Now the names are validated here, in
    process, by the vault's own `validate_names` (the shape test every door
    shares); `--where=<text>` binds the place to its flag whatever it starts
    with; `--` ends the options before the three names; and success is the
    vault's own receipt line, not its exit code.
    """
    fields = {}
    for field in ("project", "env", "name", "where"):
        value = body.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} is required as text; a leak with no `where` is a note "
                             f"nobody can act on")
        fields[field] = value.strip()
    sys.path.insert(0, str(ROOT / "tools"))
    import vault
    vault.validate_names(fields["project"], fields["env"], fields["name"])
    import subprocess
    args = [sys.executable, str(VAULT), "leak", f"--where={fields['where']}", "--",
            fields["project"], fields["env"], fields["name"]]
    audit("leak", f"{fields['project']}/{fields['env']}/{fields['name']}",
          {"location_supplied": True})
    p = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise LookupError(credential_shape.redact(p.stderr.strip()[-200:]) or "vault refused the mark")
    receipt = _RECEIPT.search(p.stdout or "")
    if not receipt:
        raise RuntimeError("the vault exited without its receipt line, so nothing is known to "
                           "be recorded; run `vault.py leaks` to see the register")
    # The slot as the VAULT recorded it, read from its receipt
    # (`leak:<time>:<project>/<env>/<NAME>`), not as the request spelled it.
    marked = receipt.group(1).rsplit(":", 1)[-1]
    return {"ok": True, "marked": marked, "receipt": receipt.group(1)}


def act_annotate(body: dict) -> dict:
    """Sign a credential — the operator's half of the signing step, from the page.

    ONE WRITER FOR BOTH PATHS: this calls `tools/sign_credential.write()`, the
    same function the CLI calls, so a refusal cannot be true at a terminal and
    false in a browser. Audited before the write, like everything else here, and
    the audit line carries only the names of changed fields. Free text is not
    copied into the journal, including text the writer subsequently refuses.
    """
    cred = str(body.get("id") or "").strip()
    if not cred:
        raise ValueError("id is required — the credential as the registry spells it")
    # THE ID IS CHECKED BEFORE IT IS JOURNALLED. The audit row names its
    # subject, so an id that is a pasted token, or one the board does not
    # carry, would otherwise land in the journal before the writer refused it.
    credential_shape.refuse("id", cred)
    sys.path.insert(0, str(ROOT / "tools"))
    import sign_credential
    ids = sign_credential.board_ids()
    if ids and cred not in ids:
        raise ValueError(f"the board carries no credential {credential_shape.echo(cred)} "
                         f"(ids are as registry/credentials.json prints them)")
    audit("annotate", cred, {"fields": sorted(k for k in
          ("purpose", "owner", "evidence", "rotation_days", "tags") if k in body)})
    try:
        row = sign_credential.write(cred, {
            "purpose": body.get("purpose"), "evidence": body.get("evidence"),
            "owner": body.get("owner"), "rotation_days": body.get("rotation_days"),
            "tags": body.get("tags") or []}, by=_signer())
    except ValueError as exc:
        raise ValueError(str(exc)) from None
    return {"ok": True, "id": cred, "signed_on": row["signed_on"]}


def _signer() -> str:
    """Who signs an annotation: the agent a delegation token proves, or an agent or service that named itself (the
    MCP tool, OBS-559), else the operator at the page. A self-named agent only ever lowers the claim from operator."""
    proven = _IDENTITY.get()
    if proven:
        return proven["caller"]
    caller = _CALLER.get()
    return caller if re.fullmatch(r"(agent|service):[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", caller) else "operator"


def act_reveal(body: dict) -> dict:
    """Read ONE variable out of ONE env file, and say so in the log first.

    THIS IS THE ONE ACTION HERE THAT RETURNS A VALUE, and every other rule on
    this page exists to keep it narrow:

      resolved against the SCAN, not the request. The body names a path and a
      variable; both must already appear in `store/raw/env.json`. A path this
      machine has not inventoried is refused even when it exists and is
      readable — that is the difference between a reader of an inventory and a
      file reader that happens to hold a token.

      one variable, never a file. The answer carries the value asked for and
      nothing else, so a reveal cannot become a dump by adding a parameter.

      audited BEFORE the read, like every other action here — and the audit line
      carries the path and the name and never the value, because a log of
      secrets is a second place to leak them.

      no cache. `_send` sets `Cache-Control: no-store` for every response.

    WHAT IT DOES NOT PROTECT AGAINST, stated because it is the operator's call
    rather than this file's: the value is on screen, and a screen can be
    photographed, screen-shared and recorded. The page hides it again after a
    short timeout and offers a copy button that never renders it at all; past
    that, revealing a secret is a decision, and this makes it a deliberate one
    rather than an easy one.
    """
    path = str(body.get("path") or "").strip()
    name = str(body.get("name") or "").strip()
    if not path or not name:
        raise ValueError("path and name are both required")
    scan = paths.SCRATCH / "env.json"
    if not scan.is_file():
        raise ValueError("no env scan on this machine — run `project-observatory full env` "
                         "first; this reads the inventory, not the disk")
    doc = json.loads(scan.read_text(encoding="utf-8"))
    rec = next((f for f in doc.get("files", []) if f.get("path") == path), None)
    if rec is None:
        raise ValueError(f"{credential_shape.echo(path)} is not in the env inventory, so it "
                         f"is not readable from here")
    if name not in {v.get("name") for v in rec.get("variables", [])}:
        raise ValueError(f"{credential_shape.echo(path)} holds no variable "
                         f"{credential_shape.echo(name)} in the current scan")
    root = paths.DATA.resolve()
    target = (root / path).resolve()
    if root not in target.parents:
        # Belt and braces: the inventory is built from a walk of this root, so a
        # path escaping it would mean the scan itself had been tampered with.
        raise ValueError(f"{path} resolves outside {root}")
    audit("reveal", f"{path}:{name}", {"class": next(
        (v.get("class") for v in rec["variables"] if v.get("name") == name), None)})
    sys.path.insert(0, str(ROOT / "collectors"))
    import scan_env
    pairs, _ = scan_env.parse(target.read_text(encoding="utf-8", errors="replace"))
    for n, v in pairs:
        if n == name:
            return {"path": path, "name": name, "value": v}
    raise LookupError(f"{name} was in the scan and is not in the file now — "
                      f"rescan with `project-observatory full env`")


def _toggle(body: dict, disabled: bool) -> dict:
    """Disable or enable a provider key. The door PATCHes `disabled` at the
    provider; nothing is minted and nothing is read."""
    name = body.get("name") or body.get("label") or ""
    if not name:
        raise ValueError("a key name is required")
    audit("disable" if disabled else "enable", name, {})
    try:
        return {"ok": True, **_door().toggle_key(name, disabled)}
    except LookupError as exc:
        raise LookupError(str(exc)) from None
    except RuntimeError as exc:
        raise LookupError(f"the provider refused: {exc}") from None


def act_disable(body: dict) -> dict:
    return _toggle(body, True)


def act_enable(body: dict) -> dict:
    return _toggle(body, False)


def act_rotate_key(body: dict) -> dict:
    """Rotate a PROVIDER key only: the door mints the successor and delivers it
    to the key's own destination, so no value crosses this process. `rotate`
    itself stays REFUSED below — that word is the vault's verb, and a vault
    rotation takes a value on stdin."""
    name = body.get("name") or body.get("label") or ""
    if not name:
        raise ValueError("a key name is required")
    audit("rotate-key", name, {})
    try:
        result = _door().rotate_one(name)
    except LookupError as exc:
        raise LookupError(str(exc)) from None
    except RuntimeError as exc:
        lines = getattr(exc, "lines", None)
        if lines is not None:
            # Held for a consumer of the old value (OBS-561): the old key stays alive; each holder is named.
            raise LookupError(f"held: {exc}; " + "; ".join(lines)) from None
        raise LookupError(f"the provider refused: {exc}") from None
    if not result.get("replaced"):
        # Never "ok" for a run that replaced nothing (J-11 round 6, R6-1).
        raise LookupError("an earlier rotation was finished; the current value was not replaced — run rotate again")
    return {"ok": True, **result}


def act_configure(body: dict) -> dict:
    """The Disk page's settings (OBS-562, CP-16): plan, apply or undo through the configuration door, the same door
    as `observatory_configure` and `full configure`, as the person at this page (`person:page`). Only the keys the
    page shows (`configuration_door.DISK_SCOPE`); every other setting is an agent's tool or a terminal's. Audited
    before the door acts, with the key names only. No command line goes back to the page."""
    import configuration
    import configuration_door as door
    op = body.get("op")
    if op not in ("plan", "apply", "undo"):
        raise ValueError("op is plan, apply or undo")
    try:
        if op == "plan":
            changes = body.get("changes")
            if not isinstance(changes, dict) or not changes:
                raise ValueError("a plan takes one or more {key: value} changes")
            keys = list(changes)
        elif op == "apply":
            keys = door.planned_keys(body.get("digest"))
        else:
            keys = door.applied_keys(body.get("journal"))
        catalogue = door.catalogue()
        for key in keys:
            if key not in catalogue:
                raise ValueError(f"{credential_shape.echo(str(key))} is not a setting this engine has")
            if not door.in_disk_scope(key):
                raise ValueError(f"{key} is not changed from this page; an agent changes it with "
                                 "observatory_configure")
        audit("configure", op, {"keys": sorted(keys)})
        if op == "plan":
            answer = door.plan(changes)
            return {"digest": answer["digest"], "changes": [{k: r[k] for k in ("key", "before", "after", "gate",
                                                                               "effect")} for r in answer["changes"]],
                    "needsPerson": answer["needsPerson"], "degraded": answer["degraded"]}
        if op == "apply":
            answer = door.apply(body.get("digest"), actor="person:page")
        else:
            answer = door.undo(body.get("journal"), actor="person:page")
        return {k: v for k, v in answer.items() if k != "undo"}
    except door.Refusal as refusal:
        raise ValueError(str(refusal)) from None
    except configuration.ConfigurationError as exc:
        raise ValueError(f"the workspace refused: {exc}"[:300]) from None


#: Who offers a workflow from the Agents page: a service, as the CLI is (`service:observatory-cli`). The store
#: refuses the operator as a handoff's owner outside the terminal's forced handoff.
PAGE_OWNER = "service:observatory-page"
#: The reasons a handoff without the executor's lease may give (store/workflow.REASONS_WITHOUT_TOKEN).
PAGE_HANDOFF_REASONS = ("limit", "crash", "restart")


def act_workflow_handoff(body: dict) -> dict:
    """The Agents page's button (OBS-551, CP-02): offer a stalled workflow, or one whose handoff lapsed, to another
    agent. The same store function as `observatory_handoff_create`, without a lease token, so only for `limit`,
    `crash` or `restart` and only once the executor has been silent (the store decides and says why). Audited
    before it acts with the workflow id, provider and reason; the pack's content and any token never come back."""
    from store import db as store_db
    from store import ledger as ledger_module
    from store import workflow as W
    wid, provider, reason = body.get("workflowId"), body.get("provider"), body.get("reason")
    if not isinstance(wid, str) or not W.WORKFLOW_ID.fullmatch(wid):
        raise ValueError("workflowId is a 'wf_' id")
    if reason not in PAGE_HANDOFF_REASONS:
        raise ValueError(f"reason is one of {', '.join(PAGE_HANDOFF_REASONS)}; another reason is the "
                         "executor's own, with its lease")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider names where the work goes")
    try:
        to = W.executor_shape({"provider": provider.strip()}, "to")
    except W.InvalidInput as exc:
        raise ValueError(str(exc)) from None
    audit("workflow-handoff", wid, {"provider": to["provider"], "reason": reason})
    conn = store_db.connect(paths.DB)         # this workspace's store, as the page that asked was built from
    try:
        out = W.handoff_create(conn, owner=PAGE_OWNER, idempotency_key=f"page-{secrets.token_hex(8)}",
                               workflow_id=wid, to=to, reason=reason)
    except W.UnknownWorkflow as exc:
        raise LookupError(str(exc)) from None
    except ledger_module.LedgerError as exc:
        raise ValueError(str(exc)[:300]) from None
    finally:
        conn.close()
    return {"handoffId": out["handoffId"], "workflowId": out["workflowId"], "expiresAt": out["expiresAt"],
            "credentialsMissing": bool(out.get("credentialsMissing")), "degraded": out.get("degraded") or []}


def act_issues_banner(body: dict) -> dict:
    """Dismiss the issue filer's one-time notice (OBS-557, CP-14): stored in `store/state/issues.json`, the same
    function the agent's `observatory_issues` calls with `dismissBanner`. Nothing else is written."""
    if body.get("op") != "dismiss":
        raise ValueError("op is dismiss")
    import issue_filer
    audit("issues-banner", "dismiss", {})
    return {"ok": True, **issue_filer.dismiss_banner(issue_filer.default_path())}

def act_mcp_accept(body: dict) -> dict:
    """The MCP page's Accept buttons (OBS-555, CP-09): make the definitions a probe found the pinned ones, for one
    server and tool or for every changed tool. The same function as `observatory_mcp_accept`. Audited with the server
    and tool names only; no definition, digest or command line goes in or comes back."""
    import mcp_supply
    server, tool = body.get("server") or None, body.get("tool") or None
    for label, value in (("server", server), ("tool", tool)):
        if value is not None and (not isinstance(value, str) or len(value) > 200):
            raise ValueError(f"{label} is a name of at most 200 characters")
    audit("mcp-accept", server or "all", {"tool": tool or "all"})
    return mcp_supply.accept_pending(server, tool)


#: What the agent panel's status leaves out: a path, the steps written as command lines, where a key was read
#: from, a provider's refusal text and the project list (the page asks about the whole estate).
PANEL_STATUS_DROPPED = ("workspace", "next", "key_source", "provider_status", "projects")
ASSISTANT_OPS = ("status", "ask", "job", "cancel", "conversation")


def act_assistant(body: dict) -> dict:
    """The Assistant page's chat (OBS-560, CP-19): the read-only assistant's own functions, the ones its MCP tools
    call. `status` is `observatory_assistant_status` without a path or a command line; `ask` is
    `observatory_assistant_ask` (a job and a conversation id; it spends inside the model budget); `job` and `cancel`
    are `fabric.job.get` and `fabric.job.cancel` for the assistant's jobs only; `conversation` is
    `observatory_assistant_conversation`. A refusal is the assistant's own code. An ask and a cancel are audited
    before they act, with the conversation or job id only: the question never reaches this journal."""
    from agent import assistant
    import configuration
    import jobs
    op = body.get("op")
    if op not in ASSISTANT_OPS:
        raise ValueError("op is " + ", ".join(ASSISTANT_OPS[:-1]) + " or " + ASSISTANT_OPS[-1])
    try:
        if op == "status":
            doc = assistant.status(include_projects=False)
            return {k: v for k, v in doc.items() if k not in PANEL_STATUS_DROPPED}
        if op == "ask":
            args = {k: v for k, v in body.items() if k != "op"}
            assistant.validate(args)                      # a malformed or key-shaped question is refused unaudited
            audit("assistant", "ask", {"conversation": args.get("conversation_id") or "new"})
            return assistant.ask(args)
        ident = body.get("id")
        if op == "conversation":
            return assistant.get_conversation(ident)
        if not isinstance(ident, str) or not ident:
            raise ValueError("invalid-input")
        found = jobs.get(ident)
        if found is None or found.get("capability") != "agent.ask":
            raise LookupError("unknown-job")
        if op == "cancel":
            audit("assistant", "cancel", {"job": ident})
            found = jobs.cancel(ident) or found
        view = jobs.view(found)
        return {"job": {k: view[k] for k in ("id", "status", "updatedAt", "error") if k in view}}
    except OSError as exc:
        if exc.errno == 28:
            raise ValueError("disk-full") from None
        raise RuntimeError("workspace-write-failed") from None
    except configuration.ConfigurationError:
        raise ValueError("backend-configuration") from None


def act_doctor(body: dict) -> dict:
    """The Assistant page's Undo beside a doctor's fix (OBS-560): `observatory_doctor(action="undo")`, the same
    function as `full doctor-daily --undo`. Audited with the journal id before it acts. A refusal is the doctor's
    own code: `unknown-journal-id` is 404, the others (`already-undone`, `expired`, `busy`) are 400."""
    sys.path.insert(0, str(ROOT / "tools"))
    import daily_doctor
    if body.get("op") != "undo":
        raise ValueError("op is undo; an agent reads and runs the doctor with observatory_doctor")
    jid = body.get("journalId")
    if not isinstance(jid, str) or not daily_doctor.JOURNAL_ID.fullmatch(jid):
        raise ValueError("invalid-journal-id")
    audit("doctor", "undo", {"journal": jid})
    out = daily_doctor.undo(paths.HOME, journal_id=jid)
    if isinstance(out.get("error"), dict):
        code = str(out["error"].get("code") or "refused")
        raise (LookupError if code == "unknown-journal-id" else ValueError)(code)
    return {"undone": out.get("undone") or [], "refused": out.get("refused") or [], "degraded": out.get("degraded") or []}


def _open_terminal(folder: str) -> None:
    """Terminal in `folder` on this machine: macOS's Terminal through `open`, an argument list with no shell.
    Elsewhere there is no terminal this server knows how to open, and it says so."""
    import subprocess
    if sys.platform != "darwin":
        raise ValueError("terminal-unsupported")       # a terminal this server opens on macOS only
    subprocess.run(["open", "-a", "Terminal", folder], check=True, timeout=10,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


#: Opens the terminal; a test replaces it.
TERMINAL_OPENER = _open_terminal


def act_session_terminal(body: dict) -> dict:
    """A session's Open terminal (fabric-session-links/1, the Agents page and a project's panel): a terminal in the
    folder a RUNNING coding session works in, found by its runner session id in the runner's own session file on
    this machine (session_links.folder_of). The folder is never sent to the page or the journal; an unknown or
    ended session opens nothing. Starting, opening or moving the session itself is Fabric's, through its links.
    Audited with the session id before it acts."""
    import session_links
    sid = body.get("sessionId")
    if not isinstance(sid, str) or not session_links.SESSION_ID.fullmatch(sid):
        raise ValueError("invalid-session-id")
    folder = session_links.folder_of(sid)
    if folder is None:
        raise LookupError("session-not-running")       # no live process holds this session on this machine
    audit("session-terminal", sid, {})
    try:
        TERMINAL_OPENER(folder)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001: a refusal the page can show, never the folder
        raise RuntimeError(f"the terminal did not open: {type(exc).__name__}") from None
    return {"opened": True, "sessionId": sid}


ACTIONS = {"mint": act_mint, "limit": act_limit, "revoke": act_revoke,
           "leak": act_leak, "reveal": act_reveal, "annotate": act_annotate,
           "disable": act_disable, "enable": act_enable, "rotate-key": act_rotate_key,
           "configure": act_configure, "workflow-handoff": act_workflow_handoff,
           "issues-banner": act_issues_banner, "mcp-accept": act_mcp_accept,
           "assistant": act_assistant, "doctor": act_doctor, "session-terminal": act_session_terminal}

#: Refused on purpose, with the reason the caller needs rather than a 404.
REFUSED = {
    "put": "a value travels on stdin and nowhere else — run `python \"$(project-observatory full-path)/tools/vault.py\" put` yourself",
    "rotate": "a value travels on stdin and nowhere else — run `python \"$(project-observatory full-path)/tools/vault.py\" rotate` yourself",
}


#: The largest request body the keyserver reads, and so the most it reads off after an answer.
BODY_LIMIT = 65536
#: How long a refused request's unread body is read off after the answer before the socket closes (OBS-309).
DRAIN_SECONDS = 1.0


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "observatory-keyserver"
    #: Body bytes this request has read; what the request declared beyond it is read off in `finish`.
    _body_read = 0

    def finish(self) -> None:
        try:
            self._read_off_unread_body()
        finally:
            super().finish()

    def _owed(self) -> int:
        """The body bytes the request declared and nothing has read, never more than BODY_LIMIT.

        A framing the keyserver refuses (Transfer-Encoding, several or malformed Content-Length) is read
        off up to the limit as well; no Content-Length and no Transfer-Encoding is no body."""
        headers = getattr(self, "headers", None)
        if headers is None:
            return 0
        lengths = headers.get_all("Content-Length", [])
        if headers.get("Transfer-Encoding") or len(lengths) > 1 or (
                lengths and not re.fullmatch(r"[0-9]{1,18}", lengths[0])):
            declared = BODY_LIMIT
        elif lengths:
            declared = min(int(lengths[0]), BODY_LIMIT)
        else:
            return 0
        return max(0, declared - self._body_read)

    def _read_off_unread_body(self) -> None:
        """Answer sent: half-close, read off what the body still owes for at most DRAIN_SECONDS, then close.

        Several refusals answer before the body is read (a Host or Origin that is not this server, a body
        over the limit, a framing the server refuses, any GET or unknown method that carries one). A socket
        closed with bytes unread makes Windows send a reset, and a reset can lose the answer the client has
        not read yet: the caller saw WinError 10053 or 10054 instead of its 403 (OBS-309, the class of
        OBS-294 and OBS-295). Nothing read here is parsed or acted on, and it is bounded twice: by the
        declared length capped at BODY_LIMIT, and by the deadline."""
        owed = self._owed()
        if owed <= 0:
            return
        try:
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + DRAIN_SECONDS
            while owed > 0 and (left := deadline - time.monotonic()) > 0:
                self.connection.settimeout(left)
                chunk = self.rfile.read1(min(owed, 8192))
                if not chunk:
                    break
                owed -= len(chunk)
        except (OSError, ValueError):
            pass

    def log_message(self, fmt, *args):                                   
        pass                                                                              

    def _send(self, code: int, payload: dict, headers: dict | None = None) -> None:
        # EVERY REFUSAL IS SCRUBBED ON THE WAY OUT. The sentences come from this
        # file, the vault, the signer and the provider door, and several quote
        # what the caller sent; a key pasted into the wrong field must not come
        # back in the answer. The shared shapes, the same ones the audit uses.
        if code >= 400 and isinstance(payload.get("error"), str):
            payload = {**payload, "error": credential_shape.redact(payload["error"])}
        if code >= 400 and isinstance(payload.get("error"), dict) and isinstance(payload["error"].get("message"), str):
            payload = {**payload, "error": {**payload["error"],
                                            "message": credential_shape.redact(payload["error"]["message"])}}
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, status: int, error_code: str, message: str, inside: dict | None = None, **beside) -> None:
        """A typed refusal (OBS-601): `{"error": {code, message, ...inside}}`, as the MCP tools answer; `beside` keeps
        the fields beside `error` that an earlier answer published."""
        self._send(status, {"error": {"code": error_code, "message": message, **(inside or {})}, **beside})

    def _local_request(self) -> bool:
        """Validate the browser authority before serving HTML containing a token.

        Missing Origin is intentional for CLI clients. An Origin, when present,
        must be the exact HTTP origin of Host, including the actual listening
        port. Host is checked even for token-free assets to stop DNS rebinding.
        """
        hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        port = self.server.server_address[1]
        expected = {f"{host}:{port}" for host in ("127.0.0.1", "localhost", "[::1]")}
        if port == 80:
            expected.update(("127.0.0.1", "localhost", "[::1]"))
        if len(hosts) != 1 or hosts[0].lower() not in expected:
            self._fail(403, "host-not-loopback", "Host must name this loopback server and port")
            return False
        if len(origins) > 1:
            self._fail(403, "origin-not-same", "one same-origin Origin is required")
            return False
        if origins:
            origin = origins[0]
            try:
                parsed = urllib.parse.urlsplit(origin)
                valid = (parsed.scheme == "http" and not parsed.username and not parsed.password
                         and not parsed.path and not parsed.query and not parsed.fragment
                         and parsed.netloc.lower() == hosts[0].lower()
                         and (parsed.port or 80) == port
                         and origin.lower() == f"http://{hosts[0].lower()}")
            except ValueError:
                valid = False
            if not valid:
                self._fail(403, "origin-not-same", "Origin must match this loopback server and port")
                return False
        return True

    def _authorised(self) -> bool:
        # `compare_digest`, not `!=`: on a loopback socket the timing leak is
        # academic, and the one-line habit is cheaper than the argument.
        given = self.headers.get(HEADER) or ""
        if (len(self.headers.get_all(HEADER, [])) != 1
                or not hmac.compare_digest(given.encode(), str(self.server.token).encode())):
            self._fail(401, "unauthenticated", "bad or missing token")
            return False
        _CALLER.set(caller_name(self.headers.get(CALLER_HEADER)))
        return True

    def _refuse(self, code: str, message: str, detail: str) -> bool:
        """A contract refusal (DL-19): `{"error": {code, message, data}}` with the code's HTTP status."""
        status = REFUSAL_STATUS.get(code, 403)
        extra = {"WWW-Authenticate": 'Bearer error="invalid_token"'} if status == 401 else None
        self._send(status, {"error": {"code": code, "message": message, "data": {"detail": detail}}}, extra)
        return False

    def _delegated(self, what: str) -> bool:
        """Verify a delegation token when the request carries one; demand one from an agent in `agents` mode.

        THE BOUNDARY, stated plainly (Fabric CO-090): any local process can send an `Origin` header and read
        this workspace's 0600 token. `agents` mode stops accidental and confused borrowing — an agent calling
        here with the operator's page's credentials by mistake — not a hostile process of the same user."""
        _IDENTITY.set(None)
        for name in ("Authorization", delegation.DELEGATION_HEADER):
            if len(self.headers.get_all(name, [])) > 1:
                return self._refuse("unauthenticated", f"send one {name} header", "duplicate-header")
        carried = delegation.read_delegation_token(dict(self.headers.items()))
        try:
            import meter as meter_module
            m = meter_module.default_meter()
        except (OSError, ValueError) as exc:
            if carried is None:
                return True               # the operator's page keeps working with a broken meter config
            return self._refuse("unknown-state", "the meter's configuration cannot be read", type(exc).__name__)
        if carried is None:
            if m.keyserver_mode == "agents" and not self.headers.get("Origin"):
                return self._refuse("unauthenticated", "an agent calls the keyserver with a Fabric delegation "
                                    "token (Authorization: Bearer) for this action's scope",
                                    "delegation-required")
            return True
        if what == "reveal":
            return self._refuse("scope-not-granted", "a value is never revealed to an agent: run the command "
                                "with `use_secret.py run PROJECT NAME -- COMMAND`, which puts it in the "
                                "child's environment", "reveal-is-the-operators")
        if what not in ACTION_SCOPES:
            return self._refuse("scope-not-granted", "this action takes no delegation token", "no-scope")
        scope, effect = ACTION_SCOPES[what]
        token = carried[0]
        result = delegation.verify_delegation(
            token, audience=m.audience, jwks=m.keys.get(meter_module._token_kid(token)),
            now=int(datetime.now(timezone.utc).timestamp()), capability=scope, effect=effect, issuer=m.issuer)
        del token, carried
        if not result.ok:
            return self._refuse(result.code, "the delegation token was refused", result.detail)
        _IDENTITY.set({"caller": result.caller, "principal": result.principal, "chain": list(result.chain),
                       "grant": list(result.claims["grant"])})
        return True

    def _page_for(self, route: str):
        """(file, content type) for a route, or (None, None).

        THE SPLIT PAGES ARE SERVED HERE TOO, not only the single page: the
        pages a person actually opens must be able to carry the token, or live
        mode is a state no page can enter and every `data-act` button on them
        is a promise the server cannot keep. Names come from the shell's
        whitelist, the same one `serverd.py` reads; `..` and an unknown name
        are 404, never a file outside the dashboard directory.
        """
        sys.path.insert(0, str(ROOT / "dashboard"))
        import shell
        if route in ("/", "/index.html"):
            page = paths.DASHBOARD_DIR / "index.html"
            return (page if page.is_file() else paths.DASHBOARD_HTML), "text/html"
        if route.startswith("/dashboard/"):
            name = route[len("/dashboard/"):]
            kind = {shell.ASSET_CSS: "text/css", shell.ASSET_JS: "text/javascript"}.get(name)
            if kind or (name.endswith(".html") and name[:-5] in shell.NAMES):
                return paths.DASHBOARD_DIR / name, kind or "text/html"
        return None, None

    def do_GET(self) -> None:                                            
        if not self._local_request():
            return
        route = self.path.split("?", 1)[0].split("#", 1)[0]
        page, ctype = self._page_for(route)
        if page is not None and ctype != "text/html":
            if not page.is_file():
                self._fail(404, "not-built", "the pages have not been built: project-observatory full dashboard")
                return
            body = page.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if page is not None:
            if not page.is_file():
                self._fail(404, "not-built", "the dashboard has not been built: project-observatory full dashboard")
                return
            html = page.read_text(encoding="utf-8")
            # THE TOKEN IS GIVEN TO THE PAGE, not typed by a person. It is not a
            # credential for anything outside this process, it never leaves the
            # machine, and the alternative is a human copying a token into a
            # browser every morning.
            html = html.replace("</head>",
                                f'<meta name="observatory-token" content="{html_module.escape(str(self.server.token), quote=True)}">'
                                f"</head>", 1)
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/state":
            if not self._authorised():
                return
            doc = paths.REGISTRY / "credentials.json"
            self._send(200, json.loads(doc.read_text(encoding="utf-8"))
                       if doc.is_file() else {"credentials": [], "totals": {}})
            return
        self._fail(404, "no-such-path", "no such path")

    def _read_bounded(self) -> bytes | None:
        """The body when its framing is valid and within the limit, else None (the checks below answer).
        Read before the refusals that follow it: on Windows, closing a socket with unread input sends a reset
        that loses the queued answer, so a refused caller saw a connection abort instead of its typed refusal
        (review of #387). The refusals answered before it (Host, Origin) and those it leaves to the checks
        below (framing, size) have their body read off after the answer in `finish` (OBS-309). Bounded at
        BODY_LIMIT, and only for a loopback caller."""
        lengths = self.headers.get_all("Content-Length", [])
        if (self.headers.get("Transfer-Encoding") or len(lengths) != 1
                or not re.fullmatch(r"[0-9]{1,18}", lengths[0])):
            return None
        length = int(lengths[0])
        if length > BODY_LIMIT:
            return None
        raw = self.rfile.read(length)
        self._body_read = len(raw)
        return raw

    def do_POST(self) -> None:                                           
        if not self._local_request():
            return
        early = self._read_bounded()
        if not self.path.startswith("/api/"):
            self._fail(404, "no-such-path", "no such path")
            return
        if not self._authorised():
            return
        what = self.path[len("/api/"):]
        if what in REFUSED:
            self._fail(403, "stdin-only", REFUSED[what])
            return
        fn = ACTIONS.get(what)
        if not fn:
            self._fail(404, "unknown-action", f"no action {credential_shape.echo(what)}")
            return
        if not self._delegated(what):
            return
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
            self._fail(400, "invalid-framing", "one Content-Length and no Transfer-Encoding are required")
            return
        try:
            if not re.fullmatch(r"[0-9]+", lengths[0]):
                raise ValueError
            length = int(lengths[0])
            if length > BODY_LIMIT:
                self._fail(413, "body-too-large", "request body is too large")
                return
            raw = early if early is not None else self.rfile.read(length)
            self._body_read = len(raw)
            if len(raw) != length:
                raise ValueError
            body = json.loads(raw or b"{}", parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, UnicodeError):
            self._fail(400, "invalid-body", "body must be a bounded JSON object with finite numbers")
            return
        try:
            self._send(200, fn(body))
        except Exception as exc:                                           # noqa: BLE001  typed below, by `refusal`
            status, code = refusal(exc)
            if isinstance(exc, key_consumers.RevokeRefused):
                # The consumer guard's typed refusal (OBS-594): its code and what it named, never a value. `code`,
                # `consumers` and `unread` beside `error` are the fields its first answer published.
                self._fail(status, code, str(exc), inside={"consumers": exc.consumers, "unread": exc.unread},
                           code=exc.code, consumers=exc.consumers, unread=exc.unread)
            elif isinstance(exc, urllib.error.HTTPError):
                self._fail(status, code, f"the provider refused: HTTP {exc.code}")
            elif isinstance(exc, (ValueError, LookupError, RuntimeError)):
                # A name the ledger or the provider does not know is the caller's mistake, not the server's: 404
                # with the door's sentence, never a 500 that reads as "the keyserver is broken". A RuntimeError is
                # an action that ran and could not confirm its effect (the vault without its receipt). Every
                # sentence is scrubbed on the way out, like everything else this server says.
                self._fail(status, code, credential_shape.redact(str(exc)))
            else:
                self._fail(status, code, "action failed; inspect the local service configuration")


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """One thread per request (J-11 round 5): a door verb may wait up to the ledger lock's 150 s for another
    operation, and on a single-threaded server that wait froze the whole key page. The module holds no mutable
    state of its own, the audit log is written with O_APPEND, and every door verb that writes the ledger runs
    under its lock. `block_on_close` stays on, so `--once` waits for its one request to finish. SO_REUSEADDR only
    off Windows, where it would let the bind take a port another live socket holds (OBS-643)."""
    allow_reuse_address = os.name != 'nt'
    daemon_threads = True

    def __init__(self, addr, handler, tok):
        if addr[0] not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("keyserver binds only to a loopback address")
        if not tok:
            raise ValueError("keyserver requires a nonempty token")
        if addr[0] == "::1":
            self.address_family = socket.AF_INET6
        # A stalled browser must not hold a request thread forever.
        super().__init__(addr, handler)
        self.token = tok

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(10)
        return sock, address

    def finish_request(self, request, client_address):
        """OBS-355: on a host with several OS accounts, another account's connection is answered 403 and
        closed before the handler exists, so not a byte of its request is read (`peer_check.py`). Here and
        not in `get_request`: this runs on the request's own thread, so a slow socket-table read or the
        refusal's lingering close never holds up the accept loop."""
        refused = peer_check.GATE.check_socket(request)
        if refused:
            peer_check.refuse(request, refused)
            return
        super().finish_request(request, client_address)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=None,
                    help="default: this OS account's recorded port (7717 on a host with one account)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--once", action="store_true",
                    help="answer one request and exit")
    a = ap.parse_args(argv[1:])
    if a.port is None:
        import local_ports                         # this OS account's port, recorded when it has none (OBS-354)
        try:
            a.port = local_ports.resolve("keyserver", allocate_missing=True)
        except local_ports.PortError as exc:
            print(f"keyserver: {exc} ({exc.code}); run: {exc.step}", file=sys.stderr)
            return 2
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"refusing to bind {a.host}: this holds a provisioning key and answers "
              f"only to this machine", file=sys.stderr)
        return 2
    try:
        tok = token()
    except IdentityError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    with Server((a.host, a.port), Handler, tok) as srv:
        shown_host = f"[{a.host}]" if a.host == "::1" else a.host
        # FLUSHED: with stdout redirected to a file or a pipe these lines sat in
        # a block buffer while the server ran and were lost when it was stopped,
        # so a supervisor's log never said where the page or the journal was.
        print(f"keyserver on http://{shown_host}:{srv.server_address[1]} — open it; the page carries "
              f"its own token", flush=True)
        print(f"  audit: {AUDIT}", flush=True)
        print(f"  refused by design: vault put and rotate — a value travels on stdin", flush=True)
        if a.once:
            srv.handle_request()
        else:
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
