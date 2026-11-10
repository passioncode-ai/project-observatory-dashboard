# Run 2026-11-10: release 0.35.10

Objective: release a vault slot copied into another store on this machine (OBS-476) and the skills section of the
machine view (OBS-477, PR #488), space daemons 1.2.0 with a one-step `replace` (OBS-475, PR #486), and the desktop
updater on `zip` 9.5.0 (PR #490); then tag, verify and install it.

## State now

- Open: tag `v0.35.10` once this release PR is merged and its `observatory-release-check` is green; verify the
  published files (`SHA256SUMS`, its signature, the attestations), install on the release manager's machine and let
  the maintenance job install it elsewhere; an administrator installs the root-owned space daemons copy again from
  this release (below); fill the Receipts below.
- Waits: this release PR's review and release check; no watcher of this record runs, the release manager tags.
- Next task: OBS-480 (the port forward `full machine-move leave` installs for a service with a local origin), then
  OBS-492 (the desktop app's true minimum Rust version) and OBS-493 (`vault.py copy` keeps `target-other-account`
  when the owner cannot be explained).

## What it releases

Merged to `main` since `v0.35.9`, in order:

- PR #486 (`dabe3825`), OBS-475 and OBS-395 part 2, space daemons 1.2.0: `remove` answers once launchd has let the
  label go (45 s, or the daemon's own ExitTimeOut and 5 s when that is longer, at most 300 s); the new
  `replace --user ACCOUNT --label LABEL` checks the new definition, boots the old daemon out, waits, loads the new one
  and puts the previous daemon back when it does not load; `apply` over its own loaded daemon replaces a changed one
  the same way and leaves an unchanged one running. Board rows OBS-482 to OBS-485 from its reviews.
- PR #489 (`290fddda`, documentation in `dispatch_packet.py`): the deadline ref may be cut to the dispatcher's
  horizon.
- PR #490 (`897065d3`, security): `tauri-plugin-updater` 2.13.3 to 2.14.1 in the desktop app's lock file, which
  pulls `zip` 9.5.0 instead of 4.6.1 (GHSA-m665-v7pm-7grj, GHSA-q7v9-x93c-xqv3, GHSA-8hxf-qr8p-62x4).
- PR #488 (`e1f2fb74`), OBS-476 and OBS-477: `tools/vault.py copy PROJECT ENV NAME --to-store PATH` copies one slot
  into another store this account owns, the value read and written in one process, both journals recording
  `copied`, with a `vault-copy/1` receipt; `observatory_machine(section="skills")` and `full machine --skills` list
  this space's skills and plugins from the harness scan, with the finding `plugin-behind`. Board rows OBS-486 to
  OBS-491 from its reviews.
- The `observatory-log` plugin stays at 0.35.9: `git diff --stat v0.35.9..origin/main -- observatory/engine/skill
  .claude-plugin` printed nothing.

## The space daemons copy

The root-owned copy `/usr/local/libexec/observatory-space-daemons` changes in this release (1.1.0 to 1.2.0). An
administrator installs it again with the steps in the file's own docstring (download and verify the release, then
`unzip -p` the wheel's `observatory/engine/tools/space_daemons.py` and `sudo install` it). Its sha256 is
`747e472e066f426a87b5174e662889109d2637fb661d0b1f59e2943279f61af0`: `version_line()` hashes the file's own bytes
(`python3 -I observatory/engine/tools/space_daemons.py --version` printed `space-daemons 1.2.0 sha256` and that digest),
`shasum -a 256` of the file gave the same digest, and `observatory/engine/SOURCE-INVENTORY.json` records the same
value as the file's `export_sha256`, the bytes that ship. This release changes nothing in that file, so the digest
holds for the published wheel.

## Decisions

- **0.35.10, a patch release**, as 0.35.9 was for additive work: a new vault subcommand, a new machine view section
  and finding, a new space daemons subcommand; no setting, schema or migration change (`docs/COMPATIBILITY.md`, the
  two sections now headed 0.35.10). An older engine does not read the skills inventory and has no `vault.py copy`.
- **Board**: OBS-475 reads `done in 0.35.10 (#486)` with its Outcome, OBS-476 `done in 0.35.10 (#488)`. OBS-477 and
  OBS-395 stay open and name what 0.35.10 released: OBS-477 still owes the finding for an organization's plugin
  enabled in a person's own space (it needs OBS-359), OBS-395 its part 1.
- **OBS-481 is cancelled, not needed.** Measured on 2026-11-10: `tools/check_public_release.py` `_deny_pattern`
  already matches every private identifier only at word boundaries, whatever its length (a negative lookbehind for
  a letter, digit, `_`, `.` or `-` before the name, the lookahead `(?![A-Za-z0-9_-])` after it). A list holding only
  the five-letter name passed `main`, and the maintainer's full list (3893 entries) passed `main` with exit 0; the
  refusal of 2026-11-09 came from other entries new to the list that day. The maintainer's denylist builder dropped
  its exclusion of the name.
- **Two new rows with the ids reserved for them** (agent-sync register `OBS`, reserved on 2026-11-10 by this run;
  the next free id is now OBS-494), both `open`:
  - OBS-492: `desktop/src-tauri/Cargo.toml` declares `rust-version = "1.77"`, while `tauri` 2.12 and
    `tauri-plugin-updater` 2.14.1 need 1.90 and `zip` 9.5.0 needs 1.88; set the true minimum or drop the field.
  - OBS-493: `vault.py copy` falls from `target-other-account` to `filesystem-refused` when `osprivacy.explain`
    cannot read the owner of another account's store; wrap the explanation so the code stays, and have the test
    `test_a_store_of_another_account_is_refused_with_its_reason` assert the message carries it.

## Checks run

- This release PR (Python 3): `tools/update_inventory.py` and `--check`, `tools/sync_engine_docs.py` and `--check`,
  `tools/check_backlog.py` (0 problems), `observatory/engine/tools/check_docs.py`,
  `observatory/engine/tools/check_paths.py`, and the root suites `test_changelog_releases`,
  `test_version_consistency`, `test_backlog_board` and `test_docs_drift`. No engine code changes here, so no
  portable suite was rerun.

## Receipts pending

- Release PR: number, merge commit and its `observatory-release-check` run.
- Tag `v0.35.10`: tag object, release run, published files and time.
- Verification: `SHA256SUMS`, the `gpg` signature, `gh attestation verify` of the wheel and the installers.
- Installs: the release manager's machine (`full update --version 0.35.10 --apply`), the maintenance job elsewhere,
  and the root-owned space daemons copy where a space runs one.
