# workspace-backup --pause-writers (OBS-583)

## What

`project-observatory full workspace-backup --pause-writers` takes the snapshot the daily step of `maintain` takes,
now, whatever its cadence. For a workspace whose tick and server always run (the maintain schedule live), so a
scheduled job of the person's can get an encrypted snapshot younger than an hour without stopping the jobs by hand.

## Data

- One function, `maintenance.snapshot_pausing_writers`, serves both callers: `step_snapshot` (after its cadence and
  back-off checks) and `maintenance.backup_pausing_writers` (the flag). It defers while a tick or another workspace
  operation holds its lock, holds `engine_update.update_lock` from the stop to the start, stops each loaded managed
  job (recorded in `stopped_services` before the stop), calls `workspace_upgrade.snapshot(writers_stopped=True,
  kind="daily")` with the daily step's retries, and starts the jobs again in a `finally`.
- The outcome lands where the daily one does: `snapshot` (or `snapshot_failure`) in `store/maintenance.json`, so
  doctor, Health and the next maintain pass read it. The flag also ensures the backup passphrase as a pass does, so
  the copy is `OBSENC1`.

## Contract

- stdout: the JSON answer `{result: taken, encrypted, file, at, stopped: [job names]}` and exit 0.
- Refused before anything is stopped, exit 2: a maintenance pass running (`maintenance-running`, the flag takes the
  pass lock), a tick running (`tick-running`), an update running (`update-running`), `--output` or `--plaintext`
  given. With `--json` the refusal is `{"error": {"code", "message"}}` on stdout.
- A snapshot that fails after the jobs were stopped: the jobs are started again, the failure is recorded as the
  daily one's (it sets the daily step's back-off), exit 1 with `snapshot-failed`.

## Failure behaviour

A pass killed between the stop and the start leaves `stopped_services`; the next pass or the next flag starts them
(`restart_left_stopped`). A job that does not start again is named in `services_not_restarted`.

## Tests

`observatory/engine/tests/test_maintenance.py`, class `PauseWriters` (suite `maintenance`): the jobs stopped and
started around an `OBSENC1` snapshot recorded in the state file; the daily step and the flag through the one
function; refusals with an update, a tick or a pass running, nothing stopped; a failure starts the jobs again and
exits 1; no scheduler means nothing to stop; the command line. Scenario: OSS-130.
