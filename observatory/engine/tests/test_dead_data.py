#!/usr/bin/env python3
"""Data written and never read, computed and never consumed.

The audit found four instances of one shape, and the shape matters more than the
four: something is produced faithfully, every run, and nothing at the other end
takes delivery. It is invisible precisely because it works — the writer succeeds,
the row lands, the test of the writer is green.

* **`proposals`** — zero rows, one writer (`mcp/server.py` via `L.proposals_add`),
  and no reader anywhere. `observatory_propose` is DECLARED surface: it is in the
  manifest and a probe exercises it. A caller was told its patch landed, into a
  table nobody looked at.
* **`FIELD_MEANING`** — a table of what each field's change means, its own
  docstring saying "in words the agent will read", and only its KEYS were ever
  used. The agent received `commits-changed: 885 -> 886` and never the sentence.
* **`name` in the fingerprint** — recorded on every scan and UNDIFFABLE, because
  the differ iterated the meaning table rather than the data. A renamed project
  produced zero deltas while the command printed "nothing moved". Verified by
  driving a rename through `cmd_diff` on a fixture: 0 rows.
* **`datapaths_anywhere` in `scan_vault.py`** — computed every run, consumed by
  nothing, and kept alive only by a source-grep in a trap test. Deleted.

The last test is the one that matters after today: a structural rule that fails
when a table gains a writer and no reader, so the class cannot return quietly.
"""
from __future__ import annotations
import json, os, pathlib, re, sqlite3, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402
sys.path.insert(0, str(ROOT / "collectors"))

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def fixture_db() -> tuple[pathlib.Path, sqlite3.Connection]:
    """A real schema, because a narrower one tests a store that cannot exist."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-dead-"))
    os.environ["OBSERVATORY_DB"] = str(d / "observatory.db")
    import importlib
    import paths
    importlib.reload(paths)
    from store import db as sdb
    importlib.reload(sdb)
    return d, sdb.connect()


def fingerprint(**over) -> dict:
    base = {"name": "Alpha", "repos": ["a"], "last_activity_on": "2026-09-01",
            "lifecycle": "active", "ownership": "owned", "sites": [],
            "stack": ["py"], "vault_notes": 1, "dirty": 0, "commits": 10}
    base.update(over)
    return {"project:alpha": base}


def put_fingerprints(conn, first: dict, second: dict) -> None:
    import compute_deltas as cd
    for i, payload in enumerate((first, second)):
        at = f"2026-09-0{i + 1}T00:00:00Z"
        conn.execute("INSERT INTO scans (id, started_at, finished_at, collector_version,"
                     " counts_json) VALUES (?,?,?,?,?)", (f"s{i}", at, at, "fixture", "{}"))
        conn.execute("INSERT INTO observations (id, scan_id, subject_id, kind, payload_json,"
                     " observed_at) VALUES (?,?,?,?,?,?)",
                     (f"obs{i}", f"s{i}", "estate", cd.KIND,
                      json.dumps(payload, sort_keys=True), at))
    conn.commit()


def test_every_field_the_fingerprint_records_is_diffable() -> None:
    """The differ must iterate the DATA, not the meaning table beside it."""
    import compute_deltas as cd
    recorded = set(fingerprint()["project:alpha"])
    undiffable = sorted(recorded - set(cd.FIELD_MEANING))
    check("no field is recorded without a meaning", not undiffable,
          f"{undiffable} would be silently undiffable if the loop iterated the table")
    src = (ROOT / "collectors/compute_deltas.py").read_text(encoding="utf-8")
    check("and the loop iterates the keys present, not the table",
          "set(before) | set(after)" in src,
          "iterating FIELD_MEANING makes the next field added invisible")


def test_a_rename_produces_a_delta() -> None:
    """The planted defect this pass was found by: 0 rows, and a reassurance."""
    d, conn = fixture_db()
    import compute_deltas as cd
    put_fingerprints(conn, fingerprint(), fingerprint(name="Beta"))
    cd.cmd_diff(conn)
    rows = [dict(r) for r in conn.execute("SELECT kind, before_json, after_json FROM deltas")]
    check("renaming a project writes a delta", len(rows) == 1, f"{len(rows)} rows")
    if rows:
        check("of the right kind", rows[0]["kind"] == "name-changed", rows[0]["kind"])
        check("carrying both sides", '"Alpha"' in rows[0]["before_json"]
              and '"Beta"' in rows[0]["after_json"], str(rows[0]))
    conn.close()


def test_the_agent_receives_the_meaning_not_only_the_numbers() -> None:
    import compute_deltas as cd
    check("meaning_of answers for a field kind",
          cd.meaning_of("commits-changed") == "commits were recorded",
          cd.meaning_of("commits-changed"))
    check("and for the two lifecycle kinds, which arrived as a bare word",
          bool(cd.meaning_of("project-appeared")) and bool(cd.meaning_of("project-disappeared")),
          f"{cd.meaning_of('project-appeared')!r}")

    sys.path.insert(0, str(ROOT / "agent"))
    import observe
    prompt = observe.build_prompt(
        "project:alpha",
        [{"kind": "commits-changed", "before_json": "885", "after_json": "886"}],
        {"lifecycle": "active"}, [])
    check("and the prompt the agent actually reads carries it",
          "commits were recorded" in prompt,
          "the sentence was written for a reader that did not receive it")
    check("beside the numbers, not instead of them",
          "885" in prompt and "886" in prompt, prompt[-200:])


def test_a_registry_proposal_reaches_the_operator() -> None:
    """`observatory_propose` writes here; nothing read it. Driven end to end."""
    d, conn = fixture_db()
    conn.execute(
        "INSERT INTO proposals (id, target_id, patch_json, evidence_json,"
        " status, created_at) VALUES (?,?,?,?,?,?)",
        ("prop:test1", "project:alpha", json.dumps({"lifecycle": "archived"}),
         json.dumps(["no commit in two years"]), "proposed", "2026-09-07T00:00:00Z"))
    conn.commit()
    conn.close()

    env = dict(os.environ, OBSERVATORY_DB=str(d / "observatory.db"))
    p = subprocess.run([PY, "tools/review.py", "digest"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=300)
    check("the digest names a pending registry proposal",
          "project:alpha" in p.stdout, p.stdout[-300:] + p.stderr[-200:])
    check("and says how many are waiting", "1 registry proposal" in p.stdout,
          p.stdout[-300:])

    sys.path.insert(0, str(ROOT / "dashboard"))
    import importlib
    import paths
    importlib.reload(paths)
    import build_dashboard
    importlib.reload(build_dashboard)
    store = build_dashboard.from_store()
    check("and the dashboard's health panel counts it",
          store["health"].get("registry_proposals") == 1,
          str(store["health"].get("registry_proposals")))


def test_agent_workflows_reach_the_operator() -> None:
    """Workflows and handoff offers are written by agents over MCP; the health
    panel is where the operator sees them. Driven end to end through the store."""
    d, conn = fixture_db()
    from store import workflow as W
    import memory_redact
    quiet = memory_redact.Redactor(known_loader=lambda: {})
    wf = W.checkpoint_write(conn, owner="agent:fixture", idempotency_key="dead-data-0001",
                            step_id="S1", status="done", body={"goal": "a fixture goal"},
                            project_id="project:alpha", redactor=quiet)
    W.handoff_create(conn, owner="service:fixture", idempotency_key="dead-data-0002",
                     workflow_id=wf["workflowId"], reason="limit", to={"provider": "fixture"},
                     lease_token=wf["leaseId"],
                     git_reader=lambda path: {"path": path},
                     related_reader=lambda c, **kw: ([], []), redactor=quiet)
    conn.close()
    sys.path.insert(0, str(ROOT / "dashboard"))
    import importlib
    import paths
    importlib.reload(paths)
    import build_dashboard
    importlib.reload(build_dashboard)
    health = build_dashboard.from_store()["health"]
    check("the health panel counts the open workflow", health.get("workflows_open") == 1,
          str(health.get("workflows_open")))
    check("and the handoff waiting for a session", health.get("handoffs_waiting") == 1,
          str(health.get("handoffs_waiting")))


def written_and_never_read(root: pathlib.Path) -> list[str]:
    """Tables with a writer and no reader outside the file that writes them.

    Deliberately not "no reader at all": a writer reading its own table back is
    how `proposals` looked defensible for weeks. The question is whether the data
    reaches a SECOND place — an operator's screen, a report, another module.
    Tests do not count, for the same reason: a fixture reading a row proves the
    fixture, not the product.
    """
    schema = (root / "store/schema.sql").read_text(encoding="utf-8")
    tables = sorted(set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", schema)))
    files = [f for f in root.rglob("*.py")
             if "/tests/" not in str(f) and ".venv" not in str(f) and "__pycache__" not in str(f)]
    bodies = {f: f.read_text(encoding="utf-8", errors="replace") for f in files}
    dead = []
    for t in tables:
        w = {f for f, b in bodies.items() if re.search(rf"INSERT (OR [A-Z]+ )?INTO {t}\b", b)}
        r = {f for f, b in bodies.items() if re.search(rf"\b(FROM|JOIN) {t}\b", b)}
        # A table seeded by schema.sql itself (a singleton row) has no Python
        # writer and is not dead; `vec_meta` is that case.
        if not w:
            continue
        if not (r - w):
            dead.append(t)
    return dead


def test_no_table_is_written_and_never_read() -> None:
    dead = written_and_never_read(ROOT)
    check("every written table reaches a second place", not dead,
          f"{dead} — written every run, read by nothing but the writer")


def test_the_rule_catches_a_planted_dead_table() -> None:
    """A green from a rule nobody has watched fail is not evidence."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-plant-"))
    (d / "store").mkdir(parents=True)
    (d / "store/schema.sql").write_text(
        "CREATE TABLE IF NOT EXISTS widgets (id TEXT PRIMARY KEY);\n", encoding="utf-8")
    (d / "writer.py").write_text(
        "conn.execute('INSERT INTO widgets (id) VALUES (?)', ('a',))\n", encoding="utf-8")
    check("a table written by one file and read by none is reported",
          written_and_never_read(d) == ["widgets"], str(written_and_never_read(d)))

    # Now give it a reader in a SECOND file; the rule must go quiet.
    (d / "report.py").write_text(
        "rows = conn.execute('SELECT id FROM widgets').fetchall()\n", encoding="utf-8")
    check("and goes quiet once a second place reads it",
          written_and_never_read(d) == [], str(written_and_never_read(d)))

    # A writer reading its OWN table back is not a reader.
    (d / "report.py").unlink()
    (d / "writer.py").write_text(
        "conn.execute('INSERT INTO widgets (id) VALUES (?)', ('a',))\n"
        "n = conn.execute('SELECT count(*) FROM widgets').fetchone()[0]\n", encoding="utf-8")
    check("a writer reading itself back does not count as delivery",
          written_and_never_read(d) == ["widgets"], str(written_and_never_read(d)))


def test_the_computed_and_unconsumed_field_is_gone() -> None:
    src = (ROOT / "collectors/scan_vault.py").read_text(encoding="utf-8")
    check("datapaths_anywhere is deleted, not merely unused",
          "datapaths_anywhere" not in src)
    check("and the collector is importable, so its rules can be driven",
          'if __name__ == "__main__":' in src and "def scan(" in src,
          "a module that walks the vault at import can only be grepped")


if __name__ == "__main__":
    print("data written and never read\n")
    for fn in (test_every_field_the_fingerprint_records_is_diffable,
               test_a_rename_produces_a_delta,
               test_the_agent_receives_the_meaning_not_only_the_numbers,
               test_a_registry_proposal_reaches_the_operator,
               test_agent_workflows_reach_the_operator,
               test_no_table_is_written_and_never_read,
               test_the_rule_catches_a_planted_dead_table,
               test_the_computed_and_unconsumed_field_is_gone):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mwhat is written is read, what is computed is consumed\033[0m")
