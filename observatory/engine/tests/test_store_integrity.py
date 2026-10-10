#!/usr/bin/env python3
"""The store's structure is checked by nothing until a read breaks on it.

The question was whether `PRAGMA integrity_check` belongs in the tick, after a
read returned `database disk image is malformed` and the same pragma answered
`ok` minutes later. **Measured before deciding, and the measurement splits the
question in two:** on a store of about twenty megabytes both `quick_check` and
`integrity_check` answered `ok` in about a tenth of a second.

* **It would NOT have caught that event.** By the time anything could run, the
  image read fine — proven, not assumed. What catches a transient fault is
  the per-project record, and `events` already journals a warning as
  `finding.notified` / `finding.cleared`, so a recurrence leaves a series
  without any new machinery.
* **It catches something else, and that is why it is worth adding.** An earlier
  corruption — hundreds of megabytes with no SQLite header — was found by a read FAILING, which
  means after it had already broken a run. A periodic check finds a damaged store
  before a reader does, and at a tenth of a second it costs nothing on a
  thirty-minute cycle.

So this is added for its own reason and says so. A fix wearing another fix's
justification is how a system accumulates machinery nobody can evaluate.

**Early in the tick, deliberately.** A cycle that writes into a corrupt store
makes it worse, and the collectors' own scratch output does not need the store to
succeed. The tick does not abort — that is its stated design — so the verdict is
recorded and the finding carries it.

The receipt keeps the DURATION as well as the verdict, because "how does this
scale" is the second half of the question and a single reading cannot
answer it.
"""
from __future__ import annotations
import importlib, json, os, pathlib, sqlite3, subprocess, sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir                                                # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def stamp(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def workspace() -> tuple[pathlib.Path, dict]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-integrity-"))
    (d / "scratch").mkdir()
    env = dict(os.environ, OBSERVATORY_DB=str(d / "observatory.db"),
               OBSERVATORY_SCRATCH=str(d / "scratch"))
    return d, env


def run_check(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([PY, "tools/check_store.py"], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=600)


def receipt(d: pathlib.Path) -> dict:
    f = d / "scratch/integrity.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


# ─────────── the check, on a real store ────────────────────────────────

def test_a_sound_store_is_reported_ok_with_its_cost() -> None:
    d, env = workspace()
    subprocess.run([PY, "store/migrate.py"], cwd=ROOT, env=env,
                   capture_output=True, text=True, timeout=600)
    p = run_check(env)
    check("the check runs", p.returncode == 0, (p.stdout + p.stderr)[-240:])
    r = receipt(d)
    check("the verdict is ok", r.get("verdict") == "ok", json.dumps(r)[:200])
    check("the duration is recorded",
          isinstance(r.get("took_ms"), (int, float)) and r["took_ms"] >= 0,
          json.dumps(r)[:200])
    check("and so is the size it took that long on",
          isinstance(r.get("bytes"), int) and r["bytes"] > 0,
          "'how does this scale' needs both numbers or neither")
    check("the pragma it ran is named", "integrity" in str(r.get("pragma", "")),
          json.dumps(r)[:200])


def test_a_corrupt_store_is_reported_rather_than_raised() -> None:
    """DRIVEN against a planted corruption, because a check nobody has watched
    refuse is a green nobody earned. The file is a valid SQLite header followed
    by garbage — the shape of the earlier corruption, minus its size."""
    d, env = workspace()
    subprocess.run([PY, "store/migrate.py"], cwd=ROOT, env=env,
                   capture_output=True, text=True, timeout=600)
    db = d / "observatory.db"
    raw = bytearray(db.read_bytes())
    # Overwrite deep inside the file, past the header, so it opens and fails on
    # a page rather than on `file is not a database`.
    for i in range(4096, min(len(raw), 20480)):
        raw[i] = 0x5A
    db.write_bytes(bytes(raw))
    p = run_check(env)
    check("it does not raise", p.returncode in (0, 1), (p.stdout + p.stderr)[-240:])
    r = receipt(d)
    check("the verdict is not ok", r.get("verdict") not in (None, "ok"),
          json.dumps(r)[:240])
    check("and the reason is kept", bool(r.get("detail")), json.dumps(r)[:240])


def test_a_malformed_search_index_is_named_rather_than_missed() -> None:
    """The damage this store ACTUALLY recorded, planted and driven.

    `store_faults.jsonl` holds one entry: `SQLITE_CORRUPT: vtable constructor
    failed: search_notes`, 2026-09-08T00:54:56Z, with 12.09 GiB free — so not
    the disk-full episode of the day before. Nothing proved the periodic check
    would name that class, and the justification in `tools/check_store.py` cited
    ordinary-index content as the thing `integrity_check` adds, which is a
    different class entirely.

    Measured: dropping the FTS5 shadow table `search_notes_data` makes the
    virtual table unusable — `SELECT count(*)` raises `database disk image is
    malformed` — and the pragma names it in words a reader can act on. The
    checker must report that as a verdict rather than raise, which is the whole
    contract of this file.
    """
    d, env = workspace()
    subprocess.run([PY, "store/migrate.py"], cwd=ROOT, env=env,
                   capture_output=True, text=True, timeout=600)
    db = d / "observatory.db"
    con = __import__("sqlite3").connect(db)
    have = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE name = 'search_notes_data'")]
    if not have:
        con.close()
        print("  NOTE  this store has no FTS5 index to damage "
              "[uncoverable: the class exists only where the virtual table does, "
              "and a store built without FTS5 cannot grow one here]")
        return
    con.execute("DROP TABLE search_notes_data")
    con.commit()
    con.close()
    p = run_check(env)
    check("the checker does not raise", p.returncode in (0, 1),
          (p.stdout + p.stderr)[-240:])
    r = receipt(d)
    check("the verdict is not ok", r.get("verdict") not in (None, "ok"),
          json.dumps(r)[:240])
    check("and it names the FTS5 index rather than a page number",
          "FTS5" in (r.get("detail") or "") or "search_notes" in (r.get("detail") or ""),
          json.dumps(r)[:240])


def test_an_unopenable_store_is_a_third_answer() -> None:
    """Not `ok`, and not a failed check either: `file is not a database` is a
    fact about the FILE, and reporting it as a failed integrity check would send
    a reader looking for a damaged page in something that has no pages."""
    d, env = workspace()
    (d / "observatory.db").write_bytes(b"this is not a database, not even close")
    p = run_check(env)
    check("it does not raise", p.returncode in (0, 1), (p.stdout + p.stderr)[-240:])
    r = receipt(d)
    check("the verdict says the file would not open",
          r.get("verdict") == "unopenable", json.dumps(r)[:240])


def test_an_absent_store_is_not_a_fault() -> None:
    """A fresh clone has no store, and calling that corrupt would make the first
    run of the pipeline report a critical."""
    d, env = workspace()
    p = run_check(env)
    check("the check is silent about a store that does not exist",
          p.returncode == 0, (p.stdout + p.stderr)[-200:])
    r = receipt(d)
    check("and says so rather than claiming ok", r.get("verdict") == "absent",
          json.dumps(r)[:200])


# ─────────── the finding ───────────────────────────────────────────────

def findings_for(rec: dict | None) -> list[dict]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-intf-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    for name, body in (("projects.json", '{"projects": []}'),
                       ("repositories.json", '{"repositories": []}'),
                       ("relations.json", '{"relations": []}')):
        (d / "registry" / name).write_text(body)
    if rec is not None:
        (d / "scratch/integrity.json").write_text(json.dumps(rec))
    os.environ.update(OBSERVATORY_REGISTRY=str(d / "registry"),
                      OBSERVATORY_SCRATCH=str(d / "scratch"),
                      OBSERVATORY_DB=str(d / "absent.db"))
    import paths
    importlib.reload(paths)
    import build_findings as B
    importlib.reload(B)
    try:
        return [f for f in B.collect() if f["type"].startswith("store.")]
    finally:
        for k in ("OBSERVATORY_REGISTRY", "OBSERVATORY_SCRATCH", "OBSERVATORY_DB"):
            os.environ.pop(k, None)
        importlib.reload(paths)


def rec(verdict: str, **over) -> dict:
    return {"ran_at": stamp(0.2), "verdict": verdict, "pragma": "integrity_check",
            "took_ms": 97, "bytes": 22_700_000, "detail": "", **over}


def test_a_failed_check_is_critical() -> None:
    got = findings_for(rec("damaged", detail="Page 42: btreeInitPage() returns error"))
    f = got[0] if len(got) == 1 else None
    check("it fires", f is not None, str(got)[:200])
    if f is None:
        return
    check("as a critical — nothing can rebuild this store",
          f["severity"] == "critical", f["severity"])
    check("quoting what SQLite said", "Page 42" in f["detail"], f["detail"][:220])
    check("and the action names the one copy that exists outside it",
          "ledger" in f["action"].lower() or "backup" in f["action"].lower(),
          f["action"])


def test_an_unopenable_store_is_also_critical_and_says_which_it_is() -> None:
    got = findings_for(rec("unopenable", detail="file is not a database"))
    f = got[0] if len(got) == 1 else None
    check("it fires", f is not None, str(got)[:200])
    if f:
        check("as a critical", f["severity"] == "critical", f["severity"])
        check("and does not send a reader looking for a damaged page",
              "page" not in f["detail"].lower(), f["detail"][:220])


def test_a_sound_store_is_silent() -> None:
    check("ok says nothing", findings_for(rec("ok")) == [],
          "a check that reports success every thirty minutes is noise")


def test_an_absent_store_is_silent() -> None:
    check("absent says nothing", findings_for(rec("absent")) == [],
          "a fresh clone has no store and that is not a fault")


def test_a_stale_verdict_is_reported_as_stale() -> None:
    """A verdict from three days ago is not a verdict about now. The check runs
    on the tick, so its own silence has to be measurable."""
    got = findings_for(rec("ok", ran_at=stamp(72)))
    f = got[0] if len(got) == 1 else None
    check("staleness is raised", f is not None, str(got)[:200])
    if f:
        check("as info rather than critical — nothing is known to be wrong",
              f["severity"] == "info", f["severity"])
        # THE TITLE carries the age and the detail carries the timestamp it
        # refers to — so the assertion reads both rather than guessing which
        # field a number landed in.
        check("and it says how old the verdict is",
              "72h" in f["title"] or "72" in f["detail"],
              f"{f['title']} / {f['detail'][:160]}")


def test_an_absent_receipt_raises_nothing() -> None:
    check("no receipt, no finding", findings_for(None) == [],
          "the check has not run since it was added; that is not a verdict")


# ─────────── wiring ────────────────────────────────────────────────────

def test_the_check_runs_early_in_the_tick() -> None:
    """A cycle that writes into a corrupt store makes it worse, and the
    collectors' scratch output does not need the store to succeed."""
    tick = (ROOT / "tools/tick.py").read_text(encoding="utf-8")
    check("the tick runs it", "check_store.py" in tick, "")
    if "check_store.py" not in tick:
        return
    # THE `step` INVOCATIONS, not the bare filenames: `tick.sh` mentions
    # retention in a comment above the insertion point, so searching raw text
    # compared a comment's position with a command's.
    sys.path.insert(0, str(ROOT / "tests"))
    import tick_reader
    steps = [" ".join(words) for _line, fn, _name, words in tick_reader.tick_calls(ROOT) if fn == "step"]
    # `scan_events.py` is NOT in this list, and the first version of this
    # comment said the tick does not run it. **That was wrong** — tick.sh line
    # 160 invokes it directly, outside the `step` wrapper, as
    # `… >/dev/null 2>&1 || log "events degraded"`. The unwrapped form is a
    # deliberate pattern there: `merge`, `emit` and `validate` are invoked the
    # same way and `bail` out, while this one logs and continues. Its failure is
    # not silent either — no fresh `scans` row means `scan.stale` fires — so
    # only the ORDER assertion was wrong, because `step "` never matches it.
    order = {name: i for i, l in enumerate(steps)
             for name in ("check_store.py", "collectors/scan_filesystem.py",
                          "store/rollup.py", "store/retention.py") if name in l}
    check("the check has a step of its own", "check_store.py" in order, str(sorted(order)))
    for later in ("collectors/scan_filesystem.py", "store/rollup.py",
                  "store/retention.py"):
        check(f"before {later}",
              order.get("check_store.py", 999) < order.get(later, -1),
              f"{order.get('check_store.py')} vs {order.get(later)}")


def test_the_receipt_is_a_foreign_write_to_the_gate() -> None:
    src = (ROOT / "observatory.py").read_text(encoding="utf-8")
    block = src.split("FOREIGN_WRITES_IGNORED = {")[1].split("\n}")[0]
    check("store/raw/integrity.json is declared", "integrity.json" in block,
          block[:300])


if __name__ == "__main__":
    print("store integrity — checked by nothing until a read broke on it\n")
    for fn in (test_a_sound_store_is_reported_ok_with_its_cost,
               test_a_corrupt_store_is_reported_rather_than_raised,
               test_a_malformed_search_index_is_named_rather_than_missed,
               test_an_unopenable_store_is_a_third_answer,
               test_an_absent_store_is_not_a_fault,
               test_a_failed_check_is_critical,
               test_an_unopenable_store_is_also_critical_and_says_which_it_is,
               test_a_sound_store_is_silent,
               test_an_absent_store_is_silent,
               test_a_stale_verdict_is_reported_as_stale,
               test_an_absent_receipt_raises_nothing,
               test_the_check_runs_early_in_the_tick,
               test_the_receipt_is_a_foreign_write_to_the_gate):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe store's structure is measured before a reader finds out\033[0m")
