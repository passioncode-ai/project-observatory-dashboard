# Run 2026-11-10: release 0.35.11

Objective: release the port forward `full machine-move leave --via USER@HOST` installs for a service moved to
another machine, with `full machine-move return` and the `cutover` refusal (OBS-480, PR #492); then tag, verify and
install it.

## State now

- Open: tag `v0.35.11` once this release PR is merged and its `observatory-release-check` is green; verify the
  published files (`SHA256SUMS`, its signature, the attestations), install on the release manager's machine and let
  the maintenance job install it elsewhere; fill the Receipts below.
- Waits: this release PR's review and release check; no watcher of this record runs, the release manager tags.
- Next task: the rows from the reviews, by priority: OBS-495 (`leave --via` records the forward this engine's own job
  still holds after a failed replacement), then OBS-498 (`port_free` checks every local address family), then
  OBS-492 (the desktop app's true minimum Rust version) and OBS-493 (`vault.py copy` keeps `target-other-account`
  when the owner cannot be explained).

## What it releases

Merged to `main` since `v0.35.10`:

- PR #492 (`59360f84`), OBS-480: on macOS, `full machine-move leave LABEL --to MACHINE_ID --via USER@HOST` moves the
  job as before and, for a Fabric service whose origin is a local port, installs one launchd job
  (`org.project-observatory.forward.<id>.<instance>`) running `/usr/bin/ssh -N` with `BatchMode=yes`,
  `ExitOnForwardFailure=yes` and `ServerAliveInterval=30`, bound to `127.0.0.1` only, as an argument array, its plist
  owner-only. A hand-made ssh forward of the same port to the same destination is adopted, not installed twice; a
  forward of that port to another destination (`forward-elsewhere`) or a port held by anything else (`port-held`) is
  refused by name, and the move itself stays done. The `elsewhere` entry of `placement.json` records the job in the
  new fields `forward` and `forwardAdopted`. The new `full machine-move return LABEL [--remove-adopted]` removes the
  forward the engine installed; an adopted one is left running unless `--remove-adopted`, which unloads it and sets
  its plist aside under `machine-move/forwards-set-aside/`. `cutover` refuses while a forward is recorded for the
  label. Off macOS, `--via` and `return` are refused before anything moves. Tests:
  `observatory/engine/tests/test_machine_forward.py`.
- PR #494 (`3ad2178e`), the blocking finding of this release PR's second review round: a plain `full machine-move
  return LABEL` over an adopted forward that still holds the port keeps its `forward` record, so the hinted rerun with
  `--remove-adopted` finds it and `cutover` keeps refusing; an adopted forward no longer loaded, with no forward left
  under its label, is cleared. Tests in `observatory/engine/tests/test_machine_forward.py`. This release PR was rebased
  onto it and `observatory/engine/SOURCE-INVENTORY.json` regenerated.
- The `observatory-log` plugin stays at 0.35.9: `git diff v0.35.10 HEAD -- observatory/engine/skill .claude-plugin`
  printed nothing.

## How #492 reached `main`

- Two CI fixes before the merge: the analytics event catalogue's line numbers in `docs/analytics-events.md` moved with
  the CLI changes (`cc5019e0`), and the `machine_forward` suite, first skipped as a whole on Windows, was narrowed so
  its parser and CLI tests run there (`64cfbcdb`).
- Two independent review rounds, neither with a blocking finding. Fixes from the first round (`ae67cc53`): a refused
  or failed forward keeps a forward already recorded for the label while that one still holds the port, and
  `--remove-adopted` checks again that the plist is still an ssh forward before unloading or moving it.
- What the reviews found and did not block on is on the board as OBS-495 to OBS-499 (below).

## Decisions

- **0.35.11, a patch release**, as 0.35.10 was for additive work: a new flag of `leave`, a new `return` action and a
  new `cutover` refusal, two optional `placement.json` fields under the unchanged schema `observatory-placement/1`;
  no setting or migration change (`docs/COMPATIBILITY.md`, whose "release after 0.35.10" wording now reads 0.35.11;
  `observatory/engine/docs/CLI-COMPATIBILITY.md` likewise). An older engine ignores the new fields and has no
  `return`.
- **Board**: OBS-480 reads `done in 0.35.11 (#492)`.
- **Six new rows with the ids reserved for them** (agent-sync register `OBS`, reserved on 2026-11-10 by this run;
  the next free id is now OBS-500), all `open`:
  - OBS-494 (from the review of #491): `docs/COMPATIBILITY.md` still heads a section "the release after 0.35.4"
    (0.35.5), and `CLI-COMPATIBILITY.md` says "the release after 0.35.4" and "the release after 0.35.6" (0.35.7);
    name the versions that shipped them.
  - OBS-495: when no forward was recorded and this engine's own forward job is left holding the port (`unload-failed`,
    or an error after its plist was written), `leave` records no `forward`, so `cutover` does not refuse; record the
    label the failed outcome carries.
  - OBS-496: `return --remove-adopted` over an adopted record whose plist is gone raises; the message names the step
    that clears the record (`return` without `--remove-adopted`).
  - OBS-497: no test covers the match by `Label` for an adopted forward whose plist file is not named after its
    label.
  - OBS-498: `port_free` binds `127.0.0.1` with `SO_REUSEADDR`, so on macOS a listener on the wildcard address or on
    `::1` reads as a free port; check every local address family.
  - OBS-499: forwards on Linux as a systemd user unit, and the forward job's ssh standard error captured so a failing
    forward says why, not only that it is not running.

## Checks run

- This release PR: `tools/update_inventory.py` and `--check`, `tools/sync_engine_docs.py` and `--check`,
  `tools/check_backlog.py` (0 problems), `observatory/engine/tools/check_docs.py`,
  `observatory/engine/tools/check_paths.py`, and every root suite `tests/test_*.py`, one at a time. No engine code
  changes here, so no portable suite was rerun.

## Receipts pending

- Release PR: number, merge commit and its `observatory-release-check` run.
- Tag `v0.35.11`: tag object, release run, published files and time.
- Verification: `SHA256SUMS`, the `gpg` signature, `gh attestation verify` of the wheel and the installers.
- Installs: the release manager's machine (`full update --version 0.35.11 --apply`) and the maintenance job
  elsewhere.
