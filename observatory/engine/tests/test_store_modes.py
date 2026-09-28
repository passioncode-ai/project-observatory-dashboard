#!/usr/bin/env python3
"""One mode for the store: 700 for its directories, 600 for its record.

Nothing under `store/` holds a value, and all of it says where the values
are: the ledger, the env fingerprints, which command ran with which secret,
who revealed what. Measured 2026-09-14 on the original installation: `store/`
755, `observatory.db` 644, six journals 644, 21 of 48 scan caches 644 — a map
of the estate readable by any local user. `paths.tighten()` closes that on
every CLI entry; this suite drives it on a planted store first, then asserts
the synthetic workspace's store, then drives each journal writer against a
file planted loose.
"""
from __future__ import annotations
import os
import pathlib
import stat
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def mode(p: pathlib.Path) -> str:
    return oct(stat.S_IMODE(p.stat().st_mode))


def test_it_tightens_a_planted_store_and_leaves_what_is_tracked_alone() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-modes-"))
    raw, logs = d / "raw", d / "logs"
    raw.mkdir(); logs.mkdir()
    (raw / "env.json").write_text("{}", encoding="utf-8")
    (raw / "gh").mkdir(); (raw / "gh" / "x.json").write_text("{}", encoding="utf-8")
    (logs / "keyserver.jsonl").write_text("", encoding="utf-8")
    (logs / "tick.log").write_text("", encoding="utf-8")
    (d / "observatory.db").write_bytes(b"")
    (d / "retention.json").write_text("{}", encoding="utf-8")     # tracked configuration
    for p in (d, raw, logs):
        os.chmod(p, 0o755)
    for p in (raw / "env.json", raw / "gh" / "x.json", logs / "keyserver.jsonl", logs / "tick.log",
              d / "observatory.db", d / "retention.json"):
        os.chmod(p, 0o644)
    import paths
    was = (paths.STORE, paths.SCRATCH, paths.STATE, paths.DB)
    paths.STORE, paths.SCRATCH, paths.STATE, paths.DB = d, raw, d, d / "observatory.db"
    try:
        changed = paths.tighten()
    finally:
        paths.STORE, paths.SCRATCH, paths.STATE, paths.DB = was
    check("directories become 700", {mode(d), mode(raw), mode(logs)} == {"0o700"}, f"{mode(d)} {mode(raw)} {mode(logs)}")
    check("the database and the journals become 600",
          mode(d / "observatory.db") == "0o600" and mode(logs / "keyserver.jsonl") == "0o600", "")
    check("scan caches under raw, nested too, become 600",
          mode(raw / "env.json") == "0o600" and mode(raw / "gh" / "x.json") == "0o600", "")
    check("tracked configuration is not touched — git would see the mode",
          mode(d / "retention.json") == "0o644", mode(d / "retention.json"))
    check("launchd's own log is not touched either", mode(logs / "tick.log") == "0o644", mode(logs / "tick.log"))
    check("and it reports what it changed", len(changed) == 7, str(changed))
    check("a second pass changes nothing", not [c for c in paths.tighten() if str(d) in c])


def test_the_live_store_is_tight() -> None:
    """Asserted after `tighten()`, which every `./observatory.py` entry runs —
    so the gate that runs this is the mechanism, not only the check. The store
    is the synthetic workspace the portable runner built, never a real one."""
    import paths
    paths.tighten()
    loose = []
    for d in (paths.STORE, paths.STORE / "logs", paths.SCRATCH):
        if d.is_dir() and mode(d) != "0o700":
            loose.append(f"{d}={mode(d)}")
    for f in [paths.DB, *(paths.STATE / "logs").glob("*.jsonl"), *paths.SCRATCH.rglob("*.json")]:
        if f.is_file() and mode(f) != "0o600":
            loose.append(f"{f}={mode(f)}")
    check("nothing under the live store is readable beyond its owner", not loose, str(loose[:6]))


def test_every_writer_of_the_record_keeps_the_mode() -> None:
    """Driven, not grepped: each journal writer appends to a file planted at 644
    and must leave it at 600. Reading the source for a `chmod` call passed while
    a writer created its file through `write_text` at the umask's mode."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-writers-"))
    sys.path.insert(0, str(ROOT / "tools"))
    import keyserver
    import use_secret
    for label, module in (("the keyserver journal", keyserver), ("the secret-use journal", use_secret)):
        journal = d / label.replace(" ", "-") / "logs" / "journal.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text("", encoding="utf-8")
        os.chmod(journal, 0o644)
        was = module.AUDIT
        module.AUDIT = journal
        try:
            module.audit("fixture.append", "fixture-subject", {})
        finally:
            module.AUDIT = was
        check(f"{label} is 600 after an append to a loose file", mode(journal) == "0o600", mode(journal))
        check(f"and {label} received the row", "fixture.append" in journal.read_text(encoding="utf-8"))
    import cloudflare
    import openrouter
    for label, module in (("the Cloudflare door", cloudflare), ("the OpenRouter door", openrouter)):
        dest = d / label.replace(" ", "-") / "key"
        dest.parent.mkdir(parents=True, mode=0o700)
        module.write_meta(dest, "{}")
        written = module.meta(dest)
        check(f"{label} writes its meta file at 600", written.is_file() and mode(written) == "0o600",
              mode(written) if written.exists() else "missing")
    check("and the CLI tightens the store on entry", "paths.tighten()" in (ROOT / "observatory.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    print("one mode for the store — 700 for its directories, 600 for its record\n")
    for fn in (test_it_tightens_a_planted_store_and_leaves_what_is_tracked_alone,
               test_the_live_store_is_tight,
               test_every_writer_of_the_record_keeps_the_mode):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe map of the estate is readable by its owner and nobody else\033[0m")
