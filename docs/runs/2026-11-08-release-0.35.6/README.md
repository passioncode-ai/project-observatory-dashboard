# Run 2026-11-08: release 0.35.6

Objective: release OBS-393, so the background jobs, the plugin hooks and the per-install manifest keep working when
the engine's virtual environment is rebuilt on another Python minor version; then tag, verify and install it on both
Macs of the move.

## State now

- Closed: 0.35.6 is tagged, published, verified and installed on the release manager's machine (Receipts). The
  server it left unloaded on a Mac was brought back by hand and fixed in 0.35.7 (OBS-410).
- Waits: none for this record.
- Next task: none here; the release line continues in [release 0.35.8](../2026-11-09-release-0.35.8/README.md).

## What it releases

- `observatory/job.py` (new): `python -P -m observatory.job maintain|tick|tick-py|serverd|mcp ARGS` finds the engine
  installed now and replaces itself with the program the old definitions named (same process, arguments and
  signals; SIGPIPE and SIGXFSZ reset to their defaults first; on Windows it runs the script in the same process).
- `tools/install_launchd.py`: definitions of an engine installed inside a `pythonX.Y` folder name
  `<venv>/bin/python3 -P -m observatory.job PROGRAM` with the workspace home as working folder; a checkout and a
  Windows venv keep naming their files; no definition is written for an interpreter outside the engine's venv or a
  `bin/python3` that is missing or a broken link. Repair compares the whole definition and merges it: the tick's
  interval, the server's port, a `PATH` without Cellar or foreign-writable folders and every key or variable the
  installer does not write are kept, the server's retired `LowPriorityIO` and `Nice` are dropped, a drift that
  cannot be mended is reported as skipped, and a job that was not loaded stays unloaded. The systemd tick and server
  units on Linux are repaired too (only `Environment=` variables are kept there). Status, uninstall and run-now work
  with a broken venv.
- `maintenance.py`: `full maintain ensure` repairs the pass, the check, the tick and the server; in an account with
  no GUI session a root daemon still running an older definition keeps answering `waiting-for-root` with the two
  administrator steps; doctor names `job-path-missing` and `venv-home-missing`, reading the workspace's own labels.
- The observatory-log plugin moves to 0.35.6: its hooks keep `OBSERVATORY_ROOT`, find the engine under the venv's
  `pyvenv.cfg` release when the root is gone, and `full maintain ensure` refreshes hook settings that name a folder
  that no longer exists; the handling-secrets skill falls back to `project-observatory full-path`.

## Decisions

- **0.35.6, a patch release**: no setting, schema or migration change; definitions are rewritten by the next pass.
- **Keep `OBSERVATORY_ROOT`** in the hook settings: removing it would have left installed copies of the old hooks
  recording nothing until their plugin updated.
- **A gone `pyvenv.cfg` home does not refuse a working interpreter**: measured, such a venv can still start through
  its `bin/python3` link; doctor warns instead.
- **Windows tick and server are not migrated**: a Windows venv path names no Python release.

## Checks run

- Three review rounds on PR #474 (an independent review and two confirming rounds). The first found two HIGH (the
  plugin version not bumped while the hook root was removed; the Linux tick and server neither repaired nor
  checked), several MEDIUM (a root request repeated over a caller's `PATH`, repair dropping keys a person added, a
  Cellar `PATH` blocking repair, unloaded jobs started, a foreign interpreter written) and LOW findings; all were
  fixed with tests, and the last two rounds found nothing blocking.
- Portable suites on Python 3.14 (macOS): lifecycle, maintenance, agent_plugin, serverd, publish_contract,
  fabric_service, interop, handed_commands, osprivacy, gate_purity, workspace, session_start, skill, skill_check,
  companion_hook, companion, engine_update, tick_py, cli_compatibility, docs_current, space_daemons, osschedule,
  plugin_reporting, key, node_lifecycle_service, workspace_scheduler, machine_backup, dashboard_stop; the new test
  classes also on Python 3.11; repository tests job_launcher, full_cli, plugin_manifests, version_consistency,
  changelog_releases, engine_doc_copies, docs_drift, update_inventory, release_boundaries, check_windows_suites.
- Hosted CI on PR #474 at `ff5a28c0`: every job of the check workflow passed (Linux 3.11 and 3.14, macOS 3.14,
  Windows); the release check failed only because the OBS-393 row still read open, fixed here.

## After installing

`project-observatory full maintain ensure` rewrites the definitions (an account with no GUI session also needs the
two administrator steps it prints: remove, then apply); `project-observatory full doctor` shows no
`job-path-missing`.

## Receipts

- Release PR #474 merged as `db205ebb` on 2026-11-08T14:42:35Z.
- Tag `v0.35.6` (tag object `236eb862`) on `db205ebb`. Release run
  [42494466480](https://github.com/passioncode-ai/project-observatory-dashboard/actions/runs/42494466480): every job succeeded on the first attempt; the release published 19
  files on 2026-11-08T16:04:40Z.
- Verified by the release manager: `shasum -a 256 -c SHA256SUMS --ignore-missing` answered OK; `gpg --verify
  SHA256SUMS.asc SHA256SUMS` answered `VALIDSIG 63B30DC324BD697487AA31944FAFB8AEC803B6A7`; `gh attestation verify`
  of the wheel exited 0.
- Installed on the release manager's machine: `full update --version 0.35.6 --apply` exited 0, and all four job
  definitions name `<venv>/bin/python3 -P -m observatory.job …` and no `lib/python3` folder.
- Regression found there: after the update the server's label was missing from launchd (the port silent, the update's
  report clean). It was restored with `tools/serverd.py --install` (the well-known answered 200) and fixed in 0.35.7 as
  OBS-410.
- The second Mac's spaces updated to 0.35.6 by themselves; they run no server job (no GUI session, scheduler off),
  so they were not affected.
