#!/usr/bin/env python3
"""A key in the wrong variable, and the reason the tick cannot report it.

The case this file exists for: `OPENAI_API_KEY` holds an OpenRouter key — it
starts with `sk-or-`, which `agent/providers.py` already knows is impossible for
OpenAI — while `OPENROUTER_API_KEY` holds a correct key beside it. The likely
history is one value copied into both.

The system itself is unharmed: `_read_from` prefers the environment, sees the
wrong shape, IGNORES the variable and falls through to the key file. What is
wrong is the machine's configuration, and every OTHER consumer of
`$OPENAI_API_KEY` gets an OpenRouter key and a 401 that names nothing.

Why this cannot simply be a finding built by the tick: the variable is read only
by the EMBEDDING path (`EMBED_KEY_ENV`), and a scheduled tick does not inherit
the shell where the variable is set. A finding built from the tick's own
environment would answer "is the scheduler's environment misconfigured" — a
different question — and would answer "no" while the operator's shell is wrong.

So the detection happens where the fault is visible and the reporting where
findings are built. `observatory.py key` — an operator-run step, deliberately
NOT in the tick — writes a receipt naming the variable and the verdict, and
`build_findings` reads it. The receipt ages rather than asserting itself for
ever, because the process that could refute it may not run again for days.

**A receipt about keys must never carry a key.** That is the assertion this file
exists for most: a distinctive value is planted and its absence checked in the
receipt, in the finding, and in everything the report returns.
"""
from __future__ import annotations
import importlib, json, os, pathlib, sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir                                                # noqa: E402

FAILURES: list[str] = []

#: A value that could not occur by accident, so its absence is evidence.
PLANTED = ("sk-" "or-v1-" "PLANTEDSECRET" "mustNEVERreachAsurface" + "0" * 10)


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def providers_with(env: dict, state: pathlib.Path):
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    os.environ["OBSERVATORY_STATE"] = str(state)
    import paths
    importlib.reload(paths)
    from agent import providers as P
    return importlib.reload(P)


def restore() -> None:
    for k in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "OBSERVATORY_STATE"):
        os.environ.pop(k, None)
    import paths
    importlib.reload(paths)


def state_dir() -> pathlib.Path:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-keyshape-"))
    return d


# ─────────── the report ────────────────────────────────────────────────

def test_a_misplaced_key_is_named_and_its_value_is_not() -> None:
    """A key in the wrong variable is a routing bug wearing an auth bug's clothes.

    Trap: T19
    """
    d = state_dir()
    P = providers_with({"OPENAI_API_KEY": PLANTED, "OPENROUTER_API_KEY": None}, d)
    try:
        rows = P.shape_report()
        by = {r["env"]: r for r in rows}
        check("the misplaced variable has a row", "OPENAI_API_KEY" in by, str(by))
        r = by.get("OPENAI_API_KEY", {})
        check("its verdict is wrong", r.get("verdict") == "wrong", str(r))
        check("it names the provider the variable was meant for",
              r.get("provider") == "OpenAI", str(r))
        check("and gives a reason", len(str(r.get("reason", "")).split()) >= 8,
              str(r.get("reason")))
        blob = json.dumps(rows, ensure_ascii=False)
        check("THE VALUE IS NOWHERE IN THE REPORT", PLANTED not in blob, "")
        check("not even its tail",
              PLANTED[-12:] not in blob, "a masked suffix is still key material")
        check("only the identifying prefix appears",
              "sk-or-" in blob, "the verdict is unreadable without it")
    finally:
        restore()


def test_a_variable_this_process_cannot_see_gets_no_row() -> None:
    """A process speaks only about the environment it has. The tick has neither
    variable, and a row saying "absent" would read as "checked and fine"."""
    d = state_dir()
    P = providers_with({"OPENAI_API_KEY": None, "OPENROUTER_API_KEY": None}, d)
    try:
        check("no rows at all", P.shape_report() == [], str(P.shape_report()))
    finally:
        restore()


def test_a_correct_variable_is_reported_as_correct() -> None:
    d = state_dir()
    P = providers_with({"OPENROUTER_API_KEY": PLANTED, "OPENAI_API_KEY": None}, d)
    try:
        by = {r["env"]: r for r in P.shape_report()}
        check("the good variable has a row", "OPENROUTER_API_KEY" in by, str(by))
        check("and its verdict is ok",
              by.get("OPENROUTER_API_KEY", {}).get("verdict") == "ok", str(by))
        check("its value is not carried either",
              PLANTED not in json.dumps(by, ensure_ascii=False), "")
    finally:
        restore()


# ─────────── the receipt ───────────────────────────────────────────────

def test_the_receipt_merges_rather_than_replaces() -> None:
    """One process sees one variable, another sees the other. Replacing the file
    would make the second run erase the first run's true observation."""
    d = state_dir()
    P = providers_with({"OPENAI_API_KEY": PLANTED, "OPENROUTER_API_KEY": None}, d)
    try:
        P.write_shape_report()
    finally:
        restore()
    P = providers_with({"OPENROUTER_API_KEY": PLANTED, "OPENAI_API_KEY": None}, d)
    try:
        P.write_shape_report()
        doc = json.loads((d / "key-shapes.json").read_text(encoding="utf-8"))
    finally:
        restore()
    obs = doc.get("observations") or {}
    check("both variables are in the receipt", set(obs) ==
          {"OPENAI_API_KEY", "OPENROUTER_API_KEY"}, str(sorted(obs)))
    check("the first run's verdict survived",
          obs.get("OPENAI_API_KEY", {}).get("verdict") == "wrong", str(obs))
    check("each observation carries a stamp",
          all(o.get("seen_at") for o in obs.values()), str(obs))
    check("AND NO VALUE REACHED THE FILE",
          PLANTED not in (d / "key-shapes.json").read_text(encoding="utf-8"), "")


def test_a_fixed_variable_clears_its_own_verdict() -> None:
    d = state_dir()
    P = providers_with({"OPENAI_API_KEY": PLANTED}, d)
    try:
        P.write_shape_report()
    finally:
        restore()
    P = providers_with({"OPENAI_API_KEY": ("sk-" "proj-" "somethingthatlooksright" "foropenai")}, d)
    try:
        P.write_shape_report()
        doc = json.loads((d / "key-shapes.json").read_text(encoding="utf-8"))
    finally:
        restore()
    check("the verdict flipped to ok",
          doc["observations"]["OPENAI_API_KEY"]["verdict"] == "ok",
          json.dumps(doc["observations"]))


# ─────────── the finding ───────────────────────────────────────────────

def findings_for(observations: dict) -> list[dict]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-keyf-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "state").mkdir()
    for name, body in (("projects.json", '{"projects": []}'),
                       ("repositories.json", '{"repositories": []}'),
                       ("relations.json", '{"relations": []}')):
        (d / "registry" / name).write_text(body)
    (d / "state/key-shapes.json").write_text(json.dumps(
        {"written_at": "2026-09-07T12:00:00Z", "observations": observations}))
    os.environ.update(OBSERVATORY_REGISTRY=str(d / "registry"),
                      OBSERVATORY_SCRATCH=str(d / "scratch"),
                      OBSERVATORY_STATE=str(d / "state"),
                      OBSERVATORY_DB=str(d / "absent.db"))
    import paths
    importlib.reload(paths)
    import build_findings as B
    importlib.reload(B)
    try:
        return [f for f in B.collect() if f["type"].startswith("env.")]
    finally:
        for k in ("OBSERVATORY_REGISTRY", "OBSERVATORY_SCRATCH", "OBSERVATORY_STATE",
                  "OBSERVATORY_DB"):
            os.environ.pop(k, None)
        importlib.reload(paths)


def seen(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


WRONG = {"env": "OPENAI_API_KEY", "verdict": "wrong", "provider": "OpenAI",
         "expected_prefix": "sk-", "looks_like": "sk-or-",
         "reason": "it starts with 'sk-or-' — that is an OpenRouter key sitting in "
                   "OPENAI_API_KEY"}


def test_the_finding_names_the_variable_and_spares_the_system() -> None:
    got = findings_for({"OPENAI_API_KEY": {**WRONG, "seen_at": seen(0.1)}})
    check("it fires", len(got) == 1, str(got)[:220])
    if not got:
        return
    f = got[0]
    check("as a warning", f["severity"] == "warning", f["severity"])
    check("the subject is the environment, not a project",
          f["subject"] == "env:OPENAI_API_KEY", f["subject"])
    check("it names the variable", "OPENAI_API_KEY" in f["title"], f["title"])
    check("and says this system is unharmed",
          "falls through" in f["detail"] or "key file" in f["detail"],
          f["detail"][:240])
    # Case-insensitive: the detail capitalises OTHER for emphasis, and an
    # assertion that fails on the emphasis is testing the typography.
    check("while naming who is harmed",
          "other" in f["detail"].lower() and "401" in f["detail"],
          f["detail"][:300])
    check("the action does not ask for the value",
          "unset" in f["action"] or "export" in f["action"], f["action"])
    check("NO KEY MATERIAL IN THE FINDING",
          PLANTED not in json.dumps(f, ensure_ascii=False), "")


def test_a_correct_variable_raises_nothing() -> None:
    got = findings_for({"OPENROUTER_API_KEY": {
        "env": "OPENROUTER_API_KEY", "verdict": "ok", "provider": "OpenRouter",
        "seen_at": seen(0.1)}})
    check("a correct key is silent", got == [], str(got)[:200])


def test_an_absent_receipt_raises_nothing() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-keyf2-"))
    (d / "registry").mkdir(); (d / "scratch").mkdir(); (d / "state").mkdir()
    for name, body in (("projects.json", '{"projects": []}'),
                       ("repositories.json", '{"repositories": []}'),
                       ("relations.json", '{"relations": []}')):
        (d / "registry" / name).write_text(body)
    os.environ.update(OBSERVATORY_REGISTRY=str(d / "registry"),
                      OBSERVATORY_SCRATCH=str(d / "scratch"),
                      OBSERVATORY_STATE=str(d / "state"),
                      OBSERVATORY_DB=str(d / "absent.db"))
    import paths
    importlib.reload(paths)
    import build_findings as B
    importlib.reload(B)
    try:
        check("no receipt, no finding",
              [f for f in B.collect() if f["type"].startswith("env.")] == [],
              "the check has not been run; that is not the same as a clean result")
    finally:
        for k in ("OBSERVATORY_REGISTRY", "OBSERVATORY_SCRATCH", "OBSERVATORY_STATE",
                  "OBSERVATORY_DB"):
            os.environ.pop(k, None)
        importlib.reload(paths)


def test_an_old_observation_is_reported_as_old() -> None:
    """The process that could refute this may not run for days — the check is
    deliberately outside the tick. So the finding states its own age rather than
    asserting a fortnight-old reading as current."""
    got = findings_for({"OPENAI_API_KEY": {**WRONG, "seen_at": seen(30)}})
    check("it still fires", len(got) == 1, str(got)[:200])
    if got:
        check("and says how old the observation is",
              "30 day" in got[0]["detail"] or "days ago" in got[0]["detail"],
              got[0]["detail"][-200:])
        check("without claiming it is still true",
              "may already" in got[0]["detail"] or "when it was last" in got[0]["detail"],
              got[0]["detail"][-200:])


def test_a_fresh_observation_does_not_hedge() -> None:
    got = findings_for({"OPENAI_API_KEY": {**WRONG, "seen_at": seen(0.05)}})
    if got:
        check("a reading from today is stated plainly",
              "may already" not in got[0]["detail"], got[0]["detail"][-160:])


# ─────────── the receipt is not in the code tree ───────────────────────

def test_the_receipt_lives_in_the_selected_state_not_the_checkout() -> None:
    """Machine state, never program source.

    The historical checkout kept this receipt beside the code and relied on a
    gitignore line. The engine keeps every mutable file in the selected
    workspace state instead, so the equivalent guarantee is that the receipt
    resolves under `paths.STATE` and outside the installed code.
    """
    d = state_dir()
    P = providers_with({"OPENAI_API_KEY": None, "OPENROUTER_API_KEY": None}, d)
    try:
        where = P.write_shape_report([])
        check("the receipt is written under the selected state",
              where.resolve().parent == d.resolve(), str(where))
        check("and not inside the program tree",
              ROOT.resolve() not in where.resolve().parents, str(where))
    finally:
        restore()


if __name__ == "__main__":
    print("key shapes — a key in the wrong variable, and who can see it\n")
    for fn in (test_a_misplaced_key_is_named_and_its_value_is_not,
               test_a_variable_this_process_cannot_see_gets_no_row,
               test_a_correct_variable_is_reported_as_correct,
               test_the_receipt_merges_rather_than_replaces,
               test_a_fixed_variable_clears_its_own_verdict,
               test_the_finding_names_the_variable_and_spares_the_system,
               test_a_correct_variable_raises_nothing,
               test_an_absent_receipt_raises_nothing,
               test_an_old_observation_is_reported_as_old,
               test_a_fresh_observation_does_not_hedge,
               test_the_receipt_lives_in_the_selected_state_not_the_checkout):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe environment's fault is on a surface, and no key is\033[0m")
