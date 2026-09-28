#!/usr/bin/env python3
"""An erasure that attested itself while the text was still in the file.

`store/retention.py` is the module whose failure `SEVERE_STEPS` grades CRITICAL,
on the ground that "`search` can return text the store considers erased". Its
own first sentence is that an erasure with no audit trail is indistinguishable
from a bug. Both claims were about ROWS, and rows are not where text lives.

**Measured with a canary statement.** Insert one ledger revision plus
its `search_notes` row, both carrying `ZZQQ-erasure-canary-…`, then run
`retention.py apply`:

    receipt   {'status': 'purged', 'before': 1, 'after': 0, 'removed': 1,
               'tombstoned_rows_remaining': 0}
    bytes in observatory.db   before: 2    after: 2    after VACUUM: 1

Two copies before, two after the purge AND a WAL checkpoint. The one surviving
copy after a VACUUM is the ledger's own row, which retention keeps on purpose.
`PRAGMA secure_delete` was 0 — SQLite's default — so a DELETE unlinks the row
and leaves its content in the freed page, recoverable from any copy of the file:
a backup, a rsync, a disk.

The attestation was true about queries and false about bytes, which is the
distinction the whole module exists to hold.

**Three fixes, and the third is the one that keeps the other two honest.**
`secure_delete=ON` at connect; a WAL checkpoint plus VACUUM after any purge that
removed something; and the receipt persisted to `store/raw/retention.json`,
because the attestation the contract demands existed only on stdout, which the
tick pipes into a log nothing reads on a schedule.

**A defect the fix itself produced, recorded because it is the sixth of its
kind.** The byte verdict was first written inside `if any(status != "absent")`,
so a store with no index at all took the `else`, printed "nothing to purge" and
returned 0 with a failed scrub. Both verdicts now sit outside the branch.

**And one the lock test found.** Holding `BEGIN EXCLUSIVE` on a second
connection — what the companion plugin's `Stop` hook does at the end of every
turn, only longer — made the tombstone insert raise `database is locked`, and
`cmd_apply` died with a traceback before writing anything. The one path where
the audit trail matters most produced none.
"""
from __future__ import annotations
import glob, json, os, pathlib, sqlite3, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402
# A private synthetic workspace owns every write below; nothing on the machine
# running the suite is read. Retention is opt-in per workspace, so it is
# enabled here the way an operator would enable it.
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
import paths  # noqa: E402
_settings = paths.CONFIG / "settings.json"
_doc = json.loads(_settings.read_text(encoding="utf-8"))
_doc.setdefault("features", {})["retention"] = True
_settings.write_text(json.dumps(_doc), encoding="utf-8")

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []
NEEDLE = "ZZQQ-erasure-canary-9f3a1c-must-not-survive"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def store(with_index: bool = True, needle: str = NEEDLE) -> tuple[pathlib.Path, dict]:
    """A store holding one ancient `proposed` revision and its index row."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-erase-"))
    (d / "scratch").mkdir()
    env = dict(os.environ, OBSERVATORY_DB=str(d / "observatory.db"),
               OBSERVATORY_SCRATCH=str(d / "scratch"))
    code = (
        "import sys; sys.path.insert(0,'.')\n"
        "from store import db as sdb\n"
        "c = sdb.connect()\n"
        "c.execute(\"INSERT INTO ledger (memory_id, revision, kind, project_id,"
        " function, scope, statement, state, confidence, owner, created_at)"
        " VALUES ('mem:canary',1,'observation','project:x','semantic','project',"
        "?,'proposed',0.5,'agent:test','2020-01-01T00:00:00Z')\", (N,))\n"
        + ("c.execute(\"INSERT INTO search_notes (memory_id, revision, statement,"
           " why) VALUES ('mem:canary',1,?,'why-canary')\", (N,))\n" if with_index else "")
        + "c.commit(); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()\n")
    p = subprocess.run([PY, "-c", f"N = {needle!r}\n" + code], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        raise AssertionError(f"fixture store could not be built: {p.stderr[-300:]}")
    return d, env


def bytes_on_disk(d: pathlib.Path, needle: str = NEEDLE) -> int:
    """Every file of the database, not only the main one.

    The first version of this counted `observatory.db` alone and reported ZERO
    before the purge — the rows were still in the WAL. A measurement that finds
    nothing where the thing certainly is measures the instrument, not the store.
    """
    total = 0
    for f in sorted(glob.glob(str(d / "observatory.db*"))):
        total += pathlib.Path(f).read_bytes().count(needle.encode())
    return total


def apply(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([PY, "store/retention.py", "apply"], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=600)


def receipt(d: pathlib.Path) -> dict:
    f = d / "scratch/retention.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


# ─────────── the canary ────────────────────────────────────────────────

def test_the_erased_text_leaves_the_file() -> None:
    d, env = store()
    before = bytes_on_disk(d)
    check("the instrument finds the canary before anything happens", before == 2,
          f"{before} — one copy in the ledger, one in the index")
    p = apply(env)
    check("the pass succeeds", p.returncode == 0, (p.stdout + p.stderr)[-300:])
    after = bytes_on_disk(d)
    check("only the ledger's own copy survives", after == 1,
          f"{before} -> {after}; the ledger row is KEPT by design, "
          f"a tombstone is not a delete")
    check("the run says the file was scrubbed", "file scrub:" in p.stdout and
          "vacuumed" in p.stdout, p.stdout[-200:])


def test_secure_delete_is_on_for_every_connection() -> None:
    d, env = store(with_index=False)
    p = subprocess.run(
        [PY, "-c", "import sys; sys.path.insert(0,'.')\n"
                   "from store import db as sdb\n"
                   "c = sdb.connect()\n"
                   "print(c.execute('PRAGMA secure_delete').fetchone()[0])"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    check("secure_delete is enabled at connect", p.stdout.strip() == "1",
          f"{p.stdout.strip()!r} — 0 is SQLite's default and leaves content in "
          f"freed pages")
    src = (ROOT / "store/db.py").read_text(encoding="utf-8")
    check("and it is ON rather than FAST", "secure_delete=ON" in src,
          "FAST skips zeroing whenever it would need an extra page read, which "
          "is exactly the content that reaches the freelist")


# ─────────── the receipt outlives the run ──────────────────────────────

def test_the_receipt_is_written_and_complete() -> None:
    d, env = store()
    apply(env)
    r = receipt(d)
    check("a receipt exists", bool(r), "the attestation lived on stdout only")
    if not r:
        return
    for k in ("ran_at", "tombstoned", "events", "observations", "deltas",
              "receipts", "scrub"):
        check(f"it carries `{k}`", k in r, str(sorted(r)))
    check("the projection receipt is in it",
          "search_notes" in (r.get("receipts") or {}), str(r.get("receipts")))
    check("and the byte verdict", (r.get("scrub") or {}).get("scrubbed") is True,
          str(r.get("scrub")))


def test_nothing_removed_is_not_reported_as_scrubbed() -> None:
    """"Nothing to do" and "done" are different claims."""
    d, env = store(with_index=False, needle="unused-canary-text")
    apply(env)                                  # first pass tombstones the row
    p = apply(env)                              # second has nothing left to do
    r = receipt(d)
    check("the second pass says there was nothing to scrub",
          (r.get("scrub") or {}).get("scrubbed") is None, str(r.get("scrub")))
    check("and still exits clean", p.returncode == 0, (p.stdout + p.stderr)[-200:])


# ─────────── the failure paths ─────────────────────────────────────────

def test_a_concurrent_writer_leaves_a_receipt_not_a_traceback() -> None:
    d, env = store()
    blocker = sqlite3.connect(str(d / "observatory.db"), timeout=1)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        p = apply(env)
    finally:
        blocker.rollback()
        blocker.close()
    check("the pass fails loudly", p.returncode == 1, str(p.returncode))
    check("without a traceback", "Traceback" not in p.stderr, p.stderr[-200:])
    check("naming the cause", "database is locked" in p.stderr, p.stderr[-160:])
    r = receipt(d)
    check("and the receipt exists anyway", bool(r),
          "the one path where the audit trail matters most produced none")
    if r:
        check("saying nothing was scrubbed",
              (r.get("scrub") or {}).get("scrubbed") is False, str(r.get("scrub")))
        check("and that the pass did not complete", "failed" in r, str(sorted(r)))
        check("so nothing was erased either",
              "still there" in (r["scrub"]["detail"]), r["scrub"]["detail"][-90:])


def test_the_byte_verdict_is_reached_with_no_index_at_all() -> None:
    """The early-return class, driven: a store with no projection took the
    `else` branch and returned 0 with the scrub verdict unread."""
    src = (ROOT / "store/retention.py").read_text(encoding="utf-8")
    # `check_paths.prose_removed`, NOT `source_reader.code_only`. The second
    # blanks every string literal, and both landmarks here ARE string literals —
    # it reported both as absent, which is the instrument answering about itself.
    # The first blanks comments and docstrings only, which is exactly the view
    # needed to order two statements without a comment about them counting.
    sys.path.insert(0, str(ROOT / "tools"))
    import check_paths
    code = check_paths.prose_removed(src)
    verdict = code.find('scrubbing["scrubbed"] is False')
    branch = code.find('no index is present to purge')
    check("the byte verdict sits AFTER the receipts branch closes",
          verdict > branch > 0, f"verdict at {verdict}, branch at {branch}")
    d, env = store(with_index=False)
    p = subprocess.run([PY, "-c",
                        "import sys; sys.path.insert(0,'.')\n"
                        "from store import db as sdb\n"
                        "c = sdb.connect(); c.execute('DROP TABLE search_notes'); c.commit()"],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    check("the fixture has no index", p.returncode == 0, p.stderr[-200:])
    q = apply(env)
    check("the pass still reports its byte verdict", "file scrub:" in q.stdout,
          q.stdout[-200:])
    check("and the receipt still carries one",
          "scrub" in receipt(d), str(sorted(receipt(d))))


# ─────────── the attested set is derived, not remembered ───────────────

def test_the_attested_tables_are_derived_from_the_schema() -> None:
    import importlib
    d, env = store(with_index=False)
    os.environ["OBSERVATORY_DB"] = env["OBSERVATORY_DB"]
    import paths
    importlib.reload(paths)
    from store import db as sdb
    importlib.reload(sdb)
    from store import retention
    importlib.reload(retention)
    conn = sdb.connect()
    conn.execute("CREATE TABLE brand_new_projection (memory_id TEXT NOT NULL,"
                 " revision INTEGER NOT NULL, statement TEXT)")
    conn.commit()
    found = retention.projection_tables(conn)
    check("a projection added today is attested without editing retention.py",
          "brand_new_projection" in found, str(found))
    check("the canon is not treated as a projection", "ledger" not in found, str(found))
    check("nor the audit trail", "tombstones" not in found, str(found))
    check("nor the queue", "outbox" not in found, str(found))
    for name, why in retention.NOT_A_PROJECTION.items():
        check(f"`{name}` is exempt with a reason", len(why.split()) >= 8, why)
    receipts = retention.purge_projections(conn)
    check("and it appears in the receipt", "brand_new_projection" in receipts,
          str(sorted(receipts)))
    check("vec_notes is named even when the table does not exist",
          "vec_notes" in receipts,
          "omitting it would make 'attested' quietly mean 'the ones I could see'")
    conn.close()


# ─────────── the finding ───────────────────────────────────────────────

def test_an_unscrubbed_erasure_becomes_a_critical_finding() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-erasefind-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()

    def findings(doc: dict) -> list[dict]:
        (d / "scratch/retention.json").write_text(json.dumps(doc), encoding="utf-8")
        p = subprocess.run([PY, "tools/build_findings.py", "--json"], cwd=ROOT,
                           env=dict(os.environ,
                                    OBSERVATORY_REGISTRY=str(d / "registry"),
                                    OBSERVATORY_SCRATCH=str(d / "scratch"),
                                    OBSERVATORY_DB=str(d / "absent.db")),
                           capture_output=True, text=True, timeout=600)
        try:
            return [f for f in json.loads(p.stdout)["findings"]
                    if f["type"] == "erasure.not_scrubbed"]
        except (ValueError, KeyError):
            check("findings built", False, (p.stdout + p.stderr)[-300:])
            return []

    got = findings({"scrub": {"scrubbed": False, "detail": "the file was NOT compacted: x"}})
    check("an unscrubbed erasure is raised", len(got) == 1, str(len(got)))
    if got:
        check("as critical", got[0]["severity"] == "critical", got[0]["severity"])
        check("saying the rows went and the bytes did not",
              "erased but the file was not scrubbed" in got[0]["title"], got[0]["title"])

    got = findings({"failed": "OperationalError: database is locked",
                    "scrub": {"scrubbed": False, "detail": "the pass did not complete"}})
    check("a pass that never ran is raised too", len(got) == 1, str(len(got)))
    if got:
        check("but NOT as an erasure that happened",
              "nothing was erased" in got[0]["title"], got[0]["title"])

    check("a scrubbed pass raises nothing",
          not findings({"scrub": {"scrubbed": True, "detail": "ok"}}))
    check("and so does one with nothing to do",
          not findings({"scrub": {"scrubbed": None, "detail": "nothing removed"}}))
    (d / "scratch/retention.json").unlink()
    p = subprocess.run([PY, "tools/build_findings.py", "--json"], cwd=ROOT,
                       env=dict(os.environ, OBSERVATORY_REGISTRY=str(d / "registry"),
                                OBSERVATORY_SCRATCH=str(d / "scratch"),
                                OBSERVATORY_DB=str(d / "absent.db")),
                       capture_output=True, text=True, timeout=600)
    check("an absent receipt is not a finding",
          "erasure.not_scrubbed" not in p.stdout,
          "every fresh clone has none, and a critical finding on clone is noise")


if __name__ == "__main__":
    print("the erasure — the rows, and then the bytes\n")
    for fn in (test_the_erased_text_leaves_the_file,
               test_secure_delete_is_on_for_every_connection,
               test_the_receipt_is_written_and_complete,
               test_nothing_removed_is_not_reported_as_scrubbed,
               test_a_concurrent_writer_leaves_a_receipt_not_a_traceback,
               test_the_byte_verdict_is_reached_with_no_index_at_all,
               test_the_attested_tables_are_derived_from_the_schema,
               test_an_unscrubbed_erasure_becomes_a_critical_finding):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32man erasure now removes the text, not only the row that pointed at it\033[0m")
