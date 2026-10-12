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
            return json.loads(p.stdout) if p.stdout.strip() else None
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
    return {"project": project, "env": env, "name": name, "slot": slot,
            "verb": "rotate" if exists else "put"}


def deliver(value: str, where: dict) -> str:
    """Into the vault on stdin: `rotate` when the slot exists, so the old value is archived.

    Returns what the vault said, which for `rotate` includes the services started
    from the slot that still hold the old value — the list a person restarts."""
    target = f"{where['project']}/{where['env']}/{where['name']}"
    if where["verb"] == "rotate" and private_files._read_private(where["slot"]).strip() == value:
        return f"vault {target}: unchanged (it already holds this value)"
    vault = [sys.executable, str(ROOT / "tools" / "vault.py")]
    p = subprocess.run([*vault, where["verb"], where["project"], where["env"], where["name"]],
                       input=value, capture_output=True, text=True)
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


URLISH = ("_URL", "DATABASE_URL", "_DSN", "_URI")


def literal_form(key: str, value: str) -> str:
    """`url` when the app keeps a connection URL under KEY, else `password`.

    A SECRET's value is encrypted in the spec (`EV[…]`), so the name decides
    unless the value is plain and itself a URL. Writing a bare password where
    the app expects `postgresql://…` breaks the app as surely as the old one."""
    if any(k in key.upper() for k in URLISH) or "://" in value:
        return "url"
    return "password"


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
                if "${" not in v and any(s in e["key"] for s in ("PASSWORD", "DATABASE_URL", "DB_PASS")):
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
    if not found:
        print("no App Platform app binds this cluster (droplets, Workers and laptops are not visible here)")
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


def context_line() -> str:
    """Which DigitalOcean account the reset will act in — names, never the email."""
    acct = doctl("account", "get") or {}
    team = ((acct.get("team") or {}).get("name") if isinstance(acct, dict) else None) or "personal account"
    ctx = os.environ.get("DIGITALOCEAN_CONTEXT") or "current"
    return f"doctl context: {ctx}; team: {team}"


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


def cmd_rotate(ref: str, user: str, db: str | None, to: str, apps: list[str],
               spec_envs: list[str], wait: float) -> int:
    # 1. everything we need, before anything changes
    try:
        where = plan(to)
    except DoorError as e:
        raise DoorError(f"{e}: nothing was reset") from None
    print(context_line())
    c = cluster(ref)
    literal = {}
    for item in spec_envs:
        app, _, key = item.partition(":")
        if not key:
            raise DoorError(f"--spec-env is APP_ID:KEY, got {item!r}")
        literal.setdefault(app, []).append(key)
    specs = {app: doctl("apps", "spec", "get", app, "--format", "json") for app in literal}
    for app, keys in literal.items():
        present = {e["key"] for e in specs[app].get("envs") or []} | {
            e["key"] for k in ("services", "workers", "jobs") for x in specs[app].get(k) or []
            for e in x.get("envs") or []}
        missing = [k for k in keys if k not in present]
        if missing:
            raise DoorError(f"app {app} has no env {missing}: nothing was reset")
    every = list(dict.fromkeys([*apps, *literal]))
    before = {app: deployments(app) for app in every}
    # 2. the reset — once
    reset = one(doctl("databases", "user", "reset", c["id"], user, tries=1))
    pw = (reset or {}).get("password")
    if not pw:
        raise DoorError("the reset answered without a password; check the user in the console")
    print(f"reset {c['name']} user {user}")
    url = connection_url(c, user, pw, db)
    # 3. the vault — a refusal now does not stop the apps from moving
    vault_failed = None
    try:
        print(deliver(url, where))
    except DoorError as e:
        vault_failed = str(e)
        print(f"digitalocean: the password of {user} on {c['name']} WAS reset, but the vault did not take "
              f"it ({e}); moving the apps anyway so none sits on a dead password", file=sys.stderr)
    # 4. literals → apps update (redeploys)
    done: list[str] = []
    for app, keys in literal.items():
        spec = specs[app]
        for scope in [spec, *[x for k in ("services", "workers", "jobs") for x in spec.get(k) or []]]:
            for e in scope.get("envs") or []:
                if e["key"] in keys:
                    form = literal_form(e["key"], str(e.get("value", "")))
                    e["value"], e["type"] = (url if form == "url" else pw), "SECRET"
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(spec, f)
            doctl("apps", "update", app, "--spec", path)
        except DoorError as e:
            left = [a for a in literal if a not in done]
            raise DoorError(f"{e}. The password WAS reset; apps done: {', '.join(done) or 'none'}; apps left: "
                            f"{', '.join(left)} — those still hold the OLD password. Next: rerun this command "
                            f"with only their --spec-env, or set the key in each app by hand") from None
        finally:
            os.remove(path)
        done.append(app)
        print(f"  app {app}: {', '.join(keys)} replaced in its spec")
    del pw, url
    # 5. bindings → redeploy
    for app in apps:
        if app not in literal:
            doctl("apps", "create-deployment", app)
    # 6. wait
    failed = []
    for app in every:
        state = wait_new(app, before[app], wait)
        if not state.endswith("ACTIVE"):
            failed.append(app)
        print(f"  app {app} deployment {state}")
    project, env, name = where["project"], where["env"], where["name"]
    if failed:
        print("A consumer did not reach ACTIVE: its running copy still holds the OLD password. Fix it now: "
              + "; ".join(f"`doctl apps logs {app} --type deploy`" for app in failed), file=sys.stderr)
    if vault_failed:
        print(f"next: put the current password in the vault — `digitalocean.py db credential {ref} {user}"
              + (f" --db {db}" if db else "") + f" --to {to}` — it reads the reset value from the API",
              file=sys.stderr)
    if failed or vault_failed:
        return 2
    print(f"next: verify each consumer's health, then `vault.py settle {project} {env} {name} "
          "--how … --revocation-evidence … --consumer-evidence …` if this closes a leak")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="area", required=True)
    db = sub.add_parser("db", help="managed databases").add_subparsers(dest="cmd", required=True)
    db.add_parser("list", help="clusters, users and exposure — no values")
    p = db.add_parser("consumers", help="apps that bind a cluster")
    p.add_argument("cluster")
    for name, text in (("credential", "deliver a user's connection URL to the vault"),
                       ("rotate", "reset a user's password and move every consumer you name to it")):
        p = db.add_parser(name, help=text)
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
