# Run 2026-11-02: release 0.30.1

Objective: release what merged since 0.30.0 (the second-machine protection OBS-283 and OBS-284, Windows verified in
CI OBS-277 with the terminal fix of OBS-279), then tag, verify and install it, before the operator starts a restore on
the second machine. The order was relayed by the fabric-workspace coordinator: 0.30.1 first (overdue since
2026-10-30), then OBS-287 in 0.31.0 by 2026-11-04.

Base: `f93ce304` (origin/main, #394 merged) on branch `release/0.30.1`, then main merged in once #403 landed. This record is committed with the version
bump; resolve the final object id with `git log -1 --format=%H` on that branch.

## What the release carries

Everything merged since `v0.30.0` (`git log v0.30.0..origin/main` at the merge of #403, 7 commits). Each pull request with code passed an
independent adversarial review and every confirming round its findings required:

- #394: a restore on a second machine never copies this machine's identity, private SSH keys or background jobs;
  bundles and snapshots name their machine; `full machine-move` (OBS-283, OBS-284); three review rounds. It also
  gives the shared synthetic estate of the engine tests 180 s for its setup on Windows (OBS-288, part).
- #387: Windows verified on every change against `tools/windows-known.json`, UTF-8 mode on Windows, and the
  terminal check of operator-only commands (OBS-277, OBS-279).
- #403: the node session test's relay stub drains before it closes, so a loaded Windows runner no longer reads 1006
  for 4403 (OBS-295); `engine_update` may also finish and fail on Windows, its failing checks listed; rows OBS-294 and
  OBS-296. It landed first because the release pull request's own Windows job failed on that test twice.
- The documentation pull requests #391, #393, #395 and #399 (rows OBS-282 to OBS-291 and the 0.30.0 receipts).

## Decisions

- **0.30.1, a patch**: one new command (`full machine-move`) that exists only to make the restore safe, and a
  restore that now refuses to copy what must stay with its machine. No contract pin, MCP tool or page changes.
- **The plugin stays 0.30.0**: `git diff --stat v0.30.0..f93ce304 -- observatory/engine/skill` prints nothing.
- **CHANGELOG**: `## Unreleased` becomes `## 0.30.1 (2026-11-02)` with a summary naming the one step for a person:
  install this release on both machines before restoring on the second one.
- **No `RELEASE.json` marker**: nothing needs a person before installation.
- **Not in this release**: #400 (OBS-287, the interim relay) and #401 (OBS-292, the Sentry door) are reviewed and
  green but carry new commands; they land after the tag, for 0.31.0.
- **Rows**: OBS-283 and OBS-284 read `done in 0.30.1 (#394)`; OBS-277 and OBS-279 read `open: partly done in 0.30.1
  (#387)` with what remains; OBS-288 already names its part done in 0.30.1.

## Checks run

On this branch, with `~/.local/share/project-observatory-venv/bin/python`:

- `python -m unittest tests.test_version_consistency tests.test_desktop_app tests.test_plugin_manifests
  tests.test_changelog_releases tests.test_backlog_board tests.test_docs_drift tests.test_engine_doc_copies
  tests.test_release_boundaries tests.test_licensing`: 165 tests OK.
- `python -m unittest discover -s tests` from the repository root: 333 tests OK (1 skipped).
- `tools/check_backlog.py`: 0 problems. `tools/update_inventory.py`: exit 0. `tools/sync_engine_docs.py --check`:
  exit 0. `python -m compileall -q observatory`: exit 0. `observatory/engine/tools/check_docs.py`: exit 0.
- The engine's own suite ran on #394's head on macOS, Linux and Windows (all eight checks green on `fd025d17`); the
  release pull request runs it again.

## Receipts

- Release PR #402 merged as `93a0c569` after #403; tag `v0.30.1` (annotated, object `cb65bd4f`) pushed on it.
- Before the merge, the release pull request's own Windows job failed twice on `node_session`
  `test_a_revoked_machine_is_closed_4403` (close code 1006 for 4403 under load). #403 fixed the test relay stub
  (OBS-295) and landed first; the release branch then took main and its checks went green (10 of 10).
- Release run `41561959654`: every job green on the first attempt (guard, wheel, manifest, macos, linux x64 and arm64,
  windows x64 and arm64, feed, publish). Published 2026-11-03T02:33:45Z with 19 assets.
- Verified after publishing: `shasum -a 256 -c` OK on `project_observatory-0.30.1-py3-none-any.whl`;
  `gpg --verify SHA256SUMS.asc SHA256SUMS`: good signature from the PassionCode.ai releases key (EdDSA
  `63B30DC324BD697487AA31944FAFB8AEC803B6A7`); `gh attestation verify` on the wheel with `--signer-repo
  passioncode-ai/.github`: exit 0.
- Installed on the operator's machine with `project-observatory full update --version 0.30.1 --apply` (a machine
  backup was taken before the update): `full doctor` reports version 0.30.1 with nothing failing; its one warning,
  three of the last ten ticks ending on their timeout, predates this release. Session MCP servers move themselves at
  their next request.

## Next

The coordinator is told that a restore on the second machine is now safe once both machines run 0.30.1. Then
0.31.0 by 2026-11-04: #401 (OBS-292, the Sentry door) and #400 (OBS-287, the interim relay) are reviewed and green
and land next, then OBS-291 (DEC-0147), OBS-282, OBS-285 and OBS-286.
