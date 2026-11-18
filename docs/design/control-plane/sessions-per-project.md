# Sessions per project

Status: built (branch `feat/obs-sessions-view`), 2026-11-16.

## The question it answers

A project page says who works on the project now and what each session last did. The machine-wide count of
coding sessions (OBS-550, [`as-is.md`](as-is.md), CONTROL-PLANE CP-03/CP-04) already attributes each running
session to a project; this cut groups it by project and joins it with the project's workflows and checkpoints.

## Sources, each kept as what it is

| Part | Source | What it says |
|---|---|---|
| `live` | the account manager's rows (`switchboard sessions --machine --json`), as the tick's `coding-sessions` step wrote them (`sessions_view.load()`) | who runs in the project now: agent, folder name, managed or not, account group, launch id, `active` (working by the one rule, OBS-599) and the manager's `state`, last turn |
| `recent` | the ledger's session rows of the last day (`agents_view.recent_sessions`) | which sessions recorded turns for the project, how many, and the workflow each carried |
| `workflows` | the store's open workflows (`store.workflow.workflow_list`) | the project's open workflows, their last step, status, time, goal and holder |

`recent[].workflow` is joined by workflow id: the step its workflow last wrote is what that session last did. A
session whose workflow is closed still names its last step; one whose workflow is gone keeps the id only. The
account manager's rows carry no session id, so a live row and a ledger row are not joined to each other: they are
two facts about the same project, shown side by side.

## Row model (shared with the other session views)

The live row keeps the account manager's own field names and meanings (`agent`, `managed`, `quiet` as `state`,
`last_write_at` as `last_turn_at`, `launch_id`, the account group), so Switchboard, Fabric Dashboards, Inbox and this
page read one row the same way. Beside them each row carries `active` (OBS-599, `sessions_view.session_rows`): it
runs and its project recorded a turn in the last hour. `active` alone makes a row "working" and feeds
`counts.working`; the manager's `state` stays in the row as its own word. Never in a row: a pid, a full folder path, a lease token or a checkpoint body.

## Surfaces

- **MCP:** `observatory_sessions(projectId="project:<slug>")` answers the machine-wide document plus `project`:
  `{projectId, live, recent, workflows, counts}`. The workflow part honours the caller's memory binding: a binding
  limited to some workflows sees only those (and only the ledger sessions that carried them); a binding below
  `project-internal` sees the live rows only, with `workflows/not-readable` in `degraded`.
- **Dashboard:** the project panel's section "Who works on it now" (`#panel-sessions`): running now, what each
  session last did, open workflows. The payload is `project_sessions`, built by `sessions_view.projects()` from
  the same file the Agents page reads.

## Honest degradation

A store that does not exist or cannot be read leaves `recent` and `workflows` empty and says so in `degraded`
(`ledger/no-store`, `ledger/unreadable`, `workflows/before-agent-memory`); the live rows still show. Each block is
bounded to 20 rows per part; `counts` keeps the true totals.

## Tests

`tests/test_sessions_view.py` `ByProjectTest`: the join, ordering, the closed and the vanished workflow, malformed
sources, the bound, a missing store and a caller who may not read workflow memory.
