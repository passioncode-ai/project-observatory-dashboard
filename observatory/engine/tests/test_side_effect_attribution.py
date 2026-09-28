#!/usr/bin/env python3
"""The published receipt could accuse the contract of a write the tick made.

`tools/run_probes.py` proves the side-effect ceiling of an `effect: none`
capability by reading `(registry_fingerprint(), ledger_max_revision())` before
the call and after it, and failing when they differ. The scheduled tick writes
the ledger AND commits the registry on its interval — so a tick landing inside
that window makes a read-only capability look like it wrote. It was observed:
the receipt suite failed minutes after a tick released its lease, and passed on
the next run with nothing changed.

**This one matters more than the trap it mirrors.** The same class was fixed in
the trap suite, where a wrong cause misleads whoever is reading the gate. Here
the artefact is the probe receipt — what a Fabric host reads to decide whether
to trust this agent — so a false `FAIL` on a side-effect ceiling is a lie about
the contract, published.

**The fix attributes rather than assumes, and says so.** On a difference the
runner reads a THIRD time. If the state is still moving with no call in between,
something other than the call is writing, and the verdict becomes an explicit
`INCONCLUSIVE` naming the tick — not a `PASS`, because nothing was proven, and
not a `FAIL`, because the call was not shown to have done it. If the state has
settled, the change coincided with the call and the `FAIL` stands as before.

**It is a heuristic and the note says which.** A tick write that both starts and
finishes between the second and third readings would still be attributed to the
call. That is strictly better than today, where every concurrent write is, and
the wording tells a reader what the verdict can and cannot mean.
"""
from __future__ import annotations
import json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()

FAILURES: list[str] = []


def concurrency_module():
    """The tick-clock discriminator, or None with the reason printed.

    It arrives with a parallel porting stream; until then these two cases
    cannot run, and saying so beats a traceback that looks like a defect.
    """
    try:
        import concurrency
    except ImportError:
        print("  SKIP  KNOWN-GAP: tests/concurrency.py is not in this distribution yet "
              "[gap: porting tests/concurrency.py closes it]")
        return None
    return concurrency


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def verdict(before, after, later):
    import run_probes
    fn = getattr(run_probes, "side_effect_verdict", None)
    if fn is None:
        return None
    return fn(before, after, later)


A, B, C = ("fp1", 10), ("fp1", 11), ("fp1", 12)


# ─────────── the three outcomes ────────────────────────────────────────

def test_nothing_moved_is_a_pass() -> None:
    got = verdict(A, A, A)
    if got is None:
        check("run_probes.side_effect_verdict exists", False,
              "the published receipt could blame the contract for the tick's write")
        return
    check("an unchanged estate passes", got["verdict"] == "PASS", str(got))
    check("and the note carries both readings", "before=" in got["note"], got["note"])


def test_a_settled_change_is_still_a_fail() -> None:
    """The property the check exists for must survive the fix. A call that wrote,
    after which the estate is quiet, is attributable to the call."""
    got = verdict(A, B, B)
    if got is None:
        return
    check("a change that settles is attributed to the call",
          got["verdict"] == "FAIL", str(got))


def test_a_still_moving_estate_is_inconclusive_not_a_failure() -> None:
    got = verdict(A, B, C)
    if got is None:
        return
    check("a state still moving is neither PASS nor FAIL",
          got["verdict"] not in ("PASS", "FAIL"), str(got))
    check("it is named INCONCLUSIVE", got["verdict"] == "INCONCLUSIVE", got["verdict"])
    check("and the note names the tick as the likely writer",
          "tick" in got["note"].lower(), got["note"])
    check("all three readings are in the note",
          got["note"].count("=") >= 3, got["note"])


def test_inconclusive_is_not_silently_a_pass() -> None:
    """The trap this could become: a verdict nobody counts as a failure quietly
    becomes a green. `coverage()` compares evaluated assertions against declared
    ones, so an INCONCLUSIVE row must still BE a row."""
    got = verdict(A, B, C)
    if got is None:
        return
    check("the assertion is still reported", bool(got.get("assertion")), str(got))
    check("with the declared wording, so coverage still matches",
          "unchanged after the call" in got["assertion"], got["assertion"])


# ─────────── the runner uses it ────────────────────────────────────────

def test_the_tick_clock_has_three_outcomes() -> None:
    """`tests/concurrency.py`, the fourth instance's discriminator. Unlike the
    probe runner's third reading this is EVIDENCE: the tick's log grows
    monotonically, so its size around a window says whether a tick wrote in it."""
    concurrency = concurrency_module()
    if concurrency is None:
        return
    check("a grown log means a tick ran", concurrency.tick_ran(10, 20) is True, "")
    check("an unchanged log means it did not", concurrency.tick_ran(10, 10) is False, "")
    for a_, b_ in ((None, 10), (10, None), (None, None)):
        check(f"an unreadable log is unknowable, not 'no tick' ({a_}, {b_})",
              concurrency.tick_ran(a_, b_) is None, "")


def test_attribution_never_excuses_silently() -> None:
    concurrency = concurrency_module()
    if concurrency is None:
        return
    guilty, why = concurrency.attribute(["store/raw/agent.json"], True, "test_plugins")
    check("a tick in the window clears the subject", guilty is False, why)
    check("and the reason names the tick", "tick" in why.lower(), why)
    check("and says the subject is not shown clean",
          "not shown to be" in why or "unattributed" in why, why)
    guilty, why = concurrency.attribute(["store/raw/agent.json"], None, "test_plugins")
    check("an unknowable window does not blame the subject", guilty is False, why)
    check("but says it is unattributed rather than excused",
          "unattributed" in why, why)
    guilty, why = concurrency.attribute(["store/raw/agent.json"], False, "test_plugins")
    check("no tick and a change is the subject's", guilty is True, why)
    check("and the reason names what changed", "agent.json" in why, why)
    check("nothing moved is nobody's fault",
          concurrency.attribute([], False, "x")[0] is False, "")


def test_the_runner_takes_a_third_reading() -> None:
    src = (ROOT / "tools/run_probes.py").read_text(encoding="utf-8")
    check("the runner calls the attributor",
          "side_effect_verdict" in src,
          "the inline `PASS if before == after else FAIL` cannot tell who wrote")
    check("and no bare two-reading verdict is left",
          '"PASS" if before == after else "FAIL"' not in src,
          "the old form attributes every concurrent write to the call")
    i = src.find("def side_effect_verdict")
    body = src[i:src.find("\ndef ", i + 10)] if i != -1 else ""
    check("the heuristic states its own limit",
          "heuristic" in body.lower() or "between the second" in body.lower(),
          "a check whose reader cannot tell what it proves is worse than none")


def test_the_live_receipt_still_records_a_verdict_for_this_assertion() -> None:
    """The receipt is written into the selected probe workspace; a workspace
    that never ran the probes has none."""
    import paths
    r = paths.STATE / "probe-receipts.json"
    if not r.is_file():
        print("  NOTE  no receipt in this workspace; `tools/run_probes.py "
              "--fixture-home` writes one [covered: the three-outcome cases above, "
              "and tests/test_conformance_receipt.py reads a real receipt]")
        return
    doc = json.loads(r.read_text(encoding="utf-8"))
    rows = [a for p in doc.get("probes", []) for a in p.get("assertions", [])
            if "unchanged after the call" in a.get("assertion", "")]
    check("the ceiling assertion is present in the receipt", bool(rows),
          str([p.get("probe") for p in doc.get("probes", [])]))
    if rows:
        check("and every verdict is one of the three",
              all(a["verdict"] in ("PASS", "FAIL", "INCONCLUSIVE") for a in rows),
              str(sorted({a["verdict"] for a in rows})))


if __name__ == "__main__":
    print("side-effect attribution — who wrote, not merely that something did\n")
    for fn in (test_nothing_moved_is_a_pass,
               test_a_settled_change_is_still_a_fail,
               test_a_still_moving_estate_is_inconclusive_not_a_failure,
               test_inconclusive_is_not_silently_a_pass,
               test_the_tick_clock_has_three_outcomes,
               test_attribution_never_excuses_silently,
               test_the_runner_takes_a_third_reading,
               test_the_live_receipt_still_records_a_verdict_for_this_assertion):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma concurrent write is no longer published as the contract's\033[0m")
