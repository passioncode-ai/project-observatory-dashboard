#!/usr/bin/env python3
"""The DigitalOcean door against a fake `doctl` and a scratch vault.

Each check is a way a rotation by hand goes wrong:
1. a value in stdout/stderr — the door exists because the API's answer leaks;
2. a 503 from App Platform between the reset and the redeploy leaves a live site on a
   dead password — every call but the reset is retried;
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
reset_done = s["password"] == s["next"]
if args[:2] == ["apps", "list-deployments"] and reset_done and s.get("lists_fail_after_reset"):
    fail("Error: 503 Service Unavailable")
if any(" ".join(args[:len(g.split())]) == g for g in s.get("garbage", [])):
    if args[:3] == ["databases", "user", "reset"]:
        s["password"] = s["next"]
    st.write_text(json.dumps(s)); print("<html>not json</html>"); sys.exit(0)
if args[:3] == ["databases", "user", "reset"] and s.get("reset_no_password"):
    s["password"] = s["next"]; out([{"name": args[4]}])
c = s["cluster"]
if args[:2] == ["auth", "list"]:
    st.write_text(json.dumps(s)); print("default\nexample-context (current)"); sys.exit(0)
if args[:2] == ["account", "get"]:
    if s.get("account_fails"):
        fail("Error: unable to get account")
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
                    "users": [{"name": "doadmin", "password": OLD}], "db_names": ["defaultdb", "shop"]},
        "apps": [{"id": "app-bind", "spec": {"name": "api", "databases": [{"name": "db", "cluster_name": "shop-db"}],
                                             "services": [{"name": "api", "envs": [{"key": "DATABASE_URL", "value": "${db.DATABASE_URL}"}]}]}},
                 {"id": "app-lit", "spec": {"name": "web", "databases": [{"name": "db", "cluster_name": "shop-db", "db_user": "doadmin"}],
                                            "envs": [{"key": "DB_PASSWORD", "value": OLD, "type": "SECRET"},
                                                     {"key": "DATABASE_URL", "value": "postgresql://doadmin:" + OLD + "@shop-db.example:25060/shop?sslmode=require", "type": "SECRET"}]}}],
        "specs": {"app-lit": {"name": "web", "envs": [{"key": "DB_PASSWORD", "value": OLD, "type": "SECRET"},
                                                     {"key": "DATABASE_URL", "value": "postgresql://doadmin:" + OLD + "@shop-db.example:25060/shop?sslmode=require", "type": "SECRET"}]}},
        "deps": {"app-bind": [{"id": "dep-old-b", "phase": "ACTIVE"}], "app-lit": [{"id": "dep-old-l", "phase": "ACTIVE"}]},
    }
    state.update(extra)
    (tmp / "state.json").write_text(json.dumps(state))
    (tmp / "vault").mkdir()
    return {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp), "OBSERVATORY_HOME": str(tmp / "workspace"),
            "PYTHONDONTWRITEBYTECODE": "1", "DOCTL": str(fake), "FAKE_DO_STATE": str(tmp / "state.json"),
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
        env = setup(tmp, flaky={"apps list-deployments app-bind": 1, "apps spec get": 1})
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
    review_round_two()
    review_round_three()
    review_round_four()
    review_round_five()
    print(f"{len(FAILURES)} failed" if FAILURES else "all passed")
    # The door's account half and its collector (OBS-155) run in the same step, `test-digitalocean`,
    # so one step covers one door; the suite is its own file and is run here, not twice.
    sys.path.insert(0, str(ROOT / "tests"))
    import test_digitalocean_account
    account = test_digitalocean_account.main()
    return 1 if FAILURES or account else 0


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
        check("and says the database password WAS reset", "WAS reset" in r.stderr, r.stderr[-300:])

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        lit2 = {"name": "web2", "envs": [{"key": "DB_PASSWORD", "value": OLD, "type": "SECRET"}]}
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
        os.environ.clear()
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
              rc == 3 and "WAS reset" in err.getvalue() and "db credential shop-db doadmin" in err.getvalue(),
              err.getvalue()[-400:])
        check("and the apps were still moved to the new password",
              any(c[:3] == ["apps", "create-deployment", "app-bind"] for c in state["calls"]))


def review_round_two() -> None:
    """Round 2 (PLN-142): nothing after the reset may strand a consumer, unnamed."""
    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp)
        put(env, "shop", "DATABASE_URL", "postgresql://old@example.invalid/x")
        (tmp / "vault" / "shop" / "prod" / "DATABASE_URL").chmod(0o644)
        r = run(env, "db", "rotate", "shop-db", "doadmin", "--to", "vault:shop/prod/DATABASE_URL", "--wait", "5")
        state = json.loads((tmp / "state.json").read_text())
        check("a slot the vault will not read stops the rotation before the reset",
              r.returncode == 1 and "nothing was reset" in r.stderr and "Traceback" not in r.stderr
              and not any(c[:3] == ["databases", "user", "reset"] for c in state["calls"]), r.stderr[-300:])

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        pool = "postgresql://doadmin:" + OLD + "@shop-db.example:25060/shop-pool?sslmode=require"
        env = setup(tmp)
        st = json.loads((tmp / "state.json").read_text())
        st["specs"]["app-pool"] = {"name": "pool", "envs": [{"key": "DATABASE_URL", "value": pool}]}
        st["deps"]["app-pool"] = [{"id": "dep-old-p", "phase": "ACTIVE"}]
        (tmp / "state.json").write_text(json.dumps(st))
        r = run(env, "db", "rotate", "shop-db", "doadmin", "--to", "vault:shop/prod/DATABASE_URL",
                "--spec-env", "app-pool:DATABASE_URL", "--wait", "5")
        got = json.loads((tmp / "state.json").read_text())["specs"]["app-pool"]["envs"][0]["value"]
        check("a plain URL literal keeps its own user, host, port, path and query; only the password changes",
              r.returncode == 0 and got == pool.replace(OLD, NEW), (r.stdout + r.stderr)[-300:])
        check("and the run names the updated app", "app-pool" in r.stdout, r.stdout[-300:])
        check("nothing printed holds either password", OLD not in r.stdout + r.stderr and NEW not in r.stdout + r.stderr)

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp)
        with_spec(tmp, "app-lit", {"name": "web", "envs": [{"key": "DATABASE_URL", "value": "EV[1:synthetic]"}]})
        r = run(env, "db", "rotate", "shop-db", "doadmin", "--to", "vault:shop/prod/DATABASE_URL",
                "--spec-env", "app-lit:DATABASE_URL", "--wait", "5")
        state = json.loads((tmp / "state.json").read_text())
        check("an encrypted URL literal is refused before the reset",
              r.returncode == 1 and "cannot prove" in r.stderr and "nothing was reset" in r.stderr
              and not any(c[:3] == ["databases", "user", "reset"] for c in state["calls"]), r.stderr[-300:])

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp, flaky={"apps update app-lit": 99})
        r = run(env, "db", "rotate", "shop-db", "doadmin", "--to", "vault:shop/prod/DATABASE_URL",
                "--app", "app-bind", "--spec-env", "app-lit:DB_PASSWORD", "--wait", "5")
        state = json.loads((tmp / "state.json").read_text())
        check("a failed literal update still redeploys the bindings",
              any(c[:3] == ["apps", "create-deployment", "app-bind"] for c in state["calls"]), r.stderr[-300:])
        check("names done and left across both kinds, with its own exit code",
              r.returncode == 4 and "left: app-lit" in r.stderr and "done: app-bind" in r.stderr, r.stderr[-400:])
        check("and never advises rerunning db rotate after a reset",
              "rerun" not in r.stderr.lower() and "db credential" in r.stderr, r.stderr[-400:])
        check("nor a redeploy for a literal (a redeploy keeps the old literal)",
              "read each app's current spec" in r.stderr and "redeploy alone keeps an old literal" in r.stderr, r.stderr[-400:])

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp, account_fails=True)
        r = run(env, "db", "rotate", "shop-db", "doadmin", "--to", "vault:shop/prod/DATABASE_URL", "--wait", "5")
        check("the real doctl context is named, and a failed account read is 'team unknown'",
              r.returncode == 0 and "doctl context: example-context" in r.stdout and "team unknown" in r.stdout,
              (r.stdout + r.stderr)[-300:])

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp)
        st = json.loads((tmp / "state.json").read_text())
        st["apps"].append({"id": "app-cache", "spec": {"name": "cache", "databases": [{"name": "db", "cluster_name": "shop-db"}],
                                                        "envs": [{"key": "REDIS_URL", "value": "EV[1:x]", "type": "SECRET"},
                                                                 {"key": "SENTRY_DSN", "value": "EV[1:y]", "type": "SECRET"}]}})
        (tmp / "state.json").write_text(json.dumps(st))
        r = run(env, "db", "consumers", "shop-db")
        check("consumers knows the URL keys (REDIS_URL) and says it sees only binding apps",
              "REDIS_URL (url)" in r.stdout and "binds this cluster" in r.stdout, r.stdout)
        check("and a non-database DSN (SENTRY_DSN, encrypted) is not a database literal",
              "SENTRY_DSN" not in r.stdout, r.stdout)
        r = run(env, "db", "rotate", "--help")
        check("rotate's help says consumers are down between the reset and ACTIVE", "down" in r.stdout, r.stdout[-300:])
        check("and that the largest exit code wins", "largest" in r.stdout, r.stdout[-400:])


def rotate(env: dict, *extra: str) -> subprocess.CompletedProcess:
    return run(env, "db", "rotate", "shop-db", "doadmin", "--to", "vault:shop/prod/DATABASE_URL", "--wait", "5", *extra)


def calls_of(tmp: pathlib.Path) -> list[list[str]]:
    return json.loads((tmp / "state.json").read_text())["calls"]


def reset_called(tmp: pathlib.Path) -> bool:
    return any(c[:3] == ["databases", "user", "reset"] for c in calls_of(tmp))


def with_spec(tmp: pathlib.Path, app: str, spec: dict) -> None:
    st = json.loads((tmp / "state.json").read_text())
    st["specs"][app] = spec
    st["deps"][app] = [{"id": "dep-old-" + app, "phase": "ACTIVE"}]
    (tmp / "state.json").write_text(json.dumps(st))


def review_round_three() -> None:
    """Round 3 (PLN-142): after the reset every failure says so and nothing stops early;
    before it, every literal at every level is checked."""
    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp, lists_fail_after_reset=True)
        r = rotate(env, "--app", "app-bind", "--spec-env", "app-lit:DB_PASSWORD")
        waits = [c for c in calls_of(tmp) if c[:2] == ["apps", "list-deployments"]]
        check("a wait that fails after the reset is exit 2 with WAS reset, not 1",
              r.returncode == 2 and "WAS reset" in r.stderr and "Traceback" not in r.stderr, r.stderr[-300:])
        check("and the door keeps checking the other apps",
              {c[2] for c in waits} >= {"app-bind", "app-lit"} and "app-bind" in r.stderr and "app-lit" in r.stderr,
              r.stderr[-300:])

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp)
        with_spec(tmp, "app-two", {"name": "two", "envs": [{"key": "DATABASE_URL", "value": "postgresql://host.example/db"}],
                                   "services": [{"name": "api", "envs": [{"key": "DATABASE_URL",
                                                 "value": "postgresql://doadmin:" + OLD + "@host.example:25060/db"}]}]})
        r = rotate(env, "--spec-env", "app-two:DATABASE_URL")
        check("a key at both app and service level is checked at every level before the reset",
              r.returncode == 1 and "nothing was reset" in r.stderr and not reset_called(tmp), r.stderr[-300:])

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp)
        with_spec(tmp, "app-rep", {"name": "rep", "envs": [{"key": "DATABASE_URL",
                                   "value": "postgresql://reporting:" + OLD + "@host.example:25060/db"}]})
        r = rotate(env, "--spec-env", "app-rep:DATABASE_URL")
        got = json.loads((tmp / "state.json").read_text())["specs"]["app-rep"]["envs"][0]["value"]
        check("a URL literal for another user is refused before the reset and left untouched",
              r.returncode == 1 and "cannot prove" in r.stderr and not reset_called(tmp) and NEW not in got,
              r.stderr[-300:])
        check("and its password is not printed", OLD not in r.stdout + r.stderr)

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp, reset_no_password=True)
        r = rotate(env, "--app", "app-bind")
        check("a reset that answers without a password is exit 5 and says unknown",
              r.returncode == 5 and "outcome unknown" in r.stderr and "may have changed" in r.stderr, r.stderr[-300:])

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp, garbage=["apps spec get"])
        r = rotate(env, "--spec-env", "app-lit:DB_PASSWORD")
        check("a non-JSON answer before the reset is exit 1, nothing reset, no traceback",
              r.returncode == 1 and "nothing was reset" in r.stderr and "Traceback" not in r.stderr
              and not reset_called(tmp), r.stderr[-300:])

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp, garbage=["apps create-deployment"])
        r = rotate(env, "--app", "app-bind")
        check("a non-JSON answer after the reset maps to its post-reset code (4)",
              r.returncode == 4 and "WAS reset" in r.stderr and "Traceback" not in r.stderr, r.stderr[-300:])

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp, garbage=["databases user reset"])
        r = rotate(env, "--app", "app-bind")
        check("a non-JSON answer from the reset itself is exit 5 with unknown outcome",
              r.returncode == 5 and "outcome unknown" in r.stderr and "Traceback" not in r.stderr, r.stderr[-300:])

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp)
        st = json.loads((tmp / "state.json").read_text())
        st["apps"].append({"id": "app-more", "spec": {"name": "more", "databases": [{"name": "db", "cluster_name": "shop-db"}],
                                                       "envs": [{"key": "DB_URL", "value": "EV[1:a]", "type": "SECRET"},
                                                                {"key": "POSTGRES_URL", "value": "EV[1:b]", "type": "SECRET"},
                                                                {"key": "ANALYTICS_DSN",
                                                                 "value": "postgresql://reader@host.example/db"}]}})
        (tmp / "state.json").write_text(json.dumps(st))
        r = run(env, "db", "consumers", "shop-db")
        check("DB_URL, POSTGRES_URL and a database-scheme DSN are listed as literals",
              "DB_URL (url)" in r.stdout and "POSTGRES_URL (url)" in r.stdout and "ANALYTICS_DSN (url)" in r.stdout,
              r.stdout)


def review_round_four() -> None:
    """Round 4 (PLN-142), what #256's door did not already close: `--db` is one of
    the cluster's databases; a `${…}` binding is left alone at every level; an
    encrypted DSN or URI under a database name is listed by `db consumers`."""
    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp)
        st = json.loads((tmp / "state.json").read_text())
        st["cluster"]["db_names"] = ["defaultdb", "shop"]
        (tmp / "state.json").write_text(json.dumps(st))
        r = rotate(env, "--db", "nosuchdb", "--app", "app-bind")
        check("--db that is not one of the cluster's databases is refused before the reset",
              r.returncode == 1 and "nosuchdb" in r.stderr and "nothing was reset" in r.stderr
              and not reset_called(tmp), r.stderr[-300:])
        r = rotate(env, "--db", "shop", "--app", "app-bind")
        check("one of the cluster's databases is accepted", r.returncode == 0, (r.stdout + r.stderr)[-300:])

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp)
        url = "postgresql://doadmin:" + OLD + "@shop-db.example:25060/shop?sslmode=require"
        with_spec(tmp, "app-mix", {"name": "mix", "envs": [{"key": "DATABASE_URL", "value": url, "type": "SECRET"}],
                                   "services": [{"name": "api", "envs": [{"key": "DATABASE_URL",
                                                                          "value": "${db.DATABASE_URL}"}]}]})
        r = rotate(env, "--spec-env", "app-mix:DATABASE_URL")
        spec = json.loads((tmp / "state.json").read_text())["specs"]["app-mix"]
        check("a ${…} binding at one level is left alone while the literal at another moves",
              r.returncode == 0 and spec["services"][0]["envs"][0]["value"] == "${db.DATABASE_URL}"
              and spec["envs"][0]["value"] == url.replace(OLD, NEW), (r.stdout + r.stderr)[-300:])

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp)
        with_spec(tmp, "app-bound", {"name": "bound", "envs": [{"key": "DATABASE_URL", "value": "${db.DATABASE_URL}"}]})
        r = rotate(env, "--spec-env", "app-bound:DATABASE_URL")
        check("a key that is only a binding is refused before the reset, naming --app and never --db",
              r.returncode == 1 and "--app" in r.stderr and "--db" not in r.stderr and not reset_called(tmp),
              r.stderr[-300:])

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp)
        st = json.loads((tmp / "state.json").read_text())
        st["apps"].append({"id": "app-dsn", "spec": {"name": "dsn", "databases": [{"name": "db", "cluster_name": "shop-db"}],
                                                      "envs": [{"key": "DATABASE_DSN", "value": "EV[1:a]", "type": "SECRET"},
                                                               {"key": "DB_URI", "value": "EV[1:b]", "type": "SECRET"},
                                                               {"key": "SENTRY_DSN", "value": "EV[1:c]", "type": "SECRET"}]}})
        (tmp / "state.json").write_text(json.dumps(st))
        r = run(env, "db", "consumers", "shop-db")
        check("db consumers lists an encrypted DATABASE_DSN and DB_URI, and still not SENTRY_DSN",
              "DATABASE_DSN" in r.stdout and "DB_URI" in r.stdout and "SENTRY_DSN" not in r.stdout, r.stdout)


def with_cluster(tmp: pathlib.Path, **fields) -> None:
    """Change the fake provider's cluster; a field given as None is removed."""
    st = json.loads((tmp / "state.json").read_text())
    for k, v in fields.items():
        if v is None:
            st["cluster"].pop(k, None)
        else:
            st["cluster"][k] = v
    (tmp / "state.json").write_text(json.dumps(st))


def slot_written(tmp: pathlib.Path, name: str) -> bool:
    return any(p.name == name for p in (tmp / "vault").rglob("*"))


def review_round_five() -> None:
    """Round 5 (J-10): `db credential` checks `--db` as rotate does; a cluster whose
    provider lists no databases, or a redis/valkey one, gets its own refusal rather
    than "not one of this cluster's databases"; printed names are identifiers only;
    `db consumers` matches database words anywhere in a key, not only as a prefix."""
    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp)
        r = run(env, "db", "credential", "shop-db", "doadmin", "--db", "nosuchdb", "--to", "vault:shop/prod/ADMIN_URL")
        check("db credential refuses a --db that is not one of the cluster's databases, exit 1",
              r.returncode == 1 and "nosuchdb" in r.stderr and "not one of this cluster's databases" in r.stderr,
              (r.stdout + r.stderr)[-300:])
        check("and writes nothing to the vault", not slot_written(tmp, "ADMIN_URL"))
        check("and never reads the user's password",
              not any(c[:3] == ["databases", "user", "get"] for c in calls_of(tmp)))
        r = run(env, "db", "credential", "shop-db", "doadmin", "--db", "shop", "--to", "vault:shop/prod/ADMIN_URL")
        check("db credential with one of the cluster's databases delivers",
              r.returncode == 0 and slot_written(tmp, "ADMIN_URL") and OLD not in r.stdout + r.stderr,
              (r.stdout + r.stderr)[-300:])

    for label, value in (("missing", None), ("null", "null"), ("empty", [])):
        with tempfile.TemporaryDirectory() as t:
            tmp = pathlib.Path(t).resolve()
            env = setup(tmp)
            if value == "null":
                st = json.loads((tmp / "state.json").read_text())
                st["cluster"]["db_names"] = None
                (tmp / "state.json").write_text(json.dumps(st))
            else:
                with_cluster(tmp, db_names=value)
            r = rotate(env, "--db", "shop", "--app", "app-bind")
            check(f"db_names {label}: rotate says the provider listed no databases, not that --db is absent",
                  r.returncode == 1 and "the provider listed no databases for this cluster" in r.stderr
                  and "not one of" not in r.stderr and "nothing was reset" in r.stderr and not reset_called(tmp),
                  r.stderr[-300:])
            r = run(env, "db", "credential", "shop-db", "doadmin", "--db", "shop", "--to", "vault:shop/prod/ADMIN_URL")
            check(f"db_names {label}: credential refuses the same way and writes nothing",
                  r.returncode == 1 and "the provider listed no databases for this cluster" in r.stderr
                  and not slot_written(tmp, "ADMIN_URL"), r.stderr[-300:])

    for engine in ("redis", "valkey"):
        with tempfile.TemporaryDirectory() as t:
            tmp = pathlib.Path(t).resolve()
            env = setup(tmp)
            with_cluster(tmp, engine=engine, db_names=None)
            r = rotate(env, "--db", "shop", "--app", "app-bind")
            check(f"{engine}: --db is refused as not applying to the engine",
                  r.returncode == 1 and f"--db does not apply to {engine}" in r.stderr
                  and "not one of" not in r.stderr and "listed no databases" not in r.stderr
                  and not reset_called(tmp), r.stderr[-300:])
            r = run(env, "db", "credential", "shop-db", "doadmin", "--db", "shop", "--to", "vault:shop/prod/CACHE_URL")
            check(f"{engine}: credential refuses --db too and writes nothing",
                  r.returncode == 1 and f"--db does not apply to {engine}" in r.stderr
                  and not slot_written(tmp, "CACHE_URL"), r.stderr[-300:])
            r = run(env, "db", "credential", "shop-db", "doadmin", "--to", "vault:shop/prod/CACHE_URL")
            check(f"{engine}: credential without --db still delivers",
                  r.returncode == 0 and slot_written(tmp, "CACHE_URL"), (r.stdout + r.stderr)[-300:])

    # Review round 6: an engine the door does not build URLs for is refused by name.
    for engine in ("mongodb", "kafka", "opensearch"):
        with tempfile.TemporaryDirectory() as t:
            tmp = pathlib.Path(t).resolve()
            env = setup(tmp)
            with_cluster(tmp, engine=engine, db_names=None)
            r = run(env, "db", "credential", "shop-db", "doadmin", "--to", "vault:shop/prod/OTHER_URL")
            check(f"{engine}: credential is refused naming the engine and writes nothing",
                  r.returncode == 1 and f"not {engine}" in r.stderr and "incomplete" not in r.stderr
                  and not slot_written(tmp, "OTHER_URL"), r.stderr[-300:])

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp)
        with_cluster(tmp, db_names=[1, {}])
        r = rotate(env, "--db", "shop", "--app", "app-bind")
        check("db_names of non-names: no raw provider entries are printed",
              r.returncode == 1 and "(1, {})" not in r.stderr and "{}" not in r.stderr
              and "not one of" not in r.stderr and not reset_called(tmp), r.stderr[-300:])
        with_cluster(tmp, db_names=["defaultdb", 1, {}, "shop"])
        r = rotate(env, "--db", "nosuchdb", "--app", "app-bind")
        check("db_names with some non-names: only the identifiers are printed",
              r.returncode == 1 and "(defaultdb, shop)" in r.stderr and "{}" not in r.stderr
              and not reset_called(tmp), r.stderr[-300:])

    with tempfile.TemporaryDirectory() as t:
        tmp = pathlib.Path(t).resolve()
        env = setup(tmp)
        listed = ("APP_DATABASE_DSN", "PRIMARY_DB_URI", "REPLICA_PG_URL", "ORDERS_POSTGRESQL_URI",
                  "CACHE_VALKEY_URI", "MONGODB_URI", "EVENTS_MYSQL_DSN")
        unlisted = ("SENTRY_DSN", "OTEL_EXPORTER_OTLP_URI", "DBT_URI", "DBX_DSN", "DATABASE_POOL_SIZE")
        st = json.loads((tmp / "state.json").read_text())
        st["apps"].append({"id": "app-parts", "spec": {
            "name": "parts", "databases": [{"name": "db", "cluster_name": "shop-db"}],
            "envs": [{"key": k, "value": f"EV[1:{i}]", "type": "SECRET"}
                     for i, k in enumerate((*listed, *unlisted))]}})
        (tmp / "state.json").write_text(json.dumps(st))
        r = run(env, "db", "consumers", "shop-db")
        missed = [k for k in listed if f"parts:{k} (url)" not in r.stdout]
        wrong = [k for k in unlisted if f":{k} (" in r.stdout]
        check("db consumers matches a database word anywhere in the key beside _URL/_URI/_DSN",
              r.returncode == 0 and not missed, f"missed {missed}: {r.stdout}")
        check("and still leaves SENTRY_DSN, OTEL_…_URI, DBT_URI, DBX_DSN and non-address keys out",
              not wrong, f"listed {wrong}: {r.stdout}")


if __name__ == "__main__":
    sys.exit(main())
