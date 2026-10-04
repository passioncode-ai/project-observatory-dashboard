"""Vector namespaces: one vector index per pinned model identity (PB-137 N-005).

Plan §6: "a separate index per model; a query is embedded by the model whose index it
asks; vectors of different models never mix". A namespace is that index plus the full
identity of the model that fills it — provider, model, revision, the SHA-256 of its
weights and tokenizer, dimensions, metric, normalization, query and passage prefixes and
the chunker version. Two identities that differ in any of these are two namespaces.

What this module guarantees, each one tested in tests/test_vector_namespaces.py:

- **No SQL identifier comes from a caller.** A namespace's table is `vec_ns_<integer id>`;
  the id is the registry's own row id. A provider or model string is data, never a name.
- **Vectors never mix.** A write checks the vector's width against the namespace's
  dimensions, and a backfill writes only the namespace it was opened for.
- **The legacy index is quarantined.** `vec_notes` — the OpenAI index from before
  namespaces — is registered as `legacy` by migration 0010: it keeps answering only where
  the embedding policy allows (N-003), it can be retired, and it is never activated again.
- **A backfill is resumable and never activates.** It walks current, untombstoned records
  in ledger order from a cursor stored with the namespace, committing the vectors and the
  cursor in one transaction, so an interruption leaves the previous state usable and the
  next run continues. Full coverage moves a namespace to `ready`, never to `active`:
  activation is N-010's, and `activate` refuses without an acceptance receipt (model
  admission, scope filtering, calibrated abstention).

The registry table is created by migration 0010 (`store/migrate.py`), whose code is
pinned by its checksum; `LEGACY` here must equal the identity written there, and the
suite checks that it does.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

STATES = ("legacy", "inactive", "backfilling", "ready", "active", "retired")
METRICS = ("cosine", "l2")
NORMALIZATIONS = ("l2", "none")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
#: The identity fields, all required. `revision` and the digests pin the artifact; the
#: rest pin how text becomes a vector. Any difference is a different namespace.
IDENTITY = ("provider", "model", "revision", "weights_sha256", "tokenizer_sha256", "dims",
            "metric", "normalization", "query_prefix", "passage_prefix", "chunker")
LEGACY = {"provider": "openai", "model": "text-embedding-3-small", "revision": "legacy",
          "weights_sha256": None, "tokenizer_sha256": None, "dims": 1536, "metric": "cosine",
          "normalization": "none", "query_prefix": "", "passage_prefix": "",
          "chunker": "statement+why/1"}
#: Kinds a namespace never holds yet: checkpoints are chunked first (N-011).
SKIP_KINDS = ("checkpoint", "handoff", "step_result")


class NamespaceError(ValueError):
    """A namespace request that would mix, mis-name or prematurely activate an index."""


@dataclass(frozen=True)
class Namespace:
    namespace_id: int
    model_key: str
    identity: dict
    state: str
    table: str
    backfill_cursor: int
    backfill_done: int
    backfill_total: int | None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def model_key(identity: Mapping) -> str:
    """The namespace's identity as one digest of its canonical JSON."""
    canon = json.dumps({k: identity.get(k) for k in IDENTITY}, sort_keys=True,
                       separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def validate_identity(identity: Mapping) -> dict:
    """A complete, well-formed identity, or NamespaceError naming what is wrong."""
    if not isinstance(identity, Mapping):
        raise NamespaceError("an identity is an object")
    missing = [k for k in IDENTITY if k not in identity]
    extra = sorted(set(identity) - set(IDENTITY))
    if missing or extra:
        raise NamespaceError(f"identity fields: missing {missing}, unknown {extra}")
    out = {k: identity[k] for k in IDENTITY}
    for k in ("provider", "model", "revision", "chunker"):
        if not isinstance(out[k], str) or not out[k].strip() or len(out[k]) > 200:
            raise NamespaceError(f"identity.{k} must be non-empty text")
    for k in ("weights_sha256", "tokenizer_sha256"):
        if not isinstance(out[k], str) or not SHA256.match(out[k]):
            raise NamespaceError(f"identity.{k} must be a SHA-256 (64 lowercase hex); "
                                 f"a model without pinned weights is not admitted")
    if type(out["dims"]) is not int or not 1 <= out["dims"] <= 8192:
        raise NamespaceError("identity.dims must be an integer from 1 to 8192")
    if out["metric"] not in METRICS:
        raise NamespaceError(f"identity.metric must be one of {list(METRICS)}")
    if out["normalization"] not in NORMALIZATIONS:
        raise NamespaceError(f"identity.normalization must be one of {list(NORMALIZATIONS)}")
    for k in ("query_prefix", "passage_prefix"):
        if not isinstance(out[k], str) or len(out[k]) > 40:
            raise NamespaceError(f"identity.{k} must be text of at most 40 characters")
    return out


def table_name(namespace_id: int) -> str:
    if type(namespace_id) is not int or namespace_id < 1:
        raise NamespaceError("a namespace id is a positive integer")
    return f"vec_ns_{namespace_id}"


def _row(r: sqlite3.Row) -> Namespace:
    return Namespace(namespace_id=r["namespace_id"], model_key=r["model_key"],
                     identity=json.loads(r["identity_json"]), state=r["state"],
                     table=r["table_name"], backfill_cursor=r["backfill_cursor"],
                     backfill_done=r["backfill_done"], backfill_total=r["backfill_total"])


def listing(conn: sqlite3.Connection) -> list[Namespace]:
    try:
        rows = conn.execute("SELECT * FROM vector_namespaces ORDER BY namespace_id").fetchall()
    except sqlite3.OperationalError:
        return []
    return [_row(r) for r in rows]


def get(conn: sqlite3.Connection, namespace_id: int) -> Namespace:
    r = conn.execute("SELECT * FROM vector_namespaces WHERE namespace_id = ?",
                     (namespace_id,)).fetchone()
    if r is None:
        raise NamespaceError(f"no namespace {namespace_id}")
    return _row(r)


def register(conn: sqlite3.Connection, identity: Mapping) -> Namespace:
    """The namespace for this exact identity — created `inactive` if new. Idempotent."""
    ident = validate_identity(identity)
    key = model_key(ident)
    found = conn.execute("SELECT * FROM vector_namespaces WHERE model_key = ?", (key,)).fetchone()
    if found is not None:
        return _row(found)
    with conn:
        cur = conn.execute(
            "INSERT INTO vector_namespaces (model_key, identity_json, state, table_name,"
            " created_at, updated_at) VALUES (?, ?, 'inactive', '', ?, ?)",
            (key, json.dumps(ident, sort_keys=True), _now(), _now()))
        nid = cur.lastrowid
        conn.execute("UPDATE vector_namespaces SET table_name = ? WHERE namespace_id = ?",
                     (table_name(nid), nid))
    return get(conn, nid)


def ensure_table(conn: sqlite3.Connection, ns: Namespace) -> None:
    """Create the namespace's vec0 table (sqlite-vec must be loaded)."""
    if ns.state in ("legacy", "retired"):
        raise NamespaceError(f"namespace {ns.namespace_id} is {ns.state}; it gets no new table")
    name = table_name(ns.namespace_id)          # from the integer, never from identity text
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {name} USING vec0("
                 f"memory_id TEXT, revision INTEGER, embedding float[{int(ns.identity['dims'])}])")
    conn.commit()


def _eligible_rows(conn: sqlite3.Connection, after: int, limit: int) -> list[sqlite3.Row]:
    kinds = ",".join("?" * len(SKIP_KINDS))
    return conn.execute(
        "SELECT l.rowid AS rid, l.memory_id, l.revision, l.statement, l.why, l.project_id,"
        " l.classification, l.scope, l.kind FROM ledger l"
        " JOIN (SELECT memory_id, MAX(revision) r FROM ledger GROUP BY memory_id) m"
        "   ON m.memory_id = l.memory_id AND m.r = l.revision"
        " WHERE l.rowid > ? AND l.memory_id NOT IN (SELECT memory_id FROM tombstones)"
        f"   AND l.kind NOT IN ({kinds}) AND trim(coalesce(l.statement,'')) != ''"
        " ORDER BY l.rowid LIMIT ?", (after, *SKIP_KINDS, limit)).fetchall()


def backfill_step(conn: sqlite3.Connection, ns: Namespace,
                  embed: Callable[[list[str]], list[list[float]]], *,
                  allowed: Callable[[Mapping], bool], batch: int = 32) -> dict:
    """Embed the next batch of current records into this namespace, resumably.

    `embed` is the namespace's own embedder (the local adapter is N-006). `allowed`
    decides each record — for a remote namespace it is the embedding-policy verdict, and
    a record it refuses is skipped, never sent. Vectors and the cursor commit together;
    an exception leaves both as they were. Returns what was done and whether the
    namespace is now complete (`ready`), which never means `active`."""
    ns = get(conn, ns.namespace_id)
    if ns.state not in ("inactive", "backfilling"):
        raise NamespaceError(f"namespace {ns.namespace_id} is {ns.state}; only an inactive or "
                             f"backfilling namespace is filled")
    rows = _eligible_rows(conn, ns.backfill_cursor, batch)
    if not rows:
        total = ns.backfill_done
        with conn:
            conn.execute("UPDATE vector_namespaces SET state = 'ready', backfill_total = ?,"
                         " updated_at = ? WHERE namespace_id = ?", (total, _now(), ns.namespace_id))
        return {"embedded": 0, "skipped": 0, "complete": True, "state": "ready"}
    chosen = [r for r in rows if allowed(dict(r))]
    texts = ["\n".join(p for p in (r["statement"], r["why"] or "") if p.strip()) for r in chosen]
    vectors = embed(texts) if texts else []
    if len(vectors) != len(chosen):
        raise NamespaceError(f"the embedder returned {len(vectors)} vectors for {len(chosen)} texts")
    dims = int(ns.identity["dims"])
    for v in vectors:
        if len(v) != dims:
            raise NamespaceError(f"a vector of width {len(v)} cannot enter a namespace of "
                                 f"{dims} dimensions; vectors of different models never mix")
    from sqlite_vec import serialize_float32
    name = table_name(ns.namespace_id)
    with conn:
        for r, v in zip(chosen, vectors):
            conn.execute(f"DELETE FROM {name} WHERE memory_id = ?", (r["memory_id"],))
            conn.execute(f"INSERT INTO {name} (memory_id, revision, embedding) VALUES (?, ?, ?)",
                         (r["memory_id"], r["revision"], serialize_float32(list(v))))
        conn.execute("UPDATE vector_namespaces SET state = 'backfilling', backfill_cursor = ?,"
                     " backfill_done = backfill_done + ?, updated_at = ? WHERE namespace_id = ?",
                     (rows[-1]["rid"], len(chosen), _now(), ns.namespace_id))
    return {"embedded": len(chosen), "skipped": len(rows) - len(chosen), "complete": False,
            "state": "backfilling"}


def activate(conn: sqlite3.Connection, ns: Namespace, *, receipt: Mapping | None) -> Namespace:
    """Make a `ready` namespace the one that answers. N-010 owns the call.

    The receipt must name the accepted model admission, the scope-filter acceptance and
    the abstention calibration. Without all three, nothing changes."""
    ns = get(conn, ns.namespace_id)
    if ns.state == "legacy":
        raise NamespaceError("the legacy index is quarantined and is never activated again")
    if ns.state != "ready":
        raise NamespaceError(f"namespace {ns.namespace_id} is {ns.state}; only a ready one is activated")
    need = ("model_admission", "scope_filter", "abstention_calibration")
    if not isinstance(receipt, Mapping) or any(not receipt.get(k) for k in need):
        raise NamespaceError(f"activation needs a receipt naming {list(need)} (N-004, N-009, N-010)")
    with conn:
        conn.execute("UPDATE vector_namespaces SET state = 'retired', updated_at = ?"
                     " WHERE state = 'active'", (_now(),))
        conn.execute("UPDATE vector_namespaces SET state = 'active', activated_at = ?,"
                     " activation_receipt_json = ?, updated_at = ? WHERE namespace_id = ?",
                     (_now(), json.dumps(dict(receipt), sort_keys=True), _now(), ns.namespace_id))
    return get(conn, ns.namespace_id)


def retire(conn: sqlite3.Connection, ns: Namespace) -> Namespace:
    with conn:
        conn.execute("UPDATE vector_namespaces SET state = 'retired', updated_at = ?"
                     " WHERE namespace_id = ?", (_now(), ns.namespace_id))
    return get(conn, ns.namespace_id)


def summary(conn: sqlite3.Connection) -> list[dict]:
    """What `full doctor` reports: names, states and coverage, never vectors."""
    return [{"namespace": n.namespace_id, "provider": n.identity.get("provider"),
             "model": n.identity.get("model"), "revision": n.identity.get("revision"),
             "dims": n.identity.get("dims"), "state": n.state, "table": n.table,
             "backfilled": n.backfill_done} for n in listing(conn)]
