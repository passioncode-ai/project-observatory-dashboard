#!/usr/bin/env python3
"""The one door to Cloudflare credentials: stash, issue, rotate, revoke, ping.

    T="$(project-observatory full-path)/tools"          # the installed engine's tools
    python "$T/cloudflare.py" stash < token-file   # admin token on stdin from a protected file, once per account
    python "$T/cloudflare.py" issue --preset analytics [--project project:x]
    python "$T/cloudflare.py" issue --preset dns-edit --zone example.com --vault PROJECT/ENV/NAME
    python "$T/cloudflare.py" issue --preset zone-analytics-read --zone example.com --vault PROJECT/ENV/NAME
    python "$T/cloudflare.py" issue --preset d1-edit --account <slug> --vault PROJECT/ENV/NAME
    python "$T/cloudflare.py" issue --preset r2-bucket --account <slug> --bucket NAME \
        [--jurisdiction eu] [--expire-days 30] [--lifecycle-rule PREFIX:DAYS ...] \
        --vault PROJECT/ENV/PREFIX
    python "$T/cloudflare.py" lifecycle --account <slug> --bucket NAME [--jurisdiction eu] \
        [--expire-days 92] [--lifecycle-rule staging/:2 ...]   # rules only, no key
    python "$T/cloudflare.py" issue --preset email-send --account <slug> --vault PROJECT/ENV/NAME
    python "$T/cloudflare.py" issue --preset email-routing --zone example.com --vault PROJECT/ENV/NAME
    python "$T/cloudflare.py" issue --preset workers-edit --account <slug> --vault PROJECT/ENV/NAME
    python "$T/cloudflare.py" issue --preset fabric-inbox-server --account <slug> --vault PROJECT/ENV/NAME
    python "$T/cloudflare.py" issue --preset fabric-inbox-account --account <slug> --vault PROJECT/ENV/NAME
    python "$T/cloudflare.py" issue --preset workers-observability-read --account <slug> --vault PROJECT/ENV/NAME
    python "$T/cloudflare.py" list
    python "$T/cloudflare.py" groups --account <slug> --match "email sending"
    python "$T/cloudflare.py" ping
    python "$T/cloudflare.py" rotate <label>   # or --leaked, for every open leak
    python "$T/cloudflare.py" revoke <label>

THE ADMIN TOKEN IS STASHED AND NEVER HANDED OUT. It carries write access to
everything the account has — billing, DNS, Workers, and the power to mint more
tokens — so the plugin that reads request counters must never hold it. It lives
in `secrets/cloudflare-admin/` at mode 600 and is read by exactly one thing:
this program, in memory, for the length of one API exchange. Every operational
token is ISSUED from it, narrow, and it is the narrow one that reaches a file a
plugin reads.

WHY A PROGRAM AND NOT A PROCEDURE. The same reasoning the vault was built on:
a rule that a human follows is a rule that holds until the day it is
inconvenient. Values travel on stdin, never in argv, where the shell history,
`ps` and an agent's transcript would all keep them. Nothing here prints a value,
including on error — an exception message that echoes a request header is
exactly how a credential ends up in a transcript.

EVERY ISSUED TOKEN CARRIES ITS OWN RECORD — `<label>.meta.json` beside it, which
holds the account, the purpose, the project it was issued for, and its rotation
history, but never the value. That file is what makes `list`, `ping` and the
credentials projection able to answer "what exists, what reads it, when was it
last rotated" without anyone opening a secret.

ROTATION IS ROLLING, NOT REPLACING. Cloudflare can issue a fresh value for an
existing token id, which keeps the token's identity, its permissions and its
name — so a rotation after a leak changes exactly the thing that leaked, and
nothing downstream has to learn a new name.
"""
from __future__ import annotations
import argparse
import datetime
import hashlib
import hmac
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins"))
import private_io
import paths                                                                    

API = "https://api.cloudflare.com/client/v4"
ADMIN_STORE = paths.source_path("secret_store", paths.SECRETS) / 'cloudflare-admin'
#: The name every token this program issues carries, per preset. Finding it
#: again is what makes a second issue ROLL one token instead of leaving a trail
#: of orphans nobody can identify in the operator's account.
PRESETS: dict[str, dict] = {
    "analytics": {
        "name": "observatory-analytics-read (managed)",
        # DNS Read joined on 2026-09-14: where a zone's records point
        # — herokudns, pages.dev, a Vercel alias — is the cheapest measured link
        # from a domain to the thing that serves it, and from there to a project.
        "groups": ("Zone Read", "Analytics Read", "DNS Read"),
        "why": "reads zone request counters and DNS targets for the observatory's dashboard",
    },
    # DNS WRITE, ONE ZONE, INTO THE VAULT. A project that must point a hostname
    # at its host (a Pages custom domain, a CNAME to a PaaS) needs exactly
    # "edit records in this zone" — not the account, not a second zone. The
    # token is scoped to the zone's own resource id, named after the zone so a
    # second issue ROLLS it, verified by reading that zone's records, and
    # delivered to a vault slot rather than to the analytics plugin's folder:
    # a writer's token is a project's credential, used through use_secret.py.
    "dns-edit": {
        "name": "observatory-dns-edit {zone} (managed)",
        "groups": ("Zone Read", "DNS Write"),
        "why": "edits DNS records in one zone for the project named by the vault slot",
        "scope": "zone",
        "probe": "/zones/{zone_id}/dns_records?per_page=1",
    },
    # ANALYTICS READ, ONE ZONE, INTO THE VAULT. An agent that measures one site
    # (a brand agent counting its site's weekly visitors, server side, without a
    # script on the page) needs that zone's counters and nothing else: not the
    # account's other zones, not DNS, and not the plugin folder, which every
    # zone of the account can read. Verified with the GraphQL read the analytics
    # plugin itself does, because listing a zone is not reading its analytics.
    "zone-analytics-read": {
        "name": "observatory-zone-analytics-read {zone} (managed)",
        "groups": ("Zone Read", "Analytics Read"),
        "why": "reads one zone's request and visitor counters for the project named by the vault slot",
        "scope": "zone",
        "verify": "analytics",
    },
    # D1 WRITE, ONE ACCOUNT, INTO THE VAULT. A project whose Worker keeps its
    # data in D1 needs a machine that can write that database — `wrangler d1
    # execute --remote` from a signing machine or a scheduled agent — and
    # nothing else: not Workers deploys, not DNS, not a second account. D1 has
    # no per-database grant, so the account is the narrowest resource there is.
    # The token is named after its SLOT, so each project's writer rolls on its
    # own, and it is verified by listing the account's databases with the NEW
    # value before the vault ever holds it.
    "d1-edit": {
        "name": "observatory-d1-edit {slot} (managed)",
        "groups": ("D1 Write",),
        "level": "account",
        "why": "writes the D1 databases of one account for the project named by the vault slot",
        "scope": "account-vault",
        "probe": "/accounts/{account_id}/d1/database?per_page=1",
    },
    # TRANSACTIONAL EMAIL, ONE ACCOUNT, INTO THE VAULT. An application that
    # sends through Cloudflare Email Service's REST API needs Email Sending on
    # the account that holds its sending domain — Cloudflare offers the group
    # at the account level only (`groups` on 2026-09-30), so the account is the
    # narrowest grant. Read joins Write so the holder can see its own
    # suppression list; verified by listing it with the NEW value.
    "email-send": {
        "name": "observatory-email-send {slot} (managed)",
        "groups": ("Email Sending Write", "Email Sending Read"),
        "level": "account",
        "why": "sends transactional email from one account's onboarded domains for the project named by the vault slot",
        "scope": "account-vault",
        "probe": "/accounts/{account_id}/email/sending/suppressions?per_page=1",
    },
    # WORKERS, ONE ACCOUNT, INTO THE VAULT. Deploying a Worker (an inbound
    # email handler, a probe) and the KV namespace it writes — not Pages, not
    # DNS, not routes. Verified by listing the account's scripts.
    "workers-edit": {
        "name": "observatory-workers-edit {slot} (managed)",
        "groups": ("Workers Scripts Write", "Workers KV Storage Write"),
        "level": "account",
        "why": "deploys Workers and their KV namespaces on one account for the project named by the vault slot",
        "scope": "account-vault",
        "probe": "/accounts/{account_id}/workers/scripts",
    },
    # EMAIL ROUTING RULES, ONE ZONE, INTO THE VAULT. Adding a rule that sends
    # one address to a Worker, without the power to change the zone's MX, DNS
    # or the routing settings themselves. Verified by listing the zone's rules.
    # Named after the zone AND the slot: two projects routing mail in one zone
    # hold two tokens, where a zone-only name would make the second issue roll
    # the first project's token and kill the value its slot still holds.
    "email-routing": {
        "name": "observatory-email-routing {zone} {slot} (managed)",
        "groups": ("Zone Read", "Email Routing Rules Write"),
        "why": "edits Email Routing rules in one zone for the project named by the vault slot",
        "scope": "zone",
        "probe": "/zones/{zone_id}/email/routing/rules?per_page=1",
    },
    # R2 OBJECTS, ONE BUCKET, INTO THE VAULT. An off-site backup needs to put
    # and read objects in one bucket — not list the account's other buckets,
    # not change a bucket's settings. `Bucket Item Write` scoped to the
    # bucket's own resource is exactly that, and R2 turns the token into an S3
    # key pair (id = token id, secret = sha256 of the value), so what reaches the
    # vault is the pair a `curl --aws-sigv4` or an S3 SDK takes. Creating the
    # bucket and its lifecycle is a DIFFERENT right (account-level Storage
    # Write); that token is minted for the setup, used, and deleted before the
    # command returns — see `R2_SETUP`. Before delivery the pair must prove a
    # put, a get and a delete, and prove that listing buckets is REFUSED: a
    # pair that can see the account's other buckets is not the one asked for.
    "r2-bucket": {
        "name": "observatory-r2-bucket {jurisdiction}/{bucket} (managed)",
        "groups": ("Workers R2 Storage Bucket Item Write",),
        "level": "bucket",
        "why": "reads and writes the objects of one R2 bucket for the project named by the vault slot",
        "scope": "bucket-vault",
    },
    # A FABRIC INBOX SERVER'S OWN TOKEN, INTO THE VAULT. The server manages its
    # own account: its domains and their mail, its own updates (Workers, R2), its
    # sign-in (Access apps, the email PIN), the service tokens its agent keys and
    # relays sign in with. The list is TOKEN_PERMISSIONS in fabric-inbox's
    # cloudflare-api.ts; verified by listing the account's Workers, since the
    # server writes its own secrets with it.
    "fabric-inbox-server": {
        "name": "fabric-inbox server {slot} (managed)",
        "groups": ("Workers Scripts Write", "Workers R2 Storage Write",
                   "Access: Apps and Policies Write",
                   "Access: Organizations, Identity Providers, and Groups Write",
                   "Access: Service Tokens Write", "Account Settings Read",
                   "Email Routing Addresses Write", "Email Routing Account Rules Read",
                   "Email Sending Write"),
        "level": "account",
        "zone_groups": ("Zone Read", "Email Routing Rules Write", "Zone Settings Write", "DNS Write"),
        "why": "the Fabric Inbox Worker manages its own account's domains, mail, sign-in and updates",
        "scope": "account-vault",
        "probe": "/accounts/{account_id}/workers/scripts?per_page=1",
    },
    # READING ONE ACCOUNT'S WORKER LOGS, INTO THE VAULT. Finding what a deployed
    # Worker actually failed on needs its telemetry — Workers Observability Read
    # on one account, nothing that can change it. Verified by listing the
    # account's telemetry keys with the new value (that endpoint is a POST, so
    # the probe carries a body).
    "workers-observability-read": {
        "name": "observatory-workers-logs {slot} (managed)",
        "groups": ("Workers Observability Read",),
        "level": "account",
        "why": "reads one account's Worker logs (Workers Observability) for the project named by the vault slot",
        "scope": "account-vault",
        "probe": "/accounts/{account_id}/workers/observability/telemetry/keys",
        "probe_body": {},
    },
    # ONE MORE ACCOUNT FOR A FABRIC INBOX SERVER, INTO THE VAULT. A Fabric Inbox
    # server runs in one account and reads another account's domains with a token
    # made in THAT account (Worker secret CLOUDFLARE_API_TOKEN_<account id>): its
    # zones, Email Routing and sending, and Workers Scripts for the relay the server
    # installs there — no storage, no sign-in, which stay in the server's account.
    # The list is ACCOUNT_TOKEN_PERMISSIONS in fabric-inbox's cloudflare-api.ts.
    # Account-level groups on the account, zone-level ones on its zones: two
    # policies, since one policy may not mix levels on one resource.
    "fabric-inbox-account": {
        "name": "fabric-inbox account {slot} (managed)",
        "groups": ("Workers Scripts Write", "Account Settings Read",
                   "Email Routing Addresses Write", "Email Routing Account Rules Read",
                   "Email Sending Write"),
        "level": "account",
        "zone_groups": ("Zone Read", "Email Routing Rules Write", "Zone Settings Write", "DNS Write"),
        "why": "lets a Fabric Inbox server in another account list, route and send from this account's domains",
        "scope": "account-vault",
        "probe": "/accounts/{account_id}/workers/scripts?per_page=1",
    },
}

#: The setup right for `r2-bucket`: create the bucket, set its lifecycle. Never
#: delivered anywhere and never left behind — minted, used, deleted in one run.
#: Not a preset: `issue --preset` cannot hand it out.
R2_SETUP: dict = {
    "name": "observatory-r2-setup (ephemeral)",
    "groups": ("Workers R2 Storage Write",),
    "level": "account",
    "why": "creates one R2 bucket and sets its lifecycle; deleted before the command returns",
}
#: Jurisdictions this door will create a bucket in. `default` is Cloudflare's
#: own placement; `eu` guarantees the objects stay in the EU.
R2_JURISDICTIONS = ("default", "eu")
R2_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")

#: Presets whose token is a project's credential, delivered to a vault slot —
#: never filed in the folder the read-only analytics plugin loads.
VAULT_SCOPES = ("zone", "account-vault", "bucket-vault")


def token_dir() -> pathlib.Path:
    """Where issued analytics tokens live — asked of the plugin, not repeated."""
    import cloudflare_analytics
    return cloudflare_analytics.TOKEN_DIR


def _journal(event: str, secret: str, **detail) -> None:
    """The vault's movements journal: every issue, rotation and
    revocation this door performs is on the record beside the vault's own.
    Never raises — a journal failure must not undo a rotation that happened."""
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import vault
        vault.journal(event, secret, tool="cloudflare.py", **detail)
    except Exception as exc:                                                      
        print(f"  (movement not journaled: {type(exc).__name__})", file=sys.stderr)


def today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


# ─────────────────────────── the wire ───────────────────────────────────────

def _request(path: str, token: str, payload: dict | None = None,
             method: str | None = None, headers: dict | None = None) -> dict:
    """One call, with the error stripped to endpoint and status.

    Never the headers, never the body we sent: a signed credential in an
    exception message is a leak into every log and transcript that records it.
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{API}{path}", data=data, method=method or ("POST" if data else "GET"),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        e.close()
        raise RuntimeError(f"cloudflare answered HTTP {e.code}; provider response withheld") from None
    except OSError as e:
        raise RuntimeError(f"cloudflare unreachable: {type(e).__name__}") from None


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "unnamed"


# ─────────────────────────── the admin stash ────────────────────────────────

def admins() -> list[tuple[str, pathlib.Path]]:
    if any(p.is_symlink() for p in (ADMIN_STORE, *ADMIN_STORE.parents)):
        raise RuntimeError("Admin store must not contain symbolic links")
    if not ADMIN_STORE.is_dir():
        return []
    return [(p.name, p) for p in sorted(ADMIN_STORE.iterdir())
            if p.is_file() and not p.name.startswith(".")
            and not p.name.endswith(".meta.json")]


def read_admin(label: str | None) -> tuple[str, str]:
    """(label, value) for one stashed admin token. The value goes no further
    than the caller's local variable."""
    have = admins()
    if not have:
        raise RuntimeError("no admin token stashed — "
                           "run cloudflare stash with a protected file redirected to stdin")
    if label:
        for name, p in have:
            if name == label:
                return name, private_io.read(p).strip()
        raise RuntimeError(f"no stashed admin token called {label!r} "
                           f"(have: {', '.join(n for n, _ in have)})")
    if len(have) > 1:
        raise RuntimeError(f"{len(have)} admin tokens stashed — name one with "
                           f"--account: {', '.join(n for n, _ in have)}")
    name, p = have[0]
    return name, private_io.read(p).strip()


def write_secret(path: pathlib.Path, value: str) -> None:
    private_io.check(meta(path))
    private_io.write(path, value + "\n")


def discover_accounts(token: str) -> list[dict]:
    """Every account a token can act in, by two roads.

    `GET /accounts` lists accounts only where the token holds `Account
    Settings: Read` — a token built for issuing (API Tokens Edit, nothing else)
    answers it with an EMPTY list while being perfectly valid, so it would
    appear to see no account while managing tokens in several. The second road
    is the user's memberships, which that same token can read; the union of
    both is the answer.
    """
    out: dict[str, dict] = {}
    try:
        for a in _request("/accounts?per_page=50", token).get("result", []):
            out[a["id"]] = {"id": a["id"], "name": a.get("name") or a["id"]}
    except RuntimeError:
        pass                                                                                  
    try:
        for m in _request("/memberships?per_page=50", token).get("result", []):
            acc = m.get("account") or {}
            if acc.get("id") and m.get("status", "accepted") == "accepted":
                out.setdefault(acc["id"], {"id": acc["id"],
                                           "name": acc.get("name") or acc["id"]})
    except RuntimeError:
        pass
    return list(out.values())


def cmd_stash(value: str) -> int:
    if not value:
        print("nothing on stdin — the clipboard was empty, or the pipe was",
              file=sys.stderr)
        return 2
    if len(value.split()) > 1:
        print(f"refused: {len(value.split())} whitespace-separated pieces arrived, "
              f"not one token — copy just the value", file=sys.stderr)
        return 1
    try:
        _request("/user/tokens/verify", value)
    except RuntimeError as exc:
        # An account-owned token 401s here while working; only a NETWORK
        # failure is fatal at this point — the discovery below is the real test.
        if "unreachable" in str(exc):
            print(f"refused: {exc}", file=sys.stderr)
            return 1
    accts = discover_accounts(value)
    if not accts:
        print(f"refused: this token sees no account by either road — `/accounts` "
              f"(needs Account Settings: Read) and `/memberships` (needs "
              f"Memberships: Read) both came back empty.\n  (what arrived was "
              f"{len(value)} characters; a Cloudflare API token is 40)",
              file=sys.stderr)
        return 1
    # ONE TOKEN, SEVERAL ACCOUNTS. A user-scoped token with `All accounts:
    # API Tokens Edit` sees every account its user belongs to. Every account is
    # recorded, and each is probed for the one right that matters here: can it
    # manage tokens. An account the token can see but cannot issue into is kept
    # in the record as `can_issue: false`, so `issue` refuses it with a reason
    # instead of failing at mint time.
    for a in accts:
        try:
            _request(f"/accounts/{a['id']}/tokens?per_page=1", value)
            a["can_issue"] = True
        except RuntimeError:
            a["can_issue"] = False
    if not any(a["can_issue"] for a in accts):
        print(f"refused: this token sees {len(accts)} account(s) but can manage "
              f"API tokens in none of them.\n  It needs `API Tokens: Edit` on the "
              f"account(s) to issue the narrow tokens this program hands to "
              f"plugins.", file=sys.stderr)
        return 1
    if len(accts) == 1:
        label = slug(accts[0]["name"])
    else:
        # The stash is named after the USER when the token spans accounts —
        # `User Details: Read` gives the email; without it, the first account
        # with a `+N` marker so the name says it is not one account's token.
        email = ""
        try:
            email = (_request("/user", value).get("result") or {}).get("email", "")
        except RuntimeError:
            email = ""
        label = slug(email) if email else f"{slug(accts[0]['name'])}-plus-{len(accts) - 1}"
    dest = ADMIN_STORE / label
    existed = dest.is_file()
    write_secret(dest, value)
    write_meta(dest, json.dumps(
        {"kind": "admin", "accounts": accts,
         # the single-account fields stay for readers of the older record shape
         "account_id": accts[0]["id"], "account_name": accts[0]["name"],
         "stashed_on": today(), "why": "issues narrow tokens; never handed to a plugin"},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    can = [a for a in accts if a["can_issue"]]
    cannot = [a for a in accts if not a["can_issue"]]
    print(f"{'replaced' if existed else 'stashed'}: admin token as {label!r} — "
          f"can issue in {len(can)} account(s): "
          + ", ".join(f"{a['name']} [{slug(a['name'])}]" for a in can))
    if cannot:
        print(f"  sees but cannot issue in: "
              + ", ".join(a["name"] for a in cannot) + " — no API Tokens: Edit there")
    print("nothing reads it but this program. Issue a working token with:\n"
          + "\n".join(f"  python \"$(project-observatory full-path)/tools/cloudflare.py\" issue --preset analytics "
                       f"--account {slug(a['name'])}" for a in can[:3]))
    return 0


def meta(path: pathlib.Path) -> pathlib.Path:
    return path.with_name(path.name + ".meta.json")


def write_meta(path: pathlib.Path, text: str, encoding: str = "utf-8") -> None:
    private_io.write(meta(path), text)


def read_meta(path: pathlib.Path) -> dict:
    file = meta(path)
    private_io.check(file)
    if not file.exists():
        return {}
    try:
        doc = json.loads(private_io.read(file))
    except (OSError, ValueError):
        raise RuntimeError("Credential metadata is unreadable; refusing to replace it") from None
    if not isinstance(doc, dict):
        raise RuntimeError("Credential metadata must be an object")
    return doc


def _group_level(g: dict) -> str:
    scopes = json.dumps(g.get("scopes", ""))
    if "r2.bucket" in scopes:
        # R2's object groups are granted on a BUCKET resource, and a policy may
        # not put them on the account — a third level, not a kind of account.
        return "bucket"
    return "zone" if "zone" in scopes else "account"


def group_ids(admin: str, account_id: str, wanted: tuple[str, ...],
              level: str = "zone") -> list[str]:
    """Ids for the named permission groups in THIS account, at ONE level.

    By name, never by id: Cloudflare's group ids differ per account, and a
    hardcoded one silently grants the wrong right on the second account. By
    level too: the catalogue carries same-named groups for zones and for the
    account, and a policy may not mix levels on one resource.
    """
    groups = _request(f"/accounts/{account_id}/tokens/permission_groups?per_page=500",
                      admin).get("result", [])
    found: dict[str, str] = {}
    for g in groups:
        if g.get("name") in wanted and _group_level(g) == level:
            found.setdefault(g["name"], g["id"])
    missing = [n for n in wanted if n not in found]
    if missing:
        raise RuntimeError(f"this account offers no permission group(s) {missing}")
    return [found[n] for n in wanted]


def existing_token(admin: str, account_id: str, name: str) -> str | None:
    for row in _request(f"/accounts/{account_id}/tokens?per_page=50",
                        admin).get("result", []):
        if row.get("name") == name:
            return row["id"]
    return None


def mint(admin: str, account_id: str, preset: dict,
         resources: dict | None = None, name: str | None = None) -> tuple[str, str]:
    """(token id, value) — rolling an existing one rather than adding a twin.

    `resources` narrows the grant (a zone preset passes its one zone); `name`
    is the token's name when the preset's own is a template."""
    ids = group_ids(admin, account_id, preset["groups"], preset.get("level", "zone"))
    name = name or preset["name"]
    resources = resources or {f"com.cloudflare.api.account.{account_id}": "*"}
    policies = [{"effect": "allow", "resources": resources,
                 "permission_groups": [{"id": i} for i in ids]}]
    if preset.get("zone_groups"):
        # Zone-level groups on every zone of the SAME account, as their own policy.
        zone_ids = group_ids(admin, account_id, preset["zone_groups"], "zone")
        policies.append({"effect": "allow",
                         "resources": {f"com.cloudflare.api.account.{account_id}":
                                       {"com.cloudflare.api.account.zone.*": "*"}},
                         "permission_groups": [{"id": i} for i in zone_ids]})
    tid = existing_token(admin, account_id, name)
    if tid:
        # GRANTS FOLLOW THE PRESET. Rolling reissues the value and keeps the
        # policies as they were — so a preset that grew a permission would roll
        # tokens that never gain it. The policy is rewritten first, then rolled;
        # the token's id, name and every reader stay put.
        _request(f"/accounts/{account_id}/tokens/{tid}", admin,
                 {"name": name, "status": "active", "policies": policies},
                 method="PUT")
        # PUT, not POST: the account-owned roll endpoint refuses POST with
        # "Method POST not available for that URI" — found the first time a
        # roll ran for real (2026-09-14); every earlier issue was a create.
        d = _request(f"/accounts/{account_id}/tokens/{tid}/value", admin, {}, method="PUT")
        value = d.get("result")
        if not isinstance(value, str) or not value:
            raise RuntimeError("cloudflare rolled the token but returned no value")
        return tid, value
    d = _request(f"/accounts/{account_id}/tokens", admin,
                 {"name": name, "policies": policies})
    res = d.get("result") or {}
    if not res.get("value"):
        raise RuntimeError("cloudflare created the token but returned no value"
                           + discard_fresh(admin, account_id, res.get("id"), False))
    return res.get("id", ""), res["value"]


def discard_fresh(admin: str, account_id: str, tid: str | None, rolled: bool) -> str:
    """What happened to a token minted by a call that then failed (issue #95).

    A token this call CREATED has no holder once the call refuses, so it is deleted at
    Cloudflare before the refusal is printed. A ROLLED token keeps its id and readers and its
    old value is already dead, so it stays; the slot needs a fresh issue either way. Returns
    the clause the refusal line carries, never the value."""
    if not tid:
        return ""
    if rolled:
        return ("; the existing token was rolled, so the value the slot held is dead — "
                "issue again to deliver a working one")
    try:
        _request(f"/accounts/{account_id}/tokens/{tid}", admin, method="DELETE")
    except RuntimeError as exc:
        return (f"; the token this call created could NOT be deleted ({exc}) — "
                f"delete it in the dashboard now")
    return "; the token this call created was deleted again"


def can_read_analytics(token: str, zone_ids: list[str]) -> str:
    """An empty string if the token can read what the plugin reads, else why not.

    LISTING ZONES IS NOT READING ANALYTICS. `Zone:Read` alone lists every zone
    and then the GraphQL dataset answers `zones [...] are not authorized` — a
    token that installs cleanly and produces a source that is silently always
    empty, which is the exact outcome this program exists to refuse.
    """
    import cloudflare_analytics as cf
    day, _ = cf.day_bounds()
    try:
        cf.fetch_day(token, zone_ids[:10], day)
    except RuntimeError as exc:
        return str(exc)
    return ""


def zones_of(token: str) -> list[dict]:
    out, page = [], 1
    while True:
        d = _request(f"/zones?per_page=50&page={page}", token)
        out += [{"id": z["id"], "name": z["name"],
                 "account": (z.get("account") or {}).get("name", "")}
                for z in d.get("result", [])]
        info = d.get("result_info") or {}
        if page >= (info.get("total_pages") or 1):
            return out
        page += 1


def stash_accounts(stash_label: str) -> list[dict]:
    """Every account a stash can issue into — the new record shape or the old."""
    m = read_meta(ADMIN_STORE / stash_label)
    if m.get("accounts"):
        return [a for a in m["accounts"] if a.get("can_issue", True)]
    if m.get("account_id"):
        return [{"id": m["account_id"], "name": m.get("account_name") or stash_label,
                 "can_issue": True}]
    return []


def find_account(wanted: str | None) -> tuple[str, str, dict]:
    """(stash label, admin value, account) for the account to issue into.

    `wanted` is the account's own slug (what `list` prints in brackets), its id,
    or — for a stash that holds exactly one account — the stash label. With
    several accounts reachable and none named, the refusal lists them: issuing
    into "whichever came first" is how a token lands in the wrong account.
    """
    catalog: list[tuple[str, dict]] = []
    for label, _p in admins():
        for a in stash_accounts(label):
            catalog.append((label, a))
    if not catalog:
        raise RuntimeError("no admin token stashed that can issue — "
                           "run cloudflare stash with a protected file redirected to stdin")
    if wanted:
        w = wanted.strip()
        hits = [(l, a) for l, a in catalog
                if slug(a["name"]) == slug(w) or a["id"] == w
                or (l == w and len(stash_accounts(l)) == 1)]
        if not hits:
            raise RuntimeError(f"no reachable account matches {wanted!r} — have: "
                               + ", ".join(f"{slug(a['name'])}" for _, a in catalog))
        label, account = hits[0]
    elif len(catalog) == 1:
        label, account = catalog[0]
    else:
        raise RuntimeError(f"{len(catalog)} accounts reachable — name one with "
                           f"--account: " + ", ".join(slug(a["name"]) for _, a in catalog))
    _l, admin = read_admin(label)
    return label, admin, account


def install_issued(value: str, label: str, account: dict, preset_key: str,
                   project: str | None, rolled: bool, stash: str | None = None) -> int:
    """Verify the issued token can do the job, then file it with its record."""
    zs = zones_of(value)
    if not zs:
        print(f"  {label}: issued, but it sees no zone — not installed",
              file=sys.stderr)
        return 1
    why = can_read_analytics(value, [z["id"] for z in zs])
    if why:
        print(f"  {label}: issued, but it cannot read analytics — {why}\n"
              f"    the preset's permission groups did not grant what the plugin "
              f"queries; nothing was installed", file=sys.stderr)
        return 1
    dest = token_dir() / label
    prior = read_meta(dest)
    write_secret(dest, value)
    write_meta(dest, json.dumps({
        "kind": "issued", "preset": preset_key,
        "account_id": account["id"], "account_name": account["name"],
        # which stash minted it — rotation reads the admin from here, because
        # a multi-account stash is not named after any one account
        "stash": stash or prior.get("stash"),
        "project": project or prior.get("project"),
        "permission_groups": list(PRESETS[preset_key]["groups"]),
        "why": PRESETS[preset_key]["why"],
        "zones": [z["name"] for z in zs],
        "issued_on": prior.get("issued_on") or today(),
        "rotated_on": today() if rolled else prior.get("rotated_on"),
        "rotations": (prior.get("rotations") or 0) + (1 if rolled else 0),
        "issued_by": "tools/cloudflare.py",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _journal("rotate" if rolled else "issue", f"cloudflare/{account['name']}/{label}",
             preset=preset_key, zones=len(zs))
    print(f"  {label}: {'rolled' if rolled else 'issued'} and installed "
          f"({len(zs)} zone(s): {', '.join(sorted(z['name'] for z in zs)[:5])}"
          f"{' …' if len(zs) > 5 else ''})"
          + (f", for {project}" if project else ""))
    return 0


def cmd_issue(preset_key: str, account_label: str | None, project: str | None) -> int:
    preset = PRESETS.get(preset_key)
    if not preset:
        print(f"unknown preset {preset_key!r} — have: {', '.join(PRESETS)}",
              file=sys.stderr)
        return 2
    if preset.get("scope") in VAULT_SCOPES:
        # A writer filed where the analytics plugin reads would be a write
        # credential in a folder a read-only plugin loads.
        print(f"refused: preset {preset_key!r} is a writer and goes to a vault slot — "
              f"use --vault" + (" and --zone" if preset["scope"] == "zone" else ""),
              file=sys.stderr)
        return 2
    if project and not project.startswith("project:"):
        project = f"project:{project}"
    try:
        stash_label, admin, account = find_account(account_label)
        destination = token_dir() / slug(account["name"])
        private_io.check(destination)
        read_meta(destination)
        rolled = existing_token(admin, account["id"], preset["name"]) is not None
        _tid, value = mint(admin, account["id"], preset)
    except RuntimeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    # The issued file is named after the ACCOUNT, not the stash: three accounts
    # behind one stash must land as three files the plugin can tell apart.
    return install_issued(value, slug(account["name"]), account, preset_key,
                          project, rolled, stash=stash_label)


def zone_id_of(admin: str, account_id: str, zone: str) -> str:
    """The zone's id inside THIS account — a same-named zone elsewhere is not it."""
    rows = _request(f"/zones?name={urllib.parse.quote(zone)}&account.id={account_id}",
                    admin).get("result", [])
    hit = [z for z in rows if z.get("name") == zone]
    if not hit:
        raise RuntimeError(f"account {account_id} holds no zone named {zone!r}")
    return hit[0]["id"]


def parse_vault_target(target: str | None) -> tuple[str, str, str]:
    """PROJECT/ENV/NAME, validated by the vault's own rules before anything is
    minted: a token issued for a slot the vault then refuses is a live
    credential with nowhere to go."""
    parts = (target or "").split("/")
    if len(parts) != 3 or not all(parts):
        raise ValueError("--vault must be PROJECT/ENV/NAME")
    sys.path.insert(0, str(ROOT / "tools"))
    import vault
    try:
        # The project is normalised to its vault FOLDER here, before anything
        # is minted, by the vault's own rule: a registry id names the same
        # project as its folder, and a name two projects claim is refused now
        # rather than after a live token exists.
        folder, said = vault.project_folder(parts[0], parts[1], parts[2])
        if said:
            print(f"project: {said}", file=sys.stderr)
        vault.validate_names(folder, parts[1], parts[2])
    except vault.VaultBoundaryError as exc:
        raise ValueError(f"--vault {exc}") from None
    return folder, parts[1], parts[2]


def deliver_to_vault(value: str, project: str, env: str, name: str) -> None:
    """Through the vault's own door, value on stdin — never argv, never a print.

    `--force` because a re-issue ROLLS the token: the slot's old value is
    already dead at Cloudflare, so there is no history worth keeping. The
    child's output is withheld; a parse error can echo its input."""
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "vault.py"), "put",
                        project, env, name, "--force"],
                       input=value + "\n", capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"vault refused the slot {project}/{env}/{name} "
                           f"(exit {r.returncode}); child output withheld")


def cmd_issue_zone(preset_key: str, zone: str | None, target: str | None,
                   account_label: str | None, wait: float = 2.0) -> int:
    """Issue a zone-scoped token (DNS edit, Email Routing rules) into a vault slot."""
    preset = PRESETS[preset_key]
    if preset.get("scope") != "zone":
        print(f"refused: preset {preset_key!r} is not zone-scoped", file=sys.stderr)
        return 2
    if not zone or not target:
        print(f"refused: preset {preset_key!r} needs --zone and --vault PROJECT/ENV/NAME",
              file=sys.stderr)
        return 2
    try:
        project, env, name = parse_vault_target(target)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    # Known before the first call, so a refusal can say what became of a minted token.
    admin, account, tid, rolled = None, None, None, False
    try:
        _stash, admin, account = find_account(account_label)
        zid = zone_id_of(admin, account["id"], zone)
        token_name = preset["name"].format(zone=zone, slot=f"{project}/{env}/{name}")
        rolled = existing_token(admin, account["id"], token_name) is not None
        tid, value = mint(admin, account["id"], preset,
                          resources={f"com.cloudflare.api.account.zone.{zid}": "*"},
                          name=token_name)
        # A fresh token can take a moment to be honoured at the edge; the
        # value is delivered only once it has proved it can read the zone.
        probe = preset.get("probe", "").format(zone_id=zid)
        for attempt in range(5):
            try:
                if preset.get("verify") == "analytics":
                    why = can_read_analytics(value, [zid])
                    if why:
                        raise RuntimeError(why)
                else:
                    _request(probe, value)
                break
            except RuntimeError:
                if attempt == 4:
                    what = "the zone's analytics" if preset.get("verify") == "analytics" else \
                        probe.split('?')[0].replace(zid, '<zone>')
                    raise RuntimeError(f"the issued token cannot do its read on {zone} ({what})") from None
                time.sleep(wait)
        deliver_to_vault(value, project, env, name)
    except RuntimeError as exc:
        print(f"refused: {exc}{discard_fresh(admin, (account or {}).get('id', ''), tid, rolled)}",
              file=sys.stderr)
        return 1
    _journal("rotate" if rolled else "issue", f"{project}/{env}/{name}",
             preset=preset_key, zone=zone, account=account["name"])
    print(f"  {project}/{env}/{name}: {'rolled' if rolled else 'issued'} — "
          f"{', '.join(preset['groups'])} on {zone} only; "
          f"use: python \"$(project-observatory full-path)/tools/use_secret.py\" run --env {env} {project} {name} -- <command>")
    return 0


def cmd_issue_account(preset_key: str, target: str | None, account_label: str | None,
                      wait: float = 2.0) -> int:
    """Issue an account-scoped writer (D1, Email Sending, Workers, a Fabric Inbox
    account) into a vault slot."""
    preset = PRESETS[preset_key]
    if preset.get("scope") != "account-vault":
        print(f"refused: preset {preset_key!r} is not an account-to-vault preset", file=sys.stderr)
        return 2
    if not target:
        print(f"refused: preset {preset_key!r} needs --vault PROJECT/ENV/NAME", file=sys.stderr)
        return 2
    try:
        project, env, name = parse_vault_target(target)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    # Known before the first call, so a refusal can say what became of a minted token.
    admin, account, tid, rolled = None, None, None, False
    try:
        _stash, admin, account = find_account(account_label)
        token_name = preset["name"].format(slot=f"{project}/{env}/{name}")
        rolled = existing_token(admin, account["id"], token_name) is not None
        tid, value = mint(admin, account["id"], preset, name=token_name)
        # The value reaches the vault only once it has proved, with its own
        # rights, the one thing its reader needs first.
        probe = preset["probe"].format(account_id=account["id"])
        for attempt in range(5):
            try:
                # A probe that is a POST (telemetry) carries its body; the rest are GETs.
                _request(probe, value, preset.get("probe_body"))
                break
            except RuntimeError:
                if attempt == 4:
                    raise RuntimeError("the issued token cannot do its read on this account "
                                       f"({probe.split('?')[0].replace(account['id'], '<account>')})") from None
                time.sleep(wait)
        deliver_to_vault(value, project, env, name)
    except RuntimeError as exc:
        print(f"refused: {exc}{discard_fresh(admin, (account or {}).get('id', ''), tid, rolled)}",
              file=sys.stderr)
        return 1
    _journal("rotate" if rolled else "issue", f"{project}/{env}/{name}",
             preset=preset_key, account=account["name"])
    # An account-owned token cannot call /memberships, which is how wrangler
    # finds an account a config does not name — so the id travels with the use.
    print(f"  {project}/{env}/{name}: {'rolled' if rolled else 'issued'} — "
          f"{', '.join(preset['groups'])} on account {account['name']} only"
          # A zone half is a real grant on every zone of that account; a line
          # that stopped at "account only" would hide DNS Write from its reader.
          + (f", and {', '.join(preset['zone_groups'])} on its zones"
             if preset.get("zone_groups") else "") + "; "
          f"use: CLOUDFLARE_ACCOUNT_ID={account['id']} "
          f"python \"$(project-observatory full-path)/tools/use_secret.py\" run --env {env} {project} {name} -- <command>")
    return 0



# ─────────────────────────── R2: one bucket, S3 keys ─────────────────────────

def r2_endpoint(account_id: str, jurisdiction: str) -> str:
    """The S3 endpoint a bucket in this jurisdiction answers on."""
    region = "" if jurisdiction == "default" else f".{jurisdiction}"
    return f"https://{account_id}{region}.r2.cloudflarestorage.com"


def r2_s3_keys(token_id: str, value: str) -> tuple[str, str]:
    """(access key id, secret access key) — R2's documented derivation."""
    return token_id, hashlib.sha256(value.encode("utf-8")).hexdigest()


def sigv4_headers(method: str, url: str, access_key: str, secret: str, body: bytes,
                  amz_date: str, region: str = "auto", service: str = "s3",
                  extra: dict | None = None) -> dict:
    """AWS Signature Version 4 for one request, header form, stdlib only.

    Only what the door itself needs — a probe put, get, delete and a bucket
    list — so no query-string canonicalisation beyond what those use. Tested
    against AWS's published GET-object example."""
    parts = urllib.parse.urlsplit(url)
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {"host": parts.netloc, "x-amz-content-sha256": payload_hash,
               "x-amz-date": amz_date}
    for k, v in (extra or {}).items():
        headers[k.lower()] = v.strip()
    signed = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    query = "&".join(sorted(parts.query.split("&"))) if parts.query else ""
    canonical = "\n".join([method, urllib.parse.quote(parts.path or "/", safe="/~"),
                            query, canonical_headers, signed, payload_hash])
    day = amz_date[:8]
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                          hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    key = f"AWS4{secret}".encode("utf-8")
    for piece in (day, region, service, "aws4_request"):
        key = hmac.new(key, piece.encode("utf-8"), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    out = {k: v for k, v in headers.items() if k != "host"}
    out["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                            f"SignedHeaders={signed}, Signature={signature}")
    return out


def _s3(method: str, url: str, access_key: str, secret: str,
        body: bytes = b"") -> tuple[int, bytes]:
    """(status, body) of one signed S3 call. An HTTP error is a status, not an
    exception — the door's checks are about WHICH status came back — and no
    header or credential ever reaches a message."""
    amz_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    hdrs = sigv4_headers(method, url, access_key, secret, body, amz_date)
    req = urllib.request.Request(url, data=body if method == "PUT" else None,
                                 method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        status = e.code
        e.close()
        return status, b""
    except OSError as e:
        raise RuntimeError(f"r2 unreachable: {type(e).__name__}") from None


def r2_prove(endpoint: str, bucket: str, access_key: str, secret: str) -> str:
    """An empty string if the pair can put, read back and delete an object in
    THIS bucket and cannot list the account's buckets; else why not."""
    key = f".observatory-probe/{datetime.datetime.now(datetime.timezone.utc):%Y%m%dT%H%M%S%f}"
    url = f"{endpoint}/{bucket}/{key}"
    body = os.urandom(16).hex().encode("ascii")
    status, _ = _s3("PUT", url, access_key, secret, body)
    if status != 200:
        return f"a probe put answered HTTP {status}"
    status, got = _s3("GET", url, access_key, secret)
    if status != 200 or got != body:
        return f"the probe read back as HTTP {status}" + ("" if status != 200 else ", different bytes")
    status, _ = _s3("DELETE", url, access_key, secret)
    if status not in (200, 204):
        return f"the probe delete answered HTTP {status}"
    status, _ = _s3("GET", f"{endpoint}/", access_key, secret)
    if status == 200:
        return "the pair can LIST the account's buckets — broader than one bucket"
    return ""


#: Lifecycle bounds. Days: at least one, at most ten years — beyond that a rule
#: is a typo, not a retention policy. A prefix is an object key's beginning, and
#: an R2 key is at most 1024 bytes.
R2_DAYS_MAX = 3650
R2_PREFIX_MAX_BYTES = 1024
#: The whole-bucket expiry `issue` writes when it is given no rule at all —
#: what it has always written. `lifecycle` has no default: it REPLACES a live
#: bucket's rules, and a guessed 30 days could shorten a 92-day retention.
R2_ISSUE_DEFAULT_DAYS = 30


def _shown(prefix: str) -> str:
    """A prefix as a refusal names it: quoted, and cut short when long."""
    return repr(prefix if len(prefix) <= 40 else prefix[:40] + "…")


def r2_lifecycle_plan(expire_days: int | None, rule_texts: list[str],
                      default: int | None = R2_ISSUE_DEFAULT_DAYS) -> list[tuple[str, int]]:
    """The lifecycle asked for, as (prefix, days) — the whole bucket (prefix "")
    first, then each `--lifecycle-rule PREFIX:DAYS` sorted by prefix, so the
    same flags in any order write the same body. ValueError names what is wrong
    before anything reaches Cloudflare.

    A PREFIX RULE THAT CAN NEVER FIRE IS REFUSED. R2 applies the shortest age
    that matches an object, so a rule for `staging/` at 120 days under a
    whole-bucket rule at 92 deletes nothing the bucket rule has not already
    deleted — almost certainly a mistake in the number, never a policy. The same
    holds under any shorter prefix that covers it (`a/` at 5, `a/b/` at 9)."""
    if expire_days is None and not rule_texts:
        if default is None:
            raise ValueError("give --expire-days, --lifecycle-rule PREFIX:DAYS, or both")
        expire_days = default
    plan: list[tuple[str, int]] = []
    if expire_days is not None:
        if not 1 <= expire_days <= R2_DAYS_MAX:
            raise ValueError(f"--expire-days is between 1 and {R2_DAYS_MAX}")
        plan.append(("", expire_days))
    rules: dict[str, int] = {}
    for text in rule_texts:
        prefix, sep, days_text = text.rpartition(":")
        if not sep or not re.fullmatch(r"[0-9]+", days_text):
            raise ValueError(f"--lifecycle-rule {_shown(text)} is not PREFIX:DAYS "
                             f"(for example staging/:2)")
        days = int(days_text)
        if not 1 <= days <= R2_DAYS_MAX:
            raise ValueError(f"--lifecycle-rule {_shown(text)}: days are between 1 and {R2_DAYS_MAX}")
        if not prefix:
            raise ValueError("--lifecycle-rule needs a prefix; the whole bucket is --expire-days")
        if prefix.startswith("/"):
            raise ValueError(f"--lifecycle-rule prefix {_shown(prefix)} has a leading '/' — "
                             f"R2 keys do not start with one, so it would match nothing")
        if not prefix.isprintable():
            raise ValueError(f"--lifecycle-rule prefix {_shown(prefix)} is not printable")
        if len(prefix.encode("utf-8")) > R2_PREFIX_MAX_BYTES:
            raise ValueError(f"--lifecycle-rule prefix {_shown(prefix)} is over "
                             f"{R2_PREFIX_MAX_BYTES} bytes, longer than any R2 key")
        if prefix in rules:
            raise ValueError(f"--lifecycle-rule prefix {_shown(prefix)} is given more than once")
        rules[prefix] = days
    plan += sorted(rules.items())
    for prefix, days in plan:
        for cover, cover_days in plan:
            if prefix and cover != prefix and prefix.startswith(cover) and days > cover_days:
                by = ("the whole-bucket rule" if not cover
                      else f"the rule for {_shown(cover)}")
                raise ValueError(f"--lifecycle-rule {prefix}:{days} would never fire — {by} "
                                 f"deletes those objects after {cover_days} days first; "
                                 f"shorten it or drop it")
    return plan


def r2_rule_id(prefix: str, days: int) -> str:
    """A rule's id: readable in the dashboard, unique per prefix, bounded.

    The whole-bucket rule keeps the id it always had, so a bucket made before
    prefix rules existed reads back unchanged. A prefix rule carries its slug
    for the eye and 8 hex of its sha256 for uniqueness: `a/` and `a-` slug
    alike."""
    if not prefix:
        return f"expire-after-{days}-days"
    tag = hashlib.sha256(prefix.encode("utf-8")).hexdigest()[:8]
    return f"expire-{slug(prefix)[:64]}-{tag}-after-{days}-days"


def r2_lifecycle(plan: list[tuple[str, int]]) -> dict:
    """The lifecycle body: per (prefix, days), objects under the prefix expire
    after the days and that prefix's unfinished multipart uploads abort after
    one — an upload abandoned half-way is billed storage nobody can read."""
    return {"rules": [{
        "id": r2_rule_id(prefix, days),
        "enabled": True,
        "conditions": {"prefix": prefix},
        "deleteObjectsTransition": {"condition": {"type": "Age", "maxAge": days * 86400}},
        "abortMultipartUploadsTransition": {"condition": {"type": "Age", "maxAge": 86400}},
    } for prefix, days in plan]}


def r2_lifecycle_summary(plan: list[tuple[str, int]]) -> str:
    """The plan in one line, for the person who ran the command."""
    whole = [d for p, d in plan if not p]
    parts = [f"objects expire after {whole[0]} days" if whole else "no whole-bucket expiry"]
    parts += [f"under {_shown(p)} after {d} days" for p, d in plan if p]
    return "; ".join(parts) + "; unfinished multipart uploads abort after 1 day"


def _rule_shape(rule: dict) -> tuple:
    def cond(key: str) -> tuple:
        c = (rule.get(key) or {}).get("condition") or {}
        return (c.get("type"), c.get("maxAge"))
    return (rule.get("enabled") is True, cond("deleteObjectsTransition"),
            cond("abortMultipartUploadsTransition"))


def _describe_shape(shape: tuple) -> str:
    enabled, (_t, delete_age), (_a, abort_age) = shape
    def days(age):
        return f"{age // 86400} days" if isinstance(age, int) else "never"
    return (f"{'enabled' if enabled else 'disabled'}, delete after {days(delete_age)}, "
            f"abort uploads after {days(abort_age)}")


def r2_lifecycle_mismatch(written: dict, read: list) -> str:
    """Empty if what read back is EXACTLY the rules written, compared rule by
    rule and keyed by prefix; else every difference, named by prefix. One rule
    matching is not the lifecycle that was asked for — a dropped prefix rule or
    a stray one left by someone else changes what gets deleted."""
    def label(prefix: str) -> str:
        return "the whole-bucket rule" if not prefix else f"the rule for {_shown(prefix)}"
    want = {(r.get("conditions") or {}).get("prefix") or "": _rule_shape(r)
            for r in written["rules"]}
    got: dict[str, tuple] = {}
    problems: list[str] = []
    for r in read:
        prefix = (r.get("conditions") or {}).get("prefix") or ""
        if prefix in got:
            problems.append(f"{label(prefix)} read back twice")
        got[prefix] = _rule_shape(r)
    for prefix, shape in want.items():
        if prefix not in got:
            problems.append(f"{label(prefix)} is missing")
        elif got[prefix] != shape:
            problems.append(f"{label(prefix)} read back {_describe_shape(got[prefix])}, "
                            f"not {_describe_shape(shape)}")
    problems += [f"{label(p)} was not asked for" for p in got if p not in want]
    return "; ".join(problems)


def r2_setup_bucket(admin: str, account_id: str, bucket: str, jurisdiction: str,
                    plan: list[tuple[str, int]], wait: float = 2.0,
                    create: bool = True) -> bool:
    """Create the bucket if it is missing (only when `create`) and replace its
    lifecycle with exactly `plan`, with a setup token that is deleted before
    this returns. True if it created one.

    The admin token can mint tokens and nothing else; R2's own API needs
    Storage Write, which is too broad to keep anywhere — so it lives for the
    length of this function. A delete that fails is an error, not a warning:
    a live account-wide storage writer nobody knows about is the thing this
    door exists to prevent. The PUT replaces the whole lifecycle, so a re-run
    with the same plan writes the same rules and nothing else survives."""
    tid, setup = mint(admin, account_id, R2_SETUP)
    created = False
    try:
        hdr = {"cf-r2-jurisdiction": jurisdiction}
        base = f"/accounts/{account_id}/r2/buckets"
        for attempt in range(5):
            try:
                _request(f"{base}?per_page=1", setup, headers=hdr)
                break
            except RuntimeError:
                if attempt == 4:
                    raise RuntimeError("the setup token was never honoured for R2 — "
                                       "is R2 enabled on this account?") from None
                time.sleep(wait)
        try:
            _request(f"{base}/{bucket}", setup, headers=hdr)
        except RuntimeError as exc:
            if "HTTP 404" not in str(exc):
                raise
            if not create:
                raise RuntimeError(f"there is no bucket {bucket!r} in jurisdiction {jurisdiction} — "
                                   f"`cloudflare.py issue --preset r2-bucket` creates one with its "
                                   f"key; check --jurisdiction") from None
            _request(base, setup, {"name": bucket}, headers=hdr)
            created = True
        body = r2_lifecycle(plan)
        _request(f"{base}/{bucket}/lifecycle", setup, body, method="PUT", headers=hdr)
        rules = (_request(f"{base}/{bucket}/lifecycle", setup, headers=hdr)
                 .get("result") or {}).get("rules") or []
        wrong = r2_lifecycle_mismatch(body, rules)
        if wrong:
            raise RuntimeError(f"the lifecycle did not read back as written — {wrong}")
    finally:
        try:
            _request(f"/accounts/{account_id}/tokens/{tid}", admin, method="DELETE")
        except RuntimeError as exc:
            raise RuntimeError(f"the R2 setup token could not be deleted ({exc}) — "
                               f"delete {R2_SETUP['name']!r} in the dashboard now") from None
    return created


def _r2_bucket_refusal(bucket: str | None, jurisdiction: str) -> str:
    """Why a bucket and jurisdiction cannot be used, or an empty string."""
    if not R2_BUCKET_RE.match(bucket or ""):
        return ("an R2 bucket name is 3–63 characters of a–z, 0–9 and '-', "
                "starting and ending with a letter or digit")
    if jurisdiction not in R2_JURISDICTIONS:
        return f"--jurisdiction is one of {', '.join(R2_JURISDICTIONS)}"
    return ""


def cmd_issue_bucket(preset_key: str, bucket: str | None, jurisdiction: str,
                     expire_days: int | None, target: str | None, account_label: str | None,
                     wait: float = 2.0, lifecycle_rules: list[str] | tuple = ()) -> int:
    """Issue a one-bucket R2 key pair into three vault slots.

    `--vault PROJECT/ENV/PREFIX` names the slots PREFIX_ACCESS_KEY_ID,
    PREFIX_SECRET_ACCESS_KEY and PREFIX_ENDPOINT — the three things an S3
    client needs, delivered together or not at all. The bucket's lifecycle is
    `--expire-days` over the whole bucket plus one rule per `--lifecycle-rule`;
    with neither given, the whole bucket expires after 30 days, as it always did."""
    preset = PRESETS[preset_key]
    if preset.get("scope") != "bucket-vault":
        print(f"refused: preset {preset_key!r} is not a bucket preset", file=sys.stderr)
        return 2
    if not bucket or not target:
        print(f"refused: preset {preset_key!r} needs --bucket and --vault PROJECT/ENV/PREFIX",
              file=sys.stderr)
        return 2
    why = _r2_bucket_refusal(bucket, jurisdiction)
    if why:
        print(f"refused: {why}", file=sys.stderr)
        return 2
    try:
        plan = r2_lifecycle_plan(expire_days, list(lifecycle_rules))
        project, env, prefix = parse_vault_target(target)
        slots = [f"{prefix}_{s}" for s in ("ACCESS_KEY_ID", "SECRET_ACCESS_KEY", "ENDPOINT")]
        for s in slots:
            parse_vault_target(f"{project}/{env}/{s}")
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    # Known before the first call, so a refusal can say what became of a minted token.
    admin, account, tid, rolled = None, None, None, False
    try:
        _stash, admin, account = find_account(account_label)
        created = r2_setup_bucket(admin, account["id"], bucket, jurisdiction, plan, wait)
        token_name = preset["name"].format(jurisdiction=jurisdiction, bucket=bucket)
        rolled = existing_token(admin, account["id"], token_name) is not None
        resource = f"com.cloudflare.edge.r2.bucket.{account['id']}_{jurisdiction}_{bucket}"
        tid, value = mint(admin, account["id"], preset, resources={resource: "*"},
                          name=token_name)
        access_key, secret = r2_s3_keys(tid, value)
        endpoint = r2_endpoint(account["id"], jurisdiction)
        why = ""
        for attempt in range(5):
            why = r2_prove(endpoint, bucket, access_key, secret)
            if not why or "broader" in why:
                break
            time.sleep(wait)
        if why:
            raise RuntimeError(f"the issued pair failed its proof — {why}; nothing delivered")
        for slot, v in zip(slots, (access_key, secret, endpoint)):
            deliver_to_vault(v, project, env, slot)
    except RuntimeError as exc:
        print(f"refused: {exc}{discard_fresh(admin, (account or {}).get('id', ''), tid, rolled)}",
              file=sys.stderr)
        return 1
    _journal("rotate" if rolled else "issue", f"{project}/{env}/{prefix}_*",
             preset=preset_key, bucket=bucket, jurisdiction=jurisdiction,
             expire_days=next((d for p, d in plan if not p), None),
             rules=[[p, d] for p, d in plan], account=account["name"])
    print(f"  bucket {bucket} ({jurisdiction}): {'created' if created else 'already there'}; "
          f"lifecycle: {r2_lifecycle_summary(plan)}")
    print(f"  {project}/{env}/{prefix}_{{ACCESS_KEY_ID,SECRET_ACCESS_KEY,ENDPOINT}}: "
          f"{'rolled' if rolled else 'issued'} — {', '.join(preset['groups'])} on {bucket} only, "
          f"proved by a put, a get and a delete, and refused a bucket list; "
          f"use: python \"$(project-observatory full-path)/tools/use_secret.py\" run --env {env} {project} "
          f"{','.join(slots)} -- <command>")
    return 0


def cmd_lifecycle(bucket: str | None, jurisdiction: str, expire_days: int | None,
                  rule_texts: list[str], account_label: str | None, wait: float = 2.0) -> int:
    """Replace an EXISTING bucket's lifecycle, and touch nothing else.

    A COMMAND OF ITS OWN, NOT A FLAG ON `issue`. Changing how long objects
    live is a different act from handing out a key: it needs no vault slot, no
    proof, no delivery, and it must never roll the key the bucket's writer is
    using right now — a roll kills the value its slot holds. As a flag on
    `issue`, `--vault` and `--preset` would be accepted and ignored, and one
    forgotten flag would turn a retention change into a key rotation. Here the
    command cannot reach the bucket key at all: it mints only the ephemeral
    setup token, which `r2_setup_bucket` deletes before returning, and it never
    creates a bucket — a bucket without its key is `issue`'s job."""
    why = _r2_bucket_refusal(bucket, jurisdiction)
    if why:
        print(f"refused: {why}", file=sys.stderr)
        return 2
    if expire_days is None and not rule_texts:
        print("refused: give --expire-days, --lifecycle-rule PREFIX:DAYS, or both — "
              "lifecycle replaces the bucket's whole lifecycle, so it never guesses one",
              file=sys.stderr)
        return 2
    try:
        plan = r2_lifecycle_plan(expire_days, list(rule_texts), default=None)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    try:
        _stash, admin, account = find_account(account_label)
        r2_setup_bucket(admin, account["id"], bucket, jurisdiction, plan, wait, create=False)
    except RuntimeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    _journal("lifecycle", f"r2/{jurisdiction}/{bucket}", bucket=bucket,
             jurisdiction=jurisdiction, rules=[[p, d] for p, d in plan],
             account=account["name"])
    print(f"  bucket {bucket} ({jurisdiction}): lifecycle replaced and read back rule by rule — "
          f"{r2_lifecycle_summary(plan)}; no key was minted, rolled or delivered")
    return 0


def cmd_groups(account_label: str | None, match: str | None) -> int:
    """The account's permission-group catalogue, by NAME and LEVEL only.

    A preset names its groups, and Cloudflare's permissions reference does not
    always list a new product's (Email Sending was absent on 2026-09-30). This
    reads the catalogue the admin sees — read-only — so a preset is written
    from the provider's own names rather than a guess. Ids are withheld: a
    policy is built by `mint`, never by pasting an id.
    """
    try:
        _stash, admin, account = find_account(account_label)
        groups = _request(f"/accounts/{account['id']}/tokens/permission_groups?per_page=500",
                          admin).get("result", [])
    except RuntimeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    words = (match or "").lower().split()
    rows = sorted({(g.get("name", ""), _group_level(g)) for g in groups
                   if all(w in (g.get("name") or "").lower() for w in words)})
    print(f"permission groups in {account['name']}"
          + (f" matching {match!r}" if match else "") + f" ({len(rows)}):")
    for name, level in rows:
        print(f"  {level:8s} {name}")
    return 0


def cmd_install(value: str) -> int:
    """A narrow token minted ELSEWHERE, pasted in: verified the same way an
    issued one is, filed with a record marking it external — so `ping` watches
    it and `rotate --leaked` knows it cannot roll it here."""
    if not value:
        print("nothing on stdin", file=sys.stderr)
        return 2
    if len(value.split()) > 1:
        print(f"refused: {len(value.split())} whitespace-separated pieces arrived, "
              f"not one token", file=sys.stderr)
        return 1
    try:
        zs = zones_of(value)
    except RuntimeError as exc:
        print(f"refused: {exc}\n  (what arrived was {len(value)} characters; a "
              f"Cloudflare API token is 40)", file=sys.stderr)
        return 1
    if not zs:
        print("refused: this token sees no zone", file=sys.stderr)
        return 1
    why = can_read_analytics(value, [z["id"] for z in zs])
    if why:
        print(f"refused: the token lists {len(zs)} zone(s) but cannot read their "
              f"analytics — {why}\n  add `Zone -> Analytics -> Read` to it, or "
              f"stash an admin token and let `issue` build the right one",
              file=sys.stderr)
        return 1
    label = slug(next((z["account"] for z in zs if z["account"]), zs[0]["name"]))
    dest = token_dir() / label
    prior = read_meta(dest)
    write_secret(dest, value)
    write_meta(dest, json.dumps({
        "kind": "external", "preset": "analytics",
        "account_name": next((z["account"] for z in zs if z["account"]), ""),
        "project": prior.get("project"),
        "zones": [z["name"] for z in zs],
        "installed_on": prior.get("installed_on") or today(),
        "note": "minted outside this program — rotation happens where it was minted",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"installed: {label} ({len(zs)} zone(s), analytics readable)")
    return 0


# ─────────────────────────── rotate, revoke, ping ───────────────────────────

def issued() -> list[pathlib.Path]:
    d = token_dir()
    if not d.is_dir():
        return []
    return [p for p in sorted(d.iterdir())
            if p.is_file() and not p.name.startswith(".")
            and not p.name.endswith(".meta.json")]


def cmd_rotate(label: str | None, leaked: bool) -> int:
    targets = issued()
    if leaked:
        names = leaked_labels()
        if not names:
            print("no open leak names a Cloudflare token — nothing to rotate")
            return 0
        targets = [p for p in targets if p.name in names]
        if not targets:
            print(f"open leaks name {sorted(names)}, none of which is an issued "
                  f"token here — rotate them where they live", file=sys.stderr)
            return 1
    elif label:
        targets = [p for p in targets if p.name == label]
        if not targets:
            print(f"no issued token called {label!r}", file=sys.stderr)
            return 1
    if not targets:
        print("nothing issued yet — `python \"$(project-observatory full-path)/tools/cloudflare.py\" issue --preset analytics`")
        return 0
    bad = 0
    for p in targets:
        m = read_meta(p)
        if m.get("kind") == "external":
            print(f"  {p.name}: minted outside this program — rotate it where it "
                  f"was minted, or stash this account's admin token and `issue` a "
                  f"managed one over it", file=sys.stderr)
            bad += 1
            continue
        try:
            _label, admin = read_admin(m.get("stash") or p.name)
            _tid, value = mint(admin, m["account_id"], PRESETS[m["preset"]])
        except (RuntimeError, KeyError) as exc:
            print(f"  {p.name}: could not rotate — {exc}", file=sys.stderr)
            bad += 1
            continue
        bad += install_issued(value, p.name,
                              {"id": m["account_id"], "name": m.get("account_name", "")},
                              m["preset"], m.get("project"), True,
                              stash=m.get("stash") or p.name)
    if leaked and not bad:
        print("the leaked values are dead; settle the register with "
              "`python \"$(project-observatory full-path)/tools/vault.py\" rotate` for each row it names")
    return 1 if bad else 0


def leaked_labels() -> set[str]:
    """Issued-token names that appear in an OPEN leak row.

    Read through the vault's own register rather than a second copy of it: one
    place records leaks, and a tool that keeps its own list is a second truth
    that will disagree.
    """
    import vault
    out: set[str] = set()
    try:
        for row in vault.open_leaks():
            blob = json.dumps(row, ensure_ascii=False).lower()
            for p in issued():
                if p.name.lower() in blob:
                    out.add(p.name)
    except Exception as exc:                                                      
        print(f"  (the leak register could not be read: {exc})", file=sys.stderr)
    return out


def cmd_revoke(label: str) -> int:
    p = token_dir() / label
    if not p.is_file():
        print(f"no issued token called {label!r}", file=sys.stderr)
        return 1
    m = read_meta(p)
    try:
        _l, admin = read_admin(m.get("stash") or label)
        tid = existing_token(admin, m["account_id"], PRESETS[m["preset"]]["name"])
        if tid:
            _request(f"/accounts/{m['account_id']}/tokens/{tid}", admin, method="DELETE")
    except (RuntimeError, KeyError) as exc:
        print(f"refused: {exc}\n  the file was NOT removed — a local delete that "
              f"leaves the token live at Cloudflare is the worst of both",
              file=sys.stderr)
        return 1
    p.unlink()
    meta(p).unlink(missing_ok=True)
    _journal("revoke", f"cloudflare/{m.get('account_name', '?')}/{label}")
    print(f"revoked at Cloudflare and removed locally: {label}")
    return 0


def cmd_ping() -> int:
    """Does every stashed and issued token still do its job? No values printed."""
    bad = 0
    for label, path in admins():
        m = read_meta(path)
        try:
            accts = discover_accounts(private_io.read(path).strip())
            if not accts:
                raise RuntimeError("sees no account by either road")
            print(f"  admin/{label}: alive, sees {len(accts)} account(s)"
                  f" — stashed {m.get('stashed_on', '?')}")
        except RuntimeError as exc:
            print(f"  admin/{label}: DEAD — {exc}", file=sys.stderr)
            bad += 1
    for p in issued():
        m = read_meta(p)
        value = private_io.read(p).strip()
        try:
            zs = zones_of(value)
        except RuntimeError as exc:
            print(f"  {p.name}: DEAD — {exc}", file=sys.stderr)
            bad += 1
            continue
        why = can_read_analytics(value, [z["id"] for z in zs]) if zs else "sees no zone"
        stamp = m.get("rotated_on") or m.get("issued_on") or "?"
        if why:
            print(f"  {p.name}: {len(zs)} zone(s) but CANNOT read analytics — {why}",
                  file=sys.stderr)
            bad += 1
        else:
            print(f"  {p.name}: alive, {len(zs)} zone(s), analytics readable "
                  f"(last change {stamp}"
                  + (f", for {m['project']}" if m.get("project") else "") + ")")
    if not admins() and not issued():
        print("nothing stashed and nothing issued")
    return 1 if bad else 0


def cmd_list() -> int:
    """Names, accounts, dates, projects. Never a value, never a length that
    could narrow one."""
    a, i = admins(), issued()
    print(f"admin tokens ({len(a)}) — read by this program only:")
    for label, p in a:
        m = read_meta(p)
        accts = m.get("accounts") or [{"name": m.get("account_name", "?"), "can_issue": True}]
        print(f"  {label:28s} stashed {m.get('stashed_on', '?')}  "
              f"mode {oct(p.stat().st_mode)[-3:]}")
        for acc in accts:
            print(f"      {'can issue' if acc.get('can_issue', True) else 'read-only':10s} "
                  f"{acc['name']}  [{slug(acc['name'])}]")
    print(f"issued tokens ({len(i)}) — what the plugins read:")
    for p in i:
        m = read_meta(p)
        print(f"  {p.name:28s} {m.get('preset', '?'):12s} "
              f"{m.get('project') or '(no project)':32s} "
              f"issued {m.get('issued_on', '?')} "
              f"rotated {m.get('rotated_on') or '—'} x{m.get('rotations', 0)}")
    if not a:
        print("\nstart with cloudflare stash, reading a protected file on stdin")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stash", help="admin token on stdin; stored, never handed out")
    sub.add_parser("install", help="a narrow token minted elsewhere, on stdin")
    p_issue = sub.add_parser("issue", help="mint a narrow token and install it")
    p_issue.add_argument("--preset", default="analytics", choices=sorted(PRESETS))
    p_issue.add_argument("--account", help="which stashed admin token to issue from")
    p_issue.add_argument("--project", help="the project this token serves")
    p_issue.add_argument("--zone", help="zone presets: the one zone the token may touch")
    p_issue.add_argument("--vault", help="writer presets: the slot PROJECT/ENV/NAME it is delivered to")
    p_issue.add_argument("--bucket", help="r2-bucket: the one bucket the key pair may touch")
    p_issue.add_argument("--jurisdiction", default="default", choices=R2_JURISDICTIONS,
                         help="r2-bucket: where the bucket's objects must stay")
    p_issue.add_argument("--expire-days", type=int, default=None,
                         help="r2-bucket: whole-bucket lifecycle — objects are deleted after this "
                              f"many days (default {R2_ISSUE_DEFAULT_DAYS} when no --lifecycle-rule is given)")
    p_issue.add_argument("--lifecycle-rule", action="append", metavar="PREFIX:DAYS",
                         help="r2-bucket: objects under PREFIX are deleted after DAYS, and its "
                              "unfinished multipart uploads abort after 1 day; repeatable")
    p_life = sub.add_parser("lifecycle", help="replace an existing R2 bucket's lifecycle; "
                                              "no key is minted, rolled or delivered")
    p_life.add_argument("--account", help="which stashed admin token to act through")
    p_life.add_argument("--bucket", required=True, help="the existing bucket")
    p_life.add_argument("--jurisdiction", default="default", choices=R2_JURISDICTIONS,
                        help="where the bucket lives")
    p_life.add_argument("--expire-days", type=int, default=None,
                        help="whole-bucket rule: objects are deleted after this many days")
    p_life.add_argument("--lifecycle-rule", action="append", metavar="PREFIX:DAYS",
                        help="objects under PREFIX are deleted after DAYS; repeatable")
    sub.add_parser("list", help="what exists, with dates — never values")
    p_groups = sub.add_parser("groups", help="permission-group names and levels, for writing a preset")
    p_groups.add_argument("--account", help="which reachable account's catalogue")
    p_groups.add_argument("--match", help="words every listed name must contain")
    sub.add_parser("ping", help="can every token still do its job")
    p_rot = sub.add_parser("rotate", help="roll a token's value in place")
    p_rot.add_argument("label", nargs="?")
    p_rot.add_argument("--leaked", action="store_true",
                       help="rotate every issued token named in an open leak")
    p_rev = sub.add_parser("revoke", help="delete at Cloudflare, then locally")
    p_rev.add_argument("label")
    a = ap.parse_args()
    if a.cmd == "stash":
        if sys.stdin.isatty():
            print("paste the admin token on stdin, e.g. "
                  "run cloudflare stash with a protected file redirected to stdin", file=sys.stderr)
            return 2
        return cmd_stash(sys.stdin.read().strip())
    if a.cmd == "install":
        if sys.stdin.isatty():
            print("paste the narrow token on stdin", file=sys.stderr)
            return 2
        return cmd_install(sys.stdin.read().strip())
    if a.cmd == "lifecycle":
        return cmd_lifecycle(a.bucket, a.jurisdiction, a.expire_days, a.lifecycle_rule or [],
                             a.account)
    if a.cmd == "issue":
        scope = PRESETS[a.preset].get("scope")
        if scope != "bucket-vault" and (a.expire_days is not None or a.lifecycle_rule):
            print("refused: --expire-days and --lifecycle-rule apply to --preset r2-bucket only",
                  file=sys.stderr)
            return 2
        if scope == "zone":
            return cmd_issue_zone(a.preset, a.zone, a.vault, a.account)
        if scope == "bucket-vault":
            if a.zone:
                print(f"refused: preset {a.preset!r} is bucket-scoped; --zone does not apply",
                      file=sys.stderr)
                return 2
            return cmd_issue_bucket(a.preset, a.bucket, a.jurisdiction, a.expire_days,
                                    a.vault, a.account, lifecycle_rules=a.lifecycle_rule or [])
        if scope == "account-vault":
            if a.zone:
                print(f"refused: preset {a.preset!r} is account-scoped; --zone does not apply",
                      file=sys.stderr)
                return 2
            return cmd_issue_account(a.preset, a.vault, a.account)
        return cmd_issue(a.preset, a.account, a.project)
    if a.cmd == "list":
        return cmd_list()
    if a.cmd == "groups":
        return cmd_groups(a.account, a.match)
    if a.cmd == "ping":
        return cmd_ping()
    if a.cmd == "rotate":
        return cmd_rotate(a.label, a.leaked)
    if a.cmd == "revoke":
        return cmd_revoke(a.label)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError):
        print("Private credential operation refused; values hidden", file=sys.stderr)
        raise SystemExit(2)
