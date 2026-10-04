#!/usr/bin/env python3
"""The projection: its version knob, its ceiling, and its lag.

`store/indexer.py` opens by promising that losing the projections is a rebuild
rather than a data loss, and the architecture declares the outbox idempotent by
`memory_id + revision + projection_version`. Three things were
untrue of that:

* **The version knob silently stopped the work it versions.** `store/ledger.py`
  wrote the literal `1` into every outbox row while the indexer filtered on its
  own `PROJECTION_VERSION`. Setting it to 2 made the pending query match nothing,
  so the indexer printed *"outbox empty — every committed revision is already
  projected"* and exited 0. The declared idempotency key, turned, produced a
  confident report of completeness over an index containing nothing.

* **It embedded without checking its ceiling.** `agent/providers.py` has exactly
  two functions that call `charge()`: `complete()` guarded itself and `embed()`
  required every caller to remember. `observe.py` remembered, `survey.py`
  remembered only after the wire audit, and the indexer — which embeds a batch
  per tick and is the largest spender of the three — never did, while the
  shared key stood well over its daily ceiling.

* **The lag was invisible.** Both indexes are fed by the outbox, so a pending
  queue means every search since then answered over an index that does not hold
  those revisions — and `degraded` named the store and the other sources while
  saying nothing about the projection.
"""
from __future__ import annotations
import ast, importlib, importlib.util, json, os, pathlib, sqlite3, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402
sys.path.insert(0, str(ROOT / "agent"))

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def utc(hours_ago: float = 0) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def fixture(rows: int = 2, *, committed_hours_ago: float = 0,
            version: int = 1) -> tuple[pathlib.Path, sqlite3.Connection]:
    """A store with committed revisions and a queue that has not drained."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-proj-"))
    os.environ["OBSERVATORY_DB"] = str(d / "observatory.db")
    import paths
    importlib.reload(paths)
    from store import db as sdb
    importlib.reload(sdb)
    conn = sdb.connect()
    for i in range(rows):
        conn.execute(
            "INSERT INTO ledger (memory_id, revision, kind, project_id, function, scope,"
            " statement, why, state, confidence, owner, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"mem:{i}", 1, "observation", "project:alpha", "semantic", "project",
             f"a statement number {i} about the estate", "because it was measured",
             "proposed", 0.5, "agent:test", utc(committed_hours_ago)))
        conn.execute("INSERT INTO outbox (memory_id, revision, projection_version)"
                     " VALUES (?,?,?)", (f"mem:{i}", 1, version))
    conn.commit()
    import embedding_consent
    embedding_consent.grant("project:alpha")
    return d, conn


def load_indexer():
    spec = importlib.util.spec_from_file_location("ix_proj", ROOT / "store/indexer.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ─────────────────────────── the version knob ────────────────────────────

def test_the_version_has_one_owner() -> None:
    from store import db as sdb
    from store import ledger as L
    ix = load_indexer()
    check("the store owns the projection version", hasattr(sdb, "PROJECTION_VERSION"))
    check("and the indexer takes it from there rather than declaring its own",
          ix.PROJECTION_VERSION is sdb.PROJECTION_VERSION or
          ix.PROJECTION_VERSION == sdb.PROJECTION_VERSION,
          f"{ix.PROJECTION_VERSION} vs {sdb.PROJECTION_VERSION}")
    src = (ROOT / "store/ledger.py").read_text(encoding="utf-8")
    check("the ledger stamps the row from the same constant, not a literal",
          "VALUES (?,?,1)" not in src and "store_db.PROJECTION_VERSION" in src,
          "two numbers that must agree, in two files, with nothing checking")


def test_a_row_stamped_for_an_older_contract_is_still_projected() -> None:
    """`=` made it invisible; `<=` makes it work to be redone."""
    d, conn = fixture(rows=1, version=0)
    ix = load_indexer()
    pending = conn.execute(
        "SELECT count(*) FROM outbox WHERE consumed_at IS NULL"
        " AND projection_version <= ?", (ix.PROJECTION_VERSION,)).fetchone()[0]
    check("an older stamp is in the queue, not hidden from it", pending == 1, str(pending))
    conn.close()


def test_a_row_stamped_for_a_newer_contract_is_refused_not_ignored() -> None:
    d, conn = fixture(rows=1, version=99)
    ix = load_indexer()
    rc = ix.cmd_index(conn, limit=10)
    check("an indexer older than its store refuses", rc == 1, f"exit {rc}")
    left = conn.execute(
        "SELECT count(*) FROM outbox WHERE consumed_at IS NULL").fetchone()[0]
    check("and consumes nothing", left == 1, str(left))
    conn.close()


def test_an_empty_queue_at_the_wrong_version_is_not_called_current() -> None:
    """The exact sentence the bump produced: complete, of the wrong contract."""
    d, conn = fixture(rows=0)
    conn.execute("UPDATE vec_meta SET projection_version = 0")
    conn.commit()
    ix = load_indexer()
    rc = ix.cmd_index(conn, limit=10)
    check("an empty queue over projections built at another version fails",
          rc == 1, f"exit {rc}")
    conn.close()


def test_rebuild_completes_the_version_transition() -> None:
    """Otherwise the only way out of a bump cannot itself perform one."""
    src = (ROOT / "store/indexer.py").read_text(encoding="utf-8")
    check("rebuild re-stamps the queue",
          "UPDATE outbox SET projection_version = ?" in src)
    check("and records the version the projections are now built at",
          "projection_version = ?" in src and "vec_meta SET checkpointed_seq = 0" in src)
    check("and says what its own cap left behind",
          "the rebuild cap is" in src,
          "a rebuild that truncates leaves data MISSING, not merely late")


# ─────────────────────────── the ceiling ─────────────────────────────────

def charging_functions_without_a_guard(source: str) -> list[str]:
    """Every function that spends must check its own ceiling, before spending.

    A rule over the module rather than over its callers: the class appeared three
    times because `embed()` left the check to whoever called it, and three
    call sites is three chances to forget. Order matters — a check after the
    spend is a receipt, not a guard.
    """
    tree = ast.parse(source)
    bad = []
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        calls = [(c.lineno, c.func.id) for c in ast.walk(fn)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
        charges = [ln for ln, name in calls if name == "charge"]
        if not charges:
            continue
        guards = [ln for ln, name in calls if name == "check_budget"]
        if not guards or min(guards) > min(charges):
            bad.append(fn.name)
    return bad


def test_every_charging_function_checks_its_own_ceiling() -> None:
    """A ceiling that lives in the request is not a ceiling.

    Trap: T13
    """
    src = (ROOT / "agent/providers.py").read_text(encoding="utf-8")
    bad = charging_functions_without_a_guard(src)
    check("no function spends without checking first", not bad,
          f"{bad} charge the wallet with no ceiling check before the spend")


def test_the_rule_catches_a_planted_spender() -> None:
    planted = (
        "def check_budget():\n    return None\n"
        "def charge(*a, **k):\n    pass\n"
        "def spend_freely(x):\n    charge('m', 1.0, 1, 1)\n"
        "def spend_then_check(x):\n    charge('m', 1.0, 1, 1)\n    check_budget()\n"
        "def spend_carefully(x):\n"
        "    if check_budget():\n        raise SystemExit\n    charge('m', 1.0, 1, 1)\n")
    bad = charging_functions_without_a_guard(planted)
    check("an unguarded spender is reported", "spend_freely" in bad, str(bad))
    check("a check AFTER the spend is not a guard", "spend_then_check" in bad, str(bad))
    check("and a guarded one is not reported", "spend_carefully" not in bad, str(bad))


def test_the_indexer_degrades_to_lexical_when_the_ceiling_is_reached() -> None:
    """The degradation this file announces, made real through its own handler."""
    d, conn = fixture(rows=2)
    ix = load_indexer()
    import providers
    original = providers.check_budget
    providers.check_budget = lambda *a, **k: "daily ceiling: 8.2851 of 2.00 credits (fixture)"
    try:
        rc = ix.cmd_index(conn, limit=10)
    finally:
        providers.check_budget = original
    check("the run still succeeds", rc == 0, f"exit {rc}")
    lex = conn.execute("SELECT count(*) FROM search_notes").fetchone()[0]
    check("the lexical index is built anyway", lex == 2, str(lex))
    left = conn.execute(
        "SELECT count(*) FROM outbox WHERE consumed_at IS NULL").fetchone()[0]
    # HELD, not drained — and this assertion was once inverted because the old
    # behaviour was the bug. "Wedged" meant "the queue can never drain";
    # consuming the rows guaranteed the opposite failure, that their vector half
    # could never arrive. A reached ceiling is TRANSIENT — it lifts at midnight —
    # so it is the most predictable trigger of a permanent hole in the vector
    # index, and keeping the rows is what lets the next day's run finish them.
    # Not wedged: the queue drains as soon as the ceiling lifts, as the second
    # run below shows.
    check("the rows are HELD so a later run can embed them", left == 2, str(left))
    ix.providers.embed = lambda texts, log=print, **_kw: {
        "vectors": [[0.01] * 1536 for _ in texts], "cost": 0.0,
        "tokens": len(texts), "model": "stub", "cost_is_estimate": True}
    ix.cmd_index(conn, limit=10)
    after = conn.execute(
        "SELECT count(*) FROM outbox WHERE consumed_at IS NULL").fetchone()[0]
    check("and it is not wedged: the next run drains it", after == 0, str(after))
    conn.close()


def test_the_ceiling_asked_is_the_one_that_applies() -> None:
    """A guard on the wrong meter, twice over.

    The wallet reads the daily and monthly totals from OpenRouter's own
    counters, and embeddings are bought from OpenAI on a different key. Gating
    them on OpenRouter's figures let another consumer of the shared key stop this
    estate's indexing — and made the verdict depend on whether OpenRouter's usage
    endpoint answered: interactively "over the ceiling, stop"; inside the gate,
    "permitted", because the provider was unreachable there and the state fell
    back to the local journal.
    """
    import providers
    state = {
        "source": "provider", "denomination": "credits",
        "today": 9.84, "daily_ceiling": 2.00,          # the shared KEY, over
        "month": 9.84, "monthly_ceiling": 100.0,
        "local_today": 0.0001, "local_month": 0.0001,  # this estate, nowhere near
        "window_spend": 0.0, "velocity_ceiling": 1.0, "window_minutes": 10,
        "key_remaining": 0.0, "key_total": 9.84, "key_limit": 10.0,
        "key_limit_reset": "monthly",
    }
    original = providers.wallet_state
    providers.wallet_state = lambda: state
    try:
        openrouter = providers.check_budget("openrouter")
        openai = providers.check_budget("openai")
    finally:
        providers.wallet_state = original
    check("the shared key's own ceiling still stops OpenRouter spending",
          openrouter is not None, str(openrouter))
    check("and does not stop the embeddings bought elsewhere", openai is None,
          str(openai))
    check("and the message names the key whose limit it is",
          "key" in (openrouter or "").lower(), str(openrouter))

    # With the key's own limit intact, a BUSY KEY no longer answers at all:
    # the caps read this project's own journal, so 9.84 spent on a
    # shared key against a 2.00 ceiling permits spending while this estate has
    # spent 0.0001. That inverts the two assertions this block used to make, and
    # the reason is the same one the docstring above already gives for
    # embeddings — extended to the agent, which is the consumer that was
    # actually disabled.
    state["key_remaining"] = 5.0
    state["key_today"], state["key_month"] = state["today"], state["month"]
    providers.wallet_state = lambda: state
    try:
        permitted = providers.check_budget("openrouter")
        state["local_today"] = state["daily_ceiling"] + 1
        own = providers.check_budget("openrouter")
    finally:
        providers.wallet_state = original
        state["local_today"] = 0.0001
    check("a busy shared key permits this project's own spending",
          permitted is None, str(permitted))
    check("while this project's OWN overspend stops it, named as its own",
          "by THIS project" in (own or ""), str(own))
    check("with the key's figure beside it rather than as the reason",
          "another consumer" in (own or ""), str(own))

    # And the other direction: this estate's OWN spend must still stop it.
    state["local_today"] = 5.0
    providers.wallet_state = lambda: state
    try:
        openai_over = providers.check_budget("openai")
    finally:
        providers.wallet_state = original
    check("this project's own daily spend does stop it", openai_over is not None,
          "otherwise embeddings would have no ceiling at all")
    check("and that message does NOT blame a shared key",
          "KEY" not in (openai_over or ""), str(openai_over))


def test_embed_asks_its_own_providers_meter() -> None:
    src = (ROOT / "agent/providers.py").read_text(encoding="utf-8")
    check("embed passes its provider to the guard",
          'check_budget(cfg["provider"])' in src,
          "a bare check_budget() here asks OpenRouter about an OpenAI purchase")


def test_budget_exceeded_is_catchable_by_the_handler_that_exists() -> None:
    import providers
    check("BudgetExceeded is a ProviderError",
          issubclass(providers.BudgetExceeded, providers.ProviderError),
          "otherwise the indexer's own except clause cannot see it")
    src = (ROOT / "store/indexer.py").read_text(encoding="utf-8")
    check("and the indexer catches that class", "providers.ProviderError" in src)


# ─────────────────────────── the lag ────────────────────────────────────

def test_search_reports_the_lag_it_searched_over() -> None:
    d, conn = fixture(rows=3)
    conn.close()
    # Offline: search embeds its query, and no provider call may leave a test.
    env = dict(os.environ, OBSERVATORY_DB=str(d / "observatory.db"),
               OBSERVATORY_OFFLINE="1")
    code = ("import sys, json; sys.path.insert(0, %r); import survey;"
            " print(json.dumps(survey.search('statement', limit=5)))" % str(ROOT))
    p = subprocess.run([PY, "-c", code], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=300)
    try:
        result = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        check("search returned a parseable answer", False, p.stdout[-200:] + p.stderr[-300:])
        return
    sources = {dd["source"]: dd["reason"] for dd in result.get("degraded", [])}
    check("the projection's lag is declared", "projection" in sources, str(list(sources)))
    if "projection" in sources:
        check("with the count of revisions it could not include",
              "3 committed revision" in sources["projection"], sources["projection"])


def test_the_lag_finding_fires_on_AGE_not_on_COUNT() -> None:
    """500 rows draining next tick are healthy; one stuck for a week is not."""
    sys.path.insert(0, str(ROOT / "tools"))

    def findings_for(hours_ago: float) -> list[dict]:
        d, conn = fixture(rows=1, committed_hours_ago=hours_ago)
        conn.close()
        env = dict(os.environ, OBSERVATORY_DB=str(d / "observatory.db"),
                   OBSERVATORY_REGISTRY=str(d / "registry"), OBSERVATORY_OFFLINE="1")
        (d / "registry").mkdir(exist_ok=True)
        p = subprocess.run([PY, "tools/build_findings.py", "--json"], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=600)
        try:
            return json.loads(p.stdout)["findings"]
        except (ValueError, KeyError):
            return [{"type": f"UNPARSEABLE: {p.stdout[-200:]}{p.stderr[-300:]}"}]

    fresh = [f["type"] for f in findings_for(0.1)]
    check("a revision committed minutes ago raises nothing",
          "projection.lagging" not in fresh, str(fresh))
    stale = [f["type"] for f in findings_for(48)]
    check("one committed two days ago and still unindexed does",
          "projection.lagging" in stale, str(stale))


def test_the_threshold_is_recorded_with_its_reasoning() -> None:
    src = (ROOT / "tools/build_findings.py").read_text(encoding="utf-8")
    check("the horizon is a named constant", "PROJECTION_LAG_HOURS" in src)
    check("and it says why that number", "ticks at the launchd cadence" in src,
          "a threshold with no stated basis is a number someone will change blindly")


if __name__ == "__main__":
    print("the projection — versioned, metered, and honest about its lag\n")
    for fn in (test_the_version_has_one_owner,
               test_a_row_stamped_for_an_older_contract_is_still_projected,
               test_a_row_stamped_for_a_newer_contract_is_refused_not_ignored,
               test_an_empty_queue_at_the_wrong_version_is_not_called_current,
               test_rebuild_completes_the_version_transition,
               test_every_charging_function_checks_its_own_ceiling,
               test_the_rule_catches_a_planted_spender,
               test_the_indexer_degrades_to_lexical_when_the_ceiling_is_reached,
               test_the_ceiling_asked_is_the_one_that_applies,
               test_embed_asks_its_own_providers_meter,
               test_budget_exceeded_is_catchable_by_the_handler_that_exists,
               test_search_reports_the_lag_it_searched_over,
               test_the_lag_finding_fires_on_AGE_not_on_COUNT,
               test_the_threshold_is_recorded_with_its_reasoning):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe projection is versioned, metered and honest\033[0m")
