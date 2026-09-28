#!/usr/bin/env python3
"""The agent's deterministic half — everything that must hold before a token is spent.

No model is called here, and no request leaves the machine. What is asserted
is the machinery around the call: the budget that has to be enforced
client-side because the API no longer offers a hard ceiling, the three
degradations, and the prompt that must not leak the delta back as prose.

Everything runs in a private synthetic workspace: its models configuration, a
planted model catalogue, its wallet and its store.
"""
from __future__ import annotations
import importlib.util, json, os, pathlib, sqlite3, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
import paths as _paths  # noqa: E402
FAILURES: list[str] = []
PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable

#: The synthetic catalogue and chain. Invented ids and prices: what is under test
#: is how a chain is resolved and ordered, not any real provider's price list.
CATALOGUE = {f"vendor/model-{n}": {"id": f"vendor/model-{n}", "name": f"Synthetic {n}",
                                    "context_length": 8000, "price_in_per_1m": price,
                                    "price_out_per_1m": price * 2,
                                    "supported_parameters": ["structured_outputs",
                                                             "response_format"]}
             for n, price in (("a", 0.5), ("b", 1.5), ("c", 3.0))}


def _prepare_workspace() -> None:
    """The agent is opt-in per workspace, so it is enabled here; the provider
    endpoints point at a closed loopback port so a call a stub missed fails
    fast and locally instead of reaching a real provider."""
    settings = _paths.CONFIG / "settings.json"
    doc = json.loads(settings.read_text(encoding="utf-8"))
    doc.setdefault("features", {})["agent"] = True
    settings.write_text(json.dumps(doc), encoding="utf-8")
    models = _paths.CONFIG / "models.json"
    m = json.loads(models.read_text(encoding="utf-8"))
    m["base_url"] = "http://127.0.0.1:9/api/v1"
    m.setdefault("embedding", {})["base_url"] = "http://127.0.0.1:9/v1"
    # Configured cheapest first; the engine keeps the configured order.
    m["chain"] = [{"id": "vendor/model-a", "why": "synthetic first choice"},
                  {"id": "vendor/model-b", "why": "synthetic second choice"}]
    models.write_text(json.dumps(m), encoding="utf-8")
    from datetime import datetime, timezone
    (_paths.STATE / "openrouter-catalogue.json").write_text(json.dumps({
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "models": CATALOGUE}), encoding="utf-8")


_prepare_workspace()


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def load_agent(db: pathlib.Path):
    os.environ["OBSERVATORY_DB"] = str(db)
    for mod in ("paths", "store.db", "store.ledger", "observe"):
        sys.modules.pop(mod, None)
    spec = importlib.util.spec_from_file_location("observe", ROOT / "agent/observe.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def scratch() -> pathlib.Path:
    return pathlib.Path(tmpdir.mkdtemp(prefix="observatory-agent-")) / "t.db"


def run_agent(db: pathlib.Path, *args: str, env: dict | None = None):
    # THE SCRATCH DIR TOO, beside the store. `agent/observe.py` writes its run
    # report to `paths.SCRATCH / "agent.json"`, and without this the LIVE report
    # was overwritten by a test: `tools/build_findings.py` reads it, so
    # `interpretation.halted` told the operator the layer was stopped by a
    # mode-644 key file in a temp directory that no longer existed.
    e = {**os.environ, "OBSERVATORY_DB": str(db),
         "OBSERVATORY_SCRATCH": str(db.parent / "scratch"), **(env or {})}
    (db.parent / "scratch").mkdir(parents=True, exist_ok=True)
    return subprocess.run([PY, str(ROOT / "agent/observe.py"), *args],
                          cwd=ROOT, capture_output=True, text=True, env=e, timeout=120)


def seed_deltas(db: pathlib.Path, n: int = 2) -> None:
    subprocess.run([PY, "collectors/compute_deltas.py", "snapshot"],
                   cwd=ROOT, capture_output=True, env={**os.environ, "OBSERVATORY_DB": str(db)})
    conn = sqlite3.connect(db)
    with conn:
        for i in range(n):
            conn.execute("INSERT INTO deltas (id, to_scan, subject_id, kind, before_json,"
                         " after_json) SELECT ?, id, ?, 'lifecycle-changed', '\"archived\"',"
                         " '\"active\"' FROM scans LIMIT 1",
                         (f"delta:test{i}", f"project:test-{i}"))
    conn.close()


def providers_mod():
    sys.path.insert(0, str(ROOT / "agent"))
    for m in ("providers",):
        sys.modules.pop(m, None)
    spec = importlib.util.spec_from_file_location("providers", ROOT / "agent/providers.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_no_vendor_id_or_price_in_source() -> None:
    """The rule the provider boundary exists for.

    Trap: T15
    """
    offenders = []
    for f in list((ROOT / "agent").glob("*.py")) + [ROOT / "collectors/compute_deltas.py"]:
        src = f.read_text(encoding="utf-8")
        if f.name == "providers.py":
            continue
        # Vendor prefixes a model id would carry; spelled in pieces so this list
        # is not itself a vendor id in source.
        for marker in ("deep" + "seek/", "claude-" + "opus", "gpt-" + "4", "gpt-" + "5",
                       "anth" + "ropic"):
            if marker in src:
                offenders.append(f"{f.name}: {marker}")
    check("no module but providers.py names a vendor model", not offenders, str(offenders))
    src = (ROOT / "agent/providers.py").read_text(encoding="utf-8")
    check("providers.py carries no price literal either",
          "0.089" not in src and "1.042" not in src,
          "a price in source is wrong the day the provider changes it")
    # The engine keeps the models configuration in the workspace, not the code.
    cfg = json.loads(_paths.config_file("models.json").read_text())
    check("the config names the chain and NOT the prices",
          all("price" not in k for e in cfg["chain"] for k in e),
          str(cfg["chain"][0].keys()))


def test_the_ceiling_is_not_a_field_in_the_request() -> None:
    """The half of T13 that nothing asserted until 2026-09-08.

    Trap: T13

    The pack's row demands three things and only two were checked: that the
    ceiling is a constant in this process, and that spend past it degrades.
    The first clause — `budget_tokens` appears nowhere in the request path —
    was true by accident, and a fact true by accident is one a future edit
    changes without argument. `budget_tokens` is REMOVED from the current API
    and answers 400; `task_budget` is a countdown the model paces itself
    against rather than a limit anything enforces. A ceiling that travels in
    the request is a ceiling the provider is free to ignore.
    """
    offenders = []
    for f in sorted((ROOT / "agent").glob("*.py")):
        src = f.read_text(encoding="utf-8")
        for line in src.splitlines():
            # The words may be DISCUSSED — this comment is itself the reason
            # a bare substring rule would fire on prose. The subject is a key
            # placed into a request body, so the test looks for the quoted
            # form a JSON payload would carry.
            if '"budget_tokens"' in line or '"task_budget"' in line:
                offenders.append(f"{f.name}: {line.strip()[:80]}")
    check("no request body carries a provider-side budget field",
          not offenders, str(offenders))


def test_chain_resolves_from_the_catalogue() -> None:
    pr = providers_mod()
    models, provenance = pr.catalogue()
    # PORTED-DIVERGED: the original counted a live catalogue (> 50 models); the
    # planted one holds exactly the three synthetic models, read from cache.
    check("the catalogue resolves from the cached copy",
          set(models) == set(CATALOGUE) and provenance.startswith("cached"),
          f"{len(models)} models, {provenance}")
    chain, level, _ = pr.resolve_chain()
    check("the configured chain resolves", len(chain) >= 1 and level == "config",
          f"{level}, {len(chain)} models")
    check("every model in the chain carries a live price",
          all(m["price_in_per_1m"] > 0 for m in chain),
          str([(m["id"], m["price_in_per_1m"]) for m in chain]))
    check("every model in the chain supports structured output",
          all(pr.needs_structured(m) for m in chain),
          str([m["id"] for m in chain if not pr.needs_structured(m)]))
    check("the chain is ordered cheapest first",
          [m["price_in_per_1m"] for m in chain] == sorted(m["price_in_per_1m"] for m in chain),
          str([m["price_in_per_1m"] for m in chain]))
    try:
        pr.resolve_chain("not/a-real-model")
        check("an unknown model id is fatal, not a silent fallback", False, "no raise")
    except pr.Fatal:
        check("an unknown model id is fatal, not a silent fallback", True)


def test_attempts_are_capped_in_total() -> None:
    cfg = json.loads(_paths.config_file("models.json").read_text())
    total = cfg["attempts"]["total"]
    check("attempts are capped in total, not per model", isinstance(total, int) and total > 0,
          str(total))
    src = (ROOT / "agent/providers.py").read_text(encoding="utf-8")
    check("the cap is compared against a single counter",
          "while attempts < budget_attempts" in src)
    # DRIVEN rather than read off a comment: the first model's call is made
    # retryable, and the answer must come from the second after exactly one
    # attempt at the first. `_post` is the only function that touches the wire,
    # so replacing it keeps every request inside this process.
    pr = providers_mod()
    saved = pr.HEALTH.read_text() if pr.HEALTH.exists() else None
    calls: list[str] = []

    def fake_post(base_url, key, body, timeout=120):
        calls.append(body["model"])
        if body["model"] == "vendor/model-a":
            raise pr.Retryable("synthetic 503")
        return {"usage": {"cost": 0.001, "prompt_tokens": 3, "completion_tokens": 2},
                "choices": [{"message": {"content": json.dumps({"ok": True})}}]}
    try:
        pr.HEALTH.unlink(missing_ok=True)
        pr._post = fake_post
        pr.read_key = lambda *a, **k: ("sk-or-v1-" + "0" * 40, "synthetic")
        pr.check_budget = lambda *a, **k: None
        pr.charge = lambda *a, **k: None
        got = pr.complete([{"role": "user", "content": "x"}], schema_name="s",
                          schema={"type": "object"}, log=lambda *a: None)
        check("a retryable failure moves to the next model rather than retrying tight",
              calls == ["vendor/model-a", "vendor/model-b"] and got["model"] == "vendor/model-b",
              str(calls))
        check("and the failing model is marked, the one that answered is not",
              pr.unhealthy("vendor/model-a") is not None and pr.unhealthy("vendor/model-b") is None,
              str(pr._health()))
    finally:
        pr.HEALTH.unlink(missing_ok=True)
        if saved:
            pr.HEALTH.write_text(saved)


def test_three_guardrails_in_credits() -> None:
    """The ceiling is a constant in this process, not a field in a request.

    Trap: T13
    """
    pr = providers_mod()
    cfg = pr.config()["wallet"]
    check("the wallet is denominated in the provider's own unit",
          cfg["denomination"] == "credits", cfg["denomination"])
    for k in ("daily_ceiling", "monthly_ceiling", "velocity_ceiling"):
        check(f"{k} is configured", isinstance(cfg[k], (int, float)) and cfg[k] > 0, str(cfg.get(k)))
    saved = pr.WALLET.read_text() if pr.WALLET.exists() else None
    # Daily and monthly now come from the PROVIDER, so seeding only the local
    # journal proves nothing about them. Both paths are exercised, and the local
    # one by making the provider unreachable — a real state, not a mock of one.
    real_usage = pr.provider_usage
    try:
        from datetime import datetime, timezone
        d = datetime.now(timezone.utc)

        pr.provider_usage = lambda force=False: None
        pr._save_wallet({"denomination": "credits", "events": [],
                         "days": {d.strftime("%Y-%m-%d"): cfg["daily_ceiling"] + 0.1},
                         "months": {}})
        got = pr.check_budget() or ""
        check("with the provider unreachable the local journal trips the daily cap",
              "daily ceiling" in got and "journal" in got, got)
        pr._save_wallet({"denomination": "credits", "events": [],
                         "days": {}, "months": {d.strftime("%Y-%m"): cfg["monthly_ceiling"] + 1}})
        check("and the monthly cap", "monthly" in (pr.check_budget() or ""),
              str(pr.check_budget()))
        pr._save_wallet({"denomination": "credits", "days": {}, "months": {},
                         "events": [{"at": pr.iso(), "model": "x",
                                     "cost": cfg["velocity_ceiling"] + 0.01, "in": 1, "out": 1}]})
        check("velocity is local ALWAYS — the API has no rolling window",
              "velocity" in (pr.check_budget() or ""), str(pr.check_budget()))
        pr._save_wallet({"denomination": "credits", "events": [], "days": {}, "months": {}})
        check("an empty wallet permits spending", pr.check_budget() is None,
              str(pr.check_budget()))

        # INVERTED, and the reason is the whole point. This asserted that the
        # PROVIDER's counter trips this system's daily cap — and that counter
        # measures the KEY, which is shared with everything else on the machine
        # that uses the same provider. Once the key had spent 104.80 in a day
        # against a 2.00 ceiling while this project's journal held 0.000001, so
        # the agent, the indexer and the semantic half of the search were
        # disabled by a neighbour with 178 of 300 credits still available. A
        # ceiling must govern what the system controls.
        pr.provider_usage = lambda force=False: {
            "daily": cfg["daily_ceiling"] + 1, "weekly": 0.0, "monthly": 0.0, "total": 0.0,
            "limit": 100, "limit_remaining": 50.0, "limit_reset": "monthly"}
        check("a busy KEY does not trip this project's daily cap",
              pr.check_budget() is None, str(pr.check_budget()))
        st = pr.wallet_state()
        check("both figures are reported so a divergence is visible, not hidden",
              st["key_today"] != st["local_today"],
              f"key={st.get('key_today')} local={st['local_today']}")
        # And this project's OWN overspend still stops it, named as its own.
        today = pr.now().strftime("%Y-%m-%d")
        pr.WALLET.write_text(json.dumps({
            "denomination": "credits", "events": [],
            "days": {today: cfg["daily_ceiling"] + 1}, "months": {today[:7]: 0.0}}),
            encoding="utf-8")
        got = pr.check_budget() or ""
        check("this project's own daily overspend trips it",
              "daily ceiling" in got and "by THIS project" in got, got)
        check("with the key's figure beside it as context",
              "another consumer" in got, got)
        pr.WALLET.write_text(json.dumps({
            "denomination": "credits", "events": [], "days": {}, "months": {}}),
            encoding="utf-8")

        pr.provider_usage = lambda force=False: {
            "daily": 0.0, "weekly": 0.0, "monthly": 0.0, "total": 100.0,
            "limit": 100, "limit_remaining": 0.0, "limit_reset": "monthly"}
        # THE WORDING CHANGED, and the reason is worth keeping: this branch said
        # only the key's own number until the key actually ran out and it read
        # as though this system had spent 300 credits while its journal held
        # 0.1185. It now carries the same three facts as the ceiling messages —
        # the key, this project's own figure, and whose the rest was.
        check("the key's own limit is checked FIRST — it 402s instead of degrading",
              "on this KEY is spent" in (pr.check_budget() or ""), str(pr.check_budget()))

        pr.provider_usage = lambda force=False: None
        stop = pr.charge("x/y", cfg["daily_ceiling"] + 1, 100, 50)
        check("charge() records and enforces in the same breath", stop is not None, str(stop))
    finally:
        pr.provider_usage = real_usage
        pr.WALLET.unlink(missing_ok=True)
        if saved:
            pr.WALLET.write_text(saved)


def test_a_credential_fault_does_not_poison_the_chain() -> None:
    """One dead key must not mark three models unhealthy.

    Trap: T14
    """
    pr = providers_mod()
    check("CredentialError is a distinct type",
          issubclass(pr.CredentialError, pr.Fatal))
    src = (ROOT / "agent/providers.py").read_text(encoding="utf-8")
    check("401 and 403 raise it", "exc.code in (401, 403)" in src)
    check("it is re-raised without marking anything unhealthy",
          "except CredentialError:" in src and "raise\n" in src.replace("                raise\n", "raise\n"))
    saved = pr.HEALTH.read_text() if pr.HEALTH.exists() else None
    try:
        pr.HEALTH.unlink(missing_ok=True)
        p = run_agent(scratch(), "--limit", "1",
                      env={"OPENROUTER_API_KEY": "sk-or-v1-not-a-key",
                            "OBSERVATORY_KEY_FILE": "/nonexistent/key"})
        check("a dead key leaves no model marked unhealthy", len(pr._health()) == 0,
              str(pr._health()))
    finally:
        pr.HEALTH.unlink(missing_ok=True)
        if saved:
            pr.HEALTH.write_text(saved)


def test_health_recovers_on_a_schedule() -> None:
    pr = providers_mod()
    saved = pr.HEALTH.read_text() if pr.HEALTH.exists() else None
    try:
        pr.mark_unhealthy("x/y", "a transient 503")
        check("a marked model is skipped now", pr.unhealthy("x/y") is not None)
        from datetime import datetime, timedelta, timezone
        old = (datetime.now(timezone.utc) - timedelta(hours=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
        pr.HEALTH.write_text(json.dumps({"x/y": {"since": old, "reason": "stale"}}))
        check("it becomes usable again after the probe window — a check that only runs "
              "on failure never recovers", pr.unhealthy("x/y") is None)
        pr.mark_healthy("x/y")
        check("a success clears the mark", pr.unhealthy("x/y") is None)
    finally:
        pr.HEALTH.unlink(missing_ok=True)
        if saved:
            pr.HEALTH.write_text(saved)


def test_no_deltas_spends_nothing() -> None:
    db = scratch()
    load_agent(db)
    p = run_agent(db)
    check("an empty delta table exits clean and says it spent nothing",
          p.returncode == 0 and "spent nothing" in p.stdout, p.stdout[:120])
    # The workspace wallet may hold events by now. What must hold is that a run
    # with nothing to do ADDS none — not that the file is absent.
    w = _paths.STATE / "wallet.json"
    before = len(json.loads(w.read_text()).get("events", [])) if w.exists() else 0
    run_agent(db)
    after = len(json.loads(w.read_text()).get("events", [])) if w.exists() else 0
    check("a run with no deltas adds no spend event", before == after, f"{before} -> {after}")


def test_degrades_without_credential() -> None:
    """No credential is a state a test can REACH, via OBSERVATORY_KEY_FILE.

    Trap: T18
    """
    db = scratch()
    seed_deltas(db)
    p = run_agent(db, env={"OPENROUTER_API_KEY": "", "OBSERVATORY_KEY_FILE": "/nonexistent/key"})
    check("no credential degrades instead of failing", p.returncode == 0, f"exit={p.returncode}")
    check("it names the remedy", "openrouter.ai/keys" in p.stderr, p.stderr[:160])
    check("it says the facts are already recorded", "already recorded" in p.stderr)
    conn = sqlite3.connect(db)
    left = conn.execute("SELECT count(*) FROM deltas WHERE consumed_at IS NULL").fetchone()[0]
    conn.close()
    check("deltas are NOT consumed, so the next run still has them", left == 2, str(left))


def test_a_broken_store_read_costs_one_project_not_the_run() -> None:
    """ASSERTED AT THE SOURCE, and the reason is stated rather than hidden: the
    duplicate-check read sits AFTER the provider call, so reaching it in a test
    would mean paying for a model call. What is asserted is the part that
    decides the blast radius.

    Once that read raised `database disk image is malformed` inside the
    scheduled tick and took the whole run with it — eleven deltas across eleven
    projects unconsumed, a traceback in a log, and `PRAGMA integrity_check`
    answering `ok` afterwards. `continue` versus `break` is the whole difference
    between this module's stated contract ("one call fails → that project's
    delta stays unconsumed, the others proceed") and what actually happened.
    """
    src = (ROOT / "agent/observe.py").read_text(encoding="utf-8")
    # The import BLOCK, read as lines rather than by splitting on blank lines —
    # the first version indexed `src.split("\n\n")[1]` and landed in the module
    # docstring, failing on a file that imports it correctly.
    imports = [l for l in src.splitlines()[:40] if l.startswith("import ")]
    check("sqlite3 is imported for the guard",
          any("sqlite3" in l for l in imports), str(imports))
    block = src.split("except sqlite3.DatabaseError as exc:")
    check("the duplicate-check read is guarded", len(block) == 2,
          f"{len(block) - 1} guard(s) found")
    if len(block) != 2:
        return
    tail = block[1].split("\n            if ")[0]
    check("it CONTINUES rather than breaking", "continue" in tail and "break" not in tail,
          "a break would abort every remaining project, which is what happened")
    check("the project is counted as failed", "failed += 1" in tail, tail[-200:])
    check("and it does not fall back to `no prior`",
          "prior = None" not in tail,
          "defaulting to None would append a SECOND record for that project today "
          "— the duplicate the read exists to prevent")
    check("the reason names the store rather than the project's work",
          "could not be read" in tail, tail[:200])


def test_degrades_at_the_ceiling() -> None:
    db = scratch()
    seed_deltas(db)
    pr = providers_mod()
    from datetime import datetime, timezone
    d = datetime.now(timezone.utc)
    saved = pr.WALLET.read_text() if pr.WALLET.exists() else None
    try:
        pr._save_wallet({"denomination": "credits", "events": [],
                         "days": {d.strftime("%Y-%m-%d"): 99.0}, "months": {}})
        p = run_agent(db, env={"OPENROUTER_API_KEY": "sk-or-v1-not-a-key",
                            "OBSERVATORY_KEY_FILE": "/nonexistent/key"})
        check("a tripped guardrail degrades instead of spending",
              p.returncode == 0 and "daily ceiling" in p.stderr, (p.stderr or p.stdout)[:180])
        check("it reports the spend behind the guardrail", "99.0000" in p.stderr,
              p.stderr[:180])
        check("it says collectors are unaffected", "collectors are unaffected" in p.stderr)
    finally:
        pr.WALLET.unlink(missing_ok=True)
        if saved:
            pr.WALLET.write_text(saved)


def test_prompt_forbids_restating_the_delta() -> None:
    db = scratch()
    a = load_agent(db)
    check("the system prompt forbids restating the delta",
          "Do not restate the delta" in a.SYSTEM)
    check("the system prompt allows an empty answer",
          "worth_recording: false" in a.SYSTEM)
    check("the system prompt forbids inventing a cause",
          "Never invent a cause" in a.SYSTEM)
    p = a.build_prompt("project:x", [{"kind": "lifecycle-changed", "before_json": '"archived"',
                                      "after_json": '"active"'}],
                       {"name": "x", "stack": ["node"]}, ["an earlier note"])
    check("the prompt carries the delta, the facts and the prior notes",
          "lifecycle-changed" in p and '"node"' in p and "an earlier note" in p, p[:160])
    check("the prompt tells the model not to repeat what is recorded",
          "do not repeat" in p.lower())
    # FOLDED, NOT TRUNCATED. This asserted that a flood was cut with
    # the omission stated — which was honest about the cut and silent about what
    # the cut did: the oldest twelve steps of a rise-then-commit cycle all rise,
    # so the sample pointed the wrong way across the window. Deltas of one kind
    # are a range now, and the endpoints are the whole point.
    many = [{"kind": "k", "before_json": str(i), "after_json": str(i + 1), "seq": i}
            for i in range(40)]
    p = a.build_prompt("project:x", many, {}, [])
    check("a flood of one kind folds to its endpoints",
          "k: 0 -> 40" in p, p[-160:])
    check("saying how many steps it took", "over 40 changes" in p, p[-160:])
    check("and nothing is dropped, so no omission has to be stated",
          "more of the same kinds" not in p, p[-160:])


def test_agent_cannot_promote() -> None:
    """Read the ledger's data, not the source that writes it.

    Trap: T21
    """
    src = (ROOT / "agent/observe.py").read_text(encoding="utf-8")
    check("every write is state=proposed", 'state="proposed"' in src)
    check("no promotion path exists in the agent",
          'to_state="supported"' not in src and "transition(" not in src)
    check("the cap is a named constant, not a literal in the call",
          "AGENT_MAX_CONFIDENCE" in src and "min(AGENT_MAX_CONFIDENCE" in src)
    # Read the DATA, not the source. The previous version of this check grepped
    # for a min() call and passed while a real run wrote confidence exactly 1.0.
    import sqlite3 as _sq
    # `paths.DB`, not a literal: the literal made this assertion undrivable —
    # `tools/trap_efficacy.py` could not put a confidence-1.0 row in front of it
    # without writing the operator's own store, so the one check here that reads
    # DATA rather than source was the one check nobody could watch fail. Every
    # other redirect in this repository exists for the same reason.
    sys.path.insert(0, str(ROOT))
    import paths as _paths
    live = _paths.DB
    if live.exists():
        conn = _sq.connect(live)
        try:
            # CURRENT revisions only. A superseded revision keeps the value it
            # was written with — the ledger is append-only, and history not
            # being rewritten is the invariant, not a violation of this one.
            bad = conn.execute(
                "SELECT l.memory_id, l.revision, l.confidence FROM ledger l"
                " JOIN (SELECT memory_id, MAX(revision) r FROM ledger GROUP BY memory_id) m"
                "   ON m.memory_id = l.memory_id AND m.r = l.revision"
                " WHERE l.owner LIKE 'agent:%' AND l.owner != 'agent:claude-code'"
                " AND l.confidence >= 1.0").fetchall()
        except _sq.Error:
            bad = []
        finally:
            conn.close()
        check("no CURRENT agent revision claims certainty", not bad, str(bad[:3]))


if __name__ == "__main__":
    print("agent — the half that must hold before a token is spent\n")
    for fn in (test_no_vendor_id_or_price_in_source, test_the_ceiling_is_not_a_field_in_the_request,
               test_chain_resolves_from_the_catalogue,
               test_attempts_are_capped_in_total, test_three_guardrails_in_credits,
               test_a_credential_fault_does_not_poison_the_chain,
               test_health_recovers_on_a_schedule, test_no_deltas_spends_nothing,
               test_degrades_without_credential, test_degrades_at_the_ceiling,
               test_a_broken_store_read_costs_one_project_not_the_run,
               test_prompt_forbids_restating_the_delta, test_agent_cannot_promote):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32magent ok\033[0m")
