#!/usr/bin/env python3
"""Your share mailbox, `observatory-cloud/1`, in your own Cloudflare account (OBS-263, contract DEC-0142 PS-10).

    project-observatory full cloud deploy --account ACCOUNT --name SCRIPT
        deploy the mailbox (a Worker with one Durable Object per share) into your Cloudflare account, through the
        Cloudflare door's narrow token; again to update it. --new-token rolls that token first; --dry-run only says
        what it would do
    project-observatory full cloud status --json
        whether the deployed mailbox answers, and whether it runs this engine's code and this machine's owner key
    project-observatory full cloud url
        the mailbox's address, which `full share create` uses

WHAT IT HOLDS. Ciphertext and names: the frames of each share's code exchange, the sealed copies, the sealed usage
reports, the revocations and deletion receipts. Never a value, never a private key. It forgets an unclaimed share a
day after its claim deadline, and a sealed one 30 days after the latest of its last sealed copy's end, its revocation
and its last renewal; a share taken back loses its sealed copies at once.

THE TOKEN. Deploying uses the Cloudflare door's `workers-edit` preset: a token minted from the admin token you
stashed once (`tools/cloudflare.py stash`), limited to Workers on one account, delivered into the vault slot
`observatory-cloud/prod/CLOUDFLARE_WORKERS_TOKEN` and read from there in memory for the API calls. It never reaches
argv, a log or this command's output. The owner key is made once under `node/shares/owner/`; its public half is bound
into the Worker, so only this machine files shares on your mailbox.

WHAT IT CANNOT DO FOR YOU. With no admin token stashed, the door cannot mint the deploy token: the step is yours, and
this command names it with its address.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import secrets
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))                    # the engine first: `node` is the package, never tools/node.py
import paths  # noqa: E402

API = "https://api.cloudflare.com/client/v4"
PROG = "project-observatory full" + " cloud"
WORKER = ROOT / "cloud" / "worker.mjs"
CONFIG = paths.CONFIG / "cloud.json"
SCHEMA = "observatory-cloud-deployment/1"
DEFAULT_SCRIPT = "observatory-cloud"
TOKEN_SLOT = ("observatory-cloud", "prod", "CLOUDFLARE_WORKERS_TOKEN")
MIGRATION_TAG = "v1"
COMPATIBILITY_DATE = "2026-10-01"
TOKENS_PAGE = "https://dash.cloudflare.com/profile/api-tokens"
DOOR = 'python "$(project-observatory full-path)/tools/cloudflare.py"'


def _node(name: str):
    """`node.<name>` of the engine. The Cloudflare door and use_secret put `tools/` first on the path, where
    `tools/node.py` (`full node`) would shadow the engine's `node` package; the engine is put back in front first."""
    import importlib
    if sys.path[0] != str(ROOT):
        sys.path.insert(0, str(ROOT))
    loaded = sys.modules.get("node")
    if loaded is not None and not hasattr(loaded, "__path__"):
        del sys.modules["node"]
    return importlib.import_module("node." + name)


class CloudError(Exception):
    """A refused or failed step. The message names the step and, for a person's step, its address; never a value."""


# ---- The Cloudflare API, with the token in a header and nowhere else -------------------------------------------------

def cf(method: str, path: str, token: str, body: bytes | None = None, content_type: str = "application/json") -> dict:
    """One call. An error names the status and Cloudflare's error codes and messages, never what was sent."""
    request = urllib.request.Request(API + path, data=body, method=method,
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": content_type,
                                              "User-Agent": "project-observatory-cloud/1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as answer:
            return json.loads(answer.read())
    except urllib.error.HTTPError as error:
        try:
            doc = json.loads(error.read(65536) or b"{}")
        except ValueError:
            doc = {}
        finally:
            error.close()
        said = "; ".join(f"{e.get('code')}: {str(e.get('message'))[:200]}" for e in doc.get("errors") or []
                         if isinstance(e, dict))
        where = path.replace(path.split("/")[2], "<account>") if path.startswith("/accounts/") else path
        raise CloudError(f"Cloudflare answered HTTP {error.code} to {method} {where}"
                         + (f" ({said})" if said else "")) from None
    except OSError as error:
        raise CloudError(f"Cloudflare did not answer ({type(error).__name__})") from None


# ---- What the door holds ---------------------------------------------------------------------------------------------

def accounts() -> list[dict]:
    """Every account a stashed admin can issue into, from the door's metadata only (no admin token is read here)."""
    import cloudflare as door
    out = []
    for label, _path in door.admins():
        for account in door.stash_accounts(label):
            out.append({"id": account["id"], "name": account.get("name") or label, "slug": door.slug(account.get("name") or label)})
    return out


def choose_account(wanted: str | None) -> dict:
    found = accounts()
    if not found:
        raise CloudError(
            "no Cloudflare admin token is stashed, so the door cannot mint the narrow deploy token. Your step, once: "
            f"create a token at {TOKENS_PAGE} that can create tokens on the account (Account API Tokens: Edit, "
            "Account Settings: Read), save it to a file only you can read, then run "
            f"{DOOR} stash < FILE  and run this command again")
    if wanted:
        hits = [a for a in found if wanted in (a["slug"], a["id"], a["name"])]
        if len(hits) != 1:
            raise CloudError(f"no single stashed account is called {wanted!r}; have: "
                             + ", ".join(a["slug"] for a in found))
        return hits[0]
    if len(found) > 1:
        raise CloudError(f"{len(found)} Cloudflare accounts are stashed: name one with --account: "
                         + ", ".join(a["slug"] for a in found))
    return found[0]


def token_present() -> bool:
    import use_secret
    slot, _env = use_secret.vault_slot(TOKEN_SLOT[0], TOKEN_SLOT[2], TOKEN_SLOT[1])
    return slot is not None


def issue_token(account: dict) -> None:
    """The door's `issue --preset workers-edit`: minted from the stashed admin, proved, delivered into the slot. Its
    output names the slot and the account, never the value; it is relayed as it is."""
    import subprocess
    project, env, name = TOKEN_SLOT
    run = subprocess.run([sys.executable, str(ROOT / "tools" / "cloudflare.py"), "issue",
                          # One argument: this tool takes no --preset of its own (tests/test_docs_drift.py).
                          "--preset=workers-edit",
                          "--account", account["slug"], "--vault", f"{project}/{env}/{name}"],
                         capture_output=True, text=True, timeout=600)
    for line in (run.stdout + run.stderr).splitlines():
        print("  door: " + line)
    if run.returncode != 0:
        raise CloudError(f"the Cloudflare door did not issue the deploy token (exit {run.returncode}); its own words "
                         "are above")


def read_token() -> str:
    """The deploy token's value, from the vault only, for this process's API calls."""
    import use_secret
    value, _where = use_secret.resolve(TOKEN_SLOT[0], TOKEN_SLOT[2], TOKEN_SLOT[1], vault_only=True)
    return value


def owner_jwk() -> dict:
    """This machine's owner key for its mailbox (made once under node/shares/owner/): only its public half leaves."""
    SH = _node("shares")
    jwk, _key = SH.Shares(paths.setting_path("OBSERVATORY_NODE", paths.HOME / "node").resolve() / "shares").owner()
    return jwk


# ---- The upload ------------------------------------------------------------------------------------------------------

def worker_code() -> bytes:
    return WORKER.read_bytes()


def digest_of(code: bytes) -> str:
    return "sha256:" + hashlib.sha256(code).hexdigest()


def metadata(owner: dict, digest: str, migrate: bool) -> dict:
    meta = {
        "main_module": "worker.mjs",
        "compatibility_date": COMPATIBILITY_DATE,
        "bindings": [
            {"type": "durable_object_namespace", "name": "SHARES", "class_name": "ShareBox"},
            {"type": "plain_text", "name": "OWNER_JWK", "text": json.dumps(owner, sort_keys=True)},
            {"type": "plain_text", "name": "WORKER_DIGEST", "text": digest},
        ],
    }
    if migrate:
        # SQLite-backed Durable Objects, the class the free plan offers.
        meta["migrations"] = {"new_tag": MIGRATION_TAG, "new_sqlite_classes": ["ShareBox"]}
    return meta


def multipart(meta: dict, code: bytes) -> tuple[bytes, str]:
    boundary = "observatory-" + secrets.token_hex(12)
    parts = [
        (f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"; filename="metadata.json"\r\n'
         'Content-Type: application/json\r\n\r\n').encode() + json.dumps(meta).encode() + b"\r\n",
        (f'--{boundary}\r\nContent-Disposition: form-data; name="worker.mjs"; filename="worker.mjs"\r\n'
         'Content-Type: application/javascript+module\r\n\r\n').encode() + code + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def well_known(origin: str, timeout: float = 15.0) -> dict:
    MB = _node("mailbox")
    request = urllib.request.Request(MB._resolved(origin + "/.well-known/observatory-cloud"),
                                     headers={"User-Agent": "project-observatory-cloud/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            doc = json.loads(answer.read(65536))
    except urllib.error.HTTPError as error:
        error.close()
        raise CloudError(f"the mailbox answered HTTP {error.code}") from None
    except (OSError, ValueError) as error:
        raise CloudError(f"the mailbox did not answer ({type(error).__name__})") from None
    if not isinstance(doc, dict) or doc.get("protocol") != "observatory-cloud/1":
        raise CloudError("the address answers, but not as an observatory-cloud/1 mailbox")
    return doc


def is_ours(settings: dict) -> bool:
    """Whether a Worker's settings are those `deploy` uploads: the ShareBox namespace and the code's digest bound."""
    bindings = settings.get("bindings") if isinstance(settings.get("bindings"), list) else []
    named = {b.get("name"): b for b in bindings if isinstance(b, dict)}
    return (named.get("SHARES", {}).get("type") == "durable_object_namespace"
            and named.get("SHARES", {}).get("class_name") == "ShareBox"
            and named.get("WORKER_DIGEST", {}).get("type") == "plain_text")


def deploy(*, account_ref: str | None, script: str, new_token: bool, dry_run: bool,
           wait_seconds: float = 120.0, now=time.time) -> dict:
    account = choose_account(account_ref)
    code = worker_code()
    digest = digest_of(code)
    owner = owner_jwk()
    PS = _node("peer_share")
    plan = {"account": account["slug"], "script": script, "tokenSlot": "/".join(TOKEN_SLOT), "digest": digest,
            "owner": PS.peer_thumbprint(owner)}
    if dry_run:
        return {**plan, "dryRun": True, "tokenPresent": token_present()}
    if new_token or not token_present():
        print(f"minting the deploy token through the Cloudflare door into {'/'.join(TOKEN_SLOT)}")
        issue_token(account)
    token = read_token()
    base = f"/accounts/{account['id']}/workers"
    listed = cf("GET", f"{base}/scripts", token).get("result") or []
    current = next((s for s in listed if isinstance(s, dict) and s.get("id") == script), None)
    subdomain = ((cf("GET", f"{base}/subdomain", token).get("result") or {}).get("subdomain") or "")
    if not subdomain:
        raise CloudError("this account has no workers.dev subdomain yet, so the mailbox would have no address. Your "
                         f"step, once: open https://dash.cloudflare.com/{account['id']}/workers-and-pages and choose a "
                         f"workers.dev subdomain, then run `{PROG} deploy` again. Nothing was uploaded")
    if current is not None:
        # Never over another Worker: one of this name is replaced only when it answers as this kind of mailbox, or
        # when its own settings show it is one (a deploy of ours that stopped before its address answered).
        try:
            well_known(f"https://{script}.{subdomain}.workers.dev")
        except CloudError as error:
            if not is_ours(cf("GET", f"{base}/scripts/{script}/settings", token).get("result") or {}):
                raise CloudError(f"a Worker called {script} already runs in this account and is not an "
                                 f"observatory-cloud mailbox ({error}); nothing was uploaded. Choose another name "
                                 f"with --name") from None
            print(f"{script} is a mailbox of ours that did not finish deploying; deploying it again")
    migrate = (current or {}).get("migration_tag") != MIGRATION_TAG
    body, content_type = multipart(metadata(owner, digest, migrate), code)
    print(f"uploading {script} ({len(code)} bytes, {digest[:19]}...) to account {account['slug']}")
    cf("PUT", f"{base}/scripts/{script}", token, body, content_type)
    cf("POST", f"{base}/scripts/{script}/subdomain", token,
       json.dumps({"enabled": True, "previews_enabled": False}).encode())
    del token
    origin = f"https://{script}.{subdomain}.workers.dev"
    deadline = now() + wait_seconds
    while True:
        try:
            answered = well_known(origin)
            break
        except CloudError:
            if now() >= deadline:
                raise CloudError(f"the Worker is deployed at {origin} but did not answer within "
                                 f"{int(wait_seconds)} s; `{PROG} status` checks it again") from None
            time.sleep(3)
    if answered.get("digest") != digest or answered.get("owner") != plan["owner"]:
        raise CloudError(f"{origin} answers with another code or owner key than this machine deployed; "
                         f"run `{PROG} deploy` again")
    record = {"schema": SCHEMA, "origin": origin, "accountId": account["id"], "account": account["slug"],
              "script": script, "version": answered.get("version"), "digest": digest, "owner": plan["owner"],
              "tokenSlot": "/".join(TOKEN_SLOT), "deployedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    import atomic
    atomic.write_json(CONFIG, record)
    return record


def deployment() -> dict | None:
    try:
        doc = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and isinstance(doc.get("origin"), str) else None


def status() -> dict:
    record = deployment()
    if record is None:
        return {"state": "not-deployed", "next": f"{PROG} deploy"}
    local = digest_of(worker_code())
    try:
        answered = well_known(record["origin"])
    except CloudError as error:
        return {"state": "unreachable", "origin": record["origin"], "why": str(error), "deployedAt": record.get("deployedAt")}
    current = answered.get("digest") == local and answered.get("owner") == record.get("owner")
    return {"state": "current" if current else "outdated", "origin": record["origin"], "version": answered.get("version"),
            "deployedAt": record.get("deployedAt"), "runsThisEngine": answered.get("digest") == local,
            "ownerMatches": answered.get("owner") == record.get("owner"),
            **({} if current else {"next": f"{PROG} deploy"})}


# ---- The command line -----------------------------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=PROG, description="Your share mailbox (observatory-cloud/1) in your own "
                                                       "Cloudflare account.")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("deploy", help="deploy or update the mailbox through the Cloudflare door")
    d.add_argument("--account", help="the stashed Cloudflare account (its slug, as the door lists it)")
    d.add_argument("--name", default=DEFAULT_SCRIPT, help=f"the Worker's name (default {DEFAULT_SCRIPT})")
    d.add_argument("--new-token", action="store_true", help="roll the deploy token first")
    d.add_argument("--dry-run", action="store_true", help="say what would be deployed; no call is made")
    s = sub.add_parser("status", help="whether the mailbox answers and runs this engine's code")
    s.add_argument("--json", action="store_true")
    sub.add_parser("url", help="the mailbox's address")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.cmd == "deploy":
            if not __import__("re").fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", args.name):
                print("cloud: a Worker name is lower-case letters, digits and hyphens", file=sys.stderr)
                return 2
            record = deploy(account_ref=args.account, script=args.name, new_token=args.new_token,
                            dry_run=args.dry_run)
            if record.get("dryRun"):
                print(json.dumps(record, indent=1, sort_keys=True))
                return 0
            print(f"your mailbox: {record['origin']} (version {record['version']}); `full share create` uses it")
            return 0
        if args.cmd == "status":
            doc = status()
            if args.json:
                print(json.dumps(doc, indent=1, sort_keys=True))
            else:
                print(f"{doc['state']}" + (f": {doc['origin']}" if doc.get("origin") else "")
                      + (f" ({doc['why']})" if doc.get("why") else "") + (f"; next: {doc['next']}" if doc.get("next") else ""))
            return 0 if doc["state"] == "current" else 1
        record = deployment()
        if record is None:
            print(f"cloud: no mailbox is deployed; `{PROG} deploy` makes one", file=sys.stderr)
            return 1
        print(record["origin"])
        return 0
    except CloudError as error:
        print(f"cloud: {error}", file=sys.stderr)
        return 1
    except LookupError as error:
        print(f"cloud: the deploy token is not in the vault: {error}", file=sys.stderr)
        return 1
    except (RuntimeError, ValueError, OSError) as error:
        print(f"cloud: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
