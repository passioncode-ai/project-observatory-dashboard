# An automatic update in launchd's system domain (OBS-581)

## What happened

On one machine whose maintenance job runs as a system daemon of the account (root's `space_daemons.py` loads it
with `UserName` set, OBS-361 part 2), the automatic update from 0.38.0 to 0.39.1 ended with the record
`undetermined: exit 3`, and `store/logs/maintain.log`, `maintain.err` and `update.jsonl` said nothing more. The
same `full update --apply` run by hand from the account's shell completed every step.

## Why nothing could be read (proven from the code, fixed)

| Gap | Where | Fix |
|---|---|---|
| Exit 3 is `Undetermined`: `full update` prints `{"status": "degraded", "degraded": [reason]}` and no `error`. `step_update` read only `error`, then the last stderr line (empty), then wrote `exit 3`. | `maintenance.step_update` | The detail is `error`, else the first `degraded` reason, else stderr, else the exit. |
| `main` journalled a `refused` apply only: an undetermined apply wrote no line to `update.jsonl`. | `engine_update.main` | An `undetermined` event with the failed step, the reasons and how the API was read; `reconcile_in_flight` reads it as final. |
| No step was named. | `engine_update.Transaction` | `stage` follows the run (`preflight`, `download`, `rollback-wheel`, `machine-backup`, `space-check`, `stop-writers`, `snapshot`, `install`, `workspace-upgrade`, `verify`, `finish`; before the transaction `release-source`, `resolve-release`, `job-helpers`, `update-lock`); every non-success report carries `failed_step`. |
| The apply's own output (`.<home>.update-apply.out` and `.err` beside the home) is truncated by the next attempt. | `maintenance.Commands.apply` | The last 40 lines of each stream, each line at most 300 characters, URL passwords and every credential shape replaced (`credential_shape.redact`), go into the maintenance record (`update.stdout_tail`, `stderr_tail`) and into an `automatic-update-failed` line of `update.jsonl` with `exit`, `result`, `detail` and `failed_step`. |
| `maintain.log` and `maintain.err` are empty by design for a system daemon: `space_daemons.py` replaces the definition's log paths with `/Library/Logs/ProjectObservatory/<user>/<label>.out.log` and `.err.log` (a root-owned folder, so root never writes through a link the account controls). | `maintenance.LaunchdSchedule.status` | A schedule in the system domain answers `logs: {stdout, stderr}`, read from the loaded daemon, else the layout `space_daemons.log_paths` writes. |

## What the system domain changes for the update (proven, said rather than changed)

`engine_update.LaunchdServices` asks `launchctl print gui/<uid>/<label>`. A tick or server that root runs as a
system daemon of the account is invisible there, and only root can stop it (`bootout system/<label>`). Before this
change the update stopped nothing, installed under the running jobs and restarted nothing, in silence. Now
`stop_writers` asks `machine_move._system_daemon_loaded` for every managed job `gui/<uid>` does not hold, and for
each one it finds: the report lists it under `services_system_domain`, adds a `degraded` line, and `human_steps`
carries `sudo launchctl kickstart -k system/<label>`. The exit code is unchanged: refusing would turn an update
that installs today into one that never does, and the account has no way to stop the job itself.

```
maintain pass (system daemon, UserName=<account>)
  -> Commands.apply: full update --apply --unattended    (stdout/stderr beside the home)
       main: release-source -> resolve-release (API) -> update-lock
       Transaction: preflight -> download -> rollback-wheel -> machine-backup -> space-check
                    -> stop-writers (gui/<uid> only; system daemons said) -> snapshot -> install
                    -> workspace-upgrade -> verify -> finish
  <- code, report {failed_step, degraded|error, api_reads}
  -> maintenance.json update {result, detail, failed_step, stdout_tail, stderr_tail}
  -> update.jsonl {event: automatic-update-failed, ...}
```

## Open causes, to measure on that machine

None of these is proven by the code; the record the next failure leaves (`failed_step`, `detail`, `api_reads`,
the output tails) says which one it was.

1. **No GitHub token in the daemon.** `github_token()` reads `GH_TOKEN`, `GITHUB_TOKEN`, then `gh auth token`. A
   shell may export a token; the job's environment carries only what its definition names. `gh` is on the job's
   PATH only when the installer's PATH held it (`install_launchd.launch_path`), and `gh` keeps its token in the
   login keychain, which a daemon of an account with no GUI session may not reach. The apply then reads the
   release API anonymously (60 requests an hour per address), and a rate limit is exit 3. Measure: `api_reads`
   reads `["anonymous"]` and `detail` names the rate limit; `failed_step` is `resolve-release` or `rollback-wheel`.
2. **Proxy and certificate variables.** `HTTPS_PROXY`, `SSL_CERT_FILE` and the like reach a job only if its
   definition carries them (`install_launchd.fitted_env` keeps them, never adds them). A machine that needs one
   fails every API read from the job: `detail` names `release source unreachable` with an SSL or connection error.
3. **The interpreter.** The job runs `OBSERVATORY_PYTHON` (the venv's stable interpreter); a shell may run another
   `project-observatory`. Measure: compare `full version` from both, and the `current` field of the report.
4. **TMPDIR.** A daemon has none, so `tempfile` uses `/tmp`; the update's work folder and the apply's working
   directory are there. Not expected to fail; a `download` step failing with an `OSError` would point here.

## Failure behaviour

Recording never decides the outcome: `engine_update.log` swallows a failed write, and the report is still the
record. A secret in the output never reaches the record: every kept line goes through `credential_shape.redact`
and the URL-password rule, and the tests plant a token-shaped fake value and grep the stored files.

## Tests

- `observatory/engine/tests/test_engine_update.py`: `test_an_undetermined_apply_names_its_step_and_is_journalled`,
  `test_an_undetermined_download_names_the_download`, `test_a_refusal_and_a_rollback_name_their_steps`,
  `test_a_successful_apply_names_no_failed_step`, `test_jobs_in_the_system_domain_are_said_not_silently_left_running`.
- `observatory/engine/tests/test_maintenance.py`: `test_the_apply_keeps_the_last_lines_of_both_streams_redacted`,
  `test_an_undetermined_apply_leaves_its_reason_step_and_output_to_read`,
  `test_a_failed_apply_without_output_still_says_its_exit`,
  `test_an_in_flight_apply_that_ended_undetermined_is_read_with_its_reason`,
  `test_a_system_daemon_names_where_root_writes_its_logs`.
