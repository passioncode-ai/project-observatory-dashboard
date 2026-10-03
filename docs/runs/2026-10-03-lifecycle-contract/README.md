# 2026-10-03 — the engine's side of the product lifecycle contract

The organisation adopted a product lifecycle contract on 2026-10-03 (the knowledge base's
[lifecycle page](https://github.com/passioncode-ai/fabric-workspace/blob/main/knowledge/lifecycle.md),
rules LC-01…LC-15). A lifecycle audit of the installed 0.13.0 measured where this engine broke
it; this run fixes the engine's part, test-first, and adds the measurement the contract asks
this engine to make for every product ("Watching it hold"). Branch `claude/lifecycle-contract`;
synthetic names only. Not released: a feature branch, not a version bump.

## What changed, finding by finding

The F-numbers are the audit's; each row names the rule and the test that proves it.

| Finding | Rule | What changed | Test |
|---|---|---|---|
| F3: every tick ran `claude mcp list` (≈480 MCP server launches a day, the operator's Claude login refreshed by a background job, the credential on another process's argv, servers orphaned for minutes) | LC-04, LC-08 | The tick runs `scan_mcp.py --declarations-only`: configs only, nothing started; the last on-demand verdict is carried with its time (`liveness_at`). Only `full scan-mcp` / `machine.mcp.refresh` probe, in a process group of its own, output to a private file (a pipe stayed open as long as any server lived), the group reaped on answer or timeout | `test_lifecycle.McpProbeStaysOutOfTheTick` (5) |
| F2: the 12-hourly disk survey sized other apps' containers and `~/Downloads`, rewriting a privacy consent at night | LC-06 | `scan_machine.protected_place` (by path, before any stat): Documents, Downloads, Desktop, media folders, iCloud Drive, other apps' containers are sized only by `full machine --disk`; defaults mark them `manual_only` | `test_lifecycle.DiskSizingStaysOutOfProtectedPlaces` (4) |
| F5: no per-step timeout; ticks measured up to 64 min | LC-02, LC-03 | `tick_lease.bounded`: each step in its own process group under a wall-clock limit (900 s, leaks 1200 s), never past the tick's deadline; the supervisor stops the whole tick at a 1500 s ceiling (lowered below shorter intervals) and sweeps a step group its runner could not stop; `store/raw/tick-run.json` records start, end, outcome, reason | `test_lifecycle.EveryStepHasAWatchdog` (8) |
| F6: tick plist without `ExitTimeOut` (launchd's 5 s) under a supervisor that waited 10 s | LC-02 | `ExitTimeOut` 30 s, above `SUPERVISOR_GRACE` 10 s + `STEP_GRACE` 5 s | `test_launchd_waits_longer_than_the_supervisor_does` |
| F7: the server's heartbeat re-read ~200 KB and rewrote its receipt every 20 s (~4,300 writes a day) | LC-08 | Inputs re-read only when their file changed; receipt written on change or every 300 s; beat 120 s with no client in 5 min; the receipt names its own `silent_after_s`, which the page, the status command and the board read | `test_lifecycle.HeartbeatIsIdle` (6) |
| F8: `serverd.err` never rotated, `secret-use.jsonl` unbounded, one log world-readable | LC-12 | `log_policy.py`: 5 × 5 MB, 0600, launchd-held files copied and truncated; the tick's `logs` step and the server apply it | `test_lifecycle.OneRotationPolicy` (6) |
| F4: per-session MCP servers kept running the previous release after an update | LC-10, LC-11 | `code_freshness.Freshness`: each call checks the server's version file; replaced or removed code answers `stale-server`. `full update` reports the session servers it could not reach (pids) and why it does not signal them | `test_lifecycle.ReplacedCodeAnswersStale` (5), `test_engine_update.test_session_servers_on_the_previous_release_are_reported`, `test_the_census_reads_only_this_engines_server` |
| F11: secrets on argv | LC-04 | The engine passes none (values go on stdin); the one indirect case was `claude`'s own, gone from the tick with F3. A lint over every engine `subprocess` call keeps it that way | `test_lifecycle.NoSecretOnArgv` (2) |
| F13: plists carried the interactive PATH (plugin folders of deleted versions) | LC-05 | Minimal PATH: the installer's directories that hold an engine tool, then system ones; the interpreter by venv path or Homebrew `opt` link; both installers refuse a plist naming a Cellar path | `test_lifecycle.PlistsCarryAStablePath` (4) |
| Watching it hold | LC-02, LC-03, LC-10, LC-12 | `collectors/scan_lifecycle.py` (tick step `lifecycle`) and `tools/lifecycle_findings.py`: orphaned product processes, session servers on replaced code, jobs past their interval, logs past their cap or readable by others, per owning product (this engine, every fabric-service descriptor, `config/lifecycle.json`) | `test_lifecycle_watch` (18) |
| LC-15 | LC-15 | `tools/prune_builds.py`: newest two releases in `dist/`, other Observatory bundles unregistered from LaunchServices and removed, cache caps with `--clean-caches`; `build-app.sh` unregisters the bundle it replaces and runs the prune | `tests/test_build_retention.py` (7) |
| LC-09 | LC-09 | `AGENTS.md` → Lifecycle: every resident process, label, port and session server, who starts and stops it, its idle budget; the build-retention line | `test_agents_md_names_the_outputs_the_cap_and_the_clean_command` |

Existing tests changed because the behaviour changed on purpose: `test_workspace_scheduler`
(the tick plist gains `OBSERVATORY_TICK_CEILING_SECONDS`; a PATH directory without an engine
tool is dropped), `test_fabric_service.test_health_is_unchanged` (`silent_after_s`),
`test_tick_failures` (the `step` helper runs commands through the watchdog),
`test_pipeline` (`findings` reads `store/raw/lifecycle.json`).

<a id="f5-f6-every-step-is-bounded"></a>
## F5, F6: every step is bounded

`tools/tick.sh` sends every command — the `step` helper and the commands whose output the tick
reads itself (`bounded`) — through `tools/tick_lease.py step NAME -- CMD…`. The runner puts the
step in a new process group, writes the group id to `store/tick-step.pgid`, waits at most
`min(limit, deadline − now)`, then SIGTERM, 5 s, SIGKILL to the group, exit 124. The supervisor
sets the deadline (`OBSERVATORY_TICK_DEADLINE`), waits at most the ceiling, stops the tick group
with a 10 s grace, kills a step group left behind, and writes `tick-run.json`. A stopped step is
a failed step in `tick.json` and on the board.

## Not fixed here, and why

- **F1 / H6 — consents keyed to an ad hoc interpreter (LC-05).** A `brew upgrade python@3.14`
  still voids the tick's privacy consents: TCC keys them to the code identity of the binary that
  runs, and the `opt` link resolves to the same ad hoc Cellar binary. The fix is a launcher in
  the app bundle, signed with the app by `.github/workflows/release.yml`, that spawns the
  virtual environment's python as its child and is the plists' `ProgramArguments[0]`. It changes
  the bundle the release workflow builds and signs, so it is its own change. Recorded in
  `AGENTS.md` → Lifecycle. The backup root's default under `~/Documents` (part of F1) is a
  data-location change with a migration of existing backups; left for that work.
- **F12 / H5 — ad hoc signed app (LC-05).** Closed on `main` by the notarisation work
  (`macos/scripts/notarize.sh`, `.github/workflows/release.yml`), merged into this branch; this
  run signs nothing.
- **F9 — 200 MB of pre-migration data.** The engine's part is closed in the follow-up pull
  request: `store/retention.py` now owns `store/migration-backups/` (the newest two copies and
  any younger than 30 days stay; `migration_backups_keep`/`_days` in `retention.json`), tested by
  `test_lifecycle.MigrationBackupsAreRetained`. The other 162 MB is one machine's copy of the
  private predecessor's store, outside any workspace this engine owns: deleting it is the
  operator's call, not a retention rule.
- **F10 — six interpreter starts per agent turn in the plugin hook.** A plugin change with its
  own version bump (`observatory-log`); not in this packet.
- **Server `ProcessType`.** The audit suggested `Adaptive`/`Background`; it stays `Standard`
  for the recorded reason (a Background server was starved and reported "not answering" 30
  times a day). Idle cost is now cut at the source instead (F7).

## Checks run

On this branch, Python 3.14.7, before the pull request (exit codes read directly):

| Check | Result |
|---|---|
| `observatory/engine/tests/run_portable.py` (what `project-observatory full check` runs) | 204 of 204 suites PASS before merging `main`; 205 of 205 after, exit 0 |
| `python -m unittest discover -s tests` | 114 tests OK before merging `main`; 124 after |
| `python tools/update_inventory.py --check` | passed |
| `python tools/check_public_release.py` (working tree) | passed, 0 findings |
| every source compiled with Python 3.11 | 0 syntax errors |
| new suites against the code before the change | `test_lifecycle` 12 failures + 25 errors, `test_lifecycle_watch` 18 errors, `test_build_retention` 7 errors — each fix then watched turning them green |

Not run here: `swift test` and `macos/scripts/build-app.sh` (no Swift source changed; the
script change is covered by `tests/test_build_retention.py` and `bash -n`; the app is not
signed by this run), `claude plugin validate --strict` (no plugin file changed).

## Landing note

`feat/macos-notarize` (release signing) and #127 (agent memory) landed on `main` first. This
branch merged `main` (`baa4664`) — no force-push — keeping both sides of `run_portable.py`,
`locales/ru.json` and `docs/HANDOFF.md`, and regenerated
`observatory/engine/SOURCE-INVENTORY.json`. The pull request squash-merges into a linear `main`.

## Follow-up (same day)

Branch `claude/lifecycle-followups`: migration-backup retention (F9, above), and two watchdog
tests and one probe test given room for a loaded machine (a 1 s limit could cut bash before it
planted the child the test then looks for). The `CHANGELOG.md` entry for this work is left to
the release run, which holds that file's lease; the text it needs is in the pull request.

## Next task

1. Land this branch through the three required checks; then a release (version bump,
   `CHANGELOG.md`) by the coordinator.
2. After installing that release on a machine, re-run `tools/install_launchd.py install` and
   `tools/serverd.py --install` so the plists get the minimal PATH, the ceiling and
   `ExitTimeOut`; `full update` restarts the old plists as they are.
3. The signed launcher (above), when the Developer ID certificate is available.
