# Run 2026-10-23: release 0.29.0

Objective: release what merged since 0.28.0 (`node.lifecycle` steps L1 to L3, the organization node's expiry,
renewal, removal and harness summary, partner publisher scopes, live state under cache roots and the contract pin),
then tag, verify and install it.

Base: `0a7a59cd` (origin/main, #348 merged) on branch `release/0.29.0`. This record is committed with the version
bump; resolve the final object id with `git log -1 --format=%H` on that branch.

## What the release carries

Everything merged since `v0.28.0` (`git log v0.28.0..0a7a59cd`, sixteen pull requests):

- #333: the 0.28.0 release receipts.
- #334, #335, #340, #343: backlog rows for review findings and dated asks (OBS-180 to OBS-210, but OBS-204, which #339 wrote).
- #336: the `node.lifecycle` port plan, file by file (`docs/design/LIFECYCLE.md`).
- #337: the profile lock renamed `observatory-profile.lock.json`; `fabric-contract.lock.json` is the contract pin
  (OBS-196).
- #338: the disk guard keeps live state under a cache root (`.observatory-keep`, a Fabric descriptor's
  `paths.data`, `paths.config`, `paths.logs`) (OBS-203).
- #339: an organization certificate's expiry warning, a removed machine ended only on HTTP 401 after the CT-65
  fallback with `unknown-org` provisional, and the organization suite on every system in CI (OBS-199 warning part,
  OBS-201, OBS-204).
- #341: renewal after expiry by the key on record, within 30 days (OBS-199, contract DEC-0100).
- #342: the lifecycle broker (OBS-79 step L1).
- #344: the broker's macOS adapter and its Swift helper (OBS-79 step L2).
- #345: the harness summary `node-summary/1`, off per organization until `full org summary --org ID --consent on`,
  with the exclusion list `config/node-summary.json` (OBS-192 step 1).
- #346: a partner's publisher key trusted only within its scope; the node vectors wholly at the pin (OBS-171).
- #347: `node.lifecycle`'s six MCP tools on the local server behind access bindings, inert until a target is enrolled
  (OBS-79 step L3).
- #348: every contract mention held to the pin; the vendoring tools read and check it first (OBS-205).

## Decisions

- **0.29.0, not a patch**: new capabilities (the lifecycle tools, renewal, the harness summary); semantic versioning
  minor.
- **The plugin stays at 0.28.0**: `git diff --stat v0.28.0..0a7a59cd -- observatory/engine/skill` prints nothing, so
  `observatory-log`'s three manifests and its `SKILL.md` files keep 0.28.0 (`tests/test_plugin_manifests.py` only
  requires them to agree with each other).
- **CHANGELOG**: there was no `## Unreleased` section (no PR since 0.28.0 wrote an entry), so `## 0.29.0 (2026-10-23)`
  is written here from the pull requests' bodies and final commits, with the one-time `probes-check` exit after the
  upgrade in its summary (COMPATIBILITY.md already describes it).
- **`full probes` gets a help line**: the CHANGELOG names it as the step after the update, and
  `tests/test_docs_drift.py` refuses a named step without a line in `full --help`.
- **COMPATIBILITY.md**: the probe-receipt paragraph reads 0.29.0 instead of "unreleased"; the lessons paragraph,
  still "unreleased", reads 0.24.5, the release whose CHANGELOG section lists `observatory_lesson`.
- **Rows closed**: OBS-196 (#337), OBS-199 (#341), OBS-201 and OBS-204 (#339), OBS-203 (#338) and OBS-205 (#348)
  read `done in 0.29.0`. OBS-79, OBS-171 and OBS-192 stay open, partly done. Five rows for the review findings of
  #344, #345, #346, #347 and #348 are added (OBS-211, OBS-213 to OBS-216); OBS-212 was written by #348. Next free
  id: `OBS-217`; every id below it is a row or a declared vacancy.
- **No `RELEASE.json` marker**: nothing needs a person before installation. The one-time `probes-check` exit is
  cleared by `full probes`, which a person or the tick may run after the update.

## Checks run

On this branch, with the project's virtual environment (Python 3.14), on a machine with about 9 GB free:

- `python -m unittest tests.test_version_consistency tests.test_desktop_app tests.test_plugin_manifests
  tests.test_changelog_releases tests.test_backlog_board tests.test_docs_drift tests.test_engine_doc_copies
  tests.test_release_boundaries tests.test_licensing`: 165 tests OK. Before the `probes` help line,
  `test_docs_drift` failed on `['probes']`.
- `observatory/engine/tests/test_cli_compatibility.py`: 22 tests OK.
- `tools/check_backlog.py`: 0 problems.
- `tools/update_inventory.py` regenerated `SOURCE-INVENTORY.json`; `--check` exits 0.
- `tools/sync_engine_docs.py --check`: stale none, exit 0.
- `observatory/engine/tools/check_docs.py`: documentation current, exit 0.
- The full portable suite was not run locally; the pull request's checks run it on macOS, Linux and Windows.

## Next

Merge this pull request, tag `v0.29.0` on the merge commit, verify the release (sums, attestation, GPG), install it
with `project-observatory full update --apply`, run `project-observatory full probes` once so `probes-check` passes,
and record the receipts here in a follow-up pull request.
