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
import json, os, pathlib, subprocess, sys

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


def test_where_reports_the_source_it_would_take() -> None:
    d, env = estate()
    p = subprocess.run([sys.executable, str(ROOT / "tools/use_secret.py"), "where",
                        "demo", "DEMO_API_KEY"], env=env, capture_output=True,
                       text=True, timeout=60)
    check("`where` names the file", "demo/.env" in p.stdout, p.stdout.strip())
    check("and prints no value", PLANTED not in p.stdout, "IT LEAKED")


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
                       env=env2, capture_output=True, text=True, timeout=300,
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
                       env=env2, capture_output=True, text=True, timeout=300, cwd=str(ROOT))
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
                   env={**env2, "CLAUDE_MEM_DB": str(broken)}, capture_output=True, text=True, timeout=300, cwd=str(ROOT))
    out2 = json.loads(((d / "scratch") / "leak-scan.json").read_text(encoding="utf-8"))
    check("a store that will not open is named among what was not scanned, with the reason",
          any("broken.db" in n.get("what", "") and "would not open" in n.get("why", "") for n in out2.get("not_scanned", [])),
          str(out2.get("not_scanned")))


def test_the_file_a_value_lives_in_is_not_a_sighting() -> None:
    """Its own `.env` is its home, not a leak, or every secret is one."""
    d, env = estate()
    env2 = {**env, "OBSERVATORY_SESSIONS": str(d / "nosessions")}
    subprocess.run([sys.executable, str(ROOT / "tools/scan_leaks.py"), "--full"],
                   env=env2, capture_output=True, text=True, timeout=300, cwd=str(ROOT))
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
                   env=env2, capture_output=True, text=True, timeout=300, cwd=str(ROOT))
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
               test_the_leak_scan_finds_a_planted_value,
               test_the_file_a_value_lives_in_is_not_a_sighting,
               test_an_undistinctive_value_is_named_rather_than_hunted,
               test_the_rules_split_a_credential_from_an_identifier,
               test_nothing_measured_is_not_nothing_wrong,
               test_what_the_scan_could_not_open_is_named,
               test_the_companion_store_is_read_as_a_database_not_named_as_unread,
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
