#!/usr/bin/env python3
"""Three criticals for one cause, two of them prescribing the impossible.

An estate reported:

    CRITICAL  held.example is on registrar hold
    CRITICAL  alpha-os publishes os.held.example, which is dark
    CRITICAL  beta-vote publishes vote.held.example, which is dark

One cause, two symptoms. The parent domain carries `clientHold` at its
registrar — with expiry far away, so a deliberate hold rather than a lapse —
and a held domain is withdrawn from DNS, which darkens every host under it.
All three hostnames fail to resolve, which is not three problems.

**And the two symptoms told the operator to "restore the host", which they
cannot do while the registrar holds the parent.** A remedy its own cause makes
impossible is worse than no remedy: it sends someone to try, and two impossible
criticals devalue the one real item beside them. That is the same principle
the board applies to a full disk — one cause, stated once — with the difference
that here the symptom must SURVIVE, because the dashboard shows findings per
project and a project whose site is dark must not look clean.

So a dead host whose parent domain the estate has ALREADY MEASURED as held stays
a finding, drops to `warning`, and points at the cause. Matched by suffix
against the estate's own RDAP results — no public-suffix list and no guess.

Effect on that estate: the critical count went from 3 to 1.
"""
from __future__ import annotations
import importlib, json, os, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir

# A private workspace of this suite's own, initialized by the real `init`, so
# the board's configuration (retention, host boundary, machine limits) is the
# shipped default and never a machine's. Location overrides a runner exports
# are dropped so every location follows it.
for _name in ("OBSERVATORY_REGISTRY", "OBSERVATORY_DB", "OBSERVATORY_STATE", "OBSERVATORY_SCRATCH"):
    os.environ.pop(_name, None)
os.environ["OBSERVATORY_HOME"] = str(pathlib.Path(tmpdir.mkdtemp(prefix="observatory-cause-home-")).resolve() / "home")
subprocess.run([sys.executable, str(ROOT / "observatory.py"), "init"], cwd=ROOT,
               capture_output=True, timeout=120, check=True)

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def estate(sites: list[tuple[str, str]], hosts: list[dict],
           statuses: dict[str, list[str]]) -> list[dict]:
    """Findings for a planted estate: projects with sites, a liveness answer per
    host, and RDAP statuses per registrable domain."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-cause-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "registry/projects.json").write_text(json.dumps({"projects": [
        {"id": f"project:{name}", "name": name, "lifecycle": "active",
         "sites": [{"host": host, "confidence": "measured", "evidence": []}]}
        for name, host in sites]}))
    (d / "registry/repositories.json").write_text('{"repositories": []}')
    (d / "registry/relations.json").write_text('{"relations": []}')
    (d / "registry/domains.json").write_text(json.dumps({"domains": [
        {"name": n} for n in statuses]}))
    # THE RDAP ANSWER LIVES IN EACH HOST'S `measured` BLOCK, keyed by `about` —
    # the registrable name every subdomain's record carries. The first version
    # of this fixture invented a separate `rdap` list in the scratch file, so
    # `held` came back empty and every assertion failed for a reason that had
    # nothing to do with the code under test.
    enriched = []
    for h in hosts:
        about = next((n for n in statuses if h["host"] == n
                      or h["host"].endswith("." + n)), h["host"])
        enriched.append({**h, "measured": {
            "about": about, "statuses": statuses.get(about, []),
            "expires_on": None}})
    (d / "registry/domain-liveness.json").write_text(json.dumps({
        "schema_version": 1, "scanned_on": "2026-09-07", "hosts": enriched,
        "verification": {"agreed": 0, "disagreed": 0, "unverifiable": 0,
                         "disagreements": []},
        "degraded": []}))
    # AND THE LEAK REGISTER, one file along again. `secret.leaked_unrotated`
    # reads `$OBSERVATORY_VAULT_DIR/leaks.jsonl`, which none of the three
    # redirects below reaches — so an unsettled leak on the machine appeared in
    # a list this fixture must entirely own, and "exactly one critical for one
    # cause" failed on a critical the fixture never planted.
    os.environ.update(OBSERVATORY_VAULT_DIR=str(d / "vault"),
                      OBSERVATORY_REGISTRY=str(d / "registry"),
                      OBSERVATORY_SCRATCH=str(d / "scratch"),
                      OBSERVATORY_DB=str(d / "absent.db"))
    import paths
    importlib.reload(paths)
    import build_findings as B
    importlib.reload(B)
    # THE LIVE VOLUME, PATCHED OUT. `host.disk_low` reads
    # `shutil.disk_usage(ROOT)`, and `ROOT` is deliberately not overridable, so
    # every count below was really a count of the fixture PLUS whatever the
    # machine's own disk was doing. When a volume fills, that finding turns
    # critical and "exactly one critical for one cause" fails with nothing
    # wrong in the code it covers. Narrowing the assertion to the planted types
    # would hide this instead: the claim is about the WHOLE list, so the
    # fixture has to own the whole list.
    import collections as C
    usage = C.namedtuple("usage", "total used free")
    healthy = (B.DISK_WARNING_MIB + 1) * 1024 * 1024
    B.shutil.disk_usage = lambda p: usage(healthy, 0, healthy)
    # AND THE LIVE WALLET, for the same reason and by the same mechanism. The
    # three environment variables above redirect the registry, the scratch and
    # the store's DB, but `key-usage.json` and `wallet.json` are read from
    # `paths.STORE`, which they do not touch. A shared key at its ceiling turns
    # `wallet.shared_key` critical by design, and "exactly one critical for one
    # cause" fails again — the same leak as the disk above, one file along. Pointed at the fixture's own directory, where
    # neither document exists, so the wallet rule is silent and the list is
    # entirely the fixture's.
    B.paths.STORE = d / "store"
    B.paths.STATE = d / "store"
    try:
        return B.collect()
    finally:
        for k in ("OBSERVATORY_REGISTRY", "OBSERVATORY_SCRATCH", "OBSERVATORY_DB",
                  "OBSERVATORY_VAULT_DIR"):
            os.environ.pop(k, None)
        importlib.reload(paths)


DARK = {"resolves": False, "status": None, "http": None, "checked_on": "2026-09-07"}
HOLD = ["client hold", "client transfer prohibited"]


def test_one_cause_produces_one_critical() -> None:
    """THE MEASURED SHAPE, with synthetic names."""
    got = estate(
        sites=[("alpha-os", "os.held.example"), ("beta-vote", "vote.held.example")],
        hosts=[{"host": "os.held.example", **DARK},
               {"host": "vote.held.example", **DARK}],
        statuses={"held.example": HOLD})
    crit = [f for f in got if f["severity"] == "critical"]
    check("exactly one critical for one cause", len(crit) == 1,
          str([f["type"] for f in crit]))
    if crit:
        check("and it is the cause", crit[0]["type"] == "domain.hold", crit[0]["type"])
    dead = [f for f in got if f["type"] == "site.dead"]
    check("both symptoms survive as findings", len(dead) == 2,
          str([f["title"] for f in dead]))
    for f in dead:
        check(f"{f['subject']} is a warning, not a critical",
              f["severity"] == "warning", f["severity"])
        check(f"{f['subject']} names the cause", "held.example is on registrar hold"
              in f["detail"], f["detail"][-140:])
        check(f"{f['subject']} does not prescribe restoring the host first",
              not f["action"].startswith("restore the host"), f["action"][:80])
        check(f"{f['subject']} says the restore is impossible until then",
              "cannot be restored" in f["action"], f["action"][:120])


def test_a_dark_host_with_no_held_parent_stays_critical() -> None:
    """The other half: a published claim that is false and CAN be acted on is
    still the most serious thing the estate says."""
    got = estate(
        sites=[("somebody", "app.example.test")],
        hosts=[{"host": "app.example.test", **DARK}],
        statuses={})
    dead = [f for f in got if f["type"] == "site.dead"]
    check("it fires", len(dead) == 1, str(got)[:160])
    if dead:
        check("as a critical", dead[0]["severity"] == "critical", dead[0]["severity"])
        check("with the original remedy",
              dead[0]["action"] == "restore the host, or remove the site from the project",
              dead[0]["action"])
        check("and no cause is invented in the detail",
              "registrar hold" not in dead[0]["detail"], dead[0]["detail"][-120:])


def test_the_suffix_match_is_a_boundary_not_a_substring() -> None:
    """`notheld.example` must not match `held.example`. The match is on
    `"." + domain`, which is what makes it a boundary; a bare `endswith` would
    attribute one owner's outage to another's domain."""
    got = estate(
        sites=[("neighbour", "www.notheld.example")],
        hosts=[{"host": "www.notheld.example", **DARK}],
        statuses={"held.example": HOLD})
    dead = [f for f in got if f["type"] == "site.dead"]
    check("the neighbouring domain's host is unaffected",
          dead and dead[0]["severity"] == "critical",
          str([(f["title"], f["severity"]) for f in dead]))
    if dead:
        check("and its action is not redirected to somebody else's hold",
              "held.example first" not in dead[0]["action"], dead[0]["action"][:100])


def test_the_host_that_IS_the_held_domain_is_not_called_its_own_child() -> None:
    """A site published at the apex — `held.example` itself — has no parent among
    the held domains, because `"held.example".endswith(".held.example")` is
    false. It stays critical, and the hold finding is the one that explains it."""
    got = estate(
        sites=[("apex", "held.example")],
        hosts=[{"host": "held.example", **DARK}],
        statuses={"held.example": HOLD})
    dead = [f for f in got if f["type"] == "site.dead"]
    check("the apex host is still reported", len(dead) == 1, str(dead)[:160])
    if dead:
        check("as a critical, since it is the cause rather than a symptom of one",
              dead[0]["severity"] == "critical", dead[0]["severity"])


def test_the_hold_finding_still_names_what_it_darkens() -> None:
    got = estate(
        sites=[("alpha-os", "os.held.example")],
        hosts=[{"host": "os.held.example", **DARK}],
        statuses={"held.example": HOLD})
    hold = [f for f in got if f["type"] == "domain.hold"]
    check("the cause is raised", len(hold) == 1, str(got)[:160])
    if hold:
        check("it stays critical", hold[0]["severity"] == "critical",
              hold[0]["severity"])
        check("it lists the hosts it darkens",
              "os.held.example" in hold[0]["detail"], hold[0]["detail"][-120:])
        check("and its action is the one thing that can be done",
              "registrar" in hold[0]["action"], hold[0]["action"])



def test_the_hold_says_how_long_the_registration_itself_has() -> None:
    """A hold with eight months of registration left and one expiring next week
    are the same sentence and different decisions, and the expiry sat unread in
    the very RDAP object this row quotes its statuses from."""
    sys.path.insert(0, str(ROOT / "tools"))
    import build_findings as B
    far = B._hold_pressure("2099-01-01")
    check("a live registration is quoted with its distance",
          "day(s) away" in far and "2099-01-01" in far, far)
    check("and says the hold is what darkens it", "not the clock" in far, far)
    gone = B._hold_pressure("2000-01-01")
    check("an expired one says the hold is no longer the only thing at stake",
          "expired on" in gone, gone)
    check("and an unmeasured expiry says nothing rather than never",
          B._hold_pressure(None) == "" and B._hold_pressure("not-a-date") == "",
          repr(B._hold_pressure("not-a-date")))


if __name__ == "__main__":
    print("cause and symptom — one thing to do, not three\n")
    for fn in (test_one_cause_produces_one_critical,
               test_a_dark_host_with_no_held_parent_stays_critical,
               test_the_suffix_match_is_a_boundary_not_a_substring,
               test_the_host_that_IS_the_held_domain_is_not_called_its_own_child,
               test_the_hold_finding_still_names_what_it_darkens,
               test_the_hold_says_how_long_the_registration_itself_has):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma symptom no longer prescribes what its cause makes "
          "impossible\033[0m")
