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
`vault.py rotate`, whose list this prints). Order — no downtime by construction where
it can be had:
  1. read everything it will need BEFORE the reset — the doctl team, the cluster,
     every consumer spec, their current deployments — and ask the vault whether the
     slot exists (put or rotate) and can be written; any doubt stops with nothing
     changed;
  2. reset the user's password (the one step not retried: a second reset would
     orphan the first value);
  3. deliver the new connection URL to the vault; if the vault refuses now, say the
     password WAS reset and keep moving the apps, so none sits on a dead password;
  4. write the new value into each `--spec-env` literal (an app that keeps it as its
     own SECRET instead of a `${db.*}` binding): the connection URL for a key named
     `*_URL`/`*DATABASE_URL*`/`*_DSN` or holding a URL, the bare password otherwise —
     `apps update` redeploys it; a failure names the apps done and the apps left;
  5. redeploy every other `--app` (a binding is resolved at deploy time);
  6. wait for each new deployment to be ACTIVE and report.
Every API call but the reset is retried; App Platform answers 503 now and then, and
a rotation that stops between steps 2 and 5 leaves a live app on a dead password.

Credential: `doctl`'s own authenticated context (the operator's); `rotate` prints the
context and team before it resets anything. `DOCTL` overrides the binary (tests). Values: never printed, never in argv; specs go through a 0600
temporary file only for `doctl apps update`, removed in `finally`.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.parse

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
    """JSON from doctl. stderr is reported by its last line only: doctl does not
    echo credentials there, but an HTML 503 page is long and says nothing."""
    last = ""
    for attempt in range(tries):
        try:
            p = subprocess.run([DOCTL, *args, "--output", "json"], capture_output=True, text=True)
        except FileNotFoundError:
            raise DoorError("doctl not found; install it and run `doctl auth init`") from None
        if p.returncode == 0:
            try:
                return json.loads(p.stdout) if p.stdout.strip() else None
            except ValueError:
                raise NotJson(f"doctl {' '.join(args[:3])} answered with something that is not JSON") from None
        last = (p.stderr.strip().splitlines() or ["no stderr"])[-1][:200]
        if attempt + 1 < tries:
            time.sleep(RETRY_DELAY)
    raise DoorError(f"doctl {' '.join(args[:3])} failed after {tries} tries: {last}")


def one(x):
    return x[0] if isinstance(x, list) else x


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
        raise DoorError(f"--to is vault:<project>/<env>/<NAME>, got {to!r}")
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
        raise DoorError(f"the vault will not read vault:{target} ({type(e).__name__}: "
                        f"{str(e).splitlines()[0][:200] if str(e) else 'no reason given'})") from None


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
        # THE FIRST LINE is the vault's reason; the tail of stderr was a hint.
        first = (p.stderr.strip().splitlines() or ["no reason given"])[0][:300]
        raise DoorError(f"vault {where['verb']} {target} refused: {first}")
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
    pw = (u or {}).get("password")
    if not pw:
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
        team_text = f"team: {team}"
    except DoorError:
        team_text = "team unknown (doctl account get failed)"
    return f"doctl context: {current_context()}; {team_text}"


def deployments(app: str) -> set[str]:
    return {d["id"] for d in doctl("apps", "list-deployments", app) or []}


def wait_new(app: str, before: set[str], timeout: float) -> str:
    end = time.monotonic() + timeout
    phase = "PENDING"
    while time.monotonic() < end:
        new = [d for d in doctl("apps", "list-deployments", app) or [] if d["id"] not in before]
        phase = new[0].get("phase", "PENDING") if new else "PENDING"
        if phase in ("ACTIVE", "ERROR", "CANCELED", "SUPERSEDED"):
            return f"{new[0]['id'][:8]} {phase}"
        time.sleep(POLL)
    return f"? still {phase} after {int(timeout)} s"


#: Exit codes of `db rotate`. 1 is a refusal before the reset: nothing changed.
#: Every other non-zero code is a failure AFTER the reset; when several happen the
#: largest is returned, and each is named on stderr with its next step.
EXIT_NOT_ACTIVE = 2       # a deployment did not reach ACTIVE
EXIT_VAULT = 3            # the vault did not take the new value
EXIT_APP = 4              # an app could not be updated or redeployed
EXIT_RESET = 5            # the reset answered without a password


def _scopes(spec: dict) -> list[dict]:
    return [spec, *[x for k in ("services", "workers", "jobs") for x in spec.get(k) or []]]


def check_literal(app: str, key: str, value: str, user: str, db: str | None) -> None:
    """Refuse, before the reset, a literal this run could only get wrong."""
    if literal_form(key, value) != "url":
        return
    if "://" in value:
        with_password(value, "x")                    # a URL we cannot rewrite is refused now
        owner = urllib.parse.urlsplit(value).username
        if owner != user:
            # ANOTHER USER'S URL (review round 3): writing the rotated user's new
            # password beside `reporting` broke that app and was reported as done.
            raise DoorError(f"app {app} keeps {key} for user {owner!r}, not {user!r}; leave it out of "
                            f"--spec-env, or rotate {owner!r} on its own")
    elif not db:
        raise DoorError(f"app {app} keeps {key} as an encrypted URL; its database cannot be read from the "
                        f"spec, so give --db NAME")


def cmd_rotate(ref: str, user: str, db: str | None, to: str, apps: list[str],
               spec_envs: list[str], wait: float) -> int:
    # 1. everything we need, before anything changes
    try:
        where = plan(to)
        print(context_line())
        c = cluster(ref)
        literal: dict[str, list[str]] = {}
        for item in spec_envs:
            app, _, key = item.partition(":")
            if not key:
                raise DoorError(f"--spec-env is APP_ID:KEY, got {item!r}")
            literal.setdefault(app, []).append(key)
        specs = {app: doctl("apps", "spec", "get", app, "--format", "json") for app in literal}
        for app, keys in literal.items():
            # EVERY LEVEL, not a merged view (review round 3): a key set at app
            # AND service level is two literals, and step 4 writes both.
            levels = [e for scope in _scopes(specs[app]) for e in scope.get("envs") or []]
            missing = [k for k in keys if k not in {e["key"] for e in levels}]
            if missing:
                raise DoorError(f"app {app} has no env {missing}")
            for e in levels:
                if e["key"] in keys:
                    check_literal(app, e["key"], str(e.get("value", "")), user, db)
        every = list(dict.fromkeys([*apps, *literal]))
        before = {app: deployments(app) for app in every}
    except DoorError as e:
        raise DoorError(f"{e}: nothing was reset") from None
    # 2. the reset — once
    try:
        reset = one(doctl("databases", "user", "reset", c["id"], user, tries=1))
    except NotJson:
        reset = None                                  # it answered rc 0: the reset happened
    pw = (reset or {}).get("password") if isinstance(reset, dict) else None
    if not pw:
        print(f"digitalocean: the password of {user} on {c['name']} WAS reset, but the answer carried no "
              f"password, so no consumer was moved. Next: `digitalocean.py db credential {ref} {user}"
              + (f" --db {db}" if db else "") + f" --to {to}` reads the new value into the vault; then set each "
              f"consumer's key by hand and redeploy each app that binds the cluster", file=sys.stderr)
        return EXIT_RESET
    print(f"reset {c['name']} user {user}")
    url = connection_url(c, user, pw, db)
    code = 0
    vault_failed = False
    # 3. the vault — a refusal now does not stop the apps from moving
    try:
        print(deliver(url, where))
    except DoorError as e:
        code, vault_failed = max(code, EXIT_VAULT), True
        print(f"digitalocean: the password of {user} on {c['name']} WAS reset, but the vault did not take "
              f"it ({e}); moving the apps anyway so none sits on a dead password", file=sys.stderr)
    # 4. literals → apps update (redeploys); 5. bindings → redeploy. A failure is
    # recorded and the rest still move: stopping would leave them on a dead password.
    done: list[str] = []
    left: list[str] = []
    for app, keys in literal.items():
        spec = specs[app]
        path = None
        try:
            for scope in _scopes(spec):
                for e in scope.get("envs") or []:
                    if e["key"] in keys:
                        old_value = str(e.get("value", ""))
                        if literal_form(e["key"], old_value) == "password":
                            e["value"] = pw
                        elif "://" in old_value:
                            e["value"] = with_password(old_value, pw)
                        else:
                            e["value"] = url
                        if literal_form(e["key"], old_value) == "url":
                            print(f"  app {app}: {e['key']} → {url_place(e['value'])}")
                        e["type"] = "SECRET"
            fd, path = tempfile.mkstemp(suffix=".json")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(spec, f)
            doctl("apps", "update", app, "--spec", path)
            done.append(app)
            print(f"  app {app}: {', '.join(keys)} replaced in its spec")
        except DoorError as e:
            left.append(app)
            print(f"digitalocean: app {app} was not updated: {e}", file=sys.stderr)
        finally:
            if path:
                os.remove(path)
    del pw, url
    for app in apps:
        if app in literal:
            continue
        try:
            doctl("apps", "create-deployment", app)
            done.append(app)
        except DoorError as e:
            left.append(app)
            print(f"digitalocean: app {app} was not redeployed: {e}", file=sys.stderr)
    if left:
        code = max(code, EXIT_APP)
        print(f"The database password WAS reset. apps done: {', '.join(done) or 'none'}; apps left: "
              f"{', '.join(left)} — those still hold the OLD password. Next: `digitalocean.py db credential "
              f"{ref} {user}" + (f" --db {db}" if db else "") + f" --to {to}` puts the current value in the "
              f"vault; then, by hand, set each left app's key in its spec (`doctl apps update APP --spec FILE`; "
              f"a redeploy alone keeps an old literal) and redeploy each left app that only binds the cluster "
              f"(`doctl apps create-deployment APP`)", file=sys.stderr)
    # 6. wait
    failed = []
    for app in [a for a in every if a not in left]:
        try:
            state = wait_new(app, before[app], wait)
        except DoorError as e:
            # AFTER THE RESET a failed wait is not "nothing changed" (review round
            # 3): it is this app unconfirmed, and the others are still checked.
            state = f"unknown ({e})"
        if not state.endswith("ACTIVE"):
            failed.append(app)
        print(f"  app {app} deployment {state}")
    if failed:
        code = max(code, EXIT_NOT_ACTIVE)
        print("The database password WAS reset, and a consumer is not confirmed ACTIVE: its running copy may "
              "still hold the OLD password. Check it now: "
              + "; ".join(f"`doctl apps logs {app} --type deploy`" for app in failed), file=sys.stderr)
    if vault_failed and not left:
        print(f"next: put the current password in the vault — `digitalocean.py db credential {ref} {user}"
              + (f" --db {db}" if db else "") + f" --to {to}` — it reads the reset value from the API",
              file=sys.stderr)
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
                                 "redeployed, 5 the reset returned no password; several: the largest wins")):
        p = db.add_parser(name, help=text, description=text)
        p.add_argument("cluster")
        p.add_argument("user")
        p.add_argument("--db", help="database name in the URL (pg/mysql); default defaultdb")
        p.add_argument("--to", required=True, help="vault:<project>/<env>/<NAME>")
        if name == "rotate":
            p.add_argument("--app", action="append", default=[], help="App Platform app that binds the cluster")
            p.add_argument("--spec-env", action="append", default=[],
                           help="APP_ID:KEY — an app env holding the password itself")
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
