#!/usr/bin/env python3
"""A ceiling must govern what the system controls, and this one governed a key.

Driving `observatory_search` through the wire once returned a degradation
nobody had asked about:

    spend guardrail reached — daily ceiling: 104.3710 of 2.00 credits

The wallet said otherwise. Measured minutes later (figures kept as the fixture):

    key spent today          104.80 credits   (the provider's own counters)
    this project's journal      0.000001
    daily ceiling               2.00          -> tripped
    key spent this month      121.84
    this project's journal      0.0924
    monthly ceiling            80.00          -> tripped
    key limit / remaining     300 / 178.16

So the agent, the indexer and the semantic half of the search were **all
disabled by a neighbour's spending**, with 178 credits available and this system
having spent nine hundredths of one credit all month. The provider key is
shared with everything else on the machine that uses the same provider — a
memory companion's observer among them — and the caps were read from the
provider's counters, which measure the KEY.

`check_budget`'s own docstring had already made the argument, for embeddings:
"another consumer of the shared OpenRouter key could stop this estate's
indexing… A guard whose verdict depends on the reachability of an unrelated
service is not a guard." It applies unchanged to the agent, which is what this
closes.

**What replaces it, and what does not change.** This system's daily and monthly
ceilings are measured against its own journal, for every provider alike — the
journal is complete for this system, since every call goes through `charge()`.
The key's own remaining limit is still checked FIRST and still stops everything,
because it is enforced by the provider, cannot drift, and at zero nothing can be
bought anyway. That is where the concern about a drifting local journal belongs:
a journal that under-counts by one dead call cannot overspend past a limit the
provider itself enforces.

**And the fact becomes a finding rather than a silence.** A shared key being
spent by something else is the operator's decision, so `wallet.shared_key` hands
it over with both numbers — `info` normally, `warning` when the key is nearly
spent, since at zero a neighbour's spending really does become this system's
problem.
"""
from __future__ import annotations
import importlib, json, os, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir                                                # noqa: E402
# A private synthetic workspace (registry, store and models configuration) owns
# every read and write below; nothing on the machine running the suite is used.
from test_portable_mcp import setup as portable_setup               # noqa: E402
portable_setup()
import paths as _paths  # noqa: E402
# NO REQUEST MAY LEAVE THE MACHINE. The synthetic workspace's provider endpoints
# point at a closed loopback port, so a call a stub failed to intercept fails
# fast and locally instead of reaching a real provider.
_models = _paths.CONFIG / "models.json"
_mdoc = json.loads(_models.read_text(encoding="utf-8"))
_mdoc["base_url"] = "http://127.0.0.1:9/api/v1"
_mdoc.setdefault("embedding", {})["base_url"] = "http://127.0.0.1:9/v1"
_models.write_text(json.dumps(_mdoc), encoding="utf-8")

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


#: The ceilings this file plants. NOT the operator's, and that distinction cost
#: a gate run: the spends below were written against `daily_ceiling: 2.0`, so
#: raising the configured budget made a 3.0 fixture stop tripping a cap and four
#: assertions went red without a line of logic changing.
#: A test whose subject is the operator's configuration measures the
#: configuration — the same confusion this file was written to end, one field
#: along.
FIXTURE_CEILINGS = {"daily_ceiling": 2.0, "monthly_ceiling": 0.8,
                    "velocity_ceiling": 0.5}


def providers_with(day: float, month: float, key: dict | None):
    """The provider boundary against a planted journal, key AND ceilings.

    The key figures are injected rather than fetched: a test that asks the real
    account what it has spent measures the account, and would pass or fail on a
    neighbour's behaviour — the exact confusion under audit here.
    """
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-budget-"))
    # `OBSERVATORY_STATE` IS THE ONE THAT MATTERS HERE, and its absence is what
    # made the first version of this file destructive: `WALLET` derived from
    # `paths.STORE`, which is deliberately not redirectable, so writing a
    # fixture journal wrote over the operator's own and truncated its spend
    # events. The totals were restorable from a measurement taken minutes
    # earlier; the events were not.
    os.environ["OBSERVATORY_DB"] = str(d / "observatory.db")
    os.environ["OBSERVATORY_SCRATCH"] = str(d / "scratch")
    os.environ["OBSERVATORY_STATE"] = str(d / "state")
    (d / "scratch").mkdir()
    (d / "state").mkdir()
    import paths
    importlib.reload(paths)
    from agent import providers as P
    importlib.reload(P)
    P.WALLET.parent.mkdir(parents=True, exist_ok=True)
    today = P.now().strftime("%Y-%m-%d")
    P.WALLET.write_text(json.dumps({
        "denomination": "credits", "events": [],
        "days": {today: day}, "months": {today[:7]: month}}), encoding="utf-8")
    P._provider_usage_cache = key           # None means "the provider is unreachable"
    P.provider_usage = lambda force=False: key
    # THE CEILINGS TOO. `check_budget` reads them from the workspace's
    # `config/models.json`, which is the operator's budget; pinning them here
    # makes the spends below mean what they say whatever that file holds.
    _live = P.config
    P.config = lambda: {**_live(), "wallet": {**_live()["wallet"], **FIXTURE_CEILINGS}}
    return P


def restore() -> None:
    for k in ("OBSERVATORY_DB", "OBSERVATORY_SCRATCH", "OBSERVATORY_STATE"):
        os.environ.pop(k, None)
    import paths
    importlib.reload(paths)


KEY_BUSY = {"daily": 104.80, "weekly": 104.80, "monthly": 121.84, "total": 121.84,
            "limit": 300, "limit_remaining": 178.16, "limit_reset": "monthly"}
KEY_SPENT = {**KEY_BUSY, "limit_remaining": 0.0, "total": 300.0, "monthly": 300.0}


# ─────────── the ceiling measures this system ──────────────────────────

def test_a_neighbours_spending_no_longer_stops_this_system() -> None:
    """THE MEASURED DEFECT, with the live numbers."""
    P = providers_with(day=0.000001, month=0.0924, key=KEY_BUSY)
    try:
        verdict = P.check_budget("openrouter")
        check("spending is permitted", verdict is None, str(verdict))
        s = P.wallet_state()
        check("this project's own figures are reported",
              s["local_today"] == 1e-06 and s["local_month"] == 0.0924, str(s)[:200])
        check("and the key's beside them, as a different subject",
              s.get("key_today") == 104.8 and s.get("key_month") == 121.84,
              f"{s.get('key_today')} / {s.get('key_month')}")
    finally:
        restore()


def test_this_systems_own_overspend_still_stops_it() -> None:
    P = providers_with(day=3.0, month=1.0, key=KEY_BUSY)
    try:
        verdict = P.check_budget("openrouter") or ""
        check("the daily cap trips on this project's own spend",
              "daily ceiling" in verdict, verdict)
        check("the message says whose spend decided",
              "by THIS project" in verdict, verdict)
        check("and shows the key's figure as context",
              "another consumer" in verdict, verdict)
    finally:
        restore()
    P = providers_with(day=0.0, month=90.0, key=KEY_BUSY)
    try:
        verdict = P.check_budget("openrouter") or ""
        check("the monthly cap trips on this project's own spend",
              "monthly ceiling" in verdict, verdict)
        check("and it too names the subject", "by THIS project" in verdict, verdict)
    finally:
        restore()


def test_a_spent_key_still_stops_everything() -> None:
    """The outer backstop, and where the journal-drift concern belongs: it is
    enforced by the provider, so a journal that under-counts cannot get past
    it.

    Trap: T17
    """
    P = providers_with(day=0.0, month=0.0, key=KEY_SPENT)
    try:
        verdict = P.check_budget("openrouter") or ""
        check("a spent key stops spending", "on this KEY is spent" in verdict,
              verdict)
        check("and it is checked before this project's own caps",
              verdict.startswith("the limit on this KEY"), verdict)
        # THE SAME THREE FACTS AS THE CEILING MESSAGES. This branch said only the
        # key's number until it became the live one for the first time and read
        # as though this system had spent 300 credits — its own journal held
        # 0.1185. Naming whose spend decided exists to prevent exactly that
        # reading, and the terser path had never been walked.
        check("and it names this project's own figure",
              "this project's own journal holds" in verdict, verdict)
        check("and says the rest was somebody else",
              "another consumer of the same key" in verdict, verdict)
    finally:
        restore()


def test_the_verdict_no_longer_depends_on_the_provider_being_reachable() -> None:
    """The property `check_budget`'s docstring demanded and did not have: the
    same call permitted spending inside `./observatory.py check` and refused it
    interactively, because the provider was unreachable in one and not the
    other."""
    for label, key in (("unreachable", None), ("reachable", KEY_BUSY)):
        P = providers_with(day=0.000001, month=0.0924, key=key)
        try:
            check(f"permitted with the provider {label}",
                  P.check_budget("openrouter") is None, str(P.check_budget("openrouter")))
        finally:
            restore()
    for label, key in (("unreachable", None), ("reachable", KEY_BUSY)):
        P = providers_with(day=5.0, month=1.0, key=key)
        try:
            v = P.check_budget("openrouter") or ""
            check(f"refused with the provider {label}", "daily ceiling" in v, v)
        finally:
            restore()


def test_velocity_is_still_local_and_still_fires() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-vel-"))
    os.environ["OBSERVATORY_DB"] = str(d / "observatory.db")
    os.environ["OBSERVATORY_STATE"] = str(d / "state")
    (d / "state").mkdir()
    import paths
    importlib.reload(paths)
    from agent import providers as P
    importlib.reload(P)
    P._provider_usage_cache = None
    P.provider_usage = lambda force=False: None
    # The fixture's ceilings, not the workspace's: 0.8 in the window must trip a
    # 0.5 velocity ceiling whatever the configured budget is.
    _live = P.config
    P.config = lambda: {**_live(), "wallet": {**_live()["wallet"], **FIXTURE_CEILINGS}}
    now = P.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    P.WALLET.parent.mkdir(parents=True, exist_ok=True)
    P.WALLET.write_text(json.dumps({
        "denomination": "credits",
        "events": [{"at": now, "cost": 0.4}, {"at": now, "cost": 0.4}],
        "days": {}, "months": {}}), encoding="utf-8")
    try:
        v = P.check_budget("openrouter") or ""
        check("a rolling window still catches a loop", "spend velocity" in v, v)
        check("and says so in those words", "something is looping" in v, v)
    finally:
        restore()


# ─────────── the key's state is recorded and read offline ──────────────

def test_the_key_snapshot_is_written_where_a_findings_build_can_read_it() -> None:
    P = providers_with(day=0.0, month=0.0, key=None)
    try:
        P.record_key_usage(KEY_BUSY)
        doc = json.loads(P.KEY_USAGE.read_text(encoding="utf-8"))
        check("the snapshot exists", bool(doc), str(doc)[:120])
        check("with a stamp, so its age is visible", bool(doc.get("checked_at")),
              str(doc)[:120])
        check("and a note naming whose spend it is",
              "shared with every other tool" in (doc.get("note") or ""),
              (doc.get("note") or "")[:80])
        check("it is a separate file from the models' health",
              P.KEY_USAGE != P.HEALTH,
              "provider-health.json is keyed by model id and read by that key")
    finally:
        restore()


def test_the_shared_key_is_a_finding_rather_than_a_stop() -> None:
    """The key's own limit is reported, not used as this system's cap.

    Trap: T17
    """
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-sharedkey-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "store").mkdir()
    (d / "registry/projects.json").write_text('{"projects": []}')
    month = __import__("datetime").date.today().isoformat()[:7]
    (d / "store/key-usage.json").write_text(json.dumps({
        "checked_at": "2026-09-07T10:43:00Z", "daily": 104.8, "monthly": 121.84,
        "total": 121.84, "limit": 300, "limit_remaining": 178.16,
        "limit_reset": "monthly", "note": "x"}))
    (d / "store/wallet.json").write_text(json.dumps({
        "denomination": "credits", "events": [], "days": {},
        "months": {month: 0.0924}}))
    os.environ.update(OBSERVATORY_REGISTRY=str(d / "registry"),
                      OBSERVATORY_SCRATCH=str(d / "scratch"),
                      OBSERVATORY_DB=str(d / "absent.db"))
    import paths
    importlib.reload(paths)
    # The finding is driven against the fixture's store by pointing the module's
    # own constants, which is what a test may redirect.
    import build_findings as B
    importlib.reload(B)
    # BOTH, because the writer and the reader now agree on `STATE` and this
    # fixture predates that. `agent/providers.py` has always written the wallet,
    # the key usage and the provider health under `paths.STATE`;
    # `tools/build_findings.py` once read two of them from `paths.STORE`. They
    # resolve to one directory by default, so setting only STORE worked — and
    # the moment the reader was corrected this fixture read the configured key
    # usage and reported `critical` where it had planted room.
    B.paths.STORE = d / "store"
    B.paths.STATE = d / "store"
    try:
        got = [f for f in B.collect() if f["type"] == "wallet.shared_key"]
        check("the shared key raises a finding", len(got) == 1, str(got)[:200])
        if got:
            check("it is info while the key has room",
                  got[0]["severity"] == "info", got[0]["severity"])
            check("and names both figures",
                  "121.8" in got[0]["title"] and "0.09" in got[0]["title"],
                  got[0]["title"])
            check("and says the ceilings no longer depend on it",
                  "no longer disables it" in got[0]["detail"],
                  got[0]["detail"][-120:])
        (d / "store/key-usage.json").write_text(json.dumps({
            "checked_at": "2026-09-07T10:43:00Z", "daily": 104.8, "monthly": 290.0,
            "total": 290.0, "limit": 300, "limit_remaining": 10.0,
            "limit_reset": "monthly", "note": "x"}))
        got = [f for f in B.collect() if f["type"] == "wallet.shared_key"]
        check("a nearly spent key is a warning",
              got and got[0]["severity"] == "warning",
              str(got)[:160])
        if got:
            check("because at zero this system stops with it",
                  "stops with it" in got[0]["detail"], got[0]["detail"][:200])
        # AND AT ZERO IT IS CRITICAL, which this suite did not cover until the
        # key actually ran out: 300.0538 of 300 spent, of which this project's
        # journal held 0.1185 — the agent and the indexer
        # stopped, and the finding stayed a warning while its own detail said
        # "at zero this system stops with it". A prediction that does not change
        # grade when it comes true is a prediction nobody has to act on.
        (d / "store/key-usage.json").write_text(json.dumps({
            "checked_at": "2026-09-07T22:40:00Z", "daily": 300.05, "monthly": 300.05,
            "total": 300.05, "limit": 300, "limit_remaining": 0.0,
            "limit_reset": "monthly", "note": "x"}))
        got = [f for f in B.collect() if f["type"] == "wallet.shared_key"]
        check("a SPENT key is critical, not a warning",
              got and got[0]["severity"] == "critical", str(got)[:160])
        if got:
            check("and the detail says what has stopped",
                  "остановил" in got[0]["detail"] or "halted" in got[0]["detail"]
                  or "stopped" in got[0]["detail"], got[0]["detail"][:220])
            check("naming the two steps that spend",
                  "agent" in got[0]["detail"] and "index" in got[0]["detail"],
                  got[0]["detail"][:220])
    finally:
        for k in ("OBSERVATORY_REGISTRY", "OBSERVATORY_SCRATCH", "OBSERVATORY_DB"):
            os.environ.pop(k, None)
        importlib.reload(paths)


def test_the_findings_build_makes_no_network_call_for_this() -> None:
    import source_reader
    src = source_reader.code_keeping_strings(
        (ROOT / "tools/build_findings.py").read_text(encoding="utf-8"))
    for forbidden in ("provider_usage", "urllib", "requests."):
        check(f"the findings build does not call `{forbidden}`",
              forbidden not in src,
              "a findings build that depends on an unrelated service produces a "
              "different set of findings depending on the weather")


def test_a_key_without_a_limit_is_not_a_spent_key() -> None:
    """OpenRouter answers `limit: null, limit_remaining: null` for a key that has
    no limit at all. `remaining = limit_remaining or 0.0` read that as a key with
    nothing left, and every call — a newcomer's first question included — was
    refused as "the limit on this KEY is spent: 0.0000 of None"."""
    unlimited = {**KEY_BUSY, "limit": None, "limit_remaining": None, "limit_reset": None}
    P = providers_with(day=0.0, month=0.0, key=unlimited)
    try:
        verdict = P.check_budget("openrouter")
        check("an unlimited key permits spending", verdict is None, str(verdict))
        check("and its remaining limit is unknown, not zero",
              P.wallet_state().get("key_remaining") is None, str(P.wallet_state().get("key_remaining")))
    finally:
        restore()


if __name__ == "__main__":
    print("the budget's subject — a ceiling that governs what the system controls\n")
    for fn in (test_a_neighbours_spending_no_longer_stops_this_system,
               test_this_systems_own_overspend_still_stops_it,
               test_a_spent_key_still_stops_everything,
               test_a_key_without_a_limit_is_not_a_spent_key,
               test_the_verdict_no_longer_depends_on_the_provider_being_reachable,
               test_velocity_is_still_local_and_still_fires,
               test_the_key_snapshot_is_written_where_a_findings_build_can_read_it,
               test_the_shared_key_is_a_finding_rather_than_a_stop,
               test_the_findings_build_makes_no_network_call_for_this):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma ceiling on this system's own spend, and a shared key handed over "
          "as a decision\033[0m")
