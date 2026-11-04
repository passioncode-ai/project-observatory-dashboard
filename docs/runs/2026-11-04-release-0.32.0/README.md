# Run 2026-11-04: release 0.32.0

Objective: release what merged since 0.31.0, then tag, verify and install it.

Base: `e97afe2c` (origin/main, #419 merged) on branch `release/0.32.0`. This record is committed with the version
bump; resolve the final object id with `git log -1 --format=%H` on that branch.

## What the release carries

Everything merged since `v0.31.0` (`git log v0.31.0..e97afe2c`, 10 commits). Each pull request with code passed an
independent adversarial review and two consecutive confirming rounds with nothing blocking:

- #418: copies and moves of repositories: a private `config/derivations.json`, a copy's own `.copy/upstream.json`,
  `registry/derivations.json` with an offline behind count, the findings `copy.behind_source`,
  `copy.relation_unrecorded`, `copy.record_differs`, `copy.moved_source_open`, `project.detail` showing the relation
  both ways, and `full copies list|plan` (OBS-321). `project.resolve` is deliberately unchanged.
- #409: a second-machine restore hands out `ssh-keys rekey NAME --fingerprint FP` and then `--apply` (OBS-300).
- #414: `full machine-move status` M3 hands out the same rekey steps, reuses this machine's key on a re-run, and the
  restore's notes for a kept `~/.ssh/config`, refused aliases and IdentityFile lines (OBS-306, items 1 to 4, 6, 7).
- #411: the disk guard at `critical` clears all unused Docker build cache and trims running Colima and Lima VMs, only
  against a Docker daemon on this machine (OBS-305).
- #416: the keyserver reads off a refused request's body before it closes, so a Windows client gets its answer
  (OBS-309); one `machine_backup` check of the OBS-278 class added to `tools/windows-known.json`.
- The documentation pull requests #410 (0.31.0 receipts), #413 (row OBS-308, COMMERCIAL-LICENSE.md from the
  template), #415 (OBS-310), #417 (the agent map decided, OBS-311 to OBS-319) and #419 (OBS-322).

## Decisions

- **0.32.0, a minor release**: a new command family (`full copies`), a new workspace file and four new finding
  types, and the disk guard acting differently at `critical`.
- **The plugin stays 0.30.0**: `git diff --stat v0.31.0..e97afe2c -- observatory/engine/skill` prints nothing.
- **CHANGELOG**: `## Unreleased` becomes `## 0.32.0 (2026-11-04)` with a summary.
- **Rows**: OBS-300, OBS-305, OBS-309 and OBS-321 read `done in 0.32.0`; OBS-306 reads `partly done in 0.32.0 (#414)`
  with item 5 remaining; OBS-297, planned for 0.32.0 and not in it, reads `planned for 0.32.x`.

## Checks run

On this branch, with `~/.local/share/project-observatory-venv/bin/python`:

- `python -m unittest tests.test_version_consistency tests.test_desktop_app tests.test_plugin_manifests
  tests.test_changelog_releases tests.test_backlog_board tests.test_docs_drift tests.test_engine_doc_copies
  tests.test_release_boundaries tests.test_licensing`: OK.
- `python -m unittest discover -s tests` from the repository root: 333 tests OK (1 skipped).
- `tools/check_backlog.py`: 0 problems. `tools/update_inventory.py`: exit 0. `tools/sync_engine_docs.py --check`:
  exit 0. `python -m compileall -q observatory`: exit 0. `observatory/engine/tools/check_docs.py`: exit 0.
- Each pull request's own CI ran the engine suite on macOS, Linux and Windows; the release pull request runs it again.

## Receipts

To be filled in after the tag.

## Next

The live Sentry steps (OBS-292) wait for two tokens a person makes; OBS-320, OBS-322 and the agent-map rows OBS-313 to
OBS-319, the tick's reliability (OBS-318) first.
