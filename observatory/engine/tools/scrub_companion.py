#!/usr/bin/env python3
"""Take the estate's credential VALUES out of the companion's memory.

    tools/scrub_companion.py            # backup, scrub both stores, journal
    tools/scrub_companion.py --dry-run  # count only, write nothing

WHY THIS EXISTS. A memory companion (claude-mem) summarises every agent
session into its SQLite database and embeds the text into a Chroma store. Both
keep whatever a transcript held. The companion's own redactor knows key SHAPES
(`sk-…`, a private-key block), not this workspace's values: a database
password inside a URL or a webhook secret gets through. The workspace's own
list of known values is the only filter that can catch them.

WHAT IT DOES. For every value the leak scanner already knows — the env
inventory in `store/raw/env.json`, the vault's slots, the installed keys, the
same `scan_leaks.known_values()` — every text column of
every table in both stores is rewritten with `replace(col, value,
'[REDACTED:<NAME>]')`. FTS mirrors follow: claude-mem's FTS tables have
AFTER UPDATE triggers, Chroma's contentful FTS5 table is updated directly.
Embedding vectors remain. This tool does not prove erasure from embeddings,
backups, caches or other copies; exposed credentials still require rotation.

BEFORE ANY WRITE: a SQLite-API backup of each store it will change, into the
companion's `backups/` directory, mode 600. The companion's worker may hold
both files open; `busy_timeout` waits for it, and WAL keeps reads working.

HOW OFTEN IT READS (PB-125): one complete pass, then only rows added since, per
table; see the PB-125 block below and docs/ONBOARDING.md, "Enter credentials
locally".

WHAT IT NEVER DOES: print a value, store a value in its journal, or touch a
row that holds none. The journal (`store/logs/scrub.jsonl`) carries names,
`table.column`, counts and the backup's path.
"""
from __future__ import annotations
import argparse
import datetime
import json
import os
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "collectors"))
import paths  # noqa: E402
import sqlite_scan  # noqa: E402

HOME = pathlib.Path(os.environ.get("CLAUDE_MEM_HOME", paths.source_path("companion_home", paths.HOME / "disabled/companion")))
STORES = [HOME / "claude-mem.db", HOME / "chroma" / "chroma.sqlite3"]
JOURNAL = paths.STATE / "logs" / "scrub.jsonl"
BACKUP_DIR = HOME / "backups"
#: Every database modified by remediation must have its own SQLite backup.
BACKED_UP = ("claude-mem.db", "chroma.sqlite3")


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def text_columns(conn: sqlite3.Connection) -> list[tuple[str, list[str]]]:
    """(table, [text columns]) for every table that can hold prose — FTS
    shadow tables (`_data`, `_idx`, `_config`, `_docsize`, `_content`) are the
    index's own storage and follow their parent."""
    out = []
    for name, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        if any(name.endswith(s) for s in ("_data", "_idx", "_config", "_docsize", "_content")):
            continue
        # An EXTERNAL-CONTENT FTS table (`content='observations'`) is a view over
        # its parent, kept by the parent's AFTER UPDATE triggers: rewriting the
        # parent rewrites it, and SQLite refuses a direct UPDATE on it. A contentful
        # FTS table (Chroma's trigram index, no `content=`) holds its own copy and
        # has no trigger, so it is rewritten directly.
        low = (sql or "").lower()
        if "using fts" in low and "content=" in low.replace(" ", ""):
            continue
        try:
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')
                    if str(r[2] or "").upper() in ("TEXT", "", "VARCHAR", "CLOB", "JSON")]
        except sqlite3.Error:
            continue
        if cols:
            out.append((name, cols))
    return out


def backup(db: pathlib.Path) -> pathlib.Path | None:
    if db.name not in BACKED_UP:
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    BACKUP_DIR.chmod(0o700)
    import uuid
    dest = BACKUP_DIR / f"{db.stem}-pre-scrub-{now().replace(':', '-')}-{uuid.uuid4().hex[:12]}.db"
    fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    dest.chmod(0o600)
    return dest


def _needles(values: dict[str, str]) -> dict[bytes, tuple[str, str]]:
    return {v.encode("utf-8", "surrogateescape"): (v, n) for v, n in values.items() if v}


def scan_store(db: pathlib.Path, values: dict[str, str], since: dict[str, int] | None = None
               ) -> tuple[list[dict], list[tuple], dict[str, int], int, str | None]:
    """Read one store once, read-only: (hits, todo, high-water rowids, rows read, problem).

    `since` maps table -> the highest rowid a previous complete pass read; only
    rows above it are read (PB-125). `todo` is (table, col, rowid, value, name)
    for every cell to rewrite. Values are tested against a whole page of cells
    joined at once and only a page that holds one is examined cell by cell: the
    per-cell loop over every value is what took a tick past half an hour on a
    5 GB Chroma store.
    """
    since = since or {}
    if not db.is_file():
        return [], [], {}, 0, None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
        conn.execute("PRAGMA busy_timeout = 60000")
    except sqlite3.Error as exc:
        return [], [], {}, 0, f"{type(exc).__name__}: {exc}"
    needles = _needles(values)
    hits: dict[tuple[str, str], int] = {}
    todo: list[tuple] = []
    high: dict[str, int] = {}
    read = 0
    try:
        for table, cols in text_columns(conn):
            sel = ", ".join(f'"{c}"' for c in cols)
            floor = int(since.get(table, 0))
            try:
                floor = sqlite_scan.floor_for(conn, table, floor)
                # rowid order makes the high-water mark the last row read.
                cur = conn.execute(f'SELECT rowid, {sel} FROM "{table}" WHERE rowid > ? ORDER BY rowid', (floor,))
            except sqlite3.Error:
                continue    # WITHOUT ROWID or virtual: not addressable by rowid, not rewritable here
            top = floor
            while True:
                page = cur.fetchmany(500)
                if not page:
                    break
                read += len(page)
                top = max(top, page[-1][0])
                blobs = [[None if c is None else (c if isinstance(c, bytes) else str(c).encode("utf-8", "surrogateescape"))
                          for c in row[1:]] for row in page]
                joined = b"\0".join(b for cells in blobs for b in cells if b)
                present = [nb for nb in needles if nb in joined]
                if not present:
                    continue
                for row, cells in zip(page, blobs):
                    for col, blob in zip(cols, cells):
                        if not blob:
                            continue
                        for nb in present:
                            if nb in blob:
                                value, name = needles[nb]
                                hits[(f"{table}.{col}", name)] = hits.get((f"{table}.{col}", name), 0) + 1
                                todo.append((table, col, row[0], value, name))
            high[table] = top
    except sqlite3.Error as exc:
        return _rows(hits), todo, {}, read, f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()
    return _rows(hits), todo, high, read, None


def _rows(hits: dict[tuple[str, str], int]) -> list[dict]:
    return [{"where": where, "name": name, "cells": n} for (where, name), n in sorted(hits.items())]


def apply_todo(db: pathlib.Path, todo: list[tuple]) -> str | None:
    """Rewrite the cells `scan_store` found, by rowid, in one transaction.

    replace() is idempotent, so a cell rewritten between scan and apply is
    left as it is; a problem rolls the whole store back.
    """
    try:
        conn = sqlite3.connect(f"file:{db}?mode=rw", uri=True, timeout=60)
    except sqlite3.Error as exc:
        return f"{type(exc).__name__}: {exc}"
    try:
        conn.execute("PRAGMA busy_timeout = 60000")
        for table, col, rowid, value, name in todo:
            conn.execute(f'UPDATE "{table}" SET "{col}" = replace("{col}", ?, ?) WHERE rowid = ?',
                         (value, f"[REDACTED:{name}]", rowid))
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        return f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()
    return None


def scrub_store(db: pathlib.Path, values: dict[str, str], dry: bool) -> tuple[list[dict], str | None]:
    """[{table.column, name, cells}] for one store, fully scanned, and a problem.

    Kept for callers that want a whole-store answer; the tick goes through
    `main`, which scans incrementally.
    """
    rows, todo, _high, _read, problem = scan_store(db, values)
    if problem or dry or not todo:
        return rows, problem
    return rows, apply_todo(db, todo)


# PB-125. When to read every row and when only new ones is decided in
# tools/sqlite_scan.py, shared with the leak scan (PB-131). This tool keeps its
# mark in its own file.
WATERMARK = paths.STATE / "scrub-watermark.json"
FULL_EVERY = sqlite_scan.FULL_EVERY


def values_digest(values: dict[str, str]) -> str | None:
    return sqlite_scan.values_digest(values)


def load_watermark() -> dict:
    try:
        doc = json.loads(WATERMARK.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def save_watermark(doc: dict) -> None:
    WATERMARK.parent.mkdir(parents=True, exist_ok=True)
    tmp = WATERMARK.with_name(WATERMARK.name + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
    os.replace(tmp, WATERMARK)


def plan(values: dict[str, str], force_full: bool, today: datetime.date) -> tuple[bool, str | None, dict, str]:
    """(complete pass?, digest, watermark, why)."""
    digest = values_digest(values)
    mark = load_watermark()
    full, why = sqlite_scan.plan(mark, digest, force_full, today)
    return full, digest, mark, why


def journal(entry: dict) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    JOURNAL.chmod(0o600)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="count, write nothing")
    ap.add_argument("--full", action="store_true", help="read every row, not only rows added since the last pass")
    a = ap.parse_args(argv[1:])
    import configuration
    if not a.dry_run and not configuration.enabled("companion_remediation", section="features"):
        print("scrub: remediation is disabled; use --dry-run or explicitly enable companion_remediation")
        return 0
    import scan_leaks
    values, _homes, _skipped = scan_leaks.known_values()
    if not values:
        print("scrub: nothing to look for — no env scan, no vault, no installed key")
        return 0
    today = datetime.datetime.now(datetime.timezone.utc).date()
    full, digest, mark, why = plan(values, a.full or a.dry_run, today)
    stores_mark = {} if full else dict(mark.get("stores") or {})
    new_mark = {"values": digest, "full_on": today.isoformat() if full else mark.get("full_on"),
                "stores": dict(mark.get("stores") or {})}
    mode = "full" if full else "incremental"
    total_cells, report, clean_run = 0, [], True
    for db in STORES:
        if not db.is_file():
            continue
        rows, todo, high, read, problem = scan_store(db, values, stores_mark.get(str(db)))
        cells = sum(r["cells"] for r in rows)
        if problem:
            clean_run = False
            print(f"scrub: {db.name}: would not open — {problem}", file=sys.stderr)
            if not a.dry_run:
                journal({"at": now(), "store": str(db), "mode": mode, "problem": problem})
            continue
        if not cells:
            print(f"scrub: {db.name}: clean — {len(values)} known value(s), {read} row(s) read ({mode}: {why})")
            new_mark["stores"][str(db)] = {**(stores_mark.get(str(db)) or {}), **high}
            continue
        if a.dry_run:
            print(f"scrub: {db.name}: {cells} cell(s) across {len({r['where'] for r in rows})} column(s) hold "
                  f"{len({r['name'] for r in rows})} distinct value(s) — dry run, nothing written")
            for r in sorted(rows, key=lambda r: -r["cells"])[:12]:
                print(f"    {r['where']:48} {r['name']:50} ×{r['cells']}")
            total_cells += cells
            continue
        bak = backup(db)
        problem = apply_todo(db, todo)
        journal({"at": now(), "store": str(db), "mode": mode, "rows_read": read,
                 "backup": str(bak) if bak else None,
                 "cells": cells, "rows": rows, "problem": problem})
        print(f"scrub: {db.name}: {cells} cell(s) rewritten ({mode}, {read} row(s) read)"
              + (f", backup {bak.name}" if bak else "") + (f" — problem: {problem}" if problem else ""))
        if problem:
            clean_run = False
        else:
            new_mark["stores"][str(db)] = {**(stores_mark.get(str(db)) or {}), **high}
        total_cells += cells
        report.extend(rows)
    # A dry run never moves the watermark; a failed store keeps its old marks,
    # and a complete pass that failed anywhere is not recorded as complete.
    if not a.dry_run and digest is not None:
        if full and not clean_run:
            new_mark["full_on"] = mark.get("full_on")
            new_mark["values"] = mark.get("values")
        save_watermark(new_mark)
    if not a.dry_run and report:
        print(f"scrub: {len({r['name'] for r in report})} distinct value(s) replaced by name; "
              f"re-run `project-observatory full leaks` to see the store read clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
