# Run 2026-10-24: release 0.29.1

Objective: release what merged since 0.29.0 (the contract re-pin at `588748ab` and the organization node's duties
there, the `node.lifecycle`, estate and flaky-test review fixes, the tick under load, and the audit fixes on the
organization node, MCP health and the disk guard), then tag, verify and install it.

Base: `95466a8b` (origin/main, #353 merged) on branch `release/0.29.1`. This record is committed with the version
bump; resolve the final object id with `git log -1 --format=%H` on that branch.

## What the release carries

Everything merged since `v0.29.0` (`git log v0.29.0..95466a8b`, four pull requests):

- #350: the contract pin moves to fabric-agent-contract `588748ab` (OBS-217). A `410 organization-closed` with the
  contract's exact body ends an enrollment and drops that organization's buffered data (DEC-0103); a `purpose`
  rejection holds its kind only on an enrollment bound to a person who has received an acknowledged notice version,
  and `org status`, doctor and Health show every hold (DEC-0100, DEC-0102); the DEC-0102 renewal and baseline
  corrections; the dashed home path check (DEC-0105); summary and attribution findings (DEC-0114, DEC-0116); the
  check-in lists only `fabric-device/0.1` (DEC-0117, DEC-0111); machine profiles and the delta anchor (FAC-SEM-134,
  FAC-SEM-137).
- #351: `node.lifecycle` safety (no signal after `identity_mismatch`, no launch hours after a crash, the quit grace
  kept on close, an unreadable `launchctl` disabled list is not an operator hold, an expired binding refused;
  OBS-210, OBS-211, OBS-213), the partner key picked by strict equality (OBS-216), and two CI flakes made
  deterministic (OBS-208, OBS-219).
- #352: the tick under load (OBS-220): the steps that must finish get their measured time and minimums, a merge,
  emit or validation stopped for time no longer stops findings and the dashboard and the run is recorded `partial`;
  the light integrity check runs only while a full pass succeeded within the day and never shows `ok` over a damaged
  verdict; `env` and `remote-env` resume across ticks.
- #353: the audit fixes (OBS-221): a renewal to another person or with a certificate without a user id is refused;
  the summary consent is cleared when an organization closes or removes the machine; MCP health older than 48 hours
  is sent as unknown; enrollments ended by 0.29.0's provisional `unknown-org` rule are restored; the disk guard lists
  kept browser clones; documentation corrections.

## Decisions

- **0.29.1, a patch**: the four pull requests fix behaviour within the declared contracts and follow the contract
  pin; no new public command or MCP tool.
- **The plugin stays at 0.28.0**: `git diff --stat v0.29.0..95466a8b -- observatory/engine/skill .claude-plugin`
  prints nothing, so `observatory-log`'s three manifests and its `SKILL.md` files keep 0.28.0.
- **CHANGELOG**: `## Unreleased` (the #353 entries) becomes `## 0.29.1 (2026-10-24)` with a summary and Added,
  Changed and Fixed sections written from the pull requests' bodies; the #353 entries gain their pull request number.
  The summary names the one-time `probes-check` exit after the update, because the contract pin moved
  (COMPATIBILITY.md: "The same happens once after any release that moves the contract pin").
- **Rows closed**: OBS-208, OBS-210, OBS-216, OBS-217, OBS-219 and OBS-220 read `done in 0.29.1 (#n)`. OBS-221,
  which #353 delivered and its body said stays open until it merged, reads `done in 0.29.1 (#353)` with its outcome;
  the item it left out is OBS-222. OBS-211 and OBS-213 stay open, partly done. OBS-223 (reserved through the OBS
  register) records the #352 confirming round's finding that a run of partial ticks does not escalate. Next free id:
  `OBS-224`.
- **No `RELEASE.json` marker**: nothing needs a person before installation. The `unknown-org` restoration runs on
  the first read of each enrollment, and the one-time `probes-check` exit is cleared by `full probes`.

## Checks run

On this branch, with `~/.local/share/project-observatory-venv/bin/python3`:

- `python -m unittest tests.test_version_consistency tests.test_desktop_app tests.test_plugin_manifests
  tests.test_changelog_releases tests.test_backlog_board tests.test_docs_drift tests.test_engine_doc_copies
  tests.test_release_boundaries tests.test_licensing`: 165 tests OK.
- `tools/check_backlog.py`: 0 problems.
- `tools/update_inventory.py` regenerated `SOURCE-INVENTORY.json`; `--check` exits 0.
- `tools/sync_engine_docs.py --check`: stale none, exit 0.
- `observatory/engine/tools/check_docs.py`: documentation current, exit 0.
- The full portable suite was not run locally; the pull request's checks run it on macOS, Linux and Windows.

## Next

Merge this pull request, tag `v0.29.1` on the merge commit, verify the release (sums, attestation, GPG), install it
with `project-observatory full update --apply`, run `project-observatory full probes` once so `probes-check` passes,
and record the receipts here in a follow-up pull request.

## Correction (2026-10-24)

The `full probes` step above, and the one-time `probes-check` exit this record and the CHANGELOG summary describe,
do not apply to an installed copy (OBS-224, measured on an installed 0.29.1). Its `project-observatory full check`
runs the portable suite and never runs `probes-check`, so nothing is needed after installing. `full probes` could not
run there either: the probes run only in an explicitly synthetic workspace with `features.probe_fixture` on
(`observatory/engine/fabric/FABRIC-CONFORMANCE.md`), the step launched the runner without one, and it ended in
argparse's `the following arguments are required: --fixture-home` (exit 2). The runner now explains that, and
`full probes --fixture-home PATH` forwards a maintainer's workspace. The text above is kept as it was written.
