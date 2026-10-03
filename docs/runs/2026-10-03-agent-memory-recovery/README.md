# Agents find and recover their work; the operator can see, move and close it (W2)

**Objective.** OBS-09 in [the plan](../../design/AGENT-MEMORY-PLAN.md#w2--agents-can-find-and-recover-their-work).
Behaviour: [design/AGENT-MEMORY.md](../../design/AGENT-MEMORY.md) → MCP tools, What the operator does.

| Plan item | Change | Proof |
|---|---|---|
| A-01 | `observatory_workflow_list` and `workflow.workflow_list`: newest first, per project or status, paged, with step, goal, executor, pending handoff, silence and kept steps; never a token | `test_list_finds_a_workflow_without_its_id`; `run_workflow` in the wire test |
| A-02 | a session that lost its token hands the workflow to itself (`restart`, after the silence) and continues; the `LeaseLost` remedy says so | `test_a_session_that_lost_its_token_takes_its_workflow_back` |
| A-03 | `project-observatory full workflow list\|show\|handoff\|close`; `handoff --force` and `close` need a terminal; `close_workflow` writes the operator's final checkpoint revision and ends every lease | `OperatorCommand` cases, `test_the_operator_closes_an_abandoned_workflow`, `test_only_a_forced_handoff_may_be_the_operators` |
| A-04 | the review queue names a step kept after a lost lease, with its workflow | `test_the_review_queue_names_a_kept_step_and_its_workflow` |

**Found while building.** Workflow retention ignored `owner_exempt`. Before W2 no operator could
own a workflow record, so nothing was erased wrongly. W2 lets the operator close a workflow,
which made the gap reachable, so retention now skips exempt owners as every other retention
query does (`test_the_operator_closes_an_abandoned_workflow`).

**Surface counts.**
- The MCP server lists 20 `observatory_*` tools, 28 in all (`test_wire_contract.py`,
  `FABRIC-CONFORMANCE.md`).
- The instructions are 1,799 characters of 1,800.
- The handed-commands check has samples for the new placeholders.

## Checks run

Checked together on the stack's tip merged with `main` (0.14.0 and the header door), `687320f`
(branch `claude/agent-memory-eval`, which carries W2–W5, OBS-13, OBS-07 and OBS-03), 2026-10-03.
They were not checked per branch.

- `python -m unittest discover -s tests` (repository root): 124 tests, OK.
- `python tests/run_portable.py --jobs 4 --timeout 1200` (engine): 210 of 210 suites PASS,
  6206 assertions.
- Two earlier full runs on this stack found five defects, all fixed before this one:
  - the runtime check did not know the stemmer;
  - the indexer failed on an index without search keys;
  - an erasure test assumed the old index timing;
  - `observatory_record` redaction was quadratic: about an hour on a 2 MB statement, now 0.26 s;
  - a fixture used a key name that matched the maintainer's denylist.

  Load averages of 27–41 timed out `write_surface` (the quadratic redaction) and once failed
  `mcp_inventory`, which passes alone.
- Privacy (`tools/check_public_release.py --history --private-denylist`): the lines this stack
  adds name no private identifier once the fixture key and one private repository name were
  neutralised. The gate's remaining findings are in history already on `main`.
- Hosted CI: the pull request's three required rows.

## Next task

W4 (OBS-11): the Stop hook records the workflow a session executes, the session history reads the
hook's own records, and a stall is derived.
