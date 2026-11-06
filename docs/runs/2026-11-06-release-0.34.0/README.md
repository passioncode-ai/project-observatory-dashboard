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

## Receipts

- Release PR #454 merged as `fa4c39ec`. Its `check.yml` run on main (`42266308925`) first failed on Windows with three
  TIMEOUTs the known list does not admit (`docs_current`, `export_scope`, `filesystem_scan`) and passed on a rerun of
  the Windows job. Tag `v0.34.0` (annotated, object `46af1e6f`) on `fa4c39ec`.
- Release run `42279745113`: the guard refused the first attempt because the `check.yml` run the tag started
  (`42279744699`) failed on Windows with a `harness` TIMEOUT; after a rerun of that Windows job passed, a rerun of
  the release passed every job (guard, wheel, manifest, Linux x64 and arm64, Windows x64 and arm64, macOS, feed,
  publish). Published 2026-11-06T21:21:34Z, 19 assets. The four Windows TIMEOUTs are filed as a row (known list or
  budgets, OBS-278).
- Verified after publishing: `shasum -a 256 -c` OK on `project_observatory-0.34.0-py3-none-any.whl`;
  `gpg --verify SHA256SUMS.asc SHA256SUMS`: good signature from the PassionCode.ai releases key; `gh attestation verify`
  on the wheel with `--signer-repo passioncode-ai/.github`: exit 0; the wheel carries `analytics-app-key`.
- Installed on the operator's Mac with `project-observatory full update --version 0.34.0 --apply`; `full doctor` reports
  0.34.0 and the service `project-observatory.default` answers 0.34.0 on its well-known document. Doctor's verdict:
  `clean: false` with two warnings, `machine-backup-not-carried` and `backups-vault-store-not-carried` (the external
  vault is named and no snapshot has carried it yet; the next snapshot with a passphrase does). `loopback`: one person's
  account on this host, so the peer check is `auto` and inactive.
- Told: Fabric (board thread `f7758225`, message `dd2b63da`), Fabric Dashboards (thread `5d2a61f1`, `661bd511`),
  Workspace with the runbook steps 2.3 to 2.6 (thread `0159c474`, `6e83433c`).

## State now

- Done: 0.34.0 released, verified, installed and announced.
- Open: OBS-350 part 2 (the restore side), OBS-355 part 2 (after the contract tag with DEC-0154), the Windows TIMEOUT row.
- Waits: none for this release.
- Next task: the next row after the shared source of truth (the operator's reconciliation pause).
