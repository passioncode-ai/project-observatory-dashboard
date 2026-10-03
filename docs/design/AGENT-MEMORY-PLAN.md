# Agent memory: what is missing, and the plan to finish it

Written 2026-10-03, after the first part shipped (OBS-02, #127). Three read-only passes looked
at it from different sides: an adversarial review of the merged code, run with experiments on
synthetic stores; a UX pass over the dashboard and its neighbours; and a pass over how agents
being built get their credentials. Every item below came out of one of them. Each row names the
failure it prevents, its fix and the test that proves it, and is executed in the wave order at
the end.

## The rule this plan enforces

**Every credential an agent uses comes from Project Observatory, by name.** It is issued through
a provider door or put into the vault, discovered with `observatory_credentials`, and injected at
run time (`use_secret.py run`, or `use_secret.py serve` for a long-running service). An agent has
no `.env` of its own, no private copy of a key in a file next to its data, and never a value in
code, chat, memory or a log. Agent memory carries credentials by **name** only. This applies to
one-shot agents, long-running agent services and remote services alike; a remote platform's
copy is recorded with `vault.py moved`.

## W1 — correctness of what shipped (review findings)

| ID | Severity | Failure | Fix | Proof |
|---|---|---|---|---|
| H-01 | P1 | `observatory_recall` and `ledger.live` return checkpoint and pack bodies whole: 1 checkpoint and 3 packs gave a 176 KB page against a promise of a few KB | `live` returns no `body_json`/`executor_json`, and leaves out checkpoints and packs unless asked; workflows are read with their own tools | a recall over a store with packs stays small and carries no body |
| H-02 | P1 | the search index keeps every revision, so a 30-step workflow answers a search with up to 30 stale checkpoints | indexing revision N removes the record's older revisions from both indexes; search keeps the latest revision only | two revisions give one hit, the latest |
| H-03 | P1 | a workflow with no project put other projects' records into its pack | a workflow starts with a `projectId`; related records never cross projects | a pack holds no record of another project |
| H-04 | P2 | anyone on the machine can take a working executor's workflow, and repeated offers bloat the store | a handoff needs the current lease token, or a silent executor (no checkpoint for `workflow_silence_minutes`); a forced handoff is recorded as such; one offer per workflow per minute | a handoff without the token while the executor is active is refused |
| H-05 | P2 | a replayed acceptance gives the lease token to any session of the same identity | acceptance carries the accepting session's id, which is part of the request | a replay from another session is refused |
| H-06 | P2 | key-shaped project, step, model and idempotency values bypass redaction and are stored raw | every identifier field refuses a credential shape | each field refuses one, and nothing reaches the store |
| H-07 | P2 | the committed ledger export copies every checkpoint and pack body | workflow records are exported without their body, with its SHA-256 | the export of a store with a pack has no body |
| H-08 | P2 | tombstoning an open workflow's checkpoint makes the workflow impossible to continue or hand over | erasing an open workflow's checkpoint is refused; close the workflow first | the refusal, and the erasure after close |
| H-09 | P2 | a value rotated in during the cache window can be stored unredacted | the known-value cache reloads when the vault or the env inventory changes | changed values are caught on the next write |
| H-10 | P3 | redaction destroys legitimate ids: a full commit id, a session UUID | typed fields normalise a commit to 12 characters and accept `session:<uuid>`; the report says which fields lost text | a commit and a session reference survive |
| H-11 | P3 | the remedies and statuses mislead: a refused write retried with the same key conflicts; a closed workflow's offer reads `ended`; an expired offer is hidden but not recorded | `LeaseLost` says to use a new key; statuses name `workflow-closed` and `expired`; `checkpoint_latest` shows the lapsed offer | one assertion each |
| H-12 | P3 | a git artifact may point anywhere, the vault included; lease rows are never pruned; `_atomic` silently commits a caller's transaction | git paths outside the vault and secret store only; lease rows of long-closed workflows pruned; `_atomic` refuses an open transaction | one test each |

## W2 — agents can find and recover their work

| ID | Gap | Change |
|---|---|---|
| A-01 | an agent after compaction cannot find its workflow without the id | `observatory_workflow_list(projectId, status)` |
| A-02 | an agent that lost its lease token cannot continue its own workflow | a documented self-handoff (`reason: restart`), allowed to the same project's agents once the executor is silent, and named in the `LeaseLost` remedy |
| A-03 | the operator cannot see, hand over or close a workflow without an agent | `project-observatory full workflow list|show|handoff|close`; `close` and `handoff --force` only from a terminal |
| A-04 | steps kept after a lost lease are mixed into the general review queue | the queue and the dashboard name them as such, linked to their workflow |

## W3 — credentials through Observatory

| ID | Gap | Change |
|---|---|---|
| S-01 | nothing in a workflow says which credentials it needs | a checkpoint field `credentials: [{project, env, name, purpose}]`, names only, a value refused |
| S-02 | a new executor learns of a missing key only when the step fails | the pack and the acceptance report each credential as `vault`, `env-only` (non-compliant) or `missing`, with its `use` command, and `credentialsMissing` stops the continuation |
| S-03 | a redaction marker does not say where the value lives | `[redacted:vault:<project>/<env>/<NAME>]` or `[redacted:env:<project>/<NAME>]` |
| S-04 | a known value written into memory leaves no trace for the operator | a `secret.seen_in_agent_memory` finding with the `vault.py leak` command; the write itself succeeds, redacted |
| S-05 | the Observatory store is not in the leak scan | the leak scan reads it incrementally; a hit means the redactor missed one |
| S-06 | `use_secret run` silently falls back to a project's `.env` | `--vault-only`, the default for agents and for `serve`, and a `agent.secret_fallback_used` finding |
| S-07 | a long-running agent service has no supported way to get its keys | `use_secret.py serve`: forwards signals, leaves the service's output alone, records the consumer; `vault.py rotate` lists the consumers to restart |
| S-08 | an agent project's own `.env` secrets go unnoticed | an `agent.secret_outside_vault` finding with the `vault.py put` command |
| S-09 | the rule is written where agents do not read it | the `handling-secrets` skill gets a section for building agents and services; a design page `AGENT-SECRETS.md`; the organisation's rules and the agent-building skills are updated in their own repositories |

## W4 — sessions linked to workflows

| ID | Gap | Change |
|---|---|---|
| L-01 | nothing ties a running session to the workflow it executes | the Stop hook records `OBSERVATORY_WORKFLOW_ID` on the session record; a checkpoint's `sessionId` links the other way |
| L-02 | the session history reads a retired companion store and is always degraded | it reads the Stop hook's own `session` records |
| L-03 | a stalled executor cannot be told from a busy one | a stall is derived: open workflow, active lease, no checkpoint and no session turn for `workflow_stall_minutes` |

## W5 — the Agents page

A new dashboard page answering *which agents are working, on what, and where their work moved*:

- **counters**: active sessions, open workflows, handoffs waiting, stalled, steps kept after a lost lease;
- **needs you**: stalled workflows, offers expiring or expired, kept steps, missing credentials — each with its reason and a command to copy;
- **workflows by project**: goal, current step and status, executor (provider, model, account handle), age of the last checkpoint, handoffs, state;
- **one workflow**: a lane per executor, a dot per checkpoint, an arrow per handoff with its reason (dashed while offered, crossed when it lapsed), and beside it the constraints, the next action, the git artifacts and the credentials by name — the same events as an ordered list for screen readers;
- **sessions**: running agent sessions with their project, last turn and workflow.

The page is built on every tick like the others. When it is opened through the local server it also polls a new read-only route, `/workflows`, every 15 seconds, and says when live updates pause. It only shows and copies commands; it changes nothing. Scenarios OSS-24 to OSS-28.

## W6 — the rest of the original plan

OBS-07 (evaluation set), OBS-03 (search: floor, abstention, word forms — its latest-revision part is H-02), OBS-04 (local embeddings), OBS-05 (per-caller bindings), OBS-06 (HTTP transport), and the Switchboard session switch, which starts the accepting session (the pack otherwise waits until it lapses).

## Order

W1 → W3 (S-01, S-02, S-03, S-06, S-09) → W2 → W4 → W5 → W3 (S-04, S-05, S-07, S-08) → W6.
Correctness first, because every later wave builds on these records. The credential rule next,
because it changes what a checkpoint carries. Then what agents and the operator need to act,
then what they need to see.
