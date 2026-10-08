# 2026-10-08 — releases 0.19.0, 0.19.1 and 0.19.2

The public record of three releases. Changes are in [CHANGELOG.md](../../../CHANGELOG.md); this
page says what was measured and how each was checked.

## 0.19.0 — one update behaviour, a self-updating Mac app, the system language

Merged as #177 (`eb593bd`), tag `v0.19.0`. CI green on Linux and macOS; the release workflow built,
signed and notarized the app after the `release` environment was approved, and published the
wheel, the app zip, `SHA256SUMS` and its signature.

On a maintainer machine with about twenty agent sessions open, installing it over 0.18.0 rolled
back: the workspace upgrade migrated a copy of the store and replaced the live one with it, and a
session's write while the copy was prepared refused the update ("Workspace changed while upgrade
was staged"). The rollback kept the store as it was; one session record written during that window
was later restored from the rolled-back copy with its original id and time.

## 0.19.1 — the store is migrated in place

Merged as #178 (`c83a580`), tag `v0.19.1`. The staged copy now only proves the migration runs; the
live store is migrated in one SQLite transaction that concurrent writers wait for. The regression
test writes to the store and a log while the update is staged: red on 0.19.0, green after. On the
same machine the update then installed while sessions kept writing, without a rollback.

## 0.19.2 — four defects and the documentation

Branch `agent/0.19.2`.

- **OBS-40.** One machine's `tick.err` records 42 ticks stopped at the 1500 s ceiling; between 4 and 7 October the folder scan reached its own 900 s limit 14 times. Measured cause: under a starved
  disk, `integrity_check` over a 70 MB store took up to 266 s (1 s on a quiet machine) and the
  folder scan its whole 900 s limit; out of a tick the same scan took 51 s. Because the scan wrote
  only at its end, each stop lost all of it, and the registry, findings and dashboard steps after
  it never ran. Fixed: collectors leave the tail 300 s; every step gets `OBSERVATORY_STEP_DEADLINE`;
  the scan writes partial results with carried rows. Tests: `test_lifecycle.py` (5 new),
  `test_filesystem_scan.py` (4 new), red before the change.
- **OBS-41.** `compatibility.read_consistently`; tests in `test_maintenance.py`.
- **OBS-44.** The session hooks' receipts in `store/raw` refused an update; test in
  `test_workspace_upgrade.py`, plus one that keeps the exempt set equal to what the hooks write.
- **OBS-45.** `doctor` asked for the companion's database; tests in `test_workspace.py`, red before.
- **Documentation.** A read-only audit compared the documents with the code and listed 18
  confirmed defects. The ones fixed are in the changelog's Documentation section.

Still open: the rest of OBS-40 — Health saying how many recent ticks were incomplete, and
incremental `google` and `machine` collectors.
