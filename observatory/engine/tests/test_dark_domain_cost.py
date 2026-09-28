#!/usr/bin/env python3
""""Let it lapse deliberately" — for domains that renew themselves.

When this was measured, every `domain.dark` domain carried
`namecheap.auto_renew: true`. The finding said

    It is being paid for and serves nothing.
    → point it somewhere, or let it lapse deliberately

and the second half read as the easy option: do nothing and it goes away. With
auto-renew on, doing nothing RENEWS it — for another year, at cost, still serving
nothing. The action offered as effortless the one outcome that cannot happen
without an act, and the act it needs was not named.

**And for some of them the estate argued with itself.** A dark domain inside
the expiry horizon also raises `domain.expiring`:

    Auto-renew is ON, so this is a date to know rather than to act on
    → confirm the card on file is current

About one domain the operator was told to make sure it renews, and to consider
letting it lapse. "A date to know rather than to act on" is right for a domain
that serves something and wrong for one that serves nothing: there, the
auto-renewal IS the thing to act on. A remedy undermining the finding beside it,
and the fix is to keep both, link them, and put the deadline on the decision
that needs it.

**One premise refuted along the way.** A domain with no RDAP expiry looked like
an ungrounded cost claim. It is not: `domains.json` records
`namecheap.expires_on`, which is the source `domain.expiring` already reads. The
claim rests on a source the dark finding simply did not cite.
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


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def in_days(n: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=n)).strftime("%Y-%m-%d")


def estate(*, resolves: bool | None, expires: str | None, auto_renew,
           rdap_expires: str | None = "same") -> list[dict]:
    """Findings for one owned domain, with both expiry sources controllable."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-dark-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    for name, body in (("projects.json", '{"projects": []}'),
                       ("repositories.json", '{"repositories": []}'),
                       ("relations.json", '{"relations": []}')):
        (d / "registry" / name).write_text(body)
    nc: dict = {"status": "active"}
    if expires is not None:
        nc["expires_on"] = expires
    if auto_renew is not None:
        nc["auto_renew"] = auto_renew
    (d / "registry/domains.json").write_text(json.dumps({"domains": [
        {"id": "domain:x.test", "name": "x.test", "ownership": "owned",
         "registrar": "namecheap", "namecheap": nc}]}))
    measured = {"about": "x.test", "statuses": [],
                "expires_on": (expires if rdap_expires == "same" else rdap_expires)}
    (d / "registry/domain-liveness.json").write_text(json.dumps({
        "schema_version": 1, "scanned_on": "2026-09-07",
        "hosts": [{"host": "x.test", "resolves": resolves, "status": None,
                   "http": None, "checked_on": "2026-09-07", "measured": measured}],
        "verification": {"agreed": 0, "disagreed": 0, "unverifiable": 0,
                         "disagreements": []},
        "degraded": []}))
    os.environ.update(OBSERVATORY_REGISTRY=str(d / "registry"),
                      OBSERVATORY_SCRATCH=str(d / "scratch"),
                      OBSERVATORY_DB=str(d / "absent.db"))
    import paths
    importlib.reload(paths)
    import build_findings as B
    importlib.reload(B)
    try:
        return [f for f in B.collect() if f["type"].startswith("domain.")]
    finally:
        for k in ("OBSERVATORY_REGISTRY", "OBSERVATORY_SCRATCH", "OBSERVATORY_DB"):
            os.environ.pop(k, None)
        importlib.reload(paths)


def one(got: list[dict], kind: str) -> dict | None:
    rows = [f for f in got if f["type"] == kind]
    return rows[0] if len(rows) == 1 else None


# ─────────── the decision gets its deadline and its real act ───────────

def test_a_dark_domain_that_renews_itself_says_so() -> None:
    got = estate(resolves=False, expires=in_days(400), auto_renew=True)
    f = one(got, "domain.dark")
    check("the dark finding fires", f is not None, str(got)[:220])
    if f is None:
        return
    check("it names the renewal date", in_days(400) in f["detail"], f["detail"])
    check("and says the renewal is automatic",
          "auto-renew" in f["detail"].lower() and "on" in f["detail"].lower(),
          f["detail"])
    check("the action names the act lapsing REQUIRES",
          "auto-renew" in f["action"].lower(), f["action"])
    check("and no longer offers lapsing as inaction",
          "lapse deliberately" not in f["action"],
          "with auto-renew on, doing nothing renews it")


def test_a_dark_domain_that_does_not_renew_is_told_plainly() -> None:
    """The other half: with auto-renew OFF, lapsing IS what inaction does, and
    the finding must not send the operator to turn off a switch already off."""
    got = estate(resolves=False, expires=in_days(400), auto_renew=False)
    f = one(got, "domain.dark")
    check("it fires", f is not None, str(got)[:200])
    if f is None:
        return
    check("it says nothing will renew it",
          "nothing will renew" in f["detail"] or "will lapse" in f["detail"],
          f["detail"])
    check("and the action does not ask for auto-renew to be turned off",
          "turn auto-renew off" not in f["action"].lower(), f["action"])


def test_an_unmeasured_renewal_is_not_asserted_as_paid() -> None:
    """Three outcomes. With no registrar record the finding must not claim a
    cost it did not measure — the three-outcome rule drawn for `resolves`."""
    got = estate(resolves=False, expires=None, auto_renew=None)
    f = one(got, "domain.dark")
    check("it still fires — the domain is dark either way", f is not None,
          str(got)[:200])
    if f is None:
        return
    check("but the renewal is reported as unmeasured",
          "not recorded" in f["detail"] or "could not" in f["detail"], f["detail"])
    check("and no date is invented", "None" not in f["detail"], f["detail"])


def test_the_rdap_gap_does_not_hide_the_registrar_record() -> None:
    """RDAP silent, registrar record present. The
    cost claim IS grounded, and citing only the liveness file was what made it
    look otherwise."""
    got = estate(resolves=False, expires=in_days(300), auto_renew=True,
                 rdap_expires=None)
    f = one(got, "domain.dark")
    check("the registrar's date is used", f is not None and in_days(300) in f["detail"],
          (f or {}).get("detail", str(got)[:200]))
    if f:
        check("and the evidence cites the file it came from",
              any("domains.json" in e for e in f["evidence"]), str(f["evidence"]))


# ─────────── the two findings stop arguing ─────────────────────────────

def test_a_dark_and_expiring_domain_is_not_told_to_confirm_the_card() -> None:
    got = estate(resolves=False, expires=in_days(60), auto_renew=True)
    dark, exp = one(got, "domain.dark"), one(got, "domain.expiring")
    check("both findings fire", dark is not None and exp is not None,
          str([f["type"] for f in got]))
    if exp is None:
        return
    check("the expiry is NOT called a date to know rather than act on",
          "rather than to act on" not in exp["detail"], exp["detail"])
    check("it says the renewal will pay for something that serves nothing",
          "serves nothing" in exp["detail"] or "resolves nowhere" in exp["detail"],
          exp["detail"])
    check("and its action points at the decision, not the card",
          "card" not in exp["action"].lower(), exp["action"])
    check("while the dark finding keeps the deadline",
          dark is not None and in_days(60) in dark["detail"],
          (dark or {}).get("detail", ""))


def test_a_live_domain_keeps_the_original_wording() -> None:
    """The change must not reach a domain that serves something: for those,
    auto-renew really is a date to know rather than to act on."""
    got = estate(resolves=True, expires=in_days(60), auto_renew=True)
    exp = one(got, "domain.expiring")
    check("the expiring finding fires", exp is not None, str(got)[:200])
    if exp:
        check("with the original reading", "rather than to act on" in exp["detail"],
              exp["detail"])
        check("and the original action", "card" in exp["action"], exp["action"])
    check("and nothing is called dark", one(got, "domain.dark") is None, str(got)[:160])


def test_a_dark_domain_outside_the_horizon_raises_one_finding_only() -> None:
    got = estate(resolves=False, expires=in_days(400), auto_renew=True)
    check("no expiry finding for a distant date",
          one(got, "domain.expiring") is None,
          str([f["type"] for f in got]))
    check("and the dark one still carries the date",
          one(got, "domain.dark") is not None, "")


def test_an_unmeasured_liveness_is_still_not_called_dark() -> None:
    """The three-outcome rule, re-asserted because this change touches the same block:
    `resolves: None` means the probe could not run."""
    got = estate(resolves=None, expires=in_days(400), auto_renew=True)
    check("nothing is called dark", one(got, "domain.dark") is None,
          str([f["type"] for f in got]))


if __name__ == "__main__":
    print("dark domains — lapsing is an act, not an absence\n")
    for fn in (test_a_dark_domain_that_renews_itself_says_so,
               test_a_dark_domain_that_does_not_renew_is_told_plainly,
               test_an_unmeasured_renewal_is_not_asserted_as_paid,
               test_the_rdap_gap_does_not_hide_the_registrar_record,
               test_a_dark_and_expiring_domain_is_not_told_to_confirm_the_card,
               test_a_live_domain_keeps_the_original_wording,
               test_a_dark_domain_outside_the_horizon_raises_one_finding_only,
               test_an_unmeasured_liveness_is_still_not_called_dark):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe decision carries its deadline and names the act it needs\033[0m")
