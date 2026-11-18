# M1: one session count (OBS-550)

Module M1 of [CONTROL-PLANE.md](../CONTROL-PLANE.md): requirements CP-03 and CP-04, decision D1, contract
`sessions_view.summary()`. Status: released in 0.37.0 (#542).

## What

The Agents page counted "sessions working" from ledger rows of `kind='session'` under one hour old
(`agents_view.py`), written only by the Stop hook for turns that changed a watched repository. The machine page
counted coding-agent processes; the account manager (Switchboard) was never read. One machine read 2, 34 and 39
at the same minute. The count now has three facets, each with its source and time, and none is invented:

| Facet | Source | Not read |
|---|---|---|
| `processes` per agent (what runs) | the account manager's process scan; the machine survey's `agent:<name>` groups (`store/raw/machine.json`) when the manager was not read | `{}` and `sources.processes.from: null` |
| `managed` (of them, run by the manager) | `switchboard sessions --machine --json`, one call, 10 s | `null`, with `not-installed`, `timeout`, `failed` or `malformed` in `degraded` |
| `active_hour` (of them, with a turn in the last hour) | the manager's rows joined to `agents_view.recent_sessions` per project (OBS-597): a project counts at most its running sessions and at most its sessions with a turn in the last hour, so a turn of a session whose process ended counts nothing | `null`, with `no-store` or `unreadable`; `null` with `needs-session-list` when the manager was not read (the survey lists no session to join) |
| `ledger_hour` (not a facet: every session with a ledger turn in the last hour, running or not) | `agents_view.recent_sessions` | `null`, with `no-store` or `unreadable` |

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

`session.unmanaged`, one row with the count, never one per session (severity info until OBS-630; a warning since
when the manager answered, so the notifier sends it once; info when no manager is installed):

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
  a turn in one hour, `ledger_hour` reads 60 and `active_hour` at most 60.
- A ledger row names a project, never a process, so `active_hour` pairs running sessions and recent turns within a
  project: a running session that is silent beside an ended one with a recent turn in the same project is counted
  for it. The count stays a subset of the running sessions and of the recent turns. Without the manager's rows the
  Agents page shows the ledger's own count under its own words ("N sessions recorded a turn; which of them still
  run needs the account manager"), never as the running sessions' activity.
- One rule for "working" (OBS-599): `sessions_view.mark_active` marks each running session `active` when its
  project holds a ledger turn of the last hour (the sessions written to most recently take the turns first),
  `None` when the ledger was not read; `active_hour` is the count of the marked rows. The Agents page shows one
  list, the running sessions, and a row reads "working" only by `active`; the manager's own `quiet` and `state`
  (its transcript silent 30 minutes) stay in the file under their names and no surface reads them as
  "working": the Projects page and the tool's per-project `counts.working` read `active` through
  `sessions_view.session_rows` as well. The
  ledger's turns of the last day are no second list. A file written before the rule is joined on the page by
  the same function. Tests: `test_each_running_session_says_whether_it_is_active`,
  `test_mark_active_is_the_one_join` (suite `sessions_view`), `test_one_session_list_with_one_rule_for_working`
  (suite `agents_page`). Times on every page come from `dashboard/page_util.py` (OBS-598).

## Tests

`observatory/engine/tests/test_sessions_view.py` (suite `sessions_view`): the three facets from a manager fixture
(managed, unmanaged, quiet), a survey and ledger rows; no full path in the summary; manager not installed (no
command run, `null`, survey fallback); malformed answers; failed, timed-out and vanished commands; skipped rows;
fewer than the processes; no survey and no store; failing attribution; the file round trip; finding the CLI; Home
and Agents reading one function and showing the same numbers; the finding's text in both cases and its silence
when the manager failed or manages every session; the type's label and both catalogs. Neighbours updated:
`test_home_page`, `test_tick_budget` (the step's measured seconds), `test_pipeline` (the findings' new read),
`test_mcp_wire` and `test_wire_contract` (thirty tools).

## The watch (OBS-630)

Asked by the account manager on the operator's order of 2026-11-18 (Fabric board request `d712d49c`): Switchboard
starts and routes coding sessions, Observatory watches them and keeps the history, Fabric Dashboards shows them.
Module `sessions_monitor.py` (engine root); `sessions_view.summary(monitor=True)` calls it, the tick's
`coding-sessions` step and `observatory_sessions(refresh=true)` pass `monitor=True`, every other caller reads the
summary as before.

### Why routed sessions read unmanaged

Measured on the reference machine on 2026-11-18: 27 coding processes, 24 of them in a folder of an account group. The
reader asked only `switchboard sessions --machine --json`, which marks `managed` the processes Switchboard launched
(2) and carries no group field at all; the collector read `pool` from it, so every `account_group` was null. A
session started by hand in a group folder is routed all the same: Switchboard keeps the route in the folder's
`.claude/settings.local.json`, which points Claude Code at its local proxy (`ANTHROPIC_BASE_URL`,
`http://127.0.0.1:<port>/claude/<group>`). The fix reads the manager's own list of those routes and its rows:

| Route | Source | Row |
|---|---|---|
| `launch` | `--machine` says managed, the process has a launch id, or `sessions --rows` lists it on a group | `account_group`: the group of the account serving it (the row's account label joined to `accounts list`, in memory), else the launch's group |
| `folder-route` | `project bindings`: the longest bound folder that is the session's folder or holds it; Claude Code only (the route is a Claude Code setting) | `account_group` and `folder_group`: the binding's group |
| none | neither | `managed: false`, `session.unmanaged` counts it |

The same measure after the fix: 24 of 27 managed, each with its group; the 3 left run in folders of no group.

### Data

Fields added, none renamed. Each session row: `route`, `folder_group`, `manager_state` (the rows' word: `working`,
`quiet`, `monitoring`, `limit`), `busy` (`manager_state == "working"`), `quiet_minutes`. The summary: `host` (one
sample), `capacity` (the cap on live sessions, how many count, the board's share and use), `groups` (per group: local
accounts, with room, usage unknown, the nearest reset of a full one), `monitor` (`source`: `feed`, `cli` or null;
`at`; `settings`).

Sources, in order:

1. The manager's sessions feed `sessions-feed.json` (`OBSERVATORY_SESSIONS_FEED`, else the manager's data folder:
   `~/Library/Application Support/ai.passioncode.fabric-switchboard/` on macOS, `$XDG_DATA_HOME` or
   `~/.local/share` elsewhere). Its schema is not published; the reader takes `sessions` or `records` of kind
   `session` (pid, launch id, `pool`/`group`, `served_pool`, `state`/`why`, `quiet_minutes`, folder) and the newest
   `host` record (`load_per_core`, `memory_pressure`, `swap_gb`, `free_disk_gb`, `capacity`, `groups`). Read through
   `osprivacy.read_regular` (no link, at most 4 MB). Used when dated within 10 minutes; a field it lacks is named
   once in `degraded` (`feed/missing-fields`) and measured here where it can be.
2. Its CLI, each call bounded at 5 s: `--json sessions --rows`, `--json project bindings`, `--json capacity`,
   `--json accounts list`. `usage` alone names no group, so `accounts list` (the same usage with each account's group)
   is read instead. A group has room when one of its accounts here (on, not moved to another machine, not given to an
   agent) is under 100 % in every usage window measured in the last hour; usage older than that is unknown.

The host sample: load per core from `os.getloadavg()` over `os.cpu_count()`, memory pressure and swap from the memory
receipts (`memory_snapshot.read_sources`), free disk of the home volume, the capacity and the groups with room.

History: `store/sessions-series.json` (`paths.STATE`), one host sample a tick and per session (launch id, else
`pid:<n>`) `[at, state, last turn, group]`; 48 hours, at most 400 host samples, 200 sessions and 100 samples a
session; mode 0600; a write that fails is `history/unwritable` in `degraded`. Never written: an account label, a
token, a full folder path, the proxy's address.

### Findings and the notify path

`tools/coding_session_findings.py` reads the summary (`monitor_findings`), so the Findings page, the Agents page and
`observatory_sessions.findings` show the same rows. Each row has `owner` (`operator` or `account-manager`) and an
`action` that names a tool or a page, never a shell command.

| Type | Subject | When | Severity | Owner |
|---|---|---|---|---|
| `host.overloaded` | `host:sessions` | load per core at or over `machine.sessions_load_per_core_critical` (2.0) or memory pressure at or over `machine.sessions_memory_pressure_critical` (4, critical) | critical when both, warning when one | operator |
| `pool.no_room` | `pool:<group>` | a group with local accounts, none with room, none unknown | warning | account manager |
| `session.wrong_group` | `session:<key>` | the group serving a session differs from its folder's group | warning | account manager |
| `session.stalled` | `session:<key>` | the manager counts it working and it wrote nothing for `machine.sessions_stall_minutes` (30, never under the manager's own quiet line: past it a session outside a turn is `quiet`) | warning | operator |
| `session.unmanaged` | `machine:sessions` | sessions outside the manager (OBS-550), now excluding routed ones | warning (info with no manager) | operator |
| free disk under 5 GB | | already `host.disk_low` (under 5120 MiB, critical under 1024 MiB): not raised twice | | |

Russian titles in `dashboard/locales/ru.json`, labels in `finding_types.LABELS`. Sending: the engine's one path,
`tools/notify_findings.py`, pushes each warning or critical finding once per finding, severity and episode
(`finding.notified`, `UNIQUE(kind, ref)`), handing it to Fabric Dashboards' event feed when its host is installed (the
host relays it to the operator) or raising its own banner. Nothing here sends a message of its own.

### Wireframe (Agents page, sessions section)

```
Sessions on this machine
[ Sessions running 27 ] [ Managed 24 ] [ Active in the last hour 9 ]
Host read 12:34 UTC: load 5.25 per core; memory pressure normal; swap 0 GB; 24.2 GB free on the disk;
live sessions 12 of 40; the board 2 of 10; groups with an account with room: agents apps-soft passioncode.
What the watch found
 (warning) Host overloaded: load per core over its line
           Owner: you. End the sessions that wait ... (switchboard_capacity) ...
 (warning) Group default has no account with room
           Owner: the account manager. Add an account with room ... (switchboard_balance) ...
 (warning) 3 coding sessions run outside a lifecycle owner
           Owner: you. Open the findings
claude-code . 26 sessions
 Project | Folder | Managed | Account group | Last turn | State
```

### Failure behaviour

- No manager and no feed: no command runs, `managed: null`, `monitor.source: null`, the host still sampled, no watch
  finding (a group not read is never "no room").
- One command times out, fails or answers nonsense: `manager-rows`, `manager-routes`, `manager-capacity` or
  `manager-accounts` in `degraded` with `timeout`, `failed`, `not-installed` or `malformed`; the others are read.
- The feed absent: no gap (the CLI covers it); unreadable (a link, too large, not JSON) or stale: named, the CLI read.
- A host value not read (no load average on Windows, no recent memory receipt, the disk unmeasurable): None with
  `host/load-unavailable`, `host/pressure-unknown` or `host/disk-unreadable`.

### Tests

`observatory/engine/tests/test_sessions_monitor.py` (suite `sessions_monitor`): the route and the group of each
session (launch, folder route, none, codex in a bound folder), the serving account's group, busy and quiet, no label
or path in the summary, the four bounded read-only commands, the summary without the monitor unchanged, absent and
failing commands, malformed answers, host values not read, the fresh feed instead of the CLI, its missing fields, a
stale or linked feed, where it is looked for, room per group, the bounded history, each finding with its owner, fix
and stable subject, the settings and the stall line, the labels and Russian titles, the rows reaching the board and
the notifier, and the Agents page's host line and findings in both languages. Neighbours: `test_sessions_view`
(`session.unmanaged` is a warning with its owner).
