# The always-on server as a Fabric service

Status: implemented on branch `agent/fabric-service` (not yet released). The protocol is
`fabric-service/0.1`, the local service extension of the Fabric Agent Contract
(`docs/specification/service.md` in `passioncode-ai/fabric-agent-contract`, commit `cc9ed2d`).
Every rule below names the test that proves it; all of them are in
`observatory/engine/tests/test_fabric_service.py`, which runs in `project-observatory full check`
as the `fabric_service` suite.

## What it is

`tools/serverd.py` is the process that keeps the dashboard up between ticks (see its module
docstring). The protocol lets a host such as Fabric Dashboards find it, confirm which build is
answering, supervise it through launchd and read what happened, without knowing Project
Observatory in advance.

| Question | Answer |
|---|---|
| `id` | `project-observatory` |
| `instance` | `default` for the standard workspace (`~/.local/share/project-observatory-full`), else `ws-<sha16>` of the resolved workspace path — the digest the launchd label already carries |
| Port | `47311` (`OBSERVATORY_SERVER_PORT`, `--port`); the installer claims it in the descriptor |
| Who calls it | the operator's browser (the dashboard); a local host reading the well-known document and the events feed; agents keep using the MCP server over stdio, unchanged |
| What it stores | nothing of its own: the lock and the events token in the workspace root, the heartbeat receipt `store/raw/serverd.json` as before. Uninstall keeps the workspace |
| What it tells the operator | a newly opened finding, as a notification-worthy event; commits, agent sessions and resolved findings as routine events |

## Surfaces

| Route | Auth | Answer |
|---|---|---|
| `GET /.well-known/fabric-service` | none; the Host/Origin/`Sec-Fetch-Site` guard applies | identity, build, pid, start time, status, `degraded`, up to six tiles, surfaces |
| `GET /fabric/v1/events?after=&limit=` | `Authorization: Bearer <token>` | a page of events, oldest first, with a cursor |
| `/`, `/dashboard/*`, `/health`, `/remote`, `/leaks`, `/skills` | none, as before | unchanged; `/health` still recomputes its answer on every request |

The dashboard stays `login: false`: it is read-open to local pages and changes nothing, which is
the access model [ACCESS.md](ACCESS.md) already states for this server. No POST route was added.

## Rules and their proof

1. **One copy per workspace, locked before any side effect.** `serve()` takes an exclusive
   `flock` on `<workspace>/service.lock` first — before the token, the heartbeat thread and the
   bind. A second copy prints one sentence naming the holder's pid and exits 75; it writes no
   heartbeat and never binds its port.
   - `test_a_held_lock_stops_the_server_before_any_side_effect`,
     `test_the_lock_comes_first_then_the_token_then_the_socket`;
   - `test_a_second_copy_exits_75_naming_the_holder_and_leaves_nothing` (real processes);
   - `test_sigterm_drains_and_releases_the_lock`: SIGTERM reports `stopping`, exits 0 and frees
     the lock for the next start.
   - `full open --serve` on another port now says which address already serves the workspace:
     `test_a_workspace_already_served_names_its_address`.
2. **The well-known document is answered from memory.** The heartbeat thread computes a
   snapshot (`service_health.snapshot`) every 20 seconds; the route reads it under a lock and
   never calls the `/health` computation. Registry documents are parsed again only when their
   mtime or size moved.
   - `test_the_route_reads_memory_only`, `test_a_registry_is_parsed_once_until_it_changes`,
     `test_well_known_is_valid_fast_and_names_this_process` (schema-valid, median under 100 ms).
3. **`degraded` says why the picture is partial.** One row per collector receipt that reports
   degraded sources (`degradations.every_collector()` plus `model.json`), a row for a tick that
   was interrupted, went stale or runs too long (`tick_health`), and a row for a registry, findings
   document or leak register that cannot be parsed. At most 64 rows, source ≤ 80 and reason ≤ 300
   characters. `ready` with a non-empty list is reported as `degraded`.
   - `test_a_dead_tick_and_a_partial_collector_degrade_the_service`,
     `test_unreadable_sources_are_degraded_not_zero`,
     `test_limits_of_the_protocol_hold_under_many_sources`,
     `test_a_bad_token_file_costs_the_feed_not_the_server`.
4. **Absent is not zero.** Tiles: Projects, Open findings (critical and warning, not
   acknowledged — the severities the notification step pushes), Open leaks, Last tick. A
   workspace that has built nothing shows `not built`, a missing leak register `not kept`.
   - `test_a_fresh_workspace_is_healthy_and_says_nothing_was_built`,
     `test_counts_attention_and_what_needs_a_person`.
5. **The build is named.** `FABRIC_BUILD_COMMIT`, else this project's own checkout (never an
   enclosing repository), else the installed wheel's `direct_url.json` (VCS commit or archive
   sha256) or its RECORD digest, else the digest of `SOURCE-INVENTORY.json`.
   - `test_build_prefers_the_stated_commit`, `test_a_foreign_enclosing_repository_is_not_this_build`,
     `test_an_installed_wheel_reports_its_archive_digest`.
6. **The events feed is a view, and the token opens nothing else.** `service_events` reads
   `store/observatory.db` read-only (`mode=ro`, `query_only`) and never migrates it. The token is
   created with mode 600 after the lock (the kit's `ensure_token`), accepted only as
   `Authorization: Bearer`, compared in constant time, and refused in a query string. A token file
   that is unsafe costs the feed (503, a `degraded` row), not the dashboard.
   - `test_events_need_the_token_in_the_header_and_only_there`, `test_events_page_with_the_token`,
     `test_the_view_never_writes`.
7. **Events read as sentences and page by cursor.** The id is the SQLite `rowid` of the event's
   last row, zero-padded to twelve digits, so ids increase as numbers and as strings. Consecutive
   commits of one author in one project are one event ("3 commits in Fabric by A. Author; latest
   “…”"), consecutive sessions of one project likewise; a finding is never grouped. Links open the
   project panel (`/dashboard/projects.html#project:<id>`) or the finding's row
   (`/dashboard/findings.html#f-<id>`). Only `finding.opened` carries `notify: true`.
   - `test_consecutive_commits_are_one_sentence`,
     `test_sessions_findings_and_unknown_kinds_read_as_sentences`,
     `test_cursor_paging_sees_every_row_once_and_then_the_new_ones`,
     `test_newest_limit_without_a_cursor_and_the_bounds`,
     `test_a_missing_store_is_an_empty_feed_and_a_broken_one_an_error`.
8. **The installer, not the process, owns the descriptor.** `--install` writes
   `project-observatory.<instance>.json` into the services directory (`FABRIC_SERVICES_DIR`, else
   `~/Library/Application Support/ai.passioncode.fabric/services/`) first, where a port another
   service declares is refused before any plist exists; then the kit's `launchd_install` writes
   and lints the plist, bootouts and waits, bootstraps with retries and polls the well-known
   document until this id and instance answer. The plist gains `ThrottleInterval 10`,
   `ExitTimeOut 40` and `ProcessType Background`, keeps mode 600 and still carries no secret.
   `--uninstall` removes plist and descriptor and keeps the workspace.
   - `test_install_writes_the_descriptor_then_the_plist`,
     `test_a_claimed_port_is_refused_before_any_plist`,
     `test_uninstall_removes_the_plist_and_the_descriptor_and_keeps_the_data`,
     `test_install_refuses_off_macos_without_writing`,
     `test_descriptor_is_valid_and_names_this_workspace`.
9. **The provider manifest points at the service.** `fabric-agent.json` revision 5 carries
   `provider.extensions["https://fabric.passioncode.ai/agent-contract/extensions/service/0.1"] =
   {"descriptor": "project-observatory.default"}`; the content hash is restamped by
   `tools/fabric_hash.py`. A non-default workspace's descriptor names its own instance; the
   manifest ships once and names the standard one.
   - `test_the_manifest_points_at_the_standard_descriptor`.
10. **The protocol's own probe passes.** The kit's `check_service.py`, vendored unedited, runs
    against a live server in a temporary workspace: 0 FAIL, and NOT_RUN only for
    `login.single-use` (the dashboard declares no login) and `lifecycle.launchd` (the test
    descriptor has no supervisor).
    - `test_the_conformance_probe_passes`; the kit's and the probe's bytes are checked against
      the digests in their headers by `test_kit_and_probe_are_the_upstream_bytes`.

## Vendored parts

| File | Upstream |
|---|---|
| `observatory/engine/fabric_service.py` | `passioncode-ai/fabric-agent-adapter` `dfd11dad72fe`, `building-fabric-services/scripts/fabric_service.py` |
| `observatory/engine/tests/check_service.py` | the same commit, `scripts/check_service.py` |
| `observatory/engine/fabric/service-schemas/*.schema.json` | `passioncode-ai/fabric-agent-contract` `cc9ed2d`, `schemas/` |

Never edit them here: update upstream, copy again, and the digest test says whether the copy is
whole.

## Limits, stated

- **A reused rowid can hide one event.** SQLite reuses the highest rowid when the newest row is
  deleted before the next insert. The only deleter of fresh rows is a session withdrawn as
  ambiguous; a host already past that id misses the one row that reuses it.
- **Finding events exist only where notifications run.** `finding.notified` rows are written by
  `tools/notify_findings.py` when it delivered a notification (`features.notifications`). A
  workspace without it shows commits and sessions only. The host may notify again for a finding
  macOS already announced; its own settings decide.
- **Sentences are English**, like the texts the finding rules write.
- **The probe reports `state.outside-code` as FAIL on a workspace whose root is a Git
  repository** — the shape registry history uses (`features.registry_history`), where the
  workspace's `.gitignore` tracks `registry/` only. The lock and token are ignored by that history
  but still inside a repository, which is all the probe checks. The workspace holds state, not code; the
  choice to keep `paths.data` equal to the workspace is deliberate (one workspace, one data
  directory, one instance). Resolving it is either a probe that accepts an ignored path or a data
  directory outside the workspace — a decision for the protocol's owners, recorded here rather
  than hidden.
- The same-user boundary of [ACCESS.md](ACCESS.md) applies: any process running as the operator
  can read the token file.
