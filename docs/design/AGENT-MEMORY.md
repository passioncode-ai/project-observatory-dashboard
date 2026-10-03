# Agent memory: workflows that outlive their sessions

An agent's workflow outlives the session that started it. It moves to another account of the
same provider when a usage limit runs out, to another model or provider when the plan assigns a
step elsewhere, and to a new session after a restart, a compaction or a crash. The session that
leaves often cannot say anything on the way out: a session out of quota cannot answer. So the
next executor's context is written **before** it is needed, one step at a time, and the handoff
is assembled by the engine rather than by the agent that leaves.

This page describes what the engine implements today. The code is
[`store/workflow.py`](../../observatory/engine/store/workflow.py), the redaction on the write
path is [`memory_redact.py`](../../observatory/engine/memory_redact.py), and the MCP tools are
in [`mcp/server.py`](../../observatory/engine/mcp/server.py). The behaviour below is proved by
[`tests/test_workflow_memory.py`](../../observatory/engine/tests/test_workflow_memory.py) (44 cases) and by `run_workflow` in
[`tests/test_mcp_wire.py`](../../observatory/engine/tests/test_mcp_wire.py), which drives the
handoff between two sessions over real stdio.

## The model

| Thing | Where it lives | What changes it |
|---|---|---|
| Workflow | a `workflows` row: id `wf_…`, project, open or closed | the first checkpoint creates it; a checkpoint with `close: true` closes it |
| Checkpoint | ONE ledger record `ckpt:<workflow>`, kind `checkpoint`, a revision per step | `observatory_checkpoint_write`, by the lease holder only |
| Executor lease | a `workflow_leases` row: `active` (one per workflow) or `offered` (one pending handoff) | the first checkpoint grants it; a handoff offers it; acceptance moves it |
| Handoff pack | a ledger record `handoff:…`, kind `handoff`, immutable | `observatory_handoff_create` |
| Refused step | a ledger record, kind `step_result`, state `proposed` | written automatically when a write without the lease is refused |
| Idempotency answer | an `idempotency` row per `(principal, operation, key)` | every write; forgotten after `idempotency_days` |

The ledger gained four columns in migration `0008-agent-memory-workflows`: `workflow_id`,
`step_id`, `executor_json` and `body_json`. They are NULL on every record that is not part of a
workflow, and a later revision of a record inherits them unchanged (`ledger._carried`): a
correction of a step's result is still that step's result.

### The checkpoint body

Typed blocks, not prose. A summary written as prose keeps the discussion and drops the state,
including the flag that said "do not push":

```json
{
  "goal": "ship the exporter",
  "plan": [{"step_id": "S2", "title": "write", "needs": ["S1"]}],
  "done": [{"step_id": "S1", "result": "read the schema", "evidence": ["test:schema"]}],
  "open": [{"step_id": "S2", "next_action": "write the exporter"}],
  "decisions": [{"id": "D-1", "choice": "stream rows", "why": "the table is large"}],
  "constraints": ["read-only: do not push"],
  "artifacts": [{"kind": "git", "path": "/absolute/checkout", "branch": "feature"}],
  "credentials": [{"project": "alpha-web", "env": "prod", "name": "STRIPE_KEY", "purpose": "charge"}],
  "questions": [], "memory_refs": [], "notes": "…"
}
```

`credentials` lists the keys the workflow needs, by name only — `{project, env, name,
purpose}` — and every read and handoff reports where each one is now
([AGENT-SECRETS.md](AGENT-SECRETS.md)).

`goal` is required. An unknown field is refused, because a field the next executor is not told
to read is state that silently does not travel. The body is at most 64 KiB; a log is linked as
an artifact, not pasted. `checkpoint_body` in `store/workflow.py` holds every bound.

## One executor at a time

The lease token (`leaseId`, `wl_…`) is the only thing that tells two sessions apart. Both are
usually `agent:claude-code` on two accounts, so a check on the identity would either refuse the
rightful successor or admit the session the workflow was taken from.

- The first checkpoint of a workflow returns its `leaseId`. Every later checkpoint presents it.
- A write without the current token is refused with `LeaseLost`, and its content is kept as a
  `step_result` episode named in `keptAs`. A session that never learnt of the handoff does not
  lose its work, and a person sees it in the review queue.
- `observatory_handoff_create` offers the workflow to `to: {provider, model, accountRef}`.
  **Who may:** the current executor, for any reason, by presenting its `leaseId`. Without the
  token — the case the handoff exists for, since the executor that should hand over often
  cannot — only for `limit`, `crash` or `restart`, only once the executor has written no
  checkpoint for two minutes (`SILENCE_SECONDS`), and no more than one offer a minute. A
  `plan_route` or `operator` handoff without the token is refused; the operator's override is
  the terminal command, recorded in the pack as `authority: operator-force`. The pack records
  how it was entitled: `lease`, `silence` or `operator-force`.
- Creating a handoff does **not** take the workflow away: the current lease stays in force until
  acceptance, an unaccepted offer lapses after `offerTtlSeconds` (default 3600), and a newer
  handoff replaces an older unaccepted one. `observatory_checkpoint_latest` names a lapsed
  offer (`lapsedHandoff`) instead of letting it vanish.
- `observatory_handoff_accept` ends the old lease and activates the new one in the same
  transaction, and returns the new `leaseId`. Acceptance is once per pack. The accepting
  session sends its `sessionId`, and a retry must send the same one: a replay returns the lease
  token, and every Claude session shares one identity.
- **A session that lost its own token** (after compaction, say) hands the workflow to itself with
  reason `restart` once its last checkpoint is two minutes old, and accepts it.
- A workflow is started with a `projectId`; its related records are read from that project only.
- An **active** lease has no expiry. Expiring it would leave the workflow with no writer, since
  the session it would expire away from is gone; the way to the next executor is a handoff.

Two partial unique indexes make "one active, one offered" a property of the table
(`workflow_one_active`, `workflow_one_offer` in `store/schema.sql`).

## The handoff pack

Assembled by the engine, immutable once written:

1. `constraints`: the latest checkpoint's constraints, first and verbatim. Then
   `credentials`: each declared key's state (`vault`, `env-only`, `missing`, `unknown`) with
   the command to use or store it, and `credentialsMissing`.
2. `checkpoint`: the latest checkpoint, with its body.
3. `git`: a fresh read of each git artifact's checkout — branch, a 12-character head and the
   paths that differ, never file contents. It is read through `safe_git`, so the checkout's
   hooks, filters and fsmonitor do not run. At most 10 checkouts and 200 paths each.
4. `related`: up to 20 records the lexical index relates to the goal and the open steps.
   Lexical only on purpose: a handoff made because a limit ran out must not depend on an
   embedding call. `degraded` names how many records are not indexed yet.
5. `transcript`: for a resume with the same provider, `{provider, sessionId}`. This is a
   pointer; the transcript is not stored.
6. `to`, `from`, `reason` (`limit`, `plan_route`, `operator`, `crash`, `restart`), and an
   `instructions` sentence: the pack is data written by agents, not instructions to the reader.

**Acceptance returns the checkpoint as it is now, beside the pack.** The previous executor holds
its lease until acceptance and may have written another step after the pack was made, so
continuing from the pack's copy would redo that step. `checkpointAdvanced` says when this
happened, and `constraints` in the answer are the newest checkpoint's.

## Guarantees and how they are enforced

| Guarantee | Mechanism | Proof |
|---|---|---|
| A lease cannot change hands between being checked and being used | each operation is one `BEGIN IMMEDIATE` transaction: the replay check, the lease check, the revision, the lease change and the idempotency record | `test_concurrent_acceptance_has_one_winner`: six threads accept one handoff and exactly one wins. Watched failing with a deferred `BEGIN` |
| A retry is not a second write | the same key with the same request returns the stored answer (`replayed: true`); a different request is `IdempotencyConflict` | `Idempotency` cases; a refused write replays its refusal without keeping the work twice |
| Checkpoints and packs are written only here | `ledger.append` refuses kind `checkpoint` or `handoff`, and any revision of such a record (`WORKFLOW_KINDS`) | `test_pack_is_immutable_through_the_ledger` |
| Nothing credential-shaped is stored | every string is redacted before the first write: credential shapes always, and the workspace's own secret values (env inventory and vault) when they can be read | `Redaction` cases; the store and the idempotency table are searched for the planted values |
| An account handle is not an address or a token | `accountRef` matches a narrow pattern with no `@`, and a credential-shaped handle is refused | `test_account_handles_are_opaque` |
| Workflow text stays on the machine | the indexer writes checkpoints and packs to the lexical index only and never sends them to the embedding provider | `Projection` case: only the ordinary note reaches the stub provider |
| Identifiers never carry a value | the project, step, model, provider and account fields refuse every credential shape; an idempotency key may be a UUID or a random run, never a provider key | `test_h06_identifiers_never_carry_a_value` |
| General listings stay small | recall, project notes and the observer's prompt leave checkpoints and packs out (`ledger.not_workflow`), and no listing carries a body column | `test_h01_listings_leave_workflow_bodies_out`: 176 KB for four rows before |
| Search answers with the current statement | indexing a revision removes the record's older ones from both indexes, and search serves the latest revision only | `test_h02_…`, `test_a_superseded_revision_is_never_served` |
| An open workflow cannot be erased into a dead end | a tombstone on an open workflow's checkpoint is refused; close it first | `test_h08_an_open_workflow_checkpoint_is_not_erased` |
| The committed export holds no workflow body | `tools/export_ledger.py` writes the row with `body_sha256` and no body | `test_h07_the_committed_export_carries_no_workflow_body` |
| A credential store is never read into a pack | a git artifact inside the secret store or a key directory (`~/.ssh`, `~/.gnupg`, `~/.aws`, …), symlinks resolved, is refused | `test_h12_…` |
| Reads never hand out the right to write | `observatory_checkpoint_latest` and `observatory_handoff_get` show the lease by `leaseRef`, never by token | wire test: no `wl_` string in a read |

When the known secret values cannot be read, shape redaction still runs and the answer says
`knownValuesChecked: false`: an honest partial, not a silent skip. The known values are re-read
as soon as the vault or the env inventory changes, so a rotated key is caught on the next write.
The report names the fields where text was replaced (`redacted.fields`), so the writer knows
what it lost. A full commit id in `head`, `ref`, `evidence` or `memory_refs` is cut to 12
characters before redaction (40 hexadecimal characters is also the shape of many keys), and a
`session:<uuid>` reference is kept whole.

## What the operator does

`project-observatory full workflow` (`tools/workflow_cli.py`):

| Command | What it does |
|---|---|
| `list [--project ID] [--status open\|closed\|all] [--json]` | the workflows, as `observatory_workflow_list` |
| `show WORKFLOW_ID [--json]` | one workflow: goal, step, constraints, open steps, executor, handoffs, keys |
| `handoff WORKFLOW_ID --to-provider P --reason R` | a handoff under the agents' rule (silence) |
| `handoff … --force` | the operator takes the workflow, for any reason; recorded as `operator-force`. Needs a terminal |
| `close WORKFLOW_ID --why WHY` | closes a workflow nobody will continue: a final checkpoint revision by the operator says why, every lease and offer ends. Needs a terminal |

The two acts that need a terminal carry the operator's authority, which a script must not be
able to mint — the same rule `tools/review.py` keeps. A workflow the operator closed keeps its
last checkpoint past the 30-day horizon, as every operator-owned record does.

The review queue (`project-observatory full review`) names a step kept after a lost lease as
such, with its workflow and the `workflow show` command: the decision is whether that work still
matters to the workflow, not whether a conclusion is true.

## What the operator sees

The dashboard's Health panel counts the open workflows and the handoffs waiting for a session
(`workflows_open`, `handoffs_waiting` in `dashboard/build_dashboard.py`): an offer nobody
accepts lapses, and a workflow stalls quietly unless someone can see it waiting. A step refused
for a lost lease is a `proposed` record, so it is counted with everything else awaiting the
operator's decision. Scenario OSS-23 in
[portable-scenarios.md](../../observatory/engine/docs/ux/portable-scenarios.md).

## Retention

| Kind | Kept | Configuration (`retention.json`) |
|---|---|---|
| `checkpoint` | while its workflow is open, whatever its age; then 30 days after it closes | `ledger.checkpoint_closed_days` |
| `handoff` | 90 days from creation | `ledger.handoff_days` |
| `step_result` | by its state, like any record (`proposed`: 90 days) | `ledger.proposed_days` |
| idempotency answers | 7 days | `idempotency_days` |

The generic age rules skip checkpoints and packs (`retention.ledger_candidates`), so an open
workflow's checkpoint is never erased because the work took long. A workspace whose
`retention.json` predates these keys uses the defaults above.

## MCP tools

| Tool | Writes | Notes |
|---|---|---|
| `observatory_checkpoint_write` | yes | starts a workflow without `workflowId`; `close: true` ends it |
| `observatory_checkpoint_latest` | no | carries `degraded` |
| `observatory_workflow_list` | no | workflows newest first, with step, goal, executor, pending handoff, seconds since the last checkpoint and kept steps; how an agent finds its workflow again after a compaction lost the id |
| `observatory_handoff_create` | yes | reads git and the local index; spends nothing |
| `observatory_handoff_accept` | yes | returns the new `leaseId`, the constraints, the current checkpoint and the pack |
| `observatory_handoff_get` | no | status: `offered`, `accepted`, `expired`, `superseded`, `workflow-closed` |

Every refusal is a typed answer with `error`, `detail` and `remedy`; `LeaseLost` adds `keptAs`.
The caller is `agent:<name>` or `service:<name>`: the operator's authority cannot be claimed
over stdio, as for every other write tool here.

These are protocol surface, **not Fabric capabilities**. The pinned Fabric contract has no
memory capability family, and serving one would invent contract surface. When the contract
gains it, these tools are what its schemas are written from.

## Not built yet

- Search over agent memory: indexing only the latest revision of each record, a similarity
  floor with an explicit "nothing found", a field-level index of checkpoint bodies, and word
  forms in Russian and English.
- Local multilingual embeddings, chosen by measurement, with a separate index per model.
- Per-caller access bindings, read scopes by classification and redaction on output, needed
  before any caller other than the local operator's agents.
- An MCP transport other than stdio (Streamable HTTP on loopback, as a `fabric-service`).
- An evaluation set: recall, abstention, handoff continuation, injected instructions,
  forgetting and freshness.

They are rows OBS-03 to OBS-07 in the [backlog](../backlog.md).
