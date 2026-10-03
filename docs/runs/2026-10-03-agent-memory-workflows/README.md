# Agent memory for workflows: checkpoints, one executor, handoff packs

**Objective.** A workflow an agent runs must continue on another account of the same
provider, another model or provider, or a new session — including when the session that
leaves cannot answer because its quota ran out. Backlog row OBS-02. The design and every
guarantee with its proof are in [design/AGENT-MEMORY.md](../../design/AGENT-MEMORY.md).

## What changed

| Area | Change | Where |
|---|---|---|
| Store | migration `0008-agent-memory-workflows`: four ledger columns (`workflow_id`, `step_id`, `executor_json`, `body_json`), tables `workflows`, `workflow_leases`, `idempotency` | `store/schema.sql`, `store/migrate.py` |
| Write path | `ledger.insert_revision` writes a revision inside a caller's transaction; `append` refuses checkpoint and handoff records (`WORKFLOW_KINDS`); later revisions inherit the workflow fields (`_carried`) | `store/ledger.py` |
| Workflows | checkpoint write and read, the executor lease, handoff create, accept and get, idempotency keys, the git read and lexical related records, retention candidates | `store/workflow.py` (new) |
| Redaction | credential shapes and the workspace's known secret values are replaced before a workflow write is stored; the answer reports counts and whether known values were checked | `memory_redact.py` (new) |
| Index | checkpoints and packs go to the lexical index only, never to the embedding provider | `store/indexer.py` |
| Retention | open workflows keep their checkpoints; 30 days after close, 90 days for packs, 7 days for idempotency answers | `store/retention.py`, `defaults/retention.json` |
| MCP | `observatory_checkpoint_write`, `_checkpoint_latest`, `observatory_handoff_create`, `_accept`, `_get`; the instructions name them within the 1,800-character budget (1,797) | `mcp/server.py` |
| Dashboard | the Health panel counts open workflows and handoffs waiting for a session; Russian strings; scenario OSS-23 | `dashboard/build_dashboard.py`, `dashboard/locales/ru.json`, `docs/ux/portable-scenarios.md` |
| Docs | design page, access table, README capability row, conformance tool count (27 tools), compatibility matrix (eight migrations), backlog OBS-02…07, handoff pointer | `docs/` |

## Checks run

Run in this worktree on Python 3.14.7 (SQLite 3.53.4), 2026-10-03:

| Command | Result |
|---|---|
| `project-observatory full check` (every engine suite, each in its own sandbox) | 203 suites pass, 0 fail; 6,115 printed assertions, 713 unittest cases, 14 explicit skips |
| `python tests/test_workflow_memory.py` | 30 cases pass (the new suite; also inside the run above) |
| `run_workflow` in `tests/test_mcp_wire.py` | the handoff between two sessions over real stdio: 8 assertions pass |
| `python -m unittest discover -s tests` | 117 root tests pass, including the source-inventory gate |
| `python3.11 -m compileall -q observatory` | passes |
| `pip wheel` + `python tools/check_package.py` | 495 runtime files, no failures; the wheel carries `store/workflow.py` and `memory_redact.py` |
| `tools/check_public_release.py --history` | see the pull request: run on the pushed branch |

The first full run failed one suite, `tests/test_dead_data.py` (below), and passed after the fix.
Hosted CI runs nightly by the organisation's policy; this record does not claim it.

**Mutation checks.** Each guard was disabled in turn and the suite was watched failing:
the token comparison, the offer expiry, the idempotency digest, the workflow-kind guard in
`ledger.append`, shape redaction, the retention filter for open workflows, the double-accept
refusal, `BEGIN IMMEDIATE` (replaced with a deferred `BEGIN`; caught only after the race test
was given a pause inside the transaction — without it the six threads ran one after another),
the transcript pointer, the embedding exclusion in the indexer and the generic-age exclusion in
retention. All eleven failed the suite; the restored code passes.

**Found while building, fixed with a test:**

- The acceptance answer took `constraints` from the pack. A step written after the pack, still
  under the old lease, can tighten them; the answer now carries the newest checkpoint's
  constraints and `checkpointAdvanced`.
- Shape redaction replaced the transcript's session UUID in the pack. The pointer is validated
  as a UUID and now added after the scrub.
- `workflows` and `workflow_leases` were written and read only by their own module — the
  dead-data rule (`tests/test_dead_data.py`) refused it in the first full run. The operator
  now sees both on the Health panel, driven end to end by `test_agent_workflows_reach_the_operator`.
- Migration 0008 assumed a ledger table; a store built by hand for another migration
  (`tests/test_time.py`) has none.

## Not done here, deliberately

- Search over memory (OBS-03), local embeddings (OBS-04), per-caller bindings (OBS-05), an HTTP
  transport (OBS-06) and the evaluation set (OBS-07) — see the design page.
- No release, version bump or `CHANGELOG.md` entry: a feature pull request does not carry them.
- Live acceptance with real sessions on two accounts is a separate, operator-assisted run.

## Next task

OBS-07 first: the evaluation set, run against today's search as the baseline, so that OBS-03's
floor and ranking are tuned by measurement rather than by guess. Then OBS-03.
