#!/usr/bin/env python3
"""38 GiB can be freed by reinstalling, 13 GiB is free, and nothing says so.

Measured on a real estate once (kept as the fixture's shape):

    disk.reclaimable_bytes   38.28 GiB across 90 projects
    volume                   13.30 GiB free of 460 (97.1% used)
    ratio                    reclaimable is 288% of what is free

The plugin layer measures exactly the right thing — `disk.reclaimable_bytes`
counts bytes inside `node_modules`, `.venv`, `build`, `target`, `Pods` and their
kin: "what can be deleted and got back by reinstalling". Its only reader is
`host.disk_low`, which is gated behind `DISK_WARNING_MIB = 5120`, an absolute
5 GiB. At 13.30 GiB free that gate is shut, so the operator has a lever that
would nearly quadruple their headroom and no signal to pull it.

**No threshold is invented here, and that is the design.** Moving
`DISK_WARNING_MIB` on the strength of one correlation was refused, and that
refusal stands. This rule needs no level at all: it fires when **free is less than
reclaimable**. That condition is two measured quantities compared, it
self-calibrates (a machine with 200 GiB free and 38 GiB of `node_modules` never
sees it), and it is meaningful precisely when it is true — a routine deletion
would more than double the headroom, which is only worth saying while the
headroom is the smaller number.

**Whole GiB in the title, for a reason that is not cosmetic.**
`tools/commit_registry.py` commits `findings.json` on every tick and free space
moves continuously, so an exact byte figure produces a commit every thirty
minutes for a change that means nothing. `host.disk_low` already learned this —
its own comment records being caught by `test_tick_repo`'s byte-equality check
two seconds apart — and this rule follows it rather than rediscovering it.

**One cause, stated once.** Below 5 GiB `host.disk_low` fires too,
and it is about danger: a VACUUM that may not fit, four scans dead on
`[Errno 28]`. This rule is about the lever. When both are live it NAMES the
other rather than repeating the danger, the way the dark-site rule names the
registrar hold above it.

**What was checked and is not wrong:** `reclaimable_holders()` selects on a
global `MAX(at)` rather than a per-project one, which would undercount if two
projects were stamped at different moments. They are not: the disk plugin stamps
the calendar day, so all rows carry one timestamp and both query shapes return the
same total. The total below is computed with the per-project join
anyway, because a plugin that one day stamps a real timestamp must not silently
shrink this number.
"""
from __future__ import annotations
import json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
# A private synthetic estate: its registry and store are what `collect()` reads
# in the driven case at the bottom; nothing on the machine running it is used.
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()

GIB = 1024 ** 3
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def rule(free, reclaimable, holders=(), disk_low=False):
    import build_findings as B
    fn = getattr(B, "reclaimable_pressure", None)
    if fn is None:
        return None
    return fn(free, reclaimable, list(holders), disk_low)


HOLDERS = ["alpha-web ~6.3 GB", "beta-api ~5.7 GB",
           "gamma-app ~3.1 GB"]


# ─────────── it fires on the comparison, not on a level ────────────────

def test_the_live_shape_is_reported() -> None:
    out = rule(13.30 * GIB, 38.28 * GIB, HOLDERS)
    if out is None:
        check("build_findings.reclaimable_pressure exists", False,
              "the metric's only reader is gated behind an absolute threshold")
        return
    check("more reclaimable than free is a finding", len(out) == 1, str(out))
    if not out:
        return
    f = out[0]
    check("as a warning — nothing is broken and nothing is lost",
          f["severity"] == "warning", f["severity"])
    check("the subject is the volume, not a project",
          f["subject"] == "host:volume", f["subject"])
    blob = json.dumps(f, ensure_ascii=False)
    check("it names what can be freed", "38" in blob, f["title"])
    check("and what is free now", "13" in blob, f["title"] + " | " + f["detail"][:120])
    check("the holders are named", "alpha-web" in blob, f["detail"][:200])
    check("and the remedy says the bytes come back",
          "reinstall" in blob.lower(), f["action"][:160])


def test_a_roomy_volume_is_silent() -> None:
    """The self-calibration. 200 GiB free with 38 GiB of `node_modules` is an
    ordinary developer machine, and a rule that fires there is noise."""
    out = rule(200 * GIB, 38.28 * GIB, HOLDERS)
    if out is None:
        return
    check("free space larger than the lever reports nothing", out == [], str(out))


def test_no_threshold_constant_was_invented() -> None:
    src = (ROOT / "tools/build_findings.py").read_text(encoding="utf-8")
    i = src.find("def reclaimable_pressure")
    body = src[i:i + 2600] if i != -1 else ""
    check("the rule holds no GiB threshold of its own",
          "GIB_THRESHOLD" not in body and "RECLAIMABLE_WARNING" not in body,
          "moving DISK_WARNING_MIB without evidence was refused; this "
          "rule needs no level, only the comparison")
    check("and it says why in its own words",
          "self-calibrat" in body.lower() or "no threshold" in body.lower()
          or "comparison" in body.lower(),
          "the absent constant is the decision, so it has to be written down")


# ─────────── the numbers do not churn the registry ─────────────────────

def test_the_title_is_stable_across_a_byte_of_drift() -> None:
    """`findings.json` is committed on every tick. `host.disk_low` rounds to
    whole GiB because an exact figure produced a commit every thirty minutes for
    a change that means nothing; this rule must not reintroduce that."""
    a = rule(13.30 * GIB, 38.28 * GIB, HOLDERS)
    b = rule(13.30 * GIB + 4096, 38.28 * GIB + 8192, HOLDERS)
    if a is None or not a or not b:
        return
    check("a few kilobytes of drift changes no character of the row",
          json.dumps(a, ensure_ascii=False) == json.dumps(b, ensure_ascii=False),
          f"{a[0]['title']!r} vs {b[0]['title']!r}")


# ─────────── one cause, stated once ────────────────────────────────────

def test_when_the_volume_is_also_dangerous_it_names_the_other_row() -> None:
    out = rule(2 * GIB, 38.28 * GIB, HOLDERS, disk_low=True)
    if out is None or not out:
        return
    f = out[0]
    check("it points at host.disk_low rather than repeating the danger",
          "disk_low" in json.dumps(f, ensure_ascii=False),
          f["detail"][-200:])
    check("and does not claim the volume is fine",
          "VACUUM" not in f["detail"],
          "the danger belongs to the other row; this one carries the lever")


# ─────────── absence is not zero ───────────────────────────────────────

def test_unmeasured_inputs_are_silent_rather_than_reassuring() -> None:
    for free, rec, why in ((None, 38.28 * GIB, "free space unknown"),
                           (13.30 * GIB, None, "reclaimable unmeasured"),
                           (13.30 * GIB, 0.0, "nothing reclaimable")):
        out = rule(free, rec, HOLDERS)
        if out is None:
            return
        check(f"{why} reports nothing", out == [], str(out))
    # A zero total means the plugin found no reinstallable directory at all,
    # which is a fact and not a lever; and a None means it has not run. Neither
    # is an occasion to tell the operator anything.


def test_holders_are_bounded_and_say_so() -> None:
    many = [f"p{i} ~{i} GB" for i in range(12)]
    out = rule(13.30 * GIB, 38.28 * GIB, many)
    if out is None or not out:
        return
    f = out[0]
    check("a long holder list is clipped", f["detail"].count("~") <= 6,
          f["detail"][:240])
    check("and the row says how many are not listed",
          "more" in f["detail"] or "ещё" in f["detail"], f["detail"][-160:])


# ─────────── and it is reached by the whole build ──────────────────────

def test_it_fires_through_the_findings_build() -> None:
    """The original drove this against a live estate. Here the synthetic
    estate's store carries planted reclaimable measurements and the volume's
    free space is patched, so the whole `collect()` path is exercised — the
    metric resolved through the manifest role, the per-project total, and the
    holders named — without depending on how full the machine running it is.
    """
    import importlib
    import collections
    import paths
    from store import db
    import build_findings as B
    importlib.reload(B)
    metric = B.metric_for_role(B.RECLAIMABLE_ROLE)
    check("an installed plugin claims the reclaimable role", bool(metric), str(metric))
    if not metric:
        return
    conn = db.connect()
    try:
        with conn:
            for pid, value in (("project:example-sample-1", 3.0e9),
                               ("project:example-sample-2", 2.5e9),
                               ("project:example-sample-3", 2.0e9)):
                conn.execute("INSERT OR REPLACE INTO metrics(project_id,metric,at,value,unit,"
                             "source,payload_json,recorded_at) VALUES (?,?,?,?,?,?,?,?)",
                             (pid, metric, "2026-01-02T00:00:00Z", value, "bytes",
                              "disk-usage", "{}", "2026-01-02T00:00:00Z"))
    finally:
        conn.close()
    usage = collections.namedtuple("usage", "total used free")
    free = (B.DISK_WARNING_MIB + 200) * 1024 * 1024       # roomy enough for no disk_low
    real = B.shutil.disk_usage
    try:
        B.shutil.disk_usage = lambda p: usage(free * 10, free * 9, free)
        rows = [f for f in B.collect() if f["type"] == "host.reclaimable_lever"]
        check("the build raises the lever once", len(rows) == 1, str(len(rows)))
        if rows:
            check("naming a planted holder",
                  "example-sample-1" in rows[0]["detail"], rows[0]["detail"][:200])
        B.shutil.disk_usage = lambda p: usage(free * 100, free, free * 99)
        rows = [f for f in B.collect() if f["type"] == "host.reclaimable_lever"]
        check("and a roomy volume raises nothing through the same path", rows == [],
              str(rows)[:160])
    finally:
        B.shutil.disk_usage = real


if __name__ == "__main__":
    print("reclaimable pressure — a lever measured, and never offered\n")
    for fn in (test_the_live_shape_is_reported,
               test_a_roomy_volume_is_silent,
               test_no_threshold_constant_was_invented,
               test_the_title_is_stable_across_a_byte_of_drift,
               test_when_the_volume_is_also_dangerous_it_names_the_other_row,
               test_unmeasured_inputs_are_silent_rather_than_reassuring,
               test_holders_are_bounded_and_say_so,
               test_it_fires_through_the_findings_build):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe lever is offered while it is still worth pulling\033[0m")
