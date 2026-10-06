#!/usr/bin/env python3
"""Forget a record: withdraw it, erase its text everywhere this machine can reach, and
say exactly where it could not (PB-137 N-013).

Two acts, kept apart because they promise different things:

- **Withdrawal** (`ledger.tombstone`, unchanged): the record leaves every read path and
  the lexical index; its revisions stay in the ledger as the audit trail. Unreadable,
  not unrecoverable — `store/retention.py` documents why that is the default.
- **Erasure** (this module, the operator's act at a terminal): on top of withdrawal, the
  TEXT of every revision is replaced in the ledger (`statement`, `why`, the body, the
  evidence) by a marker, and every copy of it this engine made is found and replaced or
  dropped. The ledger rows keep their identity — id, revision, kind, project, owner,
  times, class — so the audit trail still says that something was recorded, by whom and
  when, and that it was erased, by whom and why; it no longer says what.

The receipt names each backend and what happened there:

| backend | what erasure does |
|---|---|
| `ledger` | text of every revision replaced; ids and audit columns kept |
| `tombstones` | every revision tombstoned (withdrawal) |
| projections (`search_notes`, `vec_notes`, …) | rows of the record deleted and attested empty (`retention.purge_projections`) |
| `handoff-packs` | a pack's copy of the record (as its checkpoint or a related record) replaced by the marker |
| `idempotency` | cached answers that quote the record dropped (a retry then runs again rather than replaying) |
| `file` | WAL checkpointed and the file vacuumed, so no freed page holds the text (`retention.scrub`) |
| `export` | `registry/ledger.jsonl` rewritten now and checked to hold no erased text |
| `backups`, `encrypted-backups`, `migration-backups` | **retained, unverified**: encrypted or plain copies taken before the erasure keep the text until they rotate out |
| `workspace-snapshots` | **retained**: plaintext snapshot folders under `<home>/backups/` (taken without a passphrase) keep it until they rotate or `full backups migrate` moves them |
| `failed-update-copies` | **retained**: the whole workspace a rolled-back update kept beside this one (`<home>.failed-update-*`) keeps it until a person deletes that copy |
| `git-history` | **retained, unverified**: earlier commits of the export keep it |

`complete` is true only when nothing is retained or unverified, so a receipt never
reports an erasure complete while a backup still holds the text. Erasing again is safe:
every backend is checked again and the receipt says what changed.

The checkpoint of an OPEN workflow is refused, as withdrawal refuses it: it is the
workflow's state. Close the workflow first.

Every receipt is appended to `store/logs/forget.jsonl` (0600). It names the record, the
reason, the approver and the backends, never the erased text.
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MARKER = "[forgotten]"
JOURNAL = "forget.jsonl"
RETAINED = ("retained", "unverified")


class ForgetError(Exception):
    """The record cannot be forgotten as asked; nothing was changed."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _texts(conn: sqlite3.Connection, memory_id: str) -> list[str]:
    """The record's own text, every revision: its statement, its why and the prose of its
    body (a checkpoint's goal, decisions, constraints, notes). What must not survive."""
    import textkeys
    out = []
    for r in conn.execute("SELECT statement, why, body_json FROM ledger WHERE memory_id = ?",
                          (memory_id,)):
        values = [r["statement"], r["why"]]
        if r["body_json"]:
            try:
                body = json.loads(r["body_json"])
            except ValueError:
                body = {}
            if isinstance(body, dict) and isinstance(body.get("goal"), str):
                values.append(body["goal"])
            values += [text for _path, text in textkeys.body_chunks(body)]
        for value in values:
            if isinstance(value, str) and value.strip() and value != MARKER:
                out.append(value)
    return sorted(set(out), key=len, reverse=True)


#: A word shorter than this is too common to prove anything by its presence.
TOKEN_MIN = 5


def _unique_tokens(conn: sqlite3.Connection, texts: list[str]) -> list[str]:
    """Lowercased words of the erased text that no remaining ledger text contains. Their
    presence anywhere in the store — the full-text index's binary segments included — is
    proof the text survived; a word another record also uses proves nothing."""
    import textkeys
    mine = {w.lower() for t in texts for w in textkeys._WORD.findall(t) if len(w) >= TOKEN_MIN}
    if not mine:
        return []
    others: set[str] = set()
    for r in conn.execute("SELECT statement, why, body_json, evidence_json, provenance_json"
                          " FROM ledger"):
        for value in r:
            if isinstance(value, str) and value:
                others.update(w.lower() for w in textkeys._WORD.findall(value))
    return sorted(mine - others)


def plan(conn: sqlite3.Connection, memory_id: str) -> dict:
    """What erasing `memory_id` would touch. Reads only."""
    rows = list(conn.execute("SELECT revision, kind, project_id, workflow_id FROM ledger"
                             " WHERE memory_id = ? ORDER BY revision", (memory_id,)))
    if not rows:
        raise ForgetError(f"{memory_id} does not exist")
    packs = _packs_quoting(conn, memory_id)
    cached = _idempotency_quoting(conn, memory_id, _texts(conn, memory_id))
    return {"memoryId": memory_id, "revisions": [r["revision"] for r in rows],
            "kind": rows[-1]["kind"], "projectId": rows[-1]["project_id"],
            "handoffPacks": [p for p, _ in packs], "idempotencyAnswers": len(cached),
            "note": "erasure replaces the text of every revision and of every copy listed; "
                    "backups and git history of the export keep it until they rotate"}


def _packs_quoting(conn: sqlite3.Connection, memory_id: str) -> list[tuple[str, int]]:
    """Handoff packs (every revision) whose body carries this record."""
    found = []
    for r in conn.execute("SELECT memory_id, revision, body_json FROM ledger WHERE kind = 'handoff'"
                          " AND body_json LIKE ?", (f"%{memory_id}%",)):
        found.append((r["memory_id"], r["revision"]))
    return found


def _idempotency_quoting(conn: sqlite3.Connection, memory_id: str, texts: list[str]) -> list[tuple]:
    try:
        rows = list(conn.execute("SELECT principal, operation, key, response_json FROM idempotency"))
    except sqlite3.Error:
        return []
    hits = []
    for r in rows:
        blob = r["response_json"] or ""
        if memory_id in blob or any(t in blob for t in texts):
            hits.append((r["principal"], r["operation"], r["key"]))
    return hits


def _scrub_pack(body: dict, memory_id: str) -> tuple[dict, bool]:
    """The pack with its copy of `memory_id` replaced by the marker."""
    changed = False
    ckpt = body.get("checkpoint")
    if isinstance(ckpt, dict) and ckpt.get("memoryId") == memory_id:
        body["checkpoint"] = {k: ckpt[k] for k in ("memoryId", "revision", "stepId") if k in ckpt}
        body["checkpoint"]["body"] = {"goal": MARKER}
        if "constraints" in body:
            body["constraints"] = [MARKER]
        changed = True
    for item in body.get("related") or []:
        if isinstance(item, dict) and item.get("memoryId") == memory_id:
            for k in ("statement", "why"):
                if k in item:
                    item[k] = MARKER
            changed = True
    return body, changed


def forget(conn: sqlite3.Connection, memory_id: str, *, reason: str,
           approved_by: str = "operator") -> dict:
    """Withdraw and erase `memory_id`, and return its receipt."""
    from store import ledger as L
    if not reason or not reason.strip():
        raise ForgetError("an erasure must say why")
    if L.current(conn, memory_id) is None:
        raise ForgetError(f"{memory_id} does not exist")
    texts = _texts(conn, memory_id)
    backends: list[dict] = []

    # 1. Withdrawal: refuses an open workflow's checkpoint before anything changes.
    try:
        t = L.tombstone(conn, memory_id, reason=reason, approved_by=approved_by)
    except L.LedgerError as exc:
        raise ForgetError(str(exc)) from None
    backends.append({"backend": "tombstones", "status": "withdrawn",
                     "detail": f"revisions {t['revisions']} tombstoned"})
    step = "rewrite"
    try:
        _erase_in_store(conn, memory_id, texts, backends)
        step = "projections"
        _purge(conn, backends)
        step = "export"
        backends.append(_export(texts))
        backends += _backups()
        backends.append({"backend": "git-history", "status": "retained",
                         "detail": "earlier commits of registry/ledger.jsonl keep the text; this "
                                   "engine does not rewrite git history"})
        step = "residue"
        erased = _verify(conn, memory_id, texts, backends)
    except Exception as exc:                                                       # noqa: BLE001
        # A failure part-way is SAID, with the backends reached so far: the record is
        # withdrawn already, and running forget again finishes the erasure.
        receipt = {"memoryId": memory_id, "at": _now(), "reason": reason,
                   "approvedBy": approved_by, "revisions": t["revisions"], "withdrawn": True,
                   "erasedInStore": False, "interrupted": step,
                   "error": f"{type(exc).__name__}: {str(exc)[:200]}", "backends": backends,
                   "complete": False}
        _journal(receipt)
        raise ForgetError(f"erasure interrupted at {step} ({type(exc).__name__}); the record "
                          f"is withdrawn — run forget again to finish") from exc
    receipt = {"memoryId": memory_id, "at": _now(), "reason": reason, "approvedBy": approved_by,
               "revisions": t["revisions"], "withdrawn": True, "erasedInStore": erased,
               "backends": backends,
               "complete": erased is True and not any(b["status"] in RETAINED for b in backends),
               "note": "complete is false while any copy is retained or unverified: backups "
                       "rotate out on their own schedule"}
    _journal(receipt)
    return receipt


def _erase_in_store(conn: sqlite3.Connection, memory_id: str, texts: list[str],
                    backends: list[dict]) -> None:
    """The canon's text and every copy in this store, in one transaction."""
    packs = _packs_quoting(conn, memory_id)
    cached = _idempotency_quoting(conn, memory_id, texts)
    with conn:
        cur = conn.execute(
            "UPDATE ledger SET statement = ?, why = NULL, body_json = NULL, evidence_json = '[]',"
            " provenance_json = '[]', conflicts_with_json = '[]' WHERE memory_id = ?"
            " AND (statement != ? OR why IS NOT NULL OR body_json IS NOT NULL"
            " OR evidence_json != '[]' OR provenance_json != '[]' OR conflicts_with_json != '[]')",
            (MARKER, memory_id, MARKER))
        backends.append({"backend": "ledger", "status": "erased",
                         "detail": f"{cur.rowcount} revision(s) rewritten; ids, owner, times and "
                                   f"class kept as the audit trail"})
        changed, restated = 0, set()
        for pid, rev in packs:
            row = conn.execute("SELECT statement, body_json FROM ledger WHERE memory_id = ? AND"
                               " revision = ?", (pid, rev)).fetchone()
            body, did = _scrub_pack(json.loads(row["body_json"] or "{}"), memory_id)
            # The pack's OWN statement quotes the checkpoint's goal ("handoff of … : goal"),
            # and its lexical row indexes it: both lose the erased text too.
            statement = row["statement"] or ""
            for text in texts:
                statement = statement.replace(text, MARKER)
            if did or statement != row["statement"]:
                conn.execute("UPDATE ledger SET body_json = ?, statement = ? WHERE memory_id = ?"
                             " AND revision = ?",
                             (json.dumps(body, ensure_ascii=False), statement, pid, rev))
                changed += 1
                restated.add(pid)
        for pid in sorted(restated):
            _reindex(conn, pid)
        backends.append({"backend": "handoff-packs", "status": "erased" if packs else "absent",
                         "detail": f"{changed} pack revision(s) carried a copy and were rewritten, "
                                   f"statement and lexical row included"})
        for principal, operation, key in cached:
            conn.execute("DELETE FROM idempotency WHERE principal = ? AND operation = ? AND key = ?",
                         (principal, operation, key))
        backends.append({"backend": "idempotency", "status": "erased" if cached else "absent",
                         "detail": f"{len(cached)} cached answer(s) quoting the record dropped; "
                                   f"a retry of one runs again instead of replaying"})


def _reindex(conn: sqlite3.Connection, memory_id: str) -> None:
    """Rewrite one record's lexical row from its rewritten latest revision."""
    from store import ledger as L
    row = conn.execute("SELECT l.* FROM ledger l WHERE l.memory_id = ? AND NOT EXISTS (SELECT 1"
                       " FROM tombstones t WHERE t.memory_id = l.memory_id) ORDER BY l.revision"
                       " DESC LIMIT 1", (memory_id,)).fetchone()
    conn.execute("DELETE FROM search_notes WHERE memory_id = ?", (memory_id,))
    if row is not None:
        L._index_lexically(conn, {"memory_id": row["memory_id"], "revision": row["revision"],
                                  "statement": row["statement"], "why": row["why"],
                                  "body": json.loads(row["body_json"]) if row["body_json"] else None})


def _purge(conn: sqlite3.Connection, backends: list[dict]) -> None:
    """Derived projections (attested by retention), the full-text index's own segments, and
    the file."""
    from store import retention
    receipts = retention.purge_projections(conn)
    for table, r in sorted(receipts.items()):
        status = {"purged": "erased", "absent": "absent"}.get(r.get("status"), r.get("status"))
        backends.append({"backend": f"projection:{table}",
                         "status": "unverified" if status == "UNVERIFIABLE" else status,
                         "detail": r.get("detail") or ""})
    # FTS5 DELETE ONLY MARKS: the words of a deleted row stay in `search_notes_data` until
    # the segments are merged, live pages a VACUUM keeps. `optimize` merges them away
    # (found by the pre-release review, 2026-10-05).
    try:
        with conn:
            conn.execute("INSERT INTO search_notes(search_notes) VALUES('optimize')")
        backends.append({"backend": "fulltext-segments", "status": "erased",
                         "detail": "search_notes optimized, so no segment keeps a deleted row's words"})
    except sqlite3.Error as exc:
        backends.append({"backend": "fulltext-segments", "status": "unverified",
                         "detail": f"search_notes could not be optimized ({type(exc).__name__})"})
    scrubbed = retention.scrub(conn)
    backends.append({"backend": "file", "status": "erased" if scrubbed["scrubbed"] else "unverified",
                     "detail": scrubbed["detail"]})


def _verify(conn: sqlite3.Connection, memory_id: str, texts: list[str],
            backends: list[dict]) -> bool | None:
    """Search the store for what was erased. True: nothing found; False: named in `residue`;
    None: an earlier run erased the text, so this one has nothing left to search for."""
    if not texts:
        backends.append({"backend": "residue", "status": "unverified",
                         "detail": "an earlier run already erased this record's text, so this "
                                   "run cannot search for it; that run's receipt in "
                                   "store/logs/forget.jsonl is the evidence"})
        return None
    left = _residue(conn, memory_id, texts, _unique_tokens(conn, texts))
    if left:
        backends.append({"backend": "residue", "status": "unverified", "detail": left})
        return False
    return True


def _residue(conn: sqlite3.Connection, memory_id: str, texts: list[str],
             tokens: list[str] | None = None) -> str:
    """Every table and column of this store that still holds the erased text — a whole
    passage, or one of its words no other record uses — named; '' when none. Text, untyped
    and BLOB columns alike: the full-text index keeps words in binary segments."""
    found = []
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"
                                " AND name NOT LIKE 'sqlite_%'"):
        try:
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info({name})")
                    if (c[2] or "").upper() in ("TEXT", "", "BLOB")]
        except sqlite3.Error:
            continue
        # The ledger is checked on this record's rows only: another record may say the
        # same thing and is not this erasure's business. `instr`, not LIKE: `%` and `_`
        # in the text would be wildcards.
        mine = " AND memory_id = ?" if name == "ledger" else ""
        needles = [*texts[:5], *(tokens or [])[:40]]
        for col in cols:
            for needle in needles:
                args = (needle.encode("utf-8"), memory_id) if mine else (needle.encode("utf-8"),)
                try:
                    hit = conn.execute(f'SELECT 1 FROM "{name}" WHERE instr(CAST("{col}" AS BLOB),'
                                       f" ?) > 0{mine} LIMIT 1", args).fetchone()
                except sqlite3.Error:
                    hit = None
                if hit:
                    found.append(f"{name}.{col}")
                    break
    return ", ".join(sorted(set(found)))


def _export(texts: list[str]) -> dict:
    """Rewrite `registry/ledger.jsonl` from the erased canon and check it."""
    try:
        import paths
        out = pathlib.Path(paths.REGISTRY) / "ledger.jsonl"
        if not out.exists():
            return {"backend": "export", "status": "absent",
                    "detail": "no registry/ledger.jsonl in this workspace"}
        tools = pathlib.Path(__file__).resolve().parents[1] / "tools"
        if str(tools) not in sys.path:
            sys.path.insert(0, str(tools))
        import export_ledger
        from store import db as store_db
        c = store_db.connect()
        try:
            text = export_ledger.render(export_ledger.rows(c))
        finally:
            c.close()
        import atomic
        atomic.write_text(out, text)
        body = out.read_text(encoding="utf-8")
        if any(t in body for t in texts):
            return {"backend": "export", "status": "unverified",
                    "detail": "registry/ledger.jsonl still holds erased text after rewriting"}
        return {"backend": "export", "status": "erased",
                "detail": "registry/ledger.jsonl rewritten from the erased canon and checked"}
    except Exception as exc:                                                       # noqa: BLE001
        return {"backend": "export", "status": "unverified",
                "detail": f"the export could not be rewritten ({type(exc).__name__}); run "
                          f"`project-observatory full export-ledger`"}


def _backups() -> list[dict]:
    """Backups taken before the erasure: counted, never claimed clean."""
    out = []
    try:
        import paths
        db = pathlib.Path(paths.DB)
        plain = [p for p in db.parent.glob("observatory.db.backup-*")
                 if not p.name.endswith(("-wal", "-shm", "-journal"))]
        out.append({"backend": "backups", "status": "retained" if plain else "absent",
                    "detail": f"{len(plain)} plain backup(s) beside the store keep the text "
                              f"until they rotate" if plain else "no plain backups beside the store"})
    except Exception as exc:                                                       # noqa: BLE001
        out.append({"backend": "backups", "status": "unverified",
                    "detail": f"backups could not be listed ({type(exc).__name__})"})
    try:
        import backup_vault
        root = pathlib.Path(backup_vault.root_info()["path"])
        enc = [p for p in root.glob("*") if p.is_file()] if root.exists() else []
        out.append({"backend": "encrypted-backups", "status": "retained" if enc else "absent",
                    "detail": f"{len(enc)} encrypted backup(s) in the backups root keep the text "
                              f"until they rotate" if enc else "no encrypted backups"})
    except Exception as exc:                                                       # noqa: BLE001
        out.append({"backend": "encrypted-backups", "status": "unverified",
                    "detail": f"the backups root could not be read ({type(exc).__name__})"})
    try:
        import backup_vault
        home = pathlib.Path(paths.HOME)
        snaps = [p for kind in ("snapshot", "before-upgrade", "before-update", "daily")
                 for p in backup_vault.local_snapshots(home, kind)]
        out.append({"backend": "workspace-snapshots", "status": "retained" if snaps else "absent",
                    "detail": f"{len(snaps)} plaintext snapshot folder(s) under backups/ keep the text "
                              "until they rotate or `full backups migrate` moves them" if snaps
                              else "no plaintext snapshot folders"})
        failed = [p for p in home.parent.glob(f"{home.name}.failed-update-*") if p.is_dir()]
        out.append({"backend": "failed-update-copies", "status": "retained" if failed else "absent",
                    "detail": f"{len(failed)} workspace cop(ies) kept by a rolled-back update keep the "
                              f"text until a person deletes them ({failed[0].name}…)" if failed
                              else "no copies from a rolled-back update"})
    except Exception as exc:                                                       # noqa: BLE001
        out.append({"backend": "workspace-snapshots", "status": "unverified",
                    "detail": f"snapshot folders could not be listed ({type(exc).__name__})"})
    try:
        from store import retention
        mig = retention.migration_backup_dir()
        copies = [p for p in mig.glob("*") if p.is_file()] if mig.exists() else []
        out.append({"backend": "migration-backups", "status": "retained" if copies else "absent",
                    "detail": f"{len(copies)} pre-upgrade cop(ies) keep the text until pruned"
                              if copies else "no pre-upgrade copies"})
    except Exception as exc:                                                       # noqa: BLE001
        out.append({"backend": "migration-backups", "status": "unverified",
                    "detail": f"pre-upgrade copies could not be listed ({type(exc).__name__})"})
    return out


def _journal(receipt: dict) -> None:
    import paths
    target = pathlib.Path(paths.STATE) / "logs" / JOURNAL
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(receipt, ensure_ascii=False) + "\n")
    except OSError:
        receipt["note"] += "; the receipt could not be journalled (store/logs unwritable)"
