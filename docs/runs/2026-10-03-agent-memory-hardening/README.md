# Agent memory hardening: the review findings of the first release (W1)

**Objective.** Fix what an adversarial review of the first release confirmed by experiment
(OBS-08). The full list of what is missing, and the order the rest is built in, is
[design/AGENT-MEMORY-PLAN.md](../../design/AGENT-MEMORY-PLAN.md); the behaviour is documented in
[design/AGENT-MEMORY.md](../../design/AGENT-MEMORY.md).

## What changed, finding by finding

| ID | Fix | Where | Proof |
|---|---|---|---|
| H-01 | recall, project notes and the observer prompt leave checkpoints and packs out; no listing carries a body column | `store/ledger.py` (`not_workflow`, `live`, `live_count`), `survey.py` `LIVE_LEDGER_WHERE`, `agent/observe.py` | `test_h01_…` |
| H-02 | indexing revision N removes the older revisions from both indexes; search serves the latest revision only | `store/indexer.py`, `survey.py` hydration | `test_h02_…`, `test_a_superseded_revision_is_never_served` |
| H-03 | a workflow starts with a project; related records never cross projects | `store/workflow.py` | `test_h03_…` (two cases) |
| H-04 | a handoff needs the lease token, or `limit`/`crash`/`restart` after two silent minutes, at most one offer a minute; the pack records its authority | `store/workflow.py` `_handoff_authority`, MCP `leaseId` | `test_h04_…` (two cases), wire test |
| H-05 | acceptance is bound to the accepting session | `handoff_accept(session_id)`, MCP `sessionId` | `test_h05_…` |
| H-06 | identifier fields refuse credential shapes | `_require` | `test_h06_…` |
| H-07 | the committed ledger export carries workflow rows without their body, with `body_sha256` | `tools/export_ledger.py` | `test_h07_…` |
| H-08 | an open workflow's checkpoint cannot be tombstoned | `ledger.tombstone` | `test_h08_…` |
| H-09 | the known-value cache reloads when the vault or env inventory changes | `memory_redact._sources_fingerprint` | `test_h09_…` |
| H-10 | full commit ids are cut to 12 characters in typed fields, `session:<uuid>` is kept, and the report names the fields that lost text | `memory_redact.KEEP`, `checkpoint_body` | `test_h10_…` |
| H-11 | `LeaseLost` says a retry needs a new key and how to recover a lost token; `workflow-closed` and `lapsedHandoff` | `store/workflow.py`, MCP remedies | `test_h11_…` |
| H-12 | a credential store is never read into a pack (symlinks resolved); `_atomic` refuses an open transaction; lease rows of long-closed workflows are pruned | `_sensitive`, `_atomic`, `prune_leases` in retention | `test_h12_…` |

**Mutation checks.** Each guard below was disabled in turn, and the suite was watched failing:
- `_sensitive`;
- the silence rule;
- the session in the acceptance digest;
- the tombstone guard;
- `revision <=` in the indexer;
- the listing filter;
- the cache fingerprint;
- the project scope of related records;
- the search hydration filter.

## Checks run

Run in this worktree on Python 3.14.7, 2026-10-03:

| Command | Result |
|---|---|
| `project-observatory full check` | 201 of 203 suites passed in the full run (6,118 assertions, 695 unittest cases, 14 skips). `handed_commands` failed because two messages named the `full workflow` command, which does not exist until W2; the messages were reworded. `backup_vault` timed out under the load of a parallel run. Both were rerun alone and pass (`backup_vault` in 105 s). |
| `tests/test_workflow_memory.py` | 44 cases pass |
| `tests/test_search_path.py` | 45 assertions pass, including `test_a_superseded_revision_is_never_served` |
| `python -m unittest discover -s tests` | 117 pass |
| `python3.11 -m compileall -q observatory` | passes |

Hosted CI runs on the pull request; this record does not claim it.

## Next task

W3 first part (OBS-10): workflows declare the credentials they need by name, and the handoff
verifies them in the vault; the rule written into the `handling-secrets` skill.
