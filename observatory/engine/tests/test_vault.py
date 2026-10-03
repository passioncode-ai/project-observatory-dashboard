#!/usr/bin/env python3
"""The vault's contract, driven — including the two refusals that matter most.

Everything runs against a scratch store through `OBSERVATORY_VAULT_DIR`; the
machine's real secrets directory is never touched. The two properties worth the
most here are refusals: an inject into a repository whose `.env` is committable
must not write, and a leak record without a place must not append — both were
designed first and the tool grew around them.
"""
from __future__ import annotations
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                           

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def vault(store: pathlib.Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run([PY, "tools/vault.py", *args], input=stdin, cwd=ROOT,
                          env={**os.environ, "OBSERVATORY_VAULT_DIR": str(store)},
                          capture_output=True, text=True, timeout=120)


# Distinctive synthetic values: a check that a value is absent from a finding
# must not trip on a random temporary path that happens to contain "v1".
BOARD_V1 = "synthetic-board-value-5f0c1e"
BOARD_V2 = "synthetic-board-value-9a73d4"


def settlement_evidence() -> tuple[str, ...]:
    return ("--revocation-evidence", "fake-provider receipt revoke-old-version-7",
            "--consumer-evidence", "fixture consumers api and worker passed with replacement version")


def fresh() -> pathlib.Path:
    return pathlib.Path(tmpdir.mkdtemp(prefix="observatory-vault-")).resolve() / "projects"


def test_a_value_travels_only_on_stdin() -> None:
    s = fresh()
    p = vault(s, "put", "demo", "local", "API_TOKEN")                                
    check("empty stdin is refused", p.returncode != 0 and "empty" in p.stderr,
          p.stderr[:120])
    src = (ROOT / "tools/vault.py").read_text(encoding="utf-8")
    check("no subcommand takes a value argument",
          "add_argument(\"value\"" not in src and "--value" not in src,
          "a value in argv is a value in `ps` and the shell history")


def test_put_list_rotate_and_the_archive() -> None:
    s = fresh()
    p = vault(s, "put", "demo", "prod", "DB_URL", stdin="postgres://u:p@h/db")
    check("put reports length without the value or its tail", p.returncode == 0
          and "length" in p.stdout and "postgres" not in p.stdout
          and "h/db" not in p.stdout, p.stdout[:150])
    slot = s / "demo/prod/DB_URL"
    check("the file is mode 600", oct(slot.stat().st_mode & 0o777) == "0o600",
          oct(slot.stat().st_mode & 0o777))
    # The umask says 755 and `mkdir` obeys it, so until 2026-09-14 every value
    # sat 600 inside a directory anyone could list — the store's parent being
    # 700 was the only thing in the way. A store's own directories say 700.
    dirs = {str(d.relative_to(s.parent)): oct(d.stat().st_mode & 0o777)
            for d in (s, s / "demo", s / "demo/prod")}
    check("every directory from the store down is mode 700",
          set(dirs.values()) == {"0o700"}, str(dirs))
    p = vault(s, "put", "demo", "prod", "DB_URL", stdin="other")
    check("a second put is refused toward rotate", p.returncode != 0
          and "rotate" in p.stderr, p.stderr[:120])
    p = vault(s, "list")
    check("list never prints a value", "postgres" not in p.stdout, p.stdout[:200])
    p = vault(s, "rotate", "demo", "prod", "DB_URL", stdin="postgres://u:NEW@h/db")
    check("rotate archives the old value", p.returncode == 0 and "archived" in p.stdout,
          p.stdout[:150])
    archives = list((s / "demo/prod").glob("DB_URL.retired-*"))
    check("the archive exists at mode 600", len(archives) == 1
          and oct(archives[0].stat().st_mode & 0o777) == "0o600", str(archives))
    check("and rotate warns the retired value still works until revoked",
          "until revoked" in p.stdout, p.stdout)
    p = vault(s, "rotate", "demo", "prod", "DB_URL", stdin="postgres://u:NEW@h/db")
    check("rotating to the same value is refused", p.returncode != 0
          and "not a rotation" in p.stderr, p.stderr[:120])


def test_the_vocabulary_is_enforced() -> None:
    s = fresh()
    p = vault(s, "put", "demo", "production", "X", stdin="v")
    check("an env outside local/stage/prod is refused",
          p.returncode != 0 and "local, stage, prod" in p.stderr, p.stderr[:120])
    p = vault(s, "put", "demo", "local", "api-token", stdin="v")
    check("a name that is not UPPER_SNAKE_CASE is refused",
          p.returncode != 0 and "UPPER_SNAKE_CASE" in p.stderr, p.stderr[:120])


def _token_shaped() -> str:
    """A synthetic, obviously fake run in the shape of a pasted token: 37
    characters mixing classes. Composed, so no literal here looks like a key."""
    return "Fake" + "Tokn" + "_" + "Ab3" * 9 + "x"


def test_a_credential_shaped_name_or_note_never_reaches_the_register() -> None:
    """A token pasted as the PROJECT was accepted (any 1–64 identifier passed)
    and then printed in the registry, the findings, five dashboard pages and the
    MCP answers. Every write door now refuses a credential-shaped name, and a
    place or a settlement note never carries the value onward."""
    s = fresh()
    tok = _token_shaped()
    for verb, extra, stdin in (("put", (), "synthetic-value-1"),
                               ("leak", ("--where", "a CI log, job 4412"), ""),
                               ("rotate", (), "synthetic-value-2"),
                               ("moved", ("--how", "rotated in the provider dashboard today"), ""),
                               ("remove", (), "")):
        p = vault(s, verb, tok, "prod", "API_TOKEN", *extra, stdin=stdin)
        check(f"`{verb}` refuses a credential-shaped project, naming the field and not the value",
              p.returncode != 0 and "credential" in p.stderr and tok not in p.stdout + p.stderr,
              (p.stdout + p.stderr)[-200:])
    aws_like = "AKIA" + "FAKE" * 3 + "0000"
    p = vault(s, "put", "demo", "prod", aws_like, stdin="synthetic-value-3")
    check("a NAME in a key id's shape is refused even though it is UPPER_SNAKE",
          p.returncode != 0 and aws_like not in p.stdout + p.stderr, (p.stdout + p.stderr)[-200:])
    check("and nothing was written for any of them",
          not any(s.rglob("API_TOKEN")) and not any(s.rglob(aws_like))
          and not (s / "leaks.jsonl").exists())
    p = vault(s, "leak", "demo", "prod", "API_TOKEN", "--where", f"pasted {tok} into a review thread")
    reg = (s / "leaks.jsonl").read_text(encoding="utf-8") if (s / "leaks.jsonl").exists() else ""
    moves = (s / "movements.jsonl").read_text(encoding="utf-8") if (s / "movements.jsonl").exists() else ""
    check("a place that carries the value is recorded with the value redacted",
          p.returncode == 0 and "[redacted]" in reg and "review thread" in reg
          and tok not in reg + moves + p.stdout + p.stderr, (p.stdout + p.stderr)[-200:])
    p = vault(s, "settle", "demo", "prod", "API_TOKEN", "--how", f"revoked {tok} at the provider",
              *settlement_evidence())
    check("a settlement note carrying a value is refused, without echoing it",
          p.returncode != 0 and tok not in p.stdout + p.stderr
          and tok not in (s / "leaks.jsonl").read_text(encoding="utf-8"), (p.stdout + p.stderr)[-200:])
    p = vault(s, "put", "alpha-web", "prod", "OPENROUTER_API_KEY", stdin="synthetic-value-4")
    check("an ordinary project and NAME still store", p.returncode == 0, p.stderr[-160:])


def test_inject_refuses_a_committable_env_and_writes_an_ignored_one() -> None:
    s = fresh()
    vault(s, "put", "demo", "local", "API_TOKEN", stdin="sk-test-1234abcd")
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-vault-inj-")).resolve()
    subprocess.run(["git", "init", "-q"], cwd=d, capture_output=True, timeout=60)
    p = vault(s, "inject", "demo", "local", str(d))
    check("inject refuses while .env is committable", p.returncode != 0
          and "NOT gitignored" in p.stderr, p.stderr[:160])
    check("and wrote nothing", not (d / ".env").exists())
    (d / ".gitignore").write_text(".env\n", encoding="utf-8")
    p = vault(s, "inject", "demo", "local", str(d))
    check("inject writes once .env is ignored", p.returncode == 0, p.stderr[:120])
    body = (d / ".env").read_text(encoding="utf-8")
    check("the .env carries the value for the NAME the agent will use",
          "API_TOKEN=sk-test-1234abcd" in body, body[:150])
    check("at mode 600", oct((d / ".env").stat().st_mode & 0o777) == "0o600")
    check("and the report speaks names, not values",
          "API_TOKEN" in p.stdout and "sk-test-1234abcd" not in p.stdout, p.stdout[:200])
    (d / ".env").write_text("HANDMADE=1\n" + body, encoding="utf-8")
    p = vault(s, "inject", "demo", "local", str(d))
    check("re-inject keeps lines it does not own",
          "HANDMADE=1" in (d / ".env").read_text(encoding="utf-8"),
          "an inject that truncates the file destroys hand-kept settings")


def test_a_leak_needs_a_place_and_stays_open_after_local_rotation() -> None:
    s = fresh()
    vault(s, "put", "demo", "prod", "API_TOKEN", stdin="sk-live-zzzz")
    p = vault(s, "leak", "demo", "prod", "API_TOKEN", "--where", "log")
    check("a leak record without a real place is refused", p.returncode != 0
          and "names no place" in p.stderr.replace("\n", " ") or "cannot be judged" in p.stderr,
          p.stderr[:160])
    p = vault(s, "leak", "demo", "prod", "API_TOKEN",
              "--where", "committed in demo repo at abc123, file config.py")
    check("a leak with a place is recorded", p.returncode == 0 and "recorded" in p.stdout,
          p.stdout[:150])
    check("leak command requires external revocation", "settles this row" not in p.stdout
          and "verify consumers" in p.stdout, p.stdout[:160])
    p = vault(s, "leaks", "--check")
    check("`leaks --check` exits non-zero while one is open", p.returncode == 1
          and "OPEN" in p.stdout, p.stdout[:200])
    reg = json.loads((s / "leaks.jsonl").read_text(encoding="utf-8").splitlines()[0])
    check("the register holds names and places, never values",
          "sk-live-zzzz" not in json.dumps(reg), str(reg)[:150])
    check("the leak hint does not promise settlement by local rotation",
          "settles this row" not in p.stdout)
    before = (s / "leaks.jsonl").read_bytes()
    put = vault(s, "put", "demo", "prod", "API_TOKEN", "--force", stdin="synthetic-replacement-before-rotate")
    check("forced local put leaves the leak register unchanged",
          put.returncode == 0 and before == (s / "leaks.jsonl").read_bytes())
    rotated = vault(s, "rotate", "demo", "prod", "API_TOKEN", stdin="sk-live-NEW")
    p = vault(s, "leaks", "--check")
    check("local rotation leaves the leak open", p.returncode == 1 and "OPEN" in p.stdout,
          p.stdout[:200])
    check("local rotation does not write a settlement", before == (s / "leaks.jsonl").read_bytes())
    check("local rotation never claims a settled leak", "now settled" not in rotated.stdout,
          rotated.stdout[:200])


def test_a_leak_of_a_key_the_store_never_held_is_still_recorded() -> None:
    s = fresh()
    p = vault(s, "leak", "demo", "prod", "UNKNOWN_KEY",
              "--where", "found in an old CI log, job 4412")
    check("recorded with the note", p.returncode == 0 and "recorded anyway" in p.stdout,
          p.stdout[:200])


def test_a_leak_rotated_at_the_provider_is_settled_by_hand_with_evidence() -> None:
    """`settle` — the verb that did not exist on 2026-09-14."""
    store = fresh()
    vault(store, "leak", "example-app", "prod", "DATABASE_URL", "--where", "a traceback in a transcript")
    p = vault(store, "rotate", "example-app", "prod", "DATABASE_URL", stdin="new-value")
    check("rotate on a slotless name points at settle, not at put alone",
          p.returncode != 0 and "settle" in (p.stdout + p.stderr), (p.stdout + p.stderr)[-200:])
    p = vault(store, "settle", "example-app", "prod", "DATABASE_URL", "--how", "short")
    check("a settlement with no real evidence is refused", p.returncode != 0
          and "evidence" in (p.stdout + p.stderr), (p.stdout + p.stderr)[-160:])
    p = vault(store, "settle", "example-app", "prod", "DATABASE_URL",
              "--how", "heroku pg:credentials:rotate on example-app; releases v1080/v1081 2026-09-14", *settlement_evidence())
    check("with evidence it settles", p.returncode == 0 and "settled 1 open leak" in p.stdout, p.stdout[-200:] + p.stderr[-100:])
    p = vault(store, "leaks")
    check("the register shows it settled, not open", "OPEN" not in p.stdout and "0 still open" in p.stdout, p.stdout[-200:])
    p = vault(store, "settle", "example-app", "prod", "DATABASE_URL", "--how", "the same evidence again, long enough", *settlement_evidence())
    check("settling twice says there is nothing open", p.returncode != 0 and "no open leak" in (p.stdout + p.stderr))


def test_every_movement_lands_in_the_journal_and_hand_moves_are_recordable() -> None:
    """The movements journal: the tools write it; `moved` is for
    what was done outside them; nothing in it is ever a value."""
    store = fresh()
    vault(store, "put", "alpha", "prod", "API_TOKEN", stdin="value-one")
    vault(store, "rotate", "alpha", "prod", "API_TOKEN", stdin="value-two")
    vault(store, "leak", "alpha", "prod", "API_TOKEN", "--where", "a log line in CI")
    vault(store, "settle", "alpha", "prod", "API_TOKEN", "--how", "synthetic replacement with retained archive", *settlement_evidence())
    p = vault(store, "moved", "beta", "prod", "DB_PASSWORD", "--how", "x")
    check("a hand move with no evidence is refused", p.returncode != 0 and "evidence" in (p.stdout + p.stderr))
    p = vault(store, "moved", "beta", "prod", "DB_PASSWORD", "--at", "digitalocean",
              "--how", "rotated in the DO app dashboard, deployment 2026-09-14T01:00Z")
    check("a hand move with evidence is recorded with its provider",
          p.returncode == 0 and "digitalocean" in p.stdout, p.stdout[-160:])
    rows = [json.loads(l) for l in (store / "movements.jsonl").read_text().splitlines() if l.strip()]
    check("put, rotate, leak, settle and moved are all in the journal",
          [r["event"] for r in rows] == ["put", "rotate", "leak", "settle", "moved"], str([r["event"] for r in rows]))
    check("and no value is", "value-one" not in (store / "movements.jsonl").read_text()
          and "value-two" not in (store / "movements.jsonl").read_text())
    p = vault(store, "movements", "beta")
    check("movements lists by project, names and places only",
          "beta/prod/DB_PASSWORD" in p.stdout and "1 movement(s)" in p.stdout, p.stdout[-200:])


def test_the_board_carries_an_open_leak_and_drops_a_settled_one() -> None:
    """The rule in build_findings, driven through a planted register."""
    import importlib.util
    s = fresh()
    vault(s, "put", "demo", "prod", "API_TOKEN", stdin=BOARD_V1)
    vault(s, "leak", "demo", "prod", "API_TOKEN", "--where", "planted for the board test")
    os.environ["OBSERVATORY_VAULT_DIR"] = str(s)
    try:
        spec = importlib.util.spec_from_file_location("bf_v", ROOT / "tools/build_findings.py")
        bf = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bf)
        got = [f for f in bf.collect() if f["type"] == "secret.leaked_unrotated"]
        check("an open leak raises secret.leaked_unrotated at critical",
              len(got) == 1 and got[0]["severity"] == "critical", str(got)[:200])
        check("the finding names the slot and the place, never the value",
              got and "demo/prod/API_TOKEN" in got[0]["title"]
              and "planted for the board test" in got[0]["detail"]
              and BOARD_V1 not in json.dumps(got), str(got)[:200])
        vault(s, "rotate", "demo", "prod", "API_TOKEN", stdin=BOARD_V2)
        spec = importlib.util.spec_from_file_location("bf_v2", ROOT / "tools/build_findings.py")
        bf2 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bf2)
        got = [f for f in bf2.collect() if f["type"] == "secret.leaked_unrotated"]
        check("the board keeps a locally replaced leak open", len(got) == 1, str(got)[:150])
        if got:
            check("the board requires revocation and consumer evidence",
                  "--revocation-evidence" in got[0]["action"]
                  and "--consumer-evidence" in got[0]["action"]
                  and "either settles" not in got[0]["action"], got[0]["action"])
        settled = vault(s, "settle", "demo", "prod", "API_TOKEN",
                        "--how", "synthetic provider and consumer checks", *settlement_evidence())
        check("explicit settlement succeeds", settled.returncode == 0, settled.stderr)
        got = [f for f in bf2.collect() if f["type"] == "secret.leaked_unrotated"]
        check("a settled leak raises nothing", not got, str(got)[:150])
    finally:
        os.environ.pop("OBSERVATORY_VAULT_DIR", None)


def test_two_sightings_of_one_slot_are_one_finding_naming_both() -> None:
    """Two open leak rows of ONE slot raised two findings with one id, and the
    duplicate-id guard then stopped the whole board build (`full local` failed
    at `findings`; the dashboard, smoke and settle steps never ran). A slot is
    one thing to settle, so it is one finding that lists every sighting."""
    import importlib.util
    s = fresh()
    vault(s, "put", "demo", "local", "API_TOKEN", stdin=BOARD_V1)
    vault(s, "leak", "demo", "local", "API_TOKEN", "--where", "first sighting: a CI log, job 101")
    vault(s, "leak", "demo", "local", "API_TOKEN", "--where", "second sighting: a pasted terminal")
    vault(s, "leak", "demo", "prod", "OTHER_TOKEN", "--where", "a different slot entirely")
    prior = os.environ.get("OBSERVATORY_VAULT_DIR")
    os.environ["OBSERVATORY_VAULT_DIR"] = str(s)
    try:
        spec = importlib.util.spec_from_file_location("bf_two", ROOT / "tools/build_findings.py")
        bf = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bf)
        got = [f for f in bf.collect() if f["type"] == "secret.leaked_unrotated"]
        subjects = sorted(f["subject"] for f in got)
        check("two sightings of one slot raise one finding, another slot its own",
              subjects == ["secret:demo/local/API_TOKEN", "secret:demo/prod/OTHER_TOKEN"],
              str(subjects))
        one = next((f for f in got if f["subject"] == "secret:demo/local/API_TOKEN"), {})
        check("the one finding names both places and how many sightings",
              "first sighting: a CI log" in one.get("detail", "")
              and "second sighting: a pasted terminal" in one.get("detail", "")
              and "2 sightings" in one.get("detail", ""), one.get("detail", "")[:300])
        check("and it is still critical and carries no value",
              one.get("severity") == "critical" and BOARD_V1 not in json.dumps(got))
        settled = vault(s, "settle", "demo", "local", "API_TOKEN", "--how",
                        "synthetic provider and consumer checks", *settlement_evidence())
        check("one settlement closes every sighting of the slot",
              settled.returncode == 0 and "settled 2 open leak" in settled.stdout, settled.stdout)
        got = [f for f in bf.collect() if f["type"] == "secret.leaked_unrotated"]
        check("and the board then keeps only the other slot",
              [f["subject"] for f in got] == ["secret:demo/prod/OTHER_TOKEN"],
              str([f["subject"] for f in got]))
    finally:
        if prior is None:
            os.environ.pop("OBSERVATORY_VAULT_DIR", None)
        else:
            os.environ["OBSERVATORY_VAULT_DIR"] = prior


def test_an_unreadable_register_is_reported_not_read_as_empty() -> None:
    """`secret.register_unreadable` — empty and unreadable are different facts.

    An unreadable register reads as empty, and empty is indistinguishable from
    nothing ever having leaked — the inversion this repository refuses
    everywhere. Planted: a register that is not JSON.
    """
    import importlib.util
    s = fresh()
    s.mkdir(parents=True, exist_ok=True)
    (s / "leaks.jsonl").write_text("{not json at all\n", encoding="utf-8")
    os.environ["OBSERVATORY_VAULT_DIR"] = str(s)
    try:
        spec = importlib.util.spec_from_file_location("bf_v3", ROOT / "tools/build_findings.py")
        bf = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bf)
        got = [f for f in bf.collect() if f["type"] == "secret.register_unreadable"]
        check("a register that does not parse raises its own row", len(got) == 1,
              str([f["type"] for f in bf.collect() if f["type"].startswith("secret")]))
        check("and no leak row is invented from it",
              not [f for f in bf.collect() if f["type"] == "secret.leaked_unrotated"],
              "an unreadable file must not produce findings about its contents")
    finally:
        os.environ.pop("OBSERVATORY_VAULT_DIR", None)


def test_an_old_retired_archive_is_reported() -> None:
    """`secret.retired_unrevoked` — a live credential in a file named 'dead'.

    `rotate` says out loud that the retired value works until revoked at the
    provider. An archive a month later means that sentence was not acted on.
    """
    import subprocess as _sp
    s = fresh()
    vault(s, "put", "demo", "prod", "API_TOKEN", stdin="v1")
    old_dir = s / "demo/prod"
    (old_dir / "API_TOKEN.retired-2026-01-01T000000Z").write_text("dead-value",
                                                                  encoding="utf-8")
    code = ("import importlib.util, json, pathlib, sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            f"spec = importlib.util.spec_from_file_location('bf', {str(ROOT / 'tools/build_findings.py')!r})\n"
            "bf = importlib.util.module_from_spec(spec); spec.loader.exec_module(bf)\n"
            "print(json.dumps([f for f in bf.collect()"
            " if f['type'] == 'secret.retired_unrevoked']))\n")
    p = _sp.run([PY, "-c", code], cwd=ROOT,
                env={**os.environ, "OBSERVATORY_VAULT_DIR": str(s)},
                capture_output=True, text=True, timeout=900)
    got = json.loads(p.stdout or "[]")
    check("a month-old retired archive raises secret.retired_unrevoked",
          len(got) == 1, (p.stdout + p.stderr)[-200:])
    check("the row names the archive and never its value",
          got and "API_TOKEN.retired-2026-01-01" in got[0]["detail"]
          and "dead-value" not in json.dumps(got), str(got)[:200])


def test_manual_settlement_requires_two_independent_attestations() -> None:
    store = fresh()
    vault(store, "leak", "demo", "prod", "API_TOKEN", "--where", "synthetic CI log exposure")
    original = (store / "leaks.jsonl").read_bytes()
    moves = (store / "movements.jsonl").read_bytes()
    attempts = [(), ("--revocation-evidence", "provider revoked old version"),
                ("--consumer-evidence", "both fixture consumers checked"),
                ("--revocation-evidence", "   ", "--consumer-evidence", "both consumers checked"),
                ("--revocation-evidence", "provider revoked old version", "--consumer-evidence", "…")]
    for command in ("settle", "moved"):
        for flags in attempts:
            extra = ("--settle",) if command == "moved" else ()
            p = vault(store, command, "demo", "prod", "API_TOKEN", "--how",
                      "a local replacement alone proves no containment", *extra, *flags)
            check(f"{command} refuses incomplete settlement evidence {len(flags)}",
                  p.returncode != 0, p.stdout)
            check(f"{command} refusal leaves both journals unchanged",
                  original == (store / "leaks.jsonl").read_bytes()
                  and moves == (store / "movements.jsonl").read_bytes())
    p = vault(store, "settle", "demo", "prod", "API_TOKEN", "--how",
              "fake-provider and consumer receipt references", *settlement_evidence())
    check("two explicit attestations permit manual settlement", p.returncode == 0, p.stderr)
    rows = [json.loads(line) for line in (store / "leaks.jsonl").read_text().splitlines()]
    last = rows[-1]
    check("settlement distinguishes manual evidence from a provider probe",
          last.get("event") == "settled" and last.get("verification") == "manual_attestation")
    check("settlement keeps revocation and consumer receipts separate",
          last.get("revocation_evidence") == settlement_evidence()[1]
          and last.get("consumer_evidence") == settlement_evidence()[3])
    audit = json.loads((store / "movements.jsonl").read_text().splitlines()[-1])
    check("settlement audit contains both evidence references",
          audit.get("revocation_evidence") == last.get("revocation_evidence")
          and audit.get("consumer_evidence") == last.get("consumer_evidence")
          and audit.get("verification") == "manual_attestation")


def test_moved_settlement_and_legacy_readers_preserve_scope() -> None:
    import importlib.util
    store = fresh()
    for project in ("demo", "other"):
        vault(store, "leak", project, "prod", "API_TOKEN", "--where", "synthetic CI log exposure")
    # Two legacy leak rows in the older shape, settled by rotation and by hand.
    with (store / "leaks.jsonl").open("a") as f:
        f.write(json.dumps({"event": "leaked", "id": "old-1", "secret": "legacy/prod/KEY"}) + "\n")
        f.write(json.dumps({"event": "settled", "of": "old-1", "by": "rotation"}) + "\n")
        f.write(json.dumps({"event": "leaked", "id": "old-2", "secret": "manual/prod/KEY"}) + "\n")
        f.write(json.dumps({"event": "settled", "of": "old-2", "by": "someone",
                            "how": "rotated at the provider by hand, before 0.2.1"}) + "\n")
    before = (store / "leaks.jsonl").read_bytes()
    p = vault(store, "moved", "demo", "prod", "API_TOKEN", "--settle", "--at", "fake-provider",
              "--how", "synthetic provider replacement and checks", *settlement_evidence())
    check("moved can settle with both attestations", p.returncode == 0, p.stderr)
    body = (store / "leaks.jsonl").read_bytes()
    check("historical rows are preserved byte for byte", body.startswith(before))
    for filename, name in (("collectors/credentials_registry.py", "creds_pb001"),
                           ("tools/serverd.py", "server_pb001"), ("tools/vault.py", "vault_pb001")):
        prior = os.environ.get("OBSERVATORY_VAULT_DIR")
        os.environ["OBSERVATORY_VAULT_DIR"] = str(store)
        try:
            spec = importlib.util.spec_from_file_location(name, ROOT / filename)
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            if name == "creds_pb001":
                slots = set(module.leaks(store))
            elif name == "server_pb001":
                slots = {r["secret"] for r in module.refresh_leaks()["open_secrets"]}
            else:
                slots = {r["secret"] for r in module.open_leaks()}
            check(f"{name} reopens a leak that only a pre-0.2.1 local rotate closed",
                  slots == {"other/prod/API_TOKEN", "legacy/prod/KEY"}, str(slots))
        finally:
            if prior is None: os.environ.pop("OBSERVATORY_VAULT_DIR", None)
            else: os.environ["OBSERVATORY_VAULT_DIR"] = prior
    p = vault(store, "leaks")
    check("`leaks` names a leak closed only by a pre-0.2.1 local rotate",
          "closed only by a local rotate" in p.stdout and "manual/prod/KEY" in p.stdout, p.stdout[-400:])
    sys.path.insert(0, str(ROOT))
    import leak_register
    check("the shared rule: legacy rotation does not settle, manual and attested rows do",
          not leak_register.settles({"event": "settled", "of": "x", "by": "rotation"})
          and leak_register.settles({"event": "settled", "of": "x", "by": "rotation",
                                     "verification": "manual_attestation"})
          and leak_register.settles({"event": "settled", "of": "x", "how": "by hand"})
          and not leak_register.settles({"event": "settled", "by": "someone"}))
    moved = json.loads((store / "movements.jsonl").read_text().splitlines()[-1])
    check("moved journal records settlement evidence too",
          moved.get("verification") == "manual_attestation"
          and moved.get("revocation_evidence") == settlement_evidence()[1]
          and moved.get("consumer_evidence") == settlement_evidence()[3])


def test_the_mandatory_rules_ship_as_a_skill() -> None:
    """The rules bind agents only if they arrive in the session's skill set."""
    skill = ROOT / "skill/plugins/observatory-log/skills/handling-secrets/SKILL.md"
    check("the skill exists in the shipped plugin", skill.is_file(), str(skill))
    body = skill.read_text(encoding="utf-8")
    for phrase, why in (("stdin", "how a value travels"),
                        ("names", "the vocabulary after inject"),
                        ("--where", "a sighting is recorded with its place"),
                        ("rotate", "replacement and revocation"),
                        ("vault.py leaks", "the register that keeps exposures visible"),
                        ("Never ask the user to paste a credential", "credentials stay out of chat"),
                        ("not a sandbox", "the output filter's security boundary")):
        check(f"the skill states {why}", phrase.casefold() in body.casefold(),
              f"{phrase!r} is missing")
    check("provider operations require explicitly configured integration",
          "explicitly configured integration" in body
          and "cloudflare.py" in body and "openrouter.py" in body)



def test_remove_takes_a_slot_or_its_archives_on_the_record() -> None:
    """There was no way to remove a slot or a rotation's archive except by hand
    in the store, outside the journal."""
    s = fresh()
    vault(s, "put", "demo", "local", "API_TOKEN", stdin="synthetic-remove-value-one")
    vault(s, "rotate", "demo", "local", "API_TOKEN", stdin="synthetic-remove-value-two")
    p = vault(s, "remove", "demo", "local", "API_TOKEN", "--retired")
    check("--retired removes the archives and keeps the slot", p.returncode == 0
          and not list((s / "demo/local").glob("API_TOKEN.retired-*")) and (s / "demo/local/API_TOKEN").is_file(),
          p.stdout + p.stderr)
    vault(s, "leak", "demo", "local", "API_TOKEN", "--where", "pasted into a fixture review thread #12")
    p = vault(s, "remove", "demo", "local", "API_TOKEN")
    check("a slot with an open leak is not removed without --force", p.returncode != 0
          and (s / "demo/local/API_TOKEN").is_file() and "--force" in p.stderr, p.stderr[:200])
    p = vault(s, "remove", "demo", "local", "API_TOKEN", "--force")
    check("--force removes the slot and its metadata", p.returncode == 0
          and not (s / "demo/local/API_TOKEN").exists() and not (s / "demo/local/API_TOKEN.meta.json").exists(),
          p.stdout + p.stderr)
    check("and never prints a value", "synthetic-remove-value" not in p.stdout + p.stderr)
    moves = [json.loads(x) for x in (s / "movements.jsonl").read_text(encoding="utf-8").splitlines()]
    check("both removals are journalled", [m["event"] for m in moves if m["event"].startswith("remove")]
          == ["remove-retired", "remove"], str([m["event"] for m in moves]))
    check("the journal holds no value", "synthetic-remove-value" not in (s / "movements.jsonl").read_text())
    p = vault(s, "remove", "demo", "local", "API_TOKEN")
    check("removing what is not there is a typed refusal", p.returncode != 0 and "nothing at" in p.stderr,
          p.stderr[:160])


def test_backup_without_a_gateway_names_the_setting() -> None:
    p = vault(fresh(), "backup")
    check("no backup script configured is said as such", p.returncode != 0
          and "sources.gateway_root" in p.stderr and "disabled" not in p.stderr, p.stderr[:200])


if __name__ == "__main__":
    print("the vault — one door in, one door out, leaks on the record\n")
    for fn in (test_remove_takes_a_slot_or_its_archives_on_the_record,
               test_backup_without_a_gateway_names_the_setting,
               test_a_value_travels_only_on_stdin,
               test_put_list_rotate_and_the_archive,
               test_the_vocabulary_is_enforced,
               test_a_credential_shaped_name_or_note_never_reaches_the_register,
               test_inject_refuses_a_committable_env_and_writes_an_ignored_one,
               test_a_leak_needs_a_place_and_stays_open_after_local_rotation,
               test_a_leak_of_a_key_the_store_never_held_is_still_recorded,
               test_the_board_carries_an_open_leak_and_drops_a_settled_one,
               test_two_sightings_of_one_slot_are_one_finding_naming_both,
               test_an_unreadable_register_is_reported_not_read_as_empty,
               test_an_old_retired_archive_is_reported,
               test_the_mandatory_rules_ship_as_a_skill,
               test_a_leak_rotated_at_the_provider_is_settled_by_hand_with_evidence,
               test_manual_settlement_requires_two_independent_attestations,
               test_moved_settlement_and_legacy_readers_preserve_scope,
               test_every_movement_lands_in_the_journal_and_hand_moves_are_recordable):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe vault holds, refuses, records and settles\033[0m")
