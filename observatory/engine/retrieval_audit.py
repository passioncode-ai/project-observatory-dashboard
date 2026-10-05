"""Retrieval receipts, scoped explain and redaction on the way OUT of agent memory (PB-137 N-012).

Three promises, each visible in an answer rather than described here.

- **Every search leaves a receipt.** `record` appends one line to
  `store/logs/retrieval.jsonl` (0600, rotated with every other log) and returns its id.
  The line names the caller's binding and principal, the project and classes the
  search was scoped to, the embedding-policy revision, the arms that ran, and the exact
  `(memoryId, revision)` of every result with how it matched. It holds the QUERY only as
  an HMAC under the workspace's fingerprint salt: a receipt says which question was
  asked again, never what it was. It holds no statement text.
- **Explain answers from the receipt, not from a new query** (`explain`). It hydrates
  the exact revisions the search returned and says, for each, whether it is still the
  current revision, superseded, erased or expired, and whether the caller may still see
  it. Only the binding that received the receipt can explain it; any other caller, and an
  id that does not exist, get the same refusal, so a receipt id is not an existence oracle.
- **Text leaves redacted** (`scrub_text`, `scrub_tree`). Records are redacted on the way
  in (`memory_redact`), but other writers exist and a workspace learns new secret values
  after a record was written. The memory tools therefore run the same two filters —
  credential shapes and the workspace's known values — over every free-text field they
  return, and over error details. Identifiers (`wf_…`, `handoff:…`, lease tokens) are
  never passed through them: a redacted id is a broken protocol.

There is no retrieval cache to guard: search, recall and explain read the store on every
call. The redactor's known-value cache is keyed by its sources' fingerprint and holds
the same values for every caller.

The budget unit of an answer is UTF-8 bytes (`resultBytes`), named as such: a token count
would need the reader's tokenizer, which the engine does not have.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import pathlib
import secrets
from datetime import datetime, timezone
from typing import Any

JOURNAL = "retrieval.jsonl"
GENERATIONS = 5
RECEIPT_PREFIX = "rcpt_"
#: The free-text fields of a search or recall row; everything else is an id or a number.
#: `evidence_json` and `provenance_json` are JSON held as text: a redaction marker inside
#: one of their strings keeps it valid JSON (the marker carries no quote).
TEXT_FIELDS = ("statement", "why", "evidence_json", "provenance_json")

_HINTS = {
    "unknown-receipt": "explain only a receipt this caller received from observatory_search; "
                       "receipts older than the retrieval log's rotation are gone",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def journal_path() -> pathlib.Path:
    import paths
    return pathlib.Path(paths.STATE) / "logs" / JOURNAL


# ─────────────────────────────── redaction ──────────────────────────────────

def _redactor():
    import memory_redact
    return memory_redact.Redactor()


def scrub_rows(rows: list[dict], report: dict | None = None) -> tuple[list[dict], dict]:
    """The rows with their free-text fields redacted, and one report for all of them."""
    red = _redactor()
    texts = [{k: r.get(k) for k in TEXT_FIELDS if isinstance(r.get(k), str)} for r in rows]
    cleaned, rep = red.scrub(texts)
    out = [{**r, **c} for r, c in zip(rows, cleaned)]
    return out, _merge(report, rep.as_dict())


def scrub_tree(value: Any, report: dict | None = None) -> tuple[Any, dict]:
    """A whole subtree (a checkpoint body, a pack) redacted. Only for subtrees that were
    already redacted on the way in, so the filters are idempotent on their ids."""
    cleaned, rep = _redactor().scrub(value)
    return cleaned, _merge(report, rep.as_dict())


def scrub_text(text: str) -> str:
    return _redactor().scrub(text)[0]


def scrub_degraded(entries: list) -> list:
    """`degraded` reasons carry exception text, which can quote what failed."""
    return [{**d, "reason": scrub_text(d["reason"])} if isinstance(d, dict)
            and isinstance(d.get("reason"), str) else d for d in entries]


def _merge(a: dict | None, b: dict) -> dict:
    if not a:
        return b
    out = dict(a)
    for key, value in b.items():
        if isinstance(value, bool):
            out[key] = bool(out.get(key, True)) and value
        elif isinstance(value, (int, float)):
            out[key] = out.get(key, 0) + value
        elif isinstance(value, list):
            out[key] = sorted(set(out.get(key, [])) | set(value))
        else:
            out[key] = value
    return out


# ─────────────────────────────── receipts ───────────────────────────────────

def _query_digest(query: str) -> str | None:
    """HMAC-SHA256 of the query under the workspace's fingerprint salt, or None when
    the salt cannot be read. Reads never create the salt."""
    try:
        import sys
        collectors = pathlib.Path(__file__).resolve().parent / "collectors"
        if str(collectors) not in sys.path:
            sys.path.insert(0, str(collectors))
        import scan_env
        key = scan_env.salt().encode("ascii")
    except Exception:                                                              # noqa: BLE001
        return None
    return "hmac-sha256:" + hmac.new(key, query.encode("utf-8"), hashlib.sha256).hexdigest()


def record(grant: Any, *, query: str, project_id: str | None, answer: dict,
           policy_revision: int | None) -> dict:
    """Append the receipt of one search and return what the answer carries about it."""
    rid = RECEIPT_PREFIX + secrets.token_hex(8)
    results = [{"memoryId": r.get("memoryId"), "revision": r.get("revision"),
                "matched": r.get("matched"), "score": r.get("score"),
                "coverage": r.get("coverage"), "rank": r.get("rank"),
                "distance": r.get("distance")} for r in answer.get("results", [])]
    digest = _query_digest(query)
    row = {"receiptId": rid, "at": _now(), "tool": "observatory_search",
           "binding": grant.binding.binding_id, "principal": grant.binding.principal,
           "projectId": project_id,
           "classes": None if grant.visible_classes is None else list(grant.visible_classes),
           "policyRevision": policy_revision,
           "arms": sorted({a for r in results for a in (r["matched"] or [])}),
           "namespace": "lexical:search_notes",
           "queryDigest": digest, "total": answer.get("total"),
           "abstain": answer.get("abstain"), "floor": answer.get("floor"),
           "degraded": sorted({d.get("source") for d in answer.get("degraded", [])}),
           "resultBytes": len(json.dumps(answer.get("results", []), ensure_ascii=False)
                              .encode("utf-8")),
           "results": results}
    target = journal_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        # The answer stands; what is lost is the ability to explain it, and that is said.
        return {"receiptId": None, "reason": f"the retrieval log could not be written "
                                             f"({type(exc).__name__}); this answer cannot "
                                             f"be explained"}
    out = {"receiptId": rid, "resultBytes": row["resultBytes"]}
    if digest is None:
        out["note"] = "the workspace salt is unreadable, so the query is not fingerprinted"
    return out


def _generations() -> list[pathlib.Path]:
    base = journal_path()
    return [base] + [base.with_name(f"{base.name}.{n}") for n in range(1, GENERATIONS + 1)]


def find(receipt_id: str) -> dict | None:
    """The receipt, newest log first, or None."""
    if not isinstance(receipt_id, str) or not receipt_id.startswith(RECEIPT_PREFIX):
        return None
    needle = f'"receiptId": "{receipt_id}"'
    for path in _generations():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError):
            continue
        for line in reversed(lines):
            if needle in line:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("receiptId") == receipt_id:
                    return row
    return None


def refusal(code: str, detail: str) -> dict:
    return {"error": "explain refused", "code": code, "detail": detail,
            "hint": _HINTS[code], "degraded": []}


def explain(conn, grant: Any, receipt_id: str, hidden_kinds: tuple = ()) -> dict:
    """The receipt's own results, re-read at their exact revisions. Never a new query."""
    row = find(receipt_id)
    if row is None or row.get("binding") != grant.binding.binding_id:
        # The same answer for "not yours" and "no such receipt".
        return refusal("unknown-receipt", f"{grant.binding.binding_id}: no receipt "
                                          f"{receipt_id} for this caller")
    explained, report = [], None
    for r in row.get("results", []):
        rec = conn.execute(
            "SELECT l.*, (SELECT MAX(x.revision) FROM ledger x WHERE x.memory_id = l.memory_id)"
            " AS latest, EXISTS (SELECT 1 FROM tombstones t WHERE t.memory_id = l.memory_id)"
            " AS erased, (l.valid_to IS NOT NULL AND datetime(l.valid_to) IS NOT NULL"
            " AND datetime(l.valid_to) <= datetime('now')) AS expired"
            " FROM ledger l WHERE l.memory_id = ? AND l.revision = ?",
            (r.get("memoryId"), r.get("revision"))).fetchone()
        item: dict[str, Any] = {k: r.get(k) for k in ("memoryId", "revision", "matched", "score",
                                                      "coverage", "rank", "distance")}
        if rec is None or rec["erased"]:
            item["now"] = "erased"
        elif not grant.local and (rec["project_id"] not in _projects(grant)
                                  or not grant.may_see(rec["classification"])
                                  or rec["kind"] in hidden_kinds):
            item["now"] = "no-longer-visible"
        else:
            item["now"] = ("superseded" if rec["latest"] != rec["revision"] else
                           "expired" if rec["expired"] else "current")
            if item["now"] == "superseded":
                item["latestRevision"] = rec["latest"]
            item.update({"projectId": rec["project_id"], "classification": rec["classification"],
                         "state": rec["state"], "statement": rec["statement"], "why": rec["why"]})
        explained.append(item)
    shown = [i for i in explained if "statement" in i]
    cleaned, report = scrub_rows(shown)
    by_key = {(c["memoryId"], c["revision"]): c for c in cleaned}
    explained = [by_key.get((i["memoryId"], i["revision"]), i) for i in explained]
    return {"receiptId": receipt_id, "at": row.get("at"), "projectId": row.get("projectId"),
            "classes": row.get("classes"), "policyRevision": row.get("policyRevision"),
            "arms": row.get("arms"), "namespace": row.get("namespace"),
            "floor": row.get("floor"), "abstain": row.get("abstain"), "total": row.get("total"),
            "degradedAtSearch": row.get("degraded"), "results": explained,
            "redacted": report or {},
            "note": "re-read at the exact revisions the search returned; nothing was searched "
                    "again", "degraded": []}


def _projects(grant: Any):
    projects = grant.binding.projects
    return () if projects == "all" else projects
