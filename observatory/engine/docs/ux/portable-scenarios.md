# Full-system installation and compatibility scenarios

These supplement the original product scenarios. Approved scope: operator requested full migration, per-user private configuration, future backwards compatibility and autonomous implementation. Product outcomes remain unobserved until a real installation walkthrough.

Each OSS id is defined once, as a table row or a `### OSS-n:` heading; a heading that expands a row reads
`### OSS-n, in detail:`. A new scenario takes the next free id and moves this line
(the repository's `ScenarioIds` test checks both, and that every id below it is defined or vacant).

**Next free OSS id:** `OSS-114`

**Vacant OSS ids:** `OSS-59` (claimed by an open pull request on 2026-10-18 when the SSH-key scenarios took OSS-62; never written)

| ID | User goal | Observable result | Recovery and invariant |
|---|---|---|---|
| OSS-01 | Install on a new machine | `full init` creates a private versioned home; `full doctor` needs no credentials or network. Run from a person's terminal (or with `OBSERVATORY_SYSTEM_SETUP=1`), init on an empty home first restores the newest backup a workspace at this path left (OSS-39), then schedules the maintenance job (every 15 minutes) and keeps a backup passphrase in the OS credential store (OSS-38, OSS-42); run anywhere else it schedules nothing and answers `maintenance.result: not-scheduled-here` with the command that does it (`workspace.py` `_init`) | Unsupported format fails before writes; re-running init preserves settings. `tests/test_workspace.py` `test_init_preserves_settings_and_unknown_optional_fields`, `test_future_config_and_workspace_refused_without_mutation`, `test_doctor_lists_every_known_name_with_its_state`; `tests/test_maintenance.py` Schedule `test_init_here_says_how_scheduling_will_happen_and_touches_nothing`, `test_system_setup_needs_a_person_or_the_flag_and_never_a_temporary_home`. Doctor's `network_calls: 0`: no automated test |
| OSS-02 | Add my project directory | Explicit absolute source path is recorded; filesystem scan reads it | No implicit home-directory traversal; missing source is reported. `tests/test_filesystem_scan.py` `test_an_unconfigured_projects_source_is_a_typed_refusal`, `test_a_refused_scan_leaves_the_previous_file_intact`; `tests/test_workspace.py` `test_doctor_names_a_configured_source_that_was_deleted_with_its_fix` |
| OSS-03 | Connect an external service | Agent explains permissions; local secret input; status shows no value | Integration remains disabled until chosen; credential errors remain local. The key tools read stdin or a protected file and refuse a terminal; there is no hidden prompt. `tests/test_workspace_scheduler.py` `test_feature_gates_default_off`, `test_network_collectors_are_individually_opt_in`; `tests/test_install_key.py` `test_a_key_in_an_argument_is_impossible`. The agent's explanation of permissions: no automated test |
| OSS-04 | Bring my existing installation | Preview reports categories/counts; apply creates a separate copy, source remains usable | Changed source, symlink or unreadable data aborts; original is not overwritten. `tests/test_workspace_boundaries.py` `test_migration_refuses_wal_change_after_database_backup`, `test_migration_detects_late_identity_change_even_same_mtime`, `test_migration_failure_preserves_source_and_existing_destination`, `test_copy_refuses_dangling_symlinks` |
| OSS-05 | Upgrade | Supported legacy schema upgrades atomically with a WAL-aware backup; the live store is migrated in place, so sessions writing during the upgrade neither refuse it nor lose their rows, and the session hooks' receipts in `store/raw` may change meanwhile (`tests/test_workspace_upgrade.py` `test_a_write_while_the_upgrade_is_staged_is_kept_not_refused`, `test_the_session_hooks_receipts_may_change_while_staged`) | Unknown newer format is refused before mutation; failed migration rolls back. `tests/test_schema_compatibility.py` `test_legacy_upgrade_preserves_event_and_backup`, `test_wal_content_in_backup`, `test_partial_failure_rolls_back_ddl_and_dml`, `test_future_version_refused_without_write`; `tests/test_workspace_upgrade.py` `test_commit_failure_rolls_back_previous_files`, `test_future_workspace_refused_before_writes` |
| OSS-06 | Return to the previous release | Restore a complete snapshot into a separate home and use its matching code | Never run an old writer against a newer schema; no silent database downgrade. Using the matching code is the operator's step; the engine does not enforce it. `tests/test_workspace_upgrade.py` `test_snapshot_restore_preserves_private_files_and_modes`, `test_restore_refuses_existing_data`, `test_future_snapshot_floor_refused` |
| OSS-07 | Keep old agent clients working | Existing MCP tool names and supported schemas stay available | Breaking tool changes require a new version; unknown required capability is refused. The 44 published names are pinned exactly (`GOLDEN_TOOLS` in `tests/test_mcp_wire.py`): a rename, removal or addition fails the wire suite `must_understand`: `tests/test_workspace_profile.py` `test_unknown_major_format_and_requirements_are_refused` for a profile; for `config/settings.json`, no automated test |
| OSS-08 | Schedule observations | Explicitly enabled job carries the chosen workspace and no secret values | One writer per workspace; existing installation jobs are not replaced silently. The maintenance job of OSS-42 is separate and is scheduled by `init` from a terminal. `tests/test_workspace_scheduler.py` `test_plist_is_instance_scoped_and_contains_no_credentials`, `test_background_install_is_opt_in`, `test_workspace_lock_blocks_parallel_tick`. On a slow machine a tick reserves time for fresh findings and dashboard, without guaranteeing completion: the collectors stop before the tick's ceiling by what the steps from `merge` on have measured (the 90th percentile of their last 20 runs plus a quarter, at least 300 s or a fifth of a shorter ceiling, at most 40 % of it), every step from `merge` on leaves the steps that must finish after it their measured time and never less than a minimum, and a merge, an emit or a validator out of time leaves the findings and the dashboard to be built over the registry as it stands, the run recorded as `partial` (OBS-220; `tests/test_tick_budget.py` `TailBudget`, `TailWatchdog`; `tests/test_tick_py.py` `OutOfTimeGoesOn`; `tests/test_lifecycle.py` `test_a_tick_that_did_not_refresh_the_registry_is_recorded_as_partial`). The dashboard keeps a floor of 60 s that no step before it may spend, not even one that must finish and takes its own measured time, so a slow `findings` is stopped or waits rather than leave the page a few seconds (OBS-504; `tests/test_tick_budget.py` `test_no_step_before_the_dashboard_spends_its_floor`, `test_the_ticks_of_2026_11_11_now_build_the_dashboard`, `test_findings_leaves_the_dashboard_its_floor`). The build records the registry it read beside the pages (`built-from.json`); when a dashboard stopped at its limit leaves pages older than the registry, the tick's log and report say so (`dashboard_current: false`, `dashboard_note`), and a page served by the local server asks `/dashboard-freshness` and shows that it was built from an older registry (`tests/test_tick_py.py` `PagesOlderThanTheRegistry`; `tests/test_dashboard_render.py` `test_the_pages_say_which_registry_they_were_built_from`; `tests/test_serverd.py`). And the folder scan, told its own deadline, writes what it measured and carries each unreached folder's last row marked `carried_from` instead of being stopped with nothing written. `tests/test_lifecycle.py` `test_a_collector_leaves_the_tail_its_reserve`, `test_with_only_the_reserve_left_a_collector_waits_and_the_tail_runs`, `test_every_step_after_the_merge_is_a_tail_step`; `tests/test_filesystem_scan.py` `test_a_scan_out_of_time_carries_the_previous_rows_forward`, `test_out_of_time_with_no_earlier_scan_leaves_a_new_folder_out`. Every job runs on the engine's own interpreter whoever installs it, and a job file written with another one is repaired by the maintenance pass (a running tick waits for the next). `tests/test_lifecycle.py` `test_the_tick_runs_on_the_engines_venv_whoever_installs_it`, `test_an_installed_plist_on_another_interpreter_is_repaired_in_place`, `test_the_maintenance_pass_reloads_a_repaired_job_but_never_a_running_tick` |
| OSS-09 | Open the credential UI locally | Correct loopback Host/Origin accepted; authenticated operation logged without secret values | Lookalike origins, DNS rebinding and malformed requests are refused before effects. `tests/test_keyserver_boundary.py` `test_origin_prefix_and_different_port_refused_before_effects`, `test_rebinding_host_cannot_obtain_page_token`, `test_bad_json_and_body_bounds_do_not_reach_action`, `test_the_audit_names_the_caller` |
| OSS-10 | Copy a command from the local dashboard | Absolute program paths and Python select the displayed workspace; project and secret roots can contain spaces and quotes | Arguments remain literal, including shell metacharacters; static ENV actions locate a named slot without printing its value; importing a value requires an explicitly chosen private input file. `tests/test_dashboard_portability.py` (see below) |
| OSS-11 | See what a project runs where | The projects table groups each project's apps by the environment they serve. Production comes first, and the account is shown on hover. An app no override or provider placed in an environment is shown under "environment not stated" | Never inferred from an app's name. `config/environments.json` or a provider's own statement (a Heroku pipeline stage) places an app. `tests/test_hosting_groups.py` places apps through both and asserts the group order, the account on hover and a production-like name left unplaced; `tests/test_environments.py` covers the collector's rules |
| OSS-12 | Act on a key from the live dashboard | A refusal the server explains says "failed" ("не вышло") with its reason, and the action can be retried. A timeout, a dropped connection, an unreadable answer or a server fault says "outcome unknown · check" ("исход неизвестен · проверьте") and locks the button | A repeat after a lost answer could mint or revoke twice, so the button stays locked until a rescan shows what happened. Covered by `tests/test_action_outcomes.py`, which presses the page's own button for each outcome and asserts its label and lock in both languages |

## Dashboard workspace scenarios (2026-09-26)

| ID | User goal | Observable result | Recovery and invariant |
|---|---|---|---|
| OSS-13 | Enter an inventory and compare records | Projects/ENV show records immediately; every list exposes selection and global sorting | Primary identities never hidden by default; empty source differs from no matches. Search covers each page's own field list, not every field. `tests/test_workspace_redesign.py` `test_projects_and_env_show_records_without_expanding_groups`; `tests/test_dashboard_render.py` `test_an_empty_registry_page_gives_its_command_and_no_filter_bar`, `test_keyboard_focus_survives_sort_and_reset` |
| OSS-14 | Understand a project and return | Compact row shows purpose/activity/resources; detail gives full linked repositories, sites, deployments and keys | Close preserves list and returns focus; deep links retained. `tests/test_dashboard_render.py` `test_the_detail_panel_names_what_a_project_is_made_of`, `test_the_panel_says_why_history_was_not_recorded` (opened by its link); `tests/test_workspace_redesign.py` `test_project_panel_folds_repeats_translates_enums_and_names_a_missing_project`; `tests/test_pages.py` `test_the_panel_is_a_dialog_that_keeps_the_keyboard`. Focus returning on close: no automated test |
| OSS-15 | Triage and revisit findings | Overview counts all open severities; list shows matching rows and acknowledged history even with zero open findings | Preview names its boundary and links to full list; no invisible critical overflow. `tests/test_dashboard_shell.py` `test_warning_only_preview_is_not_empty_and_preserves_counts`, `test_overflow_is_bounded_visible_ranked_and_does_not_mutate_source`, `test_acknowledged_only_history_survives_and_sensitive_payloads_stay_scoped`; `tests/test_workspace_redesign.py` `test_acknowledged_only_findings_retain_reason_and_undo` |
| OSS-16 | Know what an action does | Capability stated before Keys/ENV rows; copy/manual/live verbs clearly differ | Copy never claims execution; uncertain outcomes and reveal protections remain. `tests/test_workspace_redesign.py` `test_copy_only_credentials_and_refresh_say_command`; `tests/test_pages.py` `test_the_keys_page_offers_the_doors_own_verbs`; `tests/test_action_outcomes.py` |
| OSS-17 | Compare infrastructure/traffic | Cost and measurement context above lists; zero distinct from missing/error; one GA4 resource counted once across credentials | Contradictory chips cannot remain selected; evidence remains accessible. `tests/test_google_identity.py` `test_same_id_counts_once_and_retains_credential_provenance`, `test_zero_is_measured_but_an_error_is_not`; `tests/test_workspace_redesign.py` `test_no_users_filter_means_measured_zero_not_unknown`, `test_unknown_unclaimed_total_is_not_reported_as_zero`, `test_contradictory_traffic_status_chips_replace_each_other`, `test_traffic_is_comparable_across_account_boundaries` |
| OSS-18 | Navigate and inspect observer | Fourteen destinations (`PAGES` in `dashboard/shell.py`: the ten of the redesign, then Machine, OSS-21, Agents, OSS-24, and DigitalOcean and Cloudflare beside Heroku under Infrastructure, OBS-235 and OBS-271), visible main, page search, narrow-screen sort; Health begins with observer/recovery | Snapshot claims qualified by time; keyboard retained; actions never replay on navigation. `tests/test_workspace.py` `test_complete_local_workflow_and_fourteen_pages`; `tests/test_infra_pages.py` `Pages`; `tests/test_dashboard_shell.py` `test_all_pages_have_one_main_containing_their_primary_content`; `tests/test_pages.py` `test_the_pages_exist_and_the_index_is_small`, `test_the_navigation_comes_before_the_alerts`; `tests/test_dashboard_render.py` `test_keyboard_focus_survives_sort_and_reset` |
| OSS-19 | Read the dashboard in one's own language | The page opens in Russian when the reader's first system language is Russian (`ru`, `ru-*`) and in English otherwise, whatever language the pages were built in (L10N-01). The rail's switch — System / English / Русский — overrides it for one reader, on every page at once, including pages opened as files, and survives a rebuild and an update; the pressed button names the choice. Precedence: the reader's choice, then the system's first language, then the build locale (`OBSERVATORY_LOCALE`, else `interface.locale`, else English), which is all a reader without a browser — the CLI, the session hook, the checks — gets. Counts use the language's plural forms; dates and numbers its format (`02.01.2026`, `1 234,5`); terms follow the organisation's glossary (a workflow is «задача», a backup «резервная копия») | An unsupported `interface.locale` is refused and nothing is written. An unknown stored choice reads as System. A browser that keeps no storage says so and follows the system language. A refusal or degraded reason the engine writes in English reads in Russian where the catalog holds it and as written otherwise, never as a key (L10N-04). Finding titles are translated; a finding's details and suggested action stay in English. `tests/test_i18n.py` `ReaderLanguage` (`ru-RU`, `ru`, `en-US`, an empty list, an unknown stored value), `Glossary`, `Formats`, `EngineText`, `test_a_page_built_in_one_language_reads_in_the_other`, `test_default_configure_and_refusals`, `test_lang_and_switch`; `tests/test_agents_page.py` and `tests/test_machine.py` render each page in one language and re-translate it into the other; `tests/test_dashboard_render.py` `test_a_finding_title_reads_in_the_readers_language`. The no-storage notice: no automated test |
| OSS-20 | Recognise the product | Every page carries the Observatory glyph on the PassionCode tile, "by PassionCode.ai", the light theme by default with a Light / Dark switch beside the language (OBS-523), gold filling action and selection, the readable gold-brown for focus and links, and a link to the PassionCode toolkit | State colour is always paired with words; the design tokens are the pinned PassionCode bytes (`dashboard/brand/manifest.json`). `tests/test_i18n.py` `test_brand`, `test_manifest_pins_the_vendored_bytes`, `test_dark_control_edge_meets_three_to_one`, `test_pages_open_light_and_switch_to_dark_on_the_readers_choice`, `test_no_line_or_text_takes_the_gold_fill_colour`; `tests/test_pages.py` "the page opens light and offers dark". Colour paired with words: no general automated check |
| OSS-21 | See what runs and where memory and disk go | `features.machine_watch` on: the Machine page shows processes grouped by origin (agent session, launchd job, simulator by device name, app, detached), memory and swap, free disk, the largest cache/VM/history places with how each comes back, idle worktrees and branches holding the only copy. By hand, `full machine` refreshes and prints the processes, memory, volume and largest places; the worktrees and branches come from `full cleanup`, which writes the plan (`workspace.py` `_machine`). `full machine --explain PID` says why one process runs (witr detail when installed) | Read-only page; a process is shown by executable and script, never its environment or full command line; an unmeasured source is listed under Not measured with the command that measures it, never shown as zero. `tests/test_machine.py` Origins `test_explain_strips_environment_and_command_line`, `test_display_name_and_script_never_keep_secrets`; Unmeasured `test_nothing_measured_is_never_shown_as_all_clean`; Page `test_renders_every_section_in_both_languages_and_empty_states` |
| OSS-22 | Get space back without losing work | `features.auto_cleanup` on: the maintenance job (`full maintain`, every 15 minutes, launchd/systemd/Task Scheduler) runs the disk guard FIRST in its pass — hourly while space is normal, every pass below the `low` line, before the update and the backup (`maintenance.step_guard`), whatever the tick does; `full disk-guard [--apply] [--level L]` runs it at once. The guard reads free space and picks a pressure — `normal`, `low` below 25 GB, `critical` below 10 GB (`config/cleanup.json` → `guard.levels`) — that moves every threshold: the cache tools' own cleanup (`uv`, `pnpm`, Homebrew, Docker only while its daemon already runs; `npm cache verify`, `uv cache clean`, Go and Yarn caches from `low`), build and dependency output git ignores in checkouts nobody worked in for 14 / 3 / 1 days (last checkout or commit, or an uncommitted file — never the index, which every `git status` rewrites), temporary entries untouched 3 / 1 / 1 days, application and tool cache folders untouched 7 / 2 / 1 days except downloaded browsers and SDKs (`cache_keep`) and live state an agent service marks with `.observatory-keep` or declares in its Fabric descriptor (`paths.data`, `paths.config`, `paths.logs`; listed as kept with the reason), abandoned browser code-sign clones, Trash items older than 30 days (3 at `critical`); then the cleanup's git auto tier (merged or pushed branches, clean idle worktrees, stale records) with its idle thresholds lowered by the same pressure (3 days at `low`, 1 at `critical`). The Machine page shows the last run, the largest files under the home directory (daily, reported, never removed) and the simulators holding memory that swap writes to the disk, with the command that stops each; `full cleanup --apply --include manual` still archives unique branches and dirty worktrees first | **Observed holders and unknown holder coverage refuse removal**; the observation is not an atomic exclusion of external writers: an explicit not-attempted unknown snapshot for preliminary admission, then a target-scoped POSIX `lsof` and command-path probe within five seconds before each path removal (unknown or missing `lsof` skips it; Windows path actions refuse unknown holder coverage); without complete holder evidence no path is removed and `degraded` says so. No symlink is followed, no privacy-guarded place opened, no other user's file touched, no discovered process stopped and no app started; a cache command is skipped while its own tool runs. Freed space is the free-space delta the disk reports (`freed_mb`), because `du` counts an APFS clone in full. Planning stops at `plan_budget_seconds` and says so. Every action is a line in `store/logs/cleanup.jsonl`; `machine.guard_idle` warns when the guard has not reported for three hours. `tests/test_disk_guard.py` (Holding, Temp, Clones, CacheDirs, Builds, Trash, Order, Acting, Run, RunBudget, Cadence, AnyDevice, GitTierPressure, GitTierSafety, SecondIteration, HoldersModule, LargeFiles, LiveStateUnderCacheRoots — each guard watched failing against a planted defect); `tests/test_maintenance.py` Guard; `tests/test_machine.py` Estate `test_auto_removes_only_what_loses_nothing`, `test_recheck_skips_what_changed_since_the_survey`, `test_manual_archives_before_removing` |
| OSS-23 | Let an agent's workflow continue in another session, account or model | The agent writes a checkpoint after every step; when its account runs out, another session accepts a handoff pack (the latest checkpoint with its constraints first, a fresh git read, related records) and continues from the open step. Health shows the open workflows and the handoffs waiting for a session | One executor at a time by lease token; the old session's late write is refused and kept as a proposed episode, never lost; an unaccepted offer lapses and leaves the previous executor in place; nothing credential-shaped is stored ([AGENT-MEMORY.md](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/AGENT-MEMORY.md)). That the agent checkpoints after every step is the agent's behaviour; nothing enforces it. `tests/test_workflow_memory.py` `test_write_without_a_token_is_refused_and_kept`, `test_an_expired_offer_cannot_be_accepted_and_the_old_lease_stands`, `test_a_value_is_never_a_credential_address` |
| OSS-24 | See which agents are working now and on what | The Agents page (Work group) opens on five counters — sessions with a turn in the last hour, open workflows, handoffs waiting, stalled, steps kept after a lost lease — then the open workflows by project with goal, current step, executor and last checkpoint age, then the agent sessions of the last day with their project and workflow | A session counts as working only by a recorded turn, with its time; a workflow's executor is its lease holder, never a guess; no lease token or key value appears anywhere on the page. `tests/test_agents_page.py` `test_counters_and_what_needs_a_person` (four of the five counters; handoffs waiting is not asserted), `test_no_token_and_no_value_reaches_the_page` |
| OSS-25 | Follow one workflow across executors | A workflow card opens to a lane per executor that held it, a dot per checkpoint, and the handoff with its reason between them (dashed while offered, struck through when it lapsed or was replaced); constraints, next actions, checkouts and keys by name beside it | The same events are an ordered list for a screen reader; colour always comes with a word; a card needing attention opens by itself. `tests/test_agents_page.py` `test_the_lanes_follow_the_workflow_across_accounts`, `test_the_page_renders_both_languages_and_escapes_what_agents_wrote`, `test_a_card_that_needs_attention_opens_by_itself`; the dashed and struck styles: no automated test |
| OSS-26 | Notice a stalled workflow or a handoff nobody took | `Needs you` lists stalled workflows (no checkpoint and no session turn for 30 minutes), lapsed offers, kept steps and keys a workflow needs that are missing, only in a `.env`, or could not be checked (`unknown`, which blocks the workflow as `missing` does), each with its reason and the command to copy | The page changes nothing: `full workflow show/handoff/close`, `full review` and `vault.py put` are run by a person, `close` and a forced handoff from a terminal. `tests/test_agents_page.py` `test_counters_and_what_needs_a_person`, `test_a_key_that_could_not_be_checked_waits_for_a_person_too`; `tests/test_workflow_memory.py` `test_a_silent_held_workflow_is_stalled_until_a_session_moves`, `test_close_and_force_need_a_terminal` |
| OSS-27 | Keep watching while agents work | Opened through the local server the page refreshes every 15 seconds from the store, keeping which cards are open and where focus is, and says when it last refreshed | A failed refresh keeps what is shown and says live updates are paused; opened as a file it says it is the snapshot of its build. The `/agents` route: `tests/test_serverd.py` `test_every_route_answers_and_none_serves_a_value`; the 15-second refresh, the kept state and the paused and snapshot texts: no automated test |
| OSS-28 | Know that a workflow's keys come from the vault | A workflow's keys show as chips: in the vault, only in a `.env`, missing or unknown; a missing, `.env`-only or unknown key is in `Needs you`, with its `vault.py put` command where one applies | Keys are shown by name only; an unreadable vault is `unknown`, never `missing`; `credentialsMissing` blocks continuation for every declared key not verified in the vault, including `unknown`. Ownership can transfer to repair access; a fresh read of verified slots clears the gate. `tests/test_workflow_memory.py` `test_an_unreadable_vault_is_unknown_not_missing`, `test_unknown_credentials_block_handoff_and_acceptance`, `test_acceptance_reads_the_vault_again` |
| OSS-29 | Decide whether a project's memory may be embedded by a remote model | Nothing leaves by default. `full embedding-policy grant project:<slug> --statement "texts of <slug> are sent to OpenAI"` at a terminal consents for one project and the configured model, and `show` lists what is in force; the indexer then sends only that project's current public and project-internal notes, one project per request, and `revoke` stops it | `confidential`, checkpoints, handoffs, global and unprojected text never leave; an agent's search query never leaves; a broken or rolled-back policy keeps everything local and says why; a refusal happens before the budget, the key and the request. `tests/test_embedding_policy.py`; `tests/test_embedding_enforcement.py` `test_only_eligible_records_leave_one_project_per_request`, `test_a_revoked_consent_sends_nothing`, `test_a_broken_policy_sends_nothing_and_says_so`, `test_a_callers_query_is_never_embedded`, `test_no_verdict_is_refused_before_budget_and_key`, `test_grant_and_revoke_refuse_without_a_terminal`, `test_confidential_cannot_be_granted` |
| OSS-30 | Let another agent reach a project's memory, and take it back | The local stdio agent needs nothing. For any other caller the operator runs `full access-binding issue agent:<name> --project project:<slug> --token-file <new file>` at a terminal; the bearer goes only into that owner-only file, `show` (and `full doctor`, the Health page) lists who may reach memory by name, and `revoke` refuses the next call | A binding reaches only the memory tools, only its projects, scopes, class ceiling and effect, as its own principal; a workflow of another project is refused, and so is one that does not exist; Fabric's `X-Fabric-Projects` narrows and never widens; a broken or rolled-back registry refuses every binding and still serves the local agent; every decision about a binding is in `store/logs/access.jsonl` without a bearer ([ACCESS-BINDING.md](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/ACCESS-BINDING.md#enforcement-n-008)). `tests/test_access_binding.py`; `tests/test_memory_access.py` `test_another_project_is_refused_before_any_write`, `test_fabric_projects_narrow_and_never_widen`, `test_a_restored_older_registry_is_refused_whole`, `test_a_broken_registry_still_serves_stdio_and_refuses_http`, `test_issue_needs_a_terminal`, `test_doctor_names_the_bindings_read_only` |
| OSS-31 | Find one's own record in a busy store | `observatory_search` in a project returns that project's current records even when hundreds of stronger matches belong to other projects or to classes the caller may not read | Only the latest revision, not erased, inside its `valid_from`/`valid_to`, in the caller's project and classes takes a place in the 300-match window; rows the caller may not see move no count (`total`, `belowFloor`, the abstain reason, the lag); a full window is said in `degraded` (`tests/test_search_scope.py`) |
| OSS-32 | Ask afterwards why memory answered what it did | Every `observatory_search` answer names `receipt.receiptId`; `observatory_explain` with it re-reads the exact revisions returned, with how each matched and whether it is still current, superseded, expired, erased or no longer visible | Explain never searches again; only the caller that received the receipt can read it, and a foreign or missing id get one answer; the receipt log holds no query text (an HMAC) and no statement; search, recall, explain and workflow answers and refusal details carry no known secret value or credential shape, and say what was replaced in `redacted` ([RETRIEVAL-AUDIT.md](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/RETRIEVAL-AUDIT.md); `tests/test_retrieval_audit.py`) |
| OSS-33 | Find the decision inside a long checkpoint | `observatory_search` for what a step decided returns the checkpoint with `chunks` naming the field it matched (`decisions[3].why`, `constraints[1]`), so the successor reads that decision rather than the whole body | Workflow ids are not search keys; a late outbox replay of an older revision or of a forgotten record writes nothing; a failed lexical write rolls the checkpoint back; the lag shown is the vector arm's (`tests/test_checkpoint_chunks.py`) |
| OSS-34 | Let an agent on this machine use memory over HTTP | The operator issues a binding (`full access-binding issue`), starts `full memory-http`, and gives the client the bearer file; the client calls the same memory tools as over stdio at `http://127.0.0.1:47313/mcp` | Without a valid binding every request is 401 before its body is read; a foreign Host 421, a foreign Origin 403, over 4 MiB 413, over the rate 429 with `Retry-After`; a binding reaches only its memory scope, and a restart loses nothing (`tests/test_memory_http.py`) |
| OSS-35 | Call memory by the contract's names | A client written against `memory/0.1` (Fabric, Switchboard, task-pipeline) calls `memory.checkpoint.write`, `memory.search` and the other seven, over stdio or HTTP, and gets the same answers as the `observatory_*` tools | An input field the contract does not define is refused by path, never by value; the three reserved names are not served; a binding's refusal is a typed answer the contract describes; the vendored schema and the declaration are checked against their pinned commit and digests (`tests/test_memory_wire.py`). Over HTTP, `tests/test_memory_http.py` `ContractNamesOverHttp` compares each `memory.*` answer with its `observatory_*` tool's in one session |
| OSS-36 | Make a recorded text unrecoverable | `full forget MEMORY_ID --plan` lists the packs and cached answers that quote the record; `full forget MEMORY_ID --why "…"` at a terminal withdraws it and erases its text from the ledger, packs, cached answers, indexes, the file's free pages and the export, and prints a receipt per backend | The record's id, owner and times stay as the audit trail; an open workflow's checkpoint is refused; backups and the export's git history are named `retained`, so the receipt is never `complete` while they exist; erasing again re-checks; the journal holds no text (`tests/test_forget_receipt.py`) |
| OSS-37 | Keep facts current and learn from a fixed failure | A fact is written with the date it holds until; recall and search serve current facts, and recall's `validity: expired` or `all` shows history (search has no `validity`). After a fix, `observatory_learn` proposes the lesson citing the failure and the fix | A fact without `valid_to` is refused; a lesson whose sources are missing, foreign or unreadable is refused alike; the lesson stays `proposed` until the operator or an independent witness promotes it, and its author cannot (`tests/test_facts_learning.py`) |
| OSS-38 | Stay on the current release without doing anything | Every five minutes (OSS-73) a newer stable release is looked for (the first check 90 seconds after the job starts at login) and the maintenance job (every 15 minutes, or at once when the check found one) installs it with `full update --apply --unattended`, but only at a safe point: the update restarts the tick and the local server on the new code, so while a tick is running, the server has answered a page or an agent in the last five minutes, or a memory-http client has called, the release waits as `deferred` and the next pass tries again; a running Claude Code session's MCP server is never stopped. On macOS the app follows once it is not running. `full auto-update status`, `doctor` and the Health page («Устанавливать обновления автоматически» / "Install updates automatically") say whether it is on, when it last checked and what it found; every stage leaves a code in the shared update log (`~/Library/Logs/Project Observatory/updates.jsonl`, `$XDG_STATE_HOME/project-observatory/logs/` on Linux) | The switch is the file `auto-update` in the workspace home: absent is on, only the word `off` turns it off, and `full auto-update on|off` writes it; an update, a reinstall or an uninstall never writes it; an older install's `updates.auto: false` still reads as off and moves into the file at the next `auto-update` command. Off stops the checks, the downloads and the installs, the app's included; the daily backup keeps running. Only a signed release is installed (OSS-41); a release that declares a step needing a person is verified and held, shown with its step in `doctor`, Health and `auto-update status`, and not downloaded again until a person acts; a failed update rolls back and the same release is tried again six hours later; a check that could not look is retried five minutes later; an update refused for a moment (a busy lock, another update, a copy changed during the snapshot) is retried at the next pass; the daily snapshot and an update never run at once; one that needs a person stops the attempts and says so; an install before 0.17.0 is reached by the `observatory-log` plugin's daily bridge, which reads the same switch (`tests/test_maintenance.py` Switch `test_automatic_updates_are_on_until_a_person_turns_them_off`, `test_only_the_word_off_turns_it_off`, `test_an_older_installs_setting_is_still_off_and_moves_into_the_file`, `test_an_update_a_reinstall_or_an_uninstall_never_writes_the_switch`; Pass `test_the_switch_set_to_off_stops_checks_downloads_and_installs_the_apps_too`, `test_a_check_every_five_minutes`, `test_a_failed_check_is_retried_after_five_minutes_too`, `test_the_schedulers_first_check_waits_90_seconds_after_the_start`, `test_a_live_client_of_the_server_defers_the_activation_to_the_next_pass`, `test_a_silent_server_and_a_probe_are_no_clients`, `test_a_memory_http_client_defers_and_a_refused_or_old_call_does_not`, `test_a_running_tick_defers_the_update_and_is_never_stopped`, `test_a_held_release_is_shown_and_not_attempted_again_until_a_person_acts`, `test_a_failed_update_is_retried_at_the_next_check_not_the_next_hour`, `test_an_update_refused_for_a_moment_is_retried_the_next_hour`, `test_an_update_that_needs_a_person_stops_the_attempts`, `test_the_daily_snapshot_waits_while_an_update_runs`; UpdateLog; App; Bridge; `tests/test_serverd.py` `test_every_route_answers_and_none_serves_a_value`; `tests/test_dashboard_render.py` the switch row). The Health rows for a held or deferred release: no automated test |
| OSS-39 | Lose nothing when the workspace or the program is deleted | Every day an encrypted full snapshot goes to a backups root outside the workspace, opened by a passphrase kept in the OS credential store. After a reinstall, `full init` in a terminal (or `full restore --latest`) brings back the newest backup of a workspace at the same path | `init --fresh` starts empty; without a kept passphrase `init` names the backup and the restore command; another workspace's backups are never chosen; a damaged newest backup falls back to an older one (`tests/test_maintenance.py` Reinstall, Passphrase) |

OSS-10 regression evidence: `tests/test_dashboard_portability.py` executes generated
commands against inert synthetic tools from an unrelated working directory. Nine
tests cover workspace selection, quoted provider names and movement evidence,
configured secret/project paths, static ENV lookup, missing-project refusal, and
platform-neutral private-file input. This verifies command behavior, not visual
layout or a live credential operation. It needs `node`: without it every case is
skipped, and `tests/run_portable.py` receipts the suite `SKIP` (named in `not_run`),
never `PASS`.

For the observer status card, the portable command starts the server explicitly in
the foreground or checks its heartbeat. It does not address a historical launchd
label or silently install a background job. The onboarding flow separately owns
opt-in scheduler installation. Empty-state observation uses `local`; Google refresh
uses its collector's supported `--force` option and rebuilds the registry before
the dashboard. `tests/test_pages.py` checks the command bridges and actual tool
subcommands; `tests/test_dashboard_render.py` executes the renderer.

### OSS-40 — a suppression cannot hide a replacement value (PB-032)

Numbered OSS-13 until 2026-10-06; the dashboard row above keeps that id.

Given an accepted sighting with a reason and expiry, the operator copies its secret name, exact location and opaque `version_id` into the suppression config. The accepted sighting remains visible as suppressed. A new value under that name, another location, or another workspace salt remains an active sighting. Expiry replays old evidence without requiring a new write to the source. Malformed and legacy unversioned rules raise a warning and suppress nothing. Proof: `tests/test_leak_coverage.py` (rotation, exact path, duplicate names in files/SQLite, salt change, malformed shapes, expiry and incremental value discovery). Detection still covers known values only; it does not discover every unknown secret shape or search all deleted Git history.

### Cleanup preservation: ignored local data

Both worktree tiers skip an unreadable or hidden index (`assume-unchanged` or
`skip-worktree`) without modifying its flags, including flags appearing after
the initial status read. Detached HEAD removal requires a verified archive ref
for its exact commit. Branch cleanup verifies the bundle's advertised tip and
uses expected-tip deletion without following symbolic refs; symbolic targets and
nonliteral/invalid branch names refuse cleanup. Changed tips and unreadable/checked-out worktree
attachments retain the branch. `CleanupNeverLosesWork` covers these cases with
real Git repositories. An external checkout writer can still race the last
attachment observation; stop writers before explicit cleanup.

Manual cleanup revalidates its archive before force removal: HEAD, status,
working and staged patches, and every untracked file's listing, mode, size and
SHA-256 must still match. Nested repositories, symlinks, special files, read or
archive errors and exhausted budgets retain the worktree with a reason. A failed
attempt names its partial archive without overwriting previous attempts. Working
and staged versions are independently recoverable; a final observation/removal
race remains, so writers must be stopped before explicit manual cleanup.
`tests/test_git_hardening.py` `CleanupNeverLosesWork` tests drift, positive restore,
separate staged content, nested repositories, errors and limits. The legacy
build-artifact action skips and directs the operator to `full disk-guard` to
replan (`test_legacy_artifact_plan_cannot_delete_newly_tracked_output`).

When `full cleanup --apply --include manual` reaches a dirty worktree with ignored
files, it leaves the worktree in place and reports a skip with a recovery command.
The operator preserves or moves those files before retrying; an archive of tracked
and untracked files does not claim to contain ignored files. Automatic cleanup and
the disk guard exclude `.wrangler` from build output even in an older workspace's
artifact list, preserving its local database and object state. Coverage:
`tests/test_disk_guard.py::GitTierSafety::test_manual_removal_preserves_ignored_local_data`
and `test_local_state_is_protected_even_by_an_older_artifact_configuration`.

## Operating the engine (2026-10-06)

These paths shipped before they had a scenario (audit A45 in
[docs/runs/2026-10-05-audit](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/runs/2026-10-05-audit/README.md)).
Each row says what the engine does on the commit that added it and names the tests that
prove it, relative to this directory (`observatory/engine/`); "no automated test" marks a
claim that only the code shows.

| ID | User goal | Observable result | Recovery and invariant |
|---|---|---|---|
| OSS-41 | Move to a published release by hand | `full update` previews and writes nothing; `full update --check` exits 0 when up to date, 10 when a newer release exists and 3 when it could not look (network, rate limit), and an unknown `--version` exits 2. `full update --apply [--version X.Y.Z]` downloads the wheel, stops the loaded tick and server, takes a `before-update` snapshot, installs, runs the new release's `upgrade --apply --writers-stopped`, checks the installed version, the dependency pins and `doctor`, restarts the jobs and prints one JSON report, including the agent sessions still running the old release. GitHub is read as the person (`GH_TOKEN`, `GITHUB_TOKEN`, then `gh auth token`), and a refused token falls back to anonymous reads (`engine_update.py`) | Nothing is stopped or installed unless the wheel matches GitHub's digest and `SHA256SUMS`, and `SHA256SUMS.asc` verifies against the pinned release key (`release_signature.py`): an unsigned release, another key or a malformed signature is refused with exit 2. `--check` and the preview compare versions only and verify no signature. A wheel that carries `observatory/engine/RELEASE.json` naming its own version and a `needs_person` step is held by `--unattended` (exit 6, nothing stopped or installed) and shown, then installed, by a person's `--apply`; a marker for another version is ignored and an unreadable one holds. `OBSERVATORY_RELEASE_REPOSITORY`/`OBSERVATORY_RELEASE_API` are honoured only from a checkout; an installed release ignores them. Each stage writes a code (`update_check`, `update_download`, `update_install`, `update_restart`) to the shared update log, never a value. A failure reinstalls the previous signed wheel and puts the snapshot back, keeping the changed workspace beside it as `<home>.failed-update-<stamp>` (exit 1); a rollback that cannot finish exits 4 with the steps for a person. One apply per workspace: a second one, or one started while the daily snapshot has the jobs stopped, is refused. A volume that would keep under 1 GB free after what the update writes there (wheels, the installed package beside the old one, the snapshot and its encrypted copy, the store's databases once more for the migration) refuses with exit 7 before anything is downloaded, stopped or installed, naming the bytes needed and free; the automatic job records `refused-disk` and tries again the next hour (`test_too_little_free_space_refuses_before_anything_changes`, `test_the_space_needed_is_what_the_update_writes`, `test_space_taken_by_the_machine_backup_is_checked_again_before_the_jobs_stop`, `test_the_preview_names_the_space_and_blocks_on_it`, `test_the_first_check_counts_the_machine_backup_it_is_about_to_take`, `test_volumes_that_share_their_free_space_are_one_volume`; `tests/test_maintenance.py` `test_an_update_refused_for_disk_space_is_named_and_retried_the_next_hour`). `tests/test_engine_update.py` `test_newer_release_is_reported_with_its_own_exit_code`, `test_network_down_is_degraded_never_up_to_date`, `test_unsigned_release_is_refused_before_anything`, `test_release_signed_by_another_key_is_refused_before_anything`, `test_a_malformed_signature_is_a_refusal_not_a_traceback`, `test_an_unsigned_rollback_release_of_a_signed_version_is_refused`, `test_github_digest_mismatch_refuses_before_anything`, `test_a_second_apply_is_refused_while_one_runs`, `test_install_failure_rolls_back_and_restores_services`, `test_failure_after_a_committed_upgrade_restores_the_workspace`, `test_failed_rollback_needs_a_person`, `test_a_refused_token_falls_back_to_anonymous_reads`, `test_a_held_release_is_verified_and_never_installed_unattended`, `test_a_person_may_still_install_a_held_release_after_seeing_the_step`, `test_a_marker_for_another_version_is_ignored_and_an_unreadable_one_holds`, `test_every_stage_writes_its_code_to_the_shared_log`, `test_an_unattended_update_never_stops_a_running_tick`, `test_the_feed_cannot_be_redirected_from_the_environment_of_a_release`, `test_a_tampered_package_and_a_failed_install_are_logged_by_code`, `test_downgrade_is_refused_and_check_says_newer_installed`; `tests/test_release_signature.py`; `tests/test_maintenance.py` `test_an_update_cannot_start_while_the_snapshot_has_the_jobs_down` |
| OSS-42 | Keep updates and daily backups running without me | `full maintain ensure` installs the job that runs every 15 minutes: a launchd agent (`org.project-observatory.<id>.maintain`) or a systemd user timer. Beside it, a second job checks for a release every five minutes (OSS-73). Each pass keeps the backup passphrase in the OS credential store, checks for a release when five minutes have passed since the last check (OSS-38) and applies it at a safe point, on macOS updates the app, and once a day takes an encrypted snapshot with the tick and server stopped. `full maintain status` shows whether the job is installed and loaded, the last pass, check, update and snapshot, and warnings; `full maintain run` runs one pass now; `full maintain uninstall` removes the job and records the choice (`maintenance.py`) | `ensure`, `run` and the plugin's hook need a person at a terminal or `OBSERVATORY_SYSTEM_SETUP=1`; otherwise they exit 2 and touch neither the scheduler nor the credential store, and `status` does not ask launchd. `full update --apply` keeps an explicit `OBSERVATORY_SYSTEM_SETUP=0`: it installs the release, schedules nothing and reports `maintenance: not-scheduled-here` (OBS-229, `tests/test_engine_update.py` `test_an_explicit_refusal_of_system_setup_schedules_nothing`). One pass at a time. The snapshot holds the update lock while the jobs are down, so a snapshot and an update never overlap. A person's `uninstall` is kept by later automatic `ensure` calls; `maintain ensure` or `auto-update on` turns it back on. `uninstall` leaves the backups and the passphrase, and refuses while a pass runs. `tests/test_maintenance.py` Pass `test_up_to_date_checks_on_its_cadence_and_takes_the_daily_backup`, `test_a_check_every_five_minutes`, `test_the_daily_snapshot_waits_while_an_update_runs`, `test_a_second_pass_finds_the_first_busy`; Schedule `test_the_launchd_job_runs_every_quarter_hour_and_is_never_a_managed_job`, `test_the_systemd_timer_runs_every_quarter_hour_persistent_and_quoted`, `test_ensure_schedules_and_respects_a_persons_uninstall`, `test_maintain_ensure_refuses_without_a_person_or_the_flag`, `test_maintain_run_and_hook_refuse_without_a_person_or_the_flag`, `test_status_does_not_probe_launchd_when_it_may_not`. The refusal of `uninstall` during a pass: no automated test |
| OSS-43 | Protect my backups and open one by hand | `full backup-passphrase set` reads the passphrase from stdin (a terminal is asked twice), needs at least 12 characters and writes `secrets/backup-passphrase` owner-only; `status` says whether one is configured; `show` prints it only to a terminal. With a passphrase, every snapshot and database copy is encrypted into a backups root outside the workspace (`OBSERVATORY_BACKUPS`, then `storage.backups`, then `~/Documents/Project Observatory/Backups` on macOS or `$XDG_DATA_HOME/project-observatory-backups`), three kept per kind (`backup_vault.py`). `full backups status` lists the root, its source, the newest backup of each kind and any plaintext copy left in the workspace; `full backups migrate` encrypts the newest verified legacy copies into the root; `full backups decrypt FILE OUTPUT` writes a new private directory or file | A changed passphrase never locks out older backups: the earlier value is appended to an append-only chain beside it in the OS credential store, and `restore --latest` tries each one (OSS-39); `decrypt` uses only the current passphrase or a terminal prompt. A credential store that does not answer is not an absent one: nothing is generated or written over, and the pass says the passphrase is missing. Without a passphrase, copies stay plaintext inside the workspace and both `backups status` and the Health "full backup" row say so. `decrypt` authenticates the whole file before writing and refuses an OUTPUT that exists; `migrate` keeps and names a copy it cannot verify. `tests/test_maintenance.py` Passphrase `test_a_changed_passphrase_keeps_the_old_one_as_previous`, `test_every_earlier_passphrase_is_kept`, `test_a_store_that_cannot_be_read_is_never_written_over`, `test_a_lost_file_and_a_silent_store_generate_nothing`, `test_a_keychain_that_does_not_answer_is_not_an_absent_item`; Reinstall `test_a_backup_taken_before_a_passphrase_change_still_restores`; `tests/test_backup_vault.py` `test_show_refuses_without_terminal`, `test_set_from_stdin`, `test_settings_then_documents_then_beside_the_workspace`, `test_moves_newest_legacy_and_removes_the_rest`, `test_decrypt_takes_the_bare_name_backups_status_prints` |
| OSS-44 | Give another machine the same engine behaviour | `full profile export FILE` writes a new owner-only file and prints what it included and excluded: the integrations, features and language from `config/settings.json`, and the policy files for activity tiers, cleanup, Google consoles, models and retention (`workspace_profile.py`). Without FILE the profile goes to stdout. `full profile import FILE` previews each change, what it ignored, and what every enabled integration still needs on this machine (for example `gh auth login`); `--apply` writes the changes under the workspace lock | Never carries sources, storage, secrets, the registry, the store, backups or the scheduler switch, and nothing shaped like a path, an e-mail address or a token. Import refuses, changing nothing, a profile from a newer engine (naming `full update --version X.Y.Z --apply`), an unknown format or `must_understand` entry, a changed checksum and a leaked value; unknown sections are listed as ignored, not refused. An existing FILE is never overwritten without `--force`. A failed apply restores every file. `tests/test_workspace_profile.py` `test_export_never_carries_machine_state`, `test_export_to_file_is_private_and_never_overwrites`, `test_preview_writes_nothing`, `test_newer_engine_profile_is_refused`, `test_unknown_major_format_and_requirements_are_refused`, `test_tampered_or_leaking_profile_is_refused`, `test_unknown_profile_sections_are_ignored_and_named`, `test_import_reports_what_this_machine_must_connect`, `test_a_failed_write_restores_every_file` |
| OSS-45 | Let Claude Code record its sessions into this workspace | `full agent install` adds this project's marketplace to Claude Code, installs the `observatory-log` plugin, turns its auto-update on (`--no-auto-update` keeps it off) and points the plugin's hooks at this workspace (`OBSERVATORY_ROOT`, `OBSERVATORY_HOME`, `OBSERVATORY_PYTHON` in Claude Code's `settings.json`, backed up once first). `full agent status` lists every problem (not installed, another version, another marketplace source, auto-update off, hooks pointed elsewhere) and exits 1 until there is none (`tools/agent_plugin.py`) | Install reports success only when Claude Code's own list of installed plugins shows the plugin; otherwise it exits 2 and writes no settings. Without `claude` it exits 2 with the manual `/plugin` steps and the exact environment to add. A plugin another marketplace already manages is left alone, and only the hook environment is written. `full agent uninstall` removes only the keys install added. `tests/test_agent_plugin.py` `test_install_turns_auto_update_on_and_points_hooks_at_this_workspace`, `test_opt_out_keeps_auto_update_off_and_status_says_so`, `test_install_does_not_claim_a_plugin_claude_did_not_install`, `test_without_claude_the_manual_route_names_the_hook_environment`, `test_install_leaves_a_launcher_managed_plugin_alone_but_writes_the_hook_env`, `test_uninstall_leaves_settings_exactly_as_before_install` |
| OSS-46 | See which MCP servers my agents declare, and whether they answer | With `integrations.mcp` on and `sources.mcp_config_root` set, `full scan-mcp` reads the MCP declarations of six agents (Claude Code, Cursor, opencode, Codex, Gemini, Kiro; `collectors/scan_mcp.py` `AGENT_FILES`) and asks `claude mcp list` whether each server answers. The MCP page and the `machine.mcp.inventory` tool list the servers by name, transport, declaring agent and answer; `machine.mcp.refresh` runs the scan as a job, and a second call joins the running one. The tick reads declarations only and starts no server | With the integration off the scan says "not configured" and reads nothing. No URL query, password, header value or environment value is kept: the page says only where a key sits (in the URL, a header or the environment). An absent, unreadable, stale or never-run scan is named in `degraded`, never shown as an empty list. `tests/test_mcp_inventory.py` `test_a_planted_secret_never_reaches_the_scan`, `test_absent_and_unreadable_sources_are_named_not_dropped`; `tests/test_machine_mcp_inventory.py`; `tests/test_interop.py` `test_a_second_start_joins_the_running_job`; `tests/test_dashboard_render.py` `test_the_mcp_page_says_how_to_probe_and_names_a_command_once`. A refresh with the integration off: no automated test |
| OSS-47 | Let an agent learn the estate in one call and read more on demand | The server's instructions tell an agent to start with `observatory_overview`, which answers counts, activity tiers, the most recently active projects, the worst findings, disk and memory, and the names of the tools that page. `observatory_status` (`limit`, `cursor`, `detail: summary`) and `observatory_findings` (`limit`, `cursor`, most severe first) return a page with `nextCursor` and totals over the whole scope; `observatory_project` (`timelineLimit`) and `observatory_timeline` (`limit`, `since`) return bounded lists without a cursor | Every read carries `degraded`: empty asserts full coverage, otherwise it names what was not read. An unknown id or cursor in `observatory_findings` is a typed `error`; a malformed `observatory_status` cursor restarts the walk and says so in `degraded`; an invalid `since` and a store that will not open are named in `degraded`, never returned as an empty answer; a credential-shaped argument is never echoed. `tests/test_mcp_wire.py` (instructions within 1,800 characters with the WRITE and SPENDS rules, overview under 20,000 characters, status summary rows and the cursor walk, project and timeline degradations); `tests/test_recall.py` `test_every_read_tool_carries_a_degraded_list`; `tests/test_wire_inputs.py`. A findings `nextCursor` walk: no automated test |
| OSS-48 | Let an agent use a project's key without seeing it | `observatory_credentials` lists a project's keys by name, from the vault and the project's `.env` files, each with the command that uses it. `tools/use_secret.py run --env ENV PROJECT NAME -- COMMAND` starts the command with the value in its environment and removes the value from its output; `--vault-only` (or `OBSERVATORY_VAULT_ONLY=1`) refuses the `.env` fallback and prints the `tools/vault.py put` command instead. A value enters the vault only on stdin: `tools/vault.py put`. `tools/vault.py bind --header-for URL` binds a slot to one https server, or to a loopback one over plain http (`http://127.0.0.1:PORT`, never `localhost`), and `tools/use_secret.py header` serves it to an agent's MCP configuration as a header — to Claude Code's `headersHelper`, or with `--server-url URL` to Codex's `http_headers_helper` (Codex: not verified against a real Codex; under its `bearer_token_env_var`/`env_http_headers` fallback with `use_secret.py serve`, four of the door's refusals do not apply — a terminal, an unbound slot, a server other than the binding, `url-conflict`). `tools/install_key.py` installs the model key from stdin after asking the provider what kind of key it is, and prints its length and spending limit | A flag after the names is refused with the order to use; a value typed at a terminal, an empty stdin or a multi-line value is refused by `vault.py put`; `header` refuses a terminal, a server other than the bound one, a slot that holds nothing, and two server URLs (the variable and `--server-url`) that name different servers (`url-conflict`); `bind` refuses plain http to anything but a loopback literal with its port, `localhost` included. Journals record names, sources and places, never a value (`use_secret.py` writes `secret-use.jsonl`; `vault.py` writes `movements.jsonl`); `install_key.py` writes no journal row. `tests/test_mcp_wire.py` (no value crosses the wire); `tests/test_use_secret.py` `test_vault_only_refuses_a_project_env_file`, `test_a_flag_after_the_names_and_a_missing_program_are_typed`, `test_a_value_split_across_a_buffer_is_still_removed`; `tests/test_vault.py` `test_a_value_travels_only_on_stdin`, `test_every_movement_lands_in_the_journal_and_hand_moves_are_recordable`; `tests/test_secret_header.py` `test_a_terminal_is_refused`, `test_a_server_other_than_the_binding_is_refused_and_named`, `test_every_call_is_audited_and_the_audit_never_holds_the_value`, `test_a_loopback_literal_with_a_port_may_be_bound_over_http`, `test_http_to_anything_but_a_loopback_literal_with_a_port_is_refused`, `test_a_loopback_binding_is_served_to_its_server_and_only_its_server`, `test_server_url_serves_a_client_that_does_not_export_one`, `test_two_urls_that_disagree_are_refused`, `test_server_url_does_not_lift_the_terminal_refusal`; `tests/test_install_key.py` `test_a_key_the_provider_rejects_never_reaches_a_file`. The terminal refusal of `vault.py put`: no automated test |
| OSS-49 | Keep memory search honest when the embedding model changes | The store registers its vector index as namespace 1 (`vec_notes`, state `legacy`, migration 0010), and `full doctor` lists each namespace and its state. A consent names the model it was given for (OSS-29), so after the configured embedding model changes the old consent no longer applies (`provider-mismatch`): no text is sent, the old vectors are not served, and search answers by words alone and says so in `degraded`. The Health row "memory embeddings" says which projects' texts may go to which model, "nothing leaves this machine", or that the policy was refused, with `full embedding-policy show`. `full reindex` rebuilds `vec_notes` from the ledger | The legacy index is never activated again, and a namespace becomes the one that answers only with a receipt naming the model admission, the scope filter and the abstention calibration (`store/namespaces.py`). No command registers, fills or activates a new namespace yet; after a model change the operator grants consent for the new model and runs `full reindex`. "Models in quarantine" on Health is about the assistant's chat models (OSS-51), not these vectors. `tests/test_vector_namespaces.py` `test_the_migration_registers_the_legacy_index_as_quarantined`, `test_activation_needs_a_receipt_and_the_legacy_index_never_activates`, `test_doctor_names_each_namespace_and_its_state`; `tests/test_embedding_enforcement.py` `test_a_verdict_for_another_model_is_refused`, `test_doctor_reports_the_policy_state`; `tests/test_dead_data.py` `test_the_embedding_policy_reaches_the_operator`. Search falling back to words after a model change: no automated test |
| OSS-50 | Agents' records need no decision from me | `observatory_record` and `observatory_learn` write ledger records as `proposed` with confidence below 1; `observatory_propose` writes a registry edit as a proposal. `full digest` and `full review` list what waits and what retention will erase. The Health page shows "agents' records not yet confirmed" (always, even at zero) and "registry edits proposed" (when above zero), and a section "Agents' records" that says nothing there needs the person: an independent source confirms a record, or retention retires it. The newest twelve rows are folded under that sentence with no accept or reject button, and the overview card no longer counts them (operator, 2026-10-06). `tools/review.py promote`, `reject`, `reject-group`, `tombstone`, `accept-proposal` and `reject-proposal` decide | Every decision needs a terminal; there is no `--yes`, and `full review` takes no decision arguments. Only the operator moves a record out of `proposed` (except to `rejected`); an independent checker other than the author may corroborate. Unreviewed proposed ledger records are tombstoned, not deleted, after 90 days (`defaults/retention.json`); registry proposals do not expire. `tests/test_review_queue.py` `test_promotion_still_refuses_a_script`, `test_the_digest_names_what_is_about_to_be_erased`; `tests/test_write_surface.py` `test_the_terminal_gate_still_refuses_a_pipe`; `tests/test_facts_learning.py` `test_nobody_promotes_their_own_proposal`; `tests/test_corroboration.py` `test_the_ledger_still_refuses_a_self_witness`; `tests/test_retention.py` `test_an_old_proposed_row_is_tombstoned_not_deleted`; `tests/test_dead_data.py` `test_a_registry_proposal_reaches_the_operator`; `tests/test_dashboard_render.py` `test_the_health_panel_shows_what_the_store_holds`, `test_the_health_page_names_versions_kinds_and_dollars`; `tests/test_pages.py` (no promote or reject on a row); `tests/test_dashboard_shell.py` `test_summary_routes_have_no_inventory_badge_and_queue_stays_labelled` |
| OSS-51 | Read the observer's state at a glance | After the observer card, the Health page lists, in this order: "remote at risk" (checkouts whose work is only on this disk), "secret leaks" (open leaks, OSS-55), "agents' records not yet confirmed" and "registry edits proposed" (OSS-50), "memory embeddings" (OSS-49), "full backup" (none yet, or the newest date and whether it is encrypted outside the workspace or was taken before the last update, with `full backups status`), "conclusions not indexed" (count and oldest date), "sources degraded" (count from the last scan), "spent by this project this month" and "of which today" from this project's own wallet journal, in dollars for OpenRouter credits, and "models in quarantine" (assistant models taken out of the chain after failures, up to three named) (`dashboard/build_dashboard.py`) | A value that was not measured gives no row rather than a zero: "remote at risk" appears only while the local server answers, leaks, unindexed conclusions and degraded sources only above zero, spend only with a wallet file. The quarantine row lists every recorded model, including one already due for a new probe. `tests/test_dashboard_render.py` `test_the_spend_row_renders_at_all`, `test_the_health_page_names_versions_kinds_and_dollars`, `test_the_health_panel_shows_what_the_store_holds`; `tests/test_workspace_redesign.py` `test_health_spend_has_its_unit_and_the_queue_hands_over_the_rest`; `tests/test_provider_health.py` `test_the_page_renders_a_non_empty_quarantine`. The "remote at risk", "secret leaks", "sources degraded" and "conclusions not indexed" rows: no automated test renders them |
| OSS-52 | Give the assistant a model chain and a budget | `full configure model chain ID[,ID]` and `full configure budget daily_ceiling\|monthly_ceiling\|velocity_ceiling AMOUNT` write `config/models.json` atomically under the workspace lock, keep every other field and print `model_status`. With `features.agent` on, `full doctor` names the next missing step (`no-model`, `no-budget`, `no-key` or `ready`) with its command (`workspace.py` `_configure_models`) | A malformed model id, an unknown ceiling and an amount that is negative or not a number exit 2 and write nothing. A ceiling of 0 is accepted and counts as unset: asking with the agent off, without a key, without a chain or with any ceiling at 0 is refused (`agent-disabled`, `provider-unconfigured`, `model-unconfigured`, `budget-unset`) before any job starts. A reached ceiling stops before any provider request: the assistant's job fails with `budget-reached`, and the indexer and search fall back to words, saying so. `tests/test_workspace.py` `test_configure_sets_the_model_chain_and_the_budget`; `tests/test_assistant.py` `test_ask_refuses_an_unconfigured_model_before_any_spend`, `test_status_names_what_the_model_still_needs`; `tests/test_budget_subject.py` `test_this_systems_own_overspend_still_stops_it`, `test_velocity_is_still_local_and_still_fires`; `tests/test_projection.py` `test_the_indexer_degrades_to_lexical_when_the_ceiling_is_reached`. The `budget-reached` code: no automated test |
| OSS-53 | Open the dashboard, live or as files | `full open` builds the pages when they are missing (`--rebuild` always) and prints a `file://` address, opening a browser where it can. `full open --serve` serves them on `127.0.0.1:47311` (or `OBSERVATORY_SERVER_PORT`), or reuses this workspace's server; `full open --stop` stops it. A bare `project-observatory` runs `full open` (`tools/dashboard_open.py`; the repository's `tests/test_full_cli.py` `test_bare_opens_the_dashboard_of_an_initialized_workspace`) | A port that already serves another workspace, a server that exits early or does not answer in time is refused with exit 2 and the reason; there is no silent fallback to files. `--stop` signals only this workspace's own server, refuses an always-on launchd server with the command that removes it, and reports `stopped: false` when nothing answers. `--serve` with `--stop` is refused. `tests/test_agent_plugin.py` `test_open_refuses_without_a_workspace`, `test_open_builds_missing_pages_and_prints_a_file_url`, `test_serve_starts_a_loopback_server_then_reuses_it`; `tests/test_dashboard_stop.py` `test_stop_ends_this_workspaces_server_and_is_idempotent`, `test_another_workspaces_server_is_left_running`, `test_a_pid_that_is_not_this_server_is_never_signalled`, `test_an_always_on_server_is_refused_with_the_command_that_stops_it`, `test_serve_and_stop_are_exclusive`. The early-exit, timeout and port-held-by-another-program messages: no automated test |
| OSS-54 | Give a project a narrow provider key without seeing its value | `tools/cloudflare.py issue --preset PRESET --vault PROJECT/ENV/NAME` mints a scoped token with the stashed admin token, waits up to about 80 seconds for Cloudflare to honour it, writes it to the vault slot on stdin and prints the slot, its rights and the `use_secret.py run` command; issuing again rolls the existing token rather than adding one. `tools/cloudflare.py lifecycle --bucket B` sets an R2 bucket's lifecycle rules and reads them back. `tools/openrouter.py issue --name N --limit USD --to vault:PROJECT/ENV/NAME` creates a key with a monthly spending ceiling; `rotate` creates and delivers the successor before deleting the old key; `revoke` deletes. Every token and key either door mints expires 90 days out unless told otherwise (`--token-expire-days N` or `--no-token-expiry` for Cloudflare, `--expire-days N` or `--no-expiry` for OpenRouter), a roll or rotation renews that lifetime, and the expiry (or null and why) is recorded in the slot's metadata and the door's records, shown by `vault.py list`, `use_secret.py names`, `observatory_credentials` and each door's `list` (OBS-191). The live Keys page offers the OpenRouter actions through the local key server. `tools/sentry.py issue --preset releases --from PROJECT/ENV/NAME --vault PROJECT/ENV/NAME [--org SLUG] [--project SLUG]` reads the minting credential from its vault slot, mints a Sentry organization token with `org:ci` only, proves it with its own value (exactly that scope, the releases read on the organization's region host, members and issues refused) and writes it to the slot; `install --preset releases|read` verifies a token a person made and already put in the slot; `revoke --from` retires the broad token only after a narrow one verifies again, and sends it only to its own Sentry and counts it revoked only when that Sentry answers 401 after once accepting it; `list` and `status` name what exists and whether it still works (OBS-292) | A missing admin token, a writer preset without `--vault`, an unknown preset, a ceiling of zero or less and a duplicate name are refused before anything is minted. A failed Cloudflare issue deletes the token it created, or says a rolled one must be issued again; a failed OpenRouter delivery deletes the new key; OpenRouter has no proof step. No value appears in arguments, output or journals. Issue, rotate, revoke and lifecycle append to the vault's movements journal; an OpenRouter `limit` change does not. The Keys page never carries a vault value (OSS-09). `tests/test_credential_doors.py` `test_cf_d1_preset_is_scoped_to_one_account_and_lands_in_the_vault`, `test_cf_issue_rolls_rather_than_duplicating`, `test_cf_proof_waits_long_enough_for_a_slow_edge`, `test_cf_a_token_this_call_created_is_deleted_when_the_issue_fails`, `test_cf_r2_lifecycle_only_changes_rules_and_never_a_key`, `test_cf_r2_lifecycle_read_back_is_compared_rule_by_rule`, `test_or_issue_deletes_the_key_when_delivery_fails`, `test_or_rotation_creates_and_delivers_before_deleting`, `test_no_door_takes_or_prints_a_value_outside_stdin`. A lifetime outside 1 to 365 days is refused before anything is minted (`tests/test_key_expiry.py`). A personal Sentry token, which Sentry lets mint nothing, and the `read` preset, which Sentry mints only from a browser session, are refused before any call with the page where a person makes the token and the `install` that follows; a minted token that is broader than declared or fails its purpose read is deleted and never delivered; a region host that is neither the base nor one of its documented region hosts is refused unless the person names it, a redirect is never followed, a value in the broad slot that is not a Sentry token is never sent, a 401 from a Sentry that never accepted the token does not count as its revocation, and a failure after a mint (a vault put that times out, a ledger write) deletes the minted token and journals its id (`tests/test_sentry_door.py`). Journal rows other than lifecycle's, and the unknown-preset refusal: no automated test |
| OSS-55 | Record a leaked key and close it once it is revoked | `tools/vault.py leak PROJECT ENV NAME --where "…"` records an open leak, even for a key the vault never held. The board carries a critical `secret.leaked_unrotated` finding per slot with the `settle` command, and, while any is open, Health shows "secret leaks: n not rotated — vault.py leaks". After revoking the key at its provider, `tools/vault.py settle PROJECT ENV NAME --how … --revocation-evidence … --consumer-evidence …` closes every open sighting of that slot. A change made directly at a provider is recorded with `tools/vault.py moved`; `tools/vault.py movements` lists the journal. The leak scan raises `secret.seen_outside_its_home` with the `vault.py leak` command and does not write the register itself. Unreadable session/log directories, dashboard metadata and tracked Git sources produce coded coverage holes while readable sibling sightings remain. Expected file targets of other types (directory, submodule entry, FIFO) are explicitly unmeasured and never opened as ordinary files. `tests/test_leak_coverage.py` `test_unreadable_logs_keep_sibling_hit_and_degrade_health`, `test_git_failure_is_not_misreported_as_an_installed_package`, `test_source_stat_failure_is_not_silent_absence`, `test_installed_tree_without_git_marker_is_explicitly_unscanned`. Unreadable vault ancestors and unfollowed session directory links remain explicitly unmeasured (`test_vault_unlistable_subtree_keeps_readable_values_and_marks_hole`, `test_session_directory_symlink_is_unmeasured_without_following`); vault symlinks, special files and exhausted discovery limits likewise cannot imply full coverage (`test_vault_discovery_boundaries_are_explicit_without_reading`). A scan the step's deadline cut short keeps every sighting it made and adds the warning `secret.leak_scan_partial` (how many sources it read, how many stores it did not), which stays on the board and is not pushed; `secret.leak_scan_unmeasured` is raised only when the scan knew no values, and even then a sighting beside it is reported | A local `vault.py rotate` never closes a leak; settling without both pieces of evidence, or with no open leak, is refused and changes nothing. A credential-shaped value in `--where` is replaced with `[redacted]`. The register and the movements journal hold names, places, dates and evidence notes only, appended owner-only. Suppressing an accepted sighting is OSS-40. `tests/test_vault.py` `test_a_leak_needs_a_place_and_stays_open_after_local_rotation`, `test_a_leak_of_a_key_the_store_never_held_is_still_recorded`, `test_a_credential_shaped_name_or_note_never_reaches_the_register`, `test_a_leak_rotated_at_the_provider_is_settled_by_hand_with_evidence`, `test_manual_settlement_requires_two_independent_attestations`, `test_every_movement_lands_in_the_journal_and_hand_moves_are_recordable`; the board finding: `test_the_board_carries_an_open_leak_and_drops_a_settled_one`; the partial scan: `tests/test_leak_coverage.py` `test_a_partial_scan_with_a_credential_hit_is_critical_and_partial`, `test_every_code_combination_keeps_the_sighting`, `test_no_values_is_unmeasured_only`, `test_a_cut_scan_from_an_older_scanner_never_claims_it_read_everything`, `tests/test_leak_scan_incremental.py` `test_a_pass_the_deadline_cuts_short_writes_what_it_read_and_names_the_rest`, `tests/test_notification.py` `test_a_partial_leak_scan_is_on_the_board_but_not_pushed`. The Health row and the journal files' modes: no automated test |
| OSS-56 | Ask any agent's name for a project and get the one id | `config/identifiers.json` maps other systems' names (`<system>:<value>`) to a `project:` or `product:` id with the name's owner; the emit publishes them as `known_as` edges and `registry/identifiers.json`, and `project.resolve` (`observatory_resolve`) answers a registry id, an alias, a `repository:`, a deployment or a `domain:` id with the canonical id, the members with their repositories, deployments and domains, and every alias with its owner. An agent proposes an alias with `observatory_propose` (`patch: {"aliases": [{"id", "owner", "source"}]}`) and `review.py accept-proposal` lands it in the table (`identifiers.py`) | One name never has two targets: a duplicate or case-variant with two targets, or an alias contradicting a measured repository, deployment or domain, is a published conflict and is withheld; resolving it answers `conflict`. A measured name on two projects answers `ambiguous`; anything else `not-found`, never a near match. A malformed table stops the emit before anything is replaced; a target the registry lacks is withheld and named in `degraded`. `tests/test_identifiers.py` (all six cases), `tests/test_validator_rules.py` (identifiers plants), `tests/test_write_surface.py` `test_an_alias_proposal_is_checked_at_the_wire_and_lands_in_the_table_of_names` |
| OSS-57 | Rotate a DigitalOcean database password without printing it | `digitalocean.py db rotate` reads the selected user's current provider credential for literal proof, checkpoints an owner-only unknown receipt, resets once, delivers to the vault and moves named apps; confirmed-reset failures do not prevent independent consumers | `--spec-env APP:KEY` accepts only a plain matching password or database URL matching engine, user, password and provider host/port. Env names are not proof; encrypted literals and unverified pool endpoints are refused before reset. A `${…}` binding is never rewritten at any level, even beside a literal of the same key; a key that is a binding at every level is refused with `--app` named. `--db` must be one of the cluster's `db_names`, else refused before reset; a cluster the provider lists no databases for, and redis/valkey, are refused with their own reason, and only identifier-shaped names are printed. `db credential` checks `--db` the same way before it reads a password, and writes nothing on refusal. `db consumers` lists a key whose `_`-separated name has a database word beside `URL`/`URI`/`DSN` anywhere in it (`APP_DATABASE_DSN`, `PRIMARY_DB_URI`), never `SENTRY_DSN`, `OTEL_…_URI`, `DBT_URI` or `DBX_DSN`. A lost/malformed/nonzero reset response is exit 5, unknown, never a safe retry; use `db credential` and reconcile consumers. Exit 1 is preflight refusal, 2 deployment unconfirmed, 3 vault failure, 4 app/spec cleanup failure, 6 receipt update failure; largest wins. Receipts under `store/digitalocean-rotations/` contain no values; interrupted work remains uncertain. Only reads retry; app updates and redeploys make one attempt and uncertain consumers remain unknown until reconciled. Local pre-call failure is not_started. Deployment metadata is validated and ACTIVE compared exactly; error bodies are withheld. No automatic reconciliation, provider transaction or health proof. `tests/test_digitalocean_safety.py` (18 cases); `tests/test_digitalocean_door.py` (fake doctl and scratch vault; `review_round_four`: `--db` outside `db_names`, a binding beside a literal, a binding-only key, encrypted `DATABASE_DSN`/`DB_URI`; `review_round_five`: `db credential --db` refused with nothing in the vault, no listed databases, redis/valkey `--db`, non-name `db_names`, database words anywhere in a key); `tests/test_credential_doors.py` `test_the_digitalocean_door_keeps_the_same_rules` |
| OSS-58 | Observe the Git checkout an agent just opened | SessionStart resolves the existing project first. For an eligible Git repository or worktree outside `sources.projects`, it writes only the exact path to `config/tracked.json`; the next `full local` or tick runs the ordinary `scan_folder` → `local.json` → merge → emit pipeline. A known project gains a checkout declaration, not a second identity. `full tracked` lists declarations and `full doctor` summarizes them | No parent is scanned and `sources.projects` and `registry/projects.json` are never written by the hook. Re-entry changes no bytes; exclusions, broad folders and ambiguous identities are refused, and `full untrack local-folder PATH` leaves a removal marker that automation does not undo. Explicit `full track local-folder PATH` opts back in. Missing or malformed declarations are degraded without losing the root scan. No credential or memory binding and no organization ownership changes. The portable 0.1 `core.discover` remains preview-only and proposal authority is unchanged (`tests/test_session_start.py`, `tests/test_local_folders.py`, `tests/test_workspace.py` `test_local_folder_track_untrack_is_atomic_idempotent_and_reversible`) |
| OSS-60 | Be asked for the why of my work once, where I can answer it | After a turn that changed a watched repository (compared with the previous turn's snapshot; the session's files are those that differ from its SessionStart baseline, so files untracked or dirty before the session never count), the `observatory-log` Stop hook first records its facts as one `proposed` record per session; then, within 2 seconds, it reads the last 4 MB of the session's transcript once for the session's writing calls (Edit, Write, MultiEdit, NotebookEdit with the file each names; Bash writes through `>`, `>>`, `cat > file <<EOF`, `tee`, `sed -i`). No request is made when such a call targeted a `decisions.md` or an `adr/` `.md`, `.markdown` or `.txt` file (outside vendored trees and submodules, not deleted, with at least one non-blank added line ignoring whitespace), and its text carries the entry's own heading (the shallowest added, so not a repeated `### Consequences`), or with no heading more than half of its added lines, and that heading appears in the register exactly once as a heading line outside fenced code; or when such a call wrote `.task-pipeline/run.md` (or ran `stage_checkpoint.py`) and a `decision:` / `stage:` line among the ledger's newest 64 KB and 200 such lines, read even when git ignores it, has its text in what those calls wrote. A second revision of the same record then links the file (`file:<path>`, the entry heading or ledger line as `anchor`), and later revisions carry that link only while it holds by bytes, with no Markdown parsing: written by this hook, naming the current repository, the stored raw heading line (full length) present exactly once as a whole line, and the file byte-identical to the linked one (stored length and SHA-256 of the whole file), or unchanged in its linked bytes with only a new entry appended (the rest starts at its first non-blank line with a column-0 heading no deeper than the entry's, and the linked file ended in a newline outside any fence and comment). Any other change to the file, a change of line endings, or a move to another repository drops it, and a dropped link is attributed afresh only from the session's writes after the drop, a mark carried in every later revision. A heading over 120 characters is never linked. Every read of a register or the run ledger passes one gate: inside the checkout, not a symbolic link and not under one, opened once without following a link and without blocking, a regular file with one hard link, read up to its limit plus one byte. The Markdown heading scanner (newline-split, fences and multi-line comments skipped) only chooses a fresh anchor. Calls whose result was an error, subagent (`isSidechain`) lines and Bash commands over 65,536 characters are not attributed, and the attribution's `git diff` gets only the time left in the 2 seconds. Otherwise the first time only, and only when the turn changed tracked content (an edit, a staged or intent-to-add file, a deletion, a commit), it leaves a request for the `why`, and the session's next prompt carries it to the model as `UserPromptSubmit` context (at most 4,000 characters; a request that does not fit waits for the following prompt) and journals the delivery in `store/raw/why-asks.jsonl` (rotated to `.1` past 1 MB, both read). `whyAsks` in `store/raw/record-turn.json` and `why_asks.py` give `{asked, delivered, pending, answered, rate, window}` over 7 days, the rate being answered ÷ delivered; the board raises `observatory.why_unanswered` below 10 % with at least 20 delivered requests; `why_asks.py reached <transcript> <memory id>` says whether a request reached the model | Nothing blocks a turn or a prompt, and the record is written before any attribution: an attribution that runs out of its 2 seconds asks. Asked about: another session's edit in the same checkout, a register the session only read or grepped, a pull-request body sharing a heading, a write by a tool the pass does not know (`cp`, a script, `perl -i`; `sed -i` behind `sudo`/`xargs`/`env`, `gsed`, a sed script containing `|` or `;`), a relative path after `cd X &&`, a case-folded path on a case-insensitive volume, a subagent's edit, a failed call, a command over 64 KB, and a turn with no transcript. Every error in the prompt hook is exit 0 with no output, and a session id that is not a plain name reaches no file; a journal path that is a symbolic link is never written through. An unreadable journal asks; an unwritable request file falls back to a Stop notice; an older engine gets the Stop notice, never an injected prompt; a fault to record is still a Stop notice. A request that never reached the model is pending, never unanswered. The request file is 0600 in a 0700 directory and is pruned after 7 days by either hook. `tests/test_ask_why.py` OnlyThisTurnsChanges (`test_pre_existing_untracked_files_never_ask`, `test_a_real_tracked_edit_asks_exactly_once`, `test_repeated_turns_supersede_one_record`, `test_a_new_untracked_file_is_recorded_but_not_asked_about`, `test_a_commit_made_in_the_turn_is_tracked_content_and_old_ones_are_not`, `test_the_state_names_no_path`, `test_a_git_call_past_the_budget_answers_empty_and_is_not_a_fault`); `tests/test_corroboration.py` `test_a_session_still_being_written_is_left_to_settle`; AskOncePerRecord; SkipWhenThisSessionWroteTheRationale (`test_a_turn_editing_the_decision_register_is_not_asked_about`, `test_a_shell_heredoc_into_the_register_counts`, `test_the_rationale_is_a_follow_up_revision_of_the_same_record`, `test_the_link_survives_a_commit_and_push_and_a_later_turn`, `test_the_link_survives_the_write_scrolling_out_of_the_tail`, `test_one_revision_per_changed_turn_once_linked`, `test_sed_in_place_on_a_later_line_of_the_command`, `test_an_ignored_run_ledger_is_read_from_disk`); TheCarriedLinkIsRechecked (an edit below a `# ` line in a sh fence, a python fence and an HTML comment, an append without a heading, a deeper heading appended, an edit to another entry, an append after an unclosed fence, a drop remembered for every later turn, list-item fence, list-item comment, a list item opened above, WITHDRAWN past 120 characters, a heading over the link limit, `<!-->`, an HTML block, a setext duplicate, a backtick info string, a one-character body edit, an unchanged entry with a later entry appended, a symlinked parent directory, a FIFO, a hard link, a pre-byte-check link, re-link only after a later write, a multi-line comment, a nested four-backtick fence, tildes inside a backtick fence, a tilde fence, an unclosed fence, an indented heading, U+2028, CRLF, a register over 8 MB, deleted, checked out, a MADR entry checked out, struck WITHDRAWN, left in a comment / a code fence / an index line, duplicated, replaced by a symlink, linked outside the checkout, an anchor near the end of a register over 512 KB, rewritten, reverted, another repository, a foreign `kind: rationale`, a new decision replacing a stale anchor, a link still true is carried); NotARationale (`test_a_common_heading_in_a_pull_request_body`, `test_a_read_only_grep_of_the_register`, `test_an_ignored_ledger_line_this_session_did_not_write`, `test_a_small_edit_to_a_register_another_session_wrote`, `test_a_failed_write_to_the_register`, `test_a_subagents_write_to_the_register`, `test_a_ledger_line_found_only_in_a_call_that_wrote_elsewhere`, `test_a_register_write_older_than_the_transcript_tail`, `test_a_read_only_sed_of_the_register`, `test_a_heredoc_over_the_scan_limit_into_the_register`, and the deleted, whitespace, empty, vendored, other-session, read-back and no-transcript cases); WithinTheBudget (`test_a_1500_line_register_and_a_large_transcript`, `test_a_400_line_ignored_ledger`, `test_the_transcript_pass_itself_stops_at_the_deadline`, `test_a_long_single_line_command_dense_with_sed`); Pins; FailOpen; PendingStoreHygiene; Measured; TheFinding; `tests/test_ask_why_reach.py`; `tests/test_skill.py` T8. Whether a live model answers: no automated test; that is what the rate measures |
| OSS-61 | Know what one project spent this month and in the last 30 days | Each tick, `scan-spend` reads every key of the OpenRouter door's ledger by its hash, and counts every key the door retired inside the windows from its tombstone (`revoke`, a completed `rotate` and `revoke --forget-pending` leave one with the key's hash, project, day and its spend by day read just before the DELETE, since the provider keeps no history of a deleted key; whole only when it reconciles with the key's own month, week and day counters read last before the DELETE and the report reaches every recent day no counter checks; kept 62 days; `issue` over a row whose key is gone leaves one too, and over a key still live under another name is refused; a `rotate` of a row a failure left part done finishes that rotation and then rotates afresh, so a rotate always replaces the value; a minted successor is on the row before its delivery, read by the scan until a newer one holds the slot), and writes, beside the estate's month, each project's month so far and last 30 days, both of completed UTC days, to `store/raw/spend.json` (`projects`). A key counts for the project its ledger row names; every name of one registered project, each of its vault folders included, is one bucket keyed by its registry id. `tools/spend.py project PROJECT --json` (a folder, the registry id or the name) answers `monthUsd`, `last30Usd`, `keys`, `retiredKeys`, `folders`, `through` (the last day the provider's report covers), `partial`, `unread` and `fresh` without asking the provider; without PROJECT it lists every project and `unattributed`. It is the figure Fabric's launcher reads as a project's recent spend (contract DEC-0037) | A key with no project in the ledger, and spend on hashes no ledger row or tombstone holds, are `unattributed`, never guessed into a project from a name. A window not read whole is `null` with the key named in `unread` and the source in `degraded` (scope `attribution`), never a lower figure: a key the provider would not answer for or answered with rows that are not activity, a pending row, a tombstone without a usage snapshot or with one that did not reconcile (the days after its last activity day), a tombstone not known to be deleted whose key is gone (one still live is read by its hash), a tombstone carrying its row's untracked rotation or pending predecessor, a hash two ledger entries hold, a ledger entry that is not an object (every project), a rotation made before tombstones existed (every window starting on or before it), and the keys not reached when the step's deadline arrives or the provider becomes unreachable (the scan stops reading and still writes). A registry that will not read makes every project `null`. The top-level `partial` is about the estate total only; `attribution_partial` covers the projects. No scan, or one written before per-project figures, exits 1 with `spend:scan`; a scan older than 36 h or of another month answers with `fresh: false` and `spend:stale`; a name two projects claim exits 2 with both named. No key or hash in the file or the answer. No finding yet: a project's limit is enforced once a recorded spend policy is readable (OBS-93). `tests/test_spend.py` `ScanSpend` (3 cases), `ProjectSpend` (41 cases: two projects and unattributed, a revoked key kept with its project through its snapshot, the door's revoke, rotate, forget and issue tombstones, reconciled and unreconciled snapshots, a snapshot with no `usage_daily`, an unreadable snapshot, a failure before and after the DELETE, a failed rename, a revoke of a row with an untracked rotation or a pending predecessor, no spend after a snapshot, the 62-day prune at its 31/61/62/63-day boundary, two rotations at once and `cmd_rotate` beside one, the lock refusal, a tombstone without a snapshot or from before the windows, a shared hash, malformed ledger entries, unreadable and malformed keys, untracked rotations, the deadline, an unreachable provider, an unreadable ledger or registry, two vault folders, no hash in the file) `Reconciliation` (12 cases: one per clause of the rule, each side of the tolerance, Monday as the week's first day, a today row, midnight between the two reads, a two-day lag at a month and week start, Monday the 1st, an empty report, a missing counter), `StoneReading` (5), `RotationRetry` (2), `RoundSixDoor` (10) and `RoundSixScan` (6: the findings of verification round 6, among them a leaked value on a part-done row, a resume-only run never called rotated, a stale successor, the annotation lock, and the window edges of an unreconciled snapshot), `IssueOverAHashedRow` (2), `KeyserverThreads` (1), `RoundFourProbes` and `RoundFourProbesRevoke` (10: the adversarial probes P1-P10 of verification round 4), `RoundFiveProbes` and `RoundFiveProbesIssue` (11: probes A-K of round 5), the `RoundSix` probe classes (P1-P9 and a crash at each step of a fresh rotation, of round 6) and `ProjectCommand` (12 cases) |
| OSS-68 | Give a project a narrow Heroku credential, and see or change who is in its team | `tools/heroku.py issue --preset PRESET --vault PROJECT/ENV/NAME [--team TEAM] [--app APP]` mints a direct OAuth authorization from the stashed admin with the preset's scopes only (`identity`, `read`, `read` + `write`, `read-protected`, `read-protected` + `write-protected`; never `global`), described `observatory PRESET SLOT [team T] [app A] (managed)`, proves it reads the app, the team's apps or the account with its own value, writes it to the slot on stdin and records it in `heroku-issued.json` (id, scopes, slot, team, app, dates; never a value or the refresh token). Issuing again regenerates the same authorization; a changed preset makes a new one and deletes the old only after delivery. Every authorization is minted with an expiry 90 days out unless `--expire-days N` or `--no-expiry` says otherwise, and the expiry Heroku answers is recorded in the ledger and the slot's metadata; an authorization that never expires while an expiry is asked is replaced rather than regenerated (OBS-191, `tests/test_key_expiry.py`). `rotate` regenerates in place, `revoke` deletes at Heroku before the ledger row, `ping` reports gone or widened authorizations and managed strays. `teams`, `members --team` and `apps --team` read every page with the slots issued for each. `member add\|role\|remove` prints its plan and changes nothing until `--apply`, then re-reads the team; on a team with `team-invite-acceptance` on, add is an invitation, `members` lists pending ones and remove of a pending invitee cancels it, verified on the invitations list. `issue`, `rotate`, `revoke` and `ping` hold one ledger lock, so concurrent runs keep every row. `cloudflare.py accounts`, `members --account` and `member add\|role\|remove --account` do the same for Cloudflare accounts | Heroku cannot narrow an authorization to a team or an app; the door says so on every issue. A team or app the admin cannot read, a config preset without `--app`, a failed proof or a refused vault write mints nothing or deletes what it minted; a failure after a regenerate says the slot's value is dead. Membership writes refuse `owner`, the last admin (a pending invitee does not count), the admin's own membership; on Cloudflare, Super Administrator is read from roles and policies, the last accepted whole-account one is never removed or re-roled, any write is refused while a member's Super Administrator state cannot be read, and a policy-based member's role is not rewritten; a write the fresh read does not show exits 1. No value in argv, output, the ledger or an error (`tests/test_heroku_door.py`; `tests/test_credential_doors.py` `test_cf_accounts_names_every_account_its_tokens_and_their_slots`, `test_cf_members_and_member_writes_are_a_plan_until_apply`, `test_cf_member_writes_refuse_what_would_lock_the_account`) |
| OSS-69 | Keep each DigitalOcean team's token in one place, and see what each team runs and costs | `tools/digitalocean.py stash` takes a team's token on stdin (or `stash --from-doctl CONTEXT|all` reads doctl's config directly) and keeps it 0600, never handed out; `teams` lists each team with the user's role and the vault slots that hold its tokens, plus the user's teams with no token here; `members --team T` gives the role and, in an organization context, the member count, and says that the public API lists no members. `team create NAME --org T --invite EMAIL:ROLE` prints its plan and writes only with `--apply`. DigitalOcean's public API cannot mint a token, so `issue` refuses and prints the control-panel recipe (custom scopes per preset); `install` takes the minted token on stdin, refuses one of another team, the team's own stashed token, one that can read outside its preset's scopes, one that cannot do its preset's reads, and a label already installed (`rotate` replaces it), then delivers it and records it; `revoke` of a token whose value is gone claims nothing and keeps the record (`--forget` drops it, saying so); `rotate` and `revoke` count a revocation only when the provider then refuses the old value. With `integrations.digitalocean` on, `full scan-digitalocean` (daily in the tick) records per team the balance, Droplets with their monthly price, App Platform apps with their repositories, databases without credentials, volumes and domains; each 401, 403 and 429 is named in `degraded`, and so is each `doctl` context whose team has no stashed token, with `stash --from-doctl CONTEXT` (`teams` lists such contexts offline and `teams --probe` names their teams; OBS-421). `full doctor` counts the stashed teams and, with the integration off, raises `digitalocean-off` with `project-observatory full configure integrations digitalocean true`. The board raises `digitalocean.droplet_off_billed` and `digitalocean.app_down` (its project named only through a repository the registry holds) | yes (OBS-155) |
| OSS-70 | Move my whole agent setup to another machine | Once a day (the maintenance job), before every engine update unless a bundle younger than an hour exists, and on `full machine-backup`, one encrypted `machine-*.obmachine` goes to the backups root with the workspace snapshots: the Claude Code and Codex configuration, Git, SSH and shell settings, CLI logins, launchd or systemd user jobs, the Fabric descriptors, Homebrew, npm, pipx and uv inventories, and every checkout's remotes, branch, HEAD, dirty and ahead/behind state with a git bundle of its unpushed commits, a patch of its uncommitted changes and its untracked files; `config/machine-backup.json` adds the operator's own paths and checkout roots. `full machine-restore [BUNDLE|--latest]` prints the plan (missing, same, conflict per item); `--apply` writes what is missing, `--overwrite` moves a conflict aside first, `--packages` installs the inventories, `--clone` clones the checkouts and puts back their carried work (`machine_backup.py`) | Without a passphrase nothing is written, not even inside the workspace; a disk that would keep under 2 GB free refuses the write, recorded and shown (`machine-backup-refused-disk`), retried an hour later; transcripts and plugin caches are skipped by default and named; anything over a cap is not carried and named per folder in the bundle, the answer, `doctor`, Health and as a `machine.backup_not_carried` finding (a warning for the only copy of commits or uncommitted changes), each with its `fix`: the push, or the exact `limits` value that carries it; the caps are 160 MB per bundle, 64 MB per patch, 32 MB per untracked set and 768 MB for all checkouts, and a carried part is streamed from the private stage, never held in memory; a link is recorded, never followed; a remote URL loses its user information; no value reaches the manifest, the state or an answer; an existing checkout is never touched by `--clone`; an update never fails on its machine backup. `doctor` warns past 36 hours. `tests/test_machine_backup.py` (Taking, including `test_everything_not_carried_says_how_to_carry_it_and_commits_come_first`, `test_a_carried_bundle_is_streamed_from_its_file_never_read_whole`, `test_the_default_caps_are_the_documented_ones`, `test_the_default_caps_carry_what_the_measured_machine_left_behind`; Refusals, Restore `test_round_trip_into_an_empty_home`, `test_clone_brings_back_every_checkout_with_its_carried_work`, Schedule, CommandLine); `tests/test_machine_findings.py` BackupLeftBehind; `tests/test_finding_rules.py` `test_the_machine_backup_pair_of_one_checkout_reaches_the_board`, `test_a_duplicate_id_drops_the_later_row_and_says_so`; `tests/test_engine_update.py` `test_the_machine_backup_is_taken_before_the_jobs_stop_and_never_holds_the_update`, `test_a_failed_machine_backup_is_degraded_and_the_update_goes_on`. The Health row: no automated test renders it |
| OSS-71 | Know which Fabric project a project is, and ask by a folder | Each tick (`full fabric-attribution` by hand) asks Fabric's hub `fabric.project.resolve` for every registry folder and records `fabric:<uuid>` on the project in `store/raw/fabric-attribution.json` with its provenance; `project.resolve` answers that alias and an absolute folder; SessionStart asks Fabric first and the contract's folder rules second (`fabric_attribution.py`, `folder_attribution.py`) | Fabric not running, its pid dead, an origin other than `http://127.0.0.1:<port>`, access refused or a refusal from the tool: named in `degraded`, nothing recorded is lost, the credential is never sent. Two Fabric projects for one project: a `fabric.attribution_conflict` finding, no alias. `tests/test_fabric_attribution.py`, `tests/test_folder_attribution.py` (the contract's vectors) |
| OSS-72 | See which agents are installed here and how each is reached | `features.machine_watch` on: each tick's `scan-agents` step reads every Fabric service descriptor, its manifest and launchd; the emit joins the MCP inventory by origin and writes `registry/agents.json`, which the wiki projection mirrors to `inventory/agents.json` (`collectors/scan_agents.py`) and renders as the readable page `inventory/agents.md` (service key, product, version, where it answers, contract pin, who holds it, MCP declarations; OBS-317). Each row: origin and port, manifest pins, supervisor state, `mcp_declarations`, `key_in_url`, `first_seen_on`, `last_seen_running_on`, and `carried`/`carried_since` when kept from an unreadable directory. The document: `stale` (scan older than one tick or dated in the future), `removed`, and `totals.mcp` `attributed`/`ambiguous`/`unattributed_http`/`stdio`/`other` | Generated, never hand-edited; nothing of `auth` is kept and no row comes from a name; URLs fail closed (origin and repository kept only as a strict `scheme://host[:port][/path]`, else null; any other URL-shaped token that is not strict is replaced whole by `[url]`; the home directory is written `~`); an unreadable descriptor, manifest, launchd or MCP inventory is named in `degraded`; carried rows are left out of the supervisor totals. `tests/test_scan_agents.py`: AgentsPage (the page: generated header, order, no path or token, a cell that cannot break the table, the projection writes it); TwoDescriptors `test_fixture_services_dir_of_two_descriptors_lists_both`; HonestDegradation; Supervisor; McpJoin; History; Validator; Pipeline `test_the_emit_writes_registry_agents_and_the_projection_mirrors_it_unchanged`; FixCycle1 `test_user_info_and_queries_never_reach_the_document`, `test_an_unchanged_old_scan_is_stale_and_dates_nothing_after_it`, `test_two_descriptors_on_one_origin_attribute_to_neither_and_say_so`, `test_carried_rows_say_since_when_and_are_not_counted_as_running`, `test_a_hostile_descriptor_still_yields_a_schema_valid_document`, `test_validate_registry_refuses_a_broken_agents_document`, `test_scan_agents_runs_only_with_machine_watch`; FixCycle2 `test_every_emitted_string_is_sanitised_and_problems_stays_empty`, `test_credential_urls_without_two_slashes_lose_their_credentials`, `test_a_scan_written_by_an_older_collector_is_sanitised_by_the_emit`, `test_stdio_is_counted_by_transport_and_the_rest_is_other`, `test_a_scan_dated_in_the_future_is_stale_with_clock_skew`; Round4 `test_port_zero_and_a_port_past_65535_are_no_port_and_not_a_key`; FixCycle3 `test_every_poc_is_replaced_whole_idempotent_and_clean`, `test_fuzz_idempotent_clean_and_no_marker_survives`, `test_the_round5_pipeline_leaks_nothing`, `test_a_200kb_pathological_command_key_collects_in_under_a_second`; EmitHistory `test_first_seen_and_removed_come_from_the_previous_registry` |
| OSS-73 | Learn of a new release within five minutes without exhausting GitHub's limit | Every five minutes, the retry after a failed check included (operator rule, 2026-10-17, for every PassionCode product), the update check job (`maintain.py check`: launchd `org.project-observatory.<id>.check`, a systemd user timer `-check.timer`, the Task Scheduler task `check`), installed by `full maintain ensure` beside the maintenance job, reads one static file: the newest release's `SHA256SUMS` at `https://github.com/<repo>/releases/latest/download/SHA256SUMS`, whose wheel name carries the version. `full update --check` reads the same file. A newer release starts the maintenance job at once, which installs it at a safe point (OSS-38); the full signed verification of `--apply` is unchanged. The first check waits 90 seconds after login | Never the GitHub REST API, whose anonymous limit is 60 requests an hour per address; `--version X.Y.Z` and a person's `--api-url` mirror still read the API. A check that finds what the last one found prints nothing to `store/logs/check.log` and adds no line to the shared update log (`update_events`): the log marks a change. A feed that names no single wheel, or does not answer, is `undetermined`, never up to date. A release held for a person, or one whose update failed for a lasting reason in the last six hours, does not start the maintenance job. With automatic updates off the job still reads the feed on its schedule and never starts the pass (OBS-333). The job never runs the pass's guard, backup or update, never takes the pass's lock or writes its record (it has its own, `store/update-check.lock` and `store/update-check.json`), never sleeps, starts offset from the pass (launchd: not at load; systemd 150 s after login; Task Scheduler 2 min 30 s later), counts a run up to 30 seconds early as on time, and ends within 240 seconds, inside its own five-minute interval (LC-03). A pass and a check at the same moment both run. Scheduled means both jobs are installed and loaded; an older install gets the check job at its next `maintain ensure`, which every update runs. `tests/test_engine_update.py` `test_check_reads_the_static_feed_and_never_the_rate_limited_api`, `test_the_feed_is_githubs_static_asset_for_the_organizations_api_only`, `test_a_feed_that_names_no_single_wheel_is_undetermined_never_current`, `test_a_feed_cut_off_mid_body_is_undetermined_not_a_traceback`; `tests/test_maintenance.py` CheckJob (`test_the_check_job_is_its_own_five_minute_job_on_every_scheduler`, `test_schedule_for_installs_both_jobs_and_scheduled_means_both`, `test_nothing_new_is_quiet_and_logged_once`, `test_the_cli_prints_nothing_for_a_quiet_check`, `test_a_newer_release_starts_the_pass_once_and_the_pass_applies_it_without_asking_again`, `test_a_held_release_or_a_recent_failure_never_starts_the_pass`, `test_a_failed_check_is_logged_and_retried_five_minutes_later`, `test_off_still_reads_the_feed_on_its_schedule_and_never_starts_the_pass`, `test_a_running_check_means_no_second_one`, `test_a_check_and_a_pass_at_the_same_moment_both_run`, `test_a_run_a_moment_early_is_still_due`), UpdateLog `test_a_check_that_finds_nothing_new_is_logged_once`, Pass `test_a_check_every_five_minutes`, `test_a_failed_check_is_retried_after_five_minutes_too`; `tests/test_osschedule.py` `test_maintenance_on_windows_is_a_task_after_logon_and_hourly`. The real `launchctl kickstart`, `systemctl start` and `schtasks /Run`: no automated test |
| OSS-74 | Know when my organization enrollment needs me, and when the organization removed this machine or closed | `full org status`, `full doctor` (`organizations`) and the Health page name each enrollment's certificate expiry date and state, read from the saved state without the network: valid, expiring in its last three days (with `full org rotate --org ID`; the maintenance pass rotates it at two thirds of its validity or an hour before the notice, whichever comes first, never in the first half, so before the notice for certificates of about 6 days or more, OBS-423), expired and renewing by itself until a date 30 days after it ended (with `full org check-in --org ID`), expired past that or with its renewal refused (with the `full org enroll` command and the new-token step), removed from the organization, with the date and the server's code, or the organization closed, with the date its closing began (`node/org.py` `notice`, `notices`) | A check-in or tick within 30 days of the certificate's end (on the server's clock) renews it first by the key on record, with no token and no `Authorization` header (contract DEC-0100), keeping the user (the same person by the certificate's user id) and role on record under a new epoch, a certificate re-issued to another person refused as `binding-changed` (`test_a_renewal_that_binds_another_person_is_refused_and_carries_nothing_to_them`); an unanswered renewal backs off, a refused one is not asked again for that certificate. Past those 30 days, after a refused renewal, or for a removed machine, a check-in, rotation or flush on an expired certificate is refused before the network as `certificate-expired`, naming the date, the step and the command, and `full org` exits 1. A refusal `certificate-revoked` or `certificate-unknown` with HTTP 401 (`certificate-unknown` only when no previous certificate is left to fall back to) ends the enrollment; any other status and every 5xx are retried, and so is `unknown-org`, a server's own enrollment code (DEC-0103) that `org enroll` names as an unknown organization id; a batch refused with these codes is held, never rejected: nothing more is sent or retried, the events it held are dropped with one count kept, `org leave` cleans it up and a new token's `org enroll` starts over. A 410 `organization-closed` with the contract's body (DEC-0103) on any request ends the enrollment on that one answer and drops everything held for that organization (events, registrations, its policy root, keys and collection start), the machine's other enrollments untouched; a 410 with another body is retried. A `purpose` rejection holds that kind of event until another acknowledged notice, never on a timer, and only on an enrollment bound to a person who has acknowledged one; elsewhere it is consumed and logged once a day; status, doctor and Health name every held kind (DEC-0100, DEC-0102; `test_an_org_cloud_machine_never_holds`, `test_status_doctor_and_health_show_every_held_kind`). `tests/test_node_org.py` `test_status_names_the_expiry_date_and_warns_three_days_before`, `test_a_certificate_said_to_be_expiring_is_rotated_by_the_tick`, `test_rotation_is_due_before_the_expiring_notice_and_never_in_the_first_half`, `test_a_check_in_on_an_expired_certificate_names_it_and_the_exact_step`, `test_the_terminal_says_an_expired_certificate_and_its_step_and_fails`, `test_health_lists_each_enrollments_expiry_without_the_network`, `test_doctor_and_health_read_the_same_notices`, `test_a_revoked_certificate_removes_the_machine_and_drops_what_it_buffered`, `test_unknown_org_on_a_device_request_is_retried_and_never_ends_the_enrollment`, `test_an_enrollment_0_29_ended_on_unknown_org_is_active_again_on_first_read`, `test_an_enrollment_the_server_refuses_unknown_org_names_the_organization`, `test_a_closed_organization_ends_the_enrollment_on_one_check_in`, `test_a_batch_answered_410_is_the_enrollments_end_not_a_rejection`, `test_a_share_registration_answered_410_stops_for_that_organization`, `test_a_410_that_is_not_the_contracts_body_is_retried`, `test_a_closed_organization_leaves_the_machines_other_enrollments_alone`, `test_a_purpose_rejection_holds_that_kind_until_the_person_acknowledges_another_notice`, `test_only_another_acknowledged_version_than_at_the_rejection_resumes`, `test_only_a_401_ends_the_enrollment_and_a_5xx_never`, `test_a_401_certificate_unknown_falls_back_first_and_ends_only_when_that_is_refused`, `test_expiry_is_judged_on_the_servers_clock`, `test_a_refused_batch_that_names_the_org_or_the_certificate_is_held_not_rejected`, `test_the_fallback_judges_the_previous_certificate_on_the_servers_clock`, `test_an_unreadable_node_folder_is_named_and_doctor_still_answers`, `test_a_certificate_the_server_forgot_with_no_previous_one_removes_the_machine`, `test_a_removed_machine_leaves_and_a_new_token_starts_over`, `test_a_check_in_past_expiry_renews_by_the_key_on_record_without_a_token`, `test_the_renewal_request_is_the_contracts`, `test_a_tick_past_expiry_renews_and_then_does_what_is_due`, `test_the_window_is_thirty_days_on_the_servers_clock`, `test_inside_the_window_status_says_it_renews_by_itself`, `test_a_server_that_refuses_the_renewal_is_asked_once_then_a_token_is_needed`, `test_an_unanswered_renewal_is_retried_with_backoff`, `test_a_revoked_device_does_not_renew_and_is_removed`, `test_a_removed_enrollment_never_renews`, `test_the_renewal_holds_the_enrollments_lock`. The Health rows: no automated test renders them |
| OSS-75 | Know what my organization is told about the agents on this machine, decide whether it is told, and see how it differs from what it expects | Nothing is shared until I run `full org summary --org ID --consent on` for that organization (recorded with its time; `--consent off` stops it; a renewal by the key on record keeps it, while a new token's enrollment, a removal or a closed organization ends it, so I am asked again: `test_a_new_token_enrollment_keeps_the_sequence_rising_and_asks_consent_again`, `test_an_organization_closed_clears_the_consent_before_a_new_enrollment`); before that `full org status`, doctor and Health say `harness summary: not shared (consent off)`, and `full org summary --org ID` prints the summary exactly as it would leave, sending nothing. Once on, to a server that lists `node-summary/1`, each due check-in carries the harness summary (contract DEC-0101): the catalogue's coding agents installed here with versions, the MCP servers loaded with source, transport and health (a verdict whose probe is more than 48 hours older than the summary is `unknown`: `test_a_verdict_older_than_48_hours_is_unknown_health`), skills, enabled plugins and the hook count, when it changed and at least once a day, minus the names my owner-only `config/node-summary.json` excludes (counted here). Status and Health say when the server last took one and how much it listed (`node/org.py` `summary_notice`). Drift from the organization's baseline (`missing`, `stopped`, `changed`, `outdated`) is computed by the port of `harnessDrift` and shown once a signed policy carrying a baseline is applied; none is applied yet, so none is sent (`tests/test_node_summary.py` `Consent`, `CheckIn`, `Build`, `Drift`; `tests/test_harness.py` `test_installed_coding_agents_by_catalogue_id_and_version_never_run`) | Never a path, command, argument, environment value, URL, hook command, prompt, output or file content: an allow-list of fields, checked against FAC-SEM-128 before it is handed over, refused whole otherwise; a name the schema refuses or that holds a home folder (the contract's dashed form after any separator, DEC-0105) is left out and counted on this machine. A summary naming another device or organization than this enrollment's certificate stays here (DEC-0114; `test_a_summary_another_device_or_organization_would_name_stays_here`), and the check-in lists only what the node reads, so no server sends it repairs or credentials yet (DEC-0116, DEC-0117; `test_the_check_in_lists_only_what_the_node_reads`). A scan whose integration is off, older than 48 hours or written before the summary existed, or an exclusion list that is not owner-only, sends nothing, and `org status` says `org:node-summary-unavailable` with the reason (`Scans`); a server that does not read it gets none (`org:node-summary-not-listed`). A check-in refused for what the summary may cause goes again at once without it, and the summary waits a day (`org:node-summary-refused`; `Refusal`), so heartbeats, rotation and renewal never stop. A failed check-in spends the summary's sequence and sends it again (`test_a_check_in_that_failed_spends_its_sequence_and_sends_again`, `test_a_summary_that_would_break_fac_sem_128_is_refused_not_sent`) |
| OSS-76 | Let an agent wake, stop or restart one of my apps without touching it itself | `full lifecycle enroll TARGET --kind app --path PATH --caller agent:NAME --startup-effect TEXT` records an app whose signature it verified (or a LaunchAgent by its hashes), `full lifecycle allow agent:NAME --binding ID --tool ... --target ...` grants the caller its tools, tied to the one access binding that proves it; the caller, holding an access binding, calls `lifecycle.list`, `lifecycle.ensure_running` and `lifecycle.operation.get` at `POST 127.0.0.1:47311/mcp` and the app starts once; `full lifecycle status` shows targets, callers and the last events | Nothing is served before a target is enrolled; a request without a binding is 401, a binding without a grant 403, the service token opens no tool; an app the person took over or is using is never stopped by an agent (`in_use_by_person`), a held target never starts (`held_off`); every change needs a terminal; other systems answer `unsupported_platform`. `tests/test_node_lifecycle_service.py` `test_one_ensure_through_the_wire_starts_once_and_reads_back`, `test_mcp_auth_scopes_and_trace`, `test_s3_the_descriptor_token_is_not_an_mcp_credential`, `test_hold_and_release_bump_the_revision_and_an_agent_gets_held_off`, `test_every_change_needs_a_terminal`, `test_unsupported_platform_is_said_never_a_start`. Stopping an idle app and restarting an always_on one wait for the supervisor (OBS-79 step L4) |
| OSS-77 | Have the whole queue of proposals reviewed constantly, without an agent running, and resolve what needs me in one launch | `full review policy show` lists the standing policy's rules (a closed set: exact duplicates superseded, `resources`-only proposals from an agent appended without replacing anything, `aliases`-only proposals without a conflict, the superseded readings of a day folded out of my count) and what each would decide on the queue now; it writes nothing. `full review policy enable`, at my terminal, enables exactly those rules and records their hash, the time and my login; from then the tick's `review-policy` step applies them, at most 500 a run, each as `policy:<rule>@<hash>` with its reason, journaled. What no rule decides waits for me: `full review batch prepare` writes every such item with a recommendation and a reason an agent may edit, and `full review batch apply FILE`, at my terminal, shows the totals and a sample, asks once and applies them all as me. The finding `review.queue_waiting` appears at 20 waiting or when the oldest is 14 days old, naming those commands; `full doctor` (`review`) and the Health row "review queue" show the policy's state, its last run and what waits | Nothing is ever decided as me without my terminal: `tests/test_review_policy.py` `test_a_resources_patch_is_appended_and_decided_by_the_policy_never_the_operator`, `test_enable_and_disable_need_a_terminal`; an edited or unknown rule decides nothing (`test_changing_the_rules_after_enabling_stops_the_policy`, `test_an_unknown_rule_makes_the_whole_policy_invalid`); `full review undo-policy ID` or `--since TIME` takes decisions back and removes only what they added (`test_undo_needs_a_terminal_and_restores_exactly_what_the_decision_added`, `test_undo_since_reverts_a_new_row_and_an_alias_and_a_fold`); a batch item that changed after preparation is skipped and named (`test_prepare_recommends_and_apply_asks_once_at_a_terminal`); the ledger's lifecycle is untouched by a fold (`test_superseded_readings_fold_in_the_journal_and_the_ledger_is_untouched`) |
| OSS-78 | Know before anything runs when my organization asks to repair this machine, decline it, and be sure nothing else runs | A repair the organization sends (`repair-request/1`, contract DEC-0101) is judged as the contract's `repairDecision` judges it, over every case of its vectors (`tests/test_node_repair.py` `Vectors`). One that passes everything but me is shown at once in `full org status` (`repairs`), `full org repair`, doctor and Health with the organization's one-line task, the package and when it starts, at least 15 minutes ahead, with `full org repair --org ID --decline REPAIR_ID`; declined, it is answered `person-declined` and never runs (`test_the_person_is_told_first_and_it_runs_after_the_notice`, `test_a_declined_repair_never_runs`, `test_enabled_the_whole_run_and_the_person_first`, `test_a_person_declines`). It runs only as the `restart` or `ensure_running` of a `node.lifecycle` target I allow-listed for that package in `config/node-repair.json`, through the broker with an access binding I issued (`Book`, `LifecycleDoor`, `Config`) | Nothing runs while the organization's signed policy and key set are not applied (today, OE-06): the check-in does not ask for any, and a repair that arrives anyway is counted in the journal and dropped (`test_with_no_policy_applied_the_node_asks_for_none_and_reads_none`). A request too deep, too large or holding a lone surrogate never stops a check-in (`Hostile`, `test_a_check_in_keeps_what_its_response_said_whatever_the_repairs_do`); only a machine that enrolled as `org-cloud` and binds no person skips my notice, and a rotation or renewal that adds a role is refused (`OrgCloud`). A request or policy that expired while the notice stood is refused when it would start (`test_decided_again_when_it_would_start`); a package with no allow-listed action fails before I am told; a request the schema refuses is never shown or run (`test_a_malformed_request_is_never_shown_or_run`); an id runs once and its one result is sent again unchanged (`test_once_per_id_the_same_result_again`, `test_a_refusal_is_recorded_answered_once_and_again_for_a_redelivery`); results go only to a server that lists `repair-result/1`, and one that refuses them never stops check-ins (`test_results_go_only_to_a_server_that_lists_repair_result`, `test_a_server_that_refuses_the_results_never_stops_check_ins`); a removed machine runs nothing more (`test_a_removed_machine_runs_nothing_more`). The Health rows: `HealthAndDocs` checks the source and both languages, no automated test renders them |
| OSS-95 | Set up a second machine from this one's backups while this one keeps running | A machine bundle and a workspace snapshot name the machine they were taken on (`machine_id`, `machine_identity.py`). Restored on another machine, or naming none, `full machine-restore` writes `~/.ssh/config`, `known_hosts`, `authorized_keys`, `allowed_signers` and every `*.pub` but no private key (each withheld key directly in `~/.ssh` gets two steps in `ssh_keys` and `next`, `full ssh-keys rekey NAME --fingerprint SHA256:...` and the same with `--apply`, before `--clone`, the fingerprint read from the bundle's public half and `--server ALIAS` for a Host whose line is not `~/.ssh/NAME`, OBS-300), stages every launchd plist (systemd user unit off macOS) but this engine's own (each machine installs its own) in `${XDG_DATA_HOME:-~/.local/share}/project-observatory/machine-move/` where nothing loads it at login, and names each in `placement.json` (`observatory-placement/1`) as running on the other machine; `full restore` leaves out `node/device/`, `node/orgs/`, `node/records/` and `<home>/store/maintenance.json`, turns `features.scheduler` off and gives the workspace its own `instance_id`, so `full node --init` makes this machine's own device id. `full machine-move leave LABEL --to ID` on the old machine, then `full machine-move cutover LABEL` on the new one, moves one job so it never runs on both (`machine_backup.py`, `machine_move.py`, `workspace_upgrade.py`). A partial move (OBS-282): every plan row names its category and the answer's `categories` says per category what an `--apply` would write; `--only` or `--skip` (`ssh-private`, `ssh-config`, `launchd`, `cli-logins`, `claude`, `codex`, `git`, `shell`, `fabric-services`, `packages`, `agents`, `operator`) limit what is written and `--projects` limits `--clone`; each writing step leaves `machine-move/restore.json`. `full machine-move status` (OBS-286) prints the checklist M1 to M4 (one release, the restore complete, its own SSH key, its own device id, every vault slot `same`, no job defined on both), each `ok`, `missing` with its step, or `unknown`, against the other machine's facts (`full machine-move facts --out FILE` there, `--peer FILE` here) | `--same-machine` restores whole and is refused away from a terminal; a file whose text is a private key is withheld under any name; a job this machine already defines is left as it is; a load that fails puts the plist back in staging; a placement file that is not this schema is refused, never replaced; a same-machine restore and an update's rollback are unchanged. `tests/test_machine_backup.py` SecondMachine (`test_another_machine_gets_no_private_key_and_no_loaded_job`, `test_the_same_machine_keeps_todays_behaviour`, `test_a_bundle_that_names_no_machine_is_another_machines`), SecondMachineKeys (`test_one_rekey_step_takes_the_place_of_create_and_send`, `test_the_rekey_step_carries_the_other_keys_fingerprint_from_its_public_half`, `test_without_its_fingerprint_the_plan_says_how_github_learns_it`, `test_a_host_rekey_would_not_see_is_named_to_it_with_server`, `test_the_fingerprint_is_read_from_the_private_files_clear_header`, `test_the_emitted_step_parses_as_rekey_with_its_name_and_fingerprint`), MachineMove, MachineId; `tests/test_workspace_upgrade.py` `test_another_machine_never_takes_the_node_identity_or_the_schedules`, `test_then_node_init_makes_this_machines_own_device_id`, `test_the_same_machine_restores_the_whole_snapshot_as_before`, `test_a_snapshot_naming_no_machine_is_another_machines_unless_asked`. An unknown category is refused with the list, `--only` with `--skip` is refused, `--packages` with `packages` left out is refused, a private key stays withheld on a second machine whatever `--only` names, a folder holding a selected key keeps its mode, a project no checkout matches is refused before anything is written, and a worktree brings its main checkout; status never guesses: with no facts, or facts older than 48 hours, the cross-machine items read `unknown`, it writes nothing and never creates the estate key. `tests/test_machine_backup.py` SecondMachine (`test_only_writes_the_named_categories_and_the_plan_says_what_each_would_write`, `test_ssh_private_stays_withheld_on_a_second_machine_whatever_only_says`, `test_a_receipt_names_the_restore_and_the_other_machines_keys`), Restore (`test_projects_clones_only_the_named_checkouts`), MoveStatus, MoveStatusVault |
| OSS-96 | Give a second machine its own SSH key everywhere the other machine's key is accepted | `full ssh-keys rekey OLD` plans, reading only: this machine's key `~/.ssh/OLD` (made, or already here and reused; `--name NEW` for another name), the GitHub account gh is signed in to, and each place the other machine's key is registered with why: GitHub sign-in and signing only where that account holds the other key's fingerprint (`--fingerprint`, or `OLD.pub` when it is here); each Host of `~/.ssh/config` naming `~/.ssh/OLD` and each `--server ALIAS` that is neither GitHub nor a `git` login. With `Match exec` in the config the plan never runs `ssh -G`. `--apply` creates the key, sends it with `send --to github --as USE` and `send --to server:ALIAS --verify`, and ends with what still accepts only the other machine's key, each with its reason and next steps (`ssh_keys.py` `rekey_plan`, `cmd_rekey`) | The other key's private file is never read or named to a tool; nothing is removed anywhere; a step that fails does not stop the others, and a key ssh-keygen did not make is never sent; a Host pattern, a `Match` block, hosts using a default key without an IdentityFile line and unread GitHub lists are named with the command that makes them known, never guessed; this machine's own key is never taken for the other one; a passphrase is refused away from a terminal before anything runs. `tests/test_ssh_keys.py` Rekey (`test_apply_creates_sends_and_verifies_everywhere`, `test_a_server_that_does_not_let_this_machine_in_is_listed_with_its_next_steps`, `test_the_other_machines_identity_is_read_from_its_public_half_only`, `test_without_the_old_fingerprint_github_is_never_a_target`, `test_a_key_the_signed_in_account_does_not_hold_is_not_uploaded_to_it`, `test_a_config_with_match_exec_is_never_handed_to_ssh_g`, `test_a_git_service_host_is_not_a_shell_target`, `test_a_second_run_reuses_this_machines_key_and_adds_nothing`) |
| OSS-97 | Know what anonymous usage data the product sends, and turn it off | The first `full` command at my terminal after the release prints once what is sent (command, tool and tick names, outcomes, durations, error class names, versions, platform, a random install id) and what never is (keys, paths, names, workspace content), and how to turn it off; the dashboard's Home shows the same for a week and Health shows the switch with its command; `full analytics status|on|off|send`, `full doctor` and the apps' Settings switch read and change it (`usage_analytics.py`) | On by default, also after an update; off is the file `analytics` in the workspace home and survives updates, and it deletes the queue and the install id; `OBSERVATORY_ANALYTICS=off` or `DO_NOT_TRACK=1` stops one process; a build without an App-Key collects nothing; no command waits on the network, and a failure is a local counter. `tests/test_usage_analytics.py` (Switch, Record, Sink, Flush, Notice, Hooks), `tests/test_analytics_catalogue.py` in the repository |
| OSS-98 | Follow a session Fabric started for a board request, and know it got only the keys the request needs | `observatory_workflow_list` and `observatory_checkpoint_latest` show `dispatch` for such a workflow, read from the packet as Fabric wrote it in the workflow's first checkpoint: the request, its thread, the capability, Fabric's project id, the deadline, the run's end and the key window (`open`, or `closed` with its reason). `use_secret.py run --workflow wf_…` releases a slot only to the workflow's lease holder (`OBSERVATORY_WORKFLOW_LEASE`, or a private file named in `OBSERVATORY_WORKFLOW_LEASE_FILE`), only when the frozen packet names it, only from the project `fabric-project` resolves to, only until the deadline (`dispatch_packet.py`). A guard for a cooperating run, not containment: a session that unsets `FABRIC_DISPATCH_WORKFLOW` takes the ordinary path, and containment is the vault's `slotsRequireGrant: always` with a delegation token | A malformed packet ref, a ref with other members, a repeated one and an absolute path in a packet are refused on write; a later checkpoint that changes the packet's refs or credentials is refused (`PacketFrozen`) and one that leaves them out keeps the keys, across a handoff too; a later checkpoint that adds packet refs to a workflow that started without them is refused (`PacketNotFirst`), and an older row whose packet first appears after revision 1 is no packet and gets no keys (`packet-not-first`); a deadline more than seven days after the packet's write is refused (`DeadlineTooFar`), and an earlier row with such a deadline or an unreadable one gets no keys; every refused release exits 2 with its code and is journalled by code, never with a value or the lease; a lease file that is not this user's private regular file is refused, judged on the opened descriptor rather than the path beforehand (a link and a FIFO are refused without being followed or waited on); a nested `use_secret.py` call that inherits only `FABRIC_DISPATCH_WORKFLOW` gets nothing. `tests/test_dispatch_packet.py` (Packet, View, Gate, Frozen), `tests/test_use_secret.py::test_a_dispatched_run_gets_only_its_packets_keys`, `tests/test_use_secret.py::test_the_lease_file_is_judged_on_its_opened_descriptor` |
| OSS-110 | Ask for a narrow credential, and have the administrator issue it into the slot without anyone seeing its value | An agent files `tools/credential_requests.py request --agent urn:fabric:agent:PUBLISHER:ID --provider P --preset NAME --slot PROJECT/ENV/NAME --reason "..."` (`--context org:ID` for an organization's agent, `--scope zone=...`, `--limit USD` for an OpenRouter key, `--expires-in-days N`): the contract's `credential-request/1`, no value. `list` shows it open; the board raises `credential.request_waiting`. The administrator runs `issue ID [--account LABEL]`: the door (`openrouter.py`, `cloudflare.py`) mints a narrowed credential in that account, ending no later than asked, and delivers it into the slot in its own process; the slot's metadata records the provenance (request, issuer door and account, agent, context, slot, preset, scope, ceiling, end, revoke handle), `vault.py list` and `use_secret.py names` say "issued on request ID", `observatory_credentials` gives `issuedOnRequest`, and the decision is recorded as `credential-decision/1`. `refuse ID --reason R`, `pending ID` and `withdraw ID` close or keep it; `revoke ID` revokes at the provider the credential it issued (by key hash or token id), proves it, and deletes the value it issued; `revoke ID --settle "how"` closes an unproved revocation (OBS-191 parts 2 and 3) | Refused before anything is written: a path or a credential-shaped word in the reason or scope, more than 30 days in an organization's context, a provider or preset no door offers on request (a Cloudflare preset whose token is named by zone), an OpenRouter key without a ceiling or with a lifetime cap, a second open request for the slot. Refused before any provider call: a decided request decided again, a slot that holds a value (never overwritten), an organization this space is not enrolled in or was removed from. A credential whose end is unconfirmed or later than asked is revoked again and the request stays open with the attempt recorded; a refused revocation keeps the request issued and raises `credential.revocation_refused`; a revocation never deletes a value the slot holds since, never revokes a key a rotation put under the same name, and leaves a Cloudflare token alone when the slot holds another value (it may be the token rolled in place); a value put while the door waited is never overwritten; an issue a crash interrupted is named; one request is never issued twice at once. No value in the queue, the journal, the metadata or an answer (`tests/test_credential_requests.py`). Sending a request to an organization and opening its sealed envelope: not built (OBS-454) |
| OSS-111 | Give another space on this machine one of my keys without anyone seeing it | `python "$(project-observatory full-path)/tools/vault.py" copy PROJECT ENV NAME --to-store PATH` (`--to-slot NAME` or `PROJECT/ENV/NAME`, `--json`) reads the slot and writes it into the other store's vault inside one process (OBS-476): the value is never printed, never in an argument, a file outside the two stores or a log; the source slot stays; both stores' movement journals record `copied`, each naming the other store and slot; the receipt `vault-copy/1` names the slot, both stores and two keyed digests (under the source's estate key when it has one, else a one-time key) that read `match: true`; the value's expiry and provenance travel with it and its metadata gains `copied_from` | Refused, each with its code and the target left as it was (on Windows a new store a copy made stays, empty but for its lock file): an invalid source slot, an empty source or one that cannot be read as a private single-line value, a target path with `.` or `..`, a target inside a Git working tree (or a working tree below its `projects/`), the home folder, a `projects/` that is not a vault, a file in the target that fails the vault's own checks (`target-unsafe`), an open leak of the source (`source-has-open-leak`: rotate and settle first), a relative, linked or missing-parent target, a target that is a file or a vault folder, the same or a nested store, a store another account owns (`target-other-account`) or this account cannot write (`target-not-writable`), an invalid target slot, and a target slot that holds another value (`target-slot-filled`) unless `--replace`, which archives it there; the same value again is `unchanged` and journals nothing. Across machines: OBS-194. `tests/test_vault_copy.py` |
| OSS-112 | Know which skills and plugins each of my agents loads, at which version, and which plugin is behind | `full machine --skills` and `observatory_machine(section="skills")` list every skill (name, version, scope `user`, `project` or `plugin`, agent `claude-code` or `codex`) and every plugin install (version, scope `user`, `project` or `local`, enabled or not, the newest version its marketplace's local copy publishes) of this space, read from the last harness scan (OBS-477); a plugin behind that version, not known to be disabled and, for a project or local install, of a known project, is the finding `plugin-behind` with `claude plugin update NAME --scope SCOPE`, and the board's `harness.plugin_behind` | Nothing is fetched and no path is recorded; `latest` follows Claude Code's order (the plugin's manifest in the marketplace's folder, then the catalogue entry), and a plugin from a Git source has none. A record or folder that cannot be read, or has another shape, is named in `degraded`, never read as empty (a settings file unread makes `enabled` null, not false); a scan older than 24 hours is `stale`; over MCP the answer comes 100 rows a page, findings, plugins, then skills (`cursor`); no scan, or one older than the inventory, is named with the command that fills it. An organization's plugin enabled in a person's own space is not reported: `open` says it waits on OBS-359. `tests/test_harness.py` Inventory, `test_the_findings_say_it`; `tests/test_machine.py` Skills |
| OSS-113 | Keep reaching a service at its port here after it moved to another machine, and undo it when it comes back | `full machine-move leave LABEL --to MACHINE_ID --via USER@HOST` (macOS) moves the job as always and, when a Fabric descriptor names LABEL with a local origin, installs one launchd job `org.project-observatory.forward.<id>.<instance>` running `/usr/bin/ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -L 127.0.0.1:PORT:127.0.0.1:PORT -- USER@HOST`; the placement entry names it in `forward`. A hand-made ssh -L job for that port to the same USER@HOST is adopted (`forwardAdopted: true`), never installed twice; a forward to another destination or a port held by anything else is refused by name (`forward-elsewhere`, `port-held` with the `lsof` line), the move itself done (exit 1). `full machine-move return LABEL` unloads and removes the forward the engine installed and clears the field; an adopted one stays loaded unless `--remove-adopted` (then unloaded and set aside, never deleted); `cutover` refuses while a forward is recorded | A malformed `--via`, a label without a local-origin descriptor and `--via` off macOS are refused before anything moves (exit 2). The ssh destination is named per `leave`: a machine id carries none, and one machine may hold several spaces. Fabric Dashboards (FD-196) reads `forward` to tell a forward that is not running from one never set up. Tests: `observatory/engine/tests/test_machine_forward.py` |


## Offline verification receipts

A contributor or agent runs `full check --suite NAME --report-dir NEW_DIRECTORY`.
The successful path prints every selected suite and publishes the final summary
only after logs and summary permissions succeed. If one sandbox, process launch or
log capture fails, its row is ERROR with unknown completion and exception type;
completed peers keep their evidence and the overall exit is nonzero. If report
export fails, stdout still carries those rows, overall FAIL and report_error_type;
a final success summary is not published. Recover by correcting the named runtime
or filesystem problem and rerunning affected verification in a new report directory.
Each suite's sandbox is removed when the suite ends, its log kept; a run that would leave
the temporary folder under its free-space floor (one source copy per running suite plus
2 GB) refuses before it copies anything, exit 7 with `insufficient_disk`, the bytes needed
and free, and `--min-free-gb 0` named as the override (OBS-167;
`tests/test_runner_footprint.py`).

Coverage: `tests/test_runner_error_receipts.py` exercises a worker setup exception,
a refused report directory and a summary permission failure. Synthetic behavior
verified; live operational acceptance remains unobserved. This contributor scenario
extends the existing scenario base without changing dashboard navigation.
OSS-22 apply revalidation: temporary candidates with Git ancestry or nested Git
markers are preserved; build output repeats checkout-idle, ignored/tracked and
new-write checks. Regression: `test_disk_guard.py::TargetRevalidation` and
`FreshTargetProbe`. These point-in-time checks do not lock out arbitrary writers.

OSS-22 measurement coverage: unavailable swap is null with source time/reason;
measured zero remains zero. A missing/unreadable root or entry, or exhausted
large-file scan budget, is incomplete. Partial successes remain visible, with
coverage metadata in the MCP overview and an incomplete label on the page.
Regression: `test_disk_guard.py::CoverageTruth`.

OSS-22 target preservation: structural bare repositories and paths beneath them
are preserved in temporary, cache and artifact inspections. Every path action
requires its planned device/inode; a replacement requires a new plan. Regression:
`test_disk_guard.py::SnapshotSafety` (including positive planner identity coverage).

OSS-22 registration fairness: refresh explicit worktree membership before build
planning, keep old hygiene timestamps unchanged, reject stale registration rows,
and rotate the persisted last-attempted root after a bounded scan or interruption.
Per-phase holder evidence retains failures even when cache commands need no probe.
Regression: `test_disk_guard.py::RegistrationFairness`. Oversized roots are not
resumed within the root; incomplete coverage remains visible.

### Harness configuration recovery (I-2)

An operator or agent running the enabled harness scan sees named credentials from
user settings and configured project MCP files without values or absolute project
paths. A malformed config keeps recovered names and adds an incomplete-coverage
warning. A tracked config raises the plaintext finding to critical. The recovery
message distinguishes parse, permission, size and unknown Git tracking failures.
Coverage: `tests/test_harness.py`:
`test_malformed_tool_config_recovers_names_and_reports_degradation`,
`test_project_headers_and_user_env_use_path_free_names`,
`test_tracked_project_config_is_critical`,
`test_malformed_shape_and_unreadable_config_remain_degraded`,
`test_project_registry_id_is_used_for_exact_folder_match`.
Status: implemented; product outcome unobserved.

### Tick scheduling and input age (K-2)

A reader sees an explicit unknown scheduling warning for unsupported receipts.
Only collectors planned in the recorded tick can trigger starvation; disabled
collectors preserve historical completion instead of pretending to refresh it.
Stopped, failed and never-started steps have distinct titles. Never-started
merge/emit/validate remain warnings, and guarantee-breaking steps remain critical.
Old merge inputs retain their source timestamps and generate degradation.
Coverage: `tests/test_tick_findings.py`, `tests/test_merge_input_age.py`.
Status: implemented; product outcome unobserved.

### Guard stop and coverage findings (K-7)

After three consecutive stopped Git cleanup summaries, the reader sees a warning
with the count and pressure. An unmeasured guard source stays visible as info;
only the Trash/FDA cause names the human macOS permission step. Malformed journal
rows break the consecutive-stop evidence and expose incomplete journal coverage.
The bounded agent view retains carried Git evidence.
Coverage: `tests/test_machine_findings.py`,
`test_machine.Tool.test_the_overview_says_when_the_git_survey_is_partial`.
Status: implemented; product outcome unobserved.

### Engine-update backup inventory

`full backups status` counts local `before-update` snapshots alongside snapshot,
before-upgrade and daily copies. `full backups migrate` verifies and encrypts those
known kinds under the existing retention policy; damaged copies remain with a skipped
receipt, and foreign kinds remain untouched. Unknown kinds are outside the reported
known-snapshot count. No automatic migration is added. Synthetic coverage:
`test_backup_vault.BeforeUpdateInventory` checks count/warning, encrypted restoration,
foreign-kind preservation and invalid-copy reporting. Live backup migration is unobserved.

### OSS-22, in detail: incomplete holders and final worktree observation

When a process survey returns partial output, diagnostics, undecodable or
unparseable `lsof` names, or an unavailable collector, cleanup treats
holder coverage as unknown. No omitted path is called free, and an incomplete
`ps` observation does not authorize a cache-tool command. Before removing either
a clean or an archived dirty worktree, a fresh target probe runs after all
Git/archive checks on POSIX, bounded by five seconds and the remaining action budget.
Held, unknown or exhausted outcomes keep the worktree and report the refusal;
a valid free result retains ordinary removal behavior. The prior 20-second
inventory cache cannot override this final observation. Age remains measured
when planning, and later writers remain outside the cleanup lock's guarantee.
Windows Git worktree removal refuses unknown holder coverage. Native shared-delete
rename behavior is a separately selected Windows control, not a non-use probe.

Evidence: `tests/test_disk_guard.py::HoldersCompleteness`,
`WorktreeFinalHolders`, and `FreshTargetProbe` (synthetic listings plus disposable
Git worktrees). `tests/test_git_hardening.py` injects both preliminary and final
holder outcomes so its Git tests do not inspect the host process table.

### Missing registration cleanup result

When a worktree path is absent, the person and agent see one registration's entry
moved to `.git/worktrees-retired/` (`retired_entry`, never a working directory, never a
repository-wide prune) and the ref that keeps its HEAD (`retained_ref`: its branch, or
a direct `archive/wt-<sha8>` for a detached HEAD). A directory that reappears at the
last moment loses nothing and can be registered again with `git worktree repair`. A path on an unplugged drive,
under a renamed parent or behind a permission error is unknown and nothing is pruned.
An unknown or changed HEAD prunes nothing. Git failure or interruption produces a
failed row with unknown effects, never `pruned`, and invents no registration count.
A path that reappears is skipped. Raw command output is absent from reasons.
Proof: `tests/test_git_hardening.py::MissingRegistrationReceipt` (eleven cases, real
Git worktrees: renamed volume, permission-denied parent, detached and branch HEADs,
a last-moment reappearance, a symbolic archive ref).

### Open-deleted-file visibility

When directory totals do not explain disk use, a person runs
`full machine --open-unlinked` on macOS/Linux. The result is a bounded observation
of logical bytes held through open handles. It never offers a delete or kill action.
Shared inode references count once; incomplete visibility and unknown reclaimability
remain visible. An agent reads the same saved result through
`observatory_machine(section="coverage")`, without launching a new scan.
Missing, unsupported, malformed and stale receipts are distinguishable from a
current successful empty observation. `full doctor` carries the same saved state.
Tests: `test_open_unlinked.py::Observation`, `test_open_unlinked.py::SavedObservation`.

### Observed local checkout state

A person runs the existing explicit-root checkout scan and reads saved coverage;
an agent reads the same coverage section. Each sample separates recorded Git state
from current state and exposes bounded status/index record counts. Ignored records
do not stand for all nested files. Assume-unchanged/skip-worktree flags, malformed
or partial output, changed bindings, stale evidence, and unsupported Windows pipes
show unknown; bare repositories show not-applicable. A clean observation grants
no cleanup authority and does not prove absence of a concurrent writer.
Tests: `test_checkout_state.py::CheckoutState` (real Git changes and index flags,
unchanged index, byte filenames, parser limits, binding change, incomplete reads,
bare/unborn/Windows, saved-reader validation).


### Partial-clone checkout observation

An explicit checkout scan observes only locally available Git data. It does not
fetch missing promisor objects, refresh remote refs or infer remote durability.
Missing required objects leave local state unknown and non-actionable. Ordinary
complete local checkouts remain observable. Proof:
`test_checkout_state.py::OfflineGitObservation`, including the actual bounded
checkout query and unchanged synthetic object-store contents.


### Local upstream graph is not remote recovery evidence

After publishing local-state results, the explicit checkout scan independently
attempts bounded offline ahead/behind counts with a persistent phase cursor.
Equal local remote-tracking commits do not establish current server contents:
remote durability stays unknown and disposal remains unauthorized. Missing,
shallow, promisor, replacement/grafted or alternate-backed history yields unknown
counts. Saved CLI/MCP coverage projects only whitelisted counts/kind/scope and
own age; source/binding changes and stale/future/bool counters refuse currentness.
A graph timeout must not erase an already saved local-state result.

Proof: `test_checkout_state.py::UpstreamGraph` and `GraphPhase`, using disposable
local file remotes including a separately changed server branch after last fetch.

### Current memory pressure independently of swap

A person or agent reads the existing memory summary and sees a separately dated
macOS pressure level alongside RAM and swap amounts. A normal pressure reading
does not erase high swap or claim reclaimed memory. Failed, unsupported, stale,
future or malformed pressure stays unknown. Extra saved fields are not projected.
Pressure cannot refresh the timestamp of missing RAM/swap measurements and never
authorizes stopping a process. The original swap finding retains its measured
swap-based wording; it is not reclassified as an OS pressure notification.
Tests: `test_memory_freshness.py::MemoryFreshness` pressure mapping, own age,
malformed fields, unsupported source and independent timestamp controls.


### Maintenance log-retention evidence

After maintenance, the person or agent reads per-scope log-retention evidence
through `full maintain status` and doctor; Health shows their incomplete/stale
warning and retains the typed result in its payload. Engine, updater and hook log
scopes have separate measured times, observed counts, completed rotations,
refusals and failures. Missing/unreadable scopes are unknown, not measured empty;
a failed scope does not suppress peers. A report older than one hour is stale
and never presented as a new attempt. Reasons are bounded codes, without file
paths, log contents or exception text. Existing rotation/list APIs retain their
contracts. No new retention policy or filesystem-lock guarantee is introduced.

Acceptance: synthetic small-file tests for empty/missing/unreadable directories,
linked files/parents/generations, copy and fsync failure, independent peer scopes,
persistence, stale/malformed receipt projection and private marker omission.
Implementation acceptance and installed observation remain pending.

### Scheduled stage diagnosis

A person or agent inspecting a stopped scheduled pass reads `tick-run.json`
`stage_progress` before assigning a cause. Only a matching run's validated stage
metadata is included; missing/stale/invalid coverage is unknown. A stage without
a terminal receipt has no inferred exit or duration. Explicit watchdog budget
refusal is `not_started`; a whole-run timeout does not prove a particular stage
timed out. The last completed `tick.json` top-level report stays available during
a new run. Coverage: `tests/test_tick_progress.py` (silent tail, supervisor stop,
write failures, stale input and exit preservation).

### Offline verification reports executed tests

A person or agent running `full check` receives an ERROR when a suite exits zero
without the minimum evidence for its declared protocol. It retains the process
exit separately, names the reason and preserves completed peer receipts. Explicit
all-skipped suites stay SKIP. A PASS marker cannot fill in missing unittest cases;
legacy assertion-line suites retain their reporting contract. Coverage:
`tests/test_runner_evidence.py` (root suite), including malformed counters and
swallowed failures. Well-formed forged reports remain outside this trusted-test
protocol's proof; a minimum is not exhaustive test coverage.

### Windows cleanup: unknown holder coverage

A Windows operator or agent asks for path cleanup. Until a supported process-holder
inventory is implemented, the collector returns unknown and path removal skips,
even if a prior plan looked free. An open FILE_SHARE_DELETE handle can permit
rename, so a successful rename is not non-use evidence. Generic tool cleanup
commands retain their separate policy and do not gain authority from this result.
Acceptance: engine `tests/test_disk_guard.py::WindowsHolderUnknown`; its native
shared-delete case is explicitly skipped outside Windows.

### POSIX cleanup: a complete listing proves a path free

An operator or agent requests cleanup, or the disk guard runs, while the disk is
nearly full. Each path class and each final removal check reads one whole `lsof`
listing and one `ps` listing, cut to the paths it may touch (the home folder and
the temporary roots, or the one target). A path that no open file, working
directory, executable or command line names is free and is removed; one that any
of them names is kept with "a process works inside it now". The listing is never
capped by name count or size the way the hint inventory is (a working Mac printed
3.5 MB and 9,647 names), so a busy machine is still decided. A listing that fails,
times out, prints a diagnostic or names nothing keeps every target, a path outside
the listing's scope is held, and Windows stays unknown. What rendered command
text cannot show (a script closed after loading, launched by a relative path from
outside the target) is accepted only because every class these tools remove is
recoverable: a worktree whose HEAD is retained, build output, caches and temporary
runs. Until 0.35.12 the system refused every path whenever argv coverage was
incomplete, which on every real machine meant always: `cleanup --apply` and the
guard removed nothing under any pressure (OBS-173, OBS-327, OBS-397). A cache
command with an unmatched nonempty busy-tool precondition still executes no child.
A path a launchd or systemd job names (its program, working folder or an argument) is
kept whatever the listing says: between its runs such a job holds nothing open.
Tests: `CompleteListing`, `ScheduledJobs`, `LossyArgvEvidence`, `LossyArgvAdmission` and
`WorktreeFinalHolders` in `tests/test_disk_guard.py` (synthetic only).

Cleanup admission: the cleanup's preliminary check takes the same complete listing
once and reuses it for 20 seconds; the final check before each removal takes a
fresh one scoped to that target. Each guard phase records the observation
(`complete`) and the time it was taken. `CheapHolderAdmission` in engine
`tests/test_disk_guard.py` verifies these outcomes; the cleanup's plan still reads
no process table.

Explicit holder observations: a requested positive-hint survey, final target probe
or nonempty cache-tool condition receives bounded capture and parser input.
Overflow, timeout, invalid encoding or read failure remains unknown; no partial
listing grants absence. The caller does not launch a second probe after its
shared budget expires. Only capture-owned process groups are stopped, never a
discovered holder. Windows does not launch a collector. The old synchronous
`/proc` entry point returns unknown without I/O. Existing positive holds and
empty tool requests retain their meanings. `test_holder_capture.py` and
`LossyArgvAdmission` cover these outcomes with owned children and synthetic
process responses; no complete lifecycle inventory or host-RAM saving is claimed.

## SSH keys on this machine (PB-163, OBS-110)

The operator asked for one way to make an SSH key, copy the part that gets pasted and send it
where it goes, without the private key ever being shown or moved. The design is
[SSH-KEYS.md](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/SSH-KEYS.md). Two readers use these paths: a person at a
terminal (`project-observatory full ssh-keys …`) and an agent (the same commands, and the read-only
MCP tool `observatory_ssh_keys`). **The rule throughout:** the private key is never printed, copied,
sent, logged or placed in an argument; Observatory reads only its clear header (the BEGIN line,
and for the OpenSSH format the cipher name and the public key stored in clear before the private
section), to say whether it has a passphrase and what its fingerprint is. They are OSS-62–67.
Tests named below are in `tests/test_ssh_keys.py`.

### OSS-62: list my keys

Trigger: "which SSH keys do I have, and where does each one work?"

Funnel:
1. `full ssh-keys list` (or `observatory_ssh_keys` for an agent).
2. One row per key: name, type and bits, `SHA256:` fingerprint, comment, passphrase
   (`yes`, `no`, `unknown`), file modes, where it is used (Host blocks of `~/.ssh/config`, loaded
   in ssh-agent) and where it is registered (GitHub, for sign-in and for signing).
3. Then the sources that could not be read, each with its reason and the command that fixes it,
   and the count of findings (OSS-66). `list` saves the inventory to `store/raw/ssh-keys.json` so
   the findings board reads the same measurement.

Failure paths:
- No `~/.ssh`, or no keys in it, and nothing went unseen: "No SSH keys yet" and the create command
  (exit 0). When something went unseen (an agent the config names that is not running, a linked
  key, a host-dependent `IdentityFile`): "No SSH keys found where Observatory could look", with the
  list of what was not seen. An empty list never hides an unread source.
- `IdentityAgent` sockets (1Password, Secretive) are read like the ssh-agent; one that cannot be
  read is in `degraded`. Every key an `IdentityFile` names is listed, at any depth and outside
  `~/.ssh` too, with `${VAR}`, `%d` and `%u` expanded as ssh does.
- ssh-agent not running, `gh` missing, not signed in, or without the read scopes: the listing
  continues and `degraded` names the source and its command (`gh auth refresh -h github.com -s
  read:public_key,read:ssh_signing_key`). Unread GitHub is "not read", never "not registered".
- A private key without its `.pub`: for the OpenSSH format the fingerprint comes from the header,
  where the public key is stored in clear; a PEM key's fingerprint stays unknown. Either way the row
  gives the command that recreates the public half (it asks for the passphrase).
- A `.pub` that is not its private key's public half is marked, and `copy` and `send` refuse it. A
  `.pub` whose first line is a private-key header is a critical finding.
- An `Include` that is a FIFO, not a regular file, or over 1 MiB is named in `degraded`, never
  waited on or read whole. Names and config values reach the terminal only through the printable
  filter.
- A key file that is a symbolic link or unreadable: the row says so; the link is not followed.
- An `IdentityFile` that names a missing file: a row of its own, marked missing.

Acceptance: no byte of a private key past its header line reaches stdout, stderr, the saved
inventory or the MCP answer; every unread source is in `degraded`.
Coverage: `Inventory`, `HeaderReadIsBounded` and `PrivateBytesNeverLeave` in `tests/test_ssh_keys.py`.
Status: implemented; product outcome unobserved.

### OSS-63: create a key

Trigger: "make me a new SSH key for this server" or "for GitHub".

Funnel:
1. `full ssh-keys create NAME [--comment TEXT] [--host ALIAS --hostname HOST [--user U] [--port N]]`.
2. In a terminal, `ssh-keygen` itself asks for the passphrase twice. Observatory never sees it.
3. On macOS, `ssh-add --apple-use-keychain` stores the passphrase in the login Keychain and loads
   the key into the running agent, so the key works without retyping it (on by default, the
   operator's "quickly"; `--no-keychain` opts out).
4. The answer prints the key's path, fingerprint and public line, and the two next commands:
   copy it (OSS-64) and send it (OSS-65). With `--host`, a Host block naming the key is appended
   to `~/.ssh/config`.

Failure paths:
- `NAME` or `NAME.pub` already exists, even as a link: refused, nothing written (exit 2). A key is
  never overwritten; "choose another name, or rotate (OSS-67)".
- An invalid name (a path, a leading dot, `.pub` at the end), or one ssh reads as something else
  (`config`, `rc`, `authorized_keys`, `known_hosts`, a `-cert` name): refused before anything runs.
- A passphrase is asked but there is no terminal (an agent's run): refused with the reason, "a
  passphrase is typed by a person". `--no-passphrase` makes an unattended key, says that the
  findings will list it, and asks for no confirmation beyond the flag.
- The Host alias is already in `~/.ssh/config`, or the config is a link: refused before the key is
  generated, so nothing is half done.
- The Keychain step fails, or the system has no Keychain: the key exists; the answer says the
  passphrase is not stored and gives the command to retry.
- `ssh-keygen` is missing: refused, naming it.

Acceptance: ed25519 only; the key is generated in a private temporary folder and linked into place
with no-replace semantics, so a race with another writer loses rather than overwrites; the private
file and a `~/.ssh` this command creates are owner-only by `osprivacy` (0600/0700, an owner-only ACL
on Windows), the public file 0644; no passphrase in any argument.
Coverage: `Create` in `tests/test_ssh_keys.py`.
Status: implemented; product outcome unobserved.

### OSS-64: copy its public part

Trigger: "copy my key so I can paste it".

Funnel:
1. `full ssh-keys copy NAME`.
2. The clipboard holds exactly one line, the public key. The terminal shows the same line and its
   fingerprint, and says where to paste it, or which `send` command does it for you.
3. `full ssh-keys show NAME` prints the same without touching the clipboard; an agent gets the
   line from `observatory_ssh_keys` with `name`.

Failure paths:
- No clipboard tool (`pbcopy` on macOS, `wl-copy` or `xclip` elsewhere): the line is printed and the
  answer says the clipboard was not set (exit 3).
- `NAME.pub` is missing: refused with the command that recreates it.
- `NAME.pub` does not hold one public key line (a private key saved under the wrong name, two
  keys, a stray header): refused, nothing printed or copied but the reason.

Acceptance: what reaches the clipboard is a line that parsed as a public key; a test plants a
private key under a `.pub` name and proves none of its bytes reach stdout, stderr, the clipboard or
an argument.
Coverage: `CopyPublic`, `PrivateBytesNeverLeave` in `tests/test_ssh_keys.py`.
Status: implemented; product outcome unobserved.

### OSS-65: send it to GitHub, a server or a cloud

Trigger: "add this key to GitHub" or "put it on that server".

Funnel, GitHub:
1. `full ssh-keys send NAME --to github [--as authentication|signing|both] [--title TEXT]`.
2. GitHub's keys are read first; a key already registered for that use is "already registered"
   and nothing is added.
3. Otherwise `gh ssh-key add - --type … --title=…`, the validated line on stdin. A receipt is written either way, and
   the answer says how to test it (`ssh -T git@github.com`).

Funnel, a server:
1. `full ssh-keys send NAME --to server:ALIAS [--verify]`, `ALIAS` being a Host of
   `~/.ssh/config` or `user@host`.
2. Through the access that already works there, a short script runs under `sh` (sent on stdin to
   `sh -s`, whatever the login shell is): it creates `~/.ssh` (0700) and `authorized_keys` (0600) if
   missing and appends the line only when the same key is not already there. Its exit code, not its
   words, says added or already.
3. `--verify` then logs in with the new key and no other: the host is resolved with `ssh -G`, the
   login runs with `-F none` and `-i NEW` only, and it passes only when ssh's log says the server
   accepted the new key's fingerprint. A Host block that still names the old key cannot make it pass.

Funnel, a cloud: `--to digitalocean` (or any other provider) is refused today with the gap named:
no Observatory door registers SSH keys there yet (OBS-112). The copied key (OSS-64) goes in the
provider's console meanwhile.

Failure paths:
- `gh` missing, not signed in, or without `admin:public_key` (`admin:ssh_signing_key` for
  signing): refused before anything is sent, with the `gh auth refresh` command.
- GitHub refuses (the key belongs to another account, validation): GitHub's own message is shown,
  with anything shaped like a credential removed, and a failure receipt is written.
- The server asks for a password while no person is at the terminal: refused, naming
  `--interactive`. Unreachable, or the remote cannot write: the remote's reason is shown.
- `--verify` fails after the key was added, or the server accepted another key: the answer says
  both, and which key the server named, exit 1. A host behind a `ProxyCommand` is not verified; the
  answer gives the command to check by hand.

Acceptance: only the public line leaves the machine; a second send is a no-op with an "already"
receipt; every outcome, failure included, is a receipt (fingerprint, where, when, who) in the
movements journal.
Coverage: `SendGithub`, `SendServer` (receipts asserted in each), `SendCloud` in `tests/test_ssh_keys.py`.
Status: GitHub and servers implemented; clouds named as a gap (OBS-112); product outcome
unobserved.

### OSS-66: findings about my keys

Trigger: the findings board, or `full ssh-keys findings`.

Rows, each with the command that fixes it: a key with no passphrase (info for a FIDO handle, which
needs the security key); a weak key (DSA, RSA under 3072 bits); a private key others can read, and a
`~/.ssh` others can read or write, both by `osprivacy` (never Windows' raw mode bits); a private key
saved as `.pub`; a key not used for a long time (estimated from the file's access time, so it says
"estimated"); a key GitHub accepts that Observatory did not find in the places it read (always info,
with no command and no action: it names where Observatory looked and what it could not read, and says
"if no machine of yours uses it, remove it in GitHub settings"; Observatory never advises deleting
a GitHub key); a key no visible place uses (not on GitHub, in no Host
block, not loaded); a private key without its public half; an `IdentityFile` that names a missing
file.

Failure paths: no inventory yet: the board shows nothing for SSH (absent is not clean) and
`ssh-keys findings` says "No inventory yet" with the `list` command. A finding never quotes a
private byte.

Acceptance: the same inventory gives the same rows; every type has a label in both languages.
Coverage: `Findings` in `tests/test_ssh_keys.py`.
Status: implemented; the dashboard section is OBS-111; product outcome unobserved.

### OSS-67: rotate a key

Trigger: a key leaked, is weak, or is old.

Funnel (specified; the one command lands with OBS-112):
1. Create the new key (OSS-63).
2. Register it everywhere the old one is registered: the same GitHub uses, the servers whose Host
   blocks name the old key (OSS-65).
3. Log in with the new key alone on each server and test GitHub.
4. Only after every check passed, remove the old registration (`gh ssh-key delete ID`; the line in
   each server's `authorized_keys`) and point the Host blocks at the new key.
5. The old private file is moved aside, never deleted by Observatory.

Each step writes a receipt. A failed check stops the rotation before any removal and says which
step to retry. Until OBS-112 the same sequence runs by hand with `create`, `send … --verify` and
`gh ssh-key delete`.
Status: specified; OBS-112.

## The person's surfaces: sharing, cloud, agents, access, home (2026-10-26)

The person who runs Observatory asked on 2026-10-26 for the surfaces a person uses, not only the
ones an agent reads: a home page that says how the whole system is, a screen of the agents they
have, a way to give an agent a token without pasting it into a chat, sharing keys with another
person, a watch for leaked keys, and a personal cloud that carries what one machine cannot. The
design and its decisions are [PERSON-SURFACES.md](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/PERSON-SURFACES.md); the build order is OBS-260 to OBS-274 and OBS-276
in [the backlog](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/backlog.md). **The rules throughout:** a credential's value
never appears in a chat, an argument, a log, a URL or a report; an agent receives a value only at
use time and only through Observatory; what a person set is never overwritten by an agent. They
are OSS-79 to OSS-93. Each is planned unless its own Status says what is built; no product outcome
is observed yet.

### OSS-79: share keys with another person

Trigger: "give my colleague access to the translation API for this project, and let me take it
back later".

Funnel:
1. Access, then Shares, then "New share". The screen asks three things in order: what to share
   (keys from the vault, or "mint a new narrow key" through a door: an OpenRouter key with a
   ceiling, a Cloudflare token with one preset, a Heroku or DigitalOcean preset), for how long
   (an end date; each sealed copy lives at most 90 days and the sender's machine seals it again before it
   ends), and a name for the recipient that only the sender sees.
2. Minting runs the door's own `issue` into a slot named for the share; the value goes from the
   provider into the vault and is never shown.
3. "Create link" returns one link, `https://CLOUD-ORIGIN/s/SHARE-ID#c=CODE&k=FPR`, and a sentence to
   send with it. The fragment stays out of HTTP requests and server logs, but the cloud's landing page
   can read it, so the link is protected by the next step, not by secrecy from the cloud. `FPR` is the
   sender's share key fingerprint, pinned only when the recipient pastes the original link into the app
   (a handoff through the landing page is protected by the code of the next step alone).
4. The share is listed as "waiting for the recipient" until it is claimed (OSS-80). A claim shows a
   six-digit code made by commit and reveal from both sides' keys and random numbers; the sender
   compares it with the recipient by a call or a message they already trust and presses "Confirm"
   only when it matches. One attempt per link: a mismatch voids it. Then "waiting for your machine" while the
   sender's node seals each value to the recipient's share key, then "active".

Failure paths:
- No personal cloud connected: the button says why and opens OSS-83; nothing is minted first.
- A chosen key is one the sender's own projects use: the screen names those consumers and says that
  taking the key back later means rotating it for them too; it recommends minting a narrow key
  instead. A door the person has not stashed an admin key for: the mint option is shown disabled
  with the stash command.
- A mint that fails at the provider leaves no share and no slot; a mint that succeeded before the
  share failed is revoked by the door (as OBS-16 does for a failed delivery).
- The link is lost before it is used: "Copy link again" while it is unclaimed; a new code makes
  the old link void.
- The six-digit codes differ, or a claim arrives that the sender did not expect: "Refuse" voids the
  link and seals nothing (someone who saw the link claimed first). This is trust on first use with a
  one-in-a-million chance per link that an impostor's code matches, and the screen says that skipping
  the comparison accepts the risk.

Acceptance: no value in the link, the page, the cloud's storage or any journal; the share's
record names keys by slot and provider only.

Built so far, from a terminal (OBS-264): `project-observatory full share create PROJECT ENV SLOT --to
LABEL --mailbox URL` registers the share at the mailbox and prints the link once; `full share status
ID` shows the six-digit code once both sides revealed; `full share confirm ID CODE` (a terminal, the
person's act) seals and sends, `full share confirm ID --without-comparing` seals and records
`confirmed: false`, and a code that differs seals nothing and spends the link. The link follows the
contract (DEC-0142): `https://MAILBOX/s/SHARE-ID#c=CODE&k=FPR`, a 16-byte code, a claim deadline of at
most 7 days. Minting through a door and the screen are OBS-265.
Coverage: `tests/test_node_peer_share.py` (the contract's vectors), `tests/test_shares.py`,
`tests/test_share_cli.py`. Status: partly built (OBS-264, the command line); the screen planned
(OBS-265); product outcome unobserved.

### OSS-80: open a share someone sent me

Trigger: a recipient opens a link a colleague sent.

Funnel:
1. The link opens a page served by the sender's personal cloud. It reads the code from the
   fragment in the browser, names the sender's label and the number of keys, and offers one button,
   "Open in Project Observatory", which hands the app the share id in its path and `c` and `k` in its fragment only, never in a query
   (contract DEC-0142 PS-4).
2. No app: the page says what Observatory is in one sentence, links the installer on
   passioncode.ai, and keeps the button: "After installing, come back here and press the button".
3. The app opens Access, Shares, "Received": the sender's label, each key's provider and purpose,
   the end date, and the notice: "These keys were shared with you. Your agents get them from
   Observatory when they need them. They are kept sealed, never in a file in the clear. Ask your
   agents to take a key from Observatory; never paste one into a chat." One button, "Accept".
4. Accepting makes a key pair for this share alone, sends its public keys with a proof made from the
   link's code (the claim), and shows a six-digit code to read to the sender; then "waiting for the
   sender's machine" until the sealed values arrive, then "ready" with the command an agent uses
   (`use_secret.py run --share LABEL NAME -- COMMAND`).

Failure paths:
- The link was already claimed by another machine: "This link was already used on another
  machine. Ask the sender for a new one"; the sender sees the same claim (OSS-81).
- The link expired or the share was revoked: the page and the app say which, with the date.
- The browser blocks the custom scheme: the page shows the link as text to copy into the app's
  "Open a share" field.
- The fragment is missing (a messenger cut it): the page says the link is incomplete and asks for
  it again; nothing is claimed.
- The sender refused the claim (the codes did not match): "The sender did not confirm this machine";
  nothing was sealed to it.
- Another app registered the same link scheme: it may open instead; nothing is lost, because the
  claim's proof and the six-digit code are checked inside Observatory.
- The person asked an agent to open the link ("Open a share", contract DEC-0147 item 4): the agent
  hands it to the node with the person's words and nothing is claimed; the share reads
  "handed-over" until the person accepts it (Accept stays theirs) or refuses it ("They differ"
  deletes the link and sends nothing), and lapses 7 days later; at most 20 wait at once. The
  sender's key is pinned only when the link is in the person's words unchanged, and Accept says
  whether it is.

Acceptance: the claim proves possession of the code without sending it to any third party; the
recipient's machine never writes a shared value to disk in the clear.

Built so far, from a terminal (OBS-264): `project-observatory full share accept LINK` (or `-` to read
it from stdin; a terminal, the person's act) prints what the sender will see and asks to accept,
makes the share's key pair and claims; `full share status ID` shows the code to read to the sender,
then the command an agent uses. A second accept of one link on one machine is refused; a link another
machine claimed first, an expired link and a sender commit that does not name the link's key
fingerprint each burn the share with that reason. A delegated (metered) agent is refused a shared key
until contract CT-187 says who pays for its use. An agent's "Open a share" (OBS-291): the node's
`share.open` tool, with the operator's decision, holds the link and claims nothing; the person accepts
it with `full share accept ID`; the agent's own MCP server does not relay the tool yet (OBS-298).
Coverage: `tests/test_shares.py`, `tests/test_share_cli.py`, `tests/test_secret_prompt.py`
(`TheOpenAShareTwin`), `tests/test_node_actions.py`. Status: partly built (OBS-264, OBS-291, the
command line and the node's tool); the landing page, the scheme and the screen planned (OBS-266);
product outcome unobserved.

### OSS-81: see how my shared keys are used, and take them back

Trigger: "is my colleague using the key, and how much? Stop it now."

Funnel:
1. Access, then Shares: one row per share with its recipient label, state, keys, uses in the last
   7 days, last use, and exports made with the export button (OSS-82), never "not exported" as a
   guarantee: any command an agent runs with a key can print it.
2. A share opens to its usage: per key, uses per day and the agent names the recipient's machine
   reported, the cloud's own count of deliveries, and the provider's spend where the door reads it
   (OpenRouter by key). Each figure says who measured it: "seen by your cloud" or "reported by their
   machine".
3. "Take back" names what it does before it runs: the cloud stops serving at once; the recipient's
   machine deletes its sealed copy on its next contact; and, because a value that reached another
   machine can have been copied, the key is revoked at the provider (a key minted for the share) or
   rotated (a key of the sender's own, with the consumers named).

Failure paths:
- The provider rotation fails: the share is still revoked at the cloud; the row says "stopped
  serving; the key is still valid at the provider" with the door's command to retry.
- The recipient's machine has not been in contact since: "stopped serving; their machine has not
  confirmed the deletion yet", never "deleted".
- The cloud is unreachable: the take-back waits in the outbox and the row says so; the provider
  revocation still runs.

Acceptance: a usage report holds counts, dates and agent names, never a value, an argument or an
output; "taken back" is shown only when the cloud refused service and the provider confirmed.

Built so far, from a terminal (OBS-264, OBS-276): `full share status ID` shows uses in 7 days and the
agents as reported by their machine, deliveries as seen by your mailbox, and exports with the button;
`full share revoke ID` hands the signed revocation to the mailbox (observed at once), the recipient's
machine deletes and returns a signed receipt at its next contact, and the share reads "taken back"
only after `full share provider-ended ID` (a terminal) records that the key was revoked or rotated at
its provider. Each report restates its day and the 35 days before it; the sender replaces each day's
count and counts each export once however many reports restate it (OBS-291, contract DEC-0147). The
provider's spend and the door's revoke from the screen are OBS-265.
Coverage: `tests/test_shares.py`, `tests/test_share_cli.py`. Status: partly built (OBS-264, OBS-276,
the command line); the screen planned (OBS-265); product outcome unobserved.

### OSS-82: export a shared value on purpose

Trigger: the recipient needs the value outside Observatory (a vendor's web form).

Funnel: Received, a key, "Export the value". The dialog says the sender will see that it was
exported and when, then copies the value to the clipboard (cleared after 60 seconds) or writes it to
a file the person chose. The share's usage shows "exported on DATE" to the sender.

Failure paths: a share the sender marked "no export" shows the button disabled with that reason; an
agent asking to export is refused: export is a person's act on the screen.

Acceptance: an export with the button is recorded before the value leaves; the record holds no
value. The screens call it "exported with the button": a value can also leave through a command an
agent runs with it, which is a use, not an export.
Coverage: none yet. Status: planned (OBS-266); product outcome unobserved.

### OSS-83: connect Observatory to my personal cloud

Trigger: "sharing needs my cloud" (OSS-79), or "keep my machines in step".

Funnel:
1. Settings, then Personal cloud: "Not connected", what the cloud is for (sharing, reaching one's
   machines, later backups), and what it holds (sealed values and their names, never a value in the
   clear).
2. "Deploy to my Cloudflare account": the Cloudflare door mints a narrow deploy token, uploads the
   engine's cloud service to the person's own account and registers this machine's public keys. The
   screen shows the address it got and "Connected".
3. When the PassionCode personal cloud is the person's, "Move to PassionCode cloud" moves the same
   service there; shares keep working because their values are sealed to machines, not to the host.

Failure paths: no Cloudflare admin key stashed: the stash command and why it is needed; a deploy
the account refuses (plan limits): the refusal verbatim and nothing half deployed; the cloud does
not answer later: shares stop at their lease (OSS-80) and Home shows "personal cloud unreachable".

Acceptance: the cloud's storage, logs and responses never hold a value or a private key.

Built so far, from a terminal (OBS-263): `project-observatory full cloud deploy` (the door's narrow
token into the vault, the Worker uploaded, its workers.dev address recorded once it answers with this
engine's code), `full cloud status` and `full cloud url`. With no admin token stashed it names the
person's step and its address before any call. The Settings page is OBS-265's.
Coverage: `tests/test_cloud_worker.py`, `tests/test_cloud_deploy.py`, `cloud/worker.test.mjs`.
Status: partly built (OBS-263, the command line); the page planned; product outcome unobserved.

### OSS-84: give an agent a token without pasting it into a chat

Trigger: an agent needs a key the vault does not hold.

Funnel:
1. The agent calls Observatory (`observatory_secret_request`: project, slot name, provider, why);
   it never asks the person in its chat.
2. The request goes to the node, which opens Observatory's own signed app through the lifecycle
   broker: a window with the phrase the person set at first use (kept in the app's own store, never in
   a slot an agent can read), which agent asks, for which project and slot, why, and one secure field:
   "Paste the token and press Save". On macOS the app's own window; on Linux and Windows the desktop
   app's window. The app writes the value into the vault itself; it never crosses a channel.
3. Save writes the value to the vault slot (as `vault.py put` does) and the agent's call returns
   "stored PROJECT/ENV/NAME", never the value. The agent then uses the key through `use_secret.py`.
   The value stays out of the chat, the transcript and the logs; a command the agent runs with it
   can still print it, and that use is journalled.

Failure paths: a window without the person's phrase is not Observatory, and the app's Settings say so;
a machine with no desktop session: the request is refused with the step for the person (run
`vault.py put` in their own shell and paste the value on its stdin), because a terminal answer could
be driven by an agent;
the node is not running: the agent receives "Observatory is not running" and no window appears; the
person presses Cancel or the window times out (10 minutes): the agent receives
"declined" or "no answer"; the person tells an agent to decline it: the agent carries their words to
the node's `secret.decline`, the window closes and the asking agent receives "declined" naming the
operator through an agent (contract DEC-0147 item 3, OBS-291); an empty or multi-line value is refused in the window; a second request
for the same slot while one is open joins it; a slot that already holds a value asks "replace?"
naming its last rotation.

Acceptance: the value is never in an argument, an environment the agent's own process can read, a
log, a notification or the request's result; no scripted dialog (`osascript`, `zenity`, `kdialog`) is
ever used; the request and its outcome are journalled by name.
Coverage: `tests/test_secret_prompt.py` (the agent named from its binding, a text claiming another
agent included; one open request per slot and five per agent per hour; the replace refusal and the
prompt's replace flag; `stored` only after a slot write, `unconfirmed`, `declined`, `no-answer` with
a late answer ignored, `cancelled` from the job, `no-client`; the node down and no desktop with no
window and no filing; no value in an argument, an environment, the queue, the journal, the runner's
log or the job's result; the review of #378: the constant-time bearer compare, the project field,
the provider's shape check, invalid arguments journalled, a request withdrawn when its job cannot
start or its runner is refused; the broker opens the enrolled app with the request id and a failed
start closes the request `no-client`; Windows `broker-unsupported`; the app's door: only a verified
signed parent reads or answers, `store` writes the slot in the app's own child with the value on
its stdin only and rotates on Replace, `decline`, `unavailable` without a phrase store; the Decline
twin `secret.decline`: no decision declines nothing, the operator's decline names who declined and
keeps the words in the audit record only, a closed request answered as it is),
`tests/test_node_lifecycle_broker.py` (the prompt's own internal caller, one operation per request),
`tests/test_mcp_wire.py` (the tool is published), and in the Mac app
`macos/Tests/ObservatoryCoreTests/SecretPromptTests.swift` (the request as the door answers it, the
window's only inputs, the phrase, no window and `no-client` without a phrase store, Save and Cancel
through the bridge).
Status: partly built (OBS-260): the server half landed in #378; the Mac window, the person's phrase
(data protection keychain, the app's access group, applied with a provisioning profile), the app's
door and the broker open shipped with pull request #382. The Windows desktop window waits for
the Windows lifecycle broker (OBS-277); Linux stays `no-client` with the `vault.py put` step. The
released app is not yet signed with the provisioning profile the phrase's keychain group needs (the
release will fetch or create it through the App Store Connect API), and the window takes a key only
through an engine bundled inside the signed app, which no release carries yet: today every request is
answered `no-client`, reason `engine-untrusted`, with the `vault.py put` step; product outcome
unobserved.

### OSS-85: see which agents I have, and connect Switchboard

Trigger: "which agents do I have, and can I manage them from here?"

Funnel: Agents opens on "Available": the coding agents installed on this machine (from the harness
scan), the Fabric services running here (from the services folder, OSS-72) and, for each, whether it
answers, its version and where it runs. A Switchboard card says "connected", "installed but not
running" or "not installed" with the one step that installs and connects it and what it adds
(accounts, limits, handing work between agents). The workflows view of today (OSS-24 to OSS-27)
moves to a tab of its own.

Failure paths: no harness scan yet: "not scanned yet" with the command, never "no agents"; a
service whose descriptor is unreadable is listed as such.

Acceptance: absent and not measured are different states on the screen.
Coverage: none yet. Status: planned (OBS-267); product outcome unobserved.

### OSS-86: see what one agent did

Trigger: "what did this agent touch this week, and did it ask for anything?"

Funnel: an agent's page: branches it created, files it changed (per repository, with counts), the
permissions it asked for and the answers, errors by kind, keys it used through Observatory (OSS-91),
and its sessions, newest first, each over a stated period with its freshness.

Failure paths: a harness whose transcripts the scan cannot read: the section says "not recorded for
this agent" and why; a branch since deleted is shown as deleted, not dropped.

Acceptance: file paths are shown relative to their repository; no transcript text is shown.
Coverage: none yet. Status: planned (OBS-268); product outcome unobserved.

### OSS-87: keep agents' logs without filling the disk

Trigger: logs grow; the person wants daily figures now and the detail on demand.

Funnel: Agents, then "Logs": fresh raw records for the hot window (30 days by default), daily
aggregates kept long, and monthly archives listed with their size; "Restore for analysis" opens a
month into a separate, read-only store and says where.

Failure paths: an archive that fails its checksum is named and never restored over anything; the
backup passphrase is needed to open an archive and its absence is stated.

Acceptance: archiving never deletes a raw record before its archive is written and verified;
retention is set in `retention.json`, never by an agent.
Coverage: none yet. Status: planned (OBS-274); product outcome unobserved.

### OSS-88: see the whole system at a glance, and the three things that need me first

Trigger: the person opens Observatory.

Funnel: Home shows, top to bottom: the state of the system in tiles (sites up and down, apps up and
down, disk and memory, keys health, agents working, personal cloud), each with its period and
freshness; the three findings that need the person first, each with its action, and "All problems
(N)" to the findings page; projects at a glance grouped by organization and label; recent activity.

Failure paths: a source not measured shows "not measured" on its tile, never green; nothing needs
the person: "Nothing needs you" with the time of the last check; a fresh install shows what to
connect first instead of empty tiles.

Acceptance: the home page shows at most three findings; every tile links to its page.
Coverage: `tests/test_home_page.py` (`test_six_tiles_in_order_each_with_its_time_and_measured_ones_link`,
`test_a_source_that_did_not_run_says_not_measured_never_zero_or_green`,
`test_a_failed_source_keeps_its_tile_with_the_reason_and_the_rest_render`,
`test_three_kinds_ranked_one_row_per_kind_with_its_count`,
`test_each_problem_row_shows_its_action`, `test_a_stale_guard_never_turns_a_fresh_low_disk_green`,
`test_a_title_naming_a_placeholder_never_becomes_a_live_tag_on_any_page`,
`test_nothing_open_says_nothing_needs_you_with_the_time_of_the_check`,
`test_grouped_by_organization_with_need_and_last_activity`,
`test_a_fresh_install_shows_what_to_connect_and_no_tiles`). Status: implemented (OBS-261); grouping by
label waits for OBS-270 and the cloud tile for OBS-263; product outcome unobserved.

### OSS-89: name and group my things; agents suggest, never overwrite

Trigger: "group these projects under the client they belong to", or an agent labels a project it
created.

Funnel: any project, key, site or agent has "Labels". A label the person sets is theirs. An agent
may set a label where the person set none; it shows as "suggested by AGENT", and the person keeps,
edits or removes it. An agent's change to a label the person set becomes a proposal in the review
queue, applied only when the person accepts it.

Failure paths: two agents suggest different labels: both are shown, neither wins; a label with a
credential-shaped value is refused.

Acceptance: no write path lets an agent replace a label whose owner is the person.
Coverage: none yet. Status: planned (OBS-270); product outcome unobserved.

### OSS-90: see my Cloudflare and DigitalOcean resources beside Heroku

Trigger: "what runs where, and what does it cost?"

Funnel: Infrastructure lists Heroku, DigitalOcean and Cloudflare, each a page: what runs (apps,
droplets, databases; Workers, Pages, R2, D1, zones), its state, its account, its cost where the
provider reports one, and the findings that concern it.

Failure paths: a provider with no read key: the page says how to connect it with the door's
read-only preset; a provider that refused: the refusal and the time.

Acceptance: read-only keys only; nothing on these pages changes a resource.
Coverage: `tests/test_infra_pages.py` (Door, Collector, Registry, Pages, Chain), `tests/test_workspace.py` `test_complete_local_workflow_and_fourteen_pages`. Status: shipped (pull request #376, OBS-271; DigitalOcean by OBS-235); product outcome unobserved.

### OSS-91: check that agents handled access correctly

Trigger: "did my agents use keys the way they should?"

Funnel: Access, then "Use": every use of a key by an agent from the secret-use journal, grouped by
agent and key: through the vault, refused, failed, or by a fallback the rules forbid (a `.env`), and
the findings about keys seen outside their home.

Failure paths: a journal that cannot be read is named; an agent that used a key outside Observatory
is visible only where the leak scan saw it, and the screen says so.

Acceptance: the screen shows names, counts and dates, never a value or a command line.
Coverage: none yet. Status: planned (OBS-269); product outcome unobserved.

### OSS-92: know whether my keys appear in public, without sending them anywhere

Trigger: "has any of my keys leaked?"

Funnel: Access, then "Leak watch": per key, the last check, the sources checked and the result.
Providers' own signals (a key the provider disabled or reported) and alerts on the person's own
repositories are read by default; public code search is off until the person turns it on, and the
panel says exactly what leaves the machine (eight characters of each eligible key, the same eight
every time). A confirmed sighting is a critical finding with the rotate command.

Failure paths: a key too short for a safe fragment is checked by provider signals only, and says
so; a source that refused is named with its time.

Acceptance: a full key never leaves the machine; a candidate found in public is compared on this
machine and never stored.
Coverage: none yet. Status: planned (OBS-272); product outcome unobserved.

### OSS-93: be told when something I connected breaks

Trigger: a site goes down, a key stops working, the disk fills.

Funnel: the watcher checks connected resources, keys, disk and memory on the maintenance schedule;
a change that needs the person becomes a notification once, with the action; Home's "hot" line names
the newest.

Failure paths: notifications switched off: Home still shows it; a check that could not run is "not
checked", never "fine".

Acceptance: one notification per change, never a repeat for the same state.
Coverage: none yet. Status: planned (OBS-273); product outcome unobserved.

## The node's ceremony surface (OBS-197, 2026-10-27)

### OSS-94: add this machine to my network, and know what happened

Trigger: "pair this machine with my other one" (the Mac app: File, then Pair this machine; the Windows and Linux
app: the same item in its File menu).

Funnel: the pairing window draws the one screen the engine answers for this machine's pairing session
(`project-observatory full assistant ceremony`, `node/ceremony.py`), read through the ceremony checks of OBS-133.
Not paired: Start pairing commits to this machine's device keys (created when it has none) and a fresh nonce. While
the hub does not run pairing, the window says "Waiting for the hub": nothing was sent and no machine was added. With
a hub: waiting for the other machine and the seconds left; the six-digit code and the network fingerprint once both
reveals opened their commitments, with The codes match and They differ; on the trusted machine, the passkey prompt
names the new machine and each of its key fingerprints before anything is signed; on the new machine, the wait for
the revision that lists it; then Pairing complete when that revision lists the new machine with exactly its keys.

Failure paths: They differ, a message that does not open its commitment, another network's revision, or a revision
that does not list what was revealed: Pairing stopped with its sentence and the check's own reason; ten minutes
without both reveals: The pairing expired; Sign with passkey stays disabled while the hub serves no passkey page, and
pressing it anyway is refused (`hub-not-ready`). Start again drops the ended session.

Acceptance: the window decides no ceremony rule, never shows progress the engine did not report, and never shows a
code over reveals that did not open their commitments.
Coverage: `tests/test_node_ceremony.py` (`ScreensFromTheVectors` over the contract's `pairing.json`,
`ScreensAfterTheCodes`, `Steps`, `OverTheAppProtocol`); the Mac app `macos/Tests/ObservatoryCoreTests/CeremonyTests.swift`;
the Windows and Linux app `desktop/src-tauri/src/main.rs` `the_pairing_window_sends_only_the_ceremonys_steps` and
`tests/test_desktop_app.py`. Status: built against the contract fixtures (OBS-197); the live ceremony waits for the
hub's side; product outcome unobserved.

## Doctor's verdict and the move checklist for agents (OBS-349 part 1, 2026-11-06)

### OSS-99: know in one answer whether this setup needs me, and whether my keys would follow me

Trigger: "is my Observatory clean?", or the move runbook's proof step "`full doctor` clean".

Funnel: `full doctor` ends with `attention` (each row: area, code, severity, sentence, step) and `clean`. A row is a
`warning` unless it is news or a choice the person made (`update-available`, `auto-off`, `deferred`,
`schedule-off`, decision WA-5), so a deliberate setting never keeps doctor from reading clean; a dead, interrupted
or stuck tick is a warning. The verdict does not read the organizations, meter, session servers, analytics or review
sections yet; they keep their own fields. A secret store the
settings name outside the workspace is named under `backups.secret_store`, with a warning when no recent encrypted
snapshot carried it, when the folder is missing here, or when the environment points the vault elsewhere; the
same rows appear on Health under "maintenance", in the reader's language, with the step as a command. An agent
reads the move checklist with `observatory_machine` and `section: "move"`, the answer `full machine-move status
--json` gives.

Failure paths: a carry record that does not read cannot prove the store was carried, so the warning stays; a release
whose snapshots do not carry the store says to copy it by hand rather than sending the person to take a snapshot that
would not carry it either; a server built before this release refuses `section: "move"` through its schema, and the
agent runs `full machine-move status --json`.

Acceptance: doctor exits 0 whatever the verdict; every existing doctor key and backup sentence is unchanged; the move
section writes nothing and makes no machine id.
Coverage: `tests/test_workspace.py` (`test_doctor_attention_unions_maintenance_items_and_coverage_by_code`,
`test_doctor_attention_takes_each_code_once_from_its_first_area`, `test_doctor_is_clean_with_only_info_items`,
`test_a_dead_or_stuck_tick_is_a_warning_and_a_fresh_one_is_not`);
`tests/test_backup_vault.py` Doctor (`test_an_external_store_not_carried_is_a_coded_warning`,
`test_without_a_carrier_the_step_says_the_store_is_copied_by_hand`, `test_a_carried_store_is_not`,
`test_a_missing_configured_store_is_named_with_its_configure_step`, `test_nested_settings_name_the_outer_store_once`,
`test_existing_backup_warnings_keep_their_text_and_gain_codes`, `test_the_environment_override_is_named`,
`test_observatory_secrets_moves_nothing_while_secret_store_is_set`,
`test_observatory_secrets_moves_the_vault_when_only_secrets_is_set`, `test_a_vault_dir_elsewhere_is_named`,
`test_a_store_only_the_environment_names_is_reported_as_external`);
`tests/test_machine_backup.py` Schedule `test_maintenance_status_joins_backup_items`; `tests/test_mcp_wire.py`
(the move section is the CLI's checklist and writes nothing); `tests/test_dashboard_render.py`
`test_the_health_page_names_versions_kinds_and_dollars` (the secret store row in Russian). Status: built (OBS-349
part 1); the restore and CLI sign-in items wait for parts 2 and 3; product outcome unobserved.

## The secret store travels with the encrypted snapshot (OBS-350 part 1, 2026-11-06)

### OSS-100: my keys kept outside the workspace reach my other machine in the same backup, except the ones I keep here

Trigger: the vault lives outside the workspace (`sources.secret_store`), and I am about to set up a second machine
from this one's backups; or a slot (a root seed, an administrator token) must never leave this machine.

Funnel: the daily encrypted snapshot, or `full workspace-backup --writers-stopped` with a passphrase, carries the
store in `external/secret_store/` beside the home, under the vault's own lock, listed by relative path, size, mode and
digest; doctor's `backups.secret_store.last_carry` reads `carried` and the not-carried warning goes away. `full
vault-digests keep-here PROJECT ENV NAME --why "..."` keeps one slot here: the snapshot names it
`this-machine-only` and still reads as carried, and `full vault-digests` shows `only here on purpose` here and
`only on <machine> on purpose` on the other machine, neither of which is a difference. `unkeep-here` lets the next
snapshot carry it. The machine bundle skips the store and says it travels in the workspace snapshot. On the other
machine `full restore SNAPSHOT` writes the store where the restored settings name it (under this machine's home after
the home rewrite), before the home, and answers `secret_store.state` `written`; a folder already holding the same
files reads `same`. `--secret-store keep|merge|replace|skip` chooses what happens to a different store already there;
`--secret-store-only` writes the stores alone into the folders this workspace's settings name. The first machine's
digest manifest becomes a peer, so `full vault-digests` compares against it at once. A restore check that must not touch the
live store adds `--secret-store-verify-into DIR`: each store is written into `DIR/<name>/`, read back against the
snapshot's SHA-256 manifest and answered `verified` with counts, the folder the settings name is never written or
locked, and the copy is left in `DIR` for the caller to remove.

Failure paths: a plaintext `--output` snapshot, no passphrase, a failed export, the pre-update snapshot, a store over
256 MB, one that changed during two copies, or one that cannot be read is never carried, and the snapshot, the carry
record and doctor name the reason; the snapshot itself never fails because of the store. An interrupt inside
the encryption, or a run killed there, never leaves the store in the plaintext copy: it is removed at once, or
by the next snapshot. A kept slot's rotation archives stay with it, and a slot whose metadata does not read
stays here (`unreadable`). A link leaving the store,
a socket and lock files stay behind; a link inside it is recorded, never followed or written. A damaged or unlisted
carried file is refused at verification (`secret-store-manifest-mismatch`). Marking a slot this machine does not
hold, or clearing a mark that is not there, exits 2. On restore, a different store already in the folder is refused
under the default `auto` with counts and the policy flags (`secret-store-not-empty`, exit 2, nothing written); a
folder the settings do not name, or one inside the new home, is refused the same way (`secret-store-unresolved`,
`secret-store-target-inside-home`) and the message names the `configure sources` and `--secret-store-only` steps; a
store that cannot be written is left as it was and the home is not restored (`secret-store-write-failed`, exit 1);
`replace` moves the old store aside and never deletes it, and no folder is ever deleted; a target in the home
itself or a sensitive folder (`~/.ssh` and the like) is refused (`secret-store-target-refused`); a snapshot from
another machine or home writes only where `--secret-store-target` or this machine's settings confirm it
(`secret-store-target-unconfirmed`); `--json` gives each as `{"error": {"code", "message"}}`. A
verification into a folder that is not empty, or inside the new home, the snapshot or a secret store's folder, is
refused (`secret-store-verify-into-not-empty`, `secret-store-verify-into-refused`), as is one beside
`--secret-store-target` or another policy (`secret-store-verify-into-conflict`); a copy that does not match is
removed (`secret-store-verify-failed`, exit 1).
`full init` on an empty home keeps a surviving store, and an update's rollback never touches it; a store init
skipped (a folder not confirmed, refused, busy or unwritable) is in its answer with the exact `next` command, and
doctor keeps `backups-secret-store-not-restored` with that command until a store is in that folder again, however
it got there; a skip chosen with `--secret-store skip` raises no warning.

Acceptance: no value reaches a manifest, an answer, a record or a command's output; a store inside the workspace
travels in `data/` exactly as before; an older engine restores the home and ignores `external/`.
Coverage: `tests/test_workspace_upgrade.py` ExternalSecretStore (`test_an_external_store_is_carried_into_an_encrypted_snapshot`,
`test_lock_files_and_temporaries_are_not_carried`, `test_a_plaintext_output_never_carries_the_store`,
`test_without_a_passphrase_the_store_stays_where_it_is`, `test_a_failed_export_keeps_no_store_in_the_plaintext_copy`,
`test_the_vault_lock_is_held_while_the_store_is_copied`, `test_a_store_that_changes_during_the_copy_is_named_not_carried`,
`test_one_change_is_retried_and_the_second_pass_carries`, `test_a_link_inside_the_store_is_recorded_and_one_outside_is_named`,
`test_a_store_over_the_cap_is_named_too_large`, `test_verify_refuses_an_unlisted_or_changed_store_file`,
`test_a_restore_by_this_release_restores_the_home_and_leaves_the_store_alone`,
`test_a_store_inside_the_workspace_travels_in_data_as_before`, `test_the_update_snapshot_never_carries_the_store`,
`test_the_environment_override_is_named_in_the_manifest`,
`test_the_daily_maintenance_record_says_whether_the_store_was_carried`,
`test_an_interrupt_inside_the_encryption_leaves_no_store_in_plaintext`,
`test_a_snapshot_first_sweeps_stores_and_dead_stages_from_the_workspace_backups`,
`test_a_failed_carry_record_is_said_not_swallowed`, `test_the_carry_record_counts_what_stayed_behind`,
`test_a_setting_that_holds_the_workspace_is_never_copied_into_its_own_snapshot`,
`test_an_undeletable_leftover_is_named_and_the_snapshot_goes_on`,
`test_a_failing_strip_never_hides_the_original_interrupt`, `test_the_sweep_touches_only_snapshot_folders`);
`tests/test_backup_vault.py`
ThisMachineOnly (`test_a_rotated_kept_slot_leaves_its_archives_behind_too`,
`test_a_mark_that_does_not_read_keeps_the_slot_here` among them);
`tests/test_vault_digests.py` ThisMachineOnly; `tests/test_machine_backup.py` ExternalSecretStore;
`tests/test_workspace_upgrade.py` RestoreSecretStore (`test_restore_writes_the_store_where_the_restored_settings_point`,
`test_auto_refuses_a_different_existing_store_before_writing_anything`, `test_keep_merge_replace_and_skip_follow_the_table`,
`test_the_store_is_written_before_the_home_and_a_failed_store_write_leaves_both_unchanged`,
`test_secret_store_only_writes_the_store_into_the_current_settings_target`,
`test_the_first_machines_digest_manifest_becomes_a_peer_on_a_second_machine`,
`test_init_on_an_empty_home_keeps_a_surviving_store`,
`test_the_command_line_takes_the_policy_and_answers_a_refusal_as_json` among them). Status: built (OBS-350 parts 1
and 2); product outcome unobserved.

### OSS-101: a job moved into an account nobody logs in to runs there, and comes back after a reboot

Trigger: on a Mac, a second OS account (a "space") received this machine's jobs through a second-machine restore,
and nobody ever signs in to it graphically, so it has no `gui/<uid>` launchd domain and launchd refuses its agents.

Funnel: in that account, `full machine-move cutover LABEL` runs its checks as always, finds `launchctl print gui/<uid>`
answering "could not find domain" (exit 112) or, once anything runs as the account, "Domain does not support
specified action" (exit 125, since 0.35.1), moves the plist into `~/.local/share/project-observatory/background/LaunchAgents/`
and answers `state: waiting-for-root` with the one command an administrator runs:
`sudo /usr/bin/python3 -I /usr/local/libexec/observatory-space-daemons apply --user ACCOUNT --label LABEL` (after
installing that root-owned copy once from the release, downloaded with `gh release download` and verified with
`shasum -a 256 -c` and the signature against the pinned release key (`gpg --status-fd 1 --verify` matched to its
`VALIDSIG` fingerprint, or `release_signature.verify` from the administrator's own install) in the administrator's
own account, never copied from the space's folders). The job then runs as a system daemon with `UserName`
set to the account, never as root, and starts again after every reboot with nobody logged in. A second `cutover`
answers `state: complete`; `machine-move show` lists it under `background` as `loaded`.

Failure paths: `cutover` refuses, naming each problem and moving nothing, a plist the administrator's tool would
refuse, and a job defined both in `~/Library/LaunchAgents` and in staging; a plist with a key that would act with root's reach (`UserName`, `Sockets`, `MachServices`, ...), a
program or a path someone else could change, a label under `com.apple.`, a symlinked, oversized or group-writable
plist, or a folder others can write is refused and named by `plan` and `apply`, never partly installed; a file in
`/Library/LaunchDaemons` the tool did not write, a label someone else loaded or any other daemon plist carries, and
another account's daemon are never replaced; `DYLD_*` variables, `KeepAlive.OtherJobEnabled`, a `PathState` path
outside the home, an integer outside 0..2147483647 and a daemon that would not write as a plist are refused; one
definition's failure is `failed` and the others go on; a crash between the registry and the plist leaves an entry
`status` shows as `missing`; a label already loaded as an agent in the account's GUI domain is refused (`loaded-in-gui`, one copy only);
the plist's own log paths are ignored (`logs-moved`) and the logs go to root's
`/Library/Logs/ProjectObservatory/ACCOUNT/` (0700, read ACL for the account), the two files created ahead and owned by
the account (0600); a link, a hard link, another account's file or a folder that is not root's is refused; a failed load puts back what was there. `apply` and `remove` refuse without root, outside Python's isolated mode
(`not-isolated`) and from a copy that is not root's alone. `leave` refuses while root's daemon runs and names the `remove` command. An account with a custom
`XDG_DATA_HOME` is refused before anything moves (part 1).

Since space daemons 1.1.0: a program under a prefix owned by the host's admin account (Homebrew) passes once the
administrator lists that account and its `admin` group in the root-owned trust file (`trust set`: only wheel or admin,
only current members of admin, rechecked at every read), never the space account itself, and a trust file that is not root's alone is ignored with a warning; `Umask` and
`SoftResourceLimits` are kept, `AssociatedBundleIdentifiers` stripped, a `WorkingDirectory` of `/` made the home, and an
XML comment holding `--` parsed as launchd does, with nothing inside a comment becoming a key
(`tests/test_space_daemons.py` Trusting, Rules, Reading; `tests/test_machine_backup.py` HeadlessSpace
`test_cutover_parses_and_trusts_as_root_will`).

Since space daemons 1.2.0 (OBS-475, OBS-395 part 2): `remove` answers only once launchd has let the label go (a stop
that does not finish in its bound is `still_loaded` and keeps the file and the entry), so an `apply` right after it
loads; `replace --label LABEL` checks the new definition, stops the old daemon, waits, loads the new one and on a
refusal puts the previous one back and loads it (`put_back`); `apply` over its own loaded daemon replaces a changed one
the same way and leaves an unchanged one running (`unchanged`); every answer lists its `steps`
(`tests/test_space_daemons.py` Replacing).

Acceptance: an account with a GUI session moves its jobs exactly as before; the engine never writes a system file and
only reads root's registry. Coverage: `tests/test_space_daemons.py` (Rules, Reading, Accounts, Applying, CommandLine,
Pure); `tests/test_machine_backup.py` HeadlessSpace
(`test_without_a_gui_session_cutover_places_the_job_and_waits_for_root`,
`test_once_root_loaded_it_cutover_and_show_say_it_runs`, `test_a_system_daemon_of_another_account_is_not_this_ones`,
`test_with_a_gui_session_cutover_is_unchanged`, `test_a_custom_xdg_data_home_is_refused_before_anything_moves`,
`test_leave_is_refused_while_registered_even_unloaded_and_in_a_sandbox`,
`test_cutover_refuses_what_root_would_refuse_and_moves_nothing`, `test_a_homebrew_label_moves_and_is_named_after_its_label`,
`test_a_job_defined_twice_is_refused_with_both_files_named`, `test_other_answers_of_print_gui_keep_the_agent_path`,
`test_leave_is_refused_while_root_runs_it_then_works_from_the_background_folder`) and MachineMove. Status: built
(OBS-361 part 1); the tick, server and maintenance of such an account, and a passphrase store with no login
keychain, are later parts; product outcome unobserved.

## Personal to organization: onboarding, projects and keys (2026-11-08)

The operator asked on 2026-11-08 how a person who connects to an organization is set up, and how a personal install
converts into an organizational one: the person chooses which projects and which keys go to the organization and
keeps the rest personal, and the groupings of GitHub organizations, DigitalOcean teams and Observatory's own
organizations must stop reading as one. The vocabulary (account, organization, context, space), the business rules
ORG-R01 to ORG-R30, the screens ORG-W1 to ORG-W9 with their owners and the gap map are in
[organization-onboarding.md](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/ux/organization-onboarding.md); the build rows are OBS-411 to OBS-420, OBS-425 and OBS-426 with
OBS-200, OBS-232 to OBS-236 and OBS-359 to OBS-372 on [the backlog](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/backlog.md). **The rules
throughout:** nothing enters a joined organization without a decision; rules propose and the person decides; the
organization receives only what the contract allows (an adaptation item Fabric sends, a credential request), never a
value and never a personal item; a key changes context only by rotation; the space that becomes the organization's
device is chosen before anything enrolls; every step has a CLI command and an MCP tool, and an agent does it on the
person's word. They are OSS-102 to OSS-109. Each is planned (Status: draft); no product outcome is observed yet.

Actor in every scenario: the person who runs Observatory on their own computer (an engineer or not), or their
coding agent acting on their words; in OSS-104 and OSS-108 also the organization's administrator in the hub.

### OSS-102: see whose everything is, before any organization

Trigger: a new person installs Observatory (the launcher's `passioncode setup`, or `full init`) and opens it; or
an existing person asks "what is a GitHub organization here, and what is an Observatory organization?".

Steps:
1. The first scan reads what the person connected: GitHub owners (`gh`), DigitalOcean teams (stashed tokens and
   `doctl` contexts), Cloudflare accounts, Heroku teams and OpenRouter accounts the doors hold.
2. Organizations (ORG-W1) shows Personal (default) with each account under it and its projects and keys counted,
   grouped Organization, then Account, then Project, and Not ours for clones of other people's code. Help ("What
   these words mean") carries the four words.
3. Accounts seen but not connected (a `doctl` context with no stashed token, a GitHub owner `ownership.json` does
   not declare) are listed under "Needs a decision" with [Connect] (the door's stash command) or [Ignore].
4. Projects and Home can group by organization, then account (OBS-418), instead of today's single GitHub owner or
   "multi".
5. [Add a local organization] names one (a client, an employer without a server) and opens ORG-W2 for its accounts.

States: empty (no provider connected: Personal with "No accounts connected yet" and the connect command of each door;
never a row of zeros); loading (groups as skeletons; a provider still scanning reads "reading"); partial (a provider
that failed keeps its rows with "not read since TIME" and its reason, e.g. a 401 for one DigitalOcean team); error
(`organizations.json` does not read: the line and the fix; projects keep their last answer and none moves into a
joined organization, ORG-R01); offline (last scan with its time; suggestions from the hub disabled); success (every
account under one organization, "Nothing needs a decision").

Edge cases: one GitHub user account holds both personal repositories and repositories of an organization's product
(shown as a shared account, decided per project, ORG-R04); the same DigitalOcean team reached by two `doctl`
contexts (one account, two ways in); an OpenRouter account whose keys serve both personal and organization projects
(shared); an account two local organizations both match (a conflict, assigned to neither, as `organizations.py` does
today); a workspace with no `organizations.json` (today: no organization field at all; the page shows Personal
holding every account, and writes no file until the person adds a rule); an existing default entry not named
Personal (shown as Personal with its own label beneath; its key is not renamed).

Acceptance: every account appears exactly once, under one organization or under "Needs a decision"; a GitHub
organization is never shown as an Observatory organization; nothing is sent anywhere; the grouping is the same on
ORG-W1, Projects and Home.
Coverage: the data under ORG-W1, `full org accounts` and `observatory_org_accounts` (OBS-411): `Views` in
`tests/test_organizations.py` (every account exactly once, Not ours, needs a decision with its command, the empty,
unconfigured and degraded states, no value in the answer) and the wire call in `tests/test_mcp_wire.py`. Not built:
the page and its help (OBS-411), joined organizations (OBS-413), the grouping on Projects and Home (OBS-418).
Status: draft; product outcome unobserved.

### OSS-103: say which organization each provider account belongs to

Trigger: "the acme-prod DigitalOcean team and the Acme Cloudflare account are Acme's", or a row of "Needs a decision"
on ORG-W1.

Steps:
1. ORG-W2 opens on one account: what it holds here (projects, apps, zones, keys) and who suggests a mapping: the
   organization's declared provider accounts read from the hub (names and owners only, never keys), a repository
   owner rule, or nobody.
2. The person picks: this organization, Personal, or decide per project (a shared account).
3. The rule is saved in `organizations.json` under the account's kind (`github_owners`, `digitalocean_teams`,
   `cloudflare_accounts`, `heroku_teams`, `openrouter_accounts`; `repository_owners` and `products` stay valid),
   with `by` (the person, or an agent with the person's words).
4. The screen says what the rule proposes ("12 projects and 3 keys will be proposed for Acme; nothing is sent") and
   ORG-W1 regroups. For a joined organization the projects read `proposed` until chosen (OSS-105): the session-start
   line and the analytics findings name none of Acme's accounts for them yet (ORG-R28).

States: empty (no account needs a decision: "Every account has an organization"); loading (counts as skeletons);
conflict (two organizations claim the account: both named with their rules, the person picks one or per project,
ORG-R04); error (the file cannot be written: nothing changed, the reason); offline (hub suggestions unavailable, rules
by hand still work); success ("Rule saved").

Edge cases: an account the hub declares for Acme while a local rule says Personal (the person's rule wins and the
difference is shown); a project whose repository is in one account and whose deploy is in an account of another
organization, or of Personal (ambiguous by the project rule ORG-R26, decided by the person); a rule removed later (its
proposals are withdrawn; anything already chosen stays until taken back, OSS-108).

Acceptance: a rule never moves anything (ORG-R02); every proposal names its rule; a person's rule is never
overwritten by an agent's or the hub's suggestion; a rule-mapped project not chosen gets no account of the joined
organization in the session-start line.
Coverage: the rules of step 3 and the readers of step 4 (OBS-412 part 2): `RulesPart2` in
`tests/test_organizations.py` (`github_owners`, `openrouter_accounts`, an unknown rule named with the known ones, a
source that is not a decision naming no account in the session-start line, the project view or the analytics
findings, a conflict named on the board). Not built: saving a rule from the screen or `full org rules`, the hub's
suggestions (OBS-412), `proposed` for a joined organization (OBS-413). Status: draft; product outcome unobserved.

### OSS-104: join an organization

Trigger: the organization's administrator invites the person in the hub; the person says "join Acme".

Steps:
1. The person redeems the invitation in the hub (People: one link, single use, 72 hours, sent through the
   organization's own mail); it registers their passkey in the hub. It enrolls no computer.
2. In Fabric, the person admits Acme into their estate: Fabric shows Acme's root fingerprint, the person compares it
   with the one in the administrator's invitation (the administrator's own Fabric shows it, the invitation message
   carries it), and only a match lets their passkey sign the pin (OM-2, Fabric ADR-0178 point 6). A mismatch, or an
   invitation without a fingerprint, pins nothing and says why. Today the hub's invitation is a link only and the
   administrator's Fabric does not show the fingerprint yet (gap map rows for Fabric and the hub), so the join waits
   here until both exist.
3. The person acknowledges Acme's notice in the hub's signed-in page (DEC-0052). Observatory never does (ORG-R18).
4. The person chooses where Acme's work lives (OSS-107) before anything enrolls (ORG-R29). In the recommended
   separate-space layout the steps below run in the new space; the current personal space never enrolls.
5. The administrator prepares a computer token for that space in the hub's Computers page (an administrator's step,
   ORG-R23). Observatory's Join (ORG-W3, or `full org join --org ID` with the token on stdin) puts the token in that
   space's vault, enrolls (`full org enroll`, OBS-161) and shows: enrolled, admitted in the estate (from the estate
   revision it applied, ORG-R19), the notice version and whether it is acknowledged.
6. The local organization of the same name, if any, is linked to this enrollment (OBS-413); ORG-W1 shows Acme as
   joined and names the device. Next: OSS-105.

States: empty (no token: where it comes from and who makes it); no fingerprint (the invitation carries none, so
nothing is pinned and the join cannot complete: "ask your administrator for the fingerprint their Fabric shows");
not admitted (no applied estate revision pins Acme: "admit Acme in Fabric first"; Observatory enrolls nothing until it
does, ORG-R19); checking (the server asked); refused (a computer token expired, used, for another
organization, or a fleet token: the server's words and "ask your administrator for a new computer token"); offline
("needs the network; nothing saved"); waiting for the notice (enrolled; "Acme keeps nothing about your work until you
acknowledge its notice. It already knows this computer is enrolled." Draft string, for copywriting); success
(enrolled, admitted, acknowledged).

Edge cases: a computer token pasted into a second OS account after it enrolled the first (refused: one computer token,
one device, ORG-R21); a managed computer enrolled by a fleet token (the administrator's path; the person sees it
enrolled and binds by signing in); a person already in another organization on this computer (joined beside it; the
notice may say two organizations on one computer can be linked); a newer notice version later (collection of the
kinds it changes waits until it is acknowledged); a clipboard shared across devices (the screen asks to turn shared
clipboard off first, as the hub's runbook does); a machine enrolled since 0.27.0 with `full org enroll` (kept, read as
the "both in one space" layout once linked; see "Machines enrolled before this design" in the design).

Acceptance: the token never appears in a chat, an argument, a log or the screen after it is read; no space enrolls
before the layout is chosen; the personal space never enrolls in the separate-space layout; the notice is never
acknowledged by Observatory or by an agent; the person's other organizations are untouched.
Coverage: none yet (OBS-414, OBS-200, OBS-413). The enrollment step itself: `tests/test_node_org.py` (OSS-74).
Status: draft; product outcome unobserved.

### OSS-105: choose which projects go to the organization

Trigger: right after joining, or "move my Acme projects into Acme", or a finding "1 new project may belong to Acme".

Steps:
1. ORG-W5, in the space that holds the projects and naming the space it serves, lists every project in three groups:
   proposed by a confirmed rule (selected by default, ORG-R15), needs you (shared accounts, mixed evidence by ORG-R26,
   no rule), stays personal (collapsed, counted). Project identities come from Fabric's project registry and
   Observatory's registry.
2. The person clears or adds projects one by one, or uses [All] and [None] on a group; each row names its rule. A
   label for Acme starts empty: Acme reads a name only if the person types one (ORG-R03).
3. When this space becomes the organization's (OSS-107), a fourth group lists the personal projects that must move
   out to the person's personal space.
4. Next: keys (OSS-106), then the review (ORG-W7): what Acme receives from the person's Fabric (one adaptation item
   per project, a reference Acme cannot trace back, the typed label), what changes on this computer, and "N projects
   and M keys stay personal and are never sent".
5. Apply records `org-selection/1` locally (never sent, never signed by a device key, ORG-R11), hands the project items
   to Fabric's adaptation plan, which shows its own preview and sends `org-adaptation/1` with the person's credential
   (DEC-0138), and starts the local journal (ORG-W8). Fabric asks the person's passkey to sign the estate revision
   that places the chosen projects in Acme's context (R1-D12: a project enters by its person's own passkey, contract
   ADR-0178 point 7). The chosen projects now read `selected` for Acme (ORG-R28).

States: empty (no project matches: [Add a rule] and [Choose by hand]); loading (rows as skeletons); partial (Fabric's
registry or a provider not read: those projects read "account not known" and stay unselected); conflict (a project
claimed by two organizations, ORG-R05); error (the record cannot be written: nothing applied); offline (the record is
saved, Fabric sends when online, "waiting to send"); success (counts done per kind).

Edge cases: a personal project on a computer that is otherwise the organization's (the person clears it; it is never
named to Acme); a project with no repository (no rule can match; by hand only); a fork of an Acme repository in the
person's own account (ambiguous; the person decides); a project added after the selection (proposed, never selected,
ORG-R16); the person changes the selection later (a new revision replacing the old one by its digest, and a new
adaptation revision in Fabric, ORG-R11); the same record applied twice (nothing changes, ORG-R12); Fabric not
installed or not signed in (the projects wait, named, and keys and layout steps go on).

Acceptance: an unselected project never reaches the server in any form; a repository name reaches it only as a label
the person typed; the preview and the applied record have the same digest (ORG-R14); undo before Fabric's entry
revision is signed leaves no trace on the server, and after it a take-back waits for the person's passkey (ORG-R13).
Coverage: the local record of steps 2, 4 and 5 (OBS-417): `full org select --org ID plan, show, apply, undo` and
`observatory_org_selection` over `tests/test_org_selection.py` (`Defaults`: the proposals and the preview's line of
what stays personal, reading writes nothing; `Changes`: item by item, a group as listed, labels; `Apply`: only the
previewed digest, a verified revision naming its predecessor, a journal in which nothing without a door is done, the
same form again changes nothing and resumes, a project another organization holds refused; `WhatTheOrganizationSees`:
a random reference and only a typed label, nothing sent; `UndoAndConfirm`; `NewSince`: found later, listed, never
chosen, a finding; `Gates`: the operator's words for an agent). Not built: the hand-off to Fabric's adaptation plan
(Fabric TP-04, contract CT-169; the items wait as `fabric-adaptation-plan`), the window (OBS-232, OBS-425). Status:
draft; product outcome unobserved.

### OSS-106: choose which keys go, without any personal key leaving

Trigger: step 2 of the selection, or "give Acme the keys its projects need".

Steps:
1. ORG-W6 lists the vault slots of the selected projects, each with its environment, its issuing account (from the
   door's record) and a proposal: organization, personal or ambiguous, with why (ORG-R06). Door admin keys, a
   DigitalOcean team token among them, are listed as not movable (ORG-R07).
2. Per key, or per group (one project's keys, one account's keys, listed before the choice): ask Acme for its own key
   and revoke mine once it works; ask Acme for its own key and keep mine personal; keep personal and ask for nothing.
3. A value found twice in this space is marked "must be split" (ORG-R09); a slot whose name Acme already holds reads
   "nothing to do" when the values are equal and "Acme keeps its own" when they differ (ORG-R10).
4. On apply, the space that will hold Acme's keys (the new space in the separate-space layout) sends a credential
   request for each (provider, scopes, environment, slot name; no value, ORG-R03) through its own enrollment; Acme's
   replacement arrives sealed to that enrollment with its provenance (OBS-191 parts 2 and 3, OBS-360); the consumers
   switch to it; then the personal value is revoked at the provider and the revocation is proved, or kept when the
   person chose to keep it (ORG-R08). In the separate-space layout the personal space cannot read Acme's space, so
   each revocation waits for the person's confirmation that Acme's replacement works (a relayed step: the person, or
   an agent on their recorded words, runs `full org select --org ID confirm-replacement KEY`); in one space the
   journal's own signal (replacement received, consumers switched) is enough.

States: empty (the chosen projects have no keys here: "Nothing to hand over; Acme's keys for these projects are Acme's
to issue"); loading (names first, provenance after); partial (an issuer unknown, the key was put by hand: proposal from
the project only, marked); ambiguous (the person must choose; it stays where it is until then); waiting for Acme (a
request sent, no replacement yet: the personal key keeps working and nothing is revoked); error (a provider refuses the
revocation: the key is listed as "replacement in use, old value not revoked", with the door command; never shown as
done, ORG-R25); offline (requests wait in the outbox); success (each key: replaced and revoked, replaced and kept, or
personal).

Edge cases: a key from the person's own OpenRouter account used by an Acme project (ambiguous; recommended: Acme issues
its own from Acme's account, mine stays personal); a DigitalOcean team token for Acme's team kept in the personal space
(not movable: Acme's space stashes its own team token, then the personal copy is revoked, ORG-R07); a slot in an
environment Acme does not declare (needs a choice, ORG-R22); a provider with no API to mint (DigitalOcean): Acme's
administrator mints in the control panel and delivers it through Acme's request queue, and this step waits; the person
changes their mind after the request (OSS-108: take back; a personal key not yet revoked is untouched); a key shared
with another person (a share is outside every context and is never selected).

Acceptance: no value is ever sent, copied or placed in two contexts; a personal key is revoked only when the person
chose it and only after its replacement works (confirmed by the person in the separate-space layout), and the preview says this step cannot be undone; a failed revocation
is visible until settled.
Coverage: the classification of step 1, `full org keys` and `observatory_org_keys` (OBS-415): `Views` in
`tests/test_organizations.py` (organization, personal and ambiguous by project and issuer, put by hand, an
undeclared environment, door admin keys not movable, every value file unreadable and none opened). Not built: values
present twice and names the organization holds (ORG-R09, ORG-R10, OBS-415), the hand-over (OBS-416, OBS-360), the
request sent to the organization and its sealed envelope (OBS-454), the window (OBS-425). The request queue, the
administrator's decision and delivery into a slot of the space that holds the issuing door are built (OSS-110,
OBS-191 parts 2 and 3). The choices of step 2 are recorded by `full org select` and `observatory_org_selection`
(OBS-417, `tests/test_org_selection.py`); each key handed over waits there as `credential-requests` and `rotation`
(the selection files no request yet), and the confirmation of step 4 is refused until the replacement has arrived.
Status: draft; product outcome unobserved.

### OSS-107: decide where the organization's work lives on this computer

Trigger: step 4 of OSS-104, before anything enrolls, or "keep Acme's work in its own account".

Steps:
1. Three layouts with their costs (organization-onboarding.md, "Where the organization's work lives"): a separate
   space (recommended), this space is the organization's, both in one space.
2. A separate space: the launcher's administrator step creates the OS account (the macOS administrator password,
   ORG-R23), installs Observatory there and creates a space bound to `org:ID` (OBS-362, OBS-359); the administrator's
   computer token enrolls that space (OSS-104 step 5). The selection made in the personal space exports the chosen
   projects' curated entries into the new space (OBS-364); the new space requests the keys (OSS-106); once the import
   there is confirmed, the personal space forgets the curated entries that moved (OBS-372) and revokes the personal
   keys the person chose to revoke, each once the person confirms Acme's replacement works (ORG-R08). The personal space never enrolls in Acme.
3. This space is the organization's: every personal kind found here is carried into the person's personal space and
   verified there, or removed with its reason, before binding (ORG-R27, OBS-426): vault slots (carried, verified,
   never forgotten), the vault-digests key and manifests, the personal context's device keys, shares received,
   memory and ledger records of personal projects, and the snapshot settings. SSH private keys never move: the
   personal space makes and registers its own key wherever the old one is registered (`full ssh-keys rekey OLD
   --apply`, OSS-96), then the old key's registrations are removed as a rotation removes them (OSS-67) and its file is
   removed here. The person's estate record stays, applied read-only, because Acme's pin and unpin live there
   (ORG-R30). Personal projects' curated entries are exported there; then the space is bound to `org:ID` and enrolls
   with its computer token.
4. Both in one space: the space enrolls; every project and slot carries its context; the organization sees only its
   own (mode C); sessions start with the context's profile (OBS-236).

States: loading (the plan built); waiting for a human step (the administrator password, the computer token: each
named); partial (the new space exists, the import not confirmed: the personal space keeps everything until it is);
error (binding refused because an item of another context remains: listed with its step, ORG-R20); offline (local
steps run, enrollment waits); success (the space reads bound, `full context check` clean).

Edge cases: a computer with one OS account and no administrator (only "both in one space" is possible, said so); a VM
(one device, as an OS account); a space nobody logs in to (its jobs run through the keeper, OBS-361); two
organizations on one computer (two spaces, or both beside personal in one space); moving the organization's space to
another computer later (the move runbook; a personal snapshot into an organization's space is refused); no personal
space exists to receive personal keys when this space becomes the organization's (one is created first; nothing is
deleted until it holds them).

Acceptance: a bound space holds none of the personal kinds ORG-R27 lists (so ORG-R09's check holds on a converted
space); the personal space keeps an item until its import is
confirmed elsewhere; no personal key is ever deleted without a verified copy in a personal space; every OS account or
VM is its own device.
Coverage: the choice of step 1 only: the selection's `--layout` and `--serves` (OBS-417, `tests/test_org_selection.py`),
recorded before anything applies and journaled as waiting on its row (`space-create`, `move-out`,
`context-binding`). Not built: the steps themselves (OBS-359, OBS-362, OBS-364, OBS-372, OBS-426, OBS-236). Status:
draft; product outcome unobserved.

### OSS-108: leave, be removed, or take a project back

Trigger: "leave Acme"; "take landing-test back"; or the administrator revokes a computer in the hub's Computers page,
or the organization closes.

Steps:
1. Take a project back: ORG-W9, [Take a project back] (or `full org unassign PROJECT --org ID`). Observatory writes a
   selection revision without it and hands Fabric the take-back; Fabric drafts the estate revision that removes the
   project's entry and asks the person's passkey for it (leaving needs either the person or an administrator, Fabric
   ADR-0180 decision 2, amending ADR-0178 point 7), and withdraws the item from the adaptation plan. Until the
   passkey signs, the project stays in Acme's context and the screen says "stays in Acme until your passkey signs".
   Once signed, the keys Acme issued for it are deleted from Acme's space here. Observatory sends nothing else: each credential
   envelope ends within 30 days (`credentialExpiresAt`, DEC-0111), when the node deletes its value, and the provider
   key itself lives as minted until Acme's administrator revokes it (DEC-0111 item 4). Personal keys are
   untouched (they never left; any the person chose to revoke stay revoked).
2. An administrator releases a project: Acme's administrator signs a release statement in their own Fabric; the
   person's Fabric verifies it, stops that project's outflow to Acme on receipt, and drafts the revision removing its
   entry for the person's passkey (Fabric ADR-0180 decision 2); the local steps of step 1 follow.
3. Leave: Fabric withdraws the adaptation plan, then drafts the one estate revision that revokes Acme's grants,
   placements and pin and asks the person's passkey for it (AD-6, OM-5, Fabric ADR-0178 point 7); until it is signed
   the membership stands. In each space enrolled
   in Acme, `full org leave --org ID --plan` (or the screen) lists what happens (ORG-R17): the enrollment ends, Acme's
   credentials here are deleted, the local entry stays with its rules paused so the projects read Personal, checkouts,
   repositories and personal keys untouched; `--apply` does it. A space bound to Acme is left empty of context and
   offered for removal, never deleted by itself.
4. Removed by the administrator, or the organization closed: the space reads `removed` or `closed` (OSS-74) and the
   same local plan applies by itself, without a token; Fabric's plan is ended by the server (AD-6), and Fabric asks the
   person's passkey for the unpin.

States: empty (nothing was ever selected: leaving only ends the enrollment and the pin); loading (the plan built);
waiting (a take-back "stays in Acme until your passkey signs"; the unpin "waiting for your passkey"; Fabric's
withdrawal not answered yet: each shown, the local steps go on, never "done" for that part); error (a
credential that cannot be deleted locally: named with its kind and command); offline (local steps run; Fabric's
withdrawal waits); success (Acme absent from contexts; ORG-W1 shows Acme as local with paused rules).

Edge cases: leaving while a request waits for Acme's replacement (nothing personal was revoked, so nothing to restore);
a project Acme still lists after it was taken back (the server keeps its records until the administrator retires them;
nothing more is sent about it); resuming the paused rules later (the projects read Acme again as a local
organization, nothing is sent); rejoining (a new computer token, a new device record, a new selection).

Acceptance: leaving never deletes a checkout, a repository or a personal key; after leaving nothing more is sent to
Acme by Observatory; the session-start line names no Acme account for the left projects; what is sent at leave is
Fabric's withdrawal and unpin, and nothing from Observatory.
Coverage: removal and closing as built (`tests/test_node_org.py`, OSS-74); the rest none yet (OBS-419). Status:
draft; product outcome unobserved.

### OSS-109: my agent does all of it on my word

Trigger: the person says to their agent "join Acme with this token, give Acme its projects and the keys they need,
keep everything else personal", or any one step of OSS-102 to OSS-108.

Steps:
1. The agent reads the state (`observatory_org_accounts`, `full org accounts`, `full org status`) and says in one
   message what it will do and what stays personal.
2. It runs each step through the CLI or MCP tool of the screen's button (organization-onboarding.md, "Agent
   parity"), recording the person's words in DEC-0121's decision shape (by, via, action, target, the words verbatim,
   at, source) for each `relayed` step (ORG-R23).
3. It stops only at the `human` steps, named once each with their link: the passkey registration from the
   invitation, the fingerprint comparison and the passkey signing the pin in Fabric, the passkey signing the estate
   revision that places the chosen projects in Acme's context (Fabric, R1-D12), the passkey signing a revision that
   removes a project's entry or ends the membership (Fabric, ADR-0180 decision 2), the notice acknowledgement in the
   hub, and the macOS administrator password for a new OS account. Two more are another person's: the administrator
   preparing the computer token (and sending the root fingerprint), and an administrator's release statement. The
   person's confirmation that a replacement key works is `relayed`: their words to the agent are enough.
4. It reports what was done with evidence (the record's revision and digest, the journal's counts), what waits and
   on what.

States: as the screens'; an agent never reports "done" for a step whose journal is not `done`.

Edge cases: the person's words cover projects but not keys (keys stay where they are, and the agent asks once naming
the keys); an ambiguous key (the agent keeps it personal, the safest reversible option, and says so); the person's
words came through another of their agents (a relay with the quote and its source counts, a paraphrase does not); a
refusal from the server (reported with its words; never retried unchanged).

Acceptance: every button of ORG-W1 to ORG-W9 has a door with the same effect, refusals and gate class (a test holds
the map); no step needs the person to press anything except the six named human steps of their own, each of which is
in Fabric, the hub or the operating system, never in Observatory; the administrator's steps are named as another
person's.
Coverage: none yet (OBS-420, OBS-385). Status: draft; product outcome unobserved.
