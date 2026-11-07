# Run 2026-11-07: release 0.35.0

Objective: release the jobs of a macOS account with nobody signed in (OBS-361 part 1, #458, and its follow-up #459)
and the Linux fix of `peer_owner` (1.2.0, #460); then tag, verify and install it.

Base: origin/main after #460 (`2180235c`) on branch `release/0.35.0`. This record is committed with the version bump;
resolve the final object id with `git log -1 --format=%H` on that branch.

## What it releases

- #458 (OBS-361 part 1): a headless cutover moves a job to the account's background folder and answers
  waiting-for-root; the administrator's standalone tool `tools/space_daemons.py` (space daemons 1.0.0) installs it as
  a system daemon with the account as `UserName`. Reviewed over five independent rounds as #457, merged as one commit
  so the history privacy check would not read a neutral test path an early commit used.
- #459 (space daemons 1.1.0): a root-owned trust file for root-equivalent owners and groups (wheel/admin, admin
  members confirmed by `dsmemberutil` at every read), Umask and SoftResourceLimits, AssociatedBundleIdentifiers
  stripped, WorkingDirectory `/` to the home, XML comments with `--`. Built from a dry run of `plan` on a real computer's
  73 staged definitions (29 passed with 1.0.0, 49 with 1.1.0; the rest wait for their agent's release).
- #460 (`peer_owner` 1.2.0): on Linux an accepted connection decides by its own row's uid, and only a not-yet-accepted
  one (inode 0) by its listener; the classify step compares the caller's own uid first. Found by another project on
  the board; the hybrid rule followed a review that found a restart race in the listener-only rule, and the contract
  adopted the same rule (CT-254).

## Decisions

- **0.35.0, a minor release**: a new administrator tool, new cutover behaviour in a headless account, and a security
  fix; every change is additive for a computer where the person signs in at the screen.
- **The plugin stays 0.30.0.**
- CHANGELOG `## 0.35.0 (2026-11-07)` checked with Copylot (en-US, editorial): report `ee6e12e5`, score 100, no findings.

## Checks run

On the release branch after rebasing on `2180235c`: the root release suites, `tools/sync_engine_docs.py --check`,
`tools/check_backlog.py`, `observatory/engine/tools/check_docs.py`, `check_paths.py` and the inventory check (results in
the release PR).

## Receipts

- Release PR #461 merged as `fbf991d1`. Its `check.yml` run on main (`42360355093`) passed every job, Windows included.
  Tag `v0.35.0` (annotated, object `14ef3d59`) on `fbf991d1`. The background step that was to push the tag failed
  only because the landing script had already removed the release worktree; the tag was pushed by hand from another
  checkout of the same remote after the run read `success`.
- Release run `42363475717` passed every job on its first attempt (guard, wheel, manifest, Linux x64 and arm64,
  Windows x64 and arm64, macOS, feed, publish); the `check.yml` run the tag started (`42363475453`) passed too.
  Published 2026-11-07T11:07:52Z, 19 assets.
- Verified after publishing: `shasum -a 256 -c SHA256SUMS --ignore-missing` OK on every downloaded asset (0 failures);
  `gpg --verify SHA256SUMS.asc SHA256SUMS`: good signature, key `63B30DC324BD697487AA31944FAFB8AEC803B6A7` (the
  PassionCode.ai releases key); `gh attestation verify` on the wheel with `--signer-repo passioncode-ai/.github`:
  exit 0; the wheel carries `analytics-app-key`.
- Installed on the operator's Mac with `project-observatory full update --version 0.35.0 --apply`; `full doctor` reports
  0.35.0 and the service `project-observatory.default` answers 0.35.0 on its well-known document. Doctor's verdict:
  `clean: false` with the same two warnings as 0.34.0 (`machine-backup-not-carried`, `backups-vault-store-not-carried`).
  `loopback`: one person's account on this host, so the peer check is `auto` and inactive.
- Told: the agents waiting for the release (board thread `c1a45468`, message `7e02410a`), Fabric (thread `f7758225`,
  `2a1494b6`), Fabric Dashboards (thread `5d2a61f1`, `e2583be8`), Workspace with the Studio steps (thread `0159c474`,
  `f485a6e0`), and the session that runs the Studio's `apply`.

## State now

- Open: the administrator's real `apply` of `tools/space_daemons.py` on the computer whose dry run is recorded above,
  after that computer's spaces update to 0.35.0 (its own session runs it).
- Waits: none in this repository.
- Next task: none for this release; the move's remaining steps are on the board (OBS-361 part 2).
