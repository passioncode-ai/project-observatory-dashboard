#!/usr/bin/env python3
"""A foreign key whose absence carried a critical finding away with it.

`collectors/scan_domains.py` builds each domain's record from RDAP's own JSON:
`status`, `events[].eventAction`/`eventDate`, `entities[].roles`/`vcardArray`.
Every read is `d.get(...) or <default>`, so an absent key and an empty value
produce the same answer — and one of those answers is load-bearing.

`statuses` decides `domain.hold`, the CRITICAL row that says a domain is on
registrar hold. If RDAP renamed `status`, every record would read `[]`,
`any("hold" in s for s in [])` would be False for every domain, and that row
would disappear — with no degradation anywhere, because the fetch itself
succeeded. `expiration` decides `domain.expiring` the same way.

On the estate this was written against, no record had an empty `statuses` and
none lacked an `expiration`. So an empty one is not a normal state — it is a key
that could not be read. RFC 9083 does make `status` optional, so a TLD may
legitimately omit it; then the record says so for that domain, which is true
rather than alarmist.

Three answers now: on hold, not on hold, and the hold could not be read.
"""
from __future__ import annotations
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "collectors"))
sys.path.insert(0, str(ROOT / "tests"))

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def record(**over):
    """One RDAP reply, as the registry would hold it."""
    base = {"about": "example.test", "statuses": ["client transfer prohibited"],
            "expiration": "2027-01-01", "registration": "2024-01-01",
            "registrar": "NameCheap, Inc."}
    base.update(over)
    return base


# ─────────── the collector names the key it could not read ─────────────

def test_the_builder_names_an_absent_foreign_key() -> None:
    import scan_domains as S
    fn = getattr(S, "rdap_record", None)
    if fn is None:
        # The builder is inline in `rdap()`; the source-level assertions below
        # hold the same property.
        src = (ROOT / "collectors/scan_domains.py").read_text(encoding="utf-8")
        check("the record carries an `unread` list", '"unread"' in src,
              "an absent foreign key and an empty value must not read alike")
        check("and `status` is one of the keys it watches",
              '("status", bool(d.get("status")))' in src, "")
        check("with `expiration` and `registrar` beside it",
              '"expiration", bool(out.get("expiration"))' in src, "")
        return
    got, _ = fn({"ldhName": "example.test"}, "example.test")
    check("a reply with no status names it", "status" in (got.get("unread") or []),
          str(got))


def test_the_scan_turns_unread_keys_into_a_degradation() -> None:
    src = (ROOT / "collectors/scan_domains.py").read_text(encoding="utf-8")
    check("the scan groups unread keys across domains",
          "by_key" in src and "rdap:{key}" in src,
          "a rename upstream hits every domain at once, and 49 identical lines "
          "would bury the one TLD that legitimately omits a field")
    check("and says an empty answer is not a negative one",
          "not the same as a negative" in src, "")
    check("the list is capped and the remainder counted",
          "and {len(names_) - 6} more" in src,
          "a truncated list that does not say so reads as complete")


# ─────────── the board keeps the three answers apart ───────────────────

def test_a_missing_status_is_not_a_clean_bill() -> None:
    import build_findings as B
    src = (ROOT / "tools/build_findings.py").read_text(encoding="utf-8")
    # DRIVEN, not read. `hold_verdict` exists so the third answer has an input
    # a fixture can supply — the collector's own record — instead of being
    # asserted from the source.
    fn = getattr(B, "hold_verdict", None)
    check("build_findings.hold_verdict exists", fn is not None,
          "the branch was inline, so the third answer could only be grepped for")
    if fn is not None:
        check("a record on hold says held",
              fn(record(statuses=["clientHold", "client transfer prohibited"])) == "held",
              str(fn(record(statuses=["clientHold"]))))
        check("a record with statuses and no hold says clear",
              fn(record()) == "clear", str(fn(record())))
        check("and a record whose `status` key was absent says UNREADABLE, "
              "not clear",
              fn(record(statuses=[], unread=["status"])) == "unreadable",
              "an emptied foreign key must not read as a clean bill")
        check("an empty statuses WITHOUT the marker still reads clear, so the "
              "marker is what carries the claim",
              fn(record(statuses=[])) == "clear",
              "the collector decides which it is; this function only reports it")
    check("and the third answer has its own row",
          '"domain.hold_unknown"' in src, "")
    # THE WHOLE ROW, not a fixed window. `src[i:i + 900]` cut the action off —
    # the same defect as the 500-char window an earlier assertion in this
    # session used, where a comment pushed the code out of range. A row ends at
    # its `evidence` key, so that is where to stop.
    i = src.find('"domain.hold_unknown"')
    end = src.find('"evidence"', i)
    block = src[i:end if end > i else i + 2000]
    check("as a warning, because the hold is what it cannot see",
          '"severity": "warning"' in block, block[:120])
    check("naming which domains", "listed(shown, 8)" in block, "")
    check("and what to run", "scan_domains.py" in block, "")
    del B


def test_a_planted_estate_reports_the_hold_and_the_unreadable_apart() -> None:
    """The rule must not have been broken by making it finer.

    Driven end to end over a sandboxed copy of the synthetic registry: one
    domain on hold, one whose reply carried no `status`, one clear. The board
    must carry exactly one hold row, one unreadable row naming only the second,
    and nothing for the third.
    """
    import os
    import shutil
    import subprocess
    import paths
    import tmp as tmpdir
    work = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-rdap-"))
    reg = work / "registry"
    shutil.copytree(paths.REGISTRY, reg)  # paths-check: allow — the synthetic registry is the fixture's source
    shutil.copytree(paths.SCRATCH, work / "raw")

    def host(name: str, **measured) -> dict:
        return {"host": name, "resolves": True, "http_status": 200,
                "measured": {"about": name, "expires_on": "2030-01-01", **measured}}

    live = json.loads((reg / "domain-liveness.json").read_text(encoding="utf-8"))
    live["hosts"] = [host("held.example.com", statuses=["client hold", "client transfer prohibited"]),
                     host("unread.example.com", statuses=[], unread=["status"]),
                     host("clear.example.com", statuses=["client transfer prohibited"])]
    (reg / "domain-liveness.json").write_text(json.dumps(live), encoding="utf-8")
    env = dict(os.environ, OBSERVATORY_REGISTRY=str(reg), OBSERVATORY_SCRATCH=str(work / "raw"))
    p = subprocess.run([sys.executable, str(ROOT / "tools/build_findings.py")], env=env,
                       capture_output=True, text=True, timeout=300)
    check("the board builds over the planted estate", p.returncode == 0, p.stderr[-300:])
    if p.returncode:
        return
    b = json.loads((reg / "findings.json").read_text(encoding="utf-8"))["findings"]
    holds = [f for f in b if f["type"] == "domain.hold"]
    unknown = [f for f in b if f["type"] == "domain.hold_unknown"]
    check("the one real hold is reported, once",
          len(holds) == 1 and "held.example.com" in json.dumps(holds[0]),
          json.dumps([f.get("subject") for f in holds]))
    check("the unreadable status has its own row, naming only that domain",
          len(unknown) == 1 and "unread.example.com" in unknown[0]["detail"]
          and "clear.example.com" not in unknown[0]["detail"]
          and "held.example.com" not in unknown[0]["detail"],
          json.dumps([f.get("detail", "")[:200] for f in unknown]))
    check("and it says the hold could not be read rather than that it is clear",
          bool(unknown) and "could not be read" in unknown[0]["title"],
          str([f["title"] for f in unknown]))


if __name__ == "__main__":
    print("RDAP's keys — three answers where a rename would have given one\n")
    for fn in (test_the_builder_names_an_absent_foreign_key,
               test_the_scan_turns_unread_keys_into_a_degradation,
               test_a_missing_status_is_not_a_clean_bill,
               test_a_planted_estate_reports_the_hold_and_the_unreadable_apart):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32man unreadable hold is not a clean bill\033[0m")
