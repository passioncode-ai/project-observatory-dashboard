#!/usr/bin/env python3
"""The one door to Cloudflare credentials: stash, issue, rotate, revoke, ping.

    ./tools/cloudflare.py stash            # admin token on stdin, once per account
    ./tools/cloudflare.py issue --preset analytics [--project project:x]
    ./tools/cloudflare.py issue --preset dns-edit --zone example.com --vault PROJECT/ENV/NAME
    ./tools/cloudflare.py issue --preset d1-edit --account <slug> --vault PROJECT/ENV/NAME
    ./tools/cloudflare.py list
    ./tools/cloudflare.py ping
    ./tools/cloudflare.py rotate <label>   # or --leaked, for every open leak
    ./tools/cloudflare.py revoke <label>

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
    },
}

#: Presets whose token is a project's credential, delivered to a vault slot —
#: never filed in the folder the read-only analytics plugin loads.
VAULT_SCOPES = ("zone", "account-vault")


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
             method: str | None = None) -> dict:
    """One call, with the error stripped to endpoint and status.

    Never the headers, never the body we sent: a signed credential in an
    exception message is a leak into every log and transcript that records it.
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{API}{path}", data=data, method=method or ("POST" if data else "GET"),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
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
          + "\n".join(f"  ./tools/cloudflare.py issue --preset analytics "
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
        raise RuntimeError("cloudflare created the token but returned no value")
    return res.get("id", ""), res["value"]


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
        vault.validate_names(*parts)
    except vault.VaultBoundaryError as exc:
        raise ValueError(f"--vault {exc}") from None
    return parts[0], parts[1], parts[2]


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
    """Issue a zone-scoped token (DNS edit) into a vault slot."""
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
    try:
        _stash, admin, account = find_account(account_label)
        zid = zone_id_of(admin, account["id"], zone)
        token_name = preset["name"].format(zone=zone)
        rolled = existing_token(admin, account["id"], token_name) is not None
        _tid, value = mint(admin, account["id"], preset,
                           resources={f"com.cloudflare.api.account.zone.{zid}": "*"},
                           name=token_name)
        # A fresh token can take a moment to be honoured at the edge; the
        # value is delivered only once it has proved it can read the zone.
        for attempt in range(5):
            try:
                _request(f"/zones/{zid}/dns_records?per_page=1", value)
                break
            except RuntimeError:
                if attempt == 4:
                    raise RuntimeError(f"the issued token cannot read {zone}'s records") from None
                time.sleep(wait)
        deliver_to_vault(value, project, env, name)
    except RuntimeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    _journal("rotate" if rolled else "issue", f"{project}/{env}/{name}",
             preset=preset_key, zone=zone, account=account["name"])
    print(f"  {project}/{env}/{name}: {'rolled' if rolled else 'issued'} — "
          f"{', '.join(preset['groups'])} on {zone} only; "
          f"use: tools/use_secret.py run --env {env} {project} {name} -- <command>")
    return 0


def cmd_issue_account(preset_key: str, target: str | None, account_label: str | None,
                      wait: float = 2.0) -> int:
    """Issue an account-scoped writer (D1 edit) into a vault slot."""
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
    try:
        _stash, admin, account = find_account(account_label)
        token_name = preset["name"].format(slot=f"{project}/{env}/{name}")
        rolled = existing_token(admin, account["id"], token_name) is not None
        _tid, value = mint(admin, account["id"], preset, name=token_name)
        for attempt in range(5):
            try:
                _request(f"/accounts/{account['id']}/d1/database?per_page=1", value)
                break
            except RuntimeError:
                if attempt == 4:
                    raise RuntimeError("the issued token cannot list this account's "
                                       "D1 databases") from None
                time.sleep(wait)
        deliver_to_vault(value, project, env, name)
    except RuntimeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    _journal("rotate" if rolled else "issue", f"{project}/{env}/{name}",
             preset=preset_key, account=account["name"])
    # An account-owned token cannot call /memberships, which is how wrangler
    # finds an account a config does not name — so the id travels with the use.
    print(f"  {project}/{env}/{name}: {'rolled' if rolled else 'issued'} — "
          f"{', '.join(preset['groups'])} on account {account['name']} only; "
          f"use: CLOUDFLARE_ACCOUNT_ID={account['id']} "
          f"tools/use_secret.py run --env {env} {project} {name} -- <command>")
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
        print("nothing issued yet — `./tools/cloudflare.py issue --preset analytics`")
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
              "`./tools/vault.py rotate` for each row it names")
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
    sub.add_parser("list", help="what exists, with dates — never values")
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
    if a.cmd == "issue":
        scope = PRESETS[a.preset].get("scope")
        if scope == "zone":
            return cmd_issue_zone(a.preset, a.zone, a.vault, a.account)
        if scope == "account-vault":
            if a.zone:
                print(f"refused: preset {a.preset!r} is account-scoped; --zone does not apply",
                      file=sys.stderr)
                return 2
            return cmd_issue_account(a.preset, a.vault, a.account)
        return cmd_issue(a.preset, a.account, a.project)
    if a.cmd == "list":
        return cmd_list()
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
