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

## State now

- Open: the release PR, then the tag `v0.35.0`, its run, verification and the local install; then the administrator's
  real `apply` on the computer whose dry run is recorded above.
- Waits: none beyond the release run (watched by this session).
- Next task: merge the release PR, push the tag, verify, install, record the receipts here.
