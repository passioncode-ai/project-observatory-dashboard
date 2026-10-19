#!/usr/bin/env python3
"""The one door to Heroku credentials and teams: stash, issue, rotate, revoke, ping, teams.

    T="$(project-observatory full-path)/tools"          # the installed engine's tools
    heroku auth:token | python "$T/heroku.py" stash     # the CLI session's token, through a pipe
    python "$T/heroku.py" stash --from-cli              # the same, the pipe made inside this program
    python "$T/heroku.py" issue --preset read --vault PROJECT/ENV/NAME [--team TEAM] [--app APP]
    python "$T/heroku.py" issue --preset deploy --vault PROJECT/ENV/NAME --app APP [--expire-days 90]
    python "$T/heroku.py" list
    python "$T/heroku.py" ping
    python "$T/heroku.py" rotate PROJECT/ENV/NAME        # or --leaked, for every open leak
    python "$T/heroku.py" revoke PROJECT/ENV/NAME
    python "$T/heroku.py" teams
    python "$T/heroku.py" members --team TEAM
    python "$T/heroku.py" apps --team TEAM
    python "$T/heroku.py" member add    --team TEAM --email ADDRESS --role member [--apply]
    python "$T/heroku.py" member role   --team TEAM --email ADDRESS --role admin  [--apply]
    python "$T/heroku.py" member remove --team TEAM --email ADDRESS             [--apply]

THE SAME DOOR AS CLOUDFLARE'S, ON THE HEROKU PLATFORM API. The admin token is
stashed in `secrets/heroku-admin/` at mode 600, read by this program alone, and
never handed out. Every working credential is ISSUED from it: a direct OAuth
authorization (`POST /oauth/authorizations`) with a narrow scope, a description
that says what it is for, proved with its own value, then delivered to a vault
slot on stdin. Values travel on stdin, never in argv; nothing here prints one,
including on error.

WHAT HEROKU CAN AND CANNOT NARROW. An authorization's only narrowing is its
SCOPE (`identity`, `read`, `write`, `read-protected`, `write-protected`,
`global`; the CLI's own list and devcenter.heroku.com/articles/oauth). It is the
USER's authorization: Heroku offers no field that limits one to a team or an app
(the Platform API schema's create link takes `client`, `description`,
`expires_in` and `scope`, nothing else). So `--team` and `--app` do not pretend
to be a grant: they are written into the description Heroku shows beside the
authorization, recorded in the ledger, and they are what the proof reads before
the value is delivered. The preset table states this, and `list` repeats it.

`read` and `write` exclude config vars; the `-protected` scopes include them,
which is why the presets that grant them are named after config, not deploys.
Heroku's own caveat holds: even `read` can see a credential a log drain's URL
embeds.

EVERY ISSUED AUTHORIZATION HAS A LEDGER ROW in `heroku-issued.json` beside the
stash: its id at Heroku, preset, scopes, slot, team, app, dates and rotation
count. Never a value, and never the refresh token Heroku also returns, which is
discarded unread. That ledger is what `list`, `ping`, `teams` and the
credentials projection read.

ROTATION IS IN PLACE. `regenerate-tokens` keeps the authorization's id, scope
and description and kills the old value at once, so the slot is written in the
same run; a delivery that fails after it says that the slot now holds a dead
value.

TEAM MEMBERSHIP WRITES ARE A PLAN UNTIL `--apply`. `member add|role|remove`
reads the team, prints what would change and stops. With `--apply` it makes the
one call, reads the team again and checks the result. It refuses `owner` (a
transfer of ownership is not a membership edit), removing or demoting the last
admin, and changing the stashed admin's own membership: a door that can lock its
own operator out of a team is not a door.

A TEAM THAT INVITES IS WRITTEN THE CLI'S WAY. When the team's
`team-invite-acceptance` feature is on, `member add` sends an invitation
(`PUT /teams/{t}/invitations`, Accept `version=3.team-invitations`) the person
must accept, `members` lists pending invitations marked as such, `member remove`
of a pending invitee cancels the invitation, and the read-back that verifies an
invitation write is the invitations list. A pending invitee is never counted as
an admin who could manage the team.

ONE OPERATION ON THE LEDGER AT A TIME. `issue`, `rotate`, `revoke` and `ping`
hold an exclusive lock beside `heroku-issued.json` from their first read to
their last write, and each write replaces only its own row in the ledger as read
under that lock.
"""
from __future__ import annotations
import argparse
import contextlib
import datetime
import http.client
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import oslocks
import osprivacy
import private_io
import paths

API = "https://api.heroku.com"
ACCEPT = "application/vnd.heroku+json; version=3"
#: Team invitations answer only under their own variant (heroku-cli 10.16,
#: lib/members/util.js and commands/members/remove.js).
ACCEPT_INVITATIONS = "application/vnd.heroku+json; version=3.team-invitations"
#: The team feature that turns `member add` into an invitation the person must accept.
INVITE_FEATURE = "team-invite-acceptance"
ADMIN_STORE = paths.source_path("secret_store", paths.SECRETS) / "heroku-admin"
LEDGER = paths.source_path("secret_store", paths.SECRETS) / "heroku-issued.json"

#: What closes a leak once the leaked value is dead: `vault.py settle`, with this
#: engine's own interpreter, as every handed command is.
SETTLE_HINT = ("the leaked values are dead; close each row the register names with "
               f"`{shlex.quote(sys.executable)} \"$(project-observatory full-path)/tools/vault.py\" "
               "settle PROJECT ENV NAME --how \"regenerated at the provider\" "
               "--revocation-evidence \"…\" --consumer-evidence \"…\"`")

#: The scopes Heroku accepts on an authorization (the CLI's ScopeCompletion,
#: heroku-cli 10.16, and devcenter.heroku.com/articles/oauth). `global` is the
#: admin's own and no preset issues it.
SCOPES = ("identity", "read", "write", "read-protected", "write-protected", "global")

#: A preset is a scope set and a purpose. None issues `global`: a credential that
#: can do everything the admin can is the admin, and that one is never handed out.
#: `probe` is what the NEW value must read before the vault holds it; {app} and
#: {team} come from --app and --team, and the narrowest one given is used.
PRESETS: dict[str, dict] = {
    "identity": {
        "scopes": ("identity",),
        "why": "reads the account's own identity; no app, no resource",
        "probe": "account",
    },
    "read": {
        "scopes": ("read",),
        "why": "reads apps and their resources, without config vars",
        "probe": "apps",
    },
    "deploy": {
        "scopes": ("read", "write"),
        "why": "reads and changes apps and their resources (deploys, scaling, releases), without config vars",
        "probe": "apps",
    },
    "config-read": {
        "scopes": ("read-protected",),
        "why": "reads apps and their resources INCLUDING config vars",
        "probe": "apps",
        "needs_app": True,
    },
    "config-write": {
        "scopes": ("read-protected", "write-protected"),
        "why": "reads and changes apps and their resources INCLUDING config vars",
        "probe": "apps",
        "needs_app": True,
    },
}
#: The roles a membership write may set. `owner` is a transfer of the team,
#: decided elsewhere; the schema's enum also allows it, this door does not.
ROLES = ("admin", "member", "collaborator")
#: The description every authorization this door issues carries; finding it
#: again is how a second issue rolls one authorization instead of adding a twin.
DESCRIPTION = "observatory {preset} {slot}{where} (managed)"


class HerokuError(RuntimeError):
    """A provider refusal, stripped to endpoint class and status."""
    def __init__(self, status: int | str, message: str):
        super().__init__(message)
        self.status = status


def today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "unnamed"


def _journal(event: str, secret: str, **detail) -> None:
    """The vault's movements journal: every issue, rotation, revocation and
    membership write is on the record beside the vault's own. Never raises."""
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import vault
        vault.journal(event, secret, tool="heroku.py", **detail)
    except Exception as exc:
        print(f"  (movement not journaled: {type(exc).__name__})", file=sys.stderr)


# ─────────────────────────── the wire ───────────────────────────────────────

def _call(method: str, path: str, token: str, payload: dict | None = None,
          rng: str | None = None, accept: str = ACCEPT) -> tuple[object, str | None]:
    """One request: (decoded body, Next-Range or None).

    Never the headers, never the body we sent, never the provider's body: an
    error that echoed a request is how a credential reaches a transcript."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    head = {"Accept": accept, "Authorization": f"Bearer {token}",
            "Content-Type": "application/json", "User-Agent": "project-observatory"}
    if rng:
        head["Range"] = rng
    req = urllib.request.Request(f"{API}{path}", data=data, method=method, headers=head)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return (json.loads(raw) if raw else {}), r.headers.get("Next-Range")
    except urllib.error.HTTPError as e:
        e.close()
        raise HerokuError(e.code, f"heroku answered HTTP {e.code} on {_shape(path)}; "
                                  "provider response withheld") from None
    except (OSError, http.client.HTTPException) as e:
        # A dropped connection mid-answer (RemoteDisconnected, IncompleteRead) is an
        # HTTPException, not an OSError: named the same way, never echoed.
        raise HerokuError("transport", f"heroku unreachable: {type(e).__name__}") from None


def _shape(path: str) -> str:
    """The endpoint without its identifiers: `/teams/<id>/members`."""
    parts = path.split("?")[0].split("/")
    keep = {"", "account", "apps", "teams", "members", "oauth", "authorizations",
            "actions", "regenerate-tokens", "config-vars", "features", "invitations"}
    return "/".join(p if p in keep else "<id>" for p in parts)


def _request(method: str, path: str, token: str, payload: dict | None = None,
             accept: str = ACCEPT):
    return _call(method, path, token, payload, None, accept)[0]


def _list(path: str, token: str, accept: str = ACCEPT) -> list:
    """A collection to the end of its `Next-Range`: page one alone looks exactly
    like a small team."""
    out, rng = [], None
    for _ in range(100):
        body, rng = _call("GET", path, token, None, rng, accept)
        if not isinstance(body, list):
            raise HerokuError("shape", f"heroku answered {_shape(path)} with no list")
        out.extend(body)
        if not rng:
            return out
    raise HerokuError("paging", f"{_shape(path)} did not end after 100 pages")


def q(text: str) -> str:
    return urllib.parse.quote(text, safe="")


# ─────────────────────────── the admin stash ────────────────────────────────

def admins() -> list[tuple[str, pathlib.Path]]:
    if any(p.is_symlink() for p in (ADMIN_STORE, *ADMIN_STORE.parents)):
        raise RuntimeError("Admin store must not contain symbolic links")
    if not ADMIN_STORE.is_dir():
        return []
    return [(p.name, p) for p in sorted(ADMIN_STORE.iterdir())
            if p.is_file() and not p.name.startswith(".")
            and not p.name.endswith(".meta.json")]


def meta(path: pathlib.Path) -> pathlib.Path:
    return path.with_name(path.name + ".meta.json")


def write_secret(path: pathlib.Path, value: str) -> None:
    private_io.check(meta(path))
    private_io.write(path, value + "\n")


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


def read_admin(label: str | None) -> tuple[str, str]:
    """(label, value) for one stashed admin token; the value goes no further
    than the caller's local variable."""
    have = admins()
    if not have:
        raise RuntimeError("no Heroku admin token stashed: run "
                           "`heroku auth:token | python heroku.py stash`")
    if label:
        for name, p in have:
            if name == label:
                return name, private_io.read(p).strip()
        raise RuntimeError(f"no stashed admin token called {label!r} "
                           f"(have: {', '.join(n for n, _ in have)})")
    if len(have) > 1:
        raise RuntimeError(f"{len(have)} admin tokens stashed; name one with "
                           f"--admin: {', '.join(n for n, _ in have)}")
    name, p = have[0]
    return name, private_io.read(p).strip()


def cli_token() -> str:
    """The Heroku CLI's session token, read from the CLI's stdout inside this
    process. Its stderr is withheld: a failure is reported by exit code only."""
    if not shutil.which("heroku"):
        raise RuntimeError("the heroku CLI is not on PATH")
    try:
        p = subprocess.run(["heroku", "auth:token"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"heroku auth:token could not run: {type(exc).__name__}") from None
    if p.returncode != 0:
        raise RuntimeError(f"heroku auth:token exited {p.returncode}; is the CLI logged in?")
    lines = [ln.strip() for ln in p.stdout.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def cmd_stash(value: str, label: str | None) -> int:
    if not value:
        print("nothing on stdin: the pipe was empty, or the CLI is not logged in", file=sys.stderr)
        return 2
    if len(value.split()) > 1:
        print(f"refused: {len(value.split())} whitespace-separated pieces arrived, "
              f"not one token", file=sys.stderr)
        return 1
    try:
        account = _request("GET", "/account", value)
    except HerokuError as exc:
        print(f"refused: the token cannot read its own account ({exc})", file=sys.stderr)
        return 1
    try:
        # The one right this door needs: managing authorizations. A token with a
        # narrow scope reads /account and still cannot issue anything.
        _list("/oauth/authorizations", value)
    except HerokuError as exc:
        print(f"refused: this token cannot list OAuth authorizations ({exc}); the door "
              f"needs a global-scope token such as the CLI session's", file=sys.stderr)
        return 1
    try:
        teams = _list("/teams", value)
    except HerokuError:
        teams = []
    email = (account or {}).get("email") or ""
    name = slug(label or email or (account or {}).get("id", ""))
    dest = ADMIN_STORE / name
    existed = dest.is_file()
    write_secret(dest, value)
    private_io.write(meta(dest), json.dumps({
        "kind": "admin", "account_id": (account or {}).get("id"),
        "email": email, "teams": len(teams), "stashed_on": today(),
        "why": "issues narrow OAuth authorizations and manages teams; never handed out",
    }, ensure_ascii=False, indent=2) + "\n")
    print(f"{'replaced' if existed else 'stashed'}: Heroku admin token as {name!r}, "
          f"sees {len(teams)} team(s). Nothing reads it but this program.")
    return 0


# ─────────────────────────── the ledger ─────────────────────────────────────

def ledger() -> dict:
    private_io.check(LEDGER)
    if not LEDGER.exists():
        return {"issued": {}}
    try:
        doc = json.loads(private_io.read(LEDGER))
    except (OSError, ValueError):
        raise RuntimeError("Issued-authorization ledger is unreadable; refusing to replace it") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("issued"), dict):
        raise RuntimeError("Issued-authorization ledger has an unsupported shape")
    return doc


def save_ledger(doc: dict) -> None:
    write_secret(LEDGER, json.dumps(doc, ensure_ascii=False, indent=2))


#: How long a second door operation waits for the ledger lock before it refuses:
#: long enough for an issue whose proof and vault delivery take their full time.
LOCK_WAIT_S = 150.0
_LOCK_STATE = threading.local()
#: The verbs that read the ledger, call Heroku and write the ledger back.
LOCKED_VERBS = ("issue", "rotate", "revoke", "ping")


@contextlib.contextmanager
def _ledger_lock():
    """One door operation on the ledger at a time, across processes and threads.

    WHY. Each verb reads `heroku-issued.json`, calls Heroku and writes it back.
    Unlocked, two `issue` runs each wrote the copy they had read, so one row was
    lost while both authorizations lived; an `issue` racing a `rotate` left a row
    naming a deleted authorization, and `ping` then called the live one a stray.
    The lock is an exclusive `flock` on a file beside the ledger, held from the
    first read to the last write (the OpenRouter door's `_ledger_lock`), and every
    write still re-reads the ledger and replaces only its own row (`put_row`).
    Re-entrant within a thread; a second operation waits LOCK_WAIT_S, then refuses."""
    depth = getattr(_LOCK_STATE, "depth", 0)
    if depth:
        _LOCK_STATE.depth = depth + 1
        try:
            yield
        finally:
            _LOCK_STATE.depth = depth
        return
    private_io.parent(LEDGER.parent)
    fd = osprivacy.open(LEDGER.parent / ".heroku-ledger.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        give_up = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                oslocks.flock(fd, oslocks.LOCK_EX | oslocks.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= give_up:
                    raise RuntimeError(f"another Heroku door operation ({', '.join(LOCKED_VERBS)}) has held "
                                       f"the ledger lock for {LOCK_WAIT_S:.0f} s; nothing was read or "
                                       f"changed: run again when it is done") from None
                time.sleep(0.05)
        _LOCK_STATE.depth = 1
        try:
            yield
        finally:
            _LOCK_STATE.depth = 0
            oslocks.flock(fd, oslocks.LOCK_UN)
    finally:
        os.close(fd)


def _locked(fn):
    """Run a door verb under `_ledger_lock`; a lock refusal is a refusal, exit 1."""
    import functools

    @functools.wraps(fn)
    def inner(*a, **k):
        try:
            with _ledger_lock():
                return fn(*a, **k)
        except RuntimeError as exc:
            if "ledger lock" not in str(exc):
                raise
            print(f"refused: {exc}", file=sys.stderr)
            return 1
    return inner


def put_row(slot: str, row: dict | None) -> None:
    """Replace ONE slot's row (None deletes it) in the ledger as it is now: read
    again under the lock, so a write never carries a stale copy of other rows."""
    with _ledger_lock():
        doc = ledger()
        if row is None:
            doc["issued"].pop(slot, None)
        else:
            doc["issued"][slot] = row
        save_ledger(doc)


def find_row(doc: dict, wanted: str) -> tuple[str, dict]:
    """A ledger row by its slot or by its authorization id."""
    issued = doc["issued"]
    if wanted in issued:
        return wanted, issued[wanted]
    for slot, row in issued.items():
        if row.get("authorization_id") == wanted:
            return slot, row
    raise RuntimeError(f"no authorization issued by this door for {wanted!r}")


# ─────────────────────────── issue ──────────────────────────────────────────

def parse_vault_target(target: str | None) -> tuple[str, str, str]:
    """PROJECT/ENV/NAME, validated by the vault's own rules before anything is
    minted: an authorization issued for a slot the vault refuses has nowhere to go."""
    parts = (target or "").split("/")
    if len(parts) != 3 or not all(parts):
        raise ValueError("--vault must be PROJECT/ENV/NAME")
    sys.path.insert(0, str(ROOT / "tools"))
    import vault
    try:
        folder, said = vault.project_folder(parts[0], parts[1], parts[2])
        if said:
            print(f"project: {said}", file=sys.stderr)
        vault.validate_names(folder, parts[1], parts[2])
    except vault.VaultBoundaryError as exc:
        raise ValueError(f"--vault {exc}") from None
    return folder, parts[1], parts[2]


def deliver_to_vault(value: str, project: str, env: str, name: str) -> None:
    """Through the vault's own door, value on stdin; the child's output withheld."""
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "vault.py"), "put",
                        project, env, name, "--force"],
                       input=value + "\n", capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"vault refused the slot {project}/{env}/{name} "
                           f"(exit {r.returncode}); child output withheld")


def probe_paths(preset: dict, team: str | None, app: str | None) -> list[tuple[str, str]]:
    """What the new value must read, as (path, what a person reads): every target
    named, the app AND the team's apps when both are, else the user's apps; the
    account for `identity`, whose scope reads no app and no team."""
    if preset["probe"] == "account":
        return [("/account", "the account")]
    out = []
    if app:
        out.append((f"/apps/{q(app)}", f"app {app}"))
    if team:
        out.append((f"/teams/{q(team)}/apps", f"team {team}'s apps"))
    return out or [("/apps", "the user's apps")]


#: Attempts and the pause between them while a fresh value is honoured.
PROOF_ATTEMPTS = 6


def prove(value: str, path: str, wait: float) -> str | None:
    """None once the value reads `path`, else the last reason. A 403 or 404 is
    final: no wait turns a missing right into one."""
    reason = None
    for i in range(PROOF_ATTEMPTS):
        try:
            _request("GET", path, value)
            return None
        except HerokuError as exc:
            reason = str(exc)
            if exc.status in (403, 404):
                return reason
        if i < PROOF_ATTEMPTS - 1:
            time.sleep(min(wait * 2 ** i, wait * 8))
    return reason


def _token_of(auth: dict) -> str:
    tok = ((auth or {}).get("access_token") or {}).get("token")
    if not isinstance(tok, str) or not tok:
        raise HerokuError("shape", "heroku returned the authorization without an access token")
    return tok


def _delete_fresh(admin: str, auth_id: str | None) -> str:
    """An authorization this call created and could not deliver has no holder:
    it is deleted again before the refusal is printed."""
    if not auth_id:
        return ""
    try:
        _request("DELETE", f"/oauth/authorizations/{q(auth_id)}", admin)
    except HerokuError as exc:
        return (f"; the authorization this call created ({auth_id}) could NOT be deleted "
                f"({exc}): revoke it with `heroku authorizations:revoke {auth_id}`")
    return f"; the authorization this call created ({auth_id}) was deleted again"


@_locked
def cmd_issue(preset_key: str, target: str | None, team: str | None, app: str | None,
              admin_label: str | None, expire_days: int | None, wait: float = 2.0) -> int:
    preset = PRESETS.get(preset_key)
    if not preset:
        print(f"unknown preset {preset_key!r}; have: {', '.join(PRESETS)}", file=sys.stderr)
        return 2
    if not target:
        print("refused: --vault PROJECT/ENV/NAME is required; a Heroku credential goes "
              "to a vault slot", file=sys.stderr)
        return 2
    if preset.get("needs_app") and not app:
        print(f"refused: preset {preset_key!r} reaches config vars and needs --app, the one "
              f"app it is for, so the description and the proof name it", file=sys.stderr)
        return 2
    if expire_days is not None and not 1 <= expire_days <= 3650:
        print("refused: --expire-days must be between 1 and 3650", file=sys.stderr)
        return 2
    try:
        project, env, name = parse_vault_target(target)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    slot = f"{project}/{env}/{name}"
    where = (f" team {team}" if team else "") + (f" app {app}" if app else "")
    description = DESCRIPTION.format(preset=preset_key, slot=slot, where=where)
    scopes = list(preset["scopes"])
    admin, fresh, rolled = None, None, False
    try:
        label, admin = read_admin(admin_label)
        doc = ledger()
        if team:
            # The team must exist for this admin before a credential names it.
            _request("GET", f"/teams/{q(team)}", admin)
        if app:
            _request("GET", f"/apps/{q(app)}", admin)
        prior = doc["issued"].get(slot)
        existing = None
        for auth in _list("/oauth/authorizations", admin):
            if auth.get("description") == description:
                existing = auth
                break
        if existing and sorted(existing.get("scope") or []) == sorted(scopes):
            # The same purpose, the same scopes: regenerate in place, so the id and
            # every record of it stay put.
            auth = _request("POST", f"/oauth/authorizations/{q(existing['id'])}"
                                    "/actions/regenerate-tokens", admin)
            rolled = True
        else:
            body = {"scope": scopes, "description": description}
            if expire_days is not None:
                body["expires_in"] = expire_days * 86400
            auth = _request("POST", "/oauth/authorizations", admin, body)
            fresh = (auth or {}).get("id")
        auth_id = (auth or {}).get("id") or (existing or {}).get("id")
        value = _token_of(auth)
        proved = []
        for path, what in probe_paths(preset, team, app):
            why = prove(value, path, wait)
            if why is not None:
                raise HerokuError("proof", f"the issued authorization cannot read {what} ({why})")
            proved.append(what)
        deliver_to_vault(value, project, env, name)
    except (HerokuError, RuntimeError) as exc:
        tail = _delete_fresh(admin, fresh) if admin else ""
        if rolled:
            tail = "; the authorization was regenerated, so the value the slot held is dead: issue again"
        print(f"refused: {exc}{tail}", file=sys.stderr)
        return 1
    # A changed preset (other scopes, another team or app) made a new
    # authorization; the slot's old one is nobody's now, and is deleted only once
    # the new value is delivered, so the slot is never left without a live value.
    retired = ""
    for old in sorted({(prior or {}).get("authorization_id"), (existing or {}).get("id")}
                      - {None, auth_id}):
        try:
            _request("DELETE", f"/oauth/authorizations/{q(old)}", admin)
            retired += f"; the slot's previous authorization {old} was deleted"
        except HerokuError as exc:
            if exc.status != 404:
                retired += (f"; the slot's previous authorization {old} could NOT be deleted "
                            f"({exc}): `revoke {old}`")
    put_row(slot, {
        "authorization_id": auth_id, "preset": preset_key, "scopes": scopes,
        "description": description, "team": team, "app": app, "admin": label,
        "expires_in": ((auth or {}).get("access_token") or {}).get("expires_in"),
        "issued_on": (prior or {}).get("issued_on") or today(),
        "rotated_on": today() if rolled else (prior or {}).get("rotated_on"),
        "rotations": ((prior or {}).get("rotations") or 0) + (1 if rolled else 0),
        "issued_by": "tools/heroku.py",
    })
    _journal("rotate" if rolled else "issue", slot, preset=preset_key,
             authorization=auth_id, team=team, app=app)
    print(f"  {slot}: {'regenerated' if rolled else 'issued'} authorization {auth_id}, "
          f"scope {', '.join(scopes)}{retired}")
    # Exactly what was proved, and nothing more: an `identity` value reads the
    # account and no team, so a team named beside it is a stated purpose only.
    print(f"  proved with its own value before delivery: it read {', '.join(proved)}")
    unproved = [w for w, given in ((f"team {team}", team), (f"app {app}", app))
                if given and not any(w in p for p in proved)]
    print("  Heroku cannot narrow an authorization to a team or an app: the scope is the grant, "
          "and it reaches every app this user reaches"
          + (f"; {' and '.join(unproved)} exist for the admin but this scope cannot read them"
             if unproved else ""))
    print(f"  use: python \"$(project-observatory full-path)/tools/use_secret.py\" run "
          f"--env {env} {project} {name} -- <command>")
    return 0


# ─────────────────────────── list, ping, rotate, revoke ─────────────────────

def cmd_list() -> int:
    """Names, ids, scopes and dates. Never a value."""
    try:
        a, doc = admins(), ledger()
    except RuntimeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"admin tokens ({len(a)}), read by this program only:")
    for label, p in a:
        m = read_meta(p)
        print(f"  {label:28s} stashed {m.get('stashed_on', '?')}  mode {oct(p.stat().st_mode)[-3:]}"
              f"  teams {m.get('teams', '?')}")
    rows = doc["issued"]
    print(f"issued authorizations ({len(rows)}); each is the user's, narrowed by scope only:")
    for slot, r in sorted(rows.items()):
        print(f"  {slot:36s} {r.get('preset', '?'):12s} {','.join(r.get('scopes') or []):28s} "
              f"{('team ' + r['team']) if r.get('team') else '':16s} {('app ' + r['app']) if r.get('app') else '':16s} "
              f"issued {r.get('issued_on', '?')} rotated {r.get('rotated_on') or 'never'} "
              f"x{r.get('rotations', 0)}  id {r.get('authorization_id')}")
    if not a:
        print("\nstart with: heroku auth:token | python heroku.py stash")
    return 0


@_locked
def cmd_ping() -> int:
    """Is every admin alive, and does every ledger row still exist at Heroku with
    the scopes it was issued with? No value is printed or read from the vault."""
    bad = 0
    try:
        have, doc = admins(), ledger()
    except RuntimeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    live: dict[str, dict] = {}
    for label, path in have:
        try:
            value = private_io.read(path).strip()
            _request("GET", "/account", value)
            mine = _list("/oauth/authorizations", value)
            for auth in mine:
                live[auth.get("id")] = auth
            print(f"  admin/{label}: alive, {len(mine)} authorization(s) at Heroku")
        except HerokuError as exc:
            print(f"  admin/{label}: DEAD: {exc}", file=sys.stderr)
            bad += 1
    if have and not bad:
        for slot, row in sorted(doc["issued"].items()):
            auth = live.get(row.get("authorization_id"))
            if not auth:
                print(f"  {slot}: GONE at Heroku (authorization {row.get('authorization_id')}); "
                      f"the slot holds a dead value: issue again", file=sys.stderr)
                bad += 1
            elif sorted(auth.get("scope") or []) != sorted(row.get("scopes") or []):
                print(f"  {slot}: scope at Heroku is {auth.get('scope')}, the ledger says "
                      f"{row.get('scopes')}", file=sys.stderr)
                bad += 1
            else:
                print(f"  {slot}: alive, scope {','.join(row.get('scopes') or [])} "
                      f"(last change {row.get('rotated_on') or row.get('issued_on')})")
        managed = {r.get("authorization_id") for r in doc["issued"].values()}
        strays = [a for i, a in live.items() if i not in managed
                  and str(a.get("description") or "").endswith("(managed)")]
        for s in strays:
            print(f"  stray: authorization {s.get('id')} is described as managed but no ledger "
                  f"row names it; `revoke {s.get('id')}` after checking who uses it")
    if not have:
        print("nothing stashed")
    return 1 if bad else 0


def leaked_slots(doc: dict) -> set[str]:
    """Ledger slots named in an OPEN leak row, read through the vault's register."""
    sys.path.insert(0, str(ROOT / "tools"))
    import vault
    out: set[str] = set()
    try:
        for row in vault.open_leaks():
            blob = json.dumps(row, ensure_ascii=False)
            for slot, r in doc["issued"].items():
                if slot in blob or (r.get("authorization_id") or "\0") in blob:
                    out.add(slot)
    except Exception as exc:
        print(f"  (the leak register could not be read: {type(exc).__name__})", file=sys.stderr)
    return out


@_locked
def cmd_rotate(wanted: str | None, leaked: bool, wait: float = 2.0) -> int:
    try:
        doc = ledger()
        if leaked:
            slots = sorted(leaked_slots(doc))
            if not slots:
                print("no open leak names an authorization this door issued; nothing to rotate")
                return 0
        elif wanted:
            slots = [find_row(doc, wanted)[0]]
        else:
            print("name a slot or an authorization id, or pass --leaked", file=sys.stderr)
            return 2
    except RuntimeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    bad = 0
    for slot in slots:
        row = doc["issued"][slot]
        try:
            _label, admin = read_admin(row.get("admin"))
            auth = _request("POST", f"/oauth/authorizations/{q(row['authorization_id'])}"
                                    "/actions/regenerate-tokens", admin)
        except (HerokuError, RuntimeError, KeyError) as exc:
            print(f"  {slot}: could not regenerate: {exc}", file=sys.stderr)
            bad += 1
            continue
        try:
            value = _token_of(auth)
            for path, what in probe_paths(PRESETS.get(row.get("preset"), PRESETS["read"]),
                                          row.get("team"), row.get("app")):
                why = prove(value, path, wait)
                if why is not None:
                    raise HerokuError("proof", f"the regenerated value cannot read {what} ({why})")
            project, env, name = slot.split("/")
            deliver_to_vault(value, project, env, name)
        except (HerokuError, RuntimeError, ValueError) as exc:
            print(f"  {slot}: REGENERATED but not delivered: {exc}; the old value is dead and the "
                  f"slot holds it: run rotate again", file=sys.stderr)
            bad += 1
            continue
        row["rotated_on"] = today()
        row["rotations"] = (row.get("rotations") or 0) + 1
        put_row(slot, row)
        _journal("rotate", slot, authorization=row.get("authorization_id"))
        print(f"  {slot}: regenerated in place and delivered (authorization {row.get('authorization_id')})")
    if leaked and not bad:
        print(SETTLE_HINT)
    return 1 if bad else 0


@_locked
def cmd_revoke(wanted: str) -> int:
    try:
        doc = ledger()
        try:
            slot, row = find_row(doc, wanted)
            auth_id, admin_label = row.get("authorization_id"), row.get("admin")
        except RuntimeError:
            # A stray `ping` named: an id with no ledger row, deleted by id.
            if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", wanted):
                raise
            slot, row, auth_id, admin_label = None, None, wanted, None
        _l, admin = read_admin(admin_label)
        try:
            _request("DELETE", f"/oauth/authorizations/{q(auth_id)}", admin)
        except HerokuError as exc:
            if exc.status != 404:
                raise
    except (HerokuError, RuntimeError) as exc:
        print(f"refused: {exc}\n  the ledger row was NOT removed: a local delete that leaves the "
              f"authorization live at Heroku is the worst of both", file=sys.stderr)
        return 1
    if slot:
        put_row(slot, None)
    _journal("revoke", slot or f"heroku/authorization/{auth_id}", authorization=auth_id)
    print(f"revoked at Heroku: authorization {auth_id}"
          + (f"; slot {slot} now holds a dead value and can be removed from the vault" if slot else ""))
    return 0


# ─────────────────────────── teams ──────────────────────────────────────────

def _slots_by(doc: dict, field: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for slot, row in sorted(doc["issued"].items()):
        if row.get(field):
            out.setdefault(row[field], []).append(slot)
    return out


def cmd_teams(admin_label: str | None) -> int:
    """Every team the admin belongs to: name, id, role, app count, and the vault
    slots this door issued for it."""
    try:
        _l, admin = read_admin(admin_label)
        doc = ledger()
        teams = _list("/teams", admin)
    except (HerokuError, RuntimeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    slots = _slots_by(doc, "team")
    bad = 0
    print(f"teams ({len(teams)}):")
    for t in sorted(teams, key=lambda t: t.get("name") or ""):
        try:
            apps = str(len(_list(f"/teams/{q(t.get('id') or t.get('name'))}/apps", admin)))
        except HerokuError as exc:
            apps, bad = f"unread ({exc.status})", bad + 1
        print(f"  {t.get('name', '?'):28s} {t.get('id', '?'):38s} role {t.get('role') or '?':13s} "
              f"apps {apps:12s} slots {', '.join(slots.get(t.get('name'), [])) or 'none'}")
    return 1 if bad else 0


def team_invites_by_email(team: str, admin: str) -> tuple[bool, list[dict]]:
    """(does this team invite rather than add, its pending invitations).

    Mirrors heroku-cli 10.16 (commands/members/add.js, remove.js, index.js): a
    team of type `team` with the `team-invite-acceptance` feature enabled takes
    new people by invitation, which they must accept, and lists the pending ones
    under `/teams/{t}/invitations` with their own Accept variant. Any other team
    adds members directly. A feature list that cannot be read is an error, never
    a guess: a guess picks the wrong endpoint for the write."""
    info = _request("GET", f"/teams/{q(team)}", admin) or {}
    if info.get("type") != "team":
        return False, []
    features = _list(f"/teams/{q(team)}/features", admin)
    if not any(f.get("name") == INVITE_FEATURE and f.get("enabled") for f in features):
        return False, []
    rows = _list(f"/teams/{q(team)}/invitations", admin, accept=ACCEPT_INVITATIONS)
    return True, [{"email": str((r.get("user") or {}).get("email") or ""),
                   "role": r.get("role"), "id": r.get("id")} for r in rows]


def cmd_members(team: str, admin_label: str | None) -> int:
    try:
        _l, admin = read_admin(admin_label)
        members = _list(f"/teams/{q(team)}/members", admin)
        invite_mode, invites = team_invites_by_email(team, admin)
    except (HerokuError, RuntimeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"members of {team} ({len(members)}"
          + (f", and {len(invites)} pending invitation(s); this team invites" if invite_mode else "")
          + "):")
    rows = [(str(m.get("email") or "?"), m.get("role") or "?",
             f"2fa {'on' if m.get('two_factor_authentication') else 'off'}"
             + ("  federated" if m.get("federated") else "")) for m in members]
    rows += [(i["email"] or "?", i.get("role") or "?", "pending invitation") for i in invites]
    for email, role, note in sorted(rows):
        print(f"  {email:40s} {role:13s} {note}")
    return 0


def cmd_apps(team: str, admin_label: str | None) -> int:
    try:
        _l, admin = read_admin(admin_label)
        doc = ledger()
        apps = _list(f"/teams/{q(team)}/apps", admin)
    except (HerokuError, RuntimeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    slots = _slots_by(doc, "app")
    print(f"apps of {team} ({len(apps)}):")
    for a in sorted(apps, key=lambda a: a.get("name") or ""):
        print(f"  {a.get('name', '?'):32s} {((a.get('region') or {}).get('name') or '?'):6s} "
              f"{'locked' if a.get('locked') else 'open':7s} updated {str(a.get('updated_at') or '?')[:10]}"
              f"  slots {', '.join(slots.get(a.get('name'), [])) or 'none'}")
    return 0


def plan_membership(action: str, members: list[dict], email: str, role: str | None,
                    me: str | None, invites: list[dict] | None = None,
                    invite_mode: bool = False) -> tuple[str, str | None]:
    """(what would happen, refusal or None). Pure: read from the team as it is,
    its members and, on a team that invites, its pending invitations."""
    email = email.strip().lower()
    by = {str(m.get("email") or "").lower(): m for m in members}
    pending = {str(i.get("email") or "").lower(): i for i in invites or []}
    current = (by.get(email) or {}).get("role")
    invited = (pending.get(email) or {}).get("role") if email in pending else None
    # Who can still manage the team: admins and the owner. A pending invitee
    # manages nothing until they accept, so is never counted.
    admins_now = [e for e, m in by.items() if m.get("role") in ("admin", "owner")]
    if action in ("add", "role"):
        if role not in ROLES:
            return "", f"role must be one of {', '.join(ROLES)}; `owner` is a transfer, not a membership"
    if me and email == me.lower() and action != "add":
        return "", "that is the stashed admin's own membership; change it in the team's settings"
    if email in pending and not current:
        if action == "add":
            if invited == role:
                return f"no change: an invitation for {email} as {role} is pending", None
            return "", (f"{email} already has a pending invitation as {invited}; cancel it with "
                        f"`member remove` and invite again")
        if action == "role":
            return "", f"{email} has a pending invitation, not a membership; cancel it and invite again"
        return f"cancel the pending invitation for {email} (invited as {invited})", None
    if action == "add":
        if current == role:
            return f"no change: {email} is already {role}", None
        if current:
            return "", f"{email} is already {current}; use `member role` to change it"
        if invite_mode:
            return f"invite {email} as {role}; they become a member once they accept", None
        return f"add {email} as {role}", None
    if not current:
        return "", f"{email} is not a member of this team and has no pending invitation"
    if current == "owner":
        return "", f"{email} owns the team; ownership is not changed by this door"
    losing_admin = current == "admin" and (action == "remove" or role != "admin")
    if losing_admin and len(admins_now) <= 1:
        return "", f"{email} is the last admin; the team would have nobody to manage it"
    if action == "role":
        if current == role:
            return f"no change: {email} is already {role}", None
        return f"change {email} from {current} to {role}", None
    return f"remove {email} (now {current})", None


def cmd_member(action: str, team: str, email: str, role: str | None,
               admin_label: str | None, apply: bool) -> int:
    """One membership change, the CLI's way: a team that invites gets an
    invitation (and `remove` cancels a pending one); any other team is written
    directly. A plan until --apply; then one call and a fresh read that must show
    the result: the members list, or for an invitation the invitations list."""
    key = email.strip().lower()
    try:
        label, admin = read_admin(admin_label)
        me = read_meta(ADMIN_STORE / label).get("email") or (
            _request("GET", "/account", admin) or {}).get("email")
        members = _list(f"/teams/{q(team)}/members", admin)
        invite_mode, invites = team_invites_by_email(team, admin)
    except (HerokuError, RuntimeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    plan, refusal = plan_membership(action, members, email, role, me, invites, invite_mode)
    if refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 1
    print(f"plan for team {team}: {plan}")
    if plan.startswith("no change"):
        return 0
    if not apply:
        print("dry run: nothing changed; repeat with --apply to make this change")
        return 0
    pending = next((i for i in invites if i["email"].lower() == key), None)
    is_member = any(str(m.get("email") or "").lower() == key for m in members)
    via = "invitation" if (action == "add" and invite_mode) or (
        action == "remove" and pending and not is_member) else "membership"
    try:
        if via == "invitation" and action == "add":
            _request("PUT", f"/teams/{q(team)}/invitations", admin,
                     {"email": email, "role": role}, accept=ACCEPT_INVITATIONS)
        elif via == "invitation":
            _request("DELETE", f"/teams/{q(team)}/invitations/{q(pending.get('id') or email)}",
                     admin, accept=ACCEPT_INVITATIONS)
        elif action == "add":
            _request("PUT", f"/teams/{q(team)}/members", admin, {"email": email, "role": role})
        elif action == "role":
            _request("PATCH", f"/teams/{q(team)}/members", admin, {"email": email, "role": role})
        else:
            _request("DELETE", f"/teams/{q(team)}/members/{q(email)}", admin)
        if via == "invitation":
            after = {i["email"].lower(): i.get("role") for i in team_invites_by_email(team, admin)[1]}
        else:
            after = {str(m.get("email") or "").lower(): m.get("role")
                     for m in _list(f"/teams/{q(team)}/members", admin)}
    except HerokuError as exc:
        print(f"refused: {exc}; read the team with `members --team {team}` before repeating, "
              f"the change may or may not have happened", file=sys.stderr)
        return 1
    want = None if action == "remove" else role
    got = after.get(key)
    if got != want:
        where = "pending invitations now read" if via == "invitation" else "team now reads"
        print(f"applied, but the {where} {email} as {got or 'absent'}, not "
              f"{want or 'absent'}", file=sys.stderr)
        return 1
    _journal("membership", f"heroku/team/{team}", action=action, member=email, role=want, via=via)
    print(f"applied and verified: {plan}")
    return 0


# ─────────────────────────── command line ───────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("stash", help="admin token on stdin (or --from-cli); stored, never handed out")
    p.add_argument("--from-cli", action="store_true",
                   help="read `heroku auth:token` inside this program instead of stdin")
    p.add_argument("--label", help="the stash's name; default the account's email as a slug")
    p = sub.add_parser("issue", help="mint a narrow OAuth authorization into a vault slot")
    p.add_argument("--preset", required=True, choices=sorted(PRESETS))
    p.add_argument("--vault", help="the slot PROJECT/ENV/NAME it is delivered to")
    p.add_argument("--team", help="the team it is for: named in its description, read by its proof")
    p.add_argument("--app", help="the app it is for: named in its description, read by its proof")
    p.add_argument("--admin", help="which stashed admin token to issue from")
    p.add_argument("--expire-days", type=int, default=None,
                   help="expire the authorization after this many days (default: Heroku's, none)")
    sub.add_parser("list", help="what exists, with dates; never values")
    sub.add_parser("ping", help="is every admin alive and every issued authorization still there")
    p = sub.add_parser("rotate", help="regenerate an authorization's value in place")
    p.add_argument("target", nargs="?", help="PROJECT/ENV/NAME or an authorization id")
    p.add_argument("--leaked", action="store_true", help="every slot named in an open leak")
    p = sub.add_parser("revoke", help="delete at Heroku, then the ledger row")
    p.add_argument("target", help="PROJECT/ENV/NAME or an authorization id")
    p = sub.add_parser("teams", help="teams: name, id, role, apps, slots")
    p.add_argument("--admin")
    for verb in ("members", "apps"):
        p = sub.add_parser(verb, help=f"a team's {verb}")
        p.add_argument("--team", required=True)
        p.add_argument("--admin")
    p = sub.add_parser("member", help="add, re-role or remove a team member (a plan until --apply)")
    p.add_argument("action", choices=("add", "role", "remove"))
    p.add_argument("--team", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--role", choices=ROLES)
    p.add_argument("--admin")
    p.add_argument("--apply", action="store_true", help="make the change; without it, print the plan")
    a = ap.parse_args()
    if a.cmd == "stash":
        if a.from_cli:
            try:
                return cmd_stash(cli_token(), a.label)
            except RuntimeError as exc:
                print(f"refused: {exc}", file=sys.stderr)
                return 1
        if sys.stdin.isatty():
            print("pipe the admin token on stdin: `heroku auth:token | python heroku.py stash`, "
                  "or use --from-cli", file=sys.stderr)
            return 2
        return cmd_stash(sys.stdin.read().strip(), a.label)
    if a.cmd == "issue":
        return cmd_issue(a.preset, a.vault, a.team, a.app, a.admin, a.expire_days)
    if a.cmd == "list":
        return cmd_list()
    if a.cmd == "ping":
        return cmd_ping()
    if a.cmd == "rotate":
        return cmd_rotate(a.target, a.leaked)
    if a.cmd == "revoke":
        return cmd_revoke(a.target)
    if a.cmd == "teams":
        return cmd_teams(a.admin)
    if a.cmd == "members":
        return cmd_members(a.team, a.admin)
    if a.cmd == "apps":
        return cmd_apps(a.team, a.admin)
    if a.cmd == "member":
        if a.action in ("add", "role") and not a.role:
            print(f"refused: `member {a.action}` needs --role", file=sys.stderr)
            return 2
        return cmd_member(a.action, a.team, a.email, a.role, a.admin, a.apply)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError):
        print("Private credential operation refused; values hidden", file=sys.stderr)
        raise SystemExit(2)
