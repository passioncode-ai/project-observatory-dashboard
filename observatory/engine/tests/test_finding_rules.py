#!/usr/bin/env python3
"""Eleven board rules nobody had ever seen fire, each driven into existence.

`registry/findings.json` is the operator's queue: every rule in
`tools/build_findings.py` is a promise that when a condition holds, a row
appears. When every declared type was measured against the board and the
suites, **eleven were on neither**:

    dashboard.blank            provider.health_unmeasured
    fixtures.leaked            provider.health_unreadable
    host.disk_unknown          provider.model_quarantined
    interpretation.not_english store.integrity
    plugin.waiting             store.integrity_stale
    wiki.links_unchecked

They are not a random eleven. Every one fires in a state that does not occur
while the system is WELL — a receipt that would not parse, a volume that cannot
be measured, an integrity check that has not run, a plugin whose requirement is
unmet. That is exactly the class that rots: a degradation nobody has watched
work is a degradation that does not work, and here the failure mode
is worse than a silent test, because the operator is told nothing at the moment
the system stops being able to tell them anything.

Each case plants the condition and asserts the row. Two techniques, chosen by
what the rule actually reads:

* **the sandbox** — a copy of the synthetic workspace's scratch and registry,
  one receipt replaced,
  the builder run as a subprocess through `OBSERVATORY_SCRATCH` /
  `OBSERVATORY_REGISTRY` / `OBSERVATORY_DB`. Nothing live is read for writing
  and nothing live is written;
* **in process** — for a rule whose trigger is an EXCEPTION rather than a file
  (`host.disk_unknown` fires when `shutil.disk_usage` raises), because no
  receipt can express "the measurement itself failed".
"""
from __future__ import annotations
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import paths                                                       # noqa: E402
import tmp as tmpdir                                               # noqa: E402
from test_portable_mcp import PROJECT_ID as SYNTHETIC_PROJECT       # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []
OLD = "2020-01-01T00:00:00Z"      # older than every horizon this file touches


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def sandbox(registry: dict | None = None, state: dict | None = None,
            sql: list[str] | None = None, **receipts: dict) -> list[dict]:
    """Build the board with these inputs replaced, and return its rows.

    The rest of `store/raw` is copied rather than invented: a rule that reads
    two receipts must see a real one beside the planted one, and a builder run
    against an empty scratch would raise its OWN degradations and drown the row
    under test.

    Four kinds of input, because the rules read four:
    `**receipts` replace a file in the scratch, `registry` patches a registry
    document in place, `state` replaces a file under `store/` (the wallet and
    the key shapes live there), and `sql` runs against a COPY of the store.
    """
    work = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-board-"))
    shutil.copytree(paths.SCRATCH, work / "raw")
    shutil.copytree(paths.REGISTRY, work / "registry")
    for name, doc in receipts.items():
        (work / "raw" / f"{name}.json").write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    for name, patch in (registry or {}).items():
        f = work / "registry" / name
        doc = json.loads(f.read_text(encoding="utf-8"))
        patch(doc)
        f.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    env = {**os.environ,
           "OBSERVATORY_SCRATCH": str(work / "raw"),
           "OBSERVATORY_REGISTRY": str(work / "registry"),
           # No acknowledgements: an ack would not remove the row, but it would
           # change what the assertion is reading, and a fixture must not
           # depend on the operator's own file.
           "OBSERVATORY_ACKS": str(work / "no-acks.json")}
    if state is not None:
        (work / "state").mkdir()
        for f in paths.STORE.glob("*.json"):
            shutil.copy2(f, work / "state" / f.name)
        for name, doc in state.items():
            (work / "state" / f"{name}.json").write_text(
                json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        env["OBSERVATORY_STATE"] = str(work / "state")
    if sql is not None:
        db = work / "t.db"
        if not paths.DB.is_file():
            # A FRESH WORKSPACE MAY HAVE NO STORE, and copying one that is not
            # there raised `FileNotFoundError` from the fixture rather than from
            # the rule. `store/schema.sql` ships with the engine, so an empty
            # store can be built and the planted SQL below is what the case is
            # actually about.
            import sqlite3 as _sq
            conn = _sq.connect(db)
            conn.executescript((ROOT / "store/schema.sql").read_text(encoding="utf-8"))
            conn.commit()
            conn.close()
        else:
            shutil.copy2(paths.DB, db)
        conn = __import__("sqlite3").connect(db)
        for stmt in sql:
            conn.execute(stmt)
        conn.commit()
        conn.close()
        env["OBSERVATORY_DB"] = str(db)
    p = subprocess.run([PY, "tools/build_findings.py"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=900)
    out = work / "registry/findings.json"
    if not out.is_file():
        check("the builder produced a board", False, (p.stdout + p.stderr)[-300:])
        return []
    return json.loads(out.read_text(encoding="utf-8"))["findings"]


def rows(board: list[dict], kind: str) -> list[dict]:
    return [f for f in board if f["type"] == kind]


def builder():
    """The module itself, for the rules whose trigger is not a file."""
    spec = importlib.util.spec_from_file_location("bf_rules", ROOT / "tools/build_findings.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ───────────────────────── the pure functions ────────────────────────────────

def test_a_blank_page_is_reported() -> None:
    """`dashboard.blank` — the page renders, and shows nothing."""
    bf = builder()
    got = bf.blank_page_findings({"verdict": "blank", "ran_at": OLD, "detail": "0 tiles"},
                                 "sha256:planted")
    check("a blank verdict raises dashboard.blank", bool(rows(got, "dashboard.blank")),
          str([f["type"] for f in got]))
    ok = bf.blank_page_findings({"verdict": "ok", "ran_at": OLD}, "sha256:planted")
    check("and a page that renders raises nothing of the kind",
          not rows(ok, "dashboard.blank"), str([f["type"] for f in ok]))


def test_an_unreadable_health_file_is_not_read_as_healthy() -> None:
    """`provider.health_unreadable` — None is not `{}`, and this is the row that says so."""
    bf = builder()
    got = bf.provider_findings(None, {"ran_at": OLD})
    check("health=None raises provider.health_unreadable",
          bool(rows(got, "provider.health_unreadable")), str([f["type"] for f in got]))
    check("and it is a warning, because nothing about health is known",
          all(f["severity"] == "warning" for f in rows(got, "provider.health_unreadable")))
    # `{}` is a DIFFERENT state and must not raise the same row.
    empty = bf.provider_findings({}, {"ran_at": OLD})
    check("an empty quarantine list does not raise it",
          not rows(empty, "provider.health_unreadable"), str([f["type"] for f in empty]))


def test_a_quarantined_model_is_named() -> None:
    """`provider.model_quarantined` — the chain fell to a later model, silently."""
    bf = builder()
    got = bf.provider_findings(
        {"vendor/model-a": {"since": OLD, "reason": "HTTP 502 from the provider"}},
        {"ran_at": OLD})
    q = rows(got, "provider.model_quarantined")
    check("a quarantined model raises provider.model_quarantined", bool(q),
          str([f["type"] for f in got]))
    check("the row names the model as its subject",
          any(f["subject"] == "model:vendor/model-a" for f in q), str(q[:1]))
    check("and repeats the provider's own reason",
          any("502" in f["title"] for f in q), str([f["title"] for f in q]))


def test_an_empty_quarantine_list_after_silence_says_so() -> None:
    """`provider.health_unmeasured` — empty means 'nothing called', not 'all healthy'."""
    bf = builder()
    got = bf.provider_findings({}, {"ran_at": OLD})
    check("an old run with an empty list raises provider.health_unmeasured",
          bool(rows(got, "provider.health_unmeasured")), str([f["type"] for f in got]))
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = bf.provider_findings({}, {"ran_at": now})
    check("and a RECENT run with an empty list does not",
          not rows(fresh, "provider.health_unmeasured"),
          "empty-and-recent is the one case where empty really does mean healthy")


def test_an_unmeasurable_volume_is_reported() -> None:
    """`host.disk_unknown` — no receipt can express 'the measurement itself failed'.

    So the failure is injected where it happens. This is the only rule of the
    eleven whose trigger is an exception, and the technique is named in the
    module docstring rather than left as a surprise here.
    """
    bf = builder()
    real = bf.shutil.disk_usage
    bf.shutil.disk_usage = lambda p: (_ for _ in ()).throw(OSError("planted: no such device"))
    try:
        got = bf.collect()
    finally:
        bf.shutil.disk_usage = real
    u = rows(got, "host.disk_unknown")
    check("a failing disk_usage raises host.disk_unknown", bool(u),
          "the volume this system measures everything on became unmeasurable")
    check("and the row carries the exception rather than a guess",
          any("no such device" in f["detail"] for f in u), str([f["detail"] for f in u][:1]))
    check("while the low-space rule stays silent, having measured nothing",
          not rows(got, "host.disk_low"),
          "a missing measurement must not be reported as a number")


# ───────────────────────── the planted receipts ──────────────────────────────

def test_leaked_fixtures_are_charged_to_this_project() -> None:
    """`fixtures.leaked` — space this project spent, so `host.disk_low` is not blamed."""
    board = sandbox(fixtures={"ran_at": OLD, "root": "/tmp/planted", "dry_run": False,
                              "stale": 12, "removed": 12, "freed_bytes": 3 * 1024 ** 3,
                              "stale_bytes": 9 * 1024 ** 3, "fresh_kept": 0,
                              "refused": [{"path": "/tmp/planted/x", "reason": "in use"}]})
    r = rows(board, "fixtures.leaked")
    check("a sweep receipt with leaked bytes raises fixtures.leaked", bool(r),
          "the row that stops host.disk_low carrying the blame")
    check("the size is the STALE bytes, not stale plus freed",
          any("9.00 GiB" in f["title"] or "9.00 GiB" in f["detail"] for f in r),
          str([f["title"] for f in r]))
    check("a refusal makes it a warning, because it recurs every run",
          all(f["severity"] == "warning" for f in r), str([f["severity"] for f in r]))


def test_an_unchecked_wiki_says_so() -> None:
    """`wiki.links_unchecked` — the audit has not run, which is not 'no broken links'."""
    board = sandbox(**{"vault-links": {"ran_at": OLD, "outcome": "checked", "broken": [],
                                       "ambiguous": [], "notes": 0, "links": 0,
                                       "detail": "", "targets": []}})
    r = rows(board, "wiki.links_unchecked")
    check("an old audit raises wiki.links_unchecked", bool(r),
          "24h is the horizon; this receipt is from 2020")
    # The stamp is in the TITLE — an absolute date, deliberately, because
    # `findings.json` is committed every tick and "37h ago" would produce a
    # commit every thirty minutes. Asserted where it is, not where I assumed.
    check("and the row carries the stamp rather than a relative age",
          any(OLD[:10] in f["title"] for f in r), str([f["title"] for f in r][:1]))


def receipt(name: str, fallback: dict) -> dict:
    """The live receipt where there is one, a minimal one where there is not.

    A fresh workspace has an empty scratch, so reading an existing receipt to
    build a fixture made three cases fail with `FileNotFoundError`, which is an
    ABSENCE read as a FAILURE. The rule under test does not care whose receipt
    it is; it cares about one field.
    """
    f = paths.SCRATCH / f"{name}.json"
    if f.is_file():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return fallback


def test_a_conclusion_in_another_language_is_reported() -> None:
    """`interpretation.not_english` — the prompt says English; this is the check on the output."""
    agent = receipt("agent", {"ran_at": OLD, "recorded": 0, "skipped": 0, "failed": 0,
                              "malformed": 0, "unreasoned": 0, "faults": [],
                              "chain_retired": [], "halted_by": None, "unconsumed": 0})
    board = sandbox(agent={**agent, "not_english": 3})
    r = rows(board, "interpretation.not_english")
    check("a non-English count raises interpretation.not_english", bool(r),
          str([f["type"] for f in board if f["type"].startswith("interpretation")]))
    check("and the row states how many", any("3" in f["title"] for f in r),
          str([f["title"] for f in r]))


def test_a_plugin_that_never_ran_is_reported() -> None:
    """`plugin.waiting` — an unmet requirement is a state, and states are still reported."""
    plugins = receipt("plugins", {"ran_at": OLD, "plugins": [], "installed": 0,
                                  "metric_rows": 0, "distinct_metrics": 0})
    planted = {**plugins, "plugins": [{"id": "planted-plugin", "classification": "waiting",
                                       "skipped": "EXAMPLE_TOKEN is not set",
                                       "written": 0, "refused": [], "last_at": None,
                                       "every_hours": 24, "series": []}]}
    board = sandbox(plugins=planted)
    r = rows(board, "plugin.waiting")
    check("a waiting plugin raises plugin.waiting", bool(r),
          str([f["type"] for f in board if f["type"].startswith("plugin")]))
    check("the row names the plugin as its subject",
          any(f["subject"] == "plugin:planted-plugin" for f in r), str(r[:1]))
    check("and repeats the unmet requirement verbatim",
          any("EXAMPLE_TOKEN" in f["title"] for f in r), str([f["title"] for f in r]))


def test_a_damaged_store_is_reported() -> None:
    """`store.integrity` — the one row that means the data itself is not trustworthy."""
    board = sandbox(integrity={"ran_at": OLD, "pragma": "integrity_check",
                               "verdict": "damaged", "took_ms": 12, "bytes": 1,
                               "detail": "planted: *** in database main ***"})
    r = rows(board, "store.integrity")
    check("a damaged verdict raises store.integrity", bool(r),
          str([f["type"] for f in board if f["type"].startswith("store")]))
    check("at critical severity, because everything else reads this store",
          all(f["severity"] == "critical" for f in r), str([f["severity"] for f in r]))


def test_an_integrity_check_that_has_not_run_recently_says_so() -> None:
    """`store.integrity_stale` — 'ok' from 2020 is not 'ok'."""
    board = sandbox(integrity={"ran_at": OLD, "pragma": "integrity_check", "verdict": "ok",
                               "took_ms": 12, "bytes": 1, "detail": "ok"})
    r = rows(board, "store.integrity_stale")
    check("an old ok verdict raises store.integrity_stale", bool(r),
          str([f["type"] for f in board if f["type"].startswith("store")]))
    check("and the damaged row is NOT raised beside it",
          not rows(board, "store.integrity"),
          "stale and damaged are different states with different remedies")



# ───────── the six that rested on the live board and nothing else ────────────

def test_a_key_in_the_wrong_variable_is_reported() -> None:
    """`env.key_misplaced` — a routing bug wearing an auth bug's clothes."""
    board = sandbox(state={"key-shapes": {"ran_at": OLD, "observations": {
        "PLANTED_API_KEY": {"verdict": "wrong", "seen_at": OLD,
                            # NOT a key-shaped string: the row echoes this
                            # reason, and a fixture whose own words look like a
                            # credential makes the "never the value" assertion
                            # unable to tell the row from the reason.
                            "reason": "the prefix belongs to another provider"}}}})
    r = rows(board, "env.key_misplaced")
    check("a wrong-shaped key raises env.key_misplaced", bool(r),
          str([f["type"] for f in board if f["type"].startswith("env")]))
    check("the row names the VARIABLE, never the value",
          any(f["subject"] == "env:PLANTED_API_KEY" for f in r)
          and not any("sk-or-" in json.dumps(f) for f in r), str(r[:1]))


def test_a_tick_standing_down_repeatedly_is_reported() -> None:
    """`tick.standing_down` — a skipped tick is correct; a run of them is not.

    This rule is why the roster check below stopped counting the live board as
    evidence. It WAS on the board when this file was written — the session held
    a lease and the tick had stood down twice — and it was gone an hour later,
    so the invariant went red on a rule nobody had touched.
    """
    board = sandbox(**{"tick-lease": {"consecutive_skips": 4, "holder": "r-planted",
                                      "last_acquired_at": OLD}})
    r = rows(board, "tick.standing_down")
    check("repeated stand-downs raise tick.standing_down", bool(r),
          str([f["type"] for f in board if f["type"].startswith("tick")]))
    check("and the holder is NAMED rather than guessed at",
          any("r-planted" in f["detail"] for f in r), str([f["detail"][:120] for f in r][:1]))


def test_a_pattern_of_store_faults_is_reported() -> None:
    """`store.faults_recurring` — one fault is an incident, three is a pattern."""
    bf = builder()
    fault = {"at": OLD, "op": "index_batch", "sqlite_errorname": "SQLITE_CORRUPT",
             "free_mib": 400, "holders": 1}
    got = bf.store_fault_findings([fault, {**fault, "op": "search"}, fault])
    r = rows(got, "store.faults_recurring")
    check("three faults raise store.faults_recurring", bool(r), str([f["type"] for f in got]))
    check("at warning severity once it is a pattern",
          all(f["severity"] == "warning" for f in r), str([f["severity"] for f in r]))
    check("and an empty log raises nothing",
          not bf.store_fault_findings([]), "absent is not zero, and zero is not a finding")


def test_an_extra_checkout_holding_work_is_reported() -> None:
    """`worktree.unpushed` — commits no remote has, in a folder nobody looks at."""
    def plant(doc):
        r = doc["repositories"][0]
        r.setdefault("local", {})["extra_checkouts"] = [
            {"folder": "/tmp/planted-worktree", "branch": "wip", "sync": "ahead",
             "worktree_of": r["id"]}]
    board = sandbox(registry={"repositories.json": plant})
    r = rows(board, "worktree.unpushed")
    check("an at-risk extra checkout raises worktree.unpushed", bool(r),
          str([f["type"] for f in board if f["type"].startswith("worktree")]))
    # THE PLANTED REPOSITORY, not the board's total. A count taken from the
    # estate on the day it was written reports a defect in the rule the day the
    # estate grows another at-risk checkout of its own.
    planted = json.loads((paths.REGISTRY / "repositories.json").read_text(
        encoding="utf-8"))["repositories"][0]["id"]
    mine = [f for f in r if f["subject"] == planted]
    check("one row for the planted repository, not one per checkout",
          len(mine) == 1, f"{len(mine)} rows for {planted}")


def test_a_repository_with_no_remote_at_all_is_reported() -> None:
    """`repo.no_remote` — history that exists on exactly one disk."""
    def plant(doc):
        doc["projects"][0]["local_only"] = {"unpublished": True, "commits": 7, "dirty": 2,
                                            "branch": "main", "last_commit": "2026-09-01",
                                            "path": "/tmp/planted-repo"}
    board = sandbox(registry={"projects.json": plant})
    r = rows(board, "repo.no_remote")
    check("an unpublished checkout with commits raises repo.no_remote", bool(r),
          str([f["type"] for f in board if f["type"].startswith("repo")]))
    check("and the uncommitted files are named as a DIFFERENT loss",
          any("not in any commit" in f["detail"] for f in r),
          "pushing would not have saved them, so the remedy must not say push")


#: The boundary the operator would curate, planted into the synthetic
#: workspace's own config: one host outside by decision, one pending with
#: candidate projects. The rule's three classes are driven, not a snapshot of
#: anybody's opinions.
BOUNDARY = {"hosts": {
    "outside.example.com": {"status": "outside", "why": "a backend host another team runs"},
    "pending.example.com": {"status": "pending", "why": "material coming",
                            "candidates": ["alpha-web", "beta-api"]}}}


def _outside_host() -> str:
    return "outside.example.com"


def _pending_host() -> str | None:
    return "pending.example.com"


def test_unmapped_analytics_traffic_is_one_registry_row() -> None:
    """`analytics.unmapped_host` — traffic the registry cannot attribute.

    A plugin that measured a host no project claims puts it in its receipt note;
    the board raises one row pointing at the registry, not one per plugin.
    """
    plugins = {"ran_at": OLD, "installed": 3, "metric_rows": 0, "distinct_metrics": 0,
               "plugins": [
                   # One host ruled outside, one pending, and a made-up one nobody
                   # spoke about — the rule's three classes.
                   {"id": "cloudflare-analytics", "note": "unmapped zones (3), by yesterday's requests: "
                    + f"{_outside_host()} (50948244), zzz-unknown.example (12)"
                    + (f", {_pending_host()} (3)" if _pending_host() else "") + " — outside"},
                   {"id": "disk-usage", "note": ""},
                   {"id": "search-console", "note": "unmapped properties (1): sc-domain:c.example"}]}
    boundary = paths.config_file("host_boundary.json")
    kept = boundary.read_bytes() if boundary.is_file() else None
    boundary.write_text(json.dumps(BOUNDARY), encoding="utf-8")
    try:
        board = sandbox(plugins=plugins)
    finally:
        if kept is None:
            boundary.unlink()
        else:
            boundary.write_bytes(kept)
    r = rows(board, "analytics.unmapped_host")
    check("unmapped traffic raises exactly one row", len(r) == 1,
          str([f["type"] for f in board if f["type"].startswith("analytics")]))
    check("naming the sources but at INFO — it is the estate's boundary, not a fault",
          r and r[0]["severity"] == "info"
          and "cloudflare-analytics" in r[0]["detail"], str(r)[:200])
    check("a plugin with no unmapped note contributes nothing",
          r and "disk-usage" not in r[0]["detail"], str(r)[:200])
    # The planted boundary is the operator's word: one host outside by
    # decision, one pending, the made-up host unclassified.
    check("a host the operator decided about is named WITH the reason",
          r and _outside_host() in r[0]["detail"] and "outside by decision" in r[0]["detail"],
          str(r)[:300])
    if _pending_host():
        check("a pending host lists its candidate projects",
              r and _pending_host() in r[0]["detail"] and "candidates" in r[0]["detail"],
              str(r)[:400])
    else:
        print("  SKIP  a pending host lists its candidate projects [covered: the boundary "
              "file holds no pending host today; tests/test_estate_surfaces.py drives the "
              "pending class on a planted file]")
    check("and a host nobody has spoken about is the only thing still asked about",
          r and "unclassified (1)" in r[0]["detail"] and "zzz-unknown.example" in r[0]["detail"],
          str(r)[:400])


def test_a_key_moved_at_heroku_with_no_journal_entry_is_a_row() -> None:
    """`secret.moved_unrecorded`: the provider's trail against the
    movements journal, driven with a temp vault dir either empty or holding
    the matching entry."""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    at = (now - _dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    heroku = {"scanned_at": OLD, "apps": [{"name": "some-app", "config_releases": [
        {"version": 42, "at": at, "vars": ["DATABASE_URL", "PL_DASH_ENABLED"], "by": "config:set"}]}]}
    vault_dir = pathlib.Path(tmpdir.mkdtemp()) / "projects"
    vault_dir.mkdir()
    prev = os.environ.get("OBSERVATORY_VAULT_DIR")
    os.environ["OBSERVATORY_VAULT_DIR"] = str(vault_dir)
    try:
        board = sandbox(heroku=heroku)
        r = rows(board, "secret.moved_unrecorded")
        check("a secret-shaped variable changed at Heroku with an empty journal is a warning",
              len(r) == 1 and r[0]["severity"] == "warning" and "DATABASE_URL" in r[0]["detail"]
              and "PL_DASH_ENABLED" not in r[0]["detail"], str(r)[:300])
        (vault_dir / "movements.jsonl").write_text(json.dumps(
            {"at": at, "event": "moved", "secret": "some-app/prod/DATABASE_URL",
             "how": "set on heroku", "tool": "vault.py"}) + "\n", encoding="utf-8")
        board = sandbox(heroku=heroku)
        check("and a journal entry within two hours naming the variable silences it",
              not rows(board, "secret.moved_unrecorded"), str(rows(board, "secret.moved_unrecorded"))[:200])
    finally:
        if prev is None:
            os.environ.pop("OBSERVATORY_VAULT_DIR", None)
        else:
            os.environ["OBSERVATORY_VAULT_DIR"] = prev


def test_the_mcp_inventory_raises_four_kinds_of_row() -> None:
    """`mcp.unreachable`, `mcp.key_in_url`, `mcp.needs_auth`, `mcp.own_unregistered`
    — the four things nothing said once agents declared their own servers."""
    doc = {"servers": [
        {"id": "mcp:claude/user/dead", "name": "dead", "agent": "claude", "liveness": "failed", "key_in_url": False},
        {"id": "mcp:claude/user/keyed", "name": "keyed", "agent": "claude", "liveness": "connected", "key_in_url": True},
        {"id": "mcp:cursor/user/keyed", "name": "keyed", "agent": "cursor", "liveness": "not-probed", "key_in_url": True},
        {"id": "mcp:claude/user/oauth", "name": "oauth", "agent": "claude", "liveness": "needs-auth", "key_in_url": False},
        {"id": "mcp:claude/user/fine", "name": "fine", "agent": "claude", "liveness": "connected", "key_in_url": False}],
        "own_declared": False,
        "totals": {"declarations": 5, "distinct_servers": 4, "by_liveness": {}, "in_one_agent_only": 3, "key_in_url": 2}}
    # `registry=` takes a MUTATION of the copied document, not a replacement.
    board = sandbox(registry={"mcp-servers.json": lambda d: (d.clear(), d.update(doc))})
    r = rows(board, "mcp.unreachable")
    check("a failed server is one WARNING row naming it", len(r) == 1
          and r[0]["severity"] == "warning" and "dead (claude)" in r[0]["detail"], str(r)[:200])
    r = rows(board, "mcp.key_in_url")
    check("a key in a URL is a warning, counted once per server not per agent",
          len(r) == 1 and "1 MCP server" in r[0]["title"] and "keyed" in r[0]["detail"], str(r)[:200])
    r = rows(board, "mcp.needs_auth")
    check("a server waiting for a sign-in is INFO with no action demanded",
          len(r) == 1 and r[0]["severity"] == "info" and "oauth" in r[0]["detail"], str(r)[:200])
    r = rows(board, "mcp.own_unregistered")
    check("the observatory's own server declared nowhere is a warning with the command",
          len(r) == 1 and "claude mcp add observatory" in r[0]["action"], str(r)[:200])
    check("and a healthy server raises nothing",
          not any("fine" in x.get("detail", "") for x in board if x["type"].startswith("mcp.")))


def test_stale_knowledge_is_one_row_per_artefact_not_160() -> None:
    """`project.graph_stale` and `project.wiki_stale` — aggregated, worst first.

    The measured case behind both: a project committed weeks past its last
    wiki note, its graph days behind, and nothing said so.
    """
    local = {"scanned_at": OLD, "folders": [
        {"folder": "alpha", "is_git": True, "last_commit": "2026-09-10",
         "graph_built_on": "2026-08-01"},
        {"folder": "beta", "is_git": True, "last_commit": "2026-09-10",
         "graph_built_on": "2026-09-09"},
        {"folder": "gamma", "is_git": True, "last_commit": "2026-09-10"}]}
    vault = [{"folder": "alpha", "notes_updated_on": "2026-07-01"},
             {"folder": "beta", "notes_updated_on": "2026-09-10"}]
    board = sandbox(local=local, vault=vault)
    g = rows(board, "project.graph_stale")
    check("an old graph raises ONE aggregated row", len(g) == 1,
          str([f["type"] for f in board if f["type"].startswith("project.")]))
    check("naming the worst offender with its lag in days",
          g and "alpha" in g[0]["detail"] and "d behind" in g[0]["detail"],
          str(g)[:200])
    check("a graph within grace is not counted",
          g and "beta" not in g[0]["detail"], str(g)[:200])
    check("and a project with NO graph is not counted — adoption is a choice",
          g and "gamma" not in g[0]["detail"], str(g)[:200])
    w = rows(board, "project.wiki_stale")
    check("old notes raise one aggregated row", len(w) == 1
          and "alpha" in w[0]["detail"], str(w)[:200])
    check("and the row explains why a stale note is not cosmetic",
          w and "named in vault notes" in w[0]["detail"], str(w)[:200])


def test_every_clone_sync_state_has_a_row_of_its_own() -> None:
    """`clone.behind` reached the board named by no suite — the roster caught it.

    The `clone.*` types are built from a variable, one per sync state, so a
    state the estate had never produced had never been driven either. The
    vocabulary lives in one dict in the builder; this plants a checkout in each
    state and asserts the row, which covers the family rather than the member
    that happened to appear.
    """
    bf = builder()
    states = sorted(bf.SYNC_FINDINGS) if hasattr(bf, "SYNC_FINDINGS") else []
    if not states:
        # The dict's name, read rather than assumed: a rename must break this
        # loudly instead of silently covering nothing.
        cand = [n for n in dir(bf) if isinstance(getattr(bf, n), dict)
                and "behind" in getattr(bf, n) and "diverged" in getattr(bf, n)]
        check("the sync vocabulary is one dict in the builder", bool(cand), str(cand))
        if not cand:
            return
        states = sorted(getattr(bf, cand[0]))
    seen = []
    for state in states:
        def plant(doc, state=state):
            doc["repositories"][0].setdefault("local", {})["extra_checkouts"] = [
                {"folder": f"/tmp/planted-{state}", "branch": "b", "sync": state}]
            doc["repositories"][0]["local"]["sync"] = state
        board = sandbox(registry={"repositories.json": plant})
        got = {f["type"] for f in board if f["type"].startswith("clone.")}
        seen.append((state, sorted(got)))
    covered = {t for _, ts in seen for t in ts}
    check(f"every one of the {len(states)} sync states was driven", len(seen) == len(states))
    check("and `clone.behind` is among the rows they raise",
          "clone.behind" in covered, str(sorted(covered)))


def test_a_quiet_project_that_never_shipped_is_reported() -> None:
    """`portfolio.unreleased_and_quiet` — the portfolio question, asked once."""
    # BOTH HALVES PLANTED, because relying on the estate for one of them made
    # this case fail on a fresh workspace — where the tiers are whatever the
    # registry says and the store is empty. The rule needs a project
    # whose tier is not the busiest one AND a release count of zero; the fixture
    # now supplies both, and reads the vocabulary from `activity` rather than
    # spelling tier names it would have to keep in step.
    tiers = json.loads(paths.config_file("activity_tiers.json").read_text(encoding="utf-8"))
    quiet_tier = (tiers["tiers"] if isinstance(tiers, dict) else tiers)[-1]["id"]
    # THE METRIC THE ROLE RESOLVES TO, asked of the manifests exactly as the
    # rule asks. An invented name made this pass for the wrong reason: the row
    # it raised on a populated estate came from the real series, not from the
    # plant, and on an empty store nothing raised it at all. A fixture that plants a key nothing reads is not a fixture.
    metric = None
    for man in sorted((ROOT / "plugins").glob("*.json")):
        for m in (json.loads(man.read_text(encoding="utf-8")).get("metrics") or []):
            if m.get("role") == "release.count":
                metric = m.get("name")
    if metric is None:
        print("  SKIP  no installed plugin claims the `release.count` role "
              "[uncoverable: the rule asks the manifests, and none answers here]")
        return
    target = SYNTHETIC_PROJECT

    def make_quiet(doc):
        for p in doc["projects"]:
            if p["id"] == target:
                p["activity_tier"] = quiet_tier
                p["lifecycle"] = "active"

    board = sandbox(registry={"projects.json": make_quiet},
                    sql=[f"DELETE FROM metrics WHERE metric = '{metric}'",
                         "INSERT INTO metrics (project_id, metric, at, value, unit,"
                         " source, payload_json, recorded_at)"
                         f" VALUES ('{target}', '{metric}', '{OLD}', 0, 'count',"
                         f" 'planted', '{{}}', '{OLD}')"])
    r = rows(board, "portfolio.unreleased_and_quiet")
    check("a quiet project measured at zero releases raises the portfolio row", bool(r),
          str([f["type"] for f in board if f["type"].startswith("portfolio")]))
    check("at info, because it is a standing state rather than a fault",
          all(f["severity"] == "info" for f in r), str([f["severity"] for f in r]))


# ───────────────────────── the roster itself ─────────────────────────────────

#: Rules driven above, so the roster check can tell "measured here" from
#: "measured nowhere". A type added to the builder joins one of three classes;
#: this list is the third, and it is the only one a person maintains.
DRIVEN = {"dashboard.blank", "fixtures.leaked", "host.disk_unknown",
          "interpretation.not_english", "plugin.waiting", "provider.health_unmeasured",
          "provider.health_unreadable", "provider.model_quarantined", "store.integrity",
          "store.integrity_stale", "wiki.links_unchecked",
          # The six that rested on the live board alone, added when the roster
          # check went red on `tick.standing_down` — a rule that was on the
          # board while this file was being written and gone an hour later.
          "env.key_misplaced", "portfolio.unreleased_and_quiet", "repo.no_remote",
          "store.faults_recurring", "tick.standing_down", "worktree.unpushed"}


#: Rules the original repository's suites name but no suite in the portable
#: sandbox does, measured with only the baseline suites and this one present.
#: A SUPERSET on purpose: the sandbox holds whichever suites are registered, so
#: a larger set only shrinks what is missing. A rule absent from both this list
#: and every portable suite fails the roster check below; an entry a portable
#: suite now names is reported so the list can shrink.
NAMED_OUTSIDE_THE_PORTABLE_SET = {
    "clone.stale", "collector.degraded", "companion.faults_unlogged", "companion.not_recording",
    "companion.stale_install", "dashboard.unverified", "deltas.not_diffed", "domain.dark",
    "domain.expiring", "domain.hold", "domain.hold_unknown", "domain.unmeasured",
    "erasure.not_scrubbed", "gate.skips_uncovered", "host.reclaimable_lever", "identity.ambiguous",
    "identity.unreadable", "interpretation.faults", "interpretation.halted", "interpretation.malformed",
    "interpretation.unreasoned", "ledger.review_backlog", "ledger.review_expiring", "model.degraded",
    "notify.channel_failing", "plugin.broken", "plugin.refused", "project.declared_alive_measured_dead",
    "project.unobservable", "projection.lagging", "projection.uncommitted", "rollup.frozen_incomplete",
    "scan.stale", "server.silent", "site.dead", "skill.stale_session",
    "tick.step_failed", "wallet.shared_key", "wiki.broken_link", "work.unverifiable",
    "work.unwitnessed",
}


def declared_types() -> set[str]:
    """Every finding type the builder can produce, INCLUDING the computed ones.

    A regex over `"type": "…"` misses the computed ones — `clone.ahead`,
    `clone.local-only-branch`, `clone.unpushed-and-remote-moved` are built from
    a variable — so the first version of this sweep called three live rules
    undeclared. The board's own rows close the gap: a type that is on the board
    is declared by definition, whatever the source looks like.
    """
    import re
    src = (ROOT / "tools/build_findings.py").read_text(encoding="utf-8")
    live = {f["type"] for f in json.loads(
        (paths.REGISTRY / "findings.json").read_text(encoding="utf-8"))["findings"]}
    return set(re.findall(r'"type": "([a-z_.]+)"', src)) | live


def test_every_rule_has_evidence_of_some_kind() -> None:
    """Evidence is a suite that names the rule — NOT a row that happens to be live.

    The first version counted "on the board right now" as evidence, and it took
    one gate run to show why that is wrong: `tick.standing_down` was live while
    this file was being written (the session held a lease) and gone by the next
    run, so the check went red on a rule nobody had touched. A row on the board
    is a fact about this hour. A suite is a fact about the repository.
    """
    types = declared_types()
    named = set()
    for p in (ROOT / "tests").glob("*.py"):
        txt = p.read_text(encoding="utf-8", errors="replace")
        if p.name == pathlib.Path(__file__).name:
            # The gap list names rules precisely BECAUSE no suite does; read
            # as evidence it would certify every rule it lists.
            start = txt.find("NAMED_OUTSIDE_THE_PORTABLE_SET = {")
            txt = txt[:start] + txt[txt.find("}\n", start) + 2:]
        named |= {t for t in types if f'"{t}"' in txt or f"'{t}'" in txt}
    unmeasured = sorted(types - named - DRIVEN)
    # PORTED-DIVERGED: the portable runner copies only the registered suites
    # into its sandbox, and some rules are still named only by suites that are
    # not portable (or not ported yet). Those are listed, the rest must hold.
    new = [t for t in unmeasured if t not in NAMED_OUTSIDE_THE_PORTABLE_SET]
    check(f"every one of the {len(types)} finding rules is named by a suite",
          not new, f"no suite names: {new}")
    gap = [t for t in unmeasured if t in NAMED_OUTSIDE_THE_PORTABLE_SET]
    if gap:
        print(f"  SKIP  KNOWN-GAP: {len(gap)} finding rule(s) are named only by suites "
              f"outside this sandbox's portable set: {', '.join(gap)}")
    covered = sorted(NAMED_OUTSIDE_THE_PORTABLE_SET - set(unmeasured))
    if covered:
        print(f"  NOTE  {len(covered)} rule(s) in NAMED_OUTSIDE_THE_PORTABLE_SET are now "
              f"named by a portable suite and can leave that list: {', '.join(covered)}")
    stale = sorted(DRIVEN - types)
    check("and every rule this file claims to drive still exists", not stale, str(stale))


def test_a_board_with_the_agent_off_does_not_ask_for_model_health() -> None:
    """A new user's board said "no model has been asked anything recently" with the
    agent feature off — a measurement nobody asked this workspace to make."""
    bf = builder()
    off = bf.provider_findings({}, {"ran_at": OLD}, agent_enabled=False)
    check("agent off: no provider.health_unmeasured", not rows(off, "provider.health_unmeasured"),
          str([f["type"] for f in off]))
    on = bf.provider_findings({}, {"ran_at": OLD}, agent_enabled=True)
    check("agent on: the unmeasured health is still said", bool(rows(on, "provider.health_unmeasured")))
    check("and an unreadable quarantine list is reported either way",
          bool(rows(bf.provider_findings(None, {"ran_at": OLD}, agent_enabled=False),
                    "provider.health_unreadable")))


def test_a_disabled_integration_is_not_an_unmeasured_source() -> None:
    """model.degraded warned that wiki, github, sessions, remotes and bitbucket were
    unmeasured on a board where none of them is switched on."""
    bf = builder()
    deg = [{"source": "wiki", "reason": "wiki scan unavailable"},
           {"source": "github", "reason": "no repository listing available"},
           {"source": "sessions.json", "reason": "optional collector output unavailable"},
           {"source": "remotes.json", "reason": "optional collector output unavailable"},
           {"source": "bitbucket.json", "reason": "optional collector output unavailable"},
           {"source": "ownership", "reason": "ownership policy unreadable"}]
    got = bf.merge_findings(deg, integrations={"github": True})
    check("only the enabled integration and the always-read source remain",
          len(got) == 1 and "github" in got[0]["detail"] and "ownership" in got[0]["detail"]
          and "wiki" not in got[0]["detail"] and "bitbucket" not in got[0]["detail"],
          str(got)[:300])
    check("nothing enabled and nothing else missing: no row at all",
          bf.merge_findings(deg[:5], integrations={}) == [])
    check("an enabled integration's gap is still a warning",
          bool(bf.merge_findings(deg[:1], integrations={"wiki": True})))


def test_no_message_names_a_command_a_user_cannot_run() -> None:
    """Finding actions, errors and hints said `./observatory.py key` and
    `node dashboard/smoke.js docs/projects-dashboard.html` — paths inside the
    engine's source tree that resolve nowhere for an installed user. The command
    a user has is `project-observatory full STEP`. Comments may keep history."""
    stale = re.compile(r"\./observatory\.py |node dashboard/smoke\.js |run observatory\.py init")
    offenders = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in ("tests", ".venv") or "__pycache__" in rel.parts:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if not line.lstrip().startswith("#") and stale.search(line):
                offenders.append(f"{rel}:{n}")
    check("no user-facing string names a source-tree command", not offenders, ", ".join(offenders[:8]))
    # THE ENGINE'S TOOLS BY THEIR INSTALLED PATH. A bare `tools/vault.py put …`
    # resolves only inside the engine directory; an installed user runs
    # `python "$(project-observatory full-path)/tools/vault.py" …`. Read through
    # the syntax tree, so a module's own usage docstring may stay relative.
    import ast
    bare = re.compile(r'(?<!full-path\)/)(?:\./)?tools/(vault|use_secret|install_key|serverd)\.py"? '
                      r'(put|rotate|settle|moved|leak|run|remove|list|inject|names|pipe|--status|--install|--uninstall|--for)')
    loose = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in ("tests", ".venv") or "__pycache__" in rel.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        docs = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs \
                    and bare.search(node.value) and not node.value.startswith(("installedBy", "tools/")):
                loose.append(f"{rel}:{node.lineno}")
    check("no message hands over a tool by its source-tree path", not loose, ", ".join(loose[:10]))


if __name__ == "__main__":
    print("the board's rules — eleven that had never been seen firing\n")
    for fn in (test_a_blank_page_is_reported,
               test_an_unreadable_health_file_is_not_read_as_healthy,
               test_a_quarantined_model_is_named,
               test_an_empty_quarantine_list_after_silence_says_so,
               test_an_unmeasurable_volume_is_reported,
               test_leaked_fixtures_are_charged_to_this_project,
               test_an_unchecked_wiki_says_so,
               test_a_conclusion_in_another_language_is_reported,
               test_a_plugin_that_never_ran_is_reported,
               test_a_damaged_store_is_reported,
               test_an_integrity_check_that_has_not_run_recently_says_so,
               test_a_key_in_the_wrong_variable_is_reported,
               test_a_tick_standing_down_repeatedly_is_reported,
               test_a_pattern_of_store_faults_is_reported,
               test_an_extra_checkout_holding_work_is_reported,
               test_a_repository_with_no_remote_at_all_is_reported,
               test_unmapped_analytics_traffic_is_one_registry_row,
               test_a_key_moved_at_heroku_with_no_journal_entry_is_a_row,
               test_the_mcp_inventory_raises_four_kinds_of_row,
               test_stale_knowledge_is_one_row_per_artefact_not_160,
               test_every_clone_sync_state_has_a_row_of_its_own,
               test_a_quiet_project_that_never_shipped_is_reported,
               test_every_rule_has_evidence_of_some_kind,
               test_no_message_names_a_command_a_user_cannot_run,
               test_a_board_with_the_agent_off_does_not_ask_for_model_health,
               test_a_disabled_integration_is_not_an_unmeasured_source):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mevery driven board rule fires\033[0m")
