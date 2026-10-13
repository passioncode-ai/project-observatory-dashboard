#!/usr/bin/env python3
"""DigitalOcean door: managed-database credentials go to the vault, never the transcript.

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
    return k.endswith(("_DSN", "_URI")) and value.lower().startswith(DB_SCHEMES)


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
    where = plan(to)
    c = cluster(ref)
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


def validate_connection(c: dict, user: str, db: str | None) -> None:
    checked_name(c.get("id"), "cluster id")
    checked_name(c.get("name"), "cluster name")
    conn = c.get("connection")
    if (c.get("engine") not in ("pg", "mysql", "redis", "valkey") or not isinstance(conn, dict)
            or not isinstance(conn.get("host"), str) or not conn["host"]
            or type(conn.get("port")) is not int or not 0 < conn["port"] < 65536):
        raise DoorError("unsupported or incomplete database connection metadata")
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
            for e in levels:
                if e["key"] in keys:
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
                    if e["key"] in keys:
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
    a = ap.parse_args(argv)
    try:
        if a.cmd == "list":
            return cmd_list()
        if a.cmd == "consumers":
            return cmd_consumers(a.cluster)
        if a.cmd == "credential":
            return cmd_credential(a.cluster, a.user, a.db, a.to)
        return cmd_rotate(a.cluster, a.user, a.db, a.to, a.app, a.spec_env, a.wait)
    except DoorError as e:
        print(f"digitalocean: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
