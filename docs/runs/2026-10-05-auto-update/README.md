# 2026-10-05 — updates that arrive by themselves, and data that survives a reinstall

Operator instruction, 2026-10-05:
- Every installation that users download updates itself by default, with no action from the person.
- Every setting and connection the product keeps is backed up, and survives uninstalling, reinstalling and deleting the program.

The operator asked for the work to be done autonomously to the end. So this brief records each decision with the reason it was taken, instead of asking about it.

## Sources read

| Source | What it says |
|---|---|
| `observatory/engine/engine_update.py` | `full update --check` (exit 10 when an update exists), and `--apply` as one verified, reversible transaction. Reads `/releases/latest`, so stable releases only. Waits for the tick's lock. Needs no terminal. Stops and restarts only `install_launchd.managed_jobs()`, which are the tick and the server. |
| `observatory/engine/backup_vault.py` | Encrypted backups (`OBSENC1`) go to a root outside the workspace, but only when a passphrase is configured (`status`, "no backup passphrase: copies stay inside the workspace"). The root's subfolder is `<home name>-<instance_id[:8]>` (`_workspace_label`). |
| `observatory/engine/workspace_upgrade.py` | A full snapshot refuses unless writers are stopped (`require_stopped`). It fails with "Workspace changed during snapshot" when anything writes during the copy. |
| `observatory/engine/tools/tick.sh:182-187` | The tick takes a daily copy of the database only. Full snapshots are taken before an upgrade or by hand. |
| `observatory/engine/workspace.py#initialize` | Every initialization writes a new `instance_id`. |
| `observatory/engine/tools/install_launchd.py` | macOS only, and the tick is gated by `features.scheduler`, off by default. Linux has no scheduler. |
| `passioncode` `lib/launcher.js:512`, `plugin/passioncode/hooks/session-start.js` | The plugin channel already updates itself: once a day at session start, from trusted publishers, on unless the person turns it off. |
| `docs/backlog.md` | OBS-19 (stable identity for jobs) and OBS-20 (backups out of `~/Documents`) are adjacent and stay their own rows. |
| Live installation, `full doctor` 2026-10-05 | Passphrase configured by hand. Newest full snapshot 2026-09-28. Daily database copies are current. |

## What is true today

**Updates**

| Component | How it updates | By itself? |
|---|---|---|
| Engine (wheel) | `full update --apply`, run by a person | no |
| Mac app | download the zip and run `install-app.sh`, by a person | no |
| `observatory-log` plugin | the PassionCode launcher, once a day | yes |
| Installs at 0.7.0–0.16.0 | carry no code that could update them | no |

**Data**
- A fresh workspace has no backup passphrase, so every copy stays inside the workspace. Deleting the workspace deletes the copies with it.
- Only the database is copied automatically. The registry, the settings, the vault and the connection configuration are copied only when an update runs or a person takes a snapshot.
- A reinstall creates a new `instance_id`, so its label differs from the old one. The new workspace does not find the old backups and starts empty.
- On Linux the backups root is `<home>/backups`, inside the workspace.

## Decisions

| # | Decision | Why |
|---|---|---|
| D1 | **Updates install by themselves**, not just a notice. Default on; `full auto-update off` turns it off and the choice is kept in `settings.json` → `updates.auto`. Stable releases only. | The operator asked for updates "without doing anything". `full update --apply` is already verified and reversible. |
| D2 | **One maintenance job per workspace.** launchd `org.project-observatory.<sha16>.maintain` on macOS, a systemd user timer on Linux. It runs `tools/maintain.py` every hour. Network checks happen at most once a day. Engine updates are retried after a failure, at most once a day. | Hourly lets a deferred app swap happen soon after the app quits. Daily network use avoids the GitHub rate limit. |
| D3 | **The maintenance job is not one of `managed_jobs()`.** | `full update` stops those jobs, so the job would stop the process running the update. |
| D4 | **The Mac app follows the engine.** When the installed app is older than the engine, the job downloads that release's app zip and verifies it: the GitHub digest, `SHA256SUMS`, `codesign --verify --strict`, the same team identifier as the installed app, and `spctl` acceptance. It swaps the bundle only while the app is not running; a running app gets the update after it quits. The previous bundle is kept in the workspace. | An app replaced under a running process can break. The engine already holds verified downloads. |
| D5 | **Backups leave the workspace by default.** When no passphrase exists, one is generated (32 random bytes). It is stored in the workspace as today and also in the OS credential store under the workspace label: the macOS login Keychain through `security -i` on stdin, the Secret Service through `secret-tool` on stdin, or else an owner-only file under `~/.config/project-observatory/`. On Linux the default root moves to `~/.local/share/project-observatory-backups`, outside the workspace. | A copy that stays in the workspace is lost with it. The secret goes on stdin, never in argv. |
| D6 | **A full encrypted snapshot every day**, taken by the maintenance job. On macOS it stops this workspace's tick and server for the copy and starts them again; on Linux nothing is running. An engine update counts as that day's snapshot. | Settings, the vault and connections live outside the database. |
| D7 | **A reinstall restores.** `full init` on an empty home looks for backups of a workspace at the same path in every backups root it knows. If it finds one and the OS credential store opens it, it restores the newest snapshot. `--fresh` skips this. When the passphrase cannot be found, `init` creates the workspace and names the backup and the restore command. `full restore --latest` does the same by hand. | A workspace restored into an empty home loses nothing. |
| D8 | **The bridge for installs at 0.7.0–0.16.0** runs from the `observatory-log` plugin's SessionStart hook, which the launcher keeps current. When the engine has no `maintain.py`, it checks once a day with `full update --check` and starts a detached `full update --apply` unless `updates.auto` is false. The new release's upgrade step installs the maintenance job. | It is the only code those installs receive by themselves. |
| D9 | Out of scope, each kept on its own row: moving the macOS backups root out of `~/Documents` (OBS-20) and the signed launcher identity (OBS-19). | Each is a data-location or bundle change with its own migration. |

## REQ table

Frozen at this brief: adding a row is free, removing one needs the operator.

| REQ | Requirement | Verified by |
|---|---|---|
| R1 | `updates.auto` defaults on; `full auto-update on\|off\|status`; older readers ignore the key | unit tests; an older-reader load test |
| R2 | The maintenance job installs updates by itself: a daily check, apply through `full update --apply`, failures recorded and retried no more than daily, nothing done when auto-update is off | unit tests with a fake release source |
| R3 | The maintenance job is scheduled by default on both platforms, at `init` and at `upgrade`, and stays out of `managed_jobs()` | tests on the plist/unit text; a test that `managed_jobs()` excludes it |
| R4 | The Mac app updates to the engine's release after verification, never while running, and keeps the previous bundle | tests with fake `codesign`/`spctl`/`pgrep` and a fixture bundle |
| R5 | A passphrase exists by default and is stored outside the workspace; the secret never appears in argv | tests that capture argv and stdin |
| R6 | Backups leave the workspace by default on Linux too | `root_info` tests |
| R7 | A full encrypted snapshot is taken daily | maintenance tests |
| R8 | `init` on an empty home restores the newest backup of the same path; `--fresh` skips; `restore --latest` | tests: restore round trip after deleting the workspace |
| R9 | Installs at 0.7.0–0.16.0 are reached through the plugin hook | hook test with a fake engine |
| R10 | `doctor`, the Health page and the docs say whether auto-update is on, the last check and its result, the newest full snapshot, and where the passphrase is kept | doctor test; dashboard data test |
| R11 | Shipped as 0.17.0, installed here, with the plugin carried through the launcher | release receipt |

## Independent review (stage 5)

A second agent read the diff adversarially and reported ten defects. Each fix below has a
test that fails without it (`tests/test_maintenance.py`, `tests/test_engine_update.py`).

| # | Severity | Defect | Fix |
|---|---|---|---|
| F1 | P0 | Scheduling from inside a pass (the update the pass runs, or the hook) booted the job out and killed the pass mid-update. The plist's `PATH` came from the caller, so the definition flipped between a terminal and an agent session | `install()` never boots the job out while a pass runs (`IN_PASS_ENV` or the pass lock); it writes the definition and marks `maintenance-reload-pending`. An installed job keeps its `PATH` |
| F2 | P1 | SIGTERM during the snapshot left the tick and server stopped | SIGTERM raises `SystemExit`. The jobs to restart are recorded before the stop, and the next pass starts any listed |
| F3 | P1 | `needs-person` blocked updates forever, even after a person fixed it | Cleared once the running engine is no longer the one that needed the person |
| F4 | P1 | An update re-enabled a schedule a person removed | The update runs `maintain ensure --if-wanted` |
| F5 | P1 | `candidates()` matched `<name>-*`, so another workspace (`<name>-dev-…`, or the same name at another path) could be restored | Exact label pattern, and a `.workspace-path` owner file written at export |
| F6 | P1 | Two `full update --apply` could run at once (job, bridge, person; twenty sessions starting together) | `update_lock` held for the whole transaction. The pass skips while it is held. The hook claims its window atomically with `mkdir` |
| F7 | P1 | `secret-tool` installed but no keyring meant no copy outside the workspace | Falls back to the owner-only file, and `get()` reads both |
| F8 | P2 | The hook's background job stayed in the session's process group | `start_new_session=True` |
| F9 | P2 | The app updater downloaded every hour after a refusal, and checked `running()` only before the copy | The team is checked before any download. A refusal is retried once a day per version. `running()` is checked again just before the rename |
| F10 | P2 | The daily snapshot killed a running tick, rotated away a person's snapshots, gave up on one damaged backup, and the Health page overstated encryption | The snapshot waits for an idle tick. It has its own `daily` kind. Restore falls back through every snapshot, newest first. The Health rows read the record. systemd refuses `%` and `$` in paths. Passphrase generation is serialised |

Separately, the first gate found that the portable runner copies root modules from a list
(`ROOT_FILES`). `maintenance.py` and `app_update.py` were added to it, and the suite to
`BOUNDARY`.

## Carry-over

None yet.

## Released and verified live (stage 8)

**0.17.0 is out.** #169 merged as `9bc586e`, tag `v0.17.0`; release run 37331123714 succeeded
(wheel, macos, publish). An agent approved the `release` environment on the operator's
instruction, recorded as an exception as before. The downloaded assets were checked:
- `shasum -a 256 -c` OK: wheel `cae84567…cdd342`, app `364adafc…0d761`;
- GPG: good signature, key `63B3 0DC3 … C803 B6A7`;
- both attestations: exit 0;
- `check_package`: 552 files;
- the app: Notarized Developer ID, stapled, version 0.17.0.

**On the maintainer's machine:**

| Check | Result |
|---|---|
| `full update --apply`, run by the 0.16.0 engine | `updated`, no rollback, `degraded: []`. `maintenance` is absent: the old transaction has no such step, which is the bridge case |
| `OBSERVATORY_SYSTEM_SETUP=1 full maintain ensure`, the path the plugin hook takes | job `org.project-observatory.<sha16>.maintain` written and loaded; the person-set passphrase mirrored to the login Keychain (`kept_outside: keychain`) |
| Keychain item "Project Observatory backups", account `<home>-<instance>` | present (looked up by attributes only, no value printed) |
| First pass (`RunAtLoad`): update check | `up-to-date` |
| First pass: app | the 0.17.0 app zip downloaded and passed the real `codesign`, team and Gatekeeper checks; `waiting-for-quit`, because the app was open |
| First pass: daily snapshot | **failed**, and the pass crashed: `sqlite3.DatabaseError: database disk image is malformed` while verifying the COPY of `store/observatory.db`. The live store and the migration backup pass `quick_check`; copying again by hand worked 3 times out of 3. Cause: a session MCP server wrote during a copy whose source was opened `immutable`. The `finally` restarted the tick and server (both loaded, `/health` 0.17.0) |

**Fixed in 0.17.1.** A torn copy is made again, the snapshot retries on a database error,
and any other error is recorded without crashing the pass. A test for each layer was watched
failing without its fix. The root cause, the immutable open, is OBS-37.

## 0.17.1 live: the job updating itself, and two more findings (stage 8, continued)

**The update check.** The live 0.17.0 job was started by launchd after its check record was
cleared, so the check was due. Its `full update --check` hit the 600-second limit
(`TimeoutExpired`, recorded as `undetermined`). By hand the same check took 2.1 s and saw
0.17.1 (exit 10). Cause: the job ran as `ProcessType Background` with `LowPriorityIO`, on a
machine at load average 280–370 from other sessions. Fixed in 0.17.2: `Standard`, nice 10,
and an undetermined check is retried the next hour.

**The database copies in the backups root.** Three `observatory-db-*.obsdb` copies of 8522
bytes each, written by the installed 0.17.0 engine at 16:51:13/15/17 UTC, pushed the real
daily copies out of the rotation. The daily and pre-update snapshots hold the store, so no
data was lost. These did not cause it:
- the gate, which ended 16:23 UTC;
- the root suite and `test_agent`/`test_docs_current`/`test_i18n`, probed against a
  temporary root;
- the tick, which has no log lines then.

Fixed in 0.17.2: a database outside `<home>/store` is copied beside itself, never into the
workspace's root. The caller is OBS-39.

## State at handoff (2026-10-05, the session's usage limit)

**Done on this branch.** R1–R10 are implemented and documented: ONBOARDING (both copies), AGENTS.md
Lifecycle, COMPATIBILITY (both), CHANGELOG 0.17.0, scenarios OSS-38/39. Version 0.17.0 is in every
declaration.

**Checks run:**
- `test_maintenance` 61 OK, `test_engine_update` 53 OK, `test_backup_vault` 32 OK,
  `test_workspace_upgrade` 29 OK, `test_workspace` 21 OK (run alone).
- 33 planted defects (15 + 18), each caught by its test.
- `systemd-analyze verify` passes on the generated units (Debian container).
- The privacy gate passes with 0 findings; `update_inventory --check` passes.

**Not yet verified.** The full local gate (`run_portable.py`, 224 suites) was started on the
release head and had not finished when the session stopped. The previous gate run, before
`ROOT_FILES` listed the new modules, failed for that reason only.

**Next task, in order:**
1. Run the full gate. Fix what it names (likely `test_handed_commands` samples for the new
   printed commands, and `wire`/`docs` counters).
2. Open the PR; CI 3/3; squash-merge; tag `v0.17.0`.
3. Release run: the `release` environment approval is the operator's (or the recorded agent
   exception).
4. Verify the downloaded assets as in the 0.16.0 receipt.
5. `full update --version 0.17.0 --apply` on the maintainer's machine. Then check that
   `full maintain status` shows the job scheduled, that a pass takes a `daily-*.obsnap`, and
   that the Keychain item "Project Observatory backups" exists.
6. Launcher: pin Observatory Log `v0.17.0`, release 0.1.29.
7. Website names 0.17.0; the receipt run record.
