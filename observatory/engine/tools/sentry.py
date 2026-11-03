#!/usr/bin/env python3
"""The one door to Sentry tokens: a narrow token for one purpose, verified, recorded, and the broad one retired.

    T="$(project-observatory full-path)/tools"          # the installed engine's tools
    python "$T/sentry.py" presets
    python "$T/sentry.py" issue --preset releases --from PROJECT/ENV/NAME --vault PROJECT/ENV/NAME \
        [--org SLUG] [--project SLUG] [--base-url URL] [--region us|de | --region-host URL]
    python "$T/sentry.py" install --preset releases|read --vault PROJECT/ENV/NAME [--org SLUG] [--project SLUG] ...
    python "$T/sentry.py" list
    python "$T/sentry.py" status [PROJECT/ENV/NAME]
    python "$T/sentry.py" revoke PROJECT/ENV/NAME [--forget] # one token this door issued or installed
    python "$T/sentry.py" revoke --from PROJECT/ENV/NAME [--org SLUG]   # the broad token, after a narrow one verifies

A site's build that holds a broad Sentry token (org:admin, member:admin, team:admin, project:admin,
event:admin) needs only to create releases and upload source maps; error triage needs only to read
projects, issues and events. This door gives each purpose the narrowest token Sentry has for it,
proves it with its own value before anything depends on it, writes it to a vault slot, and records
what it is (kind, scopes, organization, project, token id), never the value. Values move only
between vault slots: the minting credential is read from `--from` with `use_secret.resolve`
(vault only), the new one is written with `vault.py put` on stdin, and none reaches argv, a URL,
a log, the ledger or the output, including on error.

WHICH TOKEN, AND WHY (read from Sentry's source, getsentry/sentry `master`, 2026-11-02):

* `releases` is an ORGANIZATION AUTH TOKEN (`sntrys_`) with the one scope `org:ci`. The scope maps
  in `src/sentry/api/bases/` that name `org:ci` are exactly: GET, POST and PUT of
  `OrganizationReleasePermission` and `ProjectReleasePermission` (releases, their files and commits,
  artifact bundles and source maps, debug files and chunk upload, which use those permissions); GET of
  `OrganizationIntegrationsLoosePermission` (reading the organization's integrations); and GET and
  POST of `OrganizationCodeMappingsBulkPermission` (bulk code mappings). It is in none of their DELETE
  lists, so it cannot delete a release or a debug file, and it is in no member, event, project
  setting or organization setting map. It is not bound to a person, so it survives the person who
  made it leaving. `project:releases` on another token kind also deletes releases, so it is broader. Organization tokens carry no expiry (`src/sentry/models/orgauthtoken.py` has no
  expiry field), and that is recorded as the reason, not as "never asked".
* `read` is an INTERNAL INTEGRATION token (`sntryi_`) with exactly `org:read`, `project:read`,
  `event:read`: the scopes triage needs, not bound to a person, and unable to read members or
  write anything. No organization token can hold these (`ORG_AUTH_TOKEN_ALLOWED_SCOPES` is
  `{"org:ci"}`).

WHAT SENTRY'S API LETS A TOKEN MINT, and so where this door refuses:

* `POST /api/0/organizations/{org}/org-auth-tokens/` accepts a browser session, or an OAuth
  application's token (`sntrya_`) whose grant includes `org:tokens` and any one of `org:read`,
  `org:write`, `org:admin` (deleting one takes `org:write` or `org:admin`, so a credential without
  either mints, but a token whose proof fails must then be deleted by a person). A PERSONAL token (`sntryu_`), an organization token and an integration token
  are refused (`OrgAuthTokenPermission`, getsentry/sentry#99457 and #128051, the latter merged
  2026-10-23). So `issue --preset releases` mints by API when `--from` holds such an OAuth token,
  and otherwise refuses before any call, naming the page where a person makes the token in one
  click (Sentry offers only `org:ci` there) and the `install` that verifies it.
* Personal tokens (`/api/0/api-tokens/`) and internal integration tokens
  (`/api/0/sentry-apps/{slug}/api-tokens/`) are created and deleted from a signed-in browser
  session only (`SessionNoAuthTokenAuthentication`); an internal integration created through the
  API gets no token (`skip_default_auth_token=True`). So `issue --preset read` always refuses with
  the integration recipe, and `install` verifies what the person made.
* A personal token cannot revoke itself or anything else by API. `revoke --from` therefore
  re-verifies the narrow tokens first, then reads whether the broad token still answers on ITS OWN
  Sentry: `--base-url`, or the base recorded the last time that slot answered there. The slot must
  hold a Sentry token (a known prefix; an organization token must name that base). A 200 is recorded
  as proof that the token belongs to that Sentry, and the door names the page where a person deletes
  it and exits 3. A 401 counts as revoked, and removes the slot, only when such a proof was recorded
  before (by an `issue` that minted with it, or an earlier `revoke --from` that found it alive): a
  401 from a Sentry that never accepted the value proves only that it is not that Sentry's token.
  The proof belongs to the VALUE, not the slot: it records the token's kind and an HMAC-SHA256 of it
  under the ledger's own random key (`sentry-proof.key`, owner-only beside the ledger; never a plain
  hash). A slot whose value has changed since (a rotation, another Sentry's token) is not sent to the
  recorded base, and its 401 proves nothing: it is marked `unproven`, kept, and needs a new proof. A
  base named with --base-url for the first time is said on stderr; so is a --base-url that replaces a
  recorded base whose proof no longer holds for the value (a value still proved there is refused for any
  other Sentry). `status` keeps the same rule: it asks a recorded base about a broad token only when the
  slot still holds the proved value, and reports an `unproven` or changed entry without sending it.
  Narrow tokens follow the same rule: `issue` and `install` record each one's kind and keyed digest,
  and `status`, `revoke --from` and `revoke SLOT` send a narrow slot's value only while it is still
  that value (and an organization token only to the Sentry it names). `revoke SLOT` sends the minting
  credential only while its slot holds the value proved on that Sentry; otherwise it refuses with the
  `revoke --from ... --base-url` step that proves it again. Without --base-url, `issue` and `install`
  use https://sentry.io and say so.
* Service accounts (`/api/0/organizations/{org}/service-accounts/`, project-scoped) are being
  built behind a feature flag and issue no token yet; when they do, they are the route for a
  project-scoped token.

HOSTS. `--base-url` is the Sentry the tokens belong to: `https://sentry.io` by default, or a
self-hosted origin over https (plain http only on a loopback address). Organization-level calls
(`/api/0/`, organizations, organization tokens) go there. Release, issue and member reads go to the
organization's REGION host, read from the organization's `links.regionUrl`, or from an
organization token's own payload (`region_url`), or given with `--region us|de` or `--region-host`.
A region host read from a token or an answer must be the base itself or one of its documented region
hosts (`us.sentry.io` and `de.sentry.io` for `sentry.io`, by exact name, no suffix rule); another host
is used only when the person names it with `--region-host`. Redirects are never followed: urllib would
carry the Authorization header to any host they name, and from https to plain http.

VERIFIED MEANS, with the token's own value: `GET /api/0/` answers its scopes and they are exactly
the preset's; every purpose read answers 200 (retried while a fresh token replicates); and every
read outside the purpose (members, and events for `releases`) answers 401, 403 or 404. A 200 there
is a broader token than declared and is refused; a 429, a 5xx or no answer proves nothing, and the
door stops. A token this call minted and could not verify or deliver is deleted again.

THE LEDGER `sentry-issued.json` beside the other secrets holds one row per slot: preset, kind,
scopes as Sentry answered them, organization, project, base and region hosts, the token id when
this door minted it, the slot of the minting credential, dates and the last verification; and the
broad tokens retired (`broad`), by slot and state. Never a value. One operation on it at a time.
"""
from __future__ import annotations
import argparse
import base64
import datetime
import hashlib
import hmac
import http.client
import json
import os
import pathlib
import re
import subprocess
import sys
from subprocess import SubprocessError
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths  # noqa: E402
import private_io  # noqa: E402
sys.path.insert(0, str(ROOT / "tools"))
import key_expiry  # noqa: E402  every minted credential records its expiry, or why it has none (OBS-191)

API_DEFAULT = "https://sentry.io"
#: Sentry's SaaS region hosts (`--region us|de`); a self-hosted Sentry has one host, its base.
REGIONS = {"us": "https://us.sentry.io", "de": "https://de.sentry.io"}
LEDGER = paths.source_path("secret_store", paths.SECRETS) / "sentry-issued.json"
TOOL = "sentry.py"

#: A token's kind by its prefix (getsentry/sentry `src/sentry/types/token.py`, `AuthTokenType`).
#: A token from before Sentry prefixed them is `unprefixed`: its kind is unknown here.
KINDS = {"sntryu_": "personal", "sntrya_": "oauth-app", "sntrys_": "org", "sntryi_": "integration"}
KIND_WORDS = {"personal": "a personal user auth token", "oauth-app": "an OAuth application's user token",
              "org": "an organization auth token", "integration": "an internal integration token",
              "unprefixed": "an unprefixed token from before Sentry named its kinds"}

#: What an OAuth token needs to mint an organization token, as `OrgAuthTokenPermission` checks it:
#: the `org:tokens` consent and any one of the POST scopes; deleting one takes one of the DELETE scopes.
MINT_CONSENT = "org:tokens"
MINT_ANY = ("org:read", "org:write", "org:admin")
DELETE_ANY = ("org:write", "org:admin")

#: Why an organization token is recorded without an expiry: Sentry's model has no field for one.
ORG_NO_EXPIRY = ("a Sentry organization auth token has no expiry (the model has no expiry field); "
                 "it lives until `sentry.py revoke` deletes it")

#: A preset: the token kind it is, its exact scopes, what it is for, how it is minted, and the reads
#: that prove it (`purpose`, each must answer 200) and bound it (`outside`, each must be refused).
#: `{org}` and `{project}` are filled from --org and --project; a `project` probe replaces the
#: organization-wide one when --project is given.
PRESETS: dict[str, dict] = {
    "releases": {
        "kind": "org",
        "scopes": ("org:ci",),
        "why": ("lists, creates and updates releases and uploads source maps, artifact bundles and debug files "
                "for one organization, reads its integrations and reads and writes bulk code mappings; cannot "
                "delete a release or a debug file, read events or members, or change a setting"),
        "mint": "org-auth-token",
        "purpose": {"org": "/api/0/organizations/{org}/releases/?per_page=1",
                    "project": "/api/0/projects/{org}/{project}/releases/?per_page=1"},
        "outside": ("/api/0/organizations/{org}/members/?per_page=1",
                    "/api/0/organizations/{org}/issues/?limit=1"),
    },
    "read": {
        "kind": "integration",
        "scopes": ("event:read", "org:read", "project:read"),
        "why": "reads one organization's projects, issues and events for error triage; writes nothing",
        "mint": None,
        "purpose": {"org": "/api/0/organizations/{org}/issues/?limit=1",
                    "project": "/api/0/projects/{org}/{project}/issues/?limit=1"},
        "outside": ("/api/0/organizations/{org}/members/?per_page=1",),
    },
}

#: Attempts and the pause between them while a fresh token replicates to the region.
PROOF_ATTEMPTS = 6


class SentryError(RuntimeError):
    """A provider refusal, stripped to endpoint shape and status: never a header, body or value."""

    def __init__(self, status: int | str, message: str):
        super().__init__(message)
        self.status = status


class Refusal(RuntimeError):
    """A refusal this door makes; `code` is the exit code (2 usage, 3 a person's step, 1 otherwise)."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def _journal(event: str, secret: str, **detail) -> None:
    """The vault's movements journal: every issue, install and revocation is on the record. Never raises."""
    try:
        import vault
        vault.journal(event, secret, tool=TOOL, **detail)
    except Exception as exc:  # noqa: BLE001  the class only: a message could name a path
        print(f"  (movement not journaled: {type(exc).__name__})", file=sys.stderr)


# ─────────────────────────── hosts ──────────────────────────────────────────

LOOPBACK = ("127.0.0.1", "::1", "localhost")


def origin(url: str | None, what: str = "--base-url") -> str:
    """scheme://host[:port] of a Sentry, refused unless https, or http on a loopback address."""
    parts = urllib.parse.urlsplit((url or "").strip())
    host = parts.hostname or ""
    if not host or parts.username or parts.password or parts.query or parts.fragment \
            or parts.path not in ("", "/"):
        raise Refusal(f"{what} must be an origin such as https://sentry.io, with no path, query or user", 2)
    if parts.scheme == "https" or (parts.scheme == "http" and host in LOOPBACK):
        return f"{parts.scheme}://{parts.netloc}".lower()
    raise Refusal(f"{what} must be https (plain http only on a loopback address): a token is never sent "
                  f"over an open network", 2)


#: The region hosts each SaaS base documents, by exact origin. A self-hosted Sentry has none: its region
#: is its base, unless the person names another host with --region-host. No suffix rule: a region host
#: comes from the server's answer or a token's payload, and `sentry.example.co.uk` must not accept
#: `attacker.co.uk` because both end in `co.uk`.
KNOWN_REGIONS: dict[str, tuple[str, ...]] = {API_DEFAULT: tuple(REGIONS.values())}


def region_ok(region: str, base: str, *, named: bool = False) -> str:
    """The region origin, refused unless it is the base itself or one of the base's documented region
    hosts. `named` is a host the person passed with --region-host: their word, checked only for shape."""
    r = origin(region, "the region host")
    if r == base or r in KNOWN_REGIONS.get(base, ()) or named:
        return r
    allowed = ", ".join((base, *KNOWN_REGIONS.get(base, ())))
    raise Refusal(f"the region host {urllib.parse.urlsplit(r).hostname} is not one of {allowed}; a token is "
                  f"never sent to another site (name a self-hosted region with --region-host)", 1)


# ─────────────────────────── the wire ───────────────────────────────────────

TOKEN_SHAPE = re.compile(r"[\x21-\x7e]{8,1024}")


def token_ok(value: object) -> bool:
    """Printable ASCII without a space: a value with a control character makes http.client raise an
    error whose message quotes the header, value included, so such a value is never sent."""
    return isinstance(value, str) and TOKEN_SHAPE.fullmatch(value) is not None


def _shape(path: str) -> str:
    """The endpoint without its identifiers: `/api/0/organizations/<id>/members/`."""
    keep = {"", "api", "0", "organizations", "projects", "releases", "members", "issues", "org-auth-tokens"}
    return "/".join(p if p in keep else "<id>" for p in path.split("?")[0].split("/"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Every redirect refused: urllib would follow one with the Authorization header to any host, and
    from https to http. The refusal names the status and the target host, never the path or a header."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlsplit(newurl).hostname or "an unnamed host"
        raise SentryError(code, f"Sentry answered HTTP {code} on {req.get_method()} "
                                f"{_shape(urllib.parse.urlsplit(req.full_url).path)}, redirecting to {host}; "
                                "not followed, so the token went nowhere else")


_OPENER = urllib.request.build_opener(_NoRedirect)


def _call(method: str, url: str, token: str, payload: dict | None = None) -> object:
    """One request; the decoded body. Errors name the endpoint's shape and the status, never a header,
    the body sent or the provider's body: an error that echoed a request is how a token reaches a
    transcript."""
    path = urllib.parse.urlsplit(url).path
    if not token_ok(token):
        raise SentryError("shape", f"a value meant for {_shape(path)} is not shaped like a token; it was not sent")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json",
        "Content-Type": "application/json", "User-Agent": "project-observatory"})
    try:
        with _OPENER.open(req, timeout=30) as r:
            raw = r.read()
    except SentryError:
        raise
    except urllib.error.HTTPError as e:
        e.close()
        raise SentryError(e.code, f"Sentry answered HTTP {e.code} on {method} {_shape(path)}; "
                                  "provider response withheld") from None
    except (OSError, http.client.HTTPException) as e:
        raise SentryError("transport", f"Sentry unreachable on {_shape(path)}: {type(e).__name__}") from None
    try:
        return json.loads(raw) if raw.strip() else {}
    except ValueError:
        raise SentryError("shape", f"Sentry answered {_shape(path)} with something that is not JSON") from None


def q(text: str) -> str:
    return urllib.parse.quote(text, safe="")


# ─────────────────────────── tokens ─────────────────────────────────────────

def kind_of(value: str) -> str:
    for prefix, kind in KINDS.items():
        if value.startswith(prefix):
            return kind
    return "unprefixed"


def org_payload(value: str) -> dict:
    """An organization token's own payload (`sntrys_<base64 json>_<secret>`): its organization, the
    Sentry it belongs to and its region. Read in memory; the secret half is never decoded."""
    if kind_of(value) != "org" or value.count("_") != 2:
        return {}
    try:
        raw = value[len("sntrys_"):value.rindex("_")]
        doc = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(doc, dict):
        return {}
    return {k: doc.get(k) for k in ("org", "url", "region_url") if isinstance(doc.get(k), str)}


def scopes_of(value: str, base: str) -> list[str] | None:
    """The scopes Sentry answers for a token at `GET /api/0/` (`auth.scopes`), or None when the answer
    carries none. A refusal (401: the token is dead) raises."""
    body = _call("GET", f"{base}/api/0/", value)
    scopes = ((body or {}).get("auth") or {}).get("scopes") if isinstance(body, dict) else None
    if isinstance(scopes, list) and all(isinstance(s, str) for s in scopes):
        return sorted(set(scopes))
    return None


def alive(value: str, base: str) -> bool:
    """True when Sentry still accepts the token, False on 401. Anything else is not an answer."""
    try:
        _call("GET", f"{base}/api/0/", value)
        return True
    except SentryError as exc:
        if exc.status == 401:
            return False
        raise


# ─────────────────────────── vault ──────────────────────────────────────────

def parse_slot(text: str | None, flag: str) -> tuple[str, str, str]:
    """PROJECT/ENV/NAME, validated by the vault's own rules before anything is called or minted."""
    parts = (text or "").removeprefix("vault:").split("/")
    if len(parts) != 3 or not all(parts):
        raise Refusal(f"{flag} must be PROJECT/ENV/NAME", 2)
    import vault
    try:
        folder, said = vault.project_folder(parts[0], parts[1], parts[2], verb="use")
        if said:
            print(f"project: {said}", file=sys.stderr)
        vault.validate_names(folder, parts[1], parts[2])
    except vault.VaultBoundaryError as exc:
        raise Refusal(f"{flag} {exc}", 2) from None
    return folder, parts[1], parts[2]


def read_slot(project: str, env: str, name: str) -> str:
    """A slot's value, from the vault only (`use_secret.resolve`, the door every agent uses). It goes
    no further than the caller's local variable."""
    import use_secret
    try:
        value, _where = use_secret.resolve(project, name, env, vault_only=True)
    except LookupError as exc:
        raise Refusal(str(exc), 1) from None
    if not token_ok(value):
        raise Refusal(f"{project}/{env}/{name} does not hold one token (whitespace or a control character "
                      f"inside it); nothing was sent", 1)
    return value


def slot_held(project: str, env: str, name: str) -> bool:
    import use_secret
    slot, _env = use_secret.vault_slot(project, name, env)
    return slot is not None


def deliver_to_vault(value: str, project: str, env: str, name: str) -> None:
    """Through the vault's own door, value on stdin; the child's output withheld."""
    try:
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "vault.py"), "put", project, env, name, "--force"],
                           input=value + "\n", capture_output=True, text=True, timeout=60)
    except (OSError, SubprocessError) as exc:
        raise RuntimeError(f"the vault's put for {project}/{env}/{name} did not finish ({type(exc).__name__}); "
                           "the slot may or may not hold the new value") from None
    if r.returncode != 0:
        raise RuntimeError(f"vault refused the slot {project}/{env}/{name} (exit {r.returncode}); "
                           "child output withheld")


def remove_slot(project: str, env: str, name: str) -> str:
    """`vault.py remove`: the value is dead, its copy goes. A refusal (an open leak to settle) is said."""
    try:
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "vault.py"), "remove", project, env, name],
                           capture_output=True, text=True, timeout=60)
        code = r.returncode
    except (OSError, SubprocessError) as exc:
        code = type(exc).__name__
    if code != 0 and slot_held(project, env, name):
        return (f"the vault kept {project}/{env}/{name} ({code}; an open leak must be settled first, "
                f"`vault.py leaks`)")
    return f"{project}/{env}/{name} removed from the vault"


def record_slot_expiry(project: str, env: str, name: str, reason: str) -> str:
    """The slot's metadata learns why its value has no expiry. Never raises: the value is delivered."""
    try:
        import vault
        return vault.record_expiry(project, env, name, None, reason, tool=TOOL)
    except Exception as exc:  # noqa: BLE001
        print(f"  ({project}/{env}/{name}: expiry not recorded: {type(exc).__name__})", file=sys.stderr)
        return "expiry not recorded"


# ─────────────────────────── the ledger ─────────────────────────────────────

def ledger() -> dict:
    private_io.check(LEDGER)
    if not LEDGER.exists():
        return {"issued": {}, "broad": {}}
    try:
        doc = json.loads(private_io.read(LEDGER))
    except (OSError, ValueError):
        raise Refusal("the Sentry ledger is unreadable; refusing to replace it", 1) from None
    if not isinstance(doc, dict) or not isinstance(doc.get("issued"), dict):
        raise Refusal("the Sentry ledger has an unsupported shape", 1)
    doc.setdefault("broad", {})
    return doc


def save_ledger(doc: dict) -> None:
    private_io.write(LEDGER, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")


def _proof_key() -> bytes:
    """The ledger's own random key (32 bytes, owner-only, beside the ledger, made on first use): what the
    proof digests are keyed with, so the ledger never holds a plain hash of a secret."""
    path = LEDGER.with_name("sentry-proof.key")
    private_io.check(path)
    if path.exists():
        try:
            key = bytes.fromhex(private_io.read(path).strip())
        except ValueError:
            key = b""
        if len(key) != 32:
            raise Refusal("the Sentry ledger's proof key is unreadable; refusing to replace it", 1)
        return key
    key = os.urandom(32)
    private_io.write(path, key.hex() + "\n")
    return key


def proof_digest(value: str) -> str:
    """HMAC-SHA256 of a token under the ledger's key: says "the same value", never the value."""
    return "hmac-sha256:" + hmac.new(_proof_key(), value.encode("utf-8"), hashlib.sha256).hexdigest()


def proof_holds(known: dict, value: str, kind: str) -> bool:
    """Whether a recorded broad-token proof is about THIS value: same kind and same keyed digest. Refuses
    (via `_proof_key`) on an unreadable key before anything is sent."""
    digest = proof_digest(value)
    return bool(known.get("proved_on")) and known.get("proved_kind") == kind \
        and hmac.compare_digest(str(known.get("proof_digest") or ""), digest)


def ledger_lock():
    """One door operation on the ledger at a time, across processes: held from the first read to the
    last write (issue, install, revoke, status)."""
    return private_io.lock(LEDGER.parent / ".sentry-ledger.lock")


def find_row(doc: dict, wanted: str) -> tuple[str, dict]:
    """A ledger row by its slot or by the token id Sentry gave it."""
    issued = doc["issued"]
    if wanted.removeprefix("vault:") in issued:
        key = wanted.removeprefix("vault:")
        return key, issued[key]
    for slot, row in issued.items():
        if row.get("token_id") is not None and str(row.get("token_id")) == wanted:
            return slot, row
    raise Refusal(f"no token issued or installed by this door for {wanted!r}; `sentry.py list` names them", 1)


# ─────────────────────────── the organization ───────────────────────────────

def choose_org(value: str, base: str, org: str | None) -> str:
    """--org, else the organization token's own, else the one organization the credential sees."""
    own = org_payload(value).get("org")
    if org and own and org != own:
        raise Refusal(f"the token belongs to organization {own!r}, not {org!r}", 1)
    if org or own:
        return org or own
    try:
        rows = _call("GET", f"{base}/api/0/organizations/", value)
    except SentryError as exc:
        raise Refusal(f"name the organization with --org: the token cannot list its organizations ({exc})", 2) \
            from None
    slugs = sorted(r.get("slug") for r in rows or [] if isinstance(r, dict) and isinstance(r.get("slug"), str))
    if len(slugs) == 1:
        return slugs[0]
    raise Refusal("name the organization with --org: the token sees "
                  + (", ".join(slugs) if slugs else "no organization"), 2)


def choose_region(value: str, base: str, org: str, region: str | None, region_url: str | None) -> str:
    """The organization's region origin: --region-host or --region, else the organization token's own
    `region_url`, else the organization's `links.regionUrl`, else the base (a self-hosted Sentry). A
    region read from a token or an answer must be the base or one of its documented region hosts."""
    if region_url:
        return region_ok(region_url, base, named=True)
    if region:
        if base != API_DEFAULT:
            raise Refusal("--region us|de names a sentry.io region; a self-hosted Sentry has one host", 2)
        return REGIONS[region]
    own = org_payload(value).get("region_url")
    if own:
        return region_ok(own, base)
    try:
        doc = _call("GET", f"{base}/api/0/organizations/{q(org)}/", value)
    except SentryError:
        return base
    found = ((doc or {}).get("links") or {}).get("regionUrl") if isinstance(doc, dict) else None
    return region_ok(found, base) if isinstance(found, str) and found else base


def check_base(value: str, base: str) -> None:
    """An organization token names the Sentry it belongs to: it is sent to no other."""
    url = org_payload(value).get("url")
    if url:
        try:
            own = origin(url, "the token's own Sentry")
        except Refusal:
            raise Refusal("the organization token names a Sentry this door does not send tokens to", 1) from None
        if own != base:
            raise Refusal(f"the organization token belongs to {own}, not {base}: pass --base-url {own}", 2)


# ─────────────────────────── verification ───────────────────────────────────

def probes(preset: dict, org: str, project: str | None) -> tuple[list[str], list[str]]:
    p = preset["purpose"]["project" if project else "org"]
    fill = {"org": q(org), "project": q(project or "")}
    return [p.format(**fill)], [o.format(**fill) for o in preset["outside"]]


def verify(value: str, preset_key: str, org: str, project: str | None, base: str, region: str,
           wait: float, fresh: bool = False) -> dict:
    """Prove a token with its own value: its scopes are exactly the preset's, every purpose read answers,
    every read outside the purpose is refused. Raises Refusal naming what failed. A `fresh` token (minted by
    this call) is asked again while it replicates; any other is asked once, so a dead one is said at once."""
    attempts = PROOF_ATTEMPTS if fresh else 1
    preset = PRESETS[preset_key]
    kind = kind_of(value)
    if kind != preset["kind"]:
        raise Refusal(f"preset {preset_key} is {KIND_WORDS[preset['kind']]}; the slot holds {KIND_WORDS[kind]}", 1)
    want = sorted(preset["scopes"])
    scopes = None
    for i in range(attempts):
        try:
            scopes = scopes_of(value, base)
            break
        except SentryError as exc:
            if exc.status in (403, 404) or i == attempts - 1:
                raise Refusal(f"Sentry does not accept the token ({exc})", 1) from None
            time.sleep(min(wait * 2 ** i, wait * 8))
    if scopes is not None and scopes != want:
        raise Refusal(f"the token holds {', '.join(scopes) or 'no scope'}; preset {preset_key} is exactly "
                      f"{', '.join(want)}", 1)
    purpose, outside = probes(preset, org, project)
    proved = []
    for path in purpose:
        reason = None
        for i in range(attempts):
            try:
                _call("GET", region + path, value)
                reason = None
                break
            except SentryError as exc:
                reason = str(exc)
                # A fresh token replicates to the region: 401 is retried, 403 and 404 are final.
                if exc.status in (403, 404):
                    break
            if i < attempts - 1:
                time.sleep(min(wait * 2 ** i, wait * 8))
        if reason:
            raise Refusal(f"the token cannot do its purpose read {_shape(path)} ({reason})", 1)
        proved.append(_shape(path))
    refused = []
    for path in outside:
        try:
            _call("GET", region + path, value)
        except SentryError as exc:
            if exc.status in (401, 403, 404):
                refused.append(_shape(path))
                continue
            raise Refusal(f"could not prove the token is narrow: {_shape(path)} gave no refusal ({exc}); "
                          f"retry", 1) from None
        raise Refusal(f"the token can read {_shape(path)}, outside preset {preset_key}: it is broader than "
                      f"declared", 1)
    return {"scopes": scopes, "scopes_confirmed": scopes is not None, "proved": proved, "refused": refused,
            "checked_on": today()}


# ─────────────────────────── recipes ────────────────────────────────────────

def pages(base: str, org: str | None) -> dict:
    o = q(org or "ORG")
    return {"org": f"{base}/settings/{o}/auth-tokens/",
            "integration": f"{base}/settings/{o}/developer-settings/new-internal/",
            "integrations": f"{base}/settings/{o}/developer-settings/",
            "personal": f"{base}/settings/account/api/auth-tokens/",
            "oauth-app": f"{base}/settings/account/api/",
            "unprefixed": f"{base}/settings/account/api/auth-tokens/"}


def recipe(preset_key: str, target: str, base: str, org: str | None, project: str | None,
           kind: str | None, missing: list[str] | None = None) -> str:
    """What a person does when no credential the door holds can mint the preset, and the command after."""
    page = pages(base, org)
    extra = (f" --org {org}" if org else "") + (f" --project {project}" if project else "") \
        + ("" if base == API_DEFAULT else f" --base-url {base}")
    install = (f"python \"$(project-observatory full-path)/tools/sentry.py\" install --preset {preset_key} "
               f"--vault {target}{extra}")
    put = (f"put it in {target} without a chat or an argument: ask through `observatory_secret_request` (the "
           f"Observatory window takes it), or run `python \"$(project-observatory full-path)/tools/vault.py\" put "
           f"{' '.join(target.split('/'))}` with a protected file on stdin")
    if preset_key == "releases":
        why = ("Sentry mints an organization token by API only for a browser session or an OAuth application's "
               "token holding `org:tokens` and one of `org:read`, `org:write`, `org:admin`"
               + (f"; --from holds {KIND_WORDS[kind]}" if kind else "")
               + (f" missing {', '.join(missing)}" if missing else ""))
        return (f"{why}.\n  a person makes it in one click: {page['org']} (Organization Tokens, "
                f"\"Create New Token\", name it `observatory releases {target}`; Sentry offers only org:ci there),\n"
                f"  {put},\n  then: {install}")
    return ("Sentry creates an internal integration's token only from a signed-in browser session "
            "(`/api/0/sentry-apps/{slug}/api-tokens/` takes no token).\n"
            f"  a person makes it: {page['integration']}, name `observatory read {target}`, permissions "
            f"Project: Read, Issue & Event: Read, Organization: Read, every other permission No Access, no "
            f"webhooks; save, then \"New Token\",\n  {put},\n  then: {install}")


# ─────────────────────────── issue and install ──────────────────────────────

def _say_default_base(base_url: str | None, base: str) -> None:
    """Without --base-url the door uses sentry.io: said, as a first trust is, because it is where the value goes
    (an organization token that names another Sentry is still refused by `check_base`)."""
    if not base_url:
        print(f"  no --base-url: using the default Sentry {base}, the only host a token is sent to here",
              file=sys.stderr)


def _row(preset_key: str, org: str, project: str | None, base: str, region: str, proof: dict, *,
         minted_by: str, token_id, name: str | None, from_slot: str | None, prior: dict | None,
         expiry_reason: str, value: str) -> dict:
    preset = PRESETS[preset_key]
    # The value this row describes, as `proof_holds` reads it: a later `status` or `revoke` sends the slot's
    # value to this row's Sentry only while it is still this one.
    return {"proved_on": today(), "proved_kind": kind_of(value), "proof_digest": proof_digest(value),"preset": preset_key, "kind": preset["kind"], "scopes": proof["scopes"] or list(preset["scopes"]),
            "scopes_confirmed": proof["scopes_confirmed"], "org": org, "project": project,
            "base_url": base, "region_url": region, "token_id": token_id, "name": name,
            "minted_by": minted_by, "from": from_slot,
            **key_expiry.fields(None, expiry_reason),
            "issued_on": today(), "previous_issued_on": (prior or {}).get("issued_on"),
            "verified": proof, "issued_by": "tools/sentry.py"}


def _delete_token(minter: str, base: str, org: str, token_id) -> str:
    """An organization token this door minted, deleted by the credential that minted it."""
    if token_id is None:
        return "no token id to delete"
    try:
        _call("DELETE", f"{base}/api/0/organizations/{q(org)}/org-auth-tokens/{q(str(token_id))}/", minter)
    except SentryError as exc:
        if exc.status == 404:
            return f"token {token_id} was already gone"
        return (f"token {token_id} could NOT be deleted ({exc}): delete it at {pages(base, org)['org']}")
    return f"token {token_id} deleted"


def cmd_issue(preset_key: str, target: str | None, source: str | None, org: str | None, project: str | None,
              base_url: str | None, region: str | None, region_url: str | None, wait: float = 2.0) -> int:
    preset = PRESETS.get(preset_key)
    if not preset:
        raise Refusal(f"unknown preset {preset_key!r}; have: {', '.join(PRESETS)}", 2)
    if not target or not source:
        raise Refusal("issue needs --from PROJECT/ENV/NAME (the credential that mints) and --vault "
                      "PROJECT/ENV/NAME (where the new token goes)", 2)
    base = origin(base_url or API_DEFAULT)
    _say_default_base(base_url, base)
    to = parse_slot(target, "--vault")
    frm = parse_slot(source, "--from")
    slot, from_slot = "/".join(to), "/".join(frm)
    if slot == from_slot:
        raise Refusal("--vault must be another slot than --from: the broad token stays where it is until "
                      "`revoke --from` retires it", 2)
    minter = read_slot(*frm)
    kind = kind_of(minter)
    if preset["mint"] is None:
        raise Refusal(recipe(preset_key, slot, base, org or org_payload(minter).get("org"), project, None), 3)
    if kind not in ("oauth-app", "unprefixed"):
        raise Refusal(recipe(preset_key, slot, base, org or org_payload(minter).get("org"), project, kind), 3)
    check_base(minter, base)
    with ledger_lock():
        try:
            held = scopes_of(minter, base)
        except SentryError as exc:
            raise Refusal(f"the credential in {from_slot} is not accepted ({exc})", 1) from None
        have = set(held or ())
        missing = ([MINT_CONSENT] if MINT_CONSENT not in have else []) \
            + ([" or ".join(MINT_ANY)] if not have & set(MINT_ANY) else [])
        if missing:
            raise Refusal(recipe(preset_key, slot, base, org, project, kind, missing), 3)
        if not have & set(DELETE_ANY):
            print(f"  note: the credential in {from_slot} cannot delete an organization token (no "
                  f"{' or '.join(DELETE_ANY)}): a token whose proof fails is left for a person to delete",
                  file=sys.stderr)
        org = choose_org(minter, base, org)
        reg = choose_region(minter, base, org, region, region_url)
        prior = ledger()["issued"].get(slot)
        name = f"observatory {preset_key} {slot} (managed)"
        try:
            made = _call("POST", f"{base}/api/0/organizations/{q(org)}/org-auth-tokens/", minter,
                         {"name": name, "scopes": list(preset["scopes"])})
        except SentryError as exc:
            raise Refusal(f"Sentry did not mint the organization token ({exc})", 1) from None
        token_id = (made or {}).get("id") if isinstance(made, dict) else None
        # FROM HERE A TOKEN EXISTS AT SENTRY. Every way out either records it in the ledger or deletes it
        # (and says so), whatever the exception: a vault put that timed out, a full disk, an interrupt.
        delivered = False
        try:
            value = (made or {}).get("token") if isinstance(made, dict) else None
            if not token_ok(value):
                raise Refusal("Sentry answered the mint without a usable token", 1)
            proof = verify(value, preset_key, org, project, base, reg, wait, fresh=True)
            deliver_to_vault(value, *to)
            delivered = True
            expiry = record_slot_expiry(*to, ORG_NO_EXPIRY)
            doc = ledger()
            doc["issued"][slot] = _row(preset_key, org, project, base, reg, proof, minted_by="door",
                                       token_id=token_id, name=name, from_slot=from_slot, prior=prior,
                                       expiry_reason=ORG_NO_EXPIRY, value=value)
            # The minting credential answered on this base: the proof `revoke --from` needs before a 401
            # can count as its revocation.
            doc["broad"][from_slot] = {**(doc["broad"].get(from_slot) or {}), "kind": kind, "base_url": base,
                                       "org": org, "state": (doc["broad"].get(from_slot) or {}).get("state")
                                       or "in-use", "proved_on": today(), "proved_kind": kind,
                                       "proof_digest": proof_digest(minter),
                                       "proof": "GET /api/0/ answered 200 on this base at issue"}
            save_ledger(doc)
        except BaseException as exc:  # noqa: BLE001  any exit after the mint is cleaned up, then re-raised
            tail = _delete_token(minter, base, org, token_id)
            what = str(exc) if isinstance(exc, (Refusal, RuntimeError)) else f"{type(exc).__name__} (details withheld)"
            if delivered:
                what += f"; {slot} now holds that token's value, which is dead once deleted: issue again"
            _journal("issue-failed", slot, preset=preset_key, org=org, token=token_id, cleanup=tail)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                print(f"interrupted after the mint; the token this call minted: {tail}", file=sys.stderr)
                raise
            raise Refusal(f"{what}; the token this call minted: {tail}", 1) from None
        # The slot's previous token goes only once the new one is delivered AND recorded.
        retired = ""
        if prior and prior.get("minted_by") == "door" and prior.get("token_id") not in (None, token_id) \
                and prior.get("org") == org and prior.get("base_url") == base:
            retired = f"; the slot's previous token: {_delete_token(minter, base, org, prior['token_id'])}"
        elif prior and prior.get("minted_by") == "installed":
            page = pages(prior.get("base_url") or base, prior.get("org"))["org" if prior.get("kind") == "org"
                                                                            else "integrations"]
            retired = (f"; the slot's previous token was made by a person and stays live at Sentry until it is "
                       f"deleted at {page}")
    _journal("issue", slot, preset=preset_key, org=org, project=project, token=token_id, minted_from=from_slot)
    print(f"  {slot}: issued organization token {token_id} for {org}, scope {', '.join(preset['scopes'])}, "
          f"{expiry}{retired}")
    _say_proof(proof, project)
    print(f"  use: python \"$(project-observatory full-path)/tools/use_secret.py\" run --env {to[1]} "
          f"{to[0]} {to[2]} -- <command>")
    return 0


def _say_proof(proof: dict, project: str | None) -> None:
    print(f"  verified with its own value: it read {', '.join(proof['proved'])}; it was refused "
          f"{', '.join(proof['refused'])}"
          + ("" if proof["scopes_confirmed"] else "; Sentry's answer did not list its scopes, so the reads alone "
             "bound it"))
    if project:
        print(f"  Sentry scopes an organization or integration token to the whole organization: project "
              f"{project} is its stated purpose and what the proof read, not a limit Sentry enforces")


def cmd_install(preset_key: str, target: str | None, org: str | None, project: str | None,
                base_url: str | None, region: str | None, region_url: str | None, wait: float = 2.0) -> int:
    """Verify and record a token a person made (the slot already holds it); nothing is minted."""
    if preset_key not in PRESETS:
        raise Refusal(f"unknown preset {preset_key!r}; have: {', '.join(PRESETS)}", 2)
    if not target:
        raise Refusal("install needs --vault PROJECT/ENV/NAME, the slot that holds the token", 2)
    base = origin(base_url or API_DEFAULT)
    _say_default_base(base_url, base)
    to = parse_slot(target, "--vault")
    slot = "/".join(to)
    value = read_slot(*to)
    kind = kind_of(value)
    preset = PRESETS[preset_key]
    if kind != preset["kind"]:
        raise Refusal(f"preset {preset_key} is {KIND_WORDS[preset['kind']]} and {slot} holds "
                      f"{KIND_WORDS[kind]}; nothing was recorded.\n  "
                      + recipe(preset_key, slot, base, org, project, None), 3 if kind in ("personal", "oauth-app") else 1)
    check_base(value, base)
    with ledger_lock():
        prior = ledger()["issued"].get(slot)
        if prior and prior.get("minted_by") == "door":
            raise Refusal(f"{slot} is recorded as a token this door minted (id {prior.get('token_id')}); installing "
                          f"over it would leave that token live at Sentry and unrecorded: `sentry.py revoke {slot}` "
                          f"first", 1)
        org = choose_org(value, base, org)
        reg = choose_region(value, base, org, region, region_url)
        proof = verify(value, preset_key, org, project, base, reg, wait)
        reason = ORG_NO_EXPIRY if kind == "org" else key_expiry.MINTED_ELSEWHERE
        expiry = record_slot_expiry(*to, reason)
        doc = ledger()
        doc["issued"][slot] = _row(preset_key, org, project, base, reg, proof, minted_by="installed",
                                   token_id=None, name=None, from_slot=None, prior=prior, expiry_reason=reason,
                                   value=value)
        save_ledger(doc)
    _journal("install", slot, preset=preset_key, org=org, project=project)
    print(f"  {slot}: installed {KIND_WORDS[kind]} for {org}, scopes {', '.join(proof['scopes'] or preset['scopes'])}, "
          f"{expiry}")
    _say_proof(proof, project)
    return 0


# ─────────────────────────── revoke ─────────────────────────────────────────

def _narrow_value_ok(slot: str, row: dict, value: str) -> None:
    """A narrow slot's value is sent to its row's Sentry only while it is the value the row recorded (same
    kind, same keyed digest) and, for an organization token, only to the Sentry it names. Raises Refusal
    before any send."""
    check_base(value, row["base_url"])
    if not proof_holds(row, value, kind_of(value)):
        raise Refusal(f"{slot} no longer holds the token recorded for it on {row['base_url']} (replaced, or no "
                      f"digest was recorded); nothing sent. `sentry.py install --preset {row['preset']} --vault "
                      f"{slot}` records the value it holds now, or `sentry.py issue` mints a new one", 1)


def _verify_row(slot: str, row: dict, wait: float) -> tuple[bool, str]:
    try:
        value = read_slot(*slot.split("/"))
        _narrow_value_ok(slot, row, value)
        proof = verify(value, row["preset"], row["org"], row.get("project"), row["base_url"],
                       row.get("region_url") or row["base_url"], wait)
    except (Refusal, SentryError) as exc:
        return False, str(exc)
    row["verified"] = proof
    return True, f"verified: read {', '.join(proof['proved'])}, refused {', '.join(proof['refused'])}"


def cmd_revoke(target: str | None, source: str | None, org: str | None, base_url: str | None,
               wait: float = 2.0, forget: bool = False) -> int:
    if bool(target) == bool(source):
        raise Refusal("revoke takes one slot this door issued (`revoke PROJECT/ENV/NAME`) or the broad "
                      "token (`revoke --from PROJECT/ENV/NAME`)", 2)
    if source:
        return _revoke_broad(source, org, base_url, wait)
    with ledger_lock():
        doc = ledger()
        slot, row = find_row(doc, target)
        project, env, name = slot.split("/")
        base, org_ = row["base_url"], row["org"]
        value = read_slot(project, env, name) if slot_held(project, env, name) else None
        if value is not None:
            _narrow_value_ok(slot, row, value)
        said = ""
        if row.get("minted_by") == "door" and row.get("token_id") is not None and row.get("from"):
            minter = read_slot(*row["from"].split("/"))
            known = doc["broad"].get(row["from"]) or {}
            # The minting credential goes only to the Sentry it was proved on, and only while the slot still
            # holds that value: a rotated or foreign token is never sent to delete anything.
            check_base(minter, base)
            if known.get("base_url") != base or not proof_holds(known, minter, kind_of(minter)):
                raise Refusal(f"{slot}: {row['from']} no longer holds the credential that minted token "
                              f"{row['token_id']} on {base} (replaced, or never proved there); nothing was sent "
                              f"and nothing removed. While it is alive, `sentry.py revoke --from {row['from']} "
                              f"--base-url {base}` proves it again; or a person deletes token {row['token_id']} "
                              f"at {pages(base, org_)['org']}, then `sentry.py revoke {slot}` again", 1)
            said = _delete_token(minter, base, org_, row["token_id"])
            if "could NOT" in said:
                raise Refusal(f"{slot}: {said}; the slot and its record are kept", 1)
        if value is not None:
            # A deletion this call made may take a moment to reach every host: asked again, never assumed.
            for i in range(PROOF_ATTEMPTS if said else 1):
                try:
                    live = alive(value, base)
                except SentryError as exc:
                    raise Refusal(f"{slot}: could not tell whether Sentry still accepts the token ({exc}); "
                                  f"nothing was removed", 1) from None
                if not live:
                    break
                if i < (PROOF_ATTEMPTS if said else 1) - 1:
                    time.sleep(min(wait * 2 ** i, wait * 8))
            if live:
                page = pages(base, org_)["org" if row["kind"] == "org" else "integrations"]
                raise Refusal(f"{slot}: Sentry still accepts the token"
                              + (f" after this call's delete ({said})" if said else
                                 ". This door cannot delete it by API")
                              + f": a person deletes it at {page}, then `sentry.py revoke {slot}` again; "
                                f"nothing was removed", 3)
        if value is None and not said.endswith(("deleted", "already gone")):
            page = pages(base, org_)["org" if row["kind"] == "org" else "integrations"]
            if not forget:
                raise Refusal(f"{slot}: its value is gone from the vault, so this door cannot prove the token dead; "
                              f"the record is kept. A person deletes it at {page}, then `sentry.py revoke {slot} "
                              f"--forget` drops the record", 3)
        removed = remove_slot(project, env, name) if value is not None else "the slot held no value"
        if "kept" in removed:
            raise Refusal(f"{slot}: revoked at Sentry, but {removed}; the record is kept", 1)
        doc = ledger()
        doc["issued"].pop(slot, None)
        save_ledger(doc)
    _journal("forget" if value is None and not said else "revoke", slot, preset=row["preset"], org=org_,
             token=row.get("token_id"))
    print(f"  {slot}: " + (f"{said}; " if said else "")
          + ("Sentry now refuses it (401); " if value is not None else
             "no value was left to prove dead, so this door did NOT observe a revocation; ")
          + f"{removed}; the record is gone")
    return 0


def _revoke_broad(source: str, org: str | None, base_url: str | None, wait: float) -> int:
    """Retire the broad token in `source` once a narrow one verifies.

    The value is sent to ONE Sentry only: --base-url, or the base recorded for this slot the last time it
    answered there. A 401 proves a revocation only after a 200 from that same base was recorded: a Sentry
    that never accepted the value answers 401 to anything, a stranger's token included."""
    frm = parse_slot(source, "--from")
    from_slot = "/".join(frm)
    with ledger_lock():
        doc = ledger()
        known = doc["broad"].get(from_slot) or {}
        if base_url:
            base = origin(base_url)
            if not known.get("base_url"):
                print(f"  {from_slot}: trusting {base} as this token's Sentry for the first time (--base-url); "
                      f"it is the only host the value is sent to", file=sys.stderr)
        elif known.get("base_url") and known.get("proved_on"):
            base = known["base_url"]
        else:
            raise Refusal(f"name the Sentry {from_slot} belongs to with --base-url (https://sentry.io for SaaS): "
                          f"no answer from it was recorded for this slot, and a token is sent only to its own "
                          f"Sentry; nothing was sent", 2)
        value = read_slot(*frm)
        kind = kind_of(value)
        if kind == "unprefixed":
            raise Refusal(f"{from_slot} does not hold a Sentry token this door can recognise (none of "
                          f"{', '.join(KINDS)}); it was not sent anywhere and the slot is kept", 1)
        check_base(value, base)
        matches = proof_holds(known, value, kind)
        # A base taken from the record holds only for the value it was recorded with: a replaced value
        # (a rotation, another Sentry's token) is not sent there until a person names its Sentry.
        if not base_url and not matches:
            doc["broad"][from_slot] = {**known, "state": "unproven", "unproven_why":
                                       "the value in the slot is not the one recorded for this base"}
            for stale in ("proved_on", "proved_kind", "proof_digest", "proof"):
                doc["broad"][from_slot].pop(stale, None)
            save_ledger(doc)
            raise Refusal(f"{from_slot} no longer holds the value recorded for {base} (it was replaced, or is "
                          f"another kind of token): nothing was sent and the slot is kept. Name its Sentry with "
                          f"--base-url; a `revoke --from` that finds it alive there records a new proof", 2)
        if known.get("base_url") and known["base_url"] != base:
            if matches:
                raise Refusal(f"{from_slot} holds the value {known['base_url']} accepted, so it is that Sentry's "
                              f"token, not {base}'s; nothing was sent and the slot is kept", 1)
            # The recorded base described another value (or none was proved): the person's word replaces it.
            print(f"  {from_slot}: the value is not the one recorded for {known['base_url']}; trusting {base} "
                  f"instead (--base-url), the only host it is sent to", file=sys.stderr)
            known = {k: v for k, v in known.items()
                     if k not in ("proved_on", "proved_kind", "proof_digest", "proof", "requested_on")}
        rows = {s: r for s, r in doc["issued"].items() if s != from_slot and r.get("base_url") == base
                and (org is None or r.get("org") == org)}
        orgs = sorted({r.get("org") for r in rows.values()})
        if org is None and len(orgs) > 1:
            raise Refusal(f"the ledger holds tokens for {', '.join(orgs)}: name the organization the broad token "
                          f"is being replaced for with --org", 2)
        if not rows:
            raise Refusal(f"no narrow token is recorded for {org or 'any organization'} on {base}; issue or "
                          f"install one first. {from_slot} is kept", 1)
        passed, report = [], []
        for slot, row in sorted(rows.items()):
            ok, why = _verify_row(slot, row, wait)
            report.append(f"    {slot} ({row['preset']}): {why}")
            if ok:
                passed.append(slot)
        save_ledger(doc)
        print("  narrow tokens checked now:\n" + "\n".join(report))
        if not passed:
            raise Refusal(f"no narrow token verified now; {from_slot} is kept", 1)
        org_ = rows[passed[0]]["org"]
        try:
            live = alive(value, base)
        except SentryError as exc:
            raise Refusal(f"could not tell whether Sentry still accepts {from_slot} ({exc}); it is kept", 1) from None
        entry = {**known, "kind": kind, "org": org_, "base_url": base, "replaced_by": passed, "checked_on": today()}
        if live:
            page = pages(base, org_)["org" if kind == "org" else "integrations" if kind == "integration" else kind]
            doc = ledger()
            doc["broad"][from_slot] = {**entry, "state": "revocation-requested", "proved_on": today(),
                                       "proved_kind": kind, "proof_digest": proof_digest(value),
                                       "proof": "GET /api/0/ answered 200 on this base",
                                       "requested_on": known.get("requested_on") or today()}
            save_ledger(doc)
            _journal("revoke-requested", from_slot, kind=kind, org=org_)
            raise Refusal(f"{from_slot} holds {KIND_WORDS[kind]} and Sentry still accepts it. Sentry revokes "
                          f"{'it' if kind != 'org' else 'an organization token'} from a signed-in browser session "
                          f"only: a person deletes it at {page}, then `sentry.py revoke --from {from_slot}` "
                          f"again proves it dead and removes the slot", 3)
        # The recorded 200 proves only the value it was recorded for: the slot may since hold another one
        # (a rotation, a `vault.py put --force`, another Sentry's token), and a 401 to that says nothing.
        if not proof_holds(known, value, kind):
            why = ("no earlier answer from it showed the token belonged there" if not known.get("proved_on") else
                   "the value in the slot is not the one that answered there before (it was replaced, or is "
                   "another kind of token)")
            doc = ledger()
            doc["broad"][from_slot] = {**entry, "state": "unproven", "unproven_why": why}
            for stale in ("proved_on", "proved_kind", "proof_digest", "proof"):
                doc["broad"][from_slot].pop(stale, None)
            save_ledger(doc)
            raise Refusal(f"{base} refuses {from_slot} (401), but {why}: a 401 alone does not prove it was revoked. "
                          f"The slot is kept. A new proof is needed: a `sentry.py revoke --from` that finds the "
                          f"value alive records one. If this Sentry's token is already deleted, remove the slot "
                          f"yourself with `vault.py remove {' '.join(frm)}`", 1)
        removed = remove_slot(*frm)
        doc = ledger()
        doc["broad"][from_slot] = {**entry, "state": "revoked-verified", "revoked_on": today(),
                                   "slot": "kept" if "kept" in removed else "removed"}
        save_ledger(doc)
    _journal("revoke", from_slot, kind=kind, org=org_, verified="401 after a recorded 200")
    print(f"  {from_slot}: Sentry refuses it now (401) after accepting it on {known.get('proved_on')}, revocation "
          f"verified; {removed}")
    return 0 if "kept" not in removed else 1


# ─────────────────────────── list, status, presets ──────────────────────────

def cmd_presets() -> int:
    for key, p in PRESETS.items():
        print(f"{key}: {KIND_WORDS[p['kind']]}, scopes {', '.join(p['scopes'])}")
        print(f"    {p['why']}")
        print("    minted by: " + ("`issue`, from an OAuth application's token holding org:tokens and one of "
                                   "org:read, org:write, org:admin; otherwise a person makes it and `install` "
                                   "verifies it"
                                   if p["mint"] else "a person in Sentry's settings; `install` verifies it"))
    return 0


def cmd_list() -> int:
    doc = ledger()
    rows = doc["issued"]
    print(f"Sentry tokens issued or installed by this door ({len(rows)}), values never shown:")
    for slot, r in sorted(rows.items()):
        print(f"  {slot:40} {r.get('preset'):9} {r.get('org')}" + (f"/{r['project']}" if r.get("project") else "")
              + f"  {', '.join(r.get('scopes') or [])}  {r.get('minted_by')}"
              + (f" id {r['token_id']}" if r.get("token_id") is not None else "")
              + f"  since {r.get('issued_on')}  verified {(r.get('verified') or {}).get('checked_on', '-')}"
              + f"  {key_expiry.describe(r)}")
    for slot, b in sorted(doc["broad"].items()):
        print(f"  broad {slot}: {b.get('state')} ({KIND_WORDS.get(b.get('kind'), b.get('kind'))}, {b.get('org')}, "
              f"checked {b.get('checked_on')})")
    if not rows and not doc["broad"]:
        print("  none yet: `sentry.py issue` or `sentry.py install`")
    return 0


def cmd_status(target: str | None, wait: float = 2.0) -> int:
    bad = 0
    with ledger_lock():
        doc = ledger()
        rows = dict([find_row(doc, target)]) if target else dict(doc["issued"])
        for slot, row in sorted(rows.items()):
            ok, why = _verify_row(slot, row, wait)
            print(f"  {slot} ({row['preset']}, {row['org']}): {why}", file=sys.stdout if ok else sys.stderr)
            bad += 0 if ok else 1
        for slot, b in sorted(doc["broad"].items()) if not target else ():
            if b.get("state") == "revoked-verified" and b.get("slot") == "removed":
                print(f"  broad {slot}: revoked, verified on {b.get('revoked_on')}")
                continue
            if b.get("state") == "unproven" or not b.get("proved_on"):
                print(f"  broad {slot}: unproven ({b.get('unproven_why') or 'no answer from its Sentry recorded'});"
                      f" nothing sent. `sentry.py revoke --from {slot} --base-url URL` records a new proof",
                      file=sys.stderr)
                bad += 1
                continue
            try:
                value = read_slot(*slot.split("/"))
                kind = kind_of(value)
                # The recorded base holds only for the value it was proved with: a rotated or foreign
                # value is never sent there, not even to ask whether it still answers.
                if not proof_holds(b, value, kind):
                    print(f"  broad {slot}: the slot no longer holds the value proved on {b['base_url']} "
                          f"(replaced, or another kind of token); nothing sent. `sentry.py revoke --from {slot} "
                          f"--base-url URL` records a new proof", file=sys.stderr)
                    bad += 1
                    continue
                check_base(value, b["base_url"])
                live = alive(value, b["base_url"])
            except (Refusal, SentryError) as exc:
                print(f"  broad {slot}: could not read it ({exc})", file=sys.stderr)
                bad += 1
                continue
            b["checked_on"] = today()
            print(f"  broad {slot}: " + ("still accepted by Sentry; it waits for a person to delete it"
                                         if live else "refused by Sentry (401): `sentry.py revoke --from` removes it"))
        save_ledger(doc)
    if not rows and not doc["broad"]:
        print("  nothing issued, installed or retired")
    return 1 if bad else 0


# ─────────────────────────── main ───────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sentry.py", description=__doc__.splitlines()[0],
                                 epilog="Values never travel in an argument; slots are PROJECT/ENV/NAME.")
    sub = ap.add_subparsers(dest="verb", required=True)
    sub.add_parser("presets", help="each preset's token kind, scopes and how it is minted")

    def hosts(p):
        p.add_argument("--org", help="the organization's slug (default: the token's own, or the only one it sees)")
        p.add_argument("--project", help="the project the token is for: its stated purpose and what the proof reads")
        p.add_argument("--base-url", help=f"the Sentry the tokens belong to (default {API_DEFAULT})")
        g = p.add_mutually_exclusive_group()
        g.add_argument("--region", choices=sorted(REGIONS), help="a sentry.io region (default: the organization's)")
        g.add_argument("--region-host", dest="region_url",
                       help="the organization's region host, by your word (a self-hosted Sentry with a separate "
                            "region host); otherwise only the base or its documented region hosts are used")

    p = sub.add_parser("issue", help="mint a narrow token from the credential in --from into --vault")
    p.add_argument("--preset", required=True, choices=sorted(PRESETS))
    p.add_argument("--from", dest="source", required=True, metavar="PROJECT/ENV/NAME")
    p.add_argument("--vault", required=True, metavar="PROJECT/ENV/NAME")
    hosts(p)
    p = sub.add_parser("install", help="verify and record a token a person made, already in --vault")
    p.add_argument("--preset", required=True, choices=sorted(PRESETS))
    p.add_argument("--vault", required=True, metavar="PROJECT/ENV/NAME")
    hosts(p)
    sub.add_parser("list", help="what this door issued, installed and retired; never a value")
    p = sub.add_parser("status", help="verify every recorded token now, and read whether retired ones still answer")
    p.add_argument("target", nargs="?", metavar="PROJECT/ENV/NAME")
    p = sub.add_parser("revoke", help="revoke one recorded token, or (--from) the broad token after a narrow one verifies")
    p.add_argument("target", nargs="?", metavar="PROJECT/ENV/NAME|TOKEN_ID")
    p.add_argument("--from", dest="source", metavar="PROJECT/ENV/NAME")
    p.add_argument("--org")
    p.add_argument("--base-url")
    p.add_argument("--forget", action="store_true",
                   help="drop the record of a token whose value is already gone from the vault (no proof possible)")
    a = ap.parse_args(argv)
    try:
        if a.verb == "presets":
            return cmd_presets()
        if a.verb == "issue":
            return cmd_issue(a.preset, a.vault, a.source, a.org, a.project, a.base_url, a.region, a.region_url)
        if a.verb == "install":
            return cmd_install(a.preset, a.vault, a.org, a.project, a.base_url, a.region, a.region_url)
        if a.verb == "list":
            return cmd_list()
        if a.verb == "status":
            return cmd_status(a.target)
        return cmd_revoke(a.target, a.source, a.org, a.base_url, forget=a.forget)
    except Refusal as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return exc.code
    except SentryError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001  a message could quote a request; only the class leaves
        print(f"failed: an unexpected {type(exc).__name__}; details withheld, no value printed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
