#!/usr/bin/env python3
"""The keyserver's journal, read back: bursts, callers and unreadable lines.

Driven on a planted journal so the rule can be watched firing: one subject
revealed in a burst, another once, callers named and unnamed, an unreadable
line, and a journal that is not there at all. Values never enter — the journal
never held one and neither does this fixture.
"""
from __future__ import annotations
import datetime
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def load():
    spec = importlib.util.spec_from_file_location("reveal_findings", ROOT / "tools/reveal_findings.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


NOW = datetime.datetime(2026, 9, 14, 12, 0, tzinfo=datetime.timezone.utc)


def planted(rows: list[dict], extra: str = "") -> pathlib.Path:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-reveals-"))
    p = d / "keyserver.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows) + extra, encoding="utf-8")
    return p


def row(minutes_ago: int, subject: str, caller: str | None = "unnamed", action: str = "reveal") -> dict:
    at = (NOW - datetime.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = {"at": at, "action": action, "subject": subject, "class": "config"}
    if caller is not None:
        r["caller"] = caller
    return r


def test_a_burst_is_named_and_a_single_reveal_is_not() -> None:
    rf = load()
    rows = [row(5 * i, "alpha-web/.env:SERVICE_ACCOUNT_FILE", "session:abc") for i in range(8)]
    rows += [row(30, "beta-api/.env:APP_SECRET", "page:creds")]
    rows += [row(60 * 30, "alpha-web/.env:SERVICE_ACCOUNT_FILE", "session:abc")]   # outside the window
    rows += [row(10, "x/.env:Y", "page:creds", action="limit")]                        # not a reveal
    out = rf.findings(planted(rows), NOW)
    bursts = [f for f in out if f["type"] == "secret.reveal_burst"]
    check("one subject is a burst", len(bursts) == 1 and bursts[0]["subject"].endswith("SERVICE_ACCOUNT_FILE"), str([f["subject"] for f in bursts]))
    check("the title carries the count inside the window, not the lifetime count",
          "8 times" in bursts[0]["title"], bursts[0]["title"])
    check("and the detail names who asked", "session:abc ×8" in bursts[0]["detail"], bursts[0]["detail"][:160])
    check("a single reveal raises nothing", not any("APP_SECRET" in f["subject"] for f in out))
    check("every caller named means no `unnamed` row",
          not any(f["type"] == "secret.reveal_unnamed" for f in out), str([f["type"] for f in out]))
    blob = json.dumps(out)
    check("no row carries a value field or a token shape", '"value"' not in blob and "EAA" not in blob)


def test_an_unnamed_caller_is_counted_only_once_the_header_exists() -> None:
    rf = load()
    # Rows written before callers were recorded carry no `caller` key at all;
    # later rows carry `unnamed` when the header was missing. Only the second kind is a
    # skill copy to update — the first is history.
    rows = [row(10, "a/.env:X", caller=None), row(20, "a/.env:X", caller=None),
            row(30, "b/.env:Y", "unnamed"), row(40, "b/.env:Y", "unnamed"), row(50, "c/.env:Z", "unnamed")]
    out = rf.findings(planted(rows), NOW)
    un = [f for f in out if f["type"] == "secret.reveal_unnamed"]
    check("the unnamed row counts rows that HAVE the key and no name",
          len(un) == 1 and un[0]["title"].startswith("3 reveal(s)"), str([f["title"] for f in un]))
    check("and lists the subjects", "b/.env:Y" in un[0]["detail"] and "c/.env:Z" in un[0]["detail"], un[0]["detail"][:160])


def test_a_journal_that_cannot_be_read_is_a_row_not_silence() -> None:
    rf = load()
    out = rf.findings(planted([row(1, "a/.env:X")], extra="this is not json\n"), NOW)
    check("an unparseable line is reported", any(f["type"] == "secret.journal_unreadable" for f in out), str([f["type"] for f in out]))
    check("and the readable rows are still read", isinstance(out, list))
    missing = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-reveals-")) / "keyserver.jsonl"
    check("no journal at all is no finding", rf.findings(missing, NOW) == [])


def test_the_rule_is_wired_into_the_board() -> None:
    src = (ROOT / "tools/build_findings.py").read_text(encoding="utf-8")
    check("build_findings reads the journal through reveal_findings",
          "import reveal_findings" in src and "reveal_findings.findings(" in src)
    check("and reads it from STATE, the redirectable knob", 'paths.STATE / "logs" / "keyserver.jsonl"' in src)


if __name__ == "__main__":
    print("the keyserver's journal, read back — bursts, callers, and a file that will not parse\n")
    for fn in (test_a_burst_is_named_and_a_single_reveal_is_not,
               test_an_unnamed_caller_is_counted_only_once_the_header_exists,
               test_a_journal_that_cannot_be_read_is_a_row_not_silence,
               test_the_rule_is_wired_into_the_board):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthirty-three reveals in one night now have a row, and a name to ask\033[0m")
