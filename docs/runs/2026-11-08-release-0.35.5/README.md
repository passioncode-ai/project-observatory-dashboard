# Run 2026-11-08: release 0.35.5

Objective: release OBS-350 part 2, the restore side of the secret store kept outside the workspace, so a workspace
restored on another computer gets its keys back; then tag, verify and install it, and hand the «machine lost» drill
(G15, OBS-381) to the second Mac's host agent.

## State now

- Open: the «machine lost» drill on the second Mac with its host agent (G15, OBS-381); the install on the release
  manager's own machine, which answered `--version` 0.35.4 when these receipts were written; a restore check that can
  verify the carried store without touching the live one (OBS-394).
- Waits: release 0.35.6 waits on OBS-393's review (PR #474); no watcher of this record runs, the next release session
  reads the PR.
- Next task: release 0.35.6 with OBS-393 (PR #474, background jobs name no Python release) once its review is done.

## What it releases

- `secret_store_carry.py`: the restore plan and writes. The target comes from the restored settings after the home
  rewrite; `--secret-store {auto,keep,merge,replace,skip}`; a whole store written through a sibling stage
  `.<name>.restore-<hex12>` (folders 0700, files 0600 through `osprivacy`), verified, then renamed into place under
  the vault's own lock (`projects/.vault.lock`); merge adds each missing file by a hard link of a verified temporary
  (an exclusive copy where hard links are unavailable), never overwriting; replace and an empty folder move aside to
  `<target>.before-restore-*`, never deleted; refusals `secret-store-not-empty`, `secret-store-unresolved`,
  `secret-store-target-inside-home`, `secret-store-target-refused`, `secret-store-target-unconfirmed`,
  `secret-store-manifest-mismatch`; failure `secret-store-write-failed` leaves the target as it was.
- `workspace_upgrade.py`: the store is written before the workspace stage is renamed; `--secret-store-only`;
  `--secret-store-target`; `--json` refusals; a restore record `store/raw/secret-store-restore.json` (never in a
  snapshot) that doctor reads; the digest manifest of the first machine becomes a peer's in second-machine mode.
- `backup_vault.py`: doctor's `last_restore` and the warning `backups-secret-store-not-restored` with its step, cleared
  once the folder holds a vault store (a `projects/` folder with content). `workspace.py`: `full init` passes `keep`
  and says what happened to the store. `engine_update.py`: the update rollback passes `skip`.

## Decisions

- **0.35.5, a patch release**: additive flags and a new restore behaviour; no setting, schema or migration changes.
- **Never delete in the restore path**: an empty or replaced store is moved aside; doctor listing those copies is a
  follow-up row.
- **A journal that only grew since the snapshot reads `same`** (the spec counted every differing journal as a
  conflict; a store whose journal only grew is a superset, and the restore's own journal line would otherwise make a
  rerun refuse).
- **Foreign snapshots need confirmation**: a snapshot from another computer or home writes only where this computer's
  settings name the same folder, or where `--secret-store-target` confirms it.
- **The plugin stays 0.30.0.**

## Checks run

- Six review rounds on PR #470 (an independent review and five confirming rounds). They found and closed one HIGH
  (an "empty" folder holding a skipped-name folder was deleted), several MEDIUM (replace leaving no store during the
  copy, targets taken from the snapshot unchecked, a case-folded spelling of `~/.ssh` not refused, a digest checked
  after linking, skips under `keep` never shown, a doctor warning that fired falsely or was hidden by a refused
  target) and LOW findings; the last two rounds found nothing blocking.
- `tests.test_workspace_upgrade` class `RestoreSecretStore` 39 OK on Python 3.14; the link-loop, unreadable-place and
  record tests also under Python 3.11 (a real link loop and a real unreadable folder probed under 3.11); portable
  suites workspace_upgrade, workspace, maintenance, backup_vault, engine_update, machine_backup, gate_purity,
  vault_digests, osprivacy, i18n and handed_commands PASS; hosted CI on the PR found the handed-commands samples
  missing (fixed).

## Receipts

- Release PR #473 merged as `633f1b60` on 2026-11-08T07:06:52Z.
- Tag `v0.35.5` (tag object `89e3679c`) on `633f1b60`. Release run
  [42471332631](https://github.com/passioncode-ai/project-observatory-dashboard/actions/runs/42471332631): the first
  attempt failed only in `windows (arm64, windows-11-arm)` at "Bundle the NSIS installer", with
  `failed to bundle project: http status: 504` while downloading `nsis-3.11.zip` from github.com (a transient error of
  the download, not of the code); a rerun of the failed jobs succeeded, and every job of attempt 2 ended in success,
  the feed and publish jobs included. The release published 19 files on 2026-11-08T11:18:15Z.
- Verified by the release manager on 2026-11-08: `shasum -a 256 -c SHA256SUMS --ignore-missing` answered the wheel
  OK; `gpg --verify SHA256SUMS.asc SHA256SUMS` answered `VALIDSIG 63B30DC324BD697487AA31944FAFB8AEC803B6A7`;
  `gh attestation verify project_observatory-0.35.5-py3-none-any.whl -R passioncode-ai/project-observatory-dashboard
  --signer-repo passioncode-ai/.github` exited 0.
- On the second Mac: its host agent installed 0.35.5 into its spaces from a wheel downloaded with `gh` and checked
  against `SHA256SUMS`, because anonymous downloads from github.com answered 503 there.
- The release manager's own machine still answered `--version` 0.35.4 when these receipts were written (State now).
