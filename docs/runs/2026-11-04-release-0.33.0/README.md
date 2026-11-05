# Run 2026-11-04: release 0.33.0

Objective: release what merged since 0.32.0 (release wave 2 of OBS-325), then tag, verify and install it.

Base: `57689a43` (origin/main, #428 merged) on branch `release/0.33.0`. This record is committed with the version
bump; resolve the final object id with `git log -1 --format=%H` on that branch.

## What the release carries

Everything merged since `v0.32.0` (`git log v0.32.0..57689a43`, 7 commits):

- #421: anonymous usage and error data, on by default, with a switch per product (OBS-324): an independent
  adversarial review and a confirming round found nothing blocking; its two medium findings (a bounded flush, a
  second machine's own install id) and four low ones were fixed before it merged.
- #426: the release wheel carries the Aptabase App-Key from a repository secret, and `tools/check_package.py` admits
  that one file only in the key's shape (OBS-329); backlog rows OBS-330 to OBS-333.
- #427: a copy's point may be written `name@sha` (OBS-334).
- #428: `name_prefix` on a curated derivations entry and the finding `copy.name_unprefixed` (OBS-332); titles in en and
  ru checked with Copylot (en-US `f18f62e5`, ru-RU `38f80b3d`).
- The documentation pull requests #422 (rows OBS-323 to OBS-325), #423 (0.32.0 receipts) and #425 (rows OBS-326 to
  OBS-328).

## Decisions

- **0.33.0, a minor release**: a new behaviour on by default (anonymous data), a new command (`full analytics`), a new
  workspace switch file and a new finding type.
- **The plugin stays 0.30.0**: `git diff --stat v0.32.0..57689a43 -- observatory/engine/skill` prints nothing.
- **CHANGELOG**: `## Unreleased` becomes `## 0.33.0 (2026-11-04)` with a summary (Copylot en-US `78163099`, no
  findings), and gains the entries of #426, #427 and #428, which had none.
- **Rows**: OBS-324, OBS-329, OBS-332 and OBS-334 read `done in 0.33.0`.
- **The App-Key**: the analytics owner registered the app on 2026-11-04 (board thread `b99a99ce`); writing the key
  into the repository secret `APTABASE_APP_KEY_PROJECT_OBSERVATORY` is the operator's step (this session's permission
  guard refused a secret-store write). The release does not wait for it: without the secret the wheel ships no key,
  the run says so in a warning, and nothing is collected until a release carries one.

## Checks run

On this branch, with `~/.local/share/project-observatory-venv/bin/python3`:

- `python -m unittest tests.test_version_consistency tests.test_desktop_app tests.test_plugin_manifests
  tests.test_changelog_releases tests.test_backlog_board tests.test_docs_drift tests.test_engine_doc_copies
  tests.test_release_boundaries tests.test_licensing`: 167 tests OK.
- `python -m unittest discover -s tests` from the repository root: 339 tests OK (1 skipped).
- `tools/check_backlog.py`: 0 problems. `tools/update_inventory.py`: exit 0. `tools/sync_engine_docs.py --check`:
  stale none. `python -m compileall -q observatory`: exit 0. `observatory/engine/tools/check_docs.py`: exit 0.
- Each pull request's own CI ran the engine suite on macOS, Linux and Windows; the release pull request runs it again.

## Receipts

- Release PR #429 merged as `2426ea16`. Its first macOS run timed out in `test_session_reload` at 121.5 s against its
  120 s budget (run `41921013085`); the same suite took 47 s to 87 s on macOS in #416, #419, #420 and #428, before
  and after OBS-324, so the rerun was the answer and the thin budget is a backlog row.
- Tag `v0.33.0` (annotated, object `c599083b`) on `2426ea16`. Release run `41931623833`: guard, wheel, manifest,
  macOS, Linux x64 and arm64, Windows arm64 green on the first attempt. Windows x64 failed twice at signing:
  `Invoke-ArtifactSigning` was not recognized after a cache hit on `ArtifactSigning-0.1.8`, the module the Windows
  arm64 job of the same run had saved at 23:53 UTC (the cache key names no architecture). Deleting that one cache entry
  (`10030178796`) and rerunning the failed jobs signed and published it: published 2026-11-05T00:25:26Z, 19 assets.
  The shared `windows-signing@v1` action's cache key is reported to its owner.
- Verified after publishing: `shasum -a 256 -c` OK on `project_observatory-0.33.0-py3-none-any.whl`;
  `gpg --verify SHA256SUMS.asc SHA256SUMS`: good signature from the PassionCode.ai releases key (EdDSA
  `63B30DC324BD697487AA31944FAFB8AEC803B6A7`); `gh attestation verify` on the wheel with `--signer-repo
  passioncode-ai/.github`: exit 0. The wheel carries no `analytics-app-key`: the repository secret is not set yet.
- Installed on the operator's Mac with `project-observatory full update --version 0.33.0 --apply` (`status: updated`).
  `full doctor` reports 0.33.0 and no coverage warnings. `full analytics status`: on (default), `app_key: absent`,
  `collecting: false`.
- The private copies registry now gives `name_prefix` on its 14 entries (no `bad-prefix`). `full copies list` shows 6
  relations and refuses 8 entries as `unknown-repository`: those copies are not repositories in this machine's
  registry, a separate open item.
