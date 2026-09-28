#!/usr/bin/env python3
"""The tick stood down three times running and every surface looked normal.

As once seen in the tick's own log:

    15:57:42  lost `registry` — held by r-ga32562obser. Standing down.
    16:27:43  … held by r-ga36834obser. Standing down.
    16:57:44  … held by r-ga12716obser. Standing down.
    17:27:52  holding `registry` as observatory-tick

**Ninety minutes with no tick.** The registry, the findings and the dashboard are
only as fresh as the last completed cycle, and nothing said a word: the message
went to the tick's log, which nothing reads on a schedule.

**And the holder was the gate.** `tools/tick_lease.py` builds a lease identity
as `role.rsplit("-", 1)[-1][:2] + pid + "-" + role`, so `observatory-gate`
becomes `ga<pid>-observatory-gate` and truncates to `r-ga…obser`. Those were
`./observatory.py check` runs — the gate takes the registry lease for its whole
duration, deliberately, so the tick cannot rewrite the registry mid-read. A
development cadence starved the estate's own schedule.

Standing down is right — "a skipped tick is cheaper than two writers" — so the
fix is not to change the lease. It is to make the GAP visible: a receipt on every
path of `acquire()`, a count of consecutive skips, and a finding once the estate
has missed more than one cycle.
"""
from __future__ import annotations
import importlib, json, os, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir                                                # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


# ─────────── the identity that answered the question ───────────────────

def test_the_gate_and_the_tick_are_legible_in_the_journal() -> None:
    """`r-ga…` is the gate, as an assertion. Without this, "who was holding
    this when the tick stood down" has no answer."""
    import tick_lease
    importlib.reload(tick_lease)
    gate = tick_lease.run_identity(tick_lease.GATE_IDENTITY)
    tick = tick_lease.run_identity(tick_lease.TICK_IDENTITY)
    check("the gate's identity starts `ga`", gate.startswith("ga"), gate)
    check("the tick's starts `ti`", tick.startswith("ti"), tick)
    check("and they differ inside the eleven characters agent-sync locks on",
          gate.replace("-", "")[:11] != tick.replace("-", "")[:11],
          f"{gate} / {tick}")


# ─────────── a receipt on every path ───────────────────────────────────

def scratch() -> tuple[pathlib.Path, dict]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-standdown-"))
    (d / "scratch").mkdir()
    return d, dict(os.environ, OBSERVATORY_SCRATCH=str(d / "scratch"))


def receipt_of(d: pathlib.Path) -> dict:
    f = d / "scratch/tick-lease.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


def record(d: pathlib.Path, env: dict, outcome: str, holder: str = "",
           reason: str = "") -> dict:
    os.environ["OBSERVATORY_SCRATCH"] = env["OBSERVATORY_SCRATCH"]
    import paths
    importlib.reload(paths)
    import tick_lease
    importlib.reload(tick_lease)
    try:
        tick_lease.record_outcome(outcome, holder=holder, reason=reason)
        return receipt_of(d)
    finally:
        os.environ.pop("OBSERVATORY_SCRATCH", None)
        importlib.reload(paths)


def test_a_stand_down_is_counted_and_an_acquire_resets_it() -> None:
    d, env = scratch()
    r = record(d, env, "stood-down", holder="r-ga32562obser",
               reason="another writer has the registry")
    check("the first stand-down is counted", r.get("consecutive_skips") == 1,
          json.dumps(r))
    check("and the holder is kept", r.get("holder") == "r-ga32562obser", json.dumps(r))
    r = record(d, env, "stood-down", holder="r-ga36834obser")
    r = record(d, env, "stood-down", holder="r-ga12716obser")
    check("three in a row count three", r.get("consecutive_skips") == 3, json.dumps(r))
    check("the newest holder wins", r.get("holder") == "r-ga12716obser", json.dumps(r))
    r = record(d, env, "acquired")
    check("an acquire resets the count", r.get("consecutive_skips") == 0, json.dumps(r))
    check("and stamps when the tick last ran", bool(r.get("last_acquired_at")),
          json.dumps(r))
    r = record(d, env, "stood-down", holder="r-x")
    check("the last-acquired stamp SURVIVES a later skip",
          bool(r.get("last_acquired_at")),
          "it is what says how stale the estate is, and a skip must not erase it")


def test_an_unguarded_run_is_not_a_skip() -> None:
    """agent-sync absent means the tick PROCEEDS. Counting that as a skip would
    report a gap that did not happen."""
    d, env = scratch()
    r = record(d, env, "unguarded")
    check("it is not counted as a skip", r.get("consecutive_skips") == 0,
          json.dumps(r))
    check("but the outcome is recorded", r.get("outcome") == "unguarded",
          json.dumps(r))


def test_the_receipt_is_written_on_every_path_of_acquire() -> None:
    import source_reader
    src = source_reader.code_only((ROOT / "tools/tick_lease.py").read_text(encoding="utf-8"))
    body = src.split("def acquire(")[1].split("\ndef ")[0]
    returns = body.count("return ")
    calls = body.count("record_outcome(")
    check("every return from acquire() is preceded by a receipt",
          calls >= returns, f"{calls} receipt(s) for {returns} return(s)")


# ─────────── the finding ───────────────────────────────────────────────

def findings_for(receipt: dict | None) -> list[dict]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-sdf-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    for name, body in (("projects.json", '{"projects": []}'),
                       ("repositories.json", '{"repositories": []}'),
                       ("relations.json", '{"relations": []}')):
        (d / "registry" / name).write_text(body)
    if receipt is not None:
        (d / "scratch/tick-lease.json").write_text(json.dumps(receipt))
    os.environ.update(OBSERVATORY_REGISTRY=str(d / "registry"),
                      OBSERVATORY_SCRATCH=str(d / "scratch"),
                      OBSERVATORY_DB=str(d / "absent.db"))
    import paths
    importlib.reload(paths)
    import build_findings as B
    importlib.reload(B)
    try:
        return [f for f in B.collect() if f["type"].startswith("tick.")]
    finally:
        for k in ("OBSERVATORY_REGISTRY", "OBSERVATORY_SCRATCH", "OBSERVATORY_DB"):
            os.environ.pop(k, None)
        importlib.reload(paths)


def skips(n: int, holder: str = "r-ga32562obser", last: str = "2026-09-07T14:57:00Z") -> dict:
    return {"at": "2026-09-07T16:57:44Z", "outcome": "stood-down" if n else "acquired",
            "holder": holder, "reason": "another writer has the registry",
            "consecutive_skips": n, "last_acquired_at": last}


def test_one_skip_is_routine() -> None:
    check("a single stand-down raises nothing", findings_for(skips(1)) == [],
          "the next tick is thirty minutes away, and one gap is the design working")


def test_two_in_a_row_are_reported() -> None:
    got = findings_for(skips(2))
    check("it fires", len(got) == 1, str(got)[:220])
    if not got:
        return
    f = got[0]
    check("as a warning", f["severity"] == "warning", f["severity"])
    check("counting the missed cycles", "2" in f["title"], f["title"])
    check("naming who held the registry", "r-ga32562obser" in f["detail"],
          f["detail"][:240])
    check("and saying how stale the estate is",
          "old" in f["detail"] or "since" in f["detail"], f["detail"][:240])


def test_the_gate_is_named_as_the_holder_it_is() -> None:
    """`r-ga…` is a `./observatory.py check` run, and the operator can act on
    that: it is their own gate starving their own schedule."""
    got = findings_for(skips(3, holder="r-ga12716obser"))
    if not got:
        check("a finding to inspect", False, "nothing fired")
        return
    d = got[0]["detail"] + " " + got[0]["action"]
    check("the gate is identified rather than left as an opaque id",
          "gate" in d.lower(), d[:260])
    check("and the action does not tell the operator to weaken the lease",
          "release" not in got[0]["action"].lower(),
          "standing down is cheaper than two writers — the gap is the thing to see")


def test_a_foreign_holder_is_not_called_the_gate() -> None:
    got = findings_for(skips(2, holder="r-somebodyelse"))
    if got:
        check("an unknown holder is reported as itself",
              "r-somebodyelse" in got[0]["detail"], got[0]["detail"][:200])
        check("and not guessed to be the gate",
              "gate" not in got[0]["detail"].lower()
              or "r-somebodyelse" in got[0]["detail"], "")


def test_an_absent_receipt_raises_nothing() -> None:
    check("no receipt, no finding", findings_for(None) == [],
          "the tick has not run since this was added; that is not a skip")


def test_an_acquired_tick_is_silent() -> None:
    check("a healthy tick says nothing", findings_for(skips(0)) == [], "")


# ─────────── the receipt is a foreign write to the gate ────────────────

def test_the_receipt_is_declared_a_foreign_write() -> None:
    """The tick writes it, and the tick lands mid-gate — that is the whole
    subject here. Without the allowance the purity check would report the
    group as having written it."""
    src = (ROOT / "observatory.py").read_text(encoding="utf-8")
    block = src.split("FOREIGN_WRITES_IGNORED = {")[1].split("}")[0]
    check("store/raw/tick-lease.json is ignored as a foreign write",
          "tick-lease.json" in block, block[:300])


if __name__ == "__main__":
    print("the tick's stand-down — ninety minutes nobody was told about\n")
    for fn in (test_the_gate_and_the_tick_are_legible_in_the_journal,
               test_a_stand_down_is_counted_and_an_acquire_resets_it,
               test_an_unguarded_run_is_not_a_skip,
               test_the_receipt_is_written_on_every_path_of_acquire,
               test_one_skip_is_routine,
               test_two_in_a_row_are_reported,
               test_the_gate_is_named_as_the_holder_it_is,
               test_a_foreign_holder_is_not_called_the_gate,
               test_an_absent_receipt_raises_nothing,
               test_an_acquired_tick_is_silent,
               test_the_receipt_is_declared_a_foreign_write):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma missed cycle is on a surface, not only in a log\033[0m")
