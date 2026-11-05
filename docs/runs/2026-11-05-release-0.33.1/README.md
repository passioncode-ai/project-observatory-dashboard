# Run 2026-11-05: release 0.33.1

Objective: a patch release whose wheel carries the analytics App-Key, so OBS-324's data is sent; then tag, verify that
the wheel carries the key, and install it.

Base: origin/main after #430 (0.33.0 receipts) on branch `release/0.33.1`. This record is committed with the version
bump; resolve the final object id with `git log -1 --format=%H` on that branch.

## Why a patch

0.33.0 (2026-11-05 00:25 UTC) was built before the repository secret `APTABASE_APP_KEY_PROJECT_OBSERVATORY` existed,
so its wheel carries no `analytics-app-key` and `full analytics status` reads `collecting: false`. The analytics owner
set the secret at 01:57 UTC from the vault slot it registered (board thread `b99a99ce`). No code changed: the release
workflow writes the key into the wheel and `tools/check_package.py` admits it only in the key's shape (OBS-329, #426).

## Decisions

- **0.33.1, a patch**: no behaviour changes; the build now has what the 0.33.0 code expects.
- **The plugin stays 0.30.0.** CHANGELOG `## 0.33.1 (2026-11-05)` (Copylot en-US `a320dae3`, no findings).

## Checks run

With `~/.local/share/project-observatory-venv/bin/python3`: the release suites (`test_version_consistency`,
`test_desktop_app`, `test_plugin_manifests`, `test_changelog_releases`, `test_backlog_board`, `test_docs_drift`,
`test_engine_doc_copies`, `test_release_boundaries`, `test_licensing`) OK; `tools/check_backlog.py` 0 problems;
`tools/sync_engine_docs.py --check` stale none; `observatory/engine/tools/check_docs.py` exit 0. No code changed since
0.33.0, whose full root suite ran 339 tests OK.

## Receipts

To be filled in after the tag.
