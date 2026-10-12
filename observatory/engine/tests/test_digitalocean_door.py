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
if args[:2] == ["account", "get"]:
    out({"email": "operator@example.invalid", "team": {"name": "example-team"}})
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
    s["specs"][args[2]] = spec; s["deps"][args[2]].insert(0, {"id": "dep-new-" + args[2], "phase": s.get("phase", "ACTIVE")}); out([{}])
if args[:2] == ["apps", "create-deployment"]:
    s["deps"][args[2]].insert(0, {"id": "dep-new-" + args[2], "phase": s.get("phase", "ACTIVE")}); out([{}])
if args[:2] == ["apps", "list-deployments"]:
    out(s["deps"][args[2]])
fail("unexpected: " + key)
'''


def setup(tmp: pathlib.Path, flaky: dict | None = None, **extra) -> dict:
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
                                            "envs": [{"key": "DB_PASSWORD", "value": "EV[1:abc]", "type": "SECRET"},
                                                     {"key": "DATABASE_URL", "value": "EV[1:def]", "type": "SECRET"}]}}],
        "specs": {"app-lit": {"name": "web", "envs": [{"key": "DB_PASSWORD", "value": "EV[1:abc]", "type": "SECRET"},
                                                     {"key": "DATABASE_URL", "value": "EV[1:def]", "type": "SECRET"}]}},
        "deps": {"app-bind": [{"id": "dep-old-b", "phase": "ACTIVE"}], "app-lit": [{"id": "dep-old-l", "phase": "ACTIVE"}]},
    }
    state.update(extra)
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

    review_round_one()
    print(f"{len(FAILURES)} failed" if FAILURES else "all passed")
    return 1 if FAILURES else 0


def put(env: dict, project: str, name: str, value: str) -> None:
    p = subprocess.run([sys.executable, str(ROOT / "tools" / "vault.py"), "put", project, "prod", name],
                       input=value, env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr


def review_round_one() -> None:
    """The verification round's findings (PLN-142), each driven through the real command."""
    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp)
        # A NEIGHBOUR SLOT IS NOT THE SLOT. `DATABASE_URL_POOL` contains the text
        # `shop/prod/DATABASE_URL`, and a substring test chose `rotate` for a slot
        # that did not exist — after the password was already reset.
        put(env, "shop", "DATABASE_URL_POOL", "postgresql://pool@example.invalid/x")
        r = run(env, "db", "rotate", "shop-db", "doadmin", "--to", "vault:shop/prod/DATABASE_URL", "--wait", "5")
        check("a neighbour slot does not make the door rotate a slot that does not exist",
              r.returncode == 0 and vault_value(env, "shop", "DATABASE_URL") == "match", (r.stdout + r.stderr)[-300:])
        check("the doctl team is named before the reset",
              "team: example-team" in r.stdout and r.stdout.index("team:") < r.stdout.index("reset "), r.stdout[-300:])
        check("the email of the doctl account is not printed", "operator@example.invalid" not in r.stdout + r.stderr)

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp)
        (tmp / "vault").chmod(0o500)
        try:
            r = run(env, "db", "rotate", "shop-db", "doadmin", "--to", "vault:shop/prod/DATABASE_URL", "--wait", "5")
        finally:
            (tmp / "vault").chmod(0o700)
        state = json.loads((tmp / "state.json").read_text())
        check("a vault that cannot take the value stops the rotation BEFORE the reset",
              r.returncode == 1 and not any(c[:3] == ["databases", "user", "reset"] for c in state["calls"])
              and "nothing was reset" in r.stderr, r.stderr[-300:])

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp)
        r = run(env, "db", "rotate", "shop-db", "doadmin", "--db", "shop", "--to", "vault:shop/prod/DATABASE_URL",
                "--spec-env", "app-lit:DATABASE_URL", "--spec-env", "app-lit:DB_PASSWORD", "--wait", "5")
        state = json.loads((tmp / "state.json").read_text())
        envs = {e["key"]: e for e in state["specs"]["app-lit"]["envs"]}
        check("a URL-shaped literal gets the connection URL with the new password, never the bare one",
              r.returncode == 0 and envs["DATABASE_URL"]["value"].startswith("postgresql://doadmin:")
              and NEW in envs["DATABASE_URL"]["value"] and envs["DB_PASSWORD"]["value"] == NEW,
              (r.stdout + r.stderr)[-300:])
        r = run(env, "db", "consumers", "shop-db")
        check("consumers says which form each literal takes",
              "DATABASE_URL (url)" in r.stdout and "DB_PASSWORD (password)" in r.stdout, r.stdout)

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp)
        r = run(env, "db", "credential", "shop-db", "doadmin", "--to", "vault:shop/prod/ADMIN_URL")
        again = run(env, "db", "credential", "shop-db", "doadmin", "--to", "vault:shop/prod/ADMIN_URL")
        check("delivering the same URL twice is a success that says unchanged",
              r.returncode == 0 and again.returncode == 0 and "unchanged" in again.stdout, again.stdout + again.stderr)

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp, phase="ERROR")
        r = run(env, "db", "rotate", "shop-db", "doadmin", "--to", "vault:shop/prod/DATABASE_URL",
                "--app", "app-bind", "--wait", "5")
        check("a deployment that fails names its own app in the logs command",
              r.returncode == 2 and "doctl apps logs app-bind --type deploy" in r.stderr, r.stderr[-300:])

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        lit2 = {"name": "web2", "envs": [{"key": "DB_PASSWORD", "value": "EV[1:abc]", "type": "SECRET"}]}
        env = setup(tmp, flaky={"apps update app-lit2": 99})
        st = json.loads((tmp / "state.json").read_text())
        st["specs"]["app-lit2"] = lit2
        st["deps"]["app-lit2"] = [{"id": "dep-old-l2", "phase": "ACTIVE"}]
        (tmp / "state.json").write_text(json.dumps(st))
        r = run(env, "db", "rotate", "shop-db", "doadmin", "--to", "vault:shop/prod/DATABASE_URL",
                "--spec-env", "app-lit:DB_PASSWORD", "--spec-env", "app-lit2:DB_PASSWORD", "--wait", "5")
        check("a failed app update lists the apps done and the apps left",
              r.returncode != 0 and "done: app-lit" in r.stderr and "left: app-lit2" in r.stderr
              and "OLD password" in r.stderr, r.stderr[-400:])
        check("and nothing it prints holds a password", OLD not in r.stdout + r.stderr and NEW not in r.stdout + r.stderr)

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp)
        env["DOCTL"] = str(tmp / "no-such-doctl")
        r = run(env, "db", "list")
        check("a missing doctl is a named refusal, not a traceback",
              r.returncode == 1 and "doctl not found" in r.stderr and "doctl auth init" in r.stderr
              and "Traceback" not in r.stderr, r.stderr[-300:])

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp)
        # A service started with `use_secret.py serve` from this slot: vault rotate names it, and the door must not swallow that.
        put(env, "shop", "DATABASE_URL", "postgresql://old@example.invalid/x")
        state_dir = tmp / "state"
        (state_dir / "logs").mkdir(parents=True)
        (state_dir / "logs" / "secret-use.jsonl").write_text(json.dumps(
            {"action": "serve", "project": "shop", "consumer": "org.example.shop-worker", "at": "2026-01-01T00:00:00Z",
             "from": {"DATABASE_URL": "vault:shop/prod/DATABASE_URL"}}) + "\n")
        env["OBSERVATORY_STATE"] = str(state_dir)
        r = run(env, "db", "rotate", "shop-db", "doadmin", "--to", "vault:shop/prod/DATABASE_URL", "--wait", "5")
        check("the services still on the old value are named for restart",
              r.returncode == 0 and "org.example.shop-worker" in r.stdout, (r.stdout + r.stderr)[-400:])

    # After the reset the vault refuses: the run says the password WAS reset and moves the apps anyway.
    import importlib.util
    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp)
        saved = dict(os.environ)
        os.environ.update(env)
        try:
            spec = importlib.util.spec_from_file_location("do_door_t", DOOR)
            door = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(door)
            def refuse(value, plan):
                raise door.DoorError("vault rotate shop/prod/DATABASE_URL refused: synthetic refusal")
            door.deliver = refuse
            import io, contextlib
            err, out = io.StringIO(), io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                rc = door.main(["db", "rotate", "shop-db", "doadmin", "--to", "vault:shop/prod/DATABASE_URL",
                                "--app", "app-bind", "--wait", "5"])
        finally:
            os.environ.clear(); os.environ.update(saved)
        state = json.loads((tmp / "state.json").read_text())
        check("a vault failure after the reset says the password WAS reset and gives the next step",
              rc == 2 and "WAS reset" in err.getvalue() and "db credential shop-db doadmin" in err.getvalue(),
              err.getvalue()[-400:])
        check("and the apps were still moved to the new password",
              any(c[:3] == ["apps", "create-deployment", "app-bind"] for c in state["calls"]))


if __name__ == "__main__":
    sys.exit(main())
