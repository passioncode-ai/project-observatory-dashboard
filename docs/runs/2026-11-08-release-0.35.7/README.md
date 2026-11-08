# Run 2026-11-08: release 0.35.7

Objective: release OBS-394, so a weekly restore check into a temporary home verifies a backup's carried secret
store too, without writing the live store, and the «machine lost» drill (G15, OBS-381) on the second Mac can run in
full; then tag, verify and install it.

## State now

- Open: tag `v0.35.7` once the release check of this PR is green; verify the published files; install on the release
  manager's machine and in every space of the second Mac; hand the drill commands to the second Mac's host agent.
- Waits: the hosted checks of this PR, read by the release manager's session; no scheduled watcher of this record.
- Next task: tag, verify and install 0.35.7, then fill the Receipts below and run the drill with the second Mac.

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

## Decisions

- **0.35.7, a patch release**: one additive flag; no setting, schema or migration change.
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

## Receipts

- Pending: the tag, the release run, the verification and the installs.
