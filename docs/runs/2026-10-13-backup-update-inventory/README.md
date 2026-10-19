# Engine-update backup inventory and migration

## State now

- Objective: make the existing local `before-update` snapshot kind visible and eligible
  for the same verified migration as other engine snapshots, without taking ownership
  of foreign directories.
- Baseline: source `8aff34f801a4154882d536bba0472b6549b041e7` emitted update snapshots
  through `engine_update.py` update transaction `finish` / `backup_vault.after_snapshot`, and encrypted
  discovery already recognized them. The `SNAPSHOT_KINDS` migration allowlist and the
  separate local status expression omitted that kind.
- Reproduction: all three `test_backup_vault.BeforeUpdateInventory` tests failed before
  correction: status counted zero, valid migration exported zero, and an invalid update
  copy was absent from `skipped`. These were synthetic fixtures, not live backup reads.
- Correction: one known-kind tuple now supplies local counts, migration, encrypted
  status and restore candidate discovery. No storage configuration or automatic
  migration changes. The existing passphrase requirement and three-per-kind verified
  migration/retention contract remain in force.
- Evidence: `BeforeUpdateInventory` checks status both without and with a synthetic
  passphrase; encryption/authentication and snapshot verification after extraction;
  foreign-kind byte preservation; and invalid-update reporting without deletion.
- Checks: the complete `backup_vault` portable suite passes 36 cases with no skips;
  six document-copy cases, source-inventory and whitespace checks pass.
- Open: independent source acceptance and protected integration. No installed backup
  inventory, real encryption key or real backup migration was exercised here.
- Next: independently replay the known-kind/foreign-kind boundary, then integrate into
  a separate follow-up branch. The prepared release source is not changed by this branch.

## Ownership boundary

Only `snapshot`, `before-upgrade`, `before-update` and `daily` are known snapshot
kinds. An unrecognized directory is outside the reported known-snapshot count and is
not migrated or pruned. This change does not establish a complete disk inventory,
verify ownership of arbitrary backup-shaped paths, or authorize operational deletion.
Database-copy handling and the existing backup encryption format are unchanged.
