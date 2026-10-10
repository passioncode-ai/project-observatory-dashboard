#!/usr/bin/env python3
"""DigitalOcean door: managed-database credentials go to the vault, never the transcript.

Why a door. On 2026-10-10 one call of the DigitalOcean MCP tool `db-cluster-list`,
asked only for names, returned the plaintext admin and user passwords of every
database cluster in the team. DigitalOcean's API puts a password in every cluster
and user object it returns, so anything that prints the API's answer leaks. This
door is the one place that reads those objects: it keeps the password in memory,
delivers it to the vault on stdin, and prints names, ids and phases.

  digitalocean.py db list                         # clusters, engines, exposure — no values
  digitalocean.py db consumers CLUSTER            # App Platform apps that use it, and how
  digitalocean.py db credential CLUSTER USER --db NAME --to vault:<project>/<env>/<NAME>
  digitalocean.py db rotate CLUSTER USER --db NAME --to vault:<project>/<env>/<NAME>
        [--app APP_ID ...] [--spec-env APP_ID:KEY ...] [--wait 900]

`rotate` order (no downtime by construction where it can be had):
  1. read everything it will need BEFORE the reset — cluster, every consumer spec,
     their current deployments; a failure here stops with nothing changed;
  2. reset the user's password (the one step not retried: a second reset would
     orphan the first value);
  3. deliver the new connection URL to the vault;
  4. write the password into each `--spec-env` literal (an app that keeps it as its
     own SECRET instead of a `${db.*}` binding) — `apps update` redeploys it;
  5. redeploy every other `--app` (a binding is resolved at deploy time);
  6. wait for each new deployment to be ACTIVE and report.
Every API call but the reset is retried; App Platform answers 503 now and then, and
a rotation that stops between steps 2 and 5 leaves a live app on a dead password.

Credential: `doctl`'s own authenticated context (the operator's). `DOCTL` overrides
the binary (tests). Values: never printed, never in argv; specs go through a 0600
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
        p = subprocess.run([DOCTL, *args, "--output", "json"], capture_output=True, text=True)
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


def deliver(value: str, to: str) -> str:
    """Into the vault on stdin; `rotate` when the slot exists, so the old value is archived."""
    project, env, name = vault_target(to)
    vault = [sys.executable, str(ROOT / "tools" / "vault.py")]
    listed = subprocess.run([*vault, "list", project, env], capture_output=True, text=True).stdout
    verb = "rotate" if f"{project}/{env}/{name}" in listed else "put"
    p = subprocess.run([*vault, verb, project, env, name], input=value, capture_output=True, text=True)
    if p.returncode != 0:
        raise DoorError(f"vault {verb} {project}/{env}/{name} refused: {p.stderr.strip()[-200:]}")
    return f"vault {verb} {project}/{env}/{name}"


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
                    literals.append(f"{scope.get('name', 'app')}:{e['key']}")
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
    vault_target(to)
    c = cluster(ref)
    pw = user_password(c, user)
    print(f"{deliver(connection_url(c, user, pw, db), to)}: {c['name']} user {user}")
    return 0


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
    vault_target(to)
    # 1. everything we need, before anything changes
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
            raise DoorError(f"app {app[:8]} has no env {missing}: nothing was reset")
    every = list(dict.fromkeys([*apps, *literal]))
    before = {app: deployments(app) for app in every}
    # 2. the reset — once
    reset = one(doctl("databases", "user", "reset", c["id"], user, tries=1))
    pw = (reset or {}).get("password")
    if not pw:
        raise DoorError("the reset answered without a password; check the user in the console")
    print(f"reset {c['name']} user {user}")
    # 3. the vault
    print(deliver(connection_url(c, user, pw, db), to))
    # 4. literals → apps update (redeploys)
    for app, keys in literal.items():
        spec = specs[app]
        for scope in [spec, *[x for k in ("services", "workers", "jobs") for x in spec.get(k) or []]]:
            for e in scope.get("envs") or []:
                if e["key"] in keys:
                    e["value"], e["type"] = pw, "SECRET"
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(spec, f)
            doctl("apps", "update", app, "--spec", path)
        finally:
            os.remove(path)
        print(f"  app {app[:8]}: {', '.join(keys)} replaced in its spec")
    del pw
    # 5. bindings → redeploy
    for app in apps:
        if app not in literal:
            doctl("apps", "create-deployment", app)
    # 6. wait
    failed = False
    for app in every:
        state = wait_new(app, before[app], wait)
        failed |= not state.endswith("ACTIVE")
        print(f"  app {app[:8]} deployment {state}")
    if failed:
        print("A consumer did not reach ACTIVE: its running copy still holds the OLD password. "
              f"Fix it now: `doctl apps logs <app> --type deploy`.", file=sys.stderr)
        return 2
    project, env, name = vault_target(to)
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
                       ("rotate", "reset a user's password and move every consumer to it")):
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
