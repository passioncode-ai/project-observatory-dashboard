# Run 2026-11-06: release 0.34.0

Objective: release the work merged since 0.33.1 (the first wave of the move to another computer and of several OS
accounts on one computer, products' daily digests, the daily learning review), then tag, verify and install it.

Base: origin/main after #453 (OBS-350 part 1) on branch `release/0.34.0`. This record is committed with the version
bump; resolve the final object id with `git log -1 --format=%H` on that branch.

## What it releases

#432 (OBS-333), #434 (OBS-335), #436 (OBS-326), #437 (OBS-323), #440 (OBS-343, OBS-344), #442 (OBS-339 to OBS-342),
#444 (installed engine under a Git repository), #445 (OBS-349 part 1), #446 (OBS-347, OBS-348), #447 (OBS-330),
#449 (OBS-278), #450 (OBS-331 part a), #451 (OBS-355 part 1) and #453 (OBS-350 part 1). The backlog rows read
`done in 0.34.0` or `open, part ... done in 0.34.0` in the same change.

## Decisions

- **0.34.0, a minor release**: new commands, a new capability, a new server refusal and new workspace state, each
  additive (`docs/COMPATIBILITY.md` sections now name 0.34.0 instead of "the release after 0.33.1").
- **`ops.digest.record` moves into the manifest**: from `fabric/unreleased-capabilities.json` (now empty) into
  `fabric-agent.json`, provider revision 10, the content hash recomputed with `tools/fabric_hash.py`; the probe count in
  `tests/test_docs_current.py` is 11 and the revision in `tests/test_conformance_receipt.py` is 10;
  `fabric/FABRIC-CONFORMANCE.md` says revision 10 and pins the capability's schemas at `v0.34.0`;
  `tests/test_ops_digest_record.py` now holds that the capability is declared and no longer waiting.
- **The plugin stays 0.30.0**: nothing under `observatory/engine/skill/plugins/` changed since 0.33.1.
- CHANGELOG `## 0.34.0 (2026-11-06)` checked with Copylot (en-US, editorial): report `c7098494`, score 100, three
  rule-of-three suggestions, nothing blocking.

## Checks run

On the release branch before #453 joined it: `tools/fabric_hash.py --check` contentHash OK;
`tools/check_manifest_urls.py` 0 problems; `tools/publish_contract.py --local` bundled references valid;
`tests/test_ops_digest_record.py`, `tests/test_docs_current.py`, `tests/test_conformance_receipt.py` OK;
`observatory/engine/tools/check_docs.py` current; `tools/sync_engine_docs.py --check` stale none;
`tools/check_backlog.py` 0 problems. After #453 was merged in (`66e56d6e`): the root release suites
(`test_version_consistency`, `test_desktop_app`, `test_plugin_manifests`, `test_changelog_releases`,
`test_backlog_board`, `test_docs_drift`, `test_engine_doc_copies`, `test_release_boundaries`, `test_licensing`,
`test_analytics_catalogue`) OK; the three manifest tests OK again; `fabric_hash.py --check` contentHash OK;
`check_manifest_urls.py` 0 problems; `check_paths.py` 0 violations; `check_docs.py` current; the source inventory
check passed. `docs/COMPATIBILITY.md` sections that read "the release after 0.33.0" also name 0.34.0, because
0.33.1 carried no code. Backlog row OBS-377 (authority levels, board thread `eefa7959`) rides in this PR.

## After the release

- Tell Fabric (board thread `f7758225`) and Fabric Dashboards the version; tell Workspace (thread `0159c474`) the
  version and the commands for runbook steps 2.3 to 2.6, before 2026-11-11 (OBS-325 wave 2).
- The operator issues the digest binding for each product that sends digests over stdio:
  `project-observatory full access-binding issue service:fabric --project project:SLUG --scope ops.digest.record --effect propose --days 365 --token-file PATH`.

## State now

- Open: the release PR, then the tag `v0.34.0`, its run, verification and the local install.
- Waits: none beyond the release run (watched by this session).
- Next task: merge the release PR, push the tag, verify, install, record the receipts here.
