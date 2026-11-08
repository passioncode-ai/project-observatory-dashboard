# Run 2026-11-08: release 0.35.4

Objective: a fix wave for workspaces that move between machines, Python versions and accounts with nobody signed in,
found while the second Mac took over services (OBS-387, OBS-361 part 2, OBS-388 to OBS-392); then tag, verify and
install it.

## State now

- Open: the release PR, then the tag `v0.35.4`, its run, verification and the local install; then the host agent of
  the second Mac updates both spaces and puts them on one Python minor version.
- Waits: none beyond the release run (watched by the releasing session's background job).
- Next task: merge the release PR, push the tag, verify, install, record the receipts here; then OBS-350 part 2 (the
  restore side of the carried secret store, the gap that keeps the «machine lost» drill open) and OBS-393.

## What it changes

- `store/migrate.py` (OBS-387): a migration's recorded checksum is `v2:` and a digest of its source text without
  comments, docstrings (cut by their exact span) and layout outside string literals; the AST digests earlier releases
  recorded are listed per migration and interpreter (3.11, 3.12, 3.13 and 3.14, 3.15), pinned to the `v2:` digest of
  the code they attest, read and never rewritten.
- `maintenance.py` (OBS-361 part 2): in an account with no GUI session (`launchctl print gui/<uid>` 112 or 125) the
  maintenance and check jobs go to the background folder for the administrator's space daemons tool; the answer is
  `result: waiting-for-root` with the administrator's command; the plugin hook runs the pass until root loads them; a
  copy in `~/Library/LaunchAgents` stays until then; a daemon root runs is never doubled by an agent; doctor names
  the administrator's `apply`.
- `backup_vault.py`, `machine_backup.py` (OBS-388, OBS-391): backups ordered by the order they were made (header
  creation time, then mtime, then name), the backup just written never rotated away; the passphrase's outside copy
  named where it is (the owner-only file where no Keychain takes it), and moved into the Keychain once it does.
- `workspace_upgrade.py` (OBS-389): `secret_stores_inside()` lists the vault stores inside the workspace, the node
  folder (device, organization, share and relay keys) and provider key files; `--output` over them needs `--plaintext`;
  unreadable settings fail closed. The keyserver token is not counted (reason in `STATE_SECRET_FILES`).
- `machine_move.py` (OBS-390): a staged definition naming a missing program or working directory is refused as
  `staged-plist-stale` before anything moves; `ProgramArguments[0]` is checked only when `Program` is absent.
- `tests/test_machine_backup.py` (OBS-392): the signalled-restore test waits with a hang bound and asserts the exit
  status.

## Decisions

- **0.35.4, a patch release with one documented command change**: `workspace-backup --output` over a workspace that
  holds secrets needs `--plaintext` (announced on the board, thread c1a45468, before release).
- **Digests are read, never rewritten**: an earlier release keeps opening a database it wrote on its own interpreter;
  a database first created by 0.35.4 records `v2:` and needs 0.35.4 or later (documented as a downgrade).
- **The keyserver token stays out of OBS-389's count**: every workspace has one, a restore must carry it unchanged,
  and it opens only this machine's peer-checked loopback.
- **PR #468 lands here**: its commits are part of this release PR, and #468 closes as landed.
- **The plugin stays 0.30.0.**

## Checks run

- Each fix had an independent review and a confirming round (findings fixed in the branch commits named in the
  release PR). Python 3.11, 3.12, 3.13, 3.14 and 3.15.0rc3: `tests.test_schema_compatibility` OK, and the `v2:`
  digests of the ten migrations and of edge-case functions identical.
- Portable suites on the branches: maintenance, backup_vault, machine_backup, backup_store, workspace_upgrade,
  engine_update, workspace, cli_compatibility, store_integrity, store_faults, store_modes, workspace_boundaries,
  workspace_profile, dashboard_store, schema_compatibility PASS (machine_backup timed out once at 600 s under load
  and passed with a longer bound).
- Root release suites (version consistency, desktop app, plugin manifests, changelog, backlog board, docs drift,
  engine doc copies, release boundaries, licensing, analytics catalogue): 171 OK. `check_backlog.py` 0 problems,
  `check_docs.py` current, `check_paths.py` 0 violations, `update_inventory.py --check` passed.

## Receipts

To be recorded after the release run.
