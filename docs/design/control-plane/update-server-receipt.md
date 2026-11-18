# The local server's receipt during an update (OBS-591)

What loaded the job (an agent session acting on a doctor's `fail` row) and the server's wait for the update lock are
in [runtime-receipts.md](runtime-receipts.md#the-server-loaded-again-during-an-update-obs-593) (OBS-593).

Since OBS-610 the receipt is declared in `runtime_receipts.py` (`SERVERD`, with `belongs` doing what
`foreign_runtime_receipts` did) and the stillness checks read that registry: [runtime-receipts.md](runtime-receipts.md).

## What happened

On one machine the automatic update from 0.39.1 to 0.40.0 rolled back (2026-11-17, UTC). The workspace's
`store/logs/update.jsonl` read: `stopped` tick 05:09:32, `stopped` server 05:09:33, then at 05:24:51
`workspace-restored`, tick `started`, server `restarted` at 05:25:02, and `undo-ended` `rolled-back` with
"Workspace changed while upgrade was staged; stop all writers (changed: store/raw/serverd.json)".

## The cause, and what it was not

- **Another process loaded the server's job again while the upgrade was staged.** The update writes `restarted` only
  when its own `bootstrap` meets a job that is already loaded (`engine_update.Update.start_writers`); it had booted the
  job out at 05:09:33 and waited until launchd no longer knew the label (`install_launchd.stop_job`). The failed copy
  the restore set aside (`<home>.failed-update-<stamp>`) holds the receipt that moved: pid 70074, version 0.40.0,
  `uptime_s` 319 at 05:23:25, so a server of the NEW release started at about 05:18:06, after the new wheel was
  installed (05:14:14) and while `upgrade --apply --writers-stopped` was staging; its heartbeat rewrote the receipt,
  and the stillness check refused. No engine code loads the server in that window (the maintenance pass ran at
  04:47 and 05:41, the OBS-410 recovery had already decided on 2026-11-09, and the machine's lifecycle broker does
  not enrol the server), so the loader is outside the engine: a host's start button, an agent, a person.
- Not a shutdown write of the stopped server (a): the moved receipt names another pid and the new version.
- Not a stop that returned early (c): `stop_job` returns only once the label is gone, and launchd keeps the label
  until the process has exited.
- No other engine process writes `serverd.json` (b, narrow): `peer_check`, `local_ports`, `dashboard_open`,
  `maintenance.live_clients` and `build_findings` only read it; `serverd.py --once` is a test entry point.

## The choice

Waiting for the server process to exit before staging would not have helped: the stop had already waited, and the
writer was a new server started later. Disabling the label for the length of the update would hold off such a
loader, but a disabled label that outlives a crashed update is a server that stays off, which Fabric Dashboards and
`recover_lost_server` both read as the operator's stop (LC-14). So the receipt is classified instead:

- `workspace_upgrade.RUNTIME_RECEIPTS` (`store/raw/serverd.json`, kept equal to serverd's `RECEIPT` by a test) is
  volatile for the snapshot and for the staged-upgrade check, like the session hooks' and the check job's receipts.
  It is a runtime record: the restored or upgraded server rewrites it at its start, and nothing restores from it.
  The atomic writer's temporary file beside it (`.serverd.json.<random>.tmp`) is volatile too.
- **The guarantee holds.** The exemption applies only while the receipt is this workspace's own server's: a JSON
  object whose `workspace` is this home (`foreign_runtime_receipts`). Anything else written there (another home's
  receipt, a file that is no receipt) is held still and refuses, naming the path. The port is not part of the test:
  one workspace has one server behind its instance lock, and `full ports` may move it, so a receipt with another port
  and this home is still this workspace's. Every other file keeps the check.

What the exemption does not cover: a server running mid-upgrade still reads an unmigrated store until it is
restarted, which the update does (`restarted`). Anything else such a server writes outside `store/logs` would still
refuse the update.

## Tests

- `tests/test_workspace_upgrade.py`: `test_the_local_servers_receipt_may_change_while_staged` (the real
  `serverd.heartbeat` writes during staging; refused before this change with the incident's message),
  `test_a_server_receipt_of_another_workspace_still_refuses`, `test_a_server_receipt_on_another_port_is_still_this_workspaces`,
  `test_the_local_servers_receipt_may_change_during_a_snapshot`, `test_a_foreign_server_receipt_during_a_snapshot_still_refuses`,
  `test_the_runtime_receipts_are_the_files_the_server_writes`, `test_what_counts_as_volatile`.
- `tests/test_engine_update.py`: `test_a_server_loaded_again_while_the_upgrade_is_staged_leaves_the_logged_sequence`
  reproduces the logged sequence with an injected service controller (no launchd).
