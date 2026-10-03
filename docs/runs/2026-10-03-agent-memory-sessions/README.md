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

Checked together on the stack's tip, `4a9600b` (branch `claude/agent-memory-eval`, which carries
W2–W5, OBS-13, OBS-07 and OBS-03), 2026-10-03. They were not checked per branch.

- `python -m unittest discover -s tests` (repository root): 124 tests, OK.
- `python tests/run_portable.py --jobs 4 --timeout 1200` (engine): 209 of 209 suites PASS,
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

W5 (OBS-12): the Agents page.
