# Run 2026-11-07: release 0.35.3

Objective: release `peer_owner` 1.3.1 (OBS-386): contract DEC-0155 RT-1's reason forms, a remembered restricted
verdict, the method of the last read, and tests independent of their order; then tag, verify and install it.

## State now

- Open: none in this record.
- Waits: none.
- Next task: none here; the current work is in the newest run record.

## What it changes

- `observatory/engine/peer_owner.py` 1.3.1:
  - every failure of the libproc proof (`read_own_sockets`) reads `table-restricted: <detail>`, and the bare
    `table-restricted` stays for "this account's sockets were read and none matches" (a caller that needs to tell
    them apart matches the bare value exactly, one that only refuses matches the prefix);
  - a table found restricted on macOS is remembered for `RESTRICTED_TTL` (30 s), so later questions go to libproc
    without reading the table first; a full table clears the verdict;
  - `last_method()` names the method of the last read, `libproc` included;
  - the branches for an unreadable table name `last_method()` instead of calling `method()`, so they never start a
    first sysctl calibration.
- `tests/test_peer_owner.py`: both reason forms pinned; the verdict's expiry and a full table clearing it; every test
  class resets the verdict, and the restricted-table tests save and restore the sysctl and libproc calibrations.

## Decisions

- **0.35.3, a patch release**: a reason's wording and a cache; every refusal stays a refusal.
- **The contract pin does not move here.** DEC-0155 (tag `v0.1.0+2026-11-07.5`) is followed in this module; the
  engine's vendored contract pin moves with its own rows.
- **The plugin stays 0.30.0.**

## Checks run

On the branch: `tests.test_peer_owner` 86 OK (1 skipped) on macOS twice in a row and in a Python 3.11 Linux container; the order
case from the review (the failed-calibration test, then the live pair test, in one process) OK; the restricted-table
class with `find_library` forced to None (as on Windows) OK; `tests.test_peer_check` and `tests.test_osprivacy` OK.

## Receipts

- Release PR #467 merged as `a69c6fee`; its hosted CI failed once on Linux, Python 3.11, in
  `test_machine_backup` (a signalled-restore test's 30-second wait on a loaded runner); the job was rerun and passed,
  and the flake became OBS-392 (fixed in 0.35.4).
- Tag `v0.35.3` (tag object `db7b9f2d`) on `a69c6fee`; the release run published 19 files on 2026-11-07T23:47:26Z.
- Verified: SHA256SUMS OK, gpg `VALIDSIG 63B30DC324BD697487AA31944FAFB8AEC803B6A7`, attestation exit 0.
- sha256 of `observatory/engine/peer_owner.py` 1.3.1 in the wheel: `b3831e5f1026a1d93d27aa8b54926dcc62a78267b87ad51fec7461c92c9826b5`,
  announced on the board (thread c1a45468).
- Installed locally: `--version` answered 0.35.3.
