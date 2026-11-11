# Run 2026-11-11: release 0.35.12, the scenario sweep

Objective: walk every shipped scenario of the engine and its apps on the installed 0.35.11, fix what is broken in
waves, and release one build for every platform (the operator's request of 2026-11-11).

## State now

- Open: none for the release; the maintenance job installs 0.35.12 on the other machines.
- Waits: none.
- Next task: OBS-504 (the tick's dashboard step cut at its time limit leaves the pages hours old), then OBS-506 (the
  release gate does not run the desktop tests), OBS-505, OBS-507 and OBS-508.

## How the scenarios were walked

Six read-only surveys, each against the scenario text, the named tests and the installed 0.35.11, with a receipt
(a command and its output, or `file:line`) for every finding:

| Survey | Scope | Confirmed |
|---|---|---|
| A | dashboard OSS-13 to OSS-40, UI-01 to UI-09 | 5 (Machine page, number formatting, commands without the workspace, HTML descriptions) |
| B | operating the engine, OSS-41 to OSS-61, cleanup | 6 (the disk guard and cleanup remove nothing, ENOSPC, findings, scenario text) |
| C | SSH keys, person surfaces, OSS-62 to OSS-101 | 5 (secret store carry, doctor steps, ssh-keys note, status lines) |
| D | organizations, OSS-102 to OSS-113, ORG-R01 to R30 | 3 (degraded state, cut reasons, preview count) |
| E | the Mac, Windows and Linux apps, the release workflows | 4 (silent failures, Start server enabled, CI comment) |
| F | every suite, the portable CLI O1 to O9, the MCP server | 3 (short `.env` values corrupt the saved snapshot, doctor and symlinks, a fixture) |

Two more came from the board: a peer's finding that `full cleanup --apply` removed nothing at 0 bytes free (thread
5b32f39e), and a request to shorten the observatory-log skill descriptions (c52d8576). A third came from watching
the live install: the daily snapshot failed with "Workspace changed during snapshot" since 2026-11-10.

## What it releases

One pull request, #497, built in three waves and merged as one:

- Wave 1, the disk: one complete scoped `lsof` and `ps` listing proves a path free (`holders.take_complete`,
  `fresh_busy` can answer False, `running` answers False from a whole listing), so cleanup and the guard remove
  again (OBS-173, OBS-327, OBS-397); the guard keeps working at ENOSPC; an ended coding session's folder goes by
  hours (OBS-500); a guard without holders raises a warning finding.
- Wave 2: the dashboard fixes (OSS-14, OSS-19, OSS-21, OSS-26), the desktop fixes (SCN-007, SCN-008), the
  organization fixes (OSS-102, OSS-106, ORG-R14).
- Wave 3: the secret store carry (OSS-100), doctor steps (OSS-99), the ssh-keys note (OSS-63), the portable CLI
  (O4, O7, O8, O9), the daily snapshot and the check job's record (OSS-43), test fixtures and runner timeouts.

Every user-visible change has its line in `CHANGELOG.md` under 0.35.12.

## Checks run

- `observatory/engine/tests/run_portable.py --jobs 4` on the merged tree: 315 of 315 suites PASS (2026-11-11).
- Root `python -m unittest discover -s tests`: 346 tests OK, 1 skipped.
- `tools/update_inventory.py --check`, `tools/sync_engine_docs.py --check`, `tools/check_backlog.py`,
  `observatory/engine/tools/check_docs.py`, `observatory/engine/tools/check_paths.py`: pass.
- The privacy gate on each pushed head.
- The first CI round of #497 failed only in `i18n` (a finding title without its Russian), and #498's only in
  `workspace_redesign` (spend lost its third place); both fixed before the full local run above.

## Not fixed here (rows)

OBS-504 to OBS-508, each with its receipt in `docs/backlog.md`: findings the surveys saw but could not prove as a
defect of this release, or that need a decision beyond it.
