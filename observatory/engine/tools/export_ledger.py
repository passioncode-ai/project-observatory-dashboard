#!/usr/bin/env python3
"""Write the ledger out as text, because it lived in exactly one place.

WHY THIS EXISTS. `store/db.py` opened with "The store is rebuildable: MEASURED
tables come from the machine, DERIVED tables from CANONICAL. Only the ledger is
authored, and it is exported to the wiki." The first two clauses are true —
`scan_events.py` rebuilt 8947 events from git in seconds. The third was **false**:
nothing exported the ledger anywhere, and `project_into_vault.py` has never
mentioned it.

So on 2026-09-06, when the store was found corrupt — 434 MB with no SQLite
header — the only copy of what the agent had concluded, and of every session's
recorded `why`, was inside it. It came back only because `.recover` happened to
find the pages intact. That is luck, and a sentence claiming a backup exists is
worse than no sentence: it is precisely what stops anyone from adding one.

WHAT IS AND IS NOT COVERED. Every ledger revision and every tombstone, as JSON
Lines, in the repository and therefore in git. Events, scans, deltas and the
vector index are NOT exported and do not need to be: they are derived from the
machine and rebuilt by re-running the collectors.

    export_ledger.py            write registry/ledger.jsonl
    export_ledger.py --check    audit the export against the store; non-zero only
                                when the export is WRONG, not merely younger

WHAT `--check` ASSERTS, AND WHY IT IS NOT "IS IT CURRENT".

It was byte equality, and it went red twice in one session with nothing wrong.
The companion plugin's `Stop` hook writes a ledger row at the end of every turn
of every session working in a watched project, and the tick's agent writes more —
so an export is stale seconds after it is written. A gate that is routinely red
for a non-defect is a gate people learn to skip, and then the real case, an
exporter that has stopped running altogether, arrives to an audience that has
stopped looking.

So the gate asserts what has to be TRUE rather than what happens to be CURRENT:

* **Consistency** — every revision present in both must be identical. A
  difference means the export or the store diverged, which is corruption or a
  rewritten history, and it fails.
* **Completeness up to its own moment** — a row the export lacks fails only if
  it is older than the grace window. A row written in the last few minutes is
  simply younger than the export; a row written two hours ago and still absent
  means nothing is exporting any more.
* **Rows the export has and the store does not are REPORTED, never failed.**
  That is the export doing its job after a store loss — which is the case it was
  written for — and failing it would block a recovery.
"""
from __future__ import annotations
import hashlib, json, pathlib, sqlite3, sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import atomic
import paths                                                                      
from store import db as store_db                                                  
from store.ledger import WORKFLOW_KINDS

OUT = paths.REGISTRY / "ledger.jsonl"
HEADER = {
    "_export": "ledger",
    "_note": ("Every revision of the append-only ledger, and every tombstone, as "
              "JSON Lines so a diff shows one record per line. Written by "
              "tools/export_ledger.py on every tick. This is the ONLY copy of the "
              "ledger outside store/observatory.db, which is gitignored — the "
              "store was found corrupt on 2026-09-06 and recovered by luck."),
    "_tombstoned_text_is_here": (
        "A TOMBSTONED REVISION'S TEXT IS IN THIS FILE, and this file is committed. "
        "A tombstone hides a revision from every READ — `ledger.live()`, the "
        "lexical index and the vector index, measured 2026-09-07 — and keeps it in "
        "canon, because retention never deletes a ledger row: an erasure with no "
        "audit trail is indistinguishable from a bug. The export mirrors canon, so "
        "it keeps it too, and git history keeps the export. If text must be "
        "unrecoverable rather than unreadable, a tombstone is not the mechanism "
        "and this project does not have one — revisions are immutable by design, "
        "so nothing here can rewrite one."),
}


def rows(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM ledger ORDER BY memory_id, revision"):
        row = {"_kind": "revision", **{k: r[k] for k in r.keys()}}
        # A WORKFLOW'S BODY STAYS IN THE STORE. A checkpoint body is up to 64 KB
        # per step and a handoff pack up to 128 KB, with local paths and file
        # names; this file is committed. The row stays — who, when, which step —
        # and the body is named by its SHA-256, so a restore can tell whether
        # the store still holds the same one.
        if r["kind"] in WORKFLOW_KINDS and row.get("body_json") is not None:
            row["body_sha256"] = hashlib.sha256(row["body_json"].encode()).hexdigest()
            row["body_json"] = None
        out.append(row)
    try:
        for r in conn.execute("SELECT * FROM tombstones ORDER BY memory_id, revision"):
            out.append({"_kind": "tombstone", **{k: r[k] for k in r.keys()}})
    except sqlite3.Error:
        pass
    return out


def render(data: list[dict]) -> str:
    lines = [json.dumps(HEADER, ensure_ascii=False, sort_keys=True)]
    lines += [json.dumps(d, ensure_ascii=False, sort_keys=True) for d in data]
    return "\n".join(lines) + "\n"


#: Two tick intervals. The tick exports on every run, so a row still unexported
#: after two of them means the exporter is not running — while a row written in
#: the last few minutes means only that the export is younger than it.
GRACE_SECONDS = 3600


def key(row: dict) -> tuple:
    return (row.get("_kind"), row.get("memory_id"), row.get("revision"))


def read_export() -> dict[tuple, dict]:
    if not OUT.is_file():
        return {}
    out = {}
    for line in OUT.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("_kind"):
            out[key(row)] = row
    return out


def age_seconds(created_at: str | None) -> float:
    """Seconds since the row was created. Unparseable or absent reads as OLD, so
    a row whose age cannot be established is never waved through as recent."""
    if not created_at:
        return float("inf")
    try:
        made = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return float("inf")
    return (datetime.now(timezone.utc) - made).total_seconds()


def audit(stored: list[dict]) -> tuple[int, list[str]]:
    """(exit code, lines to print). See the module docstring for the contract."""
    have, want = read_export(), {key(r): r for r in stored}
    missing = [want[k] for k in want.keys() - have.keys()]
    extra = [have[k] for k in have.keys() - want.keys()]
    differing = [k for k in want.keys() & have.keys() if want[k] != have[k]]

    stale = [r for r in missing if age_seconds(r.get("created_at")) > GRACE_SECONDS]
    fresh = len(missing) - len(stale)
    out, code = [], 0

    if differing:
        code = 1
        out.append(f"DIVERGED: {len(differing)} record(s) differ between the store and the "
                   f"export — {differing[:2]}. That is not staleness; one of the two was "
                   f"rewritten or damaged.")
    if stale:
        oldest = max(age_seconds(r.get("created_at")) for r in stale)
        code = 1
        out.append(f"NOT EXPORTED: {len(stale)} record(s) older than {GRACE_SECONDS // 60} "
                   f"minutes are missing from the export, the oldest by "
                   f"{oldest / 3600:.1f} hours. Nothing is exporting. "
                   f"Run tools/export_ledger.py.")
    if extra:
        out.append(f"note: the export holds {len(extra)} record(s) the store does not. That is "
                   f"the export doing its job after a store loss, not a fault.")
    if fresh:
        out.append(f"{fresh} record(s) written since the last export, all within the "
                   f"{GRACE_SECONDS // 60}-minute grace — the next tick exports them.")
    if not out:
        out.append(f"ledger export current: {len(want)} record(s)")
    return code, out


def main(argv: list[str]) -> int:
    if "--check" in argv:
        # READ-ONLY on purpose. `store_db.connect()` opens read-write and, since
        # migrations were added, applies them — so a gate step would have been a
        # writer into the very store it is auditing.
        conn = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        code, lines = audit(rows(conn))
        conn.close()
        for line in lines:
            print(line, file=sys.stderr if code else sys.stdout)
        return code

    conn = store_db.connect()
    text = render(rows(conn))
    n = text.count("\n") - 1
    conn.close()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(OUT, text)
    # The registry lives in the private workspace, not under the program: name it
    # relative to the source only in a checkout that keeps data beside the code.
    shown = OUT.relative_to(ROOT) if OUT.is_relative_to(ROOT) else OUT
    print(f"ledger -> {shown} ({n} record(s), "
          f"{OUT.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
