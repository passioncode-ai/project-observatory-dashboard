# The estate control plane: sessions, inventory, security watch, keys and the built-in agent (OBS-549 to OBS-563)

Status: design, 2026-11-14. Owner: this repository. Builds on [PERSON-SURFACES.md](PERSON-SURFACES.md) (home, agents,
sharing; OBS-260 to OBS-276) and [HARNESS.md](HARNESS.md) (skills, plugins, hooks; H1 to H17), and changes neither:
where this record and one of them meet, the row below names the older decision it extends.

The person who runs Observatory asked, on 2026-11-14, for one product a person can read without help and an agent can
drive without a person: the home and agents pages grouped and compact; every coding session visible and managed through
its lifecycle; the skills, plugins and agents installed here on a page; a security watch over dependencies, leaked keys,
MCP servers, skills and plugins; keys held, received and shared kept apart; integrations that check themselves and open
issues; a built-in agent that audits the estate every day and fixes what it safely can; and, from day 0, nothing a
person can see or press that an agent cannot read or call. The request, verbatim and with its private context, is kept
by the operator with the run record; this page carries the requirements, the decisions and the build order.

The research behind it is two files beside this one: [control-plane/as-is.md](control-plane/as-is.md) (what the engine
does today, measured on `main` at `78cdabe2`, every claim with its file and line) and
[control-plane/research.md](control-plane/research.md) (the public data sources and tools, with their URLs).

## What exists today (summary of as-is.md)

| Area | Today | Gap |
|---|---|---|
| Home | six tiles, three findings, projects by organization, activity, then a grid of section cards (`home_page.py`, `shell.py:331-373`) | five facts appear twice (disk, apps, agents, keys, problems); a finding row can carry a multi-line shell command |
| Agents | four counters, then "Needs you": one row per workflow state with a raw `project-observatory full workflow ...` command (`agents_page.py:83-116`, `agents_view.py:192-210`) | sessions are not on the page; a workflow that is both stalled and lapsed prints twice; commands are shown to people |
| Sessions | "sessions working" counts ledger rows of `kind='session'` under one hour old, written only by the Stop hook for turns that changed a watched repository (`agents_view.py:36-183`, `record_turn.py`) | the account manager (Switchboard) is never read; the machine page counts every coding-agent process by origin; the three numbers disagree (measured: 33, 2 and 1 on one machine at 17:05 UTC) |
| Skills, plugins, coding agents | collected by `scan_harness.py` (H16, H17), served by `observatory_machine(section="skills")` | no dashboard page shows them |
| MCP servers | `mcp.html` lists declarations and connection state (`scan_mcp.py`) | tool descriptions are not pinned; a server's command line is not judged |
| Dependencies | stack from marker files; direct dependency counts only (`plugins/dependency_weight.py`) | no lockfile is read; no vulnerability is checked |
| Leaked keys | exact known-value scan of transcripts, agent stores, logs and this checkout (`tools/scan_leaks.py`) | projects' working trees and Git history are not scanned for keys the estate does not hold |
| Keys | vault with provenance for issued keys; shares sent and received under `node/shares/` | a key put by hand has no source; "received from a person or organization" is not recorded for vault slots |
| Rotation | OpenRouter, Cloudflare and Heroku revoke or roll the old value before any remote consumer is switched (`openrouter.py:1600-1668`, `cloudflare.py:776`, `heroku.py:836`); DigitalOcean's `db rotate` reads consumers first | a remote consumer (a platform's config var, an app's env, a repository secret) loses its key; one production bot answered 401 for three hours on 2026-11-13 |
| Integrations | collectors per provider; `degraded` per receipt; doors' `ping` only by hand | no daily key validity; no API drift check; no proposal of integrations found in project code; no issue filing |
| Maintenance | disk guard, cleanup auto tier, updates, snapshots every 900 s (`maintenance.py:1232`) | no daily audit with fixes; auto cleanup visible but not changeable on the page |
| Assistant | bounded, read-only (`agent/assistant.py`); chat in the native macOS app only | the dashboard has no chat; no other agent answers there |
| Agent parity | 7 dashboard actions and about 30 state-changing CLI verbs | none of the 7 dashboard actions and most CLI verbs have an MCP tool (as-is.md section 4) |

## Requirements

Each row is verified by the named check; stage 10 of every module accounts for its rows.

| REQ | Requirement | Verified by |
|---|---|---|
| CP-01 | Home answers "is everything fine, and what needs me first" in one screen at 1440x900: each fact once, grouped (state, problems, work, estate), no command text in a row; a section card appears only where its tile does not already say it | page test: no fact rendered twice; no `<code>` holding a command on Home; screenshot at 1440 and 390 |
| CP-02 | Agents opens with sessions: every coding session running on this machine, grouped by agent and project, each with whether the lifecycle manager runs it, its account group, last turn and state; problems (stalled work, lapsed handoffs) come next, one row per workflow with its worst state and a button, never a command | page test over a fixture of managed, unmanaged and quiet sessions; one row per workflow |
| CP-03 | One session count: running processes per agent, of them managed by the lifecycle manager, of them with a turn in the last hour; the three are shown together with their sources and times, and Home's Agents tile shows the same numbers | `test_sessions_view` joins a process fixture, a Switchboard answer and ledger rows; Home and Agents read one function |
| CP-04 | Sessions run through a lifecycle owner: the account manager (Switchboard) or an application's own embedded agent. A coding session outside both is a finding `session.unmanaged` (info) naming how to start it managed; when the manager is not installed, the finding says what it adds and the setup command | finding fixture; text checked |
| CP-05 | Skills, plugins and coding agents installed here are a page section: per agent, its version, its skills (scope, version, source tier), plugins (enabled, version, newest known), hooks, with the harness findings beside them | page test over the harness fixture |
| CP-06 | A dependency inventory per project: every lockfile and manifest the engine can read (npm, pnpm, yarn, PyPI pins and locks, uv, poetry, Pipfile, Cargo, Go, RubyGems, Composer), the resolved name, version, ecosystem, direct or transitive, and local (path, workspace, link) or remote; a project's stack follows from it | `test_dependency_inventory` per format; a local dependency is marked and never sent out |
| CP-07 | A vulnerability watch over that inventory, daily: OSV for the public packages (the batch API, records cached by `modified`), an exploited-in-the-wild boost from the public known-exploited catalogue, osv-scanner as an optional faster path; findings `dep.vulnerable` (severity from the advisory, raised one level when exploited) with the fixed version; an advisory published after a project was scanned is matched on the next daily pass without a rescan | fixtures of an OSV answer and the catalogue; no network in tests; private packages never queried |
| CP-08 | A key sweep over projects: working trees and Git history of the estate's repositories, on a resumable daily budget, by the built-in known-value scan plus an optional gitleaks-compatible scanner run with redaction; the value, the match and the line never reach a record, a log or a finding; a sighting closes when the value is gone and the slot rotated | `test_key_sweep`: a planted key is found, the record holds its fingerprint and place only, and the stored files grep clean of the value |
| CP-09 | Supply-chain watch over MCP servers, skills and plugins: H5 to H11 extended with an MCP server's command line (an unpinned package runner, a fetch-and-execute), its tools' full definitions pinned by digest when probed (a later change is `mcp.tool_changed`), and third-party remote scanners off unless the person turns them on (they send content out) | `test_harness` and `test_mcp` fixtures |
| CP-10 | Keys by where they came from: issued here (a door), received (a person, an organization, an agent's share), shared out (a share, a credential request), put by hand (source unknown); each with its context (personal or an organization) and its consumers; the Access page and `observatory_credentials` show the class | `test_key_classes`; a slot put without a source reads `unknown`, never `issued` |
| CP-11 | Rotation never cuts a live consumer: before a door revokes or rolls a key, it finds every known consumer by fingerprint (platform config vars, app environments, repository secrets, local services), delivers the new value through that consumer's own door, reads it back by fingerprint, and only then revokes; a consumer it cannot reach holds the revocation and is named | `test_rotation_consumers` per door with fake providers; a failed delivery leaves the old key alive |
| CP-12 | Integration health: daily, each door's validity check per key (at most once a day per key), a schema check of the provider answers the collectors rely on (a missing or renamed field is `integration.drift`), and token scope against what the collectors need | fixtures of a 401, a 403 and a renamed field |
| CP-13 | Untracked integrations proposed: a dependency or an env name that maps to a provider the estate has no door or collector for (a payments SDK, an error tracker, a cloud SDK) is `integration.untracked` with the project and the evidence | mapping table test |
| CP-14 | Issues for findings: the engine opens an issue in the project's own repository for a finding of a class the person allowed, when the code host access can write there; on by default for dependency and integration findings, off for key findings in a public repository (never a value in an issue, ever); deduplicated by a fingerprint marker and a local map; closed or commented when the finding closes; the person is told once, in a banner, that it is on and how to turn it off | `test_issue_filer` with a fake host: dedupe, serial writes, no value in a body |
| CP-15 | A daily doctor: the maintenance job runs a fixed catalogue of checks once a day; each check names its fix, and a fix marked safe (reversible, journalled, inside the estate's own files and doors) runs automatically, the rest becomes a finding with its action; a model writes the day's summary only when something changed, inside the engine's own model budget | `test_doctor`: a safe fix runs and journals its undo; an unsafe one only reports |
| CP-16 | Disk and cleanup as their own section: free space, the guard's last pass, what auto cleanup removes and keeps, and its settings (on or off, thresholds, classes) changeable from the page and over MCP through one configuration door | page test; `observatory_configure` test |
| CP-17 | Monitoring grouped at the top of the navigation: Health, Security (vulnerabilities, keys, supply chain), Availability (sites, apps, integrations) | nav test |
| CP-18 | Agent parity from day 0: every action the dashboard offers and every state-changing CLI verb has an MCP tool, or is listed as a person's gate with its reason (a typed secret, a reveal to a person); a test fails when a new action has neither | `test_agent_parity` over `keyserver.ACTIONS`, the CLI verb table and the MCP tool list |
| CP-19 | The built-in agent in the dashboard: a chat panel on the live page with the assistant (read-only, fenced evidence, spend inside its budget) and the use-case agents (the doctor, the security watch), and their notifications in the same panel | page test; assistant suite |
| CP-20 | Adaptive, scripted configuration: every setting and integration is a declared, schema-checked entry; one configuration door (CLI and MCP) previews, applies and undoes a change with a journal line, so the built-in agent or a coding agent on any machine can adapt the engine on request or on autopilot within the person's switches | `test_configure`: plan, apply, undo; an unknown key refused |

## Modules

Cut by capability (task-pipeline decomposition). Build order is the wave order below. Every module follows one path,
which is also the walking skeleton: **collector or door, then a registry file, then findings, then a page section, then
an MCP tool**, so a module that lands is readable by a person and an agent in the same release.

| # | Row | Module | Delivers | Owns | Depends on | Contracts exposed | UI | REQs |
|---|---|---|---|---|---|---|---|---|
| M0 | OBS-549 | spec | this record, its research and the backlog rows | `docs/design/CONTROL-PLANE.md` | none | the module map | no | all (planning) |
| M1 | OBS-550 | sessions | one session view from processes, the lifecycle manager and the ledger; `session.unmanaged` | `store/raw/sessions.json`, `sessions_view.py` | machine survey | `observatory_sessions` | yes | CP-03, CP-04 |
| M2 | OBS-551 | home, agents and navigation | Home without repeats, Agents with sessions first, monitoring grouped at the top | `dashboard/home_page.py`, `agents_page.py`, `shell.py` nav | M1, M3, M4 | none new | yes | CP-01, CP-02, CP-17 |
| M3 | OBS-552 | installed tooling page | skills, plugins, hooks, coding agents per agent | the page section only (data is H16, H17) | harness scan | `observatory_machine(section="skills")` (exists) | yes | CP-05 |
| M4 | OBS-553 | dependency inventory and vulnerability watch | lockfile readers, OSV and the known-exploited catalogue, `dep.vulnerable` | `store/raw/dependencies.json`, `registry/dependencies.json`, `store/state/osv-cache/` | projects registry | `observatory_dependencies` | yes | CP-06, CP-07 |
| M5 | OBS-554 | key sweep | working trees and history, optional external scanner with redaction | `store/raw/key-sweep.json` (fingerprints only) | leak register | findings `secret.in_repository` | yes | CP-08 |
| M6 | OBS-555 | supply-chain watch | MCP command-line rules, tool definition pins, opt-in remote scanners | `store/state/mcp-pins.json` | harness, MCP scan | findings `mcp.tool_changed`, `mcp.risky_command` | yes | CP-09 |
| M7 | OBS-556 | keys by source | key classes, `vault.py put --from`, Access page tabs | vault slot `meta.json` field `source` | vault, shares, credential requests | `observatory_credentials` gains `source` | yes | CP-10 |
| M8 | OBS-557 | integration health and issues | daily validity, drift, untracked integrations, the issue filer and its banner | `store/state/issues.json` (fingerprint to issue) | doors, M4 | `observatory_issues` | yes | CP-12, CP-13, CP-14 |
| M9 | OBS-558 | daily doctor | check and fix catalogue in maintenance, summary on delta | `store/state/doctor.json`, `logs/doctor.jsonl` | M11 door, findings | `observatory_doctor` | yes | CP-15 |
| M10 | OBS-559 | agent parity | the parity test with its exemption list first, then the missing MCP tools it names, wave by wave | `tests/test_agent_parity.py`, `docs/design/control-plane/parity.json` | none | the parity list; one tool per action | no | CP-18 |
| M11 | OBS-560 | built-in agent panel | chat panel on the live page, use-case agents, notifications | none (reads the assistant) | assistant, M9 | none new | yes | CP-19 |
| M12 | OBS-561 | rotation consumer safety | consumers found by fingerprint, delivered and read back before revoke, per door | movements journal lines `consumers` | remote-env collector, doors | door CLI output `consumers` | no | CP-11 |
| M13 | OBS-562 | disk and cleanup section | own page with settings | the page only | M14 | `observatory_configure` for the guard keys | yes | CP-16 |
| M14 | OBS-563 | configuration door | schema per setting, plan, apply, undo, journal | `config/*.json` writes through one module | none | `observatory_configure` | no | CP-20 |

### Cut rationale

Sessions (M1) and the pages that show them (M2) are separate because the session view is also read by Home, the MCP
tool and the doctor; the page is one consumer. The security watch is three modules (M4, M5, M6) because each owns its
own data and source (packages, key fingerprints, MCP and skill pins) and can ship alone. The configuration door (M14)
is its own module because three others (M8's switches, M9's fixes, M13's settings) write configuration and must not
each grow a writer. Rotation (M12) is first in priority although small, because it is the only module whose absence
broke a production consumer.

### Cross-module contracts

| Contract | Owner | Consumers | Shape | When the other side is missing |
|---|---|---|---|---|
| `sessions_view.summary()` | M1 | M2, M9, `observatory_sessions` | `{processes:{agent:n}, managed:n, active_hour:n, sessions:[{agent, project, pid, managed, launch_id?, account_group?, last_turn_at?, state}], sources:{processes, manager, ledger}, degraded:[]}` | manager not installed: `managed` null and `degraded` names it; never 0 |
| `registry/dependencies.json` | M4 | M2, M8, M9 | per project: `ecosystems`, `packages:[{name, version, ecosystem, direct, local}]`, `read_from`, `degraded` | a lockfile that cannot be parsed is named in `degraded`, never read as no dependencies |
| `dep.vulnerable` finding | M4 | M2, M8 (issues), M9 | finding row with `advisory`, `fixed`, `exploited` | OSV unreachable: the last cached answer with its date, and `degraded` |
| key `source` class | M7 | Access page, `observatory_credentials`, M12 | `issued`, `received`, `shared`, `unknown` plus `context` | old slots read `unknown` |
| issue fingerprint map | M8 | M9 | `fingerprint -> {repo, number, state}` | no write access: the finding carries `issue: not-filed (reason)` |
| configuration door | M14 | M8, M9, M13, M11 | `configure.plan(key, value) -> plan`; `apply(plan_digest)`; `undo(journal_id)` | unknown key refused by type |
| parity list | M10 | CI, every module | `{action, where, tool or gate, reason}` | a new action without either fails the test |

### Deferred

Person-to-person sharing screens and the personal cloud stay with OBS-262 to OBS-266. The agents' activity per repository
(branches, files, permissions) stays OBS-268. Organization contexts per workspace stay OBS-359.

## Decisions

**D1. The session count has three facets, never one invented number.** Processes say what runs; the lifecycle manager
says what it manages and on which account group; the ledger says what did work. A session the manager does not know is
still shown, as unmanaged. The manager is read through its CLI's JSON (`switchboard sessions --machine --json` or its
MCP equivalent), with a timeout; an answer that lists fewer sessions than the process scan is reported to its owner,
not hidden. Measured on 2026-11-14 at 17:31 UTC on one machine: the process table held 34 `claude` processes; the
manager's CLI (`switchboard sessions --machine --json`) listed 39 Claude Code sessions, 0 managed and 17 quiet; the
manager's MCP tool (`switchboard_sessions machine:true`) answered 0 in the same minute; the Agents page read 2. So the
CLI is the reader, and the MCP answer is reported to the manager's owner.

**D2. Vulnerabilities from public data, private names never sent.** Package names and versions of public registries go
to OSV's batch API; a dependency that is local (a path, a workspace member, a link, a private registry scope the person
names) never leaves the machine. OSV records are cached by `modified`; the known-exploited catalogue is fetched once
a day. osv-scanner is used when installed, the built-in readers otherwise. The watch is on by default
(`integrations.vulnerabilities`) and can be switched off.

**D3. A key sweep stores fingerprints, never values.** A hit keeps the rule, the file, the line number, the commit and a
keyed digest of the value under the workspace's salt; the matched text is dropped before anything is written. An
external scanner runs only with redaction, its report goes to an owner-only temporary file and is deleted after its
fields are copied. Provider verification of a found key is never done by the sweep (it would send the value).

**D4. Remote scanners of MCP servers and skills are opt-in.** The built-in rules run offline; a scanner that sends tool
descriptions or skill text to a vendor runs only when the person turns it on, and the switch says what leaves.

**D5. Rotation is deliver, read back, then revoke.** A door that cannot find or reach a consumer keeps the old key alive
and says which consumer holds it; `--force` revokes anyway and journals the consumers it cut.

**D6. Issues are on by default for dependency and integration findings, off for key findings.** The person sees a
banner once (Home, and the native app's first launch) naming what is filed, where and how to turn it off; the
acknowledgement is stored. Issues carry the finding, the evidence and the fix, never a value, a path outside the
repository or another project's name. Writes are serial, at least one second apart, and stop on the host's rate limit.

**D7. The doctor is deterministic; the model only summarizes.** Each check and fix is code with a test; a fix is
automatic only when it is reversible and touches only the engine's own files and doors; the summary uses the engine's
model key on a delta only, inside the spend ledger.

**D8. Parity is a test, not a promise.** The list in `control-plane/parity.json` (M10) is the single place a person-only gate
is declared, with its reason; everything else gets a tool, wave by wave (M10).

**D9. Sessions are started managed.** The rule for the whole toolkit is proposed to its owner through the board: a coding
session is started through the account manager, or by an application's own embedded agent, so its lifecycle (start,
limits, hand-over, end, logs) is observed; Observatory reports the rest (CP-04).

## Priorities and waves

Ordered by harm first (what breaks something real), then by what the person asked to see, then by reach.

| Wave | Release | Modules | Why first |
|---|---|---|---|
| 1 | 0.37.0 | M0 (spec), M10 part 1 (the parity test and list), M12 (rotation), M1 (sessions), M4 (dependencies and vulnerabilities) | a rotation cut a production consumer; the session count is wrong on the page today; dependencies are not watched at all |
| 2 | 0.38.0 | M2 (home, agents, navigation), M3 (installed tooling), M14 (configuration door), M13 (disk section) | the pages the person reads first, on top of wave 1's data |
| 3 | 0.39.0 | M5 (key sweep), M6 (supply chain), M8 (integration health and issues) | widen the security watch once its pages exist |
| 4 | 0.40.0 | M7 (keys by source), M9 (daily doctor), M11 (agent panel), M10 part 2 (the remaining tools) | the agent layer on top of everything above |

Each module runs stages 3 to 10 of the pipeline on its own branch and pull request; a wave is released when its modules
are merged and its checks are green.

## Contract matters

`observatory_sessions`, `observatory_dependencies`, `observatory_issues`, `observatory_doctor` and
`observatory_configure` are new MCP tools; none changes an existing tool's shape (R28: add beside, never change). If a
Fabric capability is wanted for sessions or dependencies, it is proposed to the contract through the board first.

## What is never done

- A key value, a matched line or a secret's prefix in a record, a log, a finding, an issue or a model prompt.
- A private package name sent to a public service.
- A revocation that cuts a consumer the door could have found, without `--force`.
- A fix the doctor cannot undo, run automatically.
- A setting written by any path other than the configuration door once M14 lands.
- A command shown to a person where a button or a tool can do it.
