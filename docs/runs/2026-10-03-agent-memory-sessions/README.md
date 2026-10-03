# Sessions linked to workflows (W4)

**Objective.** OBS-11 in [the plan](../../design/AGENT-MEMORY-PLAN.md#w4--sessions-linked-to-workflows).
Behaviour: [design/AGENT-MEMORY.md → Sessions and stalls](../../design/AGENT-MEMORY.md#sessions-and-stalls).

| Plan item | Change | Proof |
|---|---|---|
| L-01 | the Stop hook writes `OBSERVATORY_WORKFLOW_ID` on the session record (`ledger.append(workflow_id=…)`, carried to later revisions); a malformed value is ignored | `test_the_stop_hook_records_the_workflow_from_its_environment` (drives `tools/record_turn.py` on a real checkout), `test_a_session_record_carries_its_workflow` |
| L-02 | `scan_sessions` reads the ledger's `session` records, and the companion's store only where installed; the companion's absence becomes not applicable once the hook's records answer | `test_the_stop_hook_records_are_the_history_without_the_companion` in `tests/test_sessions.py` |
| L-03 | `stalled`, `silentSeconds`, `lastSessionAt` in `observatory_workflow_list` | `test_a_silent_held_workflow_is_stalled_until_a_session_moves` |

**Mutation checks.** Each guard was disabled in turn and the suite was watched failing:
- dropping the workflow from the hook's append;
- disabling the stall rule.

## Checks run

CHECKS_PLACEHOLDER

## Next task

W5 (OBS-12): the Agents page.
