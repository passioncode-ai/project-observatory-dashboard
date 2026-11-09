# Run 2026-11-08: release 0.35.7

Objective: release OBS-394, so a weekly restore check into a temporary home verifies a backup's carried secret
store too, without writing the live store, and the «machine lost» drill (G15, OBS-381) on the second Mac can run in
full; fix OBS-410, the server 0.35.6 left unloaded on a Mac after its update; then tag, verify and install it.

## State now

- Open: the install on the second Mac and the result of the «machine lost» drill there (G15, OBS-381), run by its
  host agent with `--secret-store-verify-into`.
- Waits: the second Mac's host agent, which received the install and the drill command; no watcher of this record
  runs, the next release session reads its answer.
- Next task: record the second Mac's install and drill result here, then release 0.35.8
  ([record](../2026-11-09-release-0.35.8/README.md)).

## What it releases

- `full restore --secret-store-verify-into DIR` (`secret_store_carry.py` `verify_into`, `check_verify_into`,
  `setting_folders`, `_verify_copy`; `workspace_upgrade.py` `restore`, `restore_store_only`; `maintenance.py`
  `restore_latest`): each carried store is written into `DIR/<name>/` (folders 0700, files 0600) and read back
  against the snapshot's manifest (every file's size and SHA-256, every folder and link, nothing extra); the answer
  reads `secret_store: verified` with counts and `verified`, the number of stores verified. The folder the
  snapshot's settings name is never written, locked or recorded, so no `--secret-store-target` is needed even for a
  snapshot from another machine, and doctor's restore record is not written. With `--secret-store-only` it needs no
  workspace and answers `nothing-to-verify` when nothing was carried; with `--latest` its checks run before the
  passphrase is asked.
- Refusals (exit 2, nothing left written): `secret-store-verify-into-not-empty` (DIR or `DIR/<name>` not empty, a
  link or a file), `secret-store-verify-into-refused` (DIR in or holding the new home, a workspace on this machine,
  the snapshot file, a backups folder or a secret store's folder, compared by name and by identity),
  `secret-store-verify-into-conflict` (with `--secret-store-target` or another `--secret-store` policy),
  `secret-store-verify-into-unchecked` (a workspace on this machine whose settings do not read). Failures (exit 1):
  `secret-store-verify-failed`, `secret-store-write-failed`; the copies and folders the run made are removed, the
  home is not restored.

- OBS-410 (`tools/install_launchd.py` `stop_job`, `start_job`; `maintenance.py` `step_jobs`; `engine_update.py`
  `recheck_writers`): found by the release manager installing 0.35.6 on 2026-11-08 (the server label gone from
  `launchctl list`, the port silent, the update's report clean; the server was brought back with
  `tools/serverd.py --install`). `full maintain ensure` rewrote the server's definition and reloaded it with
  `bootout` then `bootstrap`, which launchd refuses while the bootout is still finishing; the next pass leaves an
  unloaded job alone. A reload now waits until the label is gone (up to 45 s) and retries a bootstrap launchd
  refuses for now (exits 5, 37 and 125, up to five times); a reload that still fails is `reload-failed`; `full update`
  loads a job it started that ensure left unloaded, or names it under `services_not_restarted`; and a server 0.35.6
  left unloaded is loaded again once (`recover_lost_server`). Found by an independent review of the first fix:
  without it, installing 0.35.7 would not have brought such a server back.

## Decisions

- **0.35.7, a patch release**: one additive flag and a fix; no setting, schema or migration change.
- **0.35.7 goes out the same day as 0.35.6**: every Mac that auto-updates to 0.35.6 can lose its server. The
  update from 0.35.6 runs 0.35.6's code, which restarts only what is loaded, so 0.35.7 brings a lost server back
  itself: once, on a workspace updated to 0.35.6, its first `maintain ensure` (which the update runs) loads a server
  whose definition and Fabric descriptor are in place but whose label is not loaded (`jobs.recovered`).
- **The verified copy stays in DIR** for the caller to remove; the docs say it holds every key in plaintext.
- **No doctor record** for a verification: it must neither raise nor clear `backups-secret-store-not-restored`.
- **Only the snapshot file is avoided, not its folder**, so the transfer layout `~/obs-transfer/verify` works.

## Checks run

- Three review rounds on PR #476 (an independent review and two confirming rounds). The first found three MEDIUM
  gaps in the guard against a mistaken DIR (the same folder under another spelling, store folders the settings name
  missed, the live workspace's settings unread in a whole restore) and LOW findings; round 2 found nothing blocking
  and four LOW (a race in cleanup, the snapshot's whole folder refused, unreadable settings failing open, volumes
  without inode numbers), all fixed with tests; round 3 found nothing blocking.
- Portable suites on Python 3.14 (macOS): workspace_upgrade (150 cases), backup_vault, handed_commands, osprivacy,
  maintenance, also after merging 0.35.6; the new class and the whole `test_workspace_upgrade.py` on Python 3.11;
  hosted CI on PR #476 green before it merged as `47f3ca1c`.

- OBS-410: tests first (`test_lifecycle` classes `ReloadWaitsForLaunchd` and `ReloadFailureIsNamed`,
  `LostServerIsLoadedOnce`, `StartAfterOwnTimeout`, `test_engine_update` two cases for the recheck; they failed before the fix), then the portable suites lifecycle,
  maintenance, engine_update, serverd, osschedule, space_daemons, handed_commands and osprivacy on
  Python 3.14.

## Receipts

- Release PR #477 merged as `714cc099` on 2026-11-08T22:23:25Z.
- Tag `v0.35.7` (tag object `7257e122`) on `714cc099`. Release run
  [42534290023](https://github.com/passioncode-ai/project-observatory-dashboard/actions/runs/42534290023): the first attempt's `guard` refused because `check.yml` on `714cc099`
  was still in progress after its 60-minute wait (slow runners). That `check.yml` run's Windows job then failed once
  on `backup_vault: TIMEOUT, the list records only FAIL` (OBS-382, a known flake under load) and passed on rerun; the
  release's failed jobs were rerun and every job of attempt 2 succeeded. The release published 19 files on
  2026-11-09T01:38:30Z.
- Verified by the release manager: `SHA256SUMS` OK, `gpg` `VALIDSIG 63B30DC324BD697487AA31944FAFB8AEC803B6A7`,
  `gh attestation verify` of the wheel exited 0.
- Installed on the release manager's machine with `full update --version 0.35.7 --apply` (exit 0). The server came
  back by itself on 0.35.7: `serverd.out` shows 0.35.7 and the well-known answered 200.
- The second Mac's host agent was handed the install and the G15 drill command with `--secret-store-verify-into`; its
  result is open (State now).
