#!/usr/bin/env python3
"""Two collectors' honesty, and whether anybody hears it.

`collectors/scan_sessions.py` reads a store this repository does not own: the
session companion's SQLite database, whose tables, column names and units belong
to a third-party plugin that updates on its own. The collector handles a moved shape
honestly — `sqlite3.Error` becomes a `degraded` entry naming the exact columns it
needs — and two things were missing around that.

**Nobody read the list.** Of the receipts under the scratch directory that carry
a `degraded` key, `model.json` reached the operator's board through its own
tailored rule, `bitbucket.json` and `domains_live.json` reached the WIRE through
`survey._collector_degradation`, and `sessions.json` and `local.json` reached
nobody. So a collector could report that the companion's store was unreadable,
and the estate would lose the session half of its activity in silence.

**And a window that matches nothing read as an empty estate.**
`created_at_epoch` is the companion's field in the companion's unit —
milliseconds — and the cutoff multiplies by 1000 on that basis. A switch to seconds upstream would put every row below the cutoff:
the query succeeds, returns nothing, and the collector reports `sessions: 0` with
no degradation, while `activity.py` calls projects `cooling` on that silence. The
guard is structural rather than a unit check — rows in the table and none in the
window means the filter has stopped matching, whatever the cause.

The sessions collector is an integration, off by default, so each scan here runs
in a private workspace that enables it.
"""
from __future__ import annotations
import json
import os
import pathlib
import sqlite3
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup                 # noqa: E402
portable_setup()
import tmp as tmpdir                                                  # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def mem_store(epoch_unit: str) -> pathlib.Path:
    """A companion store of our own, with the timestamps in a chosen unit.

    Built rather than mocked: the subject is whether this collector survives
    somebody else's schema decision, and a stub would prove nothing about
    sqlite.
    """
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-foreign-"))
    db = d / "claude-mem.db"
    c = sqlite3.connect(db)
    c.executescript(
        "CREATE TABLE session_summaries (memory_session_id TEXT, project TEXT,"
        " created_at TEXT, created_at_epoch INTEGER);"
        "CREATE TABLE observations (project TEXT, files_read TEXT,"
        " files_modified TEXT, created_at_epoch INTEGER);")
    import time
    now = time.time()
    for i in range(4):
        v = int(now * (1000 if epoch_unit == "ms" else 1))
        c.execute("INSERT INTO session_summaries VALUES (?,?,?,?)",
                  (f"s{i}", "alpha-web", "2026-09-08T00:00:00Z", v))
    c.commit()
    c.close()
    return db


def run_scan(store: pathlib.Path) -> dict:
    """The collector as the tick runs it: a subprocess, with its own scratch.

    **And its own STORE.** A version that redirected the scratch and the
    companion database but not `OBSERVATORY_DB` inserted a row into the
    workspace's `scans` table on every run, while the gate was measuring whether
    it had stayed still.

    **And its own WORKSPACE**, whose settings enable the sessions integration:
    the engine keeps every integration off until a workspace turns it on.
    """
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-scan-"))
    import workspace
    home = d / "home"
    workspace.initialize(home, identities=False)
    settings = home / "config/settings.json"
    doc = json.loads(settings.read_text(encoding="utf-8"))
    doc["integrations"]["sessions"] = True
    settings.write_text(json.dumps(doc), encoding="utf-8")
    out = d / "sessions.json"
    env = {**os.environ, "OBSERVATORY_HOME": str(home), "OBSERVATORY_SCRATCH": str(d),
           "OBSERVATORY_REGISTRY": str(home / "registry"),
           "OBSERVATORY_STATE": str(home / "store"),
           "OBSERVATORY_DB": str(d / "observatory.db"),
           "CLAUDE_MEM_DB": str(store)}
    p = subprocess.run([PY, "collectors/scan_sessions.py", str(out)], cwd=ROOT,
                       env=env, capture_output=True, text=True, timeout=600)
    if not out.is_file():
        return {"_failed": (p.stdout + p.stderr)[-300:]}
    return json.loads(out.read_text(encoding="utf-8"))


# ─────────── the window that matches nothing ───────────────────────────

def test_a_window_matching_nothing_is_a_degradation_not_an_empty_estate() -> None:
    src = (ROOT / "collectors/scan_sessions.py").read_text(encoding="utf-8")
    check("the collector guards the case at all",
          "stopped matching" in src,
          "rows in the table and none in the window is the shape of a moved "
          "column or a changed unit, and it read as an empty estate")
    check("and the guard is structural, not a unit check",
          "SELECT count(*) FROM session_summaries" in src,
          "counting the table is what tells an empty window from an empty store")
    check("it names the unit it assumes, so the next reader can check it",
          "milliseconds" in src, "")
    # DRIVEN, because the seam now exists. The probe used to grep the COLLECTOR
    # for the env var and skipped — while the variable lives in `paths.py`,
    # where every redirectable location does. Asking the resolver is the
    # behavioural question; grepping a file for a name is a guess about which
    # file.
    import importlib
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-redirect-"))
    os.environ["CLAUDE_MEM_DB"] = str(d / "elsewhere.db")
    sys.modules.pop("paths", None)
    import paths as _p
    redirectable = str(_p.COMPANION_DB) == str(d / "elsewhere.db")
    os.environ.pop("CLAUDE_MEM_DB", None)
    sys.modules.pop("paths", None)
    importlib.invalidate_caches()
    check("the companion's store is redirectable, so the guard can be driven",
          redirectable, str(_p.COMPANION_DB))
    if not redirectable:
        return
    got = run_scan(mem_store("s"))
    if "_failed" in got:
        check("the collector runs against a redirected store", False, got["_failed"])
        return
    check("a seconds-unit store yields zero sessions", got["counts"]["sessions"] == 0,
          str(got["counts"]))
    reasons = " ".join(d.get("reason", "") for d in got.get("degraded") or [])
    check("and says the filter stopped matching rather than reporting silence",
          "stopped matching" in reasons, reasons[:200])


# ─────────── every collector's own words reach a person ────────────────

def test_every_receipt_with_a_degraded_list_has_a_reader() -> None:
    import degradations
    fn = getattr(degradations, "every_collector", None)
    if fn is None:
        check("degradations.every_collector exists", False,
              "four of six receipts had no reader for their own degradations")
        return
    got = fn()
    check("it returns a mapping of receipt to entries", isinstance(got, dict), str(type(got)))
    check("`model.json` is excluded, because it has a tailored rule",
          "model.json" not in got and "model.json" in degradations.OWN_READER,
          str(sorted(got)))
    check("and the exclusion carries its reason",
          len(degradations.OWN_READER.get("model.json", "").split()) >= 8,
          degradations.OWN_READER.get("model.json", ""))
    check("a receipt with no `degraded` key is not treated as a collector report",
          all("tick.json" != k and "integrity.json" != k for k in got), str(sorted(got)))


def test_the_board_carries_them_and_does_not_cry_wolf() -> None:
    """Planted: a collector receipt in the synthetic workspace that carries its
    own degradation, so the row exists and its shape can be read."""
    import paths
    reason = ("the companion's session table was readable but none of its rows "
              "fell inside the window, so the filter has stopped matching; a "
              "moved column or a changed unit reads exactly like this, and an "
              "empty estate would not")
    # The receipt's integration is ON: one left by an integration that is off is
    # a leftover, not a measurement (degradations.integration_off).
    settings = paths.HOME / "config/settings.json"
    doc = json.loads(settings.read_text(encoding="utf-8"))
    doc.setdefault("integrations", {})["sessions"] = True
    settings.write_text(json.dumps(doc), encoding="utf-8")
    (paths.SCRATCH / "sessions.json").write_text(json.dumps({
        "counts": {"sessions": 0, "projects": 0},
        "degraded": [{"source": "sessions", "reason": reason}]}), encoding="utf-8")
    import build_findings as B
    rows = [f for f in B.collect() if f["type"] == "collector.degraded"]
    check("a planted collector degradation reaches the board", bool(rows), "")
    if rows:
        check("every row is info, not warning",
              all(f["severity"] == "info" for f in rows),
              str({f["subject"]: f["severity"] for f in rows}))
        check("each names the receipt it came from",
              all(f["subject"].startswith("collector:") for f in rows),
              str([f["subject"] for f in rows]))
        check("and quotes the collector's own reason",
              all(len(f["detail"]) > 200 for f in rows),
              str([len(f["detail"]) for f in rows]))
    src = (ROOT / "tools/build_findings.py").read_text(encoding="utf-8")
    check("the row caps its list and counts the remainder",
          "DEGRADED_LISTED" in src and "not listed" in src,
          "a truncated list that does not say so reads as complete")


if __name__ == "__main__":
    print("foreign vocabulary — somebody else's schema, and who hears about it\n")
    for fn in (test_a_window_matching_nothing_is_a_degradation_not_an_empty_estate,
               test_every_receipt_with_a_degraded_list_has_a_reader,
               test_the_board_carries_them_and_does_not_cry_wolf):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma moved schema is reported, and somebody reads the report\033[0m")
