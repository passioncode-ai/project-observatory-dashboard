#!/usr/bin/env python3
"""The DigitalOcean door against a fake `doctl` and a scratch vault.

Each check is a way the 2026-10-10 rotation went wrong or could have:
1. a value in stdout/stderr — the door exists because the API's answer leaks;
2. a 503 from App Platform between the reset and the redeploy left a live site on a
   dead password for five minutes — every call but the reset is retried;
3. a literal `DB_PASSWORD` in an app's own spec is not refreshed by a redeploy — it is
   written into the spec in the same run;
4. a precondition that fails AFTER the reset strands the consumers — everything the
   rotation needs is read before it.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOOR = ROOT / "tools" / "digitalocean.py"
FAILURES: list[str] = []
OLD, NEW = "old-pw-" + "x" * 20, "new-pw-" + "y" * 20


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


FAKE = r'''#!/usr/bin/env python3
import json, os, sys, pathlib
st = pathlib.Path(os.environ["FAKE_DO_STATE"])
s = json.loads(st.read_text())
args = [a for a in sys.argv[1:] if a not in ("--output", "json")]
s["calls"].append(args)
def out(x):
    st.write_text(json.dumps(s)); print(json.dumps(x)); sys.exit(0)
def fail(msg):
    st.write_text(json.dumps(s)); print(msg, file=sys.stderr); sys.exit(1)
key = " ".join(args[:3])
if s["flaky"].get(key, 0) > 0:
    s["flaky"][key] -= 1; fail("Error: 503 Service Unavailable")
c = s["cluster"]
if args[:2] == ["databases", "list"]:
    out([c])
if args[:3] == ["databases", "firewalls", "list"]:
    out([])
if args[:3] == ["databases", "user", "get"]:
    out([{"name": args[4], "password": s["password"]}])
if args[:3] == ["databases", "user", "reset"]:
    s["password"] = s["next"]; c["users"][0]["password"] = s["next"]; out([{"name": args[4], "password": s["next"]}])
if args[:2] == ["apps", "list"]:
    out(s["apps"])
if args[:3] == ["apps", "spec", "get"]:
    out(s["specs"][args[3]])
if args[:2] == ["apps", "update"]:
    spec = json.loads(pathlib.Path(args[args.index("--spec") + 1]).read_text())
    s["specs"][args[2]] = spec; s["deps"][args[2]].insert(0, {"id": "dep-new-" + args[2], "phase": "ACTIVE"}); out([{}])
if args[:2] == ["apps", "create-deployment"]:
    s["deps"][args[2]].insert(0, {"id": "dep-new-" + args[2], "phase": "ACTIVE"}); out([{}])
if args[:2] == ["apps", "list-deployments"]:
    out(s["deps"][args[2]])
fail("unexpected: " + key)
'''


def setup(tmp: pathlib.Path, flaky: dict | None = None) -> dict:
    fake = tmp / "doctl"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    state = {
        "calls": [], "flaky": flaky or {}, "password": OLD, "next": NEW,
        "cluster": {"id": "c-1", "name": "shop-db", "engine": "pg", "region": "fra1",
                    "connection": {"host": "shop-db.example", "port": 25060, "user": "doadmin", "password": OLD},
                    "users": [{"name": "doadmin", "password": OLD}]},
        "apps": [{"id": "app-bind", "spec": {"name": "api", "databases": [{"name": "db", "cluster_name": "shop-db"}],
                                             "services": [{"name": "api", "envs": [{"key": "DATABASE_URL", "value": "${db.DATABASE_URL}"}]}]}},
                 {"id": "app-lit", "spec": {"name": "web", "databases": [{"name": "db", "cluster_name": "shop-db", "db_user": "doadmin"}],
                                            "envs": [{"key": "DB_PASSWORD", "value": "EV[1:abc]", "type": "SECRET"}]}}],
        "specs": {"app-lit": {"name": "web", "envs": [{"key": "DB_PASSWORD", "value": "EV[1:abc]", "type": "SECRET"}]}},
        "deps": {"app-bind": [{"id": "dep-old-b", "phase": "ACTIVE"}], "app-lit": [{"id": "dep-old-l", "phase": "ACTIVE"}]},
    }
    (tmp / "state.json").write_text(json.dumps(state))
    (tmp / "vault").mkdir()
    return {**os.environ, "DOCTL": str(fake), "FAKE_DO_STATE": str(tmp / "state.json"),
            "OBSERVATORY_VAULT_DIR": str(tmp / "vault"), "OBSERVATORY_DO_RETRY_DELAY": "0",
            "OBSERVATORY_DO_POLL": "0"}


def run(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(DOOR), *args], env=env, capture_output=True, text=True, timeout=120)


def vault_value(env: dict, project: str, name: str) -> str:
    p = subprocess.run([sys.executable, str(ROOT / "tools" / "use_secret.py"), "run", "--vault-only", "--env", "prod",
                        project, name, "--", sys.executable, "-c",
                        f"import os,sys; sys.stdout.write('match' if os.environ['{name}'].count('{NEW}') else 'nomatch')"],
                       env=env, capture_output=True, text=True, timeout=60)
    return p.stdout.strip() or p.stderr.strip()[-200:]


def main() -> int:
    print("digitalocean door")
    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp, flaky={"apps list-deployments app-bind": 1, "apps create-deployment app-bind": 1})
        r = run(env, "db", "rotate", "shop-db", "doadmin", "--db", "shop", "--to", "vault:shop/prod/DATABASE_URL",
                "--app", "app-bind", "--spec-env", "app-lit:DB_PASSWORD", "--wait", "5")
        both = r.stdout + r.stderr
        check("rotate succeeds through two transient 503s", r.returncode == 0, both[-300:])
        check("no password in stdout or stderr", OLD not in both and NEW not in both)
        state = json.loads((tmp / "state.json").read_text())
        calls = [" ".join(c[:3]) for c in state["calls"]]
        reset_at = calls.index("databases user reset")
        check("the literal app's spec is read BEFORE the reset", calls.index("apps spec get") < reset_at)
        check("the reset happens exactly once", calls.count("databases user reset") == 1)
        lit = state["specs"]["app-lit"]["envs"][0]
        check("the literal DB_PASSWORD now holds the new password as a SECRET",
              lit["value"] == NEW and lit["type"] == "SECRET")
        check("the binding app was redeployed", any(c[:3] == ["apps", "create-deployment", "app-bind"] for c in state["calls"]))
        check("the vault holds the new URL", vault_value(env, "shop", "DATABASE_URL") == "match",
              vault_value(env, "shop", "DATABASE_URL"))
        spec_files = list(pathlib.Path(tempfile.gettempdir()).glob("tmp*.json"))
        check("no spec file with the password is left behind",
              not any(NEW in f.read_text(errors="ignore") for f in spec_files if f.stat().st_size < 100000))

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp)
        r = run(env, "db", "rotate", "shop-db", "doadmin", "--to", "vault:shop/prod/DATABASE_URL",
                "--spec-env", "app-lit:NO_SUCH_KEY")
        state = json.loads((tmp / "state.json").read_text())
        check("a missing spec env stops BEFORE any reset",
              r.returncode == 1 and not any(c[:3] == ["databases", "user", "reset"] for c in state["calls"]),
              r.stderr[-200:])

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp)
        r = run(env, "db", "list")
        check("list names clusters and exposure without values",
              r.returncode == 0 and "shop-db" in r.stdout and "OPEN to the internet" in r.stdout
              and OLD not in r.stdout + r.stderr, r.stdout + r.stderr)
        r = run(env, "db", "consumers", "shop-db")
        check("consumers shows the binding app and the literal key",
              "app-bind" in r.stdout and "DB_PASSWORD" in r.stdout and OLD not in r.stdout, r.stdout)
        r = run(env, "db", "credential", "shop-db", "doadmin", "--to", "vault:shop/prod/ADMIN_URL")
        check("credential delivers without printing", r.returncode == 0 and OLD not in r.stdout + r.stderr, r.stderr)

    print(f"{len(FAILURES)} failed" if FAILURES else "all passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
