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
import socketserver
import sys
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "collectors"))
import credential_shape
import paths              

import stat
from runtime_identity import IdentityError, load as load_identity

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
    fd = os.open(AUDIT, os.O_WRONLY | os.O_APPEND | os.O_CREAT |
                 getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK, 0o600)
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
    audit("mint", name, {"destination": dest, "limit": limit})
    try:
        r = _door().issue_key(name, limit, body.get("account"), dest, body.get("project"))
    except (ValueError, RuntimeError) as exc:
        raise LookupError(str(exc)) from None
    return {"ok": True, **r, "destination": dest}


def act_limit(body: dict) -> dict:
    label = body.get("label") or body.get("name") or ""
    limit = float(body.get("limit") or 0)
    reset = body.get("limit_reset") or "monthly"
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
    # REFUSED FOR A KEY IN SERVICE — the door checks the scan's `serves`, and
    # so did this file before it; the refusal stays here too so a button
    # answers instantly, without a provider round-trip.
    scan = paths.SCRATCH / "openrouter.json"
    if scan.is_file():
        for kk in json.loads(scan.read_text(encoding="utf-8")).get("keys", []):
            if kk.get("label") == label and kk.get("serves"):
                raise ValueError(f"{label} is what {kk['serves']} is reading right now; "
                                 f"mint its replacement first, then revoke this one")
    audit("revoke", label, {})
    try:
        r = _door().revoke_key(label)
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
            "tags": body.get("tags") or []})
    except ValueError as exc:
        raise ValueError(str(exc)) from None
    return {"ok": True, "id": cred, "signed_on": row["signed_on"]}


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
        return {"ok": True, **_door().rotate_one(name)}
    except LookupError as exc:
        raise LookupError(str(exc)) from None
    except RuntimeError as exc:
        raise LookupError(f"the provider refused: {exc}") from None


ACTIONS = {"mint": act_mint, "limit": act_limit, "revoke": act_revoke,
           "leak": act_leak, "reveal": act_reveal, "annotate": act_annotate,
           "disable": act_disable, "enable": act_enable, "rotate-key": act_rotate_key}

#: Refused on purpose, with the reason the caller needs rather than a 404.
REFUSED = {
    "put": "a value travels on stdin and nowhere else — run `python \"$(project-observatory full-path)/tools/vault.py\" put` yourself",
    "rotate": "a value travels on stdin and nowhere else — run `python \"$(project-observatory full-path)/tools/vault.py\" rotate` yourself",
}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "observatory-keyserver"

    def log_message(self, fmt, *args):                                   
        pass                                                                              

    def _send(self, code: int, payload: dict) -> None:
        # EVERY REFUSAL IS SCRUBBED ON THE WAY OUT. The sentences come from this
        # file, the vault, the signer and the provider door, and several quote
        # what the caller sent; a key pasted into the wrong field must not come
        # back in the answer. The shared shapes, the same ones the audit uses.
        if code >= 400 and isinstance(payload.get("error"), str):
            payload = {**payload, "error": credential_shape.redact(payload["error"])}
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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
            self._send(403, {"error": "Host must name this loopback server and port"})
            return False
        if len(origins) > 1:
            self._send(403, {"error": "one same-origin Origin is required"})
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
                self._send(403, {"error": "Origin must match this loopback server and port"})
                return False
        return True

    def _authorised(self) -> bool:
        # `compare_digest`, not `!=`: on a loopback socket the timing leak is
        # academic, and the one-line habit is cheaper than the argument.
        given = self.headers.get(HEADER) or ""
        if (len(self.headers.get_all(HEADER, [])) != 1
                or not hmac.compare_digest(given.encode(), str(self.server.token).encode())):
            self._send(401, {"error": "bad or missing token"})
            return False
        _CALLER.set(caller_name(self.headers.get(CALLER_HEADER)))
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
                self._send(404, {"error": "the pages have not been built — project-observatory full dashboard"})
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
                self._send(404, {"error": "the dashboard has not been built — "
                                          "project-observatory full dashboard"})
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
        self._send(404, {"error": "no such path"})

    def do_POST(self) -> None:                                           
        if not self._local_request():
            return
        if not self.path.startswith("/api/"):
            self._send(404, {"error": "no such path"})
            return
        if not self._authorised():
            return
        what = self.path[len("/api/"):]
        if what in REFUSED:
            self._send(403, {"error": REFUSED[what]})
            return
        fn = ACTIONS.get(what)
        if not fn:
            self._send(404, {"error": f"no action {credential_shape.echo(what)}"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
            self._send(400, {"error": "one Content-Length and no Transfer-Encoding are required"})
            return
        try:
            if not re.fullmatch(r"[0-9]+", lengths[0]):
                raise ValueError
            length = int(lengths[0])
            if length > 65536:
                self._send(413, {"error": "request body is too large"})
                return
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError
            body = json.loads(raw or b"{}", parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, UnicodeError):
            self._send(400, {"error": "body must be a bounded JSON object with finite numbers"})
            return
        try:
            self._send(200, fn(body))
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
        except LookupError as exc:
            # A name the ledger or the provider does not know is the caller's
            # mistake, not the server's — 404 with the door's sentence, never a
            # 500 that reads as "the keyserver is broken".
            self._send(404, {"error": str(exc)})
        except urllib.error.HTTPError as exc:
            self._send(502, {"error": f"the provider refused: HTTP {exc.code}"})
        except RuntimeError as exc:
            # An action that ran and could not confirm its effect (the vault
            # without its receipt). Its sentence is written here, not by the
            # caller, and is scrubbed like everything else this server says.
            self._send(500, {"error": credential_shape.redact(str(exc))})
        except Exception as exc:                                           
            self._send(500, {"error": "action failed; inspect the local service configuration"})


class Server(socketserver.TCPServer):
    allow_reuse_address = True

    def __init__(self, addr, handler, tok):
        if addr[0] not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("keyserver binds only to a loopback address")
        if not tok:
            raise ValueError("keyserver requires a nonempty token")
        if addr[0] == "::1":
            self.address_family = socket.AF_INET6
        # A stalled browser must not hold this single-process server forever.
        super().__init__(addr, handler)
        self.token = tok

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(10)
        return sock, address


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=7717)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--once", action="store_true",
                    help="answer one request and exit")
    a = ap.parse_args(argv[1:])
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
