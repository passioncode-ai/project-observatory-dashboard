#!/usr/bin/env python3
"""DigitalOcean door: team tokens, narrow tokens and database credentials, never in the transcript.

Two halves. The ACCOUNT half (OBS-155) keeps one token per team and the narrow tokens
installed from the control panel, the way `tools/cloudflare.py` keeps Cloudflare's:

  digitalocean.py stash < token-file                # a team's own token, on stdin, once per team
  digitalocean.py stash --from-doctl CONTEXT|all    # or read from doctl's config; nothing printed
  digitalocean.py teams [--probe]                   # teams, roles, which vault slots belong to each
  digitalocean.py members --team T                  # role and member count; the API lists no members
  digitalocean.py team create NAME --org T [--invite EMAIL:ROLE ...] [--apply]   # plans by default
  digitalocean.py presets                           # the custom scopes each preset is minted with
  digitalocean.py issue --preset P --team T --vault PROJECT/ENV/NAME   # refused: prints the recipe
  pbpaste | digitalocean.py install --preset P --team T --vault PROJECT/ENV/NAME
  digitalocean.py list | ping
  pbpaste | digitalocean.py rotate LABEL            # new value in, old one revoked and proved dead
  digitalocean.py revoke LABEL                      # revoked and proved dead, then removed here

DigitalOcean's public API cannot mint, roll or list personal access tokens, nor list,
invite, remove or re-role team members (measured 2026-10-19; see the account half below).
So `issue` names the control-panel step and `install` takes what it minted; a revocation
counts only when `/v2/account` then answers 401 to the old value.

The DATABASE half: managed-database credentials go to the vault, never the transcript.

Why a door. A call of the DigitalOcean MCP tool `db-cluster-list` asked only for
names can return every cluster's passwords: DigitalOcean's API puts a password in
every cluster and user object it returns, so anything that prints the API's answer
leaks. This
door is the one place that reads those objects: it keeps the password in memory,
delivers it to the vault on stdin, and prints names, ids and phases.

  digitalocean.py db list                         # clusters, engines, exposure — no values
  digitalocean.py db consumers CLUSTER            # App Platform apps that use it, and how
  digitalocean.py db credential CLUSTER USER --db NAME --to vault:<project>/<env>/<NAME>
  digitalocean.py db rotate CLUSTER USER --db NAME --to vault:<project>/<env>/<NAME>
        [--app APP_ID ...] [--spec-env APP_ID:KEY ...] [--wait 900]

`rotate` moves every consumer you NAME (`db consumers` lists the App Platform ones;
services started with `use_secret.py serve` from the slot are named for restart by
`vault.py rotate`, whose list this prints). Order:
  1. read everything it will need BEFORE the reset — the doctl team, the cluster,
     every consumer spec, their current deployments — and ask the vault whether the
     slot exists (put or rotate) and can be written; any doubt stops with nothing
     changed;
  2. reset the user's password (the one step not retried: a second reset would
     orphan the first value);
  3. deliver the new connection URL to the vault; if the vault refuses now, say the
     password WAS reset and keep moving the apps, so none sits on a dead password;
  4. write each proven `--spec-env` literal: a plain value must equal the selected
     user's current provider password, or be a database URL with that password,
     user, engine and provider connection host/port. Names are not proof. Opaque
     encrypted literals and unverified pool endpoints are refused before reset;
     migrate those to provider database bindings. `apps update` redeploys a spec;
  5. redeploy every other `--app` (a binding is resolved at deploy time);
  6. wait for each new deployment to be ACTIVE and report.
A private receipt under store/digitalocean-rotations is written before reset with
an unknown outcome. A missing, malformed or failed response remains unknown (exit
5): read the current credential and reconcile consumers, never repeat reset.
Receipt update failure is exit 6; its earlier unknown record remains. Consumers
are down between the reset and ACTIVE; completion is not application health proof.
Only read calls retry; reset, app updates and redeploys each make one attempt.
An app's lost response stays unknown in its receipt; read current spec/deployments
before any retry. App Platform answers 503 now and then, and
a rotation that stops between steps 2 and 5 leaves a live app on a dead password.

Credential: `doctl`'s own authenticated context (the operator's); `rotate` prints the
context and team before it resets anything. `DOCTL` overrides the binary (tests). Values: never printed, never in argv; specs go through a 0600
temporary file only for `doctl apps update`; cleanup is attempted in `finally` and
a failure is reported without stopping independent consumers.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import vault as private_files  # noqa: E402  — the slot's own rules: names, symlinks, the store
DOCTL = os.environ.get("DOCTL", "doctl")
RETRY_DELAY = float(os.environ.get("OBSERVATORY_DO_RETRY_DELAY", "8"))
POLL = float(os.environ.get("OBSERVATORY_DO_POLL", "15"))


class DoorError(RuntimeError):
    pass


class NotJson(DoorError):
    """doctl answered rc 0 with something that is not JSON (an HTML error page, a
    banner). Before the reset it is a refusal like any other; after it, the phase
    decides the exit code."""


def doctl(*args: str, tries: int = 6) -> object:
    """Bounded JSON call. Provider stderr is untrusted and never echoed."""
    # A nonzero response may follow a committed mutation. Only reads retry.
    if args[:3] == ("databases", "user", "reset") or args[:2] in (
            ("apps", "update"), ("apps", "create-deployment")):
        tries = 1
    last = ""
    for attempt in range(tries):
        try:
            p = subprocess.run([DOCTL, *args, "--output", "json"], capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            raise DoorError("doctl not found; install it and run `doctl auth init`") from None
        except (OSError, subprocess.SubprocessError) as exc:
            raise DoorError(f"doctl transport failed ({type(exc).__name__}); result unavailable") from None
        if p.returncode == 0:
            try:
                return json.loads(p.stdout) if p.stdout.strip() else None
            except ValueError:
                raise NotJson(f"doctl {' '.join(args[:3])} answered with something that is not JSON") from None
        last = f"exit {p.returncode}; provider diagnostics withheld"
        if attempt + 1 < tries:
            time.sleep(RETRY_DELAY)
    raise DoorError(f"doctl {' '.join(args[:3])} failed after {tries} tries: {last}")


def one(x):
    return (x[0] if len(x) == 1 else None) if isinstance(x, list) else x


def cluster(ref: str) -> dict:
    for c in doctl("databases", "list") or []:
        if ref in (c.get("id"), c.get("name")):
            return c
    raise DoorError(f"no cluster named or numbered {ref!r} in this doctl context")


def connection_url(c: dict, user: str, password: str, db: str | None) -> str:
    conn = c.get("connection") or {}
    engine = c.get("engine")
    q = urllib.parse.quote
    if engine in ("redis", "valkey"):
        return f"rediss://{q(user)}:{q(password, safe='')}@{conn['host']}:{conn['port']}"
    scheme = {"pg": "postgresql", "mysql": "mysql"}.get(engine, engine)
    tail = "?sslmode=require" if engine == "pg" else ("?ssl-mode=REQUIRED" if engine == "mysql" else "")
    return f"{scheme}://{q(user)}:{q(password, safe='')}@{conn['host']}:{conn['port']}/{db or 'defaultdb'}{tail}"


def vault_target(to: str) -> list[str]:
    if not to.startswith("vault:") or len(to[6:].split("/")) != 3:
        raise DoorError("--to must be vault:<project>/<env>/<NAME>; values are never accepted")
    return to[6:].split("/")


def plan(to: str) -> dict:
    """Where the value goes and how, decided BEFORE anything changes at the provider.

    THE SLOT IS ASKED FOR, NOT SEARCHED FOR. `vault.py list`'s text was searched
    for `project/env/NAME`, so `DATABASE_URL_POOL` made a missing `DATABASE_URL`
    look present, `rotate` was chosen, and the vault refused it — after the
    password had been reset. The vault's own `_slot` names the one file, with
    its name and symlink rules, and its folder must be writable now.
    """
    project, env, name = vault_target(to)
    try:
        slot = private_files._slot(project, env, name)
    except private_files.VaultBoundaryError as e:
        raise DoorError(f"the vault refuses vault:{project}/{env}/{name}: {e}") from None
    exists = slot.is_file()
    folder = slot.parent
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    if not os.access(folder, os.W_OK | os.X_OK) or (exists and not os.access(slot, os.W_OK)):
        raise DoorError(f"the vault cannot write vault:{project}/{env}/{name} (its folder is not "
                        f"writable by this user)")
    held = None
    if exists:
        # READ IT NOW, through the vault's own reader: a slot readable beyond its
        # owner, or one that will not read, is refused here rather than after the
        # reset, where the same error stranded every consumer (review round 2).
        held = _read_slot(slot, f"{project}/{env}/{name}")
    return {"project": project, "env": env, "name": name, "slot": slot,
            "verb": "rotate" if exists else "put", "held": held}


def _read_slot(slot: pathlib.Path, target: str) -> str:
    try:
        return private_files._read_private(slot).strip()
    except (private_files.VaultBoundaryError, OSError) as e:
        raise DoorError(f"the vault will not read vault:{target} ({type(e).__name__})") from None


def deliver(value: str, where: dict) -> str:
    """Into the vault on stdin: `rotate` when the slot exists, so the old value is archived.

    Returns what the vault said, which for `rotate` includes the services started
    from the slot that still hold the old value — the list a person restarts."""
    target = f"{where['project']}/{where['env']}/{where['name']}"
    if where["verb"] == "rotate" and _read_slot(where["slot"], target) == value:
        return f"vault {target}: unchanged (it already holds this value)"
    vault = [sys.executable, str(ROOT / "tools" / "vault.py")]
    try:
        p = subprocess.run([*vault, where["verb"], where["project"], where["env"], where["name"]],
                           input=value, capture_output=True, text=True)
    except OSError as e:
        raise DoorError(f"vault {where['verb']} {target} could not run: {type(e).__name__}") from None
    if p.returncode != 0:
        raise DoorError(f"vault {where['verb']} {target} refused (exit {p.returncode}; diagnostics withheld)")
    said = [f"  {line}" for line in p.stdout.rstrip().splitlines()]
    return "\n".join([f"vault {where['verb']} {target}", *said])


def exposure(cluster_id: str) -> str:
    rules = doctl("databases", "firewalls", "list", cluster_id) or []
    return "OPEN to the internet (no trusted sources)" if not rules else \
        "trusted: " + ", ".join(f"{r.get('type')}:{str(r.get('value'))[:8]}" for r in rules)


# ── commands ───────────────────────────────────────────────────────────────────

def cmd_list() -> int:
    for c in doctl("databases", "list") or []:
        users = [u.get("name") for u in c.get("users") or []]
        print(f"{c['name']:30} {c['id']} {c['engine']} {c.get('region')} users={users} "
              f"{exposure(c['id'])}")
    return 0


#: Keys that hold a connection URL rather than a bare password.
URL_KEYS = ("DATABASE_URL", "REDIS_URL", "_URL", "_DSN", "_URI")
#: Keys that hold a database credential an app may keep in its own spec.
CREDENTIAL_KEYS = ("PASSWORD", "DB_PASS", "DATABASE_URL", "REDIS_URL", "DB_URL", "POSTGRES_URL")
#: `*_DSN` and `*_URI` are any service's address (`SENTRY_DSN`): a database
#: literal only when its plain value names a database scheme.
DB_SCHEMES = ("postgres://", "postgresql://", "mysql://", "redis://", "rediss://", "mongodb://",
              "mongodb+srv://", "valkey://")


def credential_literal(key: str, value: str) -> bool:
    k = key.upper()
    if any(s in k for s in CREDENTIAL_KEYS):
        return True
    # An encrypted value cannot show its scheme, so the name decides: a database
    # word and an address word, each a whole `_`-separated part, anywhere in the
    # key (review rounds 4 and 5) — `APP_DATABASE_DSN`, `PRIMARY_DB_URI`. Parts,
    # not substrings: `DBT_URI` and `DBX_DSN` are not databases, and `SENTRY_DSN`
    # or `OTEL_…_URI` name no database at all.
    parts = set(k.split("_"))
    if parts & DB_NAME_PARTS and parts & ADDRESS_PARTS:
        return True
    return k.endswith(("_DSN", "_URI")) and value.lower().startswith(DB_SCHEMES)


#: Name parts that say "a database's": one of them beside an address part.
DB_NAME_PARTS = frozenset({"DATABASE", "DB", "PG", "POSTGRES", "POSTGRESQL", "MYSQL", "REDIS",
                           "VALKEY", "MONGO", "MONGODB"})
#: Name parts that say "an address": a connection URL, URI or DSN.
ADDRESS_PARTS = frozenset({"URL", "URI", "DSN"})


def literal_form(key: str, value: str) -> str:
    """`url` when the app keeps a connection URL under KEY, else `password`.

    A SECRET's value is encrypted in the spec (`EV[…]`), so the name decides
    unless the value is plain and itself a URL. Writing a bare password where
    the app expects `postgresql://…` breaks the app as surely as the old one."""
    if key.upper().endswith(URL_KEYS) or any(k in key.upper() for k in URL_KEYS[:2]) or "://" in value:
        return "url"
    return "password"


def with_password(url: str, password: str) -> str:
    """The app's OWN URL with only the password replaced: its user, host, port
    (a pool listens on its own), path and query are kept (review round 2)."""
    parts = urllib.parse.urlsplit(url)
    if not parts.username or "@" not in parts.netloc:
        raise DoorError("a URL literal carries no user@ to put the password beside")
    host = parts.netloc.rsplit("@", 1)[1]
    netloc = f"{parts.username}:{urllib.parse.quote(password, safe='')}@{host}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def url_place(url: str) -> str:
    """host:port/db of a URL — where it points, never who or with what."""
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.rsplit("@", 1)[-1]
    return f"{host}{parts.path or ''}"


def consumers(c: dict) -> list[dict]:
    """Apps whose spec binds the cluster, and literal DB-ish keys they hold themselves."""
    out = []
    for app in doctl("apps", "list") or []:
        spec = app.get("spec") or {}
        bound = [d for d in spec.get("databases") or [] if d.get("cluster_name") == c["name"]]
        if not bound:
            continue
        literals = []
        for scope in [spec, *[x for k in ("services", "workers", "jobs") for x in spec.get(k) or []]]:
            for e in scope.get("envs") or []:
                v = str(e.get("value", ""))
                if "${" not in v and credential_literal(e["key"], v):
                    literals.append(f"{scope.get('name', 'app')}:{e['key']} ({literal_form(e['key'], v)})")
        out.append({"app": app["id"], "name": spec.get("name"),
                    "binds": [(d.get("name"), d.get("db_user") or "default") for d in bound],
                    "literals": literals})
    return out


def cmd_consumers(ref: str) -> int:
    c = cluster(ref)
    found = consumers(c)
    for x in found:
        print(f"{x['name']:28} {x['app']} binds={x['binds']} own-literals={x['literals'] or '-'}")
    print("only App Platform apps whose spec binds this cluster are listed; an app that reaches it by "
          "a URL alone, a droplet, a Worker or a laptop is not visible here")
    if not found:
        print("no App Platform app binds this cluster")
    print(f"exposure: {exposure(c['id'])}")
    return 0


def user_password(c: dict, user: str) -> str:
    u = one(doctl("databases", "user", "get", c["id"], user))
    pw = u.get("password") if isinstance(u, dict) and u.get("name") == user else None
    if not isinstance(pw, str) or not pw:
        raise DoorError(f"the API returned no password for {user} on {c['name']}")
    return pw


def cmd_credential(ref: str, user: str, db: str | None, to: str) -> int:
    checked_name(ref, "cluster")
    checked_name(user, "database user")
    where = plan(to)
    c = cluster(ref)
    # THE SAME CHECKS AS ROTATE (review round 5): rotate's exit-4/exit-5 hints send
    # operators here, and a URL to a database that does not exist would be written
    # into the vault as if valid. Checked before the password is even read.
    validate_connection(c, user, db)
    pw = user_password(c, user)
    print(f"{c['name']} user {user}: {deliver(connection_url(c, user, pw, db), where)}")
    return 0


def current_context() -> str:
    """doctl's context by name: `DIGITALOCEAN_CONTEXT` when set (doctl reads it), else
    the one `doctl auth list` marks `(current)`."""
    if os.environ.get("DIGITALOCEAN_CONTEXT"):
        return os.environ["DIGITALOCEAN_CONTEXT"]
    try:
        p = subprocess.run([DOCTL, "auth", "list"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    for line in p.stdout.splitlines():
        if "(current)" in line:
            return line.replace("(current)", "").strip() or "unknown"
    return "unknown"


def context_line() -> str:
    """Which DigitalOcean account the reset will act in — names, never the email."""
    try:
        acct = doctl("account", "get", tries=1) or {}
        team = ((acct.get("team") or {}).get("name") if isinstance(acct, dict) else None) or "personal account"
        team_text = f"team: {private_files.credential_shape.shown(team)}"
    except DoorError:
        team_text = "team unknown (doctl account get failed)"
    return f"doctl context: {private_files.credential_shape.shown(current_context())}; {team_text}"


DEPLOYMENT_PHASES = frozenset({"UNKNOWN", "PENDING", "PENDING_BUILD", "BUILDING",
    "PENDING_DEPLOY", "DEPLOYING", "ACTIVE", "ERROR", "CANCELED", "SUPERSEDED"})


def deployment_rows(app: str) -> list[dict]:
    rows = doctl("apps", "list-deployments", app)
    if not isinstance(rows, list):
        raise DoorError("deployment listing has an unsupported shape")
    for row in rows:
        if not isinstance(row, dict):
            raise DoorError("deployment listing has an unsupported row")
        checked_name(row.get("id"), "deployment id")
        if not isinstance(row.get("phase"), str) or row["phase"] not in DEPLOYMENT_PHASES:
            raise DoorError("deployment phase is unsupported; confirmation unavailable")
    return rows


def deployments(app: str) -> set[str]:
    return {d["id"] for d in deployment_rows(app)}


def wait_new(app: str, before: set[str], timeout: float) -> dict:
    end = time.monotonic() + timeout
    state = {"id": None, "phase": "PENDING"}
    while time.monotonic() < end:
        new = [d for d in deployment_rows(app) if d["id"] not in before]
        state = {"id": new[0]["id"], "phase": new[0]["phase"]} if new else state
        if state["phase"] in ("ACTIVE", "ERROR", "CANCELED", "SUPERSEDED"):
            return state
        time.sleep(min(POLL, max(0, end - time.monotonic())))
    return state


#: Exit codes of `db rotate`. 1 is a refusal before the reset: nothing changed.
#: Every other non-zero code follows a reset attempt; the effect may be unknown.
#: When several failures happen the
#: largest is returned, and each is named on stderr with its next step.
EXIT_NOT_ACTIVE = 2       # a deployment did not reach ACTIVE
EXIT_VAULT = 3            # the vault did not take the new value
EXIT_APP = 4              # an app could not be updated or redeployed
EXIT_RESET = 5            # reset outcome unknown: reconcile, never retry blindly


def _scopes(spec: dict) -> list[dict]:
    return [spec, *[x for k in ("services", "workers", "jobs") for x in spec.get(k) or []]]


# Expected failures are classified by phase; their message bodies can carry values.
PHASE_ERRORS = (DoorError, OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError, AttributeError)


def checked_name(value: str, field: str) -> str:
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value)
            or private_files.credential_shape.shaped(value, uuids=False)):
        raise DoorError(f"invalid {field}; give an identifier, never a credential")
    return value


def _is_name(value: object) -> bool:
    try:
        checked_name(value, "name")
    except DoorError:
        return False
    return True


def validate_connection(c: dict, user: str, db: str | None) -> None:
    checked_name(c.get("id"), "cluster id")
    checked_name(c.get("name"), "cluster name")
    conn = c.get("connection")
    engine = c.get("engine")
    if engine not in ("pg", "mysql", "redis", "valkey"):
        # Named, not "incomplete metadata" (review round 6): mongodb, kafka and
        # opensearch need scheme options this door does not build.
        shown = engine if _is_name(engine) else "this engine"
        raise DoorError(f"the door supports pg, mysql, redis and valkey clusters, not {shown}")
    if (not isinstance(conn, dict)
            or not isinstance(conn.get("host"), str) or not conn["host"]
            or type(conn.get("port")) is not int or not 0 < conn["port"] < 65536):
        raise DoorError("unsupported or incomplete database connection metadata")
    if db is not None:
        checked_name(db, "database")
        # `--db` NAMES ONE OF THE CLUSTER'S DATABASES (review round 4): a URL to a
        # database that does not exist would be delivered and written as if valid.
        # Each refusal says what is known (review round 5): an engine without
        # databases, a provider that listed none, or a list that lacks this one.
        if c["engine"] in ("redis", "valkey"):
            raise DoorError(f"--db does not apply to {c['engine']}; leave it out")
        names = c.get("db_names")
        if not isinstance(names, list) or not names:
            raise DoorError(f"the provider listed no databases for this cluster, so --db {db} cannot be "
                            "checked; read the cluster's databases with doctl before naming one")
        # Printed names are identifiers only: the provider's list is untrusted.
        shown = [n for n in names if _is_name(n)]
        if not shown:
            raise DoorError(f"the provider listed no readable database names for this cluster, so --db {db} "
                            "cannot be checked")
        if db not in shown:
            raise DoorError(f"--db {db} is not one of this cluster's databases ({', '.join(shown)})")
    # Construct once before mutation so a malformed connection cannot strand apps.
    connection_url(c, user, "synthetic-preflight", db)


def check_literal(app: str, key: str, value: str, user: str, db: str | None,
                  c: dict, current: str) -> str:
    """Prove a literal by provider bytes/connection metadata, never by its env name.

    An encrypted App Platform value exposes no type or identity. Replacing it on
    the strength of PASSWORD or URL in a name can overwrite an unrelated key.
    Such literals must be migrated to provider database bindings before rotation.
    """
    if value == current:
        return "password"
    if "://" in value:
        try:
            parts = urllib.parse.urlsplit(value)
            conn = c["connection"]
            schemes = {"pg": ("postgres", "postgresql"), "mysql": ("mysql",),
                       "redis": ("redis", "rediss"), "valkey": ("valkey", "rediss")}[c["engine"]]
            proven = (parts.scheme in schemes and urllib.parse.unquote(parts.username or "") == user
                      and urllib.parse.unquote(parts.password or "") == current
                      and parts.hostname == conn["host"] and parts.port == conn["port"])
            if proven:
                return "url"
        except (ValueError, KeyError, TypeError):
            pass
    raise DoorError(f"app {app} key {key}: cannot prove this literal belongs to the selected "
                    "database user; use a provider database binding instead of an opaque or unrelated literal")


def start_receipt(ref: str, user: str, apps: list[str]) -> tuple[pathlib.Path, dict]:
    # Written before reset. A lost process leaves UNKNOWN, never an invented success.
    path = private_files.paths.STATE / "digitalocean-rotations" / (uuid.uuid4().hex + ".json")
    row = {"schema_version": 1, "cluster": ref, "user": user, "reset": "unknown",
           "phase": "reset_attempted", "apps": {a: "unknown" for a in apps}}
    private_files._atomic_write(path, json.dumps(row) + "\n")
    return path, row


def finish_receipt(path: pathlib.Path, row: dict, code: int) -> int:
    row["exit_code"] = code
    try:
        private_files._atomic_write(path, json.dumps(row) + "\n")
    except (OSError, ValueError):
        print("digitalocean: rotation receipt could not be updated; earlier receipt remains uncertain. "
              "Reconcile the current credential and consumers; do not repeat reset.", file=sys.stderr)
        return max(code, 6)
    return code


def cmd_rotate(ref: str, user: str, db: str | None, to: str, apps: list[str],
               spec_envs: list[str], wait: float) -> int:
    # A repeatable flag names a set of consumers, never repeated mutations.
    apps = list(dict.fromkeys(apps))
    # 1. everything we need, before anything changes
    try:
        checked_name(ref, "cluster")
        checked_name(user, "database user")
        if db is not None:
            checked_name(db, "database")
        for app in apps:
            checked_name(app, "app id")
        where = plan(to)
        print(context_line())
        c = cluster(ref)
        validate_connection(c, user, db)
        literal: dict[str, list[str]] = {}
        for item in spec_envs:
            app, _, key = item.partition(":")
            if not key:
                raise DoorError("--spec-env must be APP_ID:KEY")
            checked_name(app, "app id")
            checked_name(key, "environment key")
            literal.setdefault(app, []).append(key)
        specs = {app: doctl("apps", "spec", "get", app, "--format", "json") for app in literal}
        current = user_password(c, user) if literal else None
        forms = {}
        for app, keys in literal.items():
            # EVERY LEVEL, not a merged view (review round 3): a key set at app
            # AND service level is two literals, and step 4 writes both.
            levels = [e for scope in _scopes(specs[app]) for e in scope.get("envs") or []]
            missing = [k for k in keys if k not in {e["key"] for e in levels}]
            if missing:
                raise DoorError(f"app {app} has no env {missing}")
            # A `${…}` BINDING IS NEVER REWRITTEN (review round 4): App Platform
            # resolves it at deploy time, so it is left exactly as it is at every
            # level, even beside a literal of the same key at another level.
            for k in keys:
                if all("${" in str(e.get("value", "")) for e in levels if e["key"] == k):
                    raise DoorError(f"app {app} key {k} is a ${{…}} binding at every level; a binding moves "
                                    f"with a redeploy, so name the app with --app, not --spec-env")
            for e in levels:
                if e["key"] in keys and "${" not in str(e.get("value", "")):
                    forms[id(e)] = check_literal(app, e["key"], str(e.get("value", "")), user, db, c, current)
        del current
        every = list(dict.fromkeys([*apps, *literal]))
        before = {app: deployments(app) for app in every}
        receipt_path, receipt = start_receipt(ref, user, every)
    except PHASE_ERRORS as e:
        # DoorError diagnostics above are static or validated names, never provider bodies.
        reason = str(e) if isinstance(e, DoorError) else type(e).__name__
        raise DoorError(f"{reason}: nothing was reset") from None
    # 2. Exactly one request. Even a nonzero exit can follow a committed reset.
    try:
        reset = one(doctl("databases", "user", "reset", c["id"], user, tries=1))
    except PHASE_ERRORS:
        reset = None
    pw = reset.get("password") if isinstance(reset, dict) and reset.get("name") == user else None
    if not isinstance(pw, str) or not pw:
        print(f"digitalocean: reset outcome unknown for {user} on {c['name']}; the password may have "
              "changed. No consumer was moved. Do not repeat db rotate. Next: "
              f"`digitalocean.py db credential {ref} {user}"
              + (f" --db {db}" if db else "") + f" --to {to}` reads the current value into the vault; "
              "then reconcile every named consumer without another reset", file=sys.stderr)
        receipt["phase"] = "reconciliation_required"
        return finish_receipt(receipt_path, receipt, EXIT_RESET)
    receipt["reset"] = "confirmed"
    print(f"reset {c['name']} user {user}")
    url = connection_url(c, user, pw, db)
    code = 0
    vault_failed = False
    # 3. the vault — a refusal now does not stop the apps from moving
    try:
        print(deliver(url, where))
    except PHASE_ERRORS as e:
        code, vault_failed = max(code, EXIT_VAULT), True
        print(f"digitalocean: the password of {user} on {c['name']} WAS reset, but the vault did not take "
              f"it ({type(e).__name__}); moving the apps anyway so none sits on a dead password", file=sys.stderr)
    receipt["vault"] = "failed" if vault_failed else "delivered"
    # 4. literals → apps update (redeploys); 5. bindings → redeploy. A failure is
    # recorded and the rest still move: stopping would leave them on a dead password.
    done: list[str] = []
    left: list[str] = []
    outcomes: dict[str, str] = {}
    for app, keys in literal.items():
        spec = specs[app]
        path = None
        attempted = False
        try:
            for scope in _scopes(spec):
                for e in scope.get("envs") or []:
                    if e["key"] in keys and id(e) in forms:
                        old_value = str(e.get("value", ""))
                        if forms[id(e)] == "password":
                            e["value"] = pw
                        else:
                            e["value"] = with_password(old_value, pw)
                        e["type"] = "SECRET"
            fd, path = tempfile.mkstemp(suffix=".json")
            with os.fdopen(fd, "w") as f:
                os.fchmod(f.fileno(), 0o600)
                json.dump(spec, f)
            attempted = True
            doctl("apps", "update", app, "--spec", path, tries=1)
            outcomes[app] = "submitted"
            done.append(app)
            print(f"  app {app}: {', '.join(keys)} replaced in its spec")
        except PHASE_ERRORS as e:
            left.append(app)
            outcomes[app] = "unknown" if attempted else "not_started"
            status = "outcome unknown" if attempted else "update not started"
            print(f"digitalocean: app {app} {status} ({type(e).__name__}); reconcile before any retry", file=sys.stderr)
        finally:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    code = max(code, EXIT_APP)
                    print(f"digitalocean: password WAS reset; temporary spec cleanup failed for {app}; "
                          "a private temporary file may remain", file=sys.stderr)
    del pw, url
    for app in apps:
        if app in literal:
            continue
        try:
            doctl("apps", "create-deployment", app, tries=1)
            outcomes[app] = "submitted"
            done.append(app)
        except PHASE_ERRORS as e:
            left.append(app)
            outcomes[app] = "unknown"
            print(f"digitalocean: app {app} redeploy outcome unknown ({type(e).__name__}); "
                  "reconcile before any retry", file=sys.stderr)
    if left:
        code = max(code, EXIT_APP)
        print(f"The database password WAS reset. apps done: {', '.join(done) or 'none'}; apps left: "
              f"{', '.join(left)} — their OLD password may still be in use. Mutation results can be "
              "unknown. Do not retry app updates, redeploys or db rotate blindly. Next: read each "
              "app's current spec and deployment state, then reconcile its credential against "
              f"`digitalocean.py db credential {ref} {user}"
              + (f" --db {db}" if db else "") + f" --to {to}`. A redeploy alone keeps an old literal.",
              file=sys.stderr)
    # 6. wait
    failed = []
    for app in [a for a in every if a not in left]:
        try:
            state = wait_new(app, before[app], wait)
            if not isinstance(state, dict) or state.get("phase") not in DEPLOYMENT_PHASES:
                raise DoorError("deployment confirmation unavailable")
            if state.get("id") is not None:
                checked_name(state["id"], "deployment id")
            active = state["phase"] == "ACTIVE" and state.get("id") is not None
            shown = state["phase"]
        except PHASE_ERRORS:
            active, shown = False, "unknown (confirmation unavailable)"
        if not active:
            failed.append(app)
        print(f"  app {app} deployment {shown}")
    if failed:
        code = max(code, EXIT_NOT_ACTIVE)
        print("The database password WAS reset, and a consumer is not confirmed ACTIVE: its running copy may "
              "still hold the OLD password. Check it now: "
              + "; ".join(f"`doctl apps logs {app} --type deploy`" for app in failed), file=sys.stderr)
    if vault_failed and not left:
        print(f"next: put the current password in the vault — `digitalocean.py db credential {ref} {user}"
              + (f" --db {db}" if db else "") + f" --to {to}` — it reads the reset value from the API",
              file=sys.stderr)
    receipt["apps"] = {a: (outcomes[a] if a in left else "unconfirmed" if a in failed else "active") for a in every}
    receipt["phase"] = "consumers_checked"
    code = finish_receipt(receipt_path, receipt, code)
    if code:
        return code
    print(f"next: verify each consumer's health, then `vault.py settle {where['project']} {where['env']} "
          f"{where['name']} --how … --revocation-evidence … --consumer-evidence …` if this closes a leak")
    return 0


# ══ teams and tokens (OBS-155) ═════════════════════════════════════════════════
#
# The account half of the door: one token per TEAM stashed and never handed out, narrow
# tokens installed with a record, rotation and revocation that count only when the
# provider is seen refusing the old value. Talks to the API over HTTPS directly, not
# through doctl: doctl prints whatever the API answers, and this half needs the status
# code to tell "no such scope" (403) from "dead token" (401) from "slow down" (429).
#
# WHAT THE PUBLIC API CANNOT DO, measured on 2026-10-19 and written where it binds:
#  - mint, roll or list personal access tokens: the API reference has no token endpoint,
#    and `GET /v2/tokens` answers 403 to a full-access personal token. So `issue` refuses
#    and names the control-panel step, and `install` takes the minted token on stdin;
#  - list, invite, remove or re-role team members: no such endpoint in the reference. Only
#    `POST /v2/organizations/team` (a NEW team with invitations, in an organization context)
#    exists, and `team create` is that;
#  - revoke a personal token: the documented revocation endpoint is OAuth's. The door calls
#    it and then asks `/v2/account` with the old value: only a 401 counts as revoked.

API_DEFAULT = "https://api.digitalocean.com"
OAUTH_REVOKE = "https://cloud.digitalocean.com/v1/oauth/revoke"
PANEL_TOKENS = "https://cloud.digitalocean.com/account/api/tokens"
PANEL_TEAM = "https://cloud.digitalocean.com/account/team"
API_REFERENCE = "https://docs.digitalocean.com/reference/api/digitalocean/"
SCOPES_REFERENCE = "https://docs.digitalocean.com/reference/api/scopes/"

sys.path.insert(0, str(ROOT))
import paths  # noqa: E402
import private_io  # noqa: E402

_STORE = paths.source_path("secret_store", paths.SECRETS)
#: One file per team: the team's own token (full access, from the operator), mode 600.
ADMIN_STORE = _STORE / "digitalocean-admin"
#: The collector's read tokens, one per team (`install --preset observatory-read`).
READ_STORE = _STORE / "digitalocean-read.d"
#: One record per installed token: team, preset, where it went, dates. Never a value.
RECORDS = _STORE / "digitalocean-issued"

#: Roles an invitation may carry (the API reference's `team_invitation.role`); `owner` cannot be invited.
INVITE_ROLES = ("member", "biller", "billing viewer", "resource viewer", "modifier")

#: A narrow token is MINTED in the control panel with these custom scopes (scope names from
#: the scopes reference) and then installed. `probes` are the reads the door makes with the new
#: value before it is delivered; write scopes are declared, never proven, and the record says so.
PRESETS: dict[str, dict] = {
    "observatory-read": {
        "scopes": ("account:read", "actions:read", "app:read", "billing:read", "block_storage:read",
                   "database:read", "domain:read", "droplet:read", "project:read", "regions:read",
                   "sizes:read"),
        "probes": ("/v2/account", "/v2/droplets?per_page=1"),
        "why": "what the collector reads: Droplets, apps, databases, volumes, domains and the balance",
        "destination": "read-store",
    },
    "read-only": {
        "scopes": ("account:read", "actions:read", "app:read", "billing:read", "block_storage:read",
                   "database:read", "domain:read", "droplet:read", "image:read", "project:read",
                   "regions:read", "registry:read", "sizes:read", "snapshot:read", "tag:read", "vpc:read"),
        "probes": ("/v2/account", "/v2/droplets?per_page=1"),
        "why": "reads a team's resources for a project; no database credentials (no database:view_credentials)",
    },
    "app-deploy": {
        "scopes": ("account:read", "actions:read", "app:read", "app:create", "app:update"),
        "probes": ("/v2/account", "/v2/apps?per_page=1"),
        "why": "deploys and updates App Platform apps of one team",
    },
    "droplet-deploy": {
        "scopes": ("account:read", "actions:read", "droplet:read", "droplet:create", "droplet:update",
                   "image:read", "regions:read", "sizes:read", "ssh_key:read", "tag:read", "tag:create",
                   "vpc:read"),
        "probes": ("/v2/account", "/v2/droplets?per_page=1"),
        "why": "creates and changes Droplets of one team",
    },
    "dns-edit": {
        "scopes": ("account:read", "domain:read", "domain:create", "domain:update", "domain:delete"),
        "probes": ("/v2/account", "/v2/domains/{domain}"),
        "needs": "domain",
        "why": "edits DNS records; DigitalOcean scopes are per team, so it reaches EVERY domain of the team",
    },
    "registry": {
        "scopes": ("account:read", "registry:read", "registry:update"),
        "probes": ("/v2/account", "/v2/registry"),
        "absent_ok": ("/v2/registry",),
        "why": "pushes to and reads the team's container registry",
    },
    "spaces-keys": {
        "scopes": ("account:read", "spaces_key:read", "spaces_key:create_credentials"),
        "probes": ("/v2/account", "/v2/spaces/keys?per_page=1"),
        "why": "lists and mints Spaces access keys of one team",
    },
    "billing-read": {
        "scopes": ("account:read", "billing:read"),
        "probes": ("/v2/account", "/v2/customers/my/balance"),
        "why": "reads the team's balance and month-to-date usage",
    },
}

#: Vault slot names that hold a DigitalOcean token, by convention; `teams` matches them by value.
TOKEN_SLOT = re.compile(r"^(DIGITALOCEAN|DO)_(API_|ACCESS_)?TOKEN$")


class ApiError(DoorError):
    def __init__(self, status: int, endpoint: str, retry_after: str | None = None):
        self.status, self.endpoint, self.retry_after = status, endpoint, retry_after
        wait = f", retry after {retry_after} s" if retry_after else ""
        what = {401: "token not accepted", 403: "token lacks the scope", 404: "not found",
                412: "precondition not met", 429: "rate limited"}.get(status, "refused")
        super().__init__(f"DigitalOcean answered HTTP {status} on {endpoint} ({what}{wait}); "
                         f"provider response withheld")


def api_base() -> str:
    """The API's origin. An override (tests) must be loopback: a token is never sent elsewhere."""
    base = os.environ.get("OBSERVATORY_DO_API", "").rstrip("/")
    if not base:
        return API_DEFAULT
    host = urllib.parse.urlsplit(base).hostname or ""
    if urllib.parse.urlsplit(base).scheme != "http" or host not in ("127.0.0.1", "localhost", "::1"):
        raise DoorError("OBSERVATORY_DO_API must be a loopback http:// origin; a token is never sent elsewhere")
    return base


def revoke_url() -> str:
    base = api_base()
    return OAUTH_REVOKE if base == API_DEFAULT else f"{base}/v1/oauth/revoke"


def _endpoint(path: str) -> str:
    return path.split("?", 1)[0]


#: What a token may contain: printable ASCII, no space. A value with a newline or a control
#: character makes http.client raise an error whose MESSAGE quotes the header, value included,
#: so such a value is refused before any header is built.
TOKEN_SHAPE = re.compile(r"[\x21-\x7e]{1,512}")


def token_ok(value: object) -> bool:
    return isinstance(value, str) and TOKEN_SHAPE.fullmatch(value) is not None


def _wire(method: str, url: str, token: str, *, body: bytes | None = None,
          content_type: str = "application/json") -> dict:
    """One call. The error names the endpoint and the status, never a header or a body."""
    import urllib.error
    import urllib.request
    endpoint = _endpoint(urllib.parse.urlsplit(url).path)
    if not token_ok(token):
        raise DoorError(f"a value meant for {endpoint} is not shaped like a token (whitespace or a "
                        "control character inside it); it was not sent")
    try:
        req = urllib.request.Request(url, data=body, method=method,
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": content_type,
                                              "User-Agent": "project-observatory"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        # Retry-After is the provider's RESPONSE header, a number of seconds: no credential.
        wait = e.headers.get("Retry-After") if e.code == 429 and e.headers else None
        e.close()
        raise ApiError(e.code, endpoint, wait if (wait or "").isdigit() else None) from None
    except OSError as e:
        raise DoorError(f"DigitalOcean unreachable on {endpoint}: {type(e).__name__}") from None
    except Exception as e:  # noqa: BLE001  any other message may quote the request; only the class leaves
        raise DoorError(f"the call to {endpoint} failed ({type(e).__name__}); details withheld") from None
    try:
        doc = json.loads(raw) if raw.strip() else {}
    except ValueError:
        raise DoorError(f"DigitalOcean answered {endpoint} with something that is not JSON") from None
    return doc if isinstance(doc, dict) else {}


def _api(path: str, token: str, payload: dict | None = None, method: str | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    return _wire(method or ("POST" if data is not None else "GET"), api_base() + path, token, body=data)


def _pages(path: str, token: str, key: str, limit: int = 50) -> list[dict]:
    """Every row of a listing, page by page, until the API stops naming a next page."""
    out: list[dict] = []
    sep = "&" if "?" in path else "?"
    for page in range(1, limit + 1):
        doc = _api(f"{path}{sep}page={page}&per_page=200", token)
        rows = doc.get(key) or []
        out.extend(r for r in rows if isinstance(r, dict))
        if not ((doc.get("links") or {}).get("pages") or {}).get("next") or not rows:
            return out
    raise DoorError(f"{_endpoint(path)} has more than {limit} pages; refusing to read further")


def account_of(token: str) -> dict:
    """{user_uuid, team_uuid, team_name, status} of a token. Never the email."""
    a = _api("/v2/account", token).get("account") or {}
    team = a.get("team") or {}
    return {"user_uuid": a.get("uuid"), "team_uuid": team.get("uuid"), "team_name": team.get("name"),
            "status": a.get("status")}


def user_teams(token: str) -> list[dict]:
    """The user's teams with their role. `GET /v2/teams` answers this but is NOT in the API
    reference (measured 2026-10-19), so a failure is a note, never a refusal."""
    rows = _api("/v2/teams?per_page=200", token).get("teams") or []
    return [{"uuid": t.get("uuid"), "name": t.get("name"), "role": t.get("user_role")}
            for t in rows if isinstance(t, dict)]


def slug(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "personal"


def one_value(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise DoorError("nothing arrived on stdin")
    if len(value.split()) > 1:
        raise DoorError(f"{len(value.split())} whitespace-separated pieces arrived on stdin, not one token")
    if not token_ok(value):
        raise DoorError("what arrived on stdin has a character a token cannot have")
    return value


def write_secret(path: pathlib.Path, value: str) -> None:
    private_io.write(path, value + "\n")


def _meta(path: pathlib.Path) -> pathlib.Path:
    return path.with_name(path.name + ".meta.json")


def read_record(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    try:
        doc = json.loads(private_io.read(path))
    except (OSError, ValueError, RuntimeError):
        raise DoorError(f"record {path.name} is unreadable; refusing to replace it") from None
    if not isinstance(doc, dict):
        raise DoorError(f"record {path.name} is not an object")
    return doc


def write_record(path: pathlib.Path, doc: dict) -> None:
    private_io.write(path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")


def today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def stashes() -> list[tuple[str, pathlib.Path, dict]]:
    """(label, value file, record) for every stashed team token."""
    if any(p.is_symlink() for p in (ADMIN_STORE, *ADMIN_STORE.parents)):
        raise DoorError("the DigitalOcean stash must not contain symbolic links")
    if not ADMIN_STORE.is_dir():
        return []
    return [(p.name, p, read_record(_meta(p))) for p in sorted(ADMIN_STORE.iterdir())
            if p.is_file() and not p.name.startswith(".") and not p.name.endswith(".meta.json")]


def stash_of(label: str) -> tuple[pathlib.Path, dict]:
    for name, path, meta in stashes():
        if name == label:
            return path, meta
    have = ", ".join(n for n, _p, _m in stashes()) or "none"
    raise DoorError(f"no stashed team called {label!r} (have: {have}); `digitalocean.py stash` first")


def records_lock():
    """One writer at a time over the records, the read store and the slots they name: held from
    the first read of a record to its last write (install, rotate, revoke)."""
    return private_io.lock(RECORDS / ".lock")


def records() -> list[tuple[str, pathlib.Path, dict]]:
    if not RECORDS.is_dir():
        return []
    return [(p.name.removesuffix(".meta.json"), p, read_record(p))
            for p in sorted(RECORDS.glob("*.meta.json"))]


def _journal(event: str, secret: str, **detail) -> None:
    """The vault's movements journal. Never raises: a journal failure must not undo a change."""
    try:
        private_files.journal(event, secret, tool="digitalocean.py", **detail)
    except Exception as exc:  # noqa: BLE001
        print(f"  (movement not journaled: {type(exc).__name__})", file=sys.stderr)


# ── doctl's config, read directly ─────────────────────────────────────────────

def doctl_config_path() -> pathlib.Path:
    if os.environ.get("DOCTL_CONFIG"):
        return pathlib.Path(os.environ["DOCTL_CONFIG"]).expanduser()
    home = pathlib.Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "doctl" / "config.yaml"
    if os.name == "nt":
        return pathlib.Path(os.environ.get("APPDATA", home)) / "doctl" / "config" / "config.yaml"
    return pathlib.Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "doctl" / "config.yaml"


def doctl_contexts(text: str) -> dict[str, str]:
    """{context: token} from doctl's YAML: `default` is the top-level `access-token` (as doctl
    reads it), the rest are `auth-contexts` entries. Two keys only, so no YAML library."""
    top, ctxs, inblock = "", {}, False
    for line in text.splitlines():
        if re.match(r"^\S", line):
            inblock = line.startswith("auth-contexts:")
            m = re.match(r"^access-token:\s*(.*?)\s*$", line)
            if m:
                top = m.group(1).strip("'\"")
        elif inblock:
            m = re.match(r"^\s+([^\s:#][^:]*?)\s*:\s*(.*?)\s*$", line)
            if m:
                ctxs[m.group(1).strip("'\"")] = m.group(2).strip("'\"")
    out = {k: v for k, v in ctxs.items() if v and k != "default"}
    if top:
        out["default"] = top
    return out


# ── commands ───────────────────────────────────────────────────────────────────

def _stash_value(value: str, source: str) -> str:
    try:
        acct = account_of(value)
    except ApiError as e:
        raise DoorError(f"not stashed: {e}") from None
    if not acct.get("team_uuid"):
        raise DoorError("not stashed: the token answers, but names no team")
    role, teams_note = None, None
    try:
        role = next((t["role"] for t in user_teams(value) if t["uuid"] == acct["team_uuid"]), None)
    except DoorError as e:
        teams_note = f"role unknown: {e}"
    label = slug(acct["team_name"])
    ADMIN_STORE.mkdir(parents=True, exist_ok=True)
    for name, _path, meta in stashes():
        if name == label and meta.get("team_uuid") not in (None, acct["team_uuid"]):
            label = f"{label}-{acct['team_uuid'][:8]}"
            break
    dest = ADMIN_STORE / label
    existed = dest.is_file()
    write_secret(dest, value)
    write_record(_meta(dest), {
        "kind": "team", "label": label, "team_uuid": acct["team_uuid"], "team_name": acct["team_name"],
        "role": role, "role_note": teams_note, "source": source,
        "stashed_on": today(), "why": "the team's own token: reads teams, verifies installs; never handed out"})
    _journal("stash", f"digitalocean/{label}", team=acct["team_uuid"], source=source)
    return f"{'replaced' if existed else 'stashed'}: team {acct['team_name']!r} as {label!r} (role {role or 'unknown'})"


def cmd_stash(from_doctl: str | None) -> int:
    if not from_doctl:
        if sys.stdin.isatty():
            raise DoorError("give the team's token on stdin, from a protected file: "
                            "`digitalocean.py stash < token-file`, or `stash --from-doctl CONTEXT`")
        print(_stash_value(one_value(sys.stdin.read()), "stdin"))
        return 0
    path = doctl_config_path()
    if path.is_symlink():
        raise DoorError("doctl's config is a symbolic link; refusing to follow it")
    try:
        ctxs = doctl_contexts(private_io.read(path) if path.is_file() else "")
    except RuntimeError:
        # doctl writes its config 0600; a wider file is still readable, but say so. Opened without
        # following a link, and only as a regular file.
        import stat as _stat
        import osprivacy
        fd = osprivacy.open(path, os.O_RDONLY | osprivacy.NOFOLLOW | osprivacy.NONBLOCK)
        with os.fdopen(fd, encoding="utf-8") as f:
            if not _stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise DoorError("doctl's config is not a regular file") from None
            ctxs = doctl_contexts(f.read())
        print("note: doctl's config is readable beyond its owner; `chmod 600` it", file=sys.stderr)
    if not ctxs:
        raise DoorError(f"no doctl context holds a token (looked in doctl's config: {path.name})")
    wanted = sorted(ctxs) if from_doctl == "all" else [from_doctl]
    for name in wanted:
        if name not in ctxs:
            raise DoorError(f"doctl has no context {name!r}; it has: {', '.join(sorted(ctxs))}")
    seen: dict[str, str] = {}
    bad = 0
    for name in wanted:
        value = ctxs[name]
        try:
            acct = account_of(value)
        except ApiError as e:
            print(f"  doctl:{name}: not stashed: {e}", file=sys.stderr)
            bad += 1
            continue
        if acct.get("team_uuid") in seen:
            print(f"  doctl:{name}: same team as doctl:{seen[acct['team_uuid']]}; stashed once")
            continue
        seen[acct.get("team_uuid")] = name
        print(f"  doctl:{name}: " + _stash_value(value, f"doctl:{name}"))
    return 1 if bad else 0


def _vault_slots() -> list[tuple[str, pathlib.Path]]:
    """(PROJECT/ENV/NAME, file) of every vault slot whose name says DigitalOcean token."""
    store = private_files.STORE
    if not store.is_dir():
        return []
    out = []
    for slot in sorted(store.glob("*/*/*")):
        if slot.is_file() and TOKEN_SLOT.match(slot.name) and not slot.name.endswith(".meta.json"):
            out.append(("/".join(slot.relative_to(store).parts), slot))
    return out


def _same(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a.encode(), b.encode())


def cmd_teams(probe: bool) -> int:
    st = stashes()
    if not st:
        print("no team stashed; `digitalocean.py stash < token-file` or `stash --from-doctl all`")
        return 0
    values = {label: private_io.read(path).strip() for label, path, _m in st}
    by_team: dict[str, list[str]] = {label: [] for label, _p, _m in st}
    for _label, _p, rec in records():
        owner = next((l for l, _p2, m in st if m.get("team_uuid") == rec.get("team_uuid")), None)
        where = rec.get("vault") or rec.get("destination")
        if owner and where:
            by_team[owner].append(f"{where} ({rec.get('preset')})")
    unknown = []
    for name, slot in _vault_slots():
        try:
            held = private_files._read_private(slot).strip()
        except (private_files.VaultBoundaryError, OSError):
            continue
        hit = next((l for l, v in values.items() if _same(held, v)), None)
        if hit:
            by_team[hit].append(f"{name} (the team token itself)")
            continue
        if any(name in entry for entries in by_team.values() for entry in entries):
            continue
        if probe:
            try:
                team = account_of(held).get("team_uuid")
                owner = next((l for l, _p, m in st if m.get("team_uuid") == team), None)
                if owner:
                    by_team[owner].append(f"{name} (same team, by the provider)")
                    continue
                unknown.append(f"{name} (a team with no stash here)")
            except DoorError as e:
                unknown.append(f"{name} ({e.status if isinstance(e, ApiError) else 'unreadable'})")
        else:
            unknown.append(name)
    listed: set[str] = set()
    for label, path, meta in st:
        listed.add(meta.get("team_uuid") or "")
        print(f"{label:24} {meta.get('team_name')!s:24} {meta.get('team_uuid')}  role {meta.get('role') or 'unknown'}"
              f"  stashed {meta.get('stashed_on', '?')} from {meta.get('source', '?')}  "
              f"mode {oct(path.stat().st_mode)[-3:]}")
        for s in by_team[label] or ["(no vault slot is known to belong to it)"]:
            print(f"    {s}")
    # The user's other teams, from any live stash (the listing is not in the API reference).
    others: dict[str, dict] = {}
    for label, _p, _m in st:
        try:
            for t in user_teams(values[label]):
                if t["uuid"] not in listed:
                    others.setdefault(t["uuid"], t)
            break
        except DoorError as e:
            print(f"  (team listing through {label}: {e})", file=sys.stderr)
    for t in others.values():
        print(f"{'-':24} {t['name']!s:24} {t['uuid']}  role {t.get('role') or 'unknown'}  no token stashed")
    if unknown:
        print("DigitalOcean-named vault slots of no stashed team"
              + ("" if probe else " (`teams --probe` asks the provider which team each is)") + ":")
        for s in unknown:
            print(f"    {s}")
    return 0


def cmd_members(team: str) -> int:
    path, meta = stash_of(team)
    value = private_io.read(path).strip()
    print(f"team {meta.get('team_name')} ({meta.get('team_uuid')})")
    try:
        role = next((t["role"] for t in user_teams(value) if t["uuid"] == meta.get("team_uuid")), None)
        print(f"  your role: {role or 'unknown'}")
    except DoorError as e:
        print(f"  your role: unknown ({e})")
    try:
        rows = _api("/v2/organizations/teams", value).get("teams") or []
        hit = next((t for t in rows if t.get("uuid") == meta.get("team_uuid")), None)
        if hit:
            print(f"  {hit.get('member_count')} member(s), organisation status {hit.get('status')}")
        else:
            print("  the organisation's listing does not name this team")
    except ApiError as e:
        print("  member count: this token is not in an organization context "
              f"(HTTP {e.status}); the count comes only from an organization's team listing")
    print("  names, roles and invitations: DigitalOcean's public API has no endpoint for them "
          f"({API_REFERENCE}); manage members at {PANEL_TEAM}")
    return 0


def _masked(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def cmd_team_create(name: str, org: str, invites: list[str], apply: bool) -> int:
    if not re.fullmatch(r"[\w .-]{1,64}", name):
        raise DoorError("a team name is 1-64 letters, digits, spaces, dots or dashes")
    invitations = []
    for item in invites:
        email, sep, role = item.rpartition(":")
        if not sep or "@" not in email:
            raise DoorError(f"--invite takes EMAIL:ROLE, got {_masked(item) if '@' in item else item!r}")
        if role not in INVITE_ROLES:
            raise DoorError(f"role {role!r} cannot be invited; one of: {', '.join(INVITE_ROLES)}"
                            + (" (the owner role is never given by invitation)" if role == "owner" else ""))
        invitations.append({"email": email, "role": role})
    path, meta = stash_of(org)
    roles: dict[str, int] = {}
    for i in invitations:
        roles[i["role"]] = roles.get(i["role"], 0) + 1
    plan = (f"create team {name!r} in the organization of {meta.get('team_name')}"
            + (f", inviting {len(invitations)}: " + ", ".join(f"{_masked(i['email'])} as {i['role']}"
                                                              for i in invitations) if invitations else ""))
    if not apply:
        print(f"dry run: {plan}\n  nothing was sent; repeat with --apply to create it")
        return 0
    body: dict = {"name": name}
    if invitations:
        body["invitations"] = invitations
    try:
        doc = _api("/v2/organizations/team", private_io.read(path).strip(), body)
    except ApiError as e:
        if e.status == 412:
            raise DoorError(f"not created: {org} is not an organization context (HTTP 412); "
                            "teams are created inside an organization") from None
        raise DoorError(f"not created: {e}") from None
    team = doc.get("team") or {}
    statuses = [i.get("status") for i in doc.get("invitations") or [] if isinstance(i, dict)]
    print(f"created team {team.get('name')!r} ({team.get('uuid')})"
          + (f"; invitations: " + ", ".join(f"{statuses.count(s)} {s}" for s in sorted(set(statuses)))
             if statuses else ""))
    _journal("team-create", f"digitalocean/{org}", team=team.get("uuid"), invitations=len(invitations))
    return 0


def cmd_presets() -> int:
    for key, p in PRESETS.items():
        print(f"{key}: {p['why']}")
        print(f"    scopes: {', '.join(p['scopes'])}")
        print(f"    proven on install by: {', '.join(_endpoint(x) for x in p['probes'])}"
              + (f"  (needs --{p['needs']})" if p.get("needs") else ""))
    print(f"scope names: {SCOPES_REFERENCE}")
    return 0


def _vault_spec(target: str | None) -> str | None:
    if target is None:
        return None
    parts = target.removeprefix("vault:").split("/")
    if len(parts) != 3 or not all(parts):
        raise DoorError("--vault must be PROJECT/ENV/NAME")
    return "vault:" + "/".join(parts)


def _label(preset: str, team: str, where: dict | None) -> str:
    if where is None:
        return f"{preset}--{team}"
    return f"{preset}--{where['project']}-{where['env']}-{where['name']}".lower()


def cmd_issue(preset: str, team: str, target: str | None, domain: str | None) -> int:
    p = PRESETS[preset]
    stash_of(team)
    print(f"digitalocean: DigitalOcean's public API cannot create personal access tokens: its reference "
          f"has no token endpoint ({API_REFERENCE}) and GET /v2/tokens answers 403 to a personal token.\n"
          f"  mint it in the control panel ({PANEL_TOKENS}) while signed in to team {team}:\n"
          f"    name: observatory {_label(preset, team, None) if not target else preset + ' ' + target}\n"
          f"    custom scopes: {', '.join(p['scopes'])}\n"
          f"  then install it from the clipboard, never through argv:\n"
          f"    pbpaste | python \"$(project-observatory full-path)/tools/digitalocean.py\" install "
          f"--preset {preset} --team {team}" + (f" --vault {target}" if target else "")
          + (f" --domain {domain}" if domain else ""), file=sys.stderr)
    return 3


def _prove(value: str, preset: str, domain: str | None) -> None:
    p = PRESETS[preset]
    for probe in p["probes"]:
        path = probe.format(domain=urllib.parse.quote(domain or "", safe=""))
        try:
            _api(path, value)
        except ApiError as e:
            if e.status == 404 and _endpoint(probe) in p.get("absent_ok", ()):
                continue
            raise DoorError(f"the token cannot do its preset's read {_endpoint(path)}: {e}") from None


#: Reads OUTSIDE a preset's scopes: a token that can do one of them is broader than declared.
#: Reads only; a 200 is the evidence (a 404 proves nothing either way).
BREADTH_PROBES = (("ssh_key:read", "/v2/account/keys?per_page=1"), ("tag:read", "/v2/tags?per_page=1"),
                  ("billing:read", "/v2/customers/my/balance"), ("droplet:read", "/v2/droplets?per_page=1"),
                  ("app:read", "/v2/apps?per_page=1"), ("domain:read", "/v2/domains?per_page=1"),
                  ("database:read", "/v2/databases?per_page=1"))


def _refuse_broad(value: str, preset: str) -> None:
    """A narrow preset must not carry a team's own token, nor any token wider than its scopes."""
    for label, path, _meta in stashes():
        if _same(private_io.read(path).strip(), value):
            raise DoorError(f"refused: this is the stashed token of team {label}, not a narrow token; mint a "
                            f"new one with the preset's scopes (`digitalocean.py presets`)")
    scopes = set(PRESETS[preset]["scopes"])
    for scope, path in BREADTH_PROBES:
        if scope in scopes:
            continue
        try:
            _api(path, value)
        except ApiError as e:
            # Only the provider saying "no" (401, 403) or "nothing there" (404) proves the token cannot
            # read it. A 429, a 5xx or a broken transport proves nothing, so the install stops.
            if e.status in (401, 403, 404):
                continue
            raise DoorError(f"could not prove the token is narrow: {_endpoint(path)} answered HTTP {e.status}; "
                            f"nothing was written and the installed value is kept; retry") from None
        except DoorError:
            raise DoorError(f"could not prove the token is narrow: {_endpoint(path)} could not be read; "
                            f"nothing was written and the installed value is kept; retry") from None
        raise DoorError(f"refused: the token can read {_endpoint(path)} ({scope}), which preset {preset} does "
                        f"not include; it is broader than declared (full access?). Mint it with only the "
                        f"preset's scopes")


def _verify_team(value: str, team_uuid: str | None, team: str) -> dict:
    try:
        acct = account_of(value)
    except ApiError as e:
        raise DoorError(f"refused: {e}") from None
    if not team_uuid or acct.get("team_uuid") != team_uuid:
        raise DoorError(f"refused: the token belongs to another team than {team!r} "
                        f"(it answers for {acct.get('team_name')!r})")
    return acct


def _deliver(value: str, rec: dict) -> str:
    if rec.get("vault"):
        return deliver(value, plan("vault:" + rec["vault"]))
    dest = READ_STORE / rec["team_label"]
    write_secret(dest, value)
    return f"collector token for {rec['team_label']} written (mode 600)"


def _current(rec: dict) -> str | None:
    if rec.get("vault"):
        slot = private_files._slot(*rec["vault"].split("/"))
        return _read_slot(slot, rec["vault"]) if slot.is_file() else None
    dest = READ_STORE / rec["team_label"]
    return private_io.read(dest).strip() if dest.is_file() else None


def cmd_install(preset: str, team: str, target: str | None, domain: str | None,
                project: str | None, expires_on: str | None) -> int:
    p = PRESETS[preset]
    if p.get("needs") == "domain" and not domain:
        raise DoorError(f"preset {preset} needs --domain (the one it is proven against)")
    vault_spec = _vault_spec(target)
    if p.get("destination") == "read-store":
        if vault_spec:
            raise DoorError("observatory-read goes to the collector's folder; drop --vault")
    elif not vault_spec:
        raise DoorError(f"preset {preset} is a project's credential: give --vault PROJECT/ENV/NAME")
    if sys.stdin.isatty():
        raise DoorError("give the token on stdin (e.g. `pbpaste | digitalocean.py install ...`)")
    value = one_value(sys.stdin.read())
    with records_lock():
        return _install_locked(value, preset, team, vault_spec, domain, project, expires_on)


def _install_locked(value: str, preset: str, team: str, vault_spec: str | None, domain: str | None,
                    project: str | None, expires_on: str | None) -> int:
    p = PRESETS[preset]
    _path, meta = stash_of(team)
    where = plan(vault_spec) if vault_spec else None       # the vault's own refusals, before anything
    label = _label(preset, team, where)
    rec_path = RECORDS / f"{label}.meta.json"
    prior = read_record(rec_path)
    if prior or (not where and (READ_STORE / team).exists()):
        # Installing over a live token would orphan it: still valid at DigitalOcean, no longer known here.
        raise DoorError(f"{label} is already installed; replace it with `digitalocean.py rotate {label}` "
                        f"(the old token is then revoked and proved dead)")
    acct = _verify_team(value, meta.get("team_uuid"), team)
    _prove(value, preset, domain)
    _refuse_broad(value, preset)
    rec = {"kind": "installed", "label": label, "preset": preset, "team_label": team,
           "team_uuid": acct["team_uuid"], "team_name": acct["team_name"],
           "vault": f"{where['project']}/{where['env']}/{where['name']}" if where else None,
           "destination": None if where else f"digitalocean-read.d/{team}",
           "project": project or prior.get("project"), "domain": domain,
           "scopes_declared": list(p["scopes"]),
           "proven_reads": [_endpoint(x) for x in p["probes"]],
           "note": "minted in the control panel; scopes are declared by the installer, reads are proven",
           "expires_on": expires_on or prior.get("expires_on"),
           "installed_on": prior.get("installed_on") or today(),
           "rotated_on": prior.get("rotated_on"), "rotations": prior.get("rotations") or 0,
           "issued_by": "tools/digitalocean.py"}
    said = _deliver(value, rec)
    RECORDS.mkdir(parents=True, exist_ok=True)
    write_record(rec_path, rec)
    _journal("issue", rec["vault"] or f"digitalocean/{team}/read", preset=preset, team=acct["team_uuid"])
    print(f"installed {label}: {preset} for team {acct['team_name']}; {said}")
    if rec["vault"]:
        project_, env, name = rec["vault"].split("/")
        print(f"  use: python \"$(project-observatory full-path)/tools/use_secret.py\" run --env {env} "
              f"{project_} {name} -- <command>")
    return 0


def revoke_and_prove(value: str) -> bool:
    """Ask the OAuth revocation endpoint, then the API: True only when the API refuses the value."""
    try:
        _wire("POST", revoke_url(), value, body=urllib.parse.urlencode({"token": value}).encode(),
              content_type="application/x-www-form-urlencoded")
    except DoorError:
        pass                                  # the proof below decides, not this answer
    try:
        account_of(value)
    except ApiError as e:
        return e.status == 401
    except DoorError:
        return False
    return False


def _record(label: str) -> tuple[pathlib.Path, dict]:
    path = RECORDS / f"{label}.meta.json"
    rec = read_record(path)
    if not rec:
        have = ", ".join(l for l, _p, _r in records()) or "none"
        raise DoorError(f"no installed token called {label!r} (have: {have})")
    return path, rec


def cmd_token_rotate(label: str) -> int:
    if sys.stdin.isatty():
        raise DoorError("give the NEW token on stdin; mint it in the control panel with the preset's scopes "
                        f"(`digitalocean.py presets`) at {PANEL_TOKENS}")
    value = one_value(sys.stdin.read())
    with records_lock():
        return _rotate_locked(label, value)


def _rotate_locked(label: str, value: str) -> int:
    rec_path, rec = _record(label)
    old = _current(rec)
    if old is not None and _same(old, value):
        raise DoorError("the new value is the one already installed; mint a new token")
    _verify_team(value, rec.get("team_uuid"), rec["team_label"])
    _prove(value, rec["preset"], rec.get("domain"))
    _refuse_broad(value, rec["preset"])
    said = _deliver(value, rec)
    rec.update(rotated_on=today(), rotations=(rec.get("rotations") or 0) + 1)
    write_record(rec_path, rec)
    _journal("rotate", rec.get("vault") or f"digitalocean/{rec['team_label']}/read", preset=rec["preset"])
    print(f"rotated {label}; {said}")
    if old is None:
        print("  old value: none was installed")
        return 0
    if not token_ok(old):
        # It was never sent, so nothing is known about it at DigitalOcean.
        print(f"  old value: could not be checked, the stored value was malformed (never sent); delete the old "
              f"token in the control panel ({PANEL_TOKENS})", file=sys.stderr)
        return 2
    if revoke_and_prove(old):
        print("  old value: revoked (DigitalOcean now refuses it)")
        return 0
    print(f"  old value: STILL ACCEPTED by DigitalOcean; delete it in the control panel ({PANEL_TOKENS})",
          file=sys.stderr)
    return 2


def cmd_revoke(label: str, forget: bool = False) -> int:
    with records_lock():
        return _revoke_locked(label, forget)


def _revoke_locked(label: str, forget: bool) -> int:
    rec_path, rec = _record(label)
    value = _current(rec)
    where = rec.get("vault") or rec.get("destination")
    if value is None:
        # No value, no proof: DigitalOcean has no endpoint to revoke a personal token by id, so the
        # door cannot say whether the token still lives. The record is the only trace of it.
        if not forget:
            raise DoorError(f"{label}: its value is gone from {where}, so the door cannot revoke it or prove it "
                            f"dead; the record is kept. Delete the token in the control panel ({PANEL_TOKENS}), "
                            f"then `digitalocean.py revoke {label} --forget` drops the record")
        rec_path.unlink()
        _journal("forget", rec.get("vault") or f"digitalocean/{rec['team_label']}/read", preset=rec["preset"])
        print(f"forgot {label}: record removed; this door did NOT observe a revocation (its value was gone)")
        return 0
    if not token_ok(value):
        raise DoorError(f"{label}: the stored value is malformed and was never sent, so it could not be checked; "
                        f"nothing was removed here. Delete the token in the control panel ({PANEL_TOKENS}), "
                        f"then `digitalocean.py revoke {label} --forget`")
    if not revoke_and_prove(value):
        raise DoorError(f"DigitalOcean still accepts {label}; nothing was removed here. Delete the token in "
                        f"the control panel ({PANEL_TOKENS}), then run revoke again")
    if rec.get("vault"):
        project, env, name = rec["vault"].split("/")
        p = subprocess.run([sys.executable, str(ROOT / "tools" / "vault.py"), "remove", project, env, name],
                           capture_output=True, text=True, timeout=60)
        if p.returncode != 0 and private_files._slot(project, env, name).is_file():
            raise DoorError(f"revoked at DigitalOcean, but the vault kept {rec['vault']} (exit {p.returncode}; "
                            f"an open leak must be settled first); the record is kept")
    else:
        (READ_STORE / rec["team_label"]).unlink(missing_ok=True)
    rec_path.unlink()
    _journal("revoke", rec.get("vault") or f"digitalocean/{rec['team_label']}/read", preset=rec["preset"])
    print(f"revoked {label} at DigitalOcean (now refused) and removed it here")
    return 0


def cmd_tokens_list() -> int:
    st, rs = stashes(), records()
    print(f"team tokens ({len(st)}), read by this door only:")
    for label, path, meta in st:
        print(f"  {label:24} {meta.get('team_name')!s:24} role {meta.get('role') or 'unknown':10} "
              f"stashed {meta.get('stashed_on', '?')} from {meta.get('source', '?')} mode {oct(path.stat().st_mode)[-3:]}")
    print(f"installed tokens ({len(rs)}):")
    for label, _p, rec in rs:
        print(f"  {label:40} {rec.get('preset'):16} team {rec.get('team_label')}  "
              f"-> {rec.get('vault') or rec.get('destination')}  {rec.get('project') or '(no project)'}  "
              f"installed {rec.get('installed_on')} rotated {rec.get('rotated_on') or '-'} x{rec.get('rotations', 0)}"
              + (f" expires {rec['expires_on']}" if rec.get("expires_on") else ""))
    if not st:
        print("start with `digitalocean.py stash < token-file` or `stash --from-doctl all`")
    return 0


def cmd_ping() -> int:
    bad = 0
    for label, path, meta in stashes():
        try:
            acct = account_of(private_io.read(path).strip())
            same = acct.get("team_uuid") == meta.get("team_uuid")
            print(f"  team/{label}: alive" + ("" if same else " but answers for ANOTHER team"))
            bad += 0 if same else 1
        except DoorError as e:
            print(f"  team/{label}: DEAD: {e}", file=sys.stderr)
            bad += 1
    for label, _p, rec in records():
        value = _current(rec)
        if value is None:
            print(f"  {label}: its value is missing at {rec.get('vault') or rec.get('destination')}", file=sys.stderr)
            bad += 1
            continue
        try:
            _verify_team(value, rec.get("team_uuid"), rec["team_label"])
            _prove(value, rec["preset"], rec.get("domain"))
            print(f"  {label}: alive, proven reads pass (last change {rec.get('rotated_on') or rec.get('installed_on')})")
        except DoorError as e:
            print(f"  {label}: DEAD: {e}", file=sys.stderr)
            bad += 1
    if not stashes() and not records():
        print("nothing stashed and nothing installed")
    return 1 if bad else 0


def team_tokens() -> list[tuple[str, dict, str, str]]:
    """(label, record, value, kind) per stashed team, for the collector: the team's
    observatory-read token where one is installed, else its stashed token. Values stay in memory."""
    out = []
    for label, path, meta in stashes():
        read = READ_STORE / label
        if read.is_file():
            out.append((label, meta, private_io.read(read).strip(), "observatory-read"))
        else:
            out.append((label, meta, private_io.read(path).strip(), "stash"))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="area", required=True)
    db = sub.add_parser("db", help="managed databases").add_subparsers(dest="cmd", required=True)
    db.add_parser("list", help="clusters, users and exposure — no values")
    p = db.add_parser("consumers", help="apps that bind a cluster")
    p.add_argument("cluster")
    for name, text in (("credential", "deliver a user's connection URL to the vault"),
                       ("rotate", "reset a user's password and move every consumer you name to it; the consumers are "
                                 "down between the reset and their new deployment reaching ACTIVE. Exit 0 done, "
                                 "1 refused before the reset (nothing changed), after the reset: 2 a deployment "
                                 "not ACTIVE, 3 the vault did not take the value, 4 an app not updated or "
                                 "redeployed, 5 reset outcome unknown (do not repeat reset), 6 receipt update failed; "
                                 "several: the largest wins. Opaque literals are refused; plain literals must match "
                                 "the selected user's current provider credential and connection metadata.")):
        p = db.add_parser(name, help=text, description=text)
        p.add_argument("cluster")
        p.add_argument("user")
        p.add_argument("--db", help="database name in the URL (pg/mysql); default defaultdb")
        p.add_argument("--to", required=True, help="vault:<project>/<env>/<NAME>")
        if name == "rotate":
            p.add_argument("--app", action="append", default=[], help="App Platform app that binds the cluster")
            p.add_argument("--spec-env", action="append", default=[],
                           help="APP_ID:KEY — plain literal proven by current provider credential; encrypted values refused")
            p.add_argument("--wait", type=float, default=900)
    # The account half (OBS-155): teams, their tokens, and the narrow tokens installed from them.
    p = sub.add_parser("stash", help="a team's own token on stdin (or from a doctl context); never handed out")
    p.add_argument("--from-doctl", metavar="CONTEXT", help="import doctl's context by name, or `all`")
    p = sub.add_parser("teams", help="every stashed team: role, record, the vault slots that belong to it")
    p.add_argument("--probe", action="store_true", help="ask the provider which team an unmatched slot is")
    p = sub.add_parser("members", help="what the public API says about a team's members")
    p.add_argument("--team", required=True)
    team = sub.add_parser("team", help="team management").add_subparsers(dest="cmd", required=True)
    p = team.add_parser("create", help="a new team in an organization, with invitations; plans unless --apply")
    p.add_argument("name")
    p.add_argument("--org", required=True, help="the stashed team whose token is in the organization context")
    p.add_argument("--invite", action="append", default=[], metavar="EMAIL:ROLE",
                   help=f"repeatable; ROLE one of: {', '.join(INVITE_ROLES)}")
    p.add_argument("--apply", action="store_true", help="send it; without it the plan is printed")
    sub.add_parser("presets", help="the scopes each preset is minted with")
    for name, text in (("issue", "mint a narrow token (refused: the public API cannot; prints the panel recipe)"),
                       ("install", "a narrow token minted in the control panel, on stdin: verified, delivered, recorded")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--preset", required=True, choices=sorted(PRESETS))
        p.add_argument("--team", required=True, help="the stashed team it must belong to")
        p.add_argument("--vault", help="PROJECT/ENV/NAME the token is delivered to")
        p.add_argument("--domain", help="dns-edit: the domain it is proven against")
        if name == "install":
            p.add_argument("--project", help="the project this token serves")
            p.add_argument("--expires-on", help="the expiry set in the control panel (YYYY-MM-DD), recorded")
    sub.add_parser("list", help="stashed and installed tokens, with dates; never values")
    sub.add_parser("ping", help="does every token still answer for its team and do its reads")
    p = sub.add_parser("rotate", help="a NEW token on stdin replaces an installed one; the old is revoked and proved dead")
    p.add_argument("label")
    p = sub.add_parser("revoke", help="revoke at DigitalOcean, prove it, then remove here")
    p.add_argument("label")
    p.add_argument("--forget", action="store_true",
                   help="only when the value is gone and the token was deleted in the control panel: drop the record")
    a = ap.parse_args(argv)
    try:
        if a.area == "db":
            if a.cmd == "list":
                return cmd_list()
            if a.cmd == "consumers":
                return cmd_consumers(a.cluster)
            if a.cmd == "credential":
                return cmd_credential(a.cluster, a.user, a.db, a.to)
            return cmd_rotate(a.cluster, a.user, a.db, a.to, a.app, a.spec_env, a.wait)
        api_base()                     # an override that is not loopback stops here, before any token is read
        if a.area == "stash":
            return cmd_stash(a.from_doctl)
        if a.area == "teams":
            return cmd_teams(a.probe)
        if a.area == "members":
            return cmd_members(a.team)
        if a.area == "team":
            return cmd_team_create(a.name, a.org, a.invite, a.apply)
        if a.area == "presets":
            return cmd_presets()
        if a.area == "issue":
            return cmd_issue(a.preset, a.team, a.vault, a.domain)
        if a.area == "install":
            return cmd_install(a.preset, a.team, a.vault, a.domain, a.project, a.expires_on)
        if a.area == "list":
            return cmd_tokens_list()
        if a.area == "ping":
            return cmd_ping()
        if a.area == "rotate":
            return cmd_token_rotate(a.label)
        return cmd_revoke(a.label, a.forget)
    except DoorError as e:
        print(f"digitalocean: {e}", file=sys.stderr)
        return 1
    except RuntimeError:
        # private_io's refusals (a wide or symlinked credential file) carry no value; say which class.
        print("digitalocean: a private credential file was refused (mode or symlink); values hidden",
              file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001  a message can quote a value: only the class is shown
        print(f"digitalocean: failed ({type(e).__name__}); details withheld so no value can be printed",
              file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
