#!/usr/bin/env python3
"""A secret reaches a command and not the transcript — driven, not asserted.

The one property that matters here cannot be checked by reading the source: a
scrubber either removes the value from what a child prints or it does not, and
the first version of `pump` LOOKED correct and leaked. It held the tail back and
scrubbed only what it emitted, so a value sitting across a chunk boundary was cut
in two and both halves went out unaltered. Nothing but running a careless child
would have caught it.

So every test below starts a real process that prints what it was handed, and
asserts about the bytes that came back.

`tools/scan_leaks.py` is driven the same way: a planted value in a planted file,
and the assertion is that the scanner finds it, names it, and does NOT quote it.
"""
from __future__ import annotations
import json, os, pathlib, sqlite3, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "collectors"))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402

#: EVERY SUBPROCESS BELOW JOURNALS INTO A SCRATCH STATE. `use_secret.py` writes
#: `secret-use.jsonl` under `paths.STATE`, and three `pipe` calls here ran with
#: the live one — so every gate run left two `pipe DATABASE_URL` rows in the
#: estate's real record, indistinguishable from a person's. The gate hashes the
#: journals before and after it runs, and was the thing that noticed.
SCRATCH_STATE = {**os.environ, "OBSERVATORY_STATE": tmpdir.mkdtemp(prefix="observatory-use-state-")}

FAILURES: list[str] = []

#: COMPOSED, like every other fixture credential in this suite — a literal that
#: looks like a key is a literal `tools/check_secrets.py` must treat as one, and
#: it is right to. The value is what matters: long, random-looking, and it must
#: pass `scan_env.looks_secret` or the leak hunt would skip it by design.
PLANTED = "sk-" + "or-v1-" + "9f3Qm2Xz" + "R7tLbK4w" + "N8vPc1Ye" + "H5jD6sAu"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def careless_child(d: pathlib.Path, var: str, repeats: int = 1) -> pathlib.Path:
    """A child that prints what it was given, on both streams.

    Written to a file rather than passed as `-c`: this repository's own shell
    guard refuses a command line that would expand a credential-named variable,
    and it is right to — the refusal is the same rule this tool exists to serve.
    """
    p = d / "careless.py"
    p.write_text(
        "import os, sys\n"
        f"v = os.environ.get({var!r}, '')\n"
        "sys.stdout.write('out: ' + v + '\\n')\n"
        "sys.stderr.write('err: ' + v + '\\n')\n"
        f"sys.stdout.write('run: ' + v * {repeats} + '\\n')\n"
        "sys.stdout.write('len: ' + str(len(v)) + '\\n')\n",
        encoding="utf-8")
    return p


def estate(with_value: str = PLANTED) -> tuple[pathlib.Path, dict]:
    """A one-project estate with one env file, and a scan of it."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-use-"))
    data = d / "estate"
    (data / "demo").mkdir(parents=True)
    (data / "demo" / ".env").write_text(
        f"PORT=3000\nDEMO_API_KEY={with_value}\n", encoding="utf-8")
    scratch = d / "scratch"
    scratch.mkdir()
    env = {**os.environ,
           "OBSERVATORY_DATA": str(data),
           "OBSERVATORY_SCRATCH": str(scratch),
           "OBSERVATORY_STATE": str(d / "state"),
           "OBSERVATORY_VAULT_DIR": str(d / "novault")}
    subprocess.run([sys.executable, str(ROOT / "tools/runtime_identity.py"),
                    "init", "env-fingerprint-salt"], env=env, capture_output=True,
                   text=True, check=True, timeout=15)
    subprocess.run([sys.executable, str(ROOT / "collectors/scan_env.py"),
                    str(scratch / "env.json")], env=env, capture_output=True,
                   text=True, check=True)
    return d, env


def test_the_value_reaches_the_command() -> None:
    d, env = estate()
    child = careless_child(d, "DEMO_API_KEY")
    p = subprocess.run([sys.executable, str(ROOT / "tools/use_secret.py"), "run",
                        "demo", "DEMO_API_KEY", "--", sys.executable, str(child)],
                       env=env, capture_output=True, text=True, timeout=60)
    check("the command runs", p.returncode == 0, p.stderr[-160:])
    # THE CHILD SAW IT — the length it printed is the length of the real value.
    check("and the child received the real value",
          f"len: {len(PLANTED)}" in p.stdout, p.stdout[-120:])


def test_and_not_the_transcript() -> None:
    """The property the whole tool exists for, on both streams."""
    d, env = estate()
    child = careless_child(d, "DEMO_API_KEY")
    p = subprocess.run([sys.executable, str(ROOT / "tools/use_secret.py"), "run",
                        "demo", "DEMO_API_KEY", "--", sys.executable, str(child)],
                       env=env, capture_output=True, text=True, timeout=60)
    check("the value is not in what the child printed to stdout",
          PLANTED not in p.stdout, "IT LEAKED")
    check("nor to stderr", PLANTED not in p.stderr, "IT LEAKED")
    check("and the name is there in its place",
          "«DEMO_API_KEY»" in p.stdout and "«DEMO_API_KEY»" in p.stderr,
          p.stdout[:120])


def test_a_value_split_across_a_buffer_is_still_removed() -> None:
    """The exact defect the first version shipped with.

    A child that writes far more than one chunk puts the value across a read
    boundary many times over. The first `pump` scrubbed only the part it emitted
    and carried the rest raw, so the two halves of a straddling value were both
    printed intact.
    """
    d, env = estate()
    # Enough repeats to cross the 64 KB chunk boundary many times.
    child = careless_child(d, "DEMO_API_KEY", repeats=4000)
    p = subprocess.run([sys.executable, str(ROOT / "tools/use_secret.py"), "run",
                        "demo", "DEMO_API_KEY", "--", sys.executable, str(child)],
                       env=env, capture_output=True, text=True, timeout=90)
    check("a value repeated across many chunks never appears",
          PLANTED not in p.stdout,
          f"leaked at offset {p.stdout.find(PLANTED)} of {len(p.stdout)}")
    check("and every occurrence became its name",
          p.stdout.count("«DEMO_API_KEY»") >= 4000,
          str(p.stdout.count("«DEMO_API_KEY»")))
    # A HALF is the shape of the defect, so it is asserted separately: the first
    # version emitted the value's head and then its tail.
    check("not even a half of it survives",
          PLANTED[:20] not in p.stdout and PLANTED[-20:] not in p.stdout,
          "a fragment survived, which is how the first version failed")


def test_pipe_hands_a_stdin_value_to_a_command_and_scrubs_its_output() -> None:
    """`use_secret.py pipe NAME -- cmd`: the value arrives on stdin, reaches the
    child as $NAME, and never reaches the transcript — including through a
    traceback that quotes it. The shape a provider's CLI needs.
    """
    value = "postgres://u:" + "p" * 24 + "@host/db"
    p = subprocess.run([sys.executable, str(ROOT / "tools/use_secret.py"), "pipe", "DATABASE_URL",
                        "--", sys.executable, "-c",
                        "import os; print(len(os.environ['DATABASE_URL'])); raise SystemExit(os.environ['DATABASE_URL'])"],
                       input=value, capture_output=True, text=True, timeout=60, env=SCRATCH_STATE)
    check("the child saw the value under NAME", str(len(value)) in p.stdout, p.stdout[-80:])
    check("and a traceback that quotes it comes back scrubbed",
          value not in p.stderr and "«DATABASE_URL»" in p.stderr, p.stderr[-160:])
    check("the child's exit code is passed through", p.returncode == 1, str(p.returncode))


def test_pipe_refuses_a_program_that_reads_its_code_from_stdin() -> None:
    """One stdin, two passengers — the leak of 2026-09-14, refused at the door.
    """
    p = subprocess.run([sys.executable, str(ROOT / "tools/use_secret.py"), "pipe", "DATABASE_URL",
                        "--", "python3", "-"],
                       input="secret-value-here", capture_output=True, text=True, timeout=60, env=SCRATCH_STATE)
    check("`python3 -` is refused", p.returncode == 2, str(p.returncode))
    check("with the reason and the fix, and never the value",
          "reads its PROGRAM from stdin" in p.stderr and "probe.py" in p.stderr
          and "secret-value-here" not in p.stderr, p.stderr[-200:])
    p2 = subprocess.run([sys.executable, str(ROOT / "tools/use_secret.py"), "pipe", "X", "--", "bash", "-s"],
                        input="v", capture_output=True, text=True, timeout=60, env=SCRATCH_STATE)
    check("and so is `bash -s`", p2.returncode == 2, str(p2.returncode))


def test_a_name_that_resolves_nowhere_says_where_it_looked() -> None:
    d, env = estate()
    p = subprocess.run([sys.executable, str(ROOT / "tools/use_secret.py"), "run",
                        "demo", "NOT_HERE", "--", sys.executable, "-c", "pass"],
                       env=env, capture_output=True, text=True, timeout=60)
    check("it refuses", p.returncode == 2, str(p.returncode))
    check("and names the vault and the inventory, not just 'not found'",
          "vault" in p.stderr and "inventory" in p.stderr, p.stderr[-160:])
    # The refusal names the installed command — `$(project-observatory
    # full-path)` resolves wherever the engine was installed — with this
    # project, environment and name filled in, so it can be run as written.
    check("and says how to add it, as the installed command",
          'full-path)/tools/vault.py" put demo local NOT_HERE' in p.stderr, p.stderr[-260:])


def test_an_explicit_env_never_falls_back_to_an_unlabeled_env_file() -> None:
    """`--env prod` silently took the project's unlabeled `.env` — the
    development file — and the refusal for a slot held only in another
    environment said it was in none of `{local,stage,prod}`."""
    d, env = estate()
    p = subprocess.run([sys.executable, str(ROOT / "tools/use_secret.py"), "where", "--env", "prod",
                        "demo", "DEMO_API_KEY"], env=env, capture_output=True, text=True, timeout=60)
    check("an explicit --env prod does not resolve to the unlabeled .env",
          p.returncode == 2 and "demo/.env" not in p.stdout, p.stdout + p.stderr[-200:])
    check("the refusal names the environment searched and the file that holds it",
          "{prod}" in p.stderr and "demo/.env (no environment in its name)" in p.stderr
          and PLANTED not in p.stdout + p.stderr, p.stderr[-300:])
    p = subprocess.run([sys.executable, str(ROOT / "tools/use_secret.py"), "where",
                        "demo", "DEMO_API_KEY"], env=env, capture_output=True, text=True, timeout=60)
    check("with no --env the unlabeled file still answers", "demo/.env" in p.stdout, p.stderr[-200:])


def test_pipe_with_a_missing_program_is_typed() -> None:
    d, env = estate()
    p = subprocess.run([sys.executable, str(ROOT / "tools/use_secret.py"), "pipe", "DEMO_API_KEY",
                        "--", "observatory-no-such-program-example"], input="synthetic-piped-value",
                       env=env, capture_output=True, text=True, timeout=60)
    check("pipe with a missing program exits 127 and says which",
          p.returncode == 127 and "command not found: observatory-no-such-program-example" in p.stderr
          and "input validation" not in p.stderr, f"exit {p.returncode}: {p.stderr[-200:]}")


def test_names_lists_without_values() -> None:
    d, env = estate()
    # `names` reads the REGISTRY document, which this fixture has not built — so
    # the assertion is about the refusal being informative, which is the state a
    # fresh clone is actually in.
    p = subprocess.run([sys.executable, str(ROOT / "tools/use_secret.py"), "names",
                        "demo"], env=env, capture_output=True, text=True, timeout=60)
    check("listing never prints a value", PLANTED not in p.stdout, "IT LEAKED")
    check("and it exits cleanly either way", p.returncode == 0, p.stderr[-120:])


def test_vault_only_refuses_a_project_env_file() -> None:
    """An agent's credentials live in the vault. With `--vault-only`, or
    `OBSERVATORY_VAULT_ONLY=1` in its environment, a name held only in the
    project's `.env` is an error that says how to move it — not a silent
    fallback that lets the rule erode one key at a time."""
    d, env = estate()
    child = careless_child(d, "DEMO_API_KEY")
    for label, extra, flag in (("--vault-only", {}, ["--vault-only"]),
                               ("OBSERVATORY_VAULT_ONLY=1", {"OBSERVATORY_VAULT_ONLY": "1"}, [])):
        p = subprocess.run([sys.executable, str(ROOT / "tools/use_secret.py"), "run", *flag,
                            "demo", "DEMO_API_KEY", "--", sys.executable, str(child)],
                           env={**env, **extra}, capture_output=True, text=True, timeout=60)
        check(f"{label}: the env-file fallback is refused", p.returncode != 0
              and "not in the vault" in p.stderr, p.stderr[-200:])
        check(f"{label}: and the refusal says how to store it", "vault.py" in p.stderr
              and "put demo" in p.stderr, p.stderr[-200:])
        check(f"{label}: and the child never ran", "len:" not in p.stdout, p.stdout[-80:])
        check(f"{label}: and no value was printed", PLANTED not in p.stdout + p.stderr,
              "IT LEAKED")
    p = subprocess.run([sys.executable, str(ROOT / "tools/use_secret.py"), "run",
                        "demo", "DEMO_API_KEY", "--", sys.executable, str(child)],
                       env={**env, "OBSERVATORY_VAULT_ONLY": "0"}, capture_output=True,
                       text=True, timeout=60)
    check("without it the fallback still works", f"len: {len(PLANTED)}" in p.stdout,
          p.stderr[-160:])


def test_serve_starts_a_service_from_the_vault_and_rotation_names_it() -> None:
    """A long-running agent service: values from the vault only, the process
    REPLACED by the service (same pid, launchd's signals reach it directly, its
    output is its own), and the consumer recorded so a rotation can name it."""
    d, env = estate()
    env = {**env, "OBSERVATORY_VAULT_DIR": str(d / "vault")}
    first = "sk-" + "or-v1-" + "Q1w2E3r4" * 4
    second = "sk-" + "or-v1-" + "Z9x8C7v6" * 4
    put = subprocess.run([sys.executable, str(ROOT / "tools/vault.py"), "put", "demo", "local",
                          "SERVICE_KEY"], input=first, env=env, capture_output=True, text=True,
                         timeout=60)
    check("a slot is put for the service", put.returncode == 0, put.stderr[-200:])
    svc = d / "service.py"
    svc.write_text("import os, sys\n"
                   "print('pid', os.getpid())\n"
                   "print('len', len(os.environ.get('SERVICE_KEY', '')))\n", encoding="utf-8")
    p = subprocess.run([sys.executable, str(ROOT / "tools/use_secret.py"), "serve",
                        "--consumer", "com.example.agent", "demo", "SERVICE_KEY", "--",
                        sys.executable, str(svc)], env=env, capture_output=True, text=True,
                       timeout=60)
    check("the service starts with the key", f"len {len(first)}" in p.stdout,
          p.stdout[-200:] + p.stderr[-200:])
    rows = [json.loads(l) for l in (d / "state" / "logs" / "secret-use.jsonl").read_text(
        encoding="utf-8").splitlines() if '"serve"' in l]
    pid_line = next((l for l in p.stdout.splitlines() if l.startswith("pid ")), "pid ?")
    check("and it IS the process serve was (exec, no pump in between)",
          rows and str(rows[-1].get("pid")) == pid_line.split()[1], f"{rows} / {pid_line}")
    check("the consumer is recorded by label, the value is not",
          rows and rows[-1].get("consumer") == "com.example.agent"
          and first not in json.dumps(rows), str(rows))
    only_env = subprocess.run([sys.executable, str(ROOT / "tools/use_secret.py"), "serve",
                               "demo", "DEMO_API_KEY", "--", sys.executable, str(svc)],
                              env=env, capture_output=True, text=True, timeout=60)
    check("serve never reads a project's .env", only_env.returncode != 0
          and "not in the vault" in only_env.stderr, only_env.stderr[-200:])
    rot = subprocess.run([sys.executable, str(ROOT / "tools/vault.py"), "rotate", "demo",
                          "local", "SERVICE_KEY"], input=second, env=env, capture_output=True,
                         text=True, timeout=60)
    check("a rotation names the service still on the old value",
          rot.returncode == 0 and "com.example.agent" in rot.stdout
          and "launchctl kickstart -k" in rot.stdout, rot.stdout[-300:] + rot.stderr[-200:])
    check("and prints neither value", first not in rot.stdout + rot.stderr
          and second not in rot.stdout + rot.stderr, "IT LEAKED")


def test_a_slot_is_released_only_under_a_grant() -> None:
    """OBS-57 (Fabric ADR-0144 amendment 2): an agent hands over its delegation token in
    FABRIC_DELEGATION_TOKEN (aud project-observatory, scope secret.use), and a vault slot is released only
    when a grant the token names has `spend.credentialSource {kind: project-key, ref: <the slot>}`. The
    token leaves the environment before the child starts and is never logged."""
    try:
        from test_meter import Estate, ISSUER, claims, grant
        Estate()
    except Exception:                                   # noqa: BLE001 — the full extra's cryptography
        print("  SKIP  slot grants need the cryptography package")
        return
    import time as _time
    d, env = estate()
    home = d / "home"
    (home / "config").mkdir(parents=True)
    grants = d / "grants"
    grants.mkdir(mode=0o700)
    grants.chmod(0o700)
    issuer = Estate()
    (d / "jwks.json").write_text(json.dumps(issuer.jwks), encoding="utf-8")
    config = {"issuer": ISSUER, "jwks": str(d / "jwks.json"), "grantsDir": str(grants)}
    (home / "config" / "meter.json").write_text(json.dumps(config), encoding="utf-8")
    env = {**env, "OBSERVATORY_VAULT_DIR": str(d / "vault"), "OBSERVATORY_HOME": str(home)}
    value = "sk-" + "or-v1-" + "Gr4nT5lo" * 4
    put = subprocess.run([sys.executable, str(ROOT / "tools/vault.py"), "put", "demo", "local", "SERVICE_KEY"],
                         input=value, env=env, capture_output=True, text=True, timeout=60)
    check("slot grants: a slot is put", put.returncode == 0, put.stderr[-200:])
    child = d / "child.py"
    child.write_text("import os\nprint('len', len(os.environ.get('SERVICE_KEY', '')))\n"
                     "print('token-inherited', 'FABRIC_DELEGATION_TOKEN' in os.environ)\n", encoding="utf-8")
    now = int(_time.time())

    def write_grant(source, **over):
        doc = grant(startsAt="2026-01-01T00:00:00Z", expiresAt="2099-01-01T00:00:00Z",
                    spend={"mode": "unlimited", "payer": "owner", "credentialSource": source}, **over)
        (grants / f"{doc['meta']['id']}.json").write_text(json.dumps(doc), encoding="utf-8")
        (grants / f"{doc['meta']['id']}.json").chmod(0o600)

    def run(token=None, names="SERVICE_KEY", extra=None):
        e = {**env, **(extra or {})}
        if token is not None:
            e["FABRIC_DELEGATION_TOKEN"] = token
        return subprocess.run([sys.executable, str(ROOT / "tools/use_secret.py"), "run", "demo", names, "--",
                               sys.executable, str(child)], env=e, capture_output=True, text=True, timeout=60)

    token = issuer.sign(claims(scope="secret.use", iat=now - 5, exp=now + 300))
    write_grant({"kind": "project-key", "ref": "demo/local/SERVICE_KEY"})
    p = run()
    check("slot grants: without a token the default mode still releases (until the issuer ships)",
          f"len {len(value)}" in p.stdout, p.stderr[-200:])
    p = run(token)
    check("slot grants: a grant naming the slot releases it", f"len {len(value)}" in p.stdout, p.stderr[-300:])
    check("slot grants: and the child does not inherit the token", "token-inherited False" in p.stdout, p.stdout)
    write_grant({"kind": "project-key", "ref": "demo/local/OTHER_KEY"})
    p = run(token)
    check("slot grants: a grant naming another slot is no-grant", p.returncode == 2 and "no-grant" in p.stderr
          and "len" not in p.stdout, p.stderr[-300:])
    write_grant({"kind": "capability", "agent": "urn:agent:example-writer", "capability": "writer.write"})
    p = run(token)
    check("slot grants: a capability source never releases a slot",
          p.returncode == 2 and "credential-source-refused" in p.stderr, p.stderr[-300:])
    write_grant({"kind": "project-key", "ref": "demo/local/SERVICE_KEY"}, state="revoked")
    p = run(token)
    check("slot grants: a revoked grant is grant-revoked", "grant-revoked" in p.stderr, p.stderr[-300:])
    write_grant({"kind": "project-key", "ref": "demo/local/SERVICE_KEY"})
    p = run(issuer.sign(claims(scope="grant.check", iat=now - 5, exp=now + 300)))
    check("slot grants: a token for another scope is principal-unproven", "principal-unproven" in p.stderr,
          p.stderr[-300:])
    p = run(token, names="DEMO_API_KEY")
    check("slot grants: with a token a project's .env is never read", p.returncode == 2
          and "not in the vault" in p.stderr and PLANTED not in p.stdout + p.stderr, p.stderr[-300:])
    (home / "config" / "meter.json").write_text(json.dumps({**config, "slotsRequireGrant": "always"}),
                                                encoding="utf-8")
    p = run()
    check("slot grants: `always` refuses a release without a token",
          p.returncode == 2 and "principal-unproven" in p.stderr and "len" not in p.stdout,
          f"rc={p.returncode} out={p.stdout[-200:]} err={p.stderr[-300:]}")
    p = run(token)
    check("slot grants: `always` releases under the grant", f"len {len(value)}" in p.stdout, p.stderr[-300:])
    journal = (d / "state" / "logs" / "secret-use.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(l) for l in journal.splitlines()]
    check("slot grants: the journal names the verdict", any(str(r.get("grant", "")).startswith("granted:")
                                                            for r in rows)
          and any(r.get("grant") == "no-grant" for r in rows), journal[-400:])
    check("slot grants: and never the token or the value", token not in journal and token.split(".")[2] not in
          journal and value not in journal, "IT LEAKED")


def test_a_dispatched_run_gets_only_its_packets_keys() -> None:
    """OBS-323 (Fabric's on-demand sessions spec §3.4): under a workflow Fabric wrote for a board request, a slot is
    released only to the lease holder (OBSERVATORY_WORKFLOW_LEASE, taken out of the environment), only when the
    packet's `credentials` name it, only from the project `fabric-project` resolves to, and only until the deadline.
    A helper that inherits FABRIC_DISPATCH_WORKFLOW without the lease is refused; the journal records the request."""
    import sqlite3 as _sqlite3
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    import memory_redact
    from store import workflow as W
    d, env = estate()
    fproject = "2d3e4f50-3333-4444-8555-666677778888"
    request = "0b1e2c3d-1111-4222-8333-444455556666"
    registry, scratch, db = d / "registry", pathlib.Path(env["OBSERVATORY_SCRATCH"]), d / "store.db"
    registry.mkdir()
    (registry / "projects.json").write_text(json.dumps({"projects": [
        {"id": "project:demo", "name": "demo", "local_folders": [str(d / "estate" / "demo")]}]}), encoding="utf-8")
    (scratch / "fabric-attribution.json").write_text(json.dumps(
        {"aliases": {"project:demo": {"alias": f"fabric:{fproject}"}}}), encoding="utf-8")
    env = {**env, "OBSERVATORY_VAULT_DIR": str(d / "vault"), "OBSERVATORY_DB": str(db),
           "OBSERVATORY_REGISTRY": str(registry)}
    value, other = "sk-" + "or-v1-" + "D1sp4tch" * 4, "sk-" + "or-v1-" + "0th3rK3y" * 4
    for name, v in (("DEMO_SLOT", value), ("OTHER_SLOT", other)):
        put = subprocess.run([sys.executable, str(ROOT / "tools/vault.py"), "put", "demo", "prod", name],
                             input=v, env=env, capture_output=True, text=True, timeout=60)
        check(f"dispatch: {name} is put", put.returncode == 0, put.stderr[-200:])
    conn = _sqlite3.connect(db)
    conn.row_factory = _sqlite3.Row
    conn.executescript((ROOT / "store/schema.sql").read_text(encoding="utf-8"))
    red = memory_redact.Redactor(known_loader=lambda: {})

    def packet(deadline):
        return {"goal": "answer the request", "credentials": [{"project": "demo", "env": "prod", "name": "DEMO_SLOT"}],
                "artifacts": [{"kind": "other", "ref": f"board-request:{request}"},
                              {"kind": "other", "ref": "capability:analytics.ask"},
                              {"kind": "other", "ref": f"fabric-project:{fproject}"},
                              {"kind": "other", "ref": "deadline:" + deadline.strftime("%Y-%m-%dT%H:%M:%SZ")}]}
    later = _dt.now(_tz.utc) + _td(hours=1)
    wf = W.checkpoint_write(conn, owner="service:fabric", idempotency_key="key-dispatch-0001", step_id="packet",
                            status="in_progress", body=packet(later), project_id="project:demo", redactor=red)
    conn.commit()
    child = d / "child.py"
    child.write_text("import os\nprint('len', len(os.environ.get('DEMO_SLOT', '')))\n"
                     "print('lease-inherited', 'OBSERVATORY_WORKFLOW_LEASE' in os.environ)\n", encoding="utf-8")

    def run(names="DEMO_SLOT", lease=wf["leaseId"], args=("--env", "prod", "--workflow", wf["workflowId"]),
            extra=None):
        e = {**env, **(extra or {})}
        if lease:
            e["OBSERVATORY_WORKFLOW_LEASE"] = lease
        return subprocess.run([sys.executable, str(ROOT / "tools/use_secret.py"), "run", *args, "demo", names, "--",
                               sys.executable, str(child)], env=e, capture_output=True, text=True, timeout=60)

    p = run()
    check("dispatch: the lease holder gets the named slot", f"len {len(value)}" in p.stdout, p.stderr[-300:])
    check("dispatch: and the child does not inherit the lease", "lease-inherited False" in p.stdout, p.stdout)
    for why, kw, code in (("without the lease", {"lease": None}, "not-lease-holder"),
                          ("with another lease", {"lease": "wl_" + "x" * 32}, "not-lease-holder"),
                          ("a slot the packet does not name", {"names": "OTHER_SLOT"}, "slot-not-named"),
                          ("without --env", {"args": ("--workflow", wf["workflowId"])}, "env-required"),
                          ("a helper that inherits only FABRIC_DISPATCH_WORKFLOW",
                           {"lease": None, "args": ("--env", "prod"),
                            "extra": {"FABRIC_DISPATCH_WORKFLOW": wf["workflowId"]}}, "not-lease-holder")):
        p = run(**kw)
        check(f"dispatch: {why} is {code}", p.returncode == 2 and code in p.stderr and "len" not in p.stdout,
              f"rc={p.returncode} err={p.stderr[-300:]}")
    # The packet is frozen at the dispatcher's write (B1): the run that holds the lease cannot widen it with its next
    # checkpoint, and an ordinary step that leaves it out keeps its keys.
    widened = packet(later + _td(days=30))
    widened["credentials"].append({"project": "demo", "env": "prod", "name": "OTHER_SLOT"})
    try:
        W.checkpoint_write(conn, owner="agent:claude-code", idempotency_key="key-dispatch-0002", step_id="widen",
                           status="in_progress", body=widened, workflow_id=wf["workflowId"],
                           lease_token=wf["leaseId"], redactor=red)
        frozen = False
    except W.PacketFrozen:
        frozen = True
    check("dispatch: a later checkpoint that widens the packet is refused", frozen)
    p = run(names="OTHER_SLOT")
    check("dispatch: and the widened slot is still not handed out", p.returncode == 2 and "slot-not-named" in p.stderr
          and "len" not in p.stdout, p.stderr[-300:])
    W.checkpoint_write(conn, owner="agent:claude-code", idempotency_key="key-dispatch-0003", step_id="step",
                       status="in_progress", body={"goal": "answer the request", "done": [
                           {"step_id": "read", "result": "read the request"}]},
                       workflow_id=wf["workflowId"], lease_token=wf["leaseId"], redactor=red)
    conn.commit()
    p = run()
    check("dispatch: a step that leaves the packet out keeps its keys", f"len {len(value)}" in p.stdout,
          p.stderr[-300:])
    # The lease from a file the session keeps (M2): never on a command line; refused unless it is this user's
    # private regular file.
    lease_file = d / "lease"
    lease_file.write_text(wf["leaseId"] + "\n", encoding="utf-8")
    import osprivacy
    osprivacy.make_private(lease_file)  # 0600 on POSIX, owner-only on Windows
    p = run(lease=None, extra={"OBSERVATORY_WORKFLOW_LEASE_FILE": str(lease_file)})
    check("dispatch: the lease is read from a private file", f"len {len(value)}" in p.stdout, p.stderr[-300:])
    child_env = d / "child_env.py"
    child_env.write_text("import os\nprint('file-inherited', 'OBSERVATORY_WORKFLOW_LEASE_FILE' in os.environ)\n",
                         encoding="utf-8")
    p = subprocess.run([sys.executable, str(ROOT / "tools/use_secret.py"), "run", "--env", "prod", "--workflow",
                        wf["workflowId"], "demo", "DEMO_SLOT", "--", sys.executable, str(child_env)],
                       env={**env, "OBSERVATORY_WORKFLOW_LEASE_FILE": str(lease_file)}, capture_output=True,
                       text=True, timeout=60)
    check("dispatch: and the child does not inherit the file's name", "file-inherited False" in p.stdout,
          p.stdout + p.stderr[-300:])
    p = run(lease=None, extra={"OBSERVATORY_WORKFLOW_LEASE_FILE": str(d / "no-such-lease")})
    check("dispatch: a lease file that is not there is lease-file-refused",
          p.returncode == 2 and "lease-file-refused" in p.stderr and "len" not in p.stdout, p.stderr[-300:])
    if os.name != "nt":  # Windows keeps no permission bits; its owner-only check is osprivacy's own suite
        os.chmod(lease_file, 0o644)
        p = run(lease=None, extra={"OBSERVATORY_WORKFLOW_LEASE_FILE": str(lease_file)})
        check("dispatch: a lease file others may read is lease-file-refused",
              p.returncode == 2 and "lease-file-refused" in p.stderr and "len" not in p.stdout, p.stderr[-300:])
        osprivacy.make_private(lease_file)
        link = d / "lease-link"
        link.symlink_to(lease_file)
        p = run(lease=None, extra={"OBSERVATORY_WORKFLOW_LEASE_FILE": str(link)})
        check("dispatch: a link to a lease file is lease-file-refused",
              p.returncode == 2 and "lease-file-refused" in p.stderr and "len" not in p.stdout, p.stderr[-300:])
    # A request whose deadline has passed: its own workflow, since no checkpoint may move a packet's deadline.
    past = W.checkpoint_write(conn, owner="service:fabric", idempotency_key="key-dispatch-0004", step_id="packet",
                              status="in_progress", body=packet(_dt.now(_tz.utc) - _td(minutes=1)),
                              project_id="project:demo", redactor=red)
    conn.commit()
    p = run(lease=past["leaseId"], args=("--env", "prod", "--workflow", past["workflowId"]))
    check("dispatch: after the deadline nothing is handed out", p.returncode == 2 and "deadline-passed" in p.stderr
          and "len" not in p.stdout, p.stderr[-300:])
    conn.close()
    journal = (d / "state" / "logs" / "secret-use.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in journal.splitlines()]
    check("dispatch: the journal records the request a release served",
          any(r.get("action") == "use" and r.get("request") == f"board-request:{request}" for r in rows), journal[-400:])
    check("dispatch: and each refusal by its code", {r.get("dispatch") for r in rows if r.get("action") == "use-refused"}
          >= {"not-lease-holder", "slot-not-named", "env-required", "deadline-passed"}, journal[-400:])
    check("dispatch: and a lease file it would not read",
          "lease-file-refused" in {r.get("dispatch") for r in rows if r.get("action") == "use-refused"}, journal[-400:])
    check("dispatch: never the lease or a value", wf["leaseId"] not in journal and past["leaseId"] not in journal
          and value not in journal and other not in journal, "IT LEAKED")


def test_the_lease_file_is_judged_on_its_opened_descriptor() -> None:
    """OBS-344: the lease file is opened without following a link and without waiting, and whether it is a regular
    file, this user's and private is read from the OPENED descriptor, never from the path beforehand, so a file
    swapped in between a look and the open cannot be read. The swap itself cannot be timed in a test; what is shown
    is that the decision follows the descriptor: with every path-based stat answering a lie, a private file still
    passes and a readable one is still refused. A FIFO is refused without waiting, a link is refused."""
    from unittest import mock
    import osprivacy
    import use_secret as U
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-lease-fd-"))
    lease = "wl_" + "L" * 32
    good = d / "lease"
    good.write_text(lease + "\n", encoding="utf-8")
    osprivacy.make_private(good)
    saved = (U._WORKFLOW_LEASE, U._LEASE_FILE)

    def read(path: pathlib.Path) -> tuple[str | None, str]:
        U._WORKFLOW_LEASE, U._LEASE_FILE = None, str(path)
        try:
            return U.workflow_lease(), ""
        except U.LeaseFileRefused as exc:
            return None, str(exc)

    try:
        got, why = read(good)
        check("lease fd: a private regular file of this user is read", got == lease, why)
        check("lease fd: and the refusal text never carries it", lease not in why)
        if os.name != "nt":  # Windows keeps no permission bits and no FIFOs; its owner-only check is osprivacy's suite
            real_stat, real_lstat = os.stat, os.lstat

            def lie(real, private: bool):
                def fake(target, *a, **kw):
                    info = real(target, *a, **kw)
                    mode = (info.st_mode & ~0o777) | (0o600 if private else 0o644)
                    return os.stat_result((mode,) + tuple(info)[1:])
                return fake

            # Every path-based stat says the file is shared: the descriptor says private, and it is read.
            with mock.patch.object(os, "stat", lie(real_stat, False)), \
                    mock.patch.object(os, "lstat", lie(real_lstat, False)):
                got, why = read(good)
            check("lease fd: a path stat that lies does not refuse a private file", got == lease, why)
            shared = d / "shared"
            shared.write_text(lease + "\n", encoding="utf-8")
            os.chmod(shared, 0o644)
            # Every path-based stat says the file is private: the descriptor says 0644, and it is refused.
            with mock.patch.object(os, "stat", lie(real_stat, True)), \
                    mock.patch.object(os, "lstat", lie(real_lstat, True)):
                got, why = read(shared)
            check("lease fd: a path stat that lies does not pass a shared file", got is None and "chmod 600" in why,
                  why)
            got, why = read(shared)
            check("lease fd: mode 644 is refused", got is None and "chmod 600" in why, why)
            link = d / "lease-link"
            link.symlink_to(good)
            got, why = read(link)
            check("lease fd: a link is refused, not followed", got is None and "link" in why, why)
            fifo = d / "lease-fifo"
            os.mkfifo(fifo)
            got, why = read(fifo)  # opened non-blocking: a FIFO nobody writes would otherwise hang here
            check("lease fd: a FIFO is refused without waiting", got is None and "not a regular file" in why, why)
            folder = d / "lease-folder"
            folder.mkdir()
            got, why = read(folder)
            check("lease fd: a folder is refused", got is None and why != "", why)
        big = d / "big"
        big.write_text("x" * 5000, encoding="utf-8")
        osprivacy.make_private(big)
        got, why = read(big)
        check("lease fd: more than 4 KiB is refused", got is None and why != "", why)
    finally:
        U._WORKFLOW_LEASE, U._LEASE_FILE = saved


def test_where_reports_the_source_it_would_take() -> None:
    d, env = estate()
    p = subprocess.run([sys.executable, str(ROOT / "tools/use_secret.py"), "where",
                        "demo", "DEMO_API_KEY"], env=env, capture_output=True,
                       text=True, timeout=60)
    check("`where` names the file", "demo/.env" in p.stdout, p.stdout.strip())
    check("and prints no value", PLANTED not in p.stdout, "IT LEAKED")


#: Every variable that moves an agent's transcript home away from HOME (paths.transcript_homes).
TRANSCRIPT_OVERRIDES = ("CODEX_HOME", "CODEX_SQLITE_HOME", "HERMES_HOME", "HERMES_DATA_DIR_SUFFIX",
                        "XDG_DATA_HOME", "OPENCODE_DB", "CLAUDE_CONFIG_DIR")


def transcripts_isolated(env: dict, d: pathlib.Path) -> dict:
    """The leak scan reads every agent's transcript home under HOME (I-5): HOME is a synthetic,
    empty folder here and no override survives, so a test never reads the machine's own transcripts.
    The estate's workspace, state and scratch stay where `estate()` put them (OBSERVATORY_*)."""
    user = d / "user"
    user.mkdir(exist_ok=True)
    out = {k: v for k, v in env.items() if k not in TRANSCRIPT_OVERRIDES}
    return {**out, "HOME": str(user), "USERPROFILE": str(user)}


# ── the leak hunt ──────────────────────────────────────────────────────────

def test_the_leak_scan_finds_a_planted_value() -> None:
    d, env = estate()
    sessions = d / "sessions" / "-Users-x-demo"
    sessions.mkdir(parents=True)
    (sessions / "abc.jsonl").write_text(
        '{"role":"assistant","text":"the connection failed with key '
        + PLANTED + ' returned by the provider"}\n', encoding="utf-8")
    env2 = {**env, "OBSERVATORY_SESSIONS": str(d / "sessions")}
    p = subprocess.run([sys.executable, str(ROOT / "tools/scan_leaks.py"), "--full"],
                       env=transcripts_isolated(env2, d), capture_output=True, text=True, timeout=300,
                       cwd=str(ROOT))
    out = json.loads(((d / "scratch") / "leak-scan.json").read_text(encoding="utf-8"))
    hits = [h for h in out["hits"] if "abc.jsonl" in h["where"]]
    check("the planted value is found in the transcript", len(hits) == 1, str(out["hits"]))
    check("and the hit names the variable", hits and hits[0]["secret"].endswith("DEMO_API_KEY"),
          str(hits))
    # THE REPORT MUST NOT BE A SECOND COPY OF THE LEAK.
    check("the report does not quote the value",
          PLANTED not in json.dumps(out), "the report carries the value it found")
    check("nor does what it printed", PLANTED not in p.stdout + p.stderr, "IT LEAKED")


def test_the_observatory_store_is_in_the_leak_scan() -> None:
    """Agent memory lives in the Observatory's own store. Writes into it are
    redacted, so a value found there means the redactor missed one — the alarm
    for the safety net itself."""
    d, env = estate()
    db = d / "observatory.db"
    conn = sqlite3.connect(db)
    conn.executescript((ROOT / "store/schema.sql").read_text(encoding="utf-8"))
    conn.execute("INSERT INTO ledger (memory_id, revision, kind, function, scope, statement,"
                 " state, owner, created_at) VALUES ('mem:x', 1, 'note', 'episodic', 'project',"
                 " ?, 'proposed', 'agent:x', '2026-01-01T00:00:00Z')",
                 (f"the provider returned {PLANTED}",))
    conn.commit()
    conn.close()
    env2 = {**env, "OBSERVATORY_DB": str(db)}
    subprocess.run([sys.executable, str(ROOT / "tools/scan_leaks.py"), "--full"], env=transcripts_isolated(env2, d),
                   capture_output=True, text=True, timeout=300, cwd=str(ROOT))
    out = json.loads(((d / "scratch") / "leak-scan.json").read_text(encoding="utf-8"))
    hits = [h for h in out["hits"] if str(db) in h["where"]]
    check("a value in the store is found", len(hits) == 1 and "ledger" in hits[0]["where"],
          str(out["hits"])[:300])
    check("and the store is reported as read", (out.get("observatory_store") or {}).get("read"),
          str(out.get("observatory_store")))
    check("without quoting the value", PLANTED not in json.dumps(out), "IT LEAKED")


def test_the_companion_store_is_read_as_a_database_not_named_as_unread() -> None:
    """The companion's SQLite store holds session summaries — the shape that
    has leaked before — and the scanner once only NAMED it as unread. Driven on a planted store: a value in a text column is a sighting
    that names the table and column, never the row; a store that will not
    open is a note, not silence."""
    import sqlite3
    d, env = estate()
    mem = d / "claude-mem.db"
    c = sqlite3.connect(mem)
    c.execute("CREATE TABLE observations (id INTEGER PRIMARY KEY, summary TEXT, meta TEXT)")
    c.execute("INSERT INTO observations (summary, meta) VALUES (?, ?)",
              ("the call failed with key " + PLANTED + " in the header", '{"k":1}'))
    c.execute("INSERT INTO observations (summary, meta) VALUES (?, ?)", ("nothing here", None))
    c.commit(); c.close()
    env2 = {**env, "OBSERVATORY_SESSIONS": str(d / "nosessions"), "CLAUDE_MEM_DB": str(mem)}
    p = subprocess.run([sys.executable, str(ROOT / "tools/scan_leaks.py"), "--full"],
                       env=transcripts_isolated(env2, d), capture_output=True, text=True, timeout=300, cwd=str(ROOT))
    out = json.loads(((d / "scratch") / "leak-scan.json").read_text(encoding="utf-8"))
    hits = [h for h in out["hits"] if "claude-mem.db" in h["where"]]
    check("the planted value is found inside the SQLite store",
          len(hits) == 1 and hits[0]["occurrences"] == 1, str(out["hits"]))
    check("and the sighting names table and column, never the row",
          hits and hits[0]["where"].endswith("#observations.summary"), str(hits))
    check("the store is recorded as read", out.get("companion_store", {}).get("read") is True, str(out.get("companion_store")))
    check("and no longer appears among what was not scanned",
          not any("claude-mem" in n.get("what", "") for n in out.get("not_scanned", [])), str(out.get("not_scanned")))
    check("the report does not quote the value", PLANTED not in json.dumps(out) + p.stdout + p.stderr, "IT LEAKED")
    broken = d / "broken.db"
    broken.write_text("not a database", encoding="utf-8")
    subprocess.run([sys.executable, str(ROOT / "tools/scan_leaks.py"), "--full"],
                   env=transcripts_isolated({**env2, "CLAUDE_MEM_DB": str(broken)}, d), capture_output=True, text=True, timeout=300, cwd=str(ROOT))
    out2 = json.loads(((d / "scratch") / "leak-scan.json").read_text(encoding="utf-8"))
    check("a store that will not open is named among what was not scanned, with the reason",
          any("broken.db" in n.get("what", "") and "would not open" in n.get("why", "") for n in out2.get("not_scanned", [])),
          str(out2.get("not_scanned")))


def test_the_file_a_value_lives_in_is_not_a_sighting() -> None:
    """Its own `.env` is its home, not a leak, or every secret is one."""
    d, env = estate()
    env2 = {**env, "OBSERVATORY_SESSIONS": str(d / "nosessions")}
    subprocess.run([sys.executable, str(ROOT / "tools/scan_leaks.py"), "--full"],
                   env=transcripts_isolated(env2, d), capture_output=True, text=True, timeout=300, cwd=str(ROOT))
    out = json.loads(((d / "scratch") / "leak-scan.json").read_text(encoding="utf-8"))
    homes = [h for h in out["hits"] if h["where"].endswith("demo/.env")]
    check("a value in its own file raises nothing", not homes, str(homes))


def test_an_undistinctive_value_is_named_rather_than_hunted() -> None:
    """`SESSION_NAME=examplebot_x` matched 34115 times and meant nothing."""
    # THE SHAPE THAT CAUSED IT: fifteen characters, one token, no separators —
    # so `wordish` is false and the NAME rule files it as a secret, while
    # `looks_secret` correctly refuses to call it distinctive. A value in exactly
    # this band is what produced 34115 false sightings.
    d, env = estate(with_value="examplesessions")
    env2 = {**env, "OBSERVATORY_SESSIONS": str(d / "nosessions")}
    subprocess.run([sys.executable, str(ROOT / "tools/scan_leaks.py"), "--full"],
                   env=transcripts_isolated(env2, d), capture_output=True, text=True, timeout=300, cwd=str(ROOT))
    out = json.loads(((d / "scratch") / "leak-scan.json").read_text(encoding="utf-8"))
    skipped = [s for s in out.get("skipped", []) if "DEMO_API_KEY" in s["what"]]
    check("it is not searched for", not any(
        h["secret"].endswith("DEMO_API_KEY") for h in out["hits"]), str(out["hits"]))
    check("and the reason is recorded rather than implied", len(skipped) == 1, str(out.get("skipped")))
    check("naming the shape, not the length",
          skipped and "shape" in skipped[0]["why"], str(skipped))


def test_the_rules_split_a_credential_from_an_identifier() -> None:
    import leak_findings
    rows = leak_findings.sightings({"hits": [
        {"secret": "a/STRIPE_SECRET_KEY", "where": "/x/.claude/projects/p/s.jsonl",
         "occurrences": 2},
        {"secret": "b/CLOUDFLARE_ACCOUNT_ID", "where": "/x/.claude/projects/p/s.jsonl",
         "occurrences": 9},
    ]})
    kinds = {r["type"]: r["severity"] for r in rows}
    check("a credential sighting is critical",
          kinds.get("secret.seen_outside_its_home") == "critical", str(kinds))
    check("an account id is not", kinds.get("secret.identifier_seen") == "info", str(kinds))
    check("and they are two rows, because the remedy differs", len(rows) == 2, str(len(rows)))
    check("the place is named in a form a person recognises",
          any("session transcript" in r["detail"] for r in rows), str(rows)[:200])


def test_nothing_measured_is_not_nothing_wrong() -> None:
    import leak_findings
    rows = leak_findings.findings({"degraded": [{"why": "no values"}], "hits": []})
    check("a scan with nothing to look for says so",
          len(rows) == 1 and rows[0]["type"] == "secret.leak_scan_unmeasured", str(rows))
    check("no document at all raises nothing", leak_findings.findings(None) == [])


def test_what_the_scan_could_not_open_is_named() -> None:
    """A clean scan is only as wide as what it opened — a source the scanner
    could not read leaves as a row, or its silence reads as a clean bill."""
    import leak_findings
    rows = leak_findings.findings({"hits": [], "not_scanned": [
        {"what": "~/.claude-mem/claude-mem.db", "why": "sqlite, no reader here"}]})
    check("the blind spot is a row, under the name the board carries",
          len(rows) == 1 and rows[0]["type"] == "secret.leak_scan_blind", str(rows))
    check("and it names what was not read and why",
          bool(rows) and "claude-mem.db" in rows[0]["detail"]
          and "no reader" in rows[0]["detail"], str(rows)[:200])


def test_a_flag_after_the_names_and_a_missing_program_are_typed() -> None:
    """`run demo NAME --env local -- cmd` put `--env` into the command and ran it;
    a program that does not exist ended in a traceback, after an audit row that
    recorded a use that never happened."""
    d, env = estate()
    audit = pathlib.Path(env["OBSERVATORY_STATE"]) / "logs" / "secret-use.jsonl"
    p = subprocess.run([sys.executable, str(ROOT / "tools/use_secret.py"), "run", "demo", "DEMO_API_KEY",
                        "--env", "local", "--", "true"], env=env, capture_output=True, text=True, timeout=60)
    check("a flag after the names is refused", p.returncode == 2, f"exit {p.returncode}")
    check("naming the order flags go in", "run --env ENV PROJECT NAME -- COMMAND" in p.stderr, p.stderr[-200:])
    p = subprocess.run([sys.executable, str(ROOT / "tools/use_secret.py"), "run", "demo", "DEMO_API_KEY",
                        "--", "observatory-no-such-program-example"], env=env, capture_output=True, text=True, timeout=60)
    check("a missing program exits 127", p.returncode == 127, f"exit {p.returncode}")
    check("and says which", "command not found: observatory-no-such-program-example" in p.stderr
          and "Traceback" not in p.stderr, p.stderr[-200:])
    rows = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()] if audit.is_file() else []
    check("the audit says the use failed rather than happened",
          any(r.get("action") == "use-failed" for r in rows), str(rows)[-300:])
    check("and no value reached the audit", PLANTED not in (audit.read_text(encoding="utf-8") if audit.is_file() else ""))


if __name__ == "__main__":
    print("a secret reaches the command and not the transcript\n")
    for fn in (test_a_flag_after_the_names_and_a_missing_program_are_typed,
               test_the_value_reaches_the_command,
               test_and_not_the_transcript,
               test_a_value_split_across_a_buffer_is_still_removed,
               test_a_name_that_resolves_nowhere_says_where_it_looked,
               test_an_explicit_env_never_falls_back_to_an_unlabeled_env_file,
               test_pipe_with_a_missing_program_is_typed,
               test_names_lists_without_values,
               test_where_reports_the_source_it_would_take,
               test_vault_only_refuses_a_project_env_file,
               test_serve_starts_a_service_from_the_vault_and_rotation_names_it,
               test_a_slot_is_released_only_under_a_grant,
               test_a_dispatched_run_gets_only_its_packets_keys,
               test_the_lease_file_is_judged_on_its_opened_descriptor,
               test_the_leak_scan_finds_a_planted_value,
               test_the_file_a_value_lives_in_is_not_a_sighting,
               test_an_undistinctive_value_is_named_rather_than_hunted,
               test_the_rules_split_a_credential_from_an_identifier,
               test_nothing_measured_is_not_nothing_wrong,
               test_what_the_scan_could_not_open_is_named,
               test_the_companion_store_is_read_as_a_database_not_named_as_unread,
               test_the_observatory_store_is_in_the_leak_scan,
               test_pipe_hands_a_stdin_value_to_a_command_and_scrubs_its_output,
               test_pipe_refuses_a_program_that_reads_its_code_from_stdin):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe name travels, the value does not\033[0m")
