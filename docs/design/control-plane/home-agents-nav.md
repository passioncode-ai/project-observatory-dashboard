# Home, Agents and navigation (M2, OBS-551, CP-01, CP-02, CP-17)

## What

- **Home** answers "is everything fine, and what needs me first" in one screen, each fact once, in four groups:
  State (the six tiles), Needs you first (the three kinds of problem to do first, one line each with a link to the
  finding), Work (the workflows open and the handoffs waiting, then the activity windows), Estate (projects by
  organization). A section card follows only for a page no tile and no group speaks for. No command text: a
  problem links to its finding; a first-run step or a notice is a button that copies its command without printing it.
- **Agents** opens with the sessions running on this machine (OBS-550), then the problems, one row per workflow with
  its worst state and a button, then the workflows with their counts, then the turns the ledger recorded.
- **Navigation** starts with a Monitoring group: Health, Security, Availability. Security and Availability are not
  pages: each opens the Findings page narrowed to its group. The Integrations page is an entry inside Availability
  (`page_table.NAV_PARENTS`, OBS-619), drawn indented under it on a wide screen; its address stays
  `integrations.html`. Then Work, Infrastructure, Access, System (Machine, Installed tools, Disk).

## Data

- Home: the build's payload, as before (`home_page.py`). The Work group reads `agents.counters` (`workflowsOpen`,
  `handoffsWaiting`); the Agents tile keeps sessions and stalled. The page script draws the activity windows into the
  `#work` holder the Work group carries; on the index page `shell.page_html` removes the template's own Activity
  heading and holder. `shell.HOME_COVERED` lists the pages whose card Home leaves out.
- Agents: `payload["sessions"]` (`sessions_view`, `coding-sessions/1`): the three facets with their sources, and each
  session's agent, project, folder name, managed, account group, last turn and state. `agents.needsYou` is grouped
  by workflow; the worst kind decides the row (`agents_page.WORST`).
- Navigation: `finding_types.GROUPS` maps a finding type (or a prefix) to `security` or `availability`; the builder
  sends `findings.type_groups`; `shell.counts_of` counts open findings per group for the badges.

## Wireframe, Home

```
1440x900                                                      390x844
+---------+-------------------------------------------------+ +---------------------------+
| Monitor | Home  Is everything fine, and what needs you    | | Home                      |
|  Health | [Sites][Apps][Disk+mem][Keys][Agents][Cloud]    | | [Sites] [Apps]            |
|  Secur. | Needs you first                                 | | [Disk]  [Keys]            |
|  Avail. |  (crit) Domain on hold: alpha  -> alpha  Review | | [Agents][Cloud]           |
| Work    |  (crit) Heroku app down  open: 2       Review   | | Needs you first           |
|  Home   |  (warn) ...                            Review   | |  (crit) ... Review        |
|  ...    |  All problems (5)                               | | Work                      |
| Infra   | Work                                            | |  4 workflows open . 0 ... |
| Access  |  4 workflows open . 0 handoffs waiting (link)   | |  [commits 7 d] [...]      |
| System  |  [commits 7 d][projects 7 d][...][most work]    | | Projects at a glance      |
|         | Projects at a glance  [org: n projects ...]     | |  example-org 2 projects   |
|         | [ENV card][MCP card][Traffic][Health][Tools]    | | [ENV] [MCP] ...           |
+---------+-------------------------------------------------+ +---------------------------+
```

## Wireframe, Agents

```
1440x900
Sessions on this machine
  [Sessions running 3][Managed 1][Active in the last hour 1]   (each with its source and time)
  claude-code . 2 sessions
    Project | Folder | Managed | Account group | Last turn | State
    Alpha   | alpha  | yes     | team-a        | 17:20     | working
    -       | scratch| no      | -             | -         | quiet
  codex . 1 session ...
Needs you
  (stalled) wf_... . beta-api: stalled for 60 min
     also: a handoff lapsed
     To [provider v]  Because of [crash v]  [Offer to another agent]  Open the workflow
  (no key) wf_... . alpha: key STRIPE_KEY (prod) is not in the vault
     also: 1 step kept ...   Open the keys  Review the kept steps  Open the workflow
Workflows  [Workflows open][Handoffs waiting][Stalled][Steps kept]
  project: workflow cards (details, id="wf-...")
Recorded turns (the ledger's last day)

390x844: the same order in one column; table rows stack with their labels; the controls of a
problem row wrap under it.
```

## Contract

- `POST /api/workflow-handoff` on the key server, `{workflowId, provider, reason}`: `workflowId` a `wf_` id,
  `provider` an executor provider (`store/workflow.executor_shape`), `reason` one of `limit`, `crash`, `restart`
  (the reasons a handoff without the executor's lease may give). It calls `store/workflow.handoff_create` as
  `service:observatory-page` (the store refuses the operator as a handoff's owner outside the terminal), the same
  function as `observatory_handoff_create`; the store refuses a workflow that is closed, has no checkpoint, whose
  executor wrote within two minutes, or was offered less than a minute ago. Audited before the act with the
  workflow id, provider and reason. The answer is `{handoffId, workflowId, expiresAt, credentialsMissing,
  degraded}`, never the pack or a token. 400 for a refusal, 404 for an unknown workflow.
- `parity.json`: dashboard action `workflow-handoff`, `covered` by `observatory_handoff_create`. The other actions of a
  problem row are links: a key's value is typed by a person (Keys page), kept steps are reviewed in the Health
  page's queue.
- `findings.html?group=security|availability` narrows the Findings page to the types of `finding_types.GROUPS`;
  the reset clears it.

## Failure behaviour

- Opened as a file, or served without the token: the Agents controls stay disabled, each with a tooltip naming
  `observatory_handoff_create`; the page script enables them only when `LIVE`, and again after each live refresh.
- A refusal shows "not offered" with the store's reason on the row; a dropped connection shows that the outcome is
  unknown and that the next refresh tells.
- A session list the account manager did not give says so with its reason, never "no session"; a facet not read is
  a dash with its reason (CP-03).
- No findings built: Security and Availability carry no badge, never 0.

## Tests

- `observatory/engine/tests/test_home_page.py` `ControlPlaneHome`: the four groups in order, each fact once
  (no card for a page a tile or group covers, and no shown value twice: `test_no_value_is_shown_twice_on_home`), no
  command text in any state.
- Viewport screenshots of Home at 1440x900 and 390x844, both languages, WebKit, with the command that makes them:
  [shots/](shots/README.md) (OBS-608).
- `observatory/engine/tests/test_agents_page.py` `ControlPlaneAgents`: sessions first, grouped by agent and project,
  over managed, unmanaged and quiet sessions; one row per workflow with its worst state, its button disabled with
  the tool name; `HandoffRoute`: the route offers a silent workflow and refuses bad input.
- `observatory/engine/tests/test_dashboard_shell.py` `MonitoringNavigation`: the group order, the narrowed links,
  the group counts and the type groups, and Integrations inside Availability in both languages.
- `tests/test_agent_parity.py`: the new action has its tool.
