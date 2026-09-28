#!/usr/bin/env python3
"""How current is any of this? — the question the one human surface answered wrong.

The dashboard once printed an "updated" date four days old over data four
minutes old. The value came from `registry/projects.json`'s `updated_on`, and
`collectors/emit_registry.py` set it from a date literal typed once. The tick
rewrites six registry documents on every run and every one of them carried that
date. It was not merely wrong — it carried no
information in either direction: not when the data was minutes old, and not on
the day the emit stops and the data really is a week old. The one line a person
reads to judge freshness was a constant.

Three separate facts had been collapsed into it, and each needs its own answer:

    when was the estate MEASURED        the newest `scans` row  -> the headline
    when did the registry's CONTENT     carried forward while the document
      last change                         is byte-equal, stamped when it moves
    is the watcher still running        `scan.stale`, which did not exist

**And the emit wrote the canonical registry non-atomically.** `write_text` after
`json.dumps` truncates the destination first; `atomic.py` exists in this
repository for exactly that and its docstring names three collectors that had
the defect — the one writing `registry/projects.json` was not among them. When
a volume filled, four collectors died mid-write. Had one of them been this,
`merge.py` would have read a truncated registry on the next tick.
"""
from __future__ import annotations
import importlib, json, os, pathlib, sqlite3, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
from test_portable_mcp import setup as portable_setup               # noqa: E402
portable_setup()
import tmp as tmpdir                                                # noqa: E402
import live_estate                                                  # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


# ─────────── the stamp is a measurement, carried while nothing moves ────

def test_a_stamp_moves_only_when_the_content_moves() -> None:
    import atomic
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-stamp-"))
    f = d / "doc.json"
    _, ch = atomic.write_json_carrying(f, {"schema_version": 1, "updated_on": None,
                                           "rows": [1]},
                                       stamps=("updated_on",), now="2026-09-07")
    check("the first write stamps and reports a change", ch is True)
    check("with the measured date", json.loads(f.read_text())["updated_on"] == "2026-09-07",
          f.read_text()[:80])
    _, ch = atomic.write_json_carrying(f, {"schema_version": 1, "updated_on": None,
                                           "rows": [1]},
                                       stamps=("updated_on",), now="2026-09-08")
    check("identical content carries the old stamp forward", ch is False)
    check("so a committed file does not churn on the clock",
          json.loads(f.read_text())["updated_on"] == "2026-09-07", f.read_text()[:80])
    _, ch = atomic.write_json_carrying(f, {"schema_version": 1, "updated_on": None,
                                           "rows": [1, 2]},
                                       stamps=("updated_on",), now="2026-09-08")
    check("changed content takes the new stamp", ch is True and
          json.loads(f.read_text())["updated_on"] == "2026-09-08", f.read_text()[:80])
    check("and the key order is preserved for the diff",
          list(json.loads(f.read_text())) == ["schema_version", "updated_on", "rows"],
          str(list(json.loads(f.read_text()))))


def test_an_unreadable_previous_file_is_stamped_rather_than_trusted() -> None:
    """A truncated document cannot testify to when its content last changed."""
    import atomic
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-stamp-"))
    f = d / "doc.json"
    f.write_text('{"schema_version": 1, "updated_on": "1999-01-01", "rows": [')
    _, ch = atomic.write_json_carrying(f, {"schema_version": 1, "updated_on": None,
                                           "rows": [1]},
                                       stamps=("updated_on",), now="2026-09-07")
    check("it stamps today rather than carrying a stamp it could not read",
          ch is True and json.loads(f.read_text())["updated_on"] == "2026-09-07",
          f.read_text()[:80])


def test_every_json_file_ends_with_a_newline() -> None:
    """Routing the emit through `atomic` rewrote the last line of six tracked
    documents with `\\ No newline at end of file` — a whole-file churn in the
    operator's diff for a byte nobody meant to change. Found by looking at the
    diff the change itself produced."""
    import atomic
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-nl-"))
    f = atomic.write_json(d / "x.json", {"a": 1})
    check("the writer ends its output with a newline",
          f.read_bytes().endswith(b"\n"), repr(f.read_bytes()[-12:]))
    import paths
    # The emitted documents are checked on the emitter's own output below; the
    # synthetic workspace's copies were rewritten by its fixture, not the engine.
    p = paths.REGISTRY / "findings.json"
    check("registry/findings.json ends with one",
          p.is_file() and p.read_bytes().endswith(b"\n"),
          repr(p.read_bytes()[-12:]) if p.is_file() else "absent")


def test_the_emit_no_longer_carries_a_hardcoded_date() -> None:
    import check_paths
    src = check_paths.prose_removed((ROOT / "collectors/emit_registry.py").read_text(
        encoding="utf-8"))
    check("the literal is gone", 'OBS="2026-09-03"' not in src,
          "a constant printed as a measurement")
    check("and the date is computed", "datetime.now" in src, "")
    check("every registry document is written atomically",
          src.count("_stamped(") >= 6 and "atomic.write_json" in src,
          "`write_text` truncates the destination before serialising")
    check("nothing in the emit truncates a registry file directly",
          'INV.joinpath("projects.json").write_text' not in src, "")


def test_the_emit_is_idempotent_over_the_registry() -> None:
    """Two runs, same facts: the tracked documents must be byte-identical.
    A stamp that moved would show up here as six modified files per tick.

    IN A SANDBOX, and the first version was not — it ran the emit against the
    live tree. Two rules caught it in one gate run: `check_paths` refused the
    inlined `ROOT / "registry"`, and the gate's own purity contract forbids a
    `check` step that modifies tracked files. It passed only because the emit
    happens to be idempotent right now; on a tick where the estate had moved it
    would have rewritten the registry from inside the test suite.
    """
    import shutil
    import paths
    # The emit reads the collector receipts under `store/raw/`, which are
    # workspace state: a fresh workspace has a registry and none of them.
    if not live_estate.needs("the collectors' receipts under store/raw/",
                             live_estate.has_receipt("model.json"),
                             "the emit's own invariants are driven against a "
                             "planted model in tests/test_emit_purity.py"):
        return
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-emit-"))
    reg = d / "registry"
    shutil.copytree(paths.REGISTRY, reg)
    env = dict(os.environ, OBSERVATORY_REGISTRY=str(reg))
    first = subprocess.run([PY, "collectors/emit_registry.py"], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=900)
    check("the emit succeeds", first.returncode == 0, (first.stdout + first.stderr)[-300:])
    before = {q.name: q.read_bytes() for q in sorted(reg.glob("*.json"))}
    for name in ("projects.json", "repositories.json", "relations.json", "sources.json"):
        check(f"the emitted registry/{name} ends with a newline",
              name in before and before[name].endswith(b"\n"),
              repr(before.get(name, b"absent")[-12:]))
    second = subprocess.run([PY, "collectors/emit_registry.py"], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=900)
    check("and again", second.returncode == 0, (second.stdout + second.stderr)[-300:])
    after = {q.name: q.read_bytes() for q in sorted(reg.glob("*.json"))}
    moved = [n for n in before if n in after and before[n] != after[n]]
    check("no registry document changed on a second run with the same facts",
          moved == [], f"{moved} — a stamp that moves on every write says nothing")


# ─────────── the watcher can say it stopped ────────────────────────────

def findings_for(d: pathlib.Path, scan_at: str | None) -> list[dict]:
    os.environ["OBSERVATORY_DB"] = str(d / "observatory.db")
    os.environ["OBSERVATORY_REGISTRY"] = str(d / "registry")
    os.environ["OBSERVATORY_SCRATCH"] = str(d / "scratch")
    import paths
    importlib.reload(paths)
    from store import db as store_db
    importlib.reload(store_db)
    conn = store_db.connect()
    with conn:
        conn.execute("DELETE FROM scans")
        if scan_at:
            conn.execute("INSERT INTO scans (id, started_at, collector_version)"
                         " VALUES ('s1', ?, 't')", (scan_at,))
    conn.close()
    import build_findings as B
    importlib.reload(B)
    return [f for f in B.collect() if f["type"] == "scan.stale"]


def test_a_stopped_tick_is_a_finding() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-scan-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "registry/projects.json").write_text('{"projects": []}')
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"

    got = findings_for(d, (now - timedelta(minutes=20)).strftime(fmt))
    check("a running tick raises nothing", got == [], str(got)[:160])
    got = findings_for(d, (now - timedelta(hours=5)).strftime(fmt))
    check("five hours without a scan is a warning",
          len(got) == 1 and got[0]["severity"] == "warning", str(got)[:200])
    if got:
        check("and it says everything else is as old as this",
              "as old as this" in got[0]["detail"], got[0]["detail"][:120])
    got = findings_for(d, (now - timedelta(days=2)).strftime(fmt))
    check("two days is critical",
          len(got) == 1 and got[0]["severity"] == "critical", str(got)[:200])
    got = findings_for(d, None)
    check("a store with no scan at all is critical too",
          len(got) == 1 and got[0]["severity"] == "critical", str(got)[:200])


# ─────────── the page says which stamp is which ────────────────────────

def test_the_page_leads_with_the_measurement() -> None:
    import dashboard_fixture
    page = dashboard_fixture.build(pathlib.Path(tmpdir.mkdtemp(prefix="observatory-fresh-page-")))
    s = page.read_text(encoding="utf-8")
    import re
    D = json.loads(re.search(r"const D = (\{.*?\});", s, re.S).group(1))
    check("the payload carries the measured stamp", bool(D.get("measured")),
          str(sorted(D))[:200])
    check("and the content stamp separately", "updated" in D, str(sorted(D))[:200])
    check("they are different facts and are allowed to differ",
          D.get("measured", "")[:10] != D.get("updated") and D.get("updated") == '2001-01-01' )
    check("the headline word is `Измерено`", "Измерено" in s,
          "`Обновлено` over a content stamp read as freshness")
    check("and the content stamp is labelled as what it is",
          "содержимое" in s and 'id="content-stamp"' in s, "")
    check("a missing measurement says so rather than showing nothing",
          "не измерено" in s, "an empty span reads as zero, not as unknown")


if __name__ == "__main__":
    print("freshness — measured, changed, and still running are three facts\n")
    for fn in (test_a_stamp_moves_only_when_the_content_moves,
               test_an_unreadable_previous_file_is_stamped_rather_than_trusted,
               test_every_json_file_ends_with_a_newline,
               test_the_emit_no_longer_carries_a_hardcoded_date,
               test_the_emit_is_idempotent_over_the_registry,
               test_a_stopped_tick_is_a_finding,
               test_the_page_leads_with_the_measurement):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe headline is a measurement, and the watcher can report its own "
          "silence\033[0m")
