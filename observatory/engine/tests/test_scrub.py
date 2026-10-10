#!/usr/bin/env python3
"""The companion's memory, scrubbed of the estate's values — watched on a
planted store shaped like the session companion's.

Two FTS shapes are planted on purpose: an EXTERNAL-content table kept by
AFTER UPDATE triggers (claude-mem's `observations_fts`), which must follow its
parent and must NOT be rewritten directly, and a CONTENTFUL trigram table
(Chroma's `embedding_fulltext_search`), which holds its own copy and must be
rewritten. The journal is read back for the one property the tool exists for:
a value in neither the store nor the journal afterwards.

Remediation rewrites a store this system does not own, so the engine keeps it
behind the `companion_remediation` feature; each estate here is a private
workspace that enables it, and one case asserts that without it nothing is
written.
"""
from __future__ import annotations
import importlib.util
import json
import os
import pathlib
import sqlite3
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []
#: COMPOSED, and random-looking on purpose: `scan_leaks.searchable()` hunts only
#: values `scan_env.looks_secret()` calls distinctive — real randomness, not a
#: hyphenated sentence — so a prose-shaped fixture would be `skipped`, not
#: found, and the scrub would have nothing to do. No issuer prefix, so
#: `tools/check_secrets.py` does not read the test as a committed key either.
VALUE = "".join(("fx7Q9pL2", "mN8vB4cZ", "1kR6tY3w", "H5jD0sG7"))


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def planted_home() -> pathlib.Path:
    home = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-scrub-"))
    (home / "chroma").mkdir()
    c = sqlite3.connect(home / "claude-mem.db")
    c.executescript(f"""
        CREATE TABLE observations (id INTEGER PRIMARY KEY, project TEXT, facts TEXT, narrative TEXT);
        CREATE VIRTUAL TABLE observations_fts USING fts5(facts, narrative, content='observations', content_rowid='id');
        CREATE TRIGGER observations_ai AFTER INSERT ON observations BEGIN
          INSERT INTO observations_fts(rowid, facts, narrative) VALUES (new.id, new.facts, new.narrative); END;
        CREATE TRIGGER observations_au AFTER UPDATE ON observations BEGIN
          INSERT INTO observations_fts(observations_fts, rowid, facts, narrative) VALUES ('delete', old.id, old.facts, old.narrative);
          INSERT INTO observations_fts(rowid, facts, narrative) VALUES (new.id, new.facts, new.narrative); END;
        INSERT INTO observations (project, facts, narrative) VALUES ('demo', 'the call used key {VALUE} twice: {VALUE}', 'nothing here');
        INSERT INTO observations (project, facts, narrative) VALUES ('demo', 'clean row', 'clean');
        CREATE TABLE user_prompts (id INTEGER PRIMARY KEY, prompt_text TEXT);
        INSERT INTO user_prompts (prompt_text) VALUES ('please use {VALUE} for the test');
    """)
    c.commit(); c.close()
    ch = sqlite3.connect(home / "chroma" / "chroma.sqlite3")
    ch.executescript(f"""
        CREATE TABLE embedding_metadata (id INTEGER, key TEXT NOT NULL, string_value TEXT, int_value INTEGER);
        CREATE VIRTUAL TABLE embedding_fulltext_search USING fts5(string_value, tokenize='trigram');
        INSERT INTO embedding_metadata VALUES (1, 'chroma:document', 'summary mentions {VALUE} here', NULL);
        INSERT INTO embedding_fulltext_search(rowid, string_value) VALUES (1, 'summary mentions {VALUE} here');
    """)
    ch.commit(); ch.close()
    return home


def estate_with_the_value(remediation: bool = True) -> dict:
    """An env inventory the scanner's `known_values()` will read, holding VALUE,
    in a workspace of its own whose settings enable companion remediation."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-scrub-estate-"))
    data = d / "projects" / "demo"; data.mkdir(parents=True)
    (data / ".env").write_text(f"PORT=3000\nDEMO_API_KEY={VALUE}\n", encoding="utf-8")
    scratch = d / "scratch"; scratch.mkdir()
    import workspace
    workspace.initialize(d / "home", identities=False)
    settings = d / "home/config/settings.json"
    doc = json.loads(settings.read_text(encoding="utf-8"))
    doc["features"]["companion_remediation"] = remediation
    settings.write_text(json.dumps(doc), encoding="utf-8")
    env = {**os.environ, "OBSERVATORY_HOME": str(d / "home"),
           "OBSERVATORY_REGISTRY": str(d / "home/registry"),
           "OBSERVATORY_DB": str(d / "home/store/observatory.db"),
           "OBSERVATORY_DATA": str(d / "projects"), "OBSERVATORY_SCRATCH": str(scratch),
           "OBSERVATORY_STATE": str(d / "state"), "OBSERVATORY_VAULT_DIR": str(d / "novault")}
    subprocess.run([PY, str(ROOT / "tools/runtime_identity.py"),
                    "init", "env-fingerprint-salt"], env=env, capture_output=True,
                   text=True, check=True, timeout=15)
    subprocess.run([PY, str(ROOT / "collectors/scan_env.py"), str(scratch / "env.json")],
                   env=env, capture_output=True, text=True, check=True, timeout=120)
    return env


def count(db: pathlib.Path, sql: str) -> int:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return c.execute(sql).fetchone()[0]
    finally:
        c.close()


def test_a_dry_run_counts_and_writes_nothing() -> None:
    home = planted_home()
    env = {**estate_with_the_value(), "CLAUDE_MEM_HOME": str(home)}
    before = (home / "claude-mem.db").read_bytes()
    p = subprocess.run([PY, str(ROOT / "tools/scrub_companion.py"), "--dry-run"], env=env,
                       capture_output=True, text=True, timeout=300)
    check("the dry run names the cells it would rewrite, per table.column",
          p.returncode == 0 and "observations.facts" in p.stdout and "user_prompts.prompt_text" in p.stdout
          and "embedding_metadata.string_value" in p.stdout, p.stdout[-400:] + p.stderr[-200:])
    check("an external-content FTS table is not listed — its parent's rewrite is its rewrite",
          "observations_fts" not in p.stdout, p.stdout[-300:])
    check("and nothing was written", (home / "claude-mem.db").read_bytes() == before)
    check("the dry run prints no value", VALUE not in p.stdout + p.stderr, "IT LEAKED")


def test_the_scrub_replaces_every_copy_and_keeps_no_value_anywhere() -> None:
    home = planted_home()
    env = {**estate_with_the_value(), "CLAUDE_MEM_HOME": str(home)}
    p = subprocess.run([PY, str(ROOT / "tools/scrub_companion.py")], env=env,
                       capture_output=True, text=True, timeout=300)
    check("the scrub ran", p.returncode == 0, p.stderr[-300:])
    db, ch = home / "claude-mem.db", home / "chroma" / "chroma.sqlite3"
    check("the parent table holds no value", count(db, f"SELECT count(*) FROM observations WHERE instr(facts, '{VALUE}')>0") == 0)
    check("and reads REDACTED with the variable's name",
          count(db, "SELECT count(*) FROM observations WHERE instr(facts, '[REDACTED:env:demo/DEMO_API_KEY]')>0") == 1)
    check("both occurrences in one cell were replaced",
          count(db, "SELECT count(*) FROM observations WHERE facts LIKE '%[REDACTED:env:demo/DEMO_API_KEY] twice: [REDACTED:env:demo/DEMO_API_KEY]%'") == 1)
    check("the external-content FTS followed its parent through the trigger",
          count(db, f"SELECT count(*) FROM observations_fts WHERE observations_fts MATCH '{VALUE}'") == 0
          and count(db, "SELECT count(*) FROM observations_fts WHERE observations_fts MATCH 'REDACTED'") == 1)
    check("the operator's own prompt is scrubbed too", count(db, f"SELECT count(*) FROM user_prompts WHERE instr(prompt_text, '{VALUE}')>0") == 0)
    check("the clean row is untouched", count(db, "SELECT count(*) FROM observations WHERE facts='clean row'") == 1)
    check("Chroma's metadata is rewritten", count(ch, f"SELECT count(*) FROM embedding_metadata WHERE instr(string_value, '{VALUE}')>0") == 0)
    check("and its contentful trigram index directly, having no trigger",
          count(ch, f"SELECT count(*) FROM embedding_fulltext_search WHERE instr(string_value, '{VALUE}')>0") == 0
          and count(ch, "SELECT count(*) FROM embedding_fulltext_search WHERE instr(string_value, 'REDACTED')>0") == 1)
    backups = list((home / "backups").glob("claude-mem-pre-scrub-*.db"))
    check("a backup of claude-mem.db was taken first, at mode 600",
          len(backups) == 1 and oct(backups[0].stat().st_mode & 0o777) == "0o600", str(backups))
    check("and the backup still holds the value — that is what a backup is for",
          count(backups[0], f"SELECT count(*) FROM observations WHERE instr(facts, '{VALUE}')>0") == 1)
    journal = pathlib.Path(env["OBSERVATORY_STATE"]) / "logs" / "scrub.jsonl"
    rows = [json.loads(l) for l in journal.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("the journal names table.column, variable and cell counts",
          any(r.get("cells") for r in rows) and any(x["where"] == "observations.facts" for r in rows for x in r.get("rows", [])), str(rows)[:300])
    check("and holds no value", VALUE not in journal.read_text(encoding="utf-8") and VALUE not in p.stdout + p.stderr, "IT LEAKED")
    p2 = subprocess.run([PY, str(ROOT / "tools/scrub_companion.py")], env=env, capture_output=True, text=True, timeout=300)
    check("a second run finds the stores clean and takes no second backup",
          "clean" in p2.stdout and len(list((home / "backups").glob("claude-mem-pre-scrub-*.db"))) == 1, p2.stdout[-200:])


def test_remediation_is_off_until_a_workspace_turns_it_on() -> None:
    """The engine's own gate: rewriting another program's store is opt-in."""
    home = planted_home()
    env = {**estate_with_the_value(remediation=False), "CLAUDE_MEM_HOME": str(home)}
    before = (home / "claude-mem.db").read_bytes()
    p = subprocess.run([PY, str(ROOT / "tools/scrub_companion.py")], env=env,
                       capture_output=True, text=True, timeout=300)
    check("without the feature the scrub declines and says why",
          p.returncode == 0 and "remediation is disabled" in p.stdout, p.stdout[-200:])
    check("and writes nothing", (home / "claude-mem.db").read_bytes() == before)
    check("and takes no backup", not (home / "backups").exists()
          or not list((home / "backups").glob("*.db")))


def test_the_tick_runs_it_and_the_rule_is_written_where_agents_read() -> None:
    tick = (ROOT / "tools/tick.py").read_text(encoding="utf-8")
    check("the tick scrubs the companion after the leak scan",
          "scrub_companion.py" in tick and tick.index("scrub_companion.py") > tick.index("scan_leaks.py"), "")
    obs = (ROOT / "observatory.py").read_text(encoding="utf-8")
    check("`scrub-companion` is a step and rides in `all` after `leaks`",
          '"scrub-companion"' in obs and obs.index('"scrub-companion"', obs.index('"all":')) > obs.index('"leaks"', obs.index('"all":')), "")


if __name__ == "__main__":
    print("the companion's memory — the estate's values taken out, by name, with a backup first\n")
    for fn in (test_a_dry_run_counts_and_writes_nothing,
               test_the_scrub_replaces_every_copy_and_keeps_no_value_anywhere,
               test_remediation_is_off_until_a_workspace_turns_it_on,
               test_the_tick_runs_it_and_the_rule_is_written_where_agents_read):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma value in the companion's memory lasts one tick\033[0m")
