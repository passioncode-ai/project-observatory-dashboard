# A job cancel asks before it stops (OBS-590)

## What

`fabric.job.cancel` used to stop a job's runner with a signal: SIGTERM to its process group on
POSIX, which `tools/run_job.py` turns into a `SystemExit` so cleanup runs, and `taskkill /T /F`
on Windows, which runs no handler at all. A cancelled secret prompt therefore kept its request
on Windows until its deadline. A cancel now asks first, the same way on every OS, and forces
only what does not answer.

## Data

The job record (`<state>/jobs/<id>.json`) is the signal; no new file.

- `cancel: "cooperative"`: written by the runner through `jobs.listen` once it checks for a
  cancel between its polls.
- `status: "cancelled"`: written by `jobs.cancel` before any process is touched.

## Contract

```
caller                jobs.cancel                         runner (listening)
  |-- job.cancel -->  record := cancelled  (under lock)
  |                   listening? wait for the runner's exit  jobs.pause sees "cancelled"
  |                   (at most COOPERATIVE_SECONDS, 10 s)    raises jobs.Cancelled
  |                                                          cleanup (secret.cancel), exit
  |                   still alive? forced stop (SIGTERM / taskkill)
  |<-- cancelled ---
```

- `jobs.listen(job_id, **fields)`: the runner's word that it listens; the fields go in the same write.
- `jobs.pause(job_id, seconds)`: the runner's sleep between polls; returns after `seconds`, or
  raises `jobs.Cancelled` (a `SystemExit`, the exception the SIGTERM handler raises, so every
  cleanup written for the signal runs for it too) as soon as the record says `cancelled`.
- `jobs.cancel(job_id, *, wait, clock, sleep)`: the clock and the sleep are injected for tests.

Runners and how they stop:

| Capability | Listens | Stop |
|---|---|---|
| `secret.request` (`secret_client.follow`) | yes, between its `secret.get` polls | its own exit after `secret.cancel` |
| `agent.ask` (`assistant.run_question`) | no: one blocking model call | forced at once, as before |
| `machine.mcp.refresh` (`run_job.refresh`) | no: one blocking collector run | forced at once, as before |

## Failure behaviour

- A runner that listens and never looks (hung in a call) is stopped by force once the wait ends.
- The race before a runner listens (a cancel right after the start) is a forced stop, as before;
  the secret runner listens before its status says it is waiting.
- The forced stop on POSIX stays SIGTERM only, unchanged.

## Tests

`observatory/engine/tests/test_jobs_cancel.py` (suite `jobs_cancel`, every OS): a listening
runner does its own cleanup and is never forced; a runner that listens and never looks is
stopped after the bounded wait (clock injected); a runner that never listened is stopped at
once without a wait; `pause` and `listen` against the record alone. End to end:
`test_secret_prompt` `TheAgentsSide.test_cancelling_the_job_withdraws_the_request`, no longer
listed in `tools/windows-known.json`.
