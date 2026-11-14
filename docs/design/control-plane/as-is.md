# What the engine does today (control plane research, part 1)

Measured on `main` at `78cdabe2` (2026-11-14). Paths are under `observatory/engine/` unless they start with `docs/`.
Read-only research for [CONTROL-PLANE.md](../CONTROL-PLANE.md); every line names where it was read.

## 1. Pages

- Fourteen pages, four navigation groups (`dashboard/shell.py:29-44`, groups `:77-82`): Work (index, projects, agents,
  findings), Infrastructure (heroku, digitalocean, cloudflare, domains, traffic), Access (creds, env, mcp), System
  (health, machine). One template builds them all (`dashboard/build_dashboard.py:1306`, body `:1853-1985`).
- Home: notices, six tiles (sites, apps, disk and memory, keys, agents, personal cloud), "Needs you first" (top three
  finding types), projects by organization (`dashboard/home_page.py:474-481`), activity, then a grid of section cards
  (`shell.py:331-373`).
- Repeated on Home: disk (tile `home_page.py:208`, Machine card `shell.py:327`); apps and providers (`home_page.py:137`
  and `shell.py:343-346` through the same `_infra_line`); agents (tile `home_page.py:238-240`, card `shell.py:105-112`);
  keys and sites (tile, card `shell.py:347`); problems (`home_page.py:380`, Findings card `shell.py:339`).
- Agents: commands printed to people through `_cmd` (`dashboard/agents_page.py:83-84`), per "Needs you" row (`:116`) and
  per workflow (`:197`); built in `agents_view.py:195-210` (`workflow show`, `workflow handoff ... --to-provider PROVIDER`,
  `full review`, a vault `put`). The empty state names an MCP tool to a person (`agents_page.py:268`).
- Machine (`dashboard/machine_page.py`): four tiles (`:110-142`), memory by origin, largest processes, memory by project
  (`:225-245`), where the disk goes (`:247-255`), disk guard (`:153-202`), Git and cleanup plan (`:257-313`). Auto
  cleanup is shown read-only (`:124`); hints print commands (`:179`, `:237`, `:290`). The page never acts (`:3-5`).
- Health: core, local server, records and review, privacy and access, updates and backups, organizations, agents and
  spend (`build_dashboard.py:2342-2679`), then agents' records (`:4579-4607`).

## 2. Sessions and workflows

- "Sessions working": sessions whose last ledger row of `kind='session'` is under an hour old (`agents_view.py:36`,
  `:164-173`, `:183`); the sublabel is a fixed string (`agents_page.py:88`).
- The rows come only from the Stop hook through `tools/record_turn.py` (`OWNER="agent:claude-code"`, `:38`; writes
  `:1302`, `:1354`), for turns that changed files in a watched repository (`store/workflow.py:101-104`).
- The account manager is never read: it is named only in comments (`folder_attribution.py:6`, `tools/openrouter.py:693`,
  `tools/use_secret.py:209`) and as a lifecycle readiness kind (`tools/lifecycle_cli.py:57`); `launch_id` appears only
  in tests.
- Stalled: open, leased, and silent for over `STALL_SECONDS` 1800 since the newer of the last checkpoint and session row
  (`store/workflow.py:705-731`, `:105`). Lapsed handoff: an offer past `expires_at`, or the last handoff lease ended
  `lapsed` (`:612-625`). Both are independent `if`s, so one workflow can print two rows (`agents_view.py:192-200`).

## 3. Installed tooling

- `collectors/scan_harness.py` writes `store/raw/harness.json`: skills (user, plugin, project, Codex), enabled plugins
  (`:237`), hooks (`:385`), coding agents from the contract catalogue (`:343-383`), and `inventory()` per space (`:998`).
- Read by `machine_view.skills_section` (`machine_view.py:103-210`), the MCP tool `observatory_machine(section="skills")`
  (`mcp/server.py:737-755`), `tools/harness.py`, findings `harness.*` (`dashboard/finding_types.py:142-151`) and the node
  summary. No dashboard page renders skills, plugins or coding agents.
- MCP: `collectors/scan_mcp.py` reads declarations (Claude Code, Cursor, opencode, Codex, Gemini, Kiro) with
  `--declarations-only` in the tick; `machine.mcp.refresh` probes on request; `mcp.html` renders `mcp-servers.json`
  (`build_dashboard.py:929-930`, renderer `:4191`). Findings `mcp.unreachable`, `mcp.key_in_url`, `mcp.needs_auth`,
  `mcp.own_unregistered` (`tools/build_findings.py:2997-3032`). Tool descriptions are not read or pinned.

## 4. Agent parity

The live key server offers seven actions (`tools/keyserver.py:509-511`, `do_POST` `:784`): mint an OpenRouter key, set
its ceiling, disable or enable, rotate, revoke, record a leak, sign or annotate; plus a reveal of an env value to a
person (`:403`). None has an MCP tool. Of the state-changing CLI verbs (`observatory.py:476-660`) only `workflow
handoff` (`observatory_handoff_create`, `_accept`), `org select` (`observatory_org_selection`) and `scan-mcp`
(`machine.mcp.refresh`) have one; `workflow close` partly (`observatory_checkpoint_write close`). Without a tool:
`configure`, `track`, `agent install`, machine and workspace backup, restore and move, `auto-update`, `maintain`,
`update`, `cleanup --apply`, `disk-guard --apply`, the binding verbs, `lifecycle` admin, `forget`, `ssh-keys` writes,
`vault-digests`, `share`, `cloud deploy`, `relay`, `org enroll`, `review` decisions, `harness --accept`, the vault and
door writes.

## 5. Keys

- Leak scan (`tools/scan_leaks.py`, tick step `leaks`, `tools/tick.py:667`; 30 % of the tick ceiling,
  `tick_budget.py:82`): exact known values (`:342`) in agent transcripts and stores, engine logs, the dashboard page and
  this checkout's tracked tree (`:674-733`). Never the value or line (`:26-31`); an HMAC of the value set
  (`sqlite_scan.py:38-49`). Not scanned: Git history, other projects' trees, MCP configs. `tools/check_secrets.py` is a
  shape check of this tree with an opt-in history depth.
- Vault provenance (`tools/vault.py:618`, `record_provenance` `:648`) is written only on an issued credential request;
  a plain `put` takes no source (`:1620`). Shares sent and received live under `node/shares/` (`node/shares.py`,
  `tools/share_cli.py`); received values stay sealed and are opened per use. `org_view.keys` (`:792`, `_classify`
  `:890`) calls a slot without a door record `ambiguous`.

## 6. Rotation

- OpenRouter: `_rotate_fresh` mints, delivers to one destination (`tools/openrouter.py:520`), then deletes the
  predecessor at once (`:1600-1668`).
- Cloudflare rolls in place (`tools/cloudflare.py:776`); Heroku regenerates first (`tools/heroku.py:836`, `:873`).
- DigitalOcean `db rotate` reads named consumers before resetting and redeploys them (`tools/digitalocean.py:34-60`).
- `vault.py rotate` is local and prints launchd consumers (`:729-767`).
- Consumer maps that exist but are not consulted: Heroku config vars as salted fingerprints, daily
  (`collectors/scan_remote_env.py`, `tools/remote_findings.py:91`), and `env.json` fingerprints (`env.shared_secret`).

## 7. Dependencies, integrations, maintenance

- Stack from marker files (`collectors/scan_filesystem.py:134-142`); direct dependency counts (`plugins/dependency_weight.py`,
  `PARSERS` `:112-120`), no lockfile, no network. No vulnerability check anywhere.
- Collectors per provider report `degraded`; findings `collector.degraded`, `collector.degraded_persistent`,
  `collector.not_applicable` (`tools/build_findings.py:3802`, `:1678`, `:3834`). Doors' `ping` (`openrouter.py:2040`,
  `cloudflare.py:2242`, `heroku.py:773`) only by hand. No drift or scope check; no issue filing.
- Tick every 30 minutes (`tools/install_launchd.py:5`); maintenance every 900 s (`maintenance.py:199`, `run_pass`
  `:1232`): disk guard (with `features.auto_cleanup`), hung commands, updates, snapshots, bundles, ports, jobs, digests,
  organization tick, shares, analytics. No daily audit with fixes; `learning_review.py` reviews only.
- Assistant: read-only, fenced (`agent/assistant.py`), served as `observatory_assistant_ask`; chat in the native app
  (`macos/Sources/ObservatoryApp/Assistant.swift`), none in the dashboard. Notifications through the system notification
  centre once per finding and severity (`tools/notify_findings.py:12-16`).
