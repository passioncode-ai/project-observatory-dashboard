# Run 2026-10-28: release 0.30.0

Objective: release what merged since 0.29.2 (sharing a key with another person's Observatory and the mailbox in the
person's own Cloudflare account, the secret prompt's server half and Mac window, the new home, the DigitalOcean and
Cloudflare pages, the pairing screens, the lifecycle steps L2r, L4 and L5, the Cloudflare `access-app` preset), then
tag, verify and install it. The operator asked on 2026-10-28 for the highest-quality releases of Fabric, Dashboards
and Observatory first (relayed by the fabric-workspace coordinator).

Base: `e9a12f16` (origin/main, #386 merged) on branch `release/0.30.0`. This record is committed with the version
bump; resolve the final object id with `git log -1 --format=%H` on that branch.

## What the release carries

Everything merged since `v0.29.2` (`git log v0.29.2..e9a12f16`, 26 commits). Each pull request with code passed an
independent adversarial review and every confirming round its findings required:

- #386: the share mailbox `observatory-cloud/1` as a Cloudflare Worker, `full cloud deploy|status|url` through the
  door's `workers-edit` preset (OBS-263); four security rounds. Live in the operator's account at version 1.3.0.
- #385: `full share` over the contract's `fabric-peer-share/1` (DEC-0142), the contract pin `759704b4`, the
  maintenance job syncing shares hourly, `use_secret.py run --share` (OBS-264, OBS-276); three security rounds.
- #382 and #378: the secret prompt (OBS-260): agent bindings, the node's request queue, `observatory_secret_request`,
  the Mac window, the app's door and the person's phrase; every request is answered `no-client` with the `vault.py
  put` step until an engine is bundled inside the signed app (three preconditions recorded on OBS-260).
- #388: the Cloudflare preset `access-app` (OBS-281, Fabric board request 3bf4a84e).
- #383: the lifecycle helper inside the signed app (OBS-79 step L2r); #369 and #368: steps L5 and L4; #367 (OBS-222).
- #377: Home replaces Overview (OBS-261); #376: DigitalOcean and Cloudflare pages (OBS-235, OBS-271); #380: the
  pairing screens and the passkey prompt (OBS-197).
- #374 (OBS-192), #366 (OBS-193), #365 (OBS-191), #364 (OBS-194), #363 (OBS-226), #384 (a test made reliable on
  Ubuntu), and the documentation pull requests #362, #370, #371, #372, #373, #375, #379, #381.

## Decisions

- **0.30.0, a minor**: new commands (`full share`, `full cloud`, `full agent-binding`), a new MCP tool, a moved
  contract pin and a new home page.
- **The plugin moves to 0.30.0**: the `handling-secrets` skill changed (`git diff --stat v0.29.2..e9a12f16 --
  observatory/engine/skill` shows it).
- **CHANGELOG**: `## Unreleased` becomes `## 0.30.0 (2026-10-28)` with a summary naming the one optional operator
  step: `project-observatory full lifecycle import --from PATH` once at a terminal to move off another lifecycle
  broker, then `full lifecycle status`, then that broker's own uninstall.
- **No `RELEASE.json` marker**: nothing needs a person before installation.
- **OBS-277 (Windows) is not in this release**: its pull request had not reached green CI; it ships in the next one.
- **Rows**: OBS-222 and OBS-235 read `done in 0.30.0 (#n)`; OBS-197, OBS-263, OBS-264, OBS-271 and OBS-276 read
  `open: partly done in 0.30.0 (#n)` with what remains; OBS-260 names its Mac half as landed.

## Checks run

On this branch, with `~/.local/share/project-observatory-venv/bin/python`:

- `python -m unittest tests.test_version_consistency tests.test_desktop_app tests.test_plugin_manifests
  tests.test_changelog_releases tests.test_backlog_board tests.test_docs_drift tests.test_engine_doc_copies
  tests.test_release_boundaries tests.test_licensing`: 165 tests OK.
- `python -m unittest discover -s tests` from the repository root: 318 tests OK (1 skipped).
- `tools/check_backlog.py`: 0 problems. `tools/update_inventory.py`: exit 0. `tools/sync_engine_docs.py --check`:
  exit 0. `python -m compileall -q observatory`: exit 0. `observatory/engine/tools/check_docs.py`: exit 0.
- The engine's own suite (`observatory/engine/tests`) was not run locally (the disk had about 5 GB free); the pull
  request's checks run it on macOS, Linux and Windows.

## Next

Merge this pull request, tag `v0.30.0` on the merge commit, verify the release (sums, attestation, GPG), install it
with `project-observatory full update --apply`, post on Fabric board thread `db5cd8a1` that the `access-app` preset is
installed, and record the receipts here in a follow-up pull request.
