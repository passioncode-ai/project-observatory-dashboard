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
| `backups`, `migration-backups` | **retained, unverified**: encrypted or plain copies taken before the erasure keep the text until they rotate out |
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
    """The record's own text, every revision: what must not survive anywhere."""
    out = []
    for r in conn.execute("SELECT statement, why FROM ledger WHERE memory_id = ?", (memory_id,)):
        for value in (r["statement"], r["why"]):
            if isinstance(value, str) and value.strip() and value != MARKER:
                out.append(value)
    return sorted(set(out), key=len, reverse=True)


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
    from store import retention
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

    # 2. The canon's text, and every copy in this store, in one transaction.
    packs = _packs_quoting(conn, memory_id)
    cached = _idempotency_quoting(conn, memory_id, texts)
    with conn:
        cur = conn.execute(
            "UPDATE ledger SET statement = ?, why = NULL, body_json = NULL, evidence_json = '[]'"
            " WHERE memory_id = ? AND (statement != ? OR why IS NOT NULL OR body_json IS NOT NULL"
            " OR evidence_json != '[]')", (MARKER, memory_id, MARKER))
        backends.append({"backend": "ledger", "status": "erased",
                         "detail": f"{cur.rowcount} revision(s) rewritten; ids, owner, times and "
                                   f"class kept as the audit trail"})
        changed = 0
        for pid, rev in packs:
            raw = conn.execute("SELECT body_json FROM ledger WHERE memory_id = ? AND revision = ?",
                               (pid, rev)).fetchone()[0]
            body, did = _scrub_pack(json.loads(raw or "{}"), memory_id)
            if did:
                conn.execute("UPDATE ledger SET body_json = ? WHERE memory_id = ? AND revision = ?",
                             (json.dumps(body, ensure_ascii=False), pid, rev))
                changed += 1
        backends.append({"backend": "handoff-packs", "status": "erased" if packs else "absent",
                         "detail": f"{changed} pack revision(s) carried a copy and were rewritten"})
        for principal, operation, key in cached:
            conn.execute("DELETE FROM idempotency WHERE principal = ? AND operation = ? AND key = ?",
                         (principal, operation, key))
        backends.append({"backend": "idempotency", "status": "erased" if cached else "absent",
                         "detail": f"{len(cached)} cached answer(s) quoting the record dropped; "
                                   f"a retry of one runs again instead of replaying"})

    # 3. Derived projections, attested by retention's own purge.
    receipts = retention.purge_projections(conn)
    for table, r in sorted(receipts.items()):
        status = {"purged": "erased", "absent": "absent"}.get(r.get("status"), r.get("status"))
        backends.append({"backend": f"projection:{table}",
                         "status": "unverified" if status == "UNVERIFIABLE" else status,
                         "detail": r.get("detail") or ""})

    # 4. The file itself.
    scrubbed = retention.scrub(conn)
    backends.append({"backend": "file", "status": "erased" if scrubbed["scrubbed"] else "unverified",
                     "detail": scrubbed["detail"]})

    # 5. Copies outside the store.
    backends.append(_export(texts))
    backends += _backups()
    backends.append({"backend": "git-history", "status": "retained",
                     "detail": "earlier commits of registry/ledger.jsonl keep the text; this engine "
                               "does not rewrite git history"})

    # 6. Nothing left in this store, checked rather than assumed.
    left = _residue(conn, memory_id, texts)
    if left:
        backends.append({"backend": "residue", "status": "unverified", "detail": left})
    receipt = {"memoryId": memory_id, "at": _now(), "reason": reason, "approvedBy": approved_by,
               "revisions": t["revisions"], "withdrawn": True,
               "erasedInStore": not left, "backends": backends,
               "complete": not any(b["status"] in RETAINED for b in backends),
               "note": "complete is false while any copy is retained or unverified: backups "
                       "rotate out on their own schedule"}
    _journal(receipt)
    return receipt


def _residue(conn: sqlite3.Connection, memory_id: str, texts: list[str]) -> str:
    """Any table of this store that still holds the erased text, named; '' when none."""
    if not texts:
        return ""
    found = []
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"
                                " AND name NOT LIKE 'sqlite_%'"):
        try:
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info({name})")
                    if (c[2] or "").upper() in ("TEXT", "")]
        except sqlite3.Error:
            continue
        # The ledger is checked on this record's rows only: another record may say the
        # same thing and is not this erasure's business. `instr`, not LIKE: `%` and `_`
        # in the text would be wildcards.
        mine = " AND memory_id = ?" if name == "ledger" else ""
        for col in cols:
            for t in texts[:5]:
                args = (t, memory_id) if mine else (t,)
                try:
                    hit = conn.execute(f'SELECT 1 FROM "{name}" WHERE instr("{col}", ?) > 0'
                                       f"{mine} LIMIT 1", args).fetchone()
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
