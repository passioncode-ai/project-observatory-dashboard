# M1: one session count (OBS-550)

Module M1 of [CONTROL-PLANE.md](../CONTROL-PLANE.md): requirements CP-03 and CP-04, decision D1, contract
`sessions_view.summary()`. Status: built on branch `feat/sessions-view`, 2026-11-14.

## What

The Agents page counted "sessions working" from ledger rows of `kind='session'` under one hour old
(`agents_view.py`), written only by the Stop hook for turns that changed a watched repository. The machine page
counted coding-agent processes; the account manager (Switchboard) was never read. One machine read 2, 34 and 39
at the same minute. The count now has three facets, each with its source and time, and none is invented:

| Facet | Source | Not read |
|---|---|---|
| `processes` per agent (what runs) | the account manager's process scan; the machine survey's `agent:<name>` groups (`store/raw/machine.json`) when the manager was not read | `{}` and `sources.processes.from: null` |
| `managed` (of them, run by the manager) | `switchboard sessions --machine --json`, one call, 10 s | `null`, with `not-installed`, `timeout`, `failed` or `malformed` in `degraded` |
| `active_hour` (of them, with a turn in the last hour) | `agents_view.recent_sessions`, the query the Agents page always used | `null`, with `no-store` or `unreadable` |

## Data

`sessions_view.py` (engine root):

- `summary(home=None, *, now, runner, manager, attribute)`: reads the three sources now. The contract's shape plus
  `schema: coding-sessions/1`, `measuredAt` and `quiet`. Each session: `agent`, `project` (an id or null),
  `folder_name` (the last component only; a full path is never written), `pid`, `managed`, `launch_id`,
  `account_group` (the manager's `pool` when it gives one), `last_turn_at`, `quiet`, `state` (`working` or `quiet`).
  Projects come from the Fabric contract's path rules (`folder_attribution.attribute_folder` over the registry's
  folders, Git left out: one registry read for every session, no command per folder). A worktree outside every
  project folder therefore reads no project.
- `write(doc, out)` and `load(home)`: the tick's file `store/raw/coding-sessions.json`. The contract table names
  `store/raw/sessions.json`, which is already `collectors/scan_sessions.py`'s output, hence the other name.
- `facets(doc)`: the three numbers and their sources. Home's Agents tile (`home_page.agents_tile`) and the Agents
  page's counters (`agents_page._session_tiles`) both read it, so the two pages cannot disagree.

The tick runs `sessions_view.py` as the collector step `coding-sessions`, under `features.machine_watch`, after
the machine survey. `tools/coding_session_findings.py` reads the file for `tools/build_findings.py`.
`observatory_sessions` (MCP, read-only) answers the file, or a live summary with `refresh: true` or before the
first tick wrote one.

## Finding

`session.unmanaged`, severity info, one row with the count, never one per session:

- the manager answered and lists sessions it does not manage: "N coding sessions run outside a lifecycle owner",
  by agent and project, and the action: start sessions through the account manager (`switchboard launch
  --provider claude --working-directory FOLDER`) or an application's embedded agent;
- the manager is not installed and the survey counts sessions: "N coding sessions run without an account
  manager", what it adds (account per group, switching before a limit, hand-over, end, logs) and the setup command
  (`npx @passioncode-ai/passioncode doctor`).

A manager that is installed but failed raises nothing: that is a gap in the reading (`degraded`), not a fact.

## Failure behaviour

- Manager absent, slow, failing or answering nonsense: `managed: null`, `processes` from the survey, a named
  `degraded` entry. Never 0.
- A row of the answer that is not a session (no integer pid, no agent): skipped and counted (`rows-skipped`).
- The manager lists fewer sessions of an agent than a survey under 30 minutes old counted: reported as
  `fewer-than-processes` with the numbers (D1: reported, not hidden); the manager's own count is kept.
- Attribution that raises: the session is kept without a project; `degraded` names the attribution.
- The file missing or unreadable: `load` answers the empty shape with `not-collected` or `unreadable`.
- The ledger list is `agents_view.MAX_SESSIONS` (60) sessions of the last day, newest first: past 60 sessions with
  a turn in one hour, `active_hour` reads 60.

## Tests

`observatory/engine/tests/test_sessions_view.py` (suite `sessions_view`): the three facets from a manager fixture
(managed, unmanaged, quiet), a survey and ledger rows; no full path in the summary; manager not installed (no
command run, `null`, survey fallback); malformed answers; failed, timed-out and vanished commands; skipped rows;
fewer than the processes; no survey and no store; failing attribution; the file round trip; finding the CLI; Home
and Agents reading one function and showing the same numbers; the finding's text in both cases and its silence
when the manager failed or manages every session; the type's label and both catalogs. Neighbours updated:
`test_home_page`, `test_tick_budget` (the step's measured seconds), `test_pipeline` (the findings' new read),
`test_mcp_wire` and `test_wire_contract` (thirty tools).
