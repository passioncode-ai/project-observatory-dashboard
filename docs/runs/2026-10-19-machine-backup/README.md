# Run 2026-10-19: machine backup and restore (OBS-157)

Objective (operator request, 2026-10-19): a backup from which the whole starting state of an
agent machine can be moved to another machine, taken automatically once a day and before
updates, into a file in an agreed local place.

Base: `21e04267` (origin/main), merged with `ef9c8c33` (release 0.25.0, #303) on branch `feat/OBS-157-machine-backup`. This record is
committed with the implementation; resolve the final object id with `git log -1 --format=%H`
on that branch.

## Decisions

- **One root, one format, one passphrase.** The bundle is `machine-<stamp>-<id>.obmachine` in
  the same per-workspace folder of the backups root as the workspace snapshots, in `OBSENC1`
  (`backup_vault.encrypt_stream`, new). The tar is streamed into the encrypting writer, so no
  plaintext copy of what it carries is written beside the root.
- **No passphrase, no bundle.** Workspace snapshots fall back to plaintext copies inside the
  workspace; a machine bundle holds `~/.ssh` and CLI logins, so it is never written
  unencrypted anywhere.
- **Generic built-in manifest, private extras.** The code names only well-known tool locations.
  An operator's own agents and paths go in `<home>/config/machine-backup.json` (`include`,
  `exclude`, `roots`, `transcripts`, `enabled`, `limits`), validated by name.
- **Checkout roots come from the engine's own configuration** (`sources.projects` and the
  declared local folders) unless `roots` is set: no machine-specific default in public code.
- **Caps, then names.** 16 MB per file, 128 MB per entry, 320 MB of file copies (raised in
  fix cycle 1 so a normal machine's skills are carried, not warned about daily), 16 MB per
  checkout bundle (estimated with `rev-list --objects` and `cat-file` sizes before it is written), 8 MB per patch or untracked set, 64 MB for checkouts. What a cap leaves out
  is named once per top-level folder of its entry (the first measurement listed 5,888 files
  one by one; aggregated it is 205 lines).
- **Schedule.** Daily from the maintenance pass after the workspace snapshot, with every job
  running (it reads the home, not the workspace); a failure or a disk refusal is retried an
  hour later. Before an engine update, before the jobs stop, unless a bundle under an hour old
  exists; the update never fails on it (`Dependencies.machine_backup`, reported in the update's
  answer and `degraded`). Scheduled and pre-update bundles run only for the account's real
  home and never with `OBSERVATORY_SYSTEM_SETUP=0`, so a test or sandbox never backs up a
  machine.
- **Disk floor.** `min_free_gb` 2: checked before collecting and again with the estimated size.
- **KEEP 3.** A measured bundle is 69 MB; three are about 210 MB.
- **Backups root follows `root_info`** (operator decision O15: the root moves to the per-user
  data directory in REL-BK/OBS-20); the `machine` kind never names a folder of its own.
- **Restore uses plain git** with the person's own configuration (SSH keys, credential
  helpers); the backup side reads checkouts through `safe_git`.
- **Adjacent defect.** `backup_vault.SNAPSHOT_KINDS` lacked `before-update`, so `backups
  migrate` never exported a local before-update folder, rotation skipped it, and `doctor` kept
  asking for a migrate that could not clear it. This branch fixed it; #303 (release 0.25.0)
  landed the same fix on main meanwhile, so the merge keeps main's list and this branch adds the
  `machine` kind and the tests (export, verification, removal, rotation of before-update).
- **Not solved here:** OBS-20 (the backups root in `~/Documents`, a privacy-guarded folder:
  the bundle follows the root) and OBS-19 (the jobs' privacy consent: a file the job may not
  read is named as not carried).
- `docs/HANDOFF.md` is not edited in this branch: two other builders change it in parallel;
  the pointer is set at merge.

## What shipped

- `observatory/engine/machine_backup.py`: manifest, inventories, checkouts, bundle, rotation,
  disk floor, state (`store/machine-backup.json`), status, restore (plan, apply, overwrite,
  packages, clone), `full machine-backup [now|status]`, `full machine-restore`.
- `backup_vault.py`: the `machine` kind in `backups status`; `encrypt_stream`.
- `maintenance.py`: `step_machine` in the pass, the bundle's warnings in `maintain status`.
- `engine_update.py`: the bundle before the jobs stop.
- `workspace.py`, `observatory.py`: the two commands, `doctor.machine_backup`.
- Dashboard Health: a "machine backup" row and five warning codes, in English and Russian.
- Docs: ONBOARDING (what a bundle holds, the settings file, moving step by step),
  AGENT-ONBOARDING, COMPATIBILITY (`.obmachine`), CLI-COMPATIBILITY, scenario OSS-70.

## Checks run

- `observatory/engine/tests/run_portable.py --jobs 3`: 260 suites, 257 PASS and 3 FAIL on the
  first run (a doc path read as a repository path, a test value shaped like a secret, an
  `os.open` outside `osprivacy`); after the fixes those three and `machine_backup` PASS.
- New suite `tests/test_machine_backup.py`: 24 cases, among them the round trip of a synthetic
  home into an empty one (every carried file's bytes and mode, links, `~/.ssh` 700) and the
  clone of three checkouts with their unpushed commits, uncommitted change and untracked file.
- `tests/test_backup_vault.py`: a local before-update snapshot is exported, verified and
  removed (failed before the fix, passes after); `tests/test_engine_update.py`: the bundle is
  taken before any job stops, and a failed one leaves the update `updated` and degraded.
- Root tests `test_backlog_board`, `test_engine_doc_copies`, `test_update_inventory`,
  `test_full_cli`, `test_version_consistency`, `test_release_boundaries`: 110 OK.
- `tools/update_inventory.py --check`, `tools/sync_engine_docs.py --check`,
  `tools/check_backlog.py` (with the maintainer's private denylist): 0 problems.
- `tools/check_public_release.py` with the private denylist: no finding in a changed file; two
  remain outside this change (an excluded local `.claude/settings.local.json`, and a
  private-identifier hit in `collectors/merge.py` already on main).
- Measured on a working agent machine into a throwaway root, removed afterwards: 46.9 MB,
  8,235 items, 456 checkouts, 205 not-carried lines (skill folders over the per-entry cap),
  65 to 98 seconds. After fix cycle 1: 68.9 MB, 14,221 items, 517 checkouts, 21 not-carried
  lines (over the per-checkout caps, and one repository whose source lacks an object), 106 s.

## Fix cycle 1 (adversarial round 1 on #305)

- M1, commits outside branches: one bundle per repository is built in a temporary bare
  repository that borrows the checkout's objects (alternates, plus its `shallow` file) and
  names every local tip: branches, tags, each worktree's detached HEAD
  (`refs/carried/head/<id>`) and each stash entry (`refs/carried/stash/<n>`); full ref names
  and `^object` exclusions go to git on stdin. Tag names are recorded so a tag on a pushed
  commit comes back too. A repository with no commit carries `git diff --cached` as
  `staged.patch`. Restore fetches the carried refs, stores the stashes oldest first, detaches
  HEAD where it was detached, restages an unborn repository's index, then deletes
  `refs/carried`. On this machine 99 of 100 repositories with local-only commits bundle; the
  one left names an object missing from its own source as not carried.
- M2, plaintext left by a killed run: every decrypted or temporary copy lives in
  `<home>/store/.machine-stage/` (0700, locked, swept at the start and end of every backup and
  restore, legacy `store/.machine-backup-*` too); a restore decrypts one step's members at a
  time; SIGTERM and SIGHUP raise; bundle temp files are 0600.
- LOWs: L1 size estimate before writing and against the free floor; L2 fixed patch prefixes;
  L3 untracked modes; L4 submodules told from worktrees; L5 every record, arcname, ref, remote
  and path from a bundle validated before use; L6 the suite stubs free space and makes the KDF
  cheap; L7 caps raised; L8 `_destination` traversal tests.

## Fix cycle 2 (confirming round on #305)

- A run refused the stage lock (a second restore, even `--plan`, the daily step, the
  pre-update bundle) swept the live restore's stage: `_staging` now locks first and sweeps
  only with the lock. `clone` names a carried file missing from the unpacked bundle
  (`carried-missing`), `restore` lists every checkout not restored in `failed`, and
  `full machine-restore` exits 1 when there is one.
- The stage used before a workspace exists is swept too, under its own lock, at the start of
  every run. A main checkout already on the new machine gets the carried commits (under
  `refs/carried` only) before a detached worktree is added to it.

## Open

- The Health row is not rendered by an automated test.
- `forget`'s receipt counts machine bundles among the encrypted backups that keep memory text;
  they do only with `transcripts: true`.

## Next task

Review and merge the pull request; then release, so the operator's installed engine takes its
first daily machine bundle at the next maintenance pass.
