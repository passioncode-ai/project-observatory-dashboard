#!/usr/bin/env python3
"""The canonical write path: append only, guarded by compare-and-swap, operator protected.

Three rules the rest of the system leans on:

1. **Revisions are immutable.** A change of content OR of state appends a new
   revision that supersedes the prior one. Nothing rewrites a row, so
   `contested` and `stale` are recoverable history rather than a lost argument.
2. **The owner guard is structural.** It is applied on every write, not passed
   as a parameter — an optional guard with a default of `operator` lets an
   unnamed writer claim the highest authority simply by saying nothing.
3. **No index write without a committed revision.** The ledger append and its
   outbox row are one transaction; indexers consume the outbox idempotently.
   SQLite and a vector index cannot share a transaction, so the outbox is what
   keeps the two consistent: an index can lag the ledger, never lead it.
"""
from __future__ import annotations
import json, sqlite3, sys, pathlib, uuid
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from store import db as store_db                                                 

FUNCTIONS = ("working", "episodic", "semantic", "experiential")
SCOPES = ("run", "agent-private", "project", "global")

#: The lifecycle from the Fabric memory contract. A transition absent here is
#: refused; the diagram is the specification, not a suggestion.
TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed":   frozenset({"observed", "rejected"}),
    "observed":   frozenset({"supported", "archived"}),
    "supported":  frozenset({"contested", "stale", "superseded"}),
    "contested":  frozenset({"supported", "stale", "superseded"}),
    "stale":      frozenset({"supported", "archived"}),
    "superseded": frozenset({"archived"}),
    "rejected":   frozenset(),
    "archived":   frozenset(),
}
OPERATOR = "operator"

#: Kinds whose revisions belong to a WORKFLOW rather than to their author, and
#: so are written only by `store/workflow.py`, under its executor lease. A
#: checkpoint is continued by whichever session holds the workflow now — often
#: the same `agent:claude-code` identity on another account — so the author
#: guard below would either refuse the rightful successor or, keyed on the
#: identity alone, admit a session the workflow was taken from. A handoff pack
#: is immutable once written. Refusing both here is structural: every door that
#: reaches `append` (`transition`, the MCP record tool, the review queue) meets it.
WORKFLOW_KINDS = frozenset({"checkpoint", "handoff"})

#: The longest text a single record may carry. See `append` for the
#: measurement behind the number.
MAX_TEXT = 4000


class LedgerError(Exception):
    """Base for every refusal. Each subclass is a different remedy."""


class OwnerRequired(LedgerError):
    """A write with no declared owner. Trap T7."""


class OwnerRefused(LedgerError):
    """The writer may not touch this record. The operator's rows are sacred."""


class RevisionConflict(LedgerError):
    """Compare-and-swap failed. Carries the current revision so the caller can merge."""

    def __init__(self, memory_id: str, expected: int, current: int) -> None:
        super().__init__(f"{memory_id}: expected revision {expected}, current is {current}")
        self.memory_id, self.expected, self.current = memory_id, expected, current


class IllegalTransition(LedgerError):
    """The lifecycle has no edge from here to there."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def current(conn: sqlite3.Connection, memory_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM ledger WHERE memory_id = ? ORDER BY revision DESC LIMIT 1",
        (memory_id,)).fetchone()


def history(conn: sqlite3.Connection, memory_id: str) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM ledger WHERE memory_id = ? ORDER BY revision", (memory_id,)))


def _check_owner(prior: sqlite3.Row | None, writer: str) -> None:
    if not writer or not writer.strip():
        raise OwnerRequired("a write must declare its owner; there is no default")
    if prior is None:
        # A BRAND-NEW record may not be operator-owned. The operator's authority
        # is exercised OVER existing records — promote, reject, tombstone, all of
        # which reach here through `transition()` with a prior — and never
        # asserted by inventing one. Minting a fresh operator-owned row is what
        # forgery looks like: `owner == "operator"` buys permanent exemption from
        # retention (`store/retention.json: owner_exempt`), immunity from any
        # other writer's supersession (below), and no confidence discount. Three
        # privileges, previously available to any caller willing to type the
        # word.
        #
        # `tools/review.py` already refuses to mint this from a script — it
        # requires a terminal, on the stated grounds that otherwise anything with
        # shell access could forge it. This is the same rule at the canonical
        # write path, where every door passes.
        if writer == OPERATOR:
            raise OwnerRefused(
                "a new record may not be created as `operator`. The operator's authority "
                "applies to records that already exist — promote or reject one with "
                "`review.py`, from a terminal. Record the observation under the identity "
                "that made it (e.g. `agent:claude-code`).")
        return
    if writer == OPERATOR:
        return
    if prior["owner"] == OPERATOR:
        raise OwnerRefused(
            f"{prior['memory_id']} is owned by the operator; {writer} may not supersede it")
    if prior["owner"] != writer:
        raise OwnerRefused(
            f"{prior['memory_id']} is owned by {prior['owner']}; {writer} may only correct its own")


def _commit_revision(conn: sqlite3.Connection, row: dict) -> int:
    """The one place a revision reaches disk — ledger and outbox, one transaction.

    Extracted so that `append` and `corroborate` can each carry their OWN
    complete set of guards instead of one of them passing a flag that switches
    the other's off. A guard that can be disabled at a call site is a known
    failure shape (see `owner_exempt_note` in the retention defaults); the two
    callers share the write and nothing else.

    **AN ERASED RECORD TAKES NO FURTHER REVISIONS**, and the guard sits here
    because it must hold for every path — `append`, `transition`, `corroborate`
    — rather than in whichever caller remembers it. Every read is record-wide:
    `live()`, `survey.search`'s hydration, `review.py`'s queue,
    `build_findings.py` and `retention.ledger_candidates` all join tombstones on
    `memory_id` alone. So a revision appended to a tombstoned record is
    invisible to every reader the moment it is written — the writer believes it
    recorded something and nothing did. It would also put the text back into an
    index that had just been purged of it. Refusing loudly is strictly better
    than accepting silently; a new conclusion about the same subject is a new
    record.
    """
    with conn:
        return insert_revision(conn, row)


def insert_revision(conn: sqlite3.Connection, row: dict) -> int:
    """`_commit_revision` without the transaction, for a caller that holds one.

    `store/workflow.py` checks a lease, writes a checkpoint and records the
    answer to an idempotency key as ONE transaction: a lease checked in one
    transaction and a revision written in another lets the lease change hands
    in between. `with conn:` commits whatever transaction is open on exit, so
    it cannot nest inside that one; the guard below is a read and travels with
    the insert, so every path still meets it.
    """
    tomb = conn.execute("SELECT revision FROM tombstones WHERE memory_id = ?"
                        " ORDER BY revision LIMIT 1", (row["memory_id"],)).fetchone()
    if tomb is not None:
        raise LedgerError(
            f"{row['memory_id']} is tombstoned (from revision {tomb[0]}) and takes no "
            f"further revisions — every read joins tombstones on memory_id, so this "
            f"row would be written and never served. Append a new record instead.")
    conn.execute(
        "INSERT INTO ledger (memory_id, revision, kind, project_id, agent_id, run_id,"
        " session_id, function, scope, statement, why, state, confidence, owner,"
        " classification, valid_from, valid_to, supersedes_json,"
        " conflicts_with_json, provenance_json, evidence_json, created_at,"
        " workflow_id, step_id, executor_json, body_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (row["memory_id"], row["revision"], row["kind"], row["project_id"],
         row["agent_id"], row["run_id"], row["session_id"], row["function"],
         row["scope"], row["statement"], row["why"], row["state"], row["confidence"],
         row["owner"], row["classification"], row["valid_from"],
         row["valid_to"], json.dumps(row["supersedes"]),
         json.dumps(row["conflicts_with"]),
         json.dumps(row["provenance"], ensure_ascii=False),
         json.dumps(row["evidence"], ensure_ascii=False), row["created_at"],
         row.get("workflow_id"), row.get("step_id"),
         None if row.get("executor") is None else json.dumps(row["executor"], ensure_ascii=False),
         None if row.get("body") is None else json.dumps(row["body"], ensure_ascii=False)))
    # The version comes from the store's own vocabulary, not from a literal
    # here. This line held `1` while `store/indexer.py` filtered on its own
    # constant — two numbers that had to agree, in two files, with nothing
    # checking. See `store/db.py:PROJECTION_VERSION`.
    cur = conn.execute(
        "INSERT INTO outbox (memory_id, revision, projection_version) VALUES (?,?,?)",
        (row["memory_id"], row["revision"], store_db.PROJECTION_VERSION))
    return cur.lastrowid


def _carried(prior: sqlite3.Row | None) -> dict:
    """The workflow fields a later revision inherits unchanged.

    They say WHICH workflow step a record came out of, which is identity rather
    than content: a correction or a promotion of a step's result is still that
    step's result. `append` takes no argument for them, so carrying them is the
    only way a revision can have them, and it cannot be skipped by a caller.
    """
    if prior is None:
        return {}
    keys = prior.keys()
    def loaded(name: str):
        raw = prior[name] if name in keys else None
        return None if raw is None else json.loads(raw)
    return {"workflow_id": prior["workflow_id"] if "workflow_id" in keys else None,
            "step_id": prior["step_id"] if "step_id" in keys else None,
            "executor": loaded("executor_json"), "body": loaded("body_json")}


def append(
    conn: sqlite3.Connection,
    *,
    owner: str,
    statement: str,
    kind: str = "note",
    memory_id: str | None = None,
    expected_revision: int | None = None,
    project_id: str | None = None,
    agent_id: str | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
    function: str = "episodic",
    scope: str = "project",
    state: str = "proposed",
    #: The writer's note about the RECORD — why it was written, or why a
    #: revision changed it — as against `statement`, which is the claim itself.
    #: It had no stated meaning at all, and a field with none collects a third
    #: use: measured 2026-09-07, the observer writes a revision note into it
    #: ("confidence corrected: an automated writer may not assert certainty"),
    #: `tools/record_lost_projects.py` writes why the record is worth keeping,
    #: and the companion writes nothing because its statement IS the fact. The
    #: first two fit the definition above; the third is an absence, not a third
    #: use. Indexed in FTS beside `statement`, so what goes here is
    #: searchable and a placeholder would pollute every query.
    why: str | None = None,
    confidence: float | None = None,
    classification: str = "project-internal",
    valid_from: str | None = None,
    valid_to: str | None = None,
    conflicts_with: list[str] | None = None,
    provenance: list[dict] | None = None,
    evidence: list[dict] | None = None,
    #: The workflow this record is part of — for a session record, the workflow
    #: the session executes (`OBSERVATORY_WORKFLOW_ID`, set by whoever started
    #: it). It may change between revisions: a session can move to another
    #: workflow. Checkpoints and packs never come through here.
    workflow_id: str | None = None,
) -> dict:
    """Append a revision. Returns the accepted revision and a consistency cursor.

    A new record omits `memory_id`. A correction supplies it together with
    `expected_revision`; a mismatch raises RevisionConflict rather than winning.
    """
    if function not in FUNCTIONS:
        raise LedgerError(f"unknown function {function!r}; expected one of {FUNCTIONS}")
    if scope not in SCOPES:
        raise LedgerError(f"unknown scope {scope!r}; expected one of {SCOPES}")
    if confidence is not None and not (0 < confidence <= 1):
        raise LedgerError("confidence must be in (0, 1]")
    # BOUNDED, and structurally — here rather than at each call site, for the
    # same reason `owner_exempt` is inlined into retention's SQL: an optional
    # guard is one that is eventually forgotten. Measured 2026-09-07 over the
    # live ledger: 139 rows, median statement 191 characters, p95 380, MAX 406.
    # And `observatory_record` accepted a two-million-character statement over
    # the wire without complaint — a store the size of an agent's patience, and
    # a row no index can carry: the embedding request would fail for ever while
    # the outbox kept retrying it.
    #
    # 4000 is ten times the observed maximum. A statement is "the minimal claim
    # or episode", per the wire's own field description — a transcript is
    # evidence, and evidence is LINKED rather than stored.
    # A RECORD WITH NO CONTENT IS NOT A RECORD. Measured 2026-09-07: an empty
    # statement was accepted and stored as `''`. The wire declares
    # `min_length=1`, so it could not arrive that way — but `agent/observe.py`
    # appends `parsed.interpretation` straight from the model, and a model
    # answering `worth_recording: true` with an empty interpretation would have
    # put a blank conclusion in the queue for an operator to adjudicate. The
    # indexer would then skip it as "tombstoned or empty" and the review list
    # would show a blank line. Structural, for the same reason as MAX_TEXT: the
    # wire's guard protects one caller.
    if not (statement or "").strip():
        raise LedgerError(
            "a record must carry a statement. An empty conclusion cannot be "
            "reviewed, indexed or corrected — if there is nothing to say, do not "
            "append.")
    for field, value in (("statement", statement), ("why", why)):
        if value is not None and len(value) > MAX_TEXT:
            raise LedgerError(
                f"{field} is {len(value):,} characters; the limit is {MAX_TEXT:,}. "
                f"A record is the minimal claim, not a transcript — link the "
                f"transcript as evidence instead. The live ledger's longest "
                f"statement is 406 characters.")
    if owner != OPERATOR and state != "proposed" and memory_id is None:
        raise LedgerError(
            f"{owner} may not create a record already in state {state!r}; "
            "an automated writer proposes and something else promotes")

    new = memory_id is None
    mid = memory_id or f"mem:{uuid.uuid4().hex[:16]}"
    prior = None if new else current(conn, mid)
    if not new and prior is None:
        raise LedgerError(f"{mid} does not exist; omit memory_id to create it")
    guarded = prior["kind"] if prior is not None and prior["kind"] in WORKFLOW_KINDS else kind
    if guarded in WORKFLOW_KINDS:
        raise LedgerError(
            f"a {guarded} belongs to its workflow, not to a writer: checkpoints are written under the "
            f"workflow's executor lease and a handoff pack never changes. Use the "
            f"checkpoint and handoff operations (store/workflow.py) instead.")
    _check_owner(prior, owner)

    if prior is not None:
        if expected_revision is None:
            raise RevisionConflict(mid, -1, prior["revision"])
        if expected_revision != prior["revision"]:
            raise RevisionConflict(mid, expected_revision, prior["revision"])
        if state != prior["state"] and state not in TRANSITIONS[prior["state"]]:
            raise IllegalTransition(
                f"{mid}: {prior['state']} -> {state} is not a lifecycle edge; "
                f"from {prior['state']} the legal moves are "
                f"{sorted(TRANSITIONS[prior['state']]) or 'none — it is terminal'}")

    revision = 1 if prior is None else prior["revision"] + 1
    supersedes = [] if prior is None else [f"{mid}@{prior['revision']}"]
    created = _now()

    cursor = _commit_revision(conn, dict(
        memory_id=mid, revision=revision, kind=kind, project_id=project_id,
        agent_id=agent_id, run_id=run_id, session_id=session_id, function=function,
        scope=scope, statement=statement, why=why, state=state, confidence=confidence,
        owner=owner, classification=classification, valid_from=valid_from,
        valid_to=valid_to, supersedes=supersedes, conflicts_with=conflicts_with or [],
        provenance=provenance or [], evidence=evidence or [], created_at=created,
        **{**_carried(prior), **({"workflow_id": workflow_id} if workflow_id else {})}))
    return {"memoryId": mid, "revision": revision, "state": state, "owner": owner,
            "supersedes": supersedes, "consistencyCursor": cursor, "createdAt": created}


def transition(conn: sqlite3.Connection, memory_id: str, *, to_state: str,
               owner: str, expected_revision: int, why: str | None = None) -> dict:
    """Move a record's state by appending a revision. Content is carried forward."""
    prior = current(conn, memory_id)
    if prior is None:
        raise LedgerError(f"{memory_id} does not exist")
    return append(
        conn, memory_id=memory_id, expected_revision=expected_revision, owner=owner,
        statement=prior["statement"], kind=prior["kind"], project_id=prior["project_id"],
        agent_id=prior["agent_id"], run_id=prior["run_id"], session_id=prior["session_id"],
        function=prior["function"], scope=prior["scope"], state=to_state,
        why=why or prior["why"], confidence=prior["confidence"],
        classification=prior["classification"], valid_from=prior["valid_from"],
        valid_to=prior["valid_to"],
        conflicts_with=json.loads(prior["conflicts_with_json"]),
        provenance=json.loads(prior["provenance_json"]),
        evidence=json.loads(prior["evidence_json"]))


def corroborate(conn: sqlite3.Connection, memory_id: str, *, by: str, check: dict,
                expected_revision: int) -> dict:
    """Promote `proposed` to `observed` on an INDEPENDENT mechanical check.

    The design promises two ways out of `proposed`: the operator approves, or a
    second, independent scan corroborates it. Nothing else promotes anything.
    Without this function only the first was reachable, and rows could sit
    unreviewed until retention erased them. Erasure by timeout is not review;
    it is the estate quietly forgetting what it observed.

    This is deliberately NOT `transition` with a different argument. Three
    guards make it a different act:

    * **The claim stays its author's.** `owner` is carried forward unchanged. A
      corroboration is a second witness, not a transfer of authorship, and the
      corroborator is recorded in `provenance` where a reader can see who
      checked what.
    * **Nobody corroborates themselves.** `by` must differ from the row's owner.
      This is the exact inverse of `_check_owner`, which stops an agent editing
      another's row; here the danger is an agent promoting its own, and one
      guard cannot serve both directions.
    * **The operator may not use this path.** An operator's approval is a
      decision and belongs in `review`, where it is recorded as one. Routing it
      through a function named `corroborate` would launder a judgement into a
      measurement.

    `check` is the evidence and is required: what was verified, and how. A
    promotion whose justification is not written down is indistinguishable from
    a bug six months later.
    """
    prior = current(conn, memory_id)
    if prior is None:
        raise LedgerError(f"{memory_id} does not exist")
    if not by or not by.strip():
        raise OwnerRequired("a corroboration must name who checked; there is no default")
    if by == OPERATOR:
        raise OwnerRefused(
            "the operator approves through review, not corroboration: a decision "
            "recorded as a measurement cannot be told apart from one later")
    if by == prior["owner"]:
        raise OwnerRefused(
            f"{memory_id} is owned by {prior['owner']}, which is also the corroborator; "
            "a second witness cannot be the first one")
    if prior["state"] != "proposed":
        raise IllegalTransition(
            f"{memory_id} is {prior['state']!r}; corroboration promotes only from "
            f"'proposed'")
    if expected_revision != prior["revision"]:
        raise RevisionConflict(memory_id, expected_revision, prior["revision"])
    if not check or "how" not in check:
        raise LedgerError("check must describe how the claim was verified: {'how': ...}")

    revision = prior["revision"] + 1
    created = _now()
    cursor = _commit_revision(conn, dict(
        memory_id=memory_id, revision=revision, kind=prior["kind"],
        project_id=prior["project_id"], agent_id=prior["agent_id"],
        run_id=prior["run_id"], session_id=prior["session_id"],
        function=prior["function"], scope=prior["scope"], statement=prior["statement"],
        why=prior["why"], state="observed", confidence=prior["confidence"],
        owner=prior["owner"], classification=prior["classification"],
        valid_from=prior["valid_from"], valid_to=prior["valid_to"],
        supersedes=[f"{memory_id}@{prior['revision']}"],
        conflicts_with=json.loads(prior["conflicts_with_json"]),
        provenance=json.loads(prior["provenance_json"]) +
                   [{"source": "corroboration", "by": by, "at": created}],
        evidence=json.loads(prior["evidence_json"]) + [dict(check, kind="corroboration")],
        created_at=created, **_carried(prior)))
    return {"memoryId": memory_id, "revision": revision, "state": "observed",
            "owner": prior["owner"], "corroboratedBy": by,
            "consistencyCursor": cursor, "createdAt": created}


def tombstone(conn: sqlite3.Connection, memory_id: str, *, reason: str,
              approved_by: str) -> dict:
    """Record an erasure. The ledger rows stay; retention never DELETEs.

    **The unit is the RECORD, one tombstone row per revision.** It used to
    tombstone the CURRENT revision alone, and every read is record-wide — this
    function's own `live()` below, `survey.search`'s hydration, `review.py` and
    `build_findings.py` all `LEFT JOIN tombstones ON t.memory_id = l.memory_id`.
    So one tombstone already hid the whole record from every reader, while
    `retention.purge_projections` deleted index rows for that one revision and
    attested `status: purged, tombstoned_rows_remaining: 0`.

    On a multi-revision record that left an earlier revision's statement as a
    live row in `search_notes` — never deleted, so no page was freed and the
    VACUUM had nothing to zero. Nothing leaked to a caller, because hydration
    drops the record by memory_id; the text simply stayed in the file.
    Single-revision records, which is most of them, made the two units coincide
    and hid it.

    Writing the trail per revision fixes both halves at once: the purge's
    per-revision loop now covers every revision, and the trail names exactly
    which revisions were erased rather than only the last one.

    Completion is still the caller's job: the derived projections must be purged
    and each backend must attest the purge before this is claimed done.
    """
    row = current(conn, memory_id)
    if row is None:
        raise LedgerError(f"{memory_id} does not exist")
    if not approved_by or not approved_by.strip():
        raise OwnerRequired("an erasure must name who approved it")
    if row["kind"] == "checkpoint" and _workflow_open(conn, row["workflow_id"]):
        # AN OPEN WORKFLOW'S CHECKPOINT IS ITS STATE. A tombstoned record takes
        # no further revisions, so erasing it left a workflow that could be
        # neither continued (every write refused) nor handed over (no
        # checkpoint to carry). Close the workflow first, then erase.
        raise LedgerError(
            f"{memory_id} is the checkpoint of open workflow {row['workflow_id']}; close the "
            f"workflow first (a final checkpoint with `close`), then erase it")
    revisions = [r["revision"] for r in history(conn, memory_id)]
    now = _now()
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO tombstones (memory_id, revision, reason, approved_by,"
            " created_at) VALUES (?,?,?,?,?)",
            [(memory_id, rev, reason, approved_by, now) for rev in revisions])
    return {"memoryId": memory_id, "revision": row["revision"],
            "revisions": revisions, "reason": reason, "approvedBy": approved_by,
            "note": "every revision tombstoned; ledger rows retained; purge the "
                    "derived projections and collect receipts"}


def _workflow_open(conn: sqlite3.Connection, workflow_id: str | None) -> bool:
    if not workflow_id:
        return False
    try:
        row = conn.execute("SELECT status FROM workflows WHERE workflow_id = ?",
                           (workflow_id,)).fetchone()
    except sqlite3.Error:
        return False
    return row is not None and row[0] == "open"


def live_count(conn: sqlite3.Connection, project_id: str | None = None,
               states: tuple[str, ...] = ("supported", "contested", "observed",
                                          "proposed")) -> int:
    """How many live records the scope holds, ignoring any limit.

    A reader that returns fifty of a hundred and thirteen and reports `count: 50`
    has told the caller the size of its own page, not the size of what it knows.
    Readers that page (such as the MCP server's recall tool) report this total
    beside the page, so a cap is never silent.
    """
    sql = ("SELECT count(*) FROM ledger l JOIN (SELECT memory_id, MAX(revision) r"
           " FROM ledger GROUP BY memory_id) m ON l.memory_id = m.memory_id"
           " AND l.revision = m.r LEFT JOIN tombstones t"
           " ON t.memory_id = l.memory_id WHERE t.memory_id IS NULL")
    sql += _NOT_WORKFLOW
    args: list = []
    if project_id:
        sql += " AND l.project_id = ?"
        args.append(project_id)
    sql += f" AND l.state IN ({','.join('?' * len(states))})"
    args += list(states)
    return conn.execute(sql, args).fetchone()[0]


def live(conn: sqlite3.Connection, project_id: str | None = None,
         states: tuple[str, ...] = ("supported", "contested", "observed", "proposed"),
         limit: int = 100, cursor: str | None = None) -> list[dict]:
    """Current revisions, tombstoned records excluded.

    Conflicting records are returned TOGETHER: a `contested` row appears beside
    the `supported` one it disagrees with, and nothing here ranks them. A
    retrieval that quietly picks a winner is how a memory becomes confidently
    wrong (contract: "ranking MUST NOT silently collapse disagreement").
    """
    sql = ("SELECT l.* FROM ledger l JOIN (SELECT memory_id, MAX(revision) r FROM ledger"
           " GROUP BY memory_id) m ON l.memory_id = m.memory_id AND l.revision = m.r"
           " LEFT JOIN tombstones t ON t.memory_id = l.memory_id"
           " WHERE t.memory_id IS NULL")
    sql += _NOT_WORKFLOW
    args: list = []
    if project_id:
        sql += " AND l.project_id = ?"
        args.append(project_id)
    sql += f" AND l.state IN ({','.join('?' * len(states))})"
    args += list(states)
    # The cursor's key is the PAIR `(created_at, memory_id)`, because
    # `created_at` is second-resolution and therefore not unique: two records
    # written in the same second would make a cursor over the timestamp alone
    # ambiguous, skipping one or repeating it. The same non-uniqueness cost
    # `compute_deltas.latest_two` a random ordering until `rowid` was added as a
    # tiebreak. `memory_id` is unique, so the pair is a total order.
    if cursor:
        at, _, mid = cursor.partition("|")
        sql += " AND (l.created_at, l.memory_id) < (?, ?)"
        args += [at, mid]
    sql += " ORDER BY l.created_at DESC, l.memory_id DESC LIMIT ?"
    args.append(limit)
    return [{k: r[k] for k in r.keys() if k not in _BODY_COLUMNS}
            for r in conn.execute(sql, args)]


#: A workflow's checkpoint and handoff pack are read through the workflow tools,
#: never in a general listing: one pack is up to 128 KB, so a page of ten could
#: be a megabyte where the reader was promised a few KB. Their structured
#: columns leave every other row too — a step's result keeps its statement.
def not_workflow(alias: str = "l") -> str:
    """The SQL condition that leaves workflow records out of a general reader.

    One spelling, built from `WORKFLOW_KINDS`, for every reader that lists
    conclusions — recall, project notes, the observer's prompt, retention's
    age rules — so a kind added to the set leaves all of them at once."""
    prefix = f"{alias}." if alias else ""
    return f"{prefix}kind NOT IN ({','.join(repr(k) for k in sorted(WORKFLOW_KINDS))})"


_NOT_WORKFLOW = " AND " + not_workflow()
_BODY_COLUMNS = frozenset({"body_json", "executor_json"})


def live_cursor(row: dict) -> str:
    """The cursor that continues after this row."""
    return f"{row['created_at']}|{row['memory_id']}"


def proposals_add(conn: sqlite3.Connection, *, target_id: str, patch: dict,
                  evidence: list[dict], owner: str) -> dict:
    """Propose a registry change. It NEVER touches registry/*.json.

    The registry is written by collectors and by the operator. A proposal is a
    row waiting for a decision, which is the whole point: an agent that could
    edit the fact base directly would poison it one plausible sentence at a time.
    """
    if not owner or not owner.strip():
        raise OwnerRequired("a proposal must declare its owner")
    pid = f"prop:{uuid.uuid4().hex[:16]}"
    with conn:
        conn.execute(
            "INSERT INTO proposals (id, target_id, patch_json, evidence_json, status,"
            " created_at) VALUES (?,?,?,?, 'proposed', ?)",
            (pid, target_id, json.dumps(patch, ensure_ascii=False),
             json.dumps({"owner": owner, "evidence": evidence}, ensure_ascii=False), _now()))
    return {"proposalId": pid, "targetId": target_id, "status": "proposed", "owner": owner}
