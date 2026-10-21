# Run 2026-10-21: release 0.28.0

Objective (operator request, 2026-10-21): release what merged since 0.27.0, the Cloudflare `zone-edge-rules` preset
(#331) included, install it, and issue the first token with that preset.

Base: `026bd297` (origin/main, #331 merged) on branch `agent/release-0.28.0`. This record is committed with the
version bump; resolve the final object id with `git log -1 --format=%H` on that branch.

## What the release carries

Everything merged since `v0.27.0` (`git log v0.27.0..026bd297`, nine pull requests):

- #321: a machine restore never starts a background `git gc` (OBS-157 follow-up).
- #322: an OpenRouter consumer's key past every listed page is asked about itself.
- #323: no test's git leaves a background gc or maintenance behind (the class behind the refused first v0.27.0 run).
- #325: free-space preflight for `full update --apply`, a bounded portable-suite footprint (OBS-167), the machine
  backup's losses as findings with their fix, and larger default backup caps.
- #326: nine documentation drifts closed, each behind a check (`check_backlog.py`, `test_changelog_releases.py`,
  `test_docs_drift.py`, `test_cli_compatibility.py`).
- #328: every collector a turn and a bounded share of the tick; health signals that tell (OBS-52; OBS-40 superseded).
- #329: a release found within five minutes on the static feed (OBS-176), and the apps' lifecycle defaults (OBS-177).
- #330: `google-auth` 2.62.0 with both lock copies (supersedes Dependabot #324).
- #331: the Cloudflare door's `zone-edge-rules` preset: Zone Read, Cache Settings Write, Config Settings Write and
  Bot Management Write on one zone, proved by reading the zone's cache-settings phase.

## Decisions

- **0.28.0, not a patch**: new capabilities (a preset, a second scheduled job), semantic versioning minor.
- **The plugin moves with the engine** to 0.28.0: its `handling-secrets` skill names `zone-edge-rules` (#331) and the
  DigitalOcean and Cloudflare presets #326 documented.
- **CHANGELOG**: `## Unreleased` becomes `## 0.28.0 (2026-10-21)` with a summary; the one new entry is the
  `zone-edge-rules` preset under Added, which #331 did not write. The second `### Changed` heading that followed
  `### Fixed` is merged into the first; no other entry is reworded.
- **Rows closed**: OBS-52 (#328), OBS-167 (#325), OBS-176 and OBS-177 (#329) as `done in 0.28.0`. OBS-64, OBS-66 and
  OBS-92 still read `done — merged (#n), unreleased`; their commits are first contained in `v0.24.5`
  (`git tag --contains` on the squash commits of #243, #240 and #256), so they read `done in 0.24.5` now.
- **No `RELEASE.json` marker**: nothing needs a person before installation. The five-minute check job is installed
  by `full maintain ensure`, which the update and the maintenance job already run.

## Local gate

- `tools/release/gate.sh` (maintainer tools), run bare twice at a load average of 23 to 36: the source inventory
  passed both times; the portable set ran 276 suites. Run 1 had four red (`mcp_inventory`, `secret_header`,
  `ssh_keys` and `digitalocean_door`, two of them timeouts at 120 s); all four passed alone at `--jobs 2`. Run 2 had
  one red, `ssh_keys`, a timeout at 120.6 s; alone it takes 101.8 s. Recorded as OBS-190; this change touches no
  code that suite runs.
- The gate's later steps, run by hand because the script stops at the first red: `python -m unittest discover -s
  tests`, 298 tests OK (1 skipped); `tools/check_public_release.py` with the private denylist: passed, 1198 files,
  no findings.
- `tests/test_changelog_releases.py`, `test_docs_drift.py`, `test_version_consistency.py`, `test_backlog_board.py`,
  `test_plugin_manifests.py`, `test_desktop_app.py`: 121 tests OK; `observatory/engine/tests/test_cli_compatibility.py`:
  22 OK.

## Next

Merge this pull request, tag `v0.28.0` on the merge commit, verify the release (sums, attestation, GPG), install it
with `project-observatory full update --apply`, then issue the first `zone-edge-rules` token into its vault slot and
record the receipts here.
