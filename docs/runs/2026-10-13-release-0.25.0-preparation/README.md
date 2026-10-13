# Release 0.25.0 preparation

## Current integrated candidate

The accepted wave4 source `7d260bcd5b44e75a31468b76783a369454cf1593` is now merged
with the local version preparation. It includes the accepted rotation, checkout, scoped
Heroku and cache coverage work plus the pipeline contract-test dependency correction.
Application 0.25.0 and plugin 0.22.1 remain unchanged. The cache changelog addition
records metadata coverage and its limits, without promising safe reclamation.

The independently accepted saved-coverage reader source
`5aa9eac9477398e39eb60466c695a9363e290df5` is included as a fix-only cherry-pick.
Its inventory conflict was resolved by regenerating against this candidate after
clearing the unmerged index state; the first generation refused that state safely.
The independently accepted per-issued-key activity source
`3c3510ea2eaa34aa8d9403d56e2e714160ac379c` is now included. Its docs/scenario append
conflicts preserve both saved-coverage and spend sections; inventory was regenerated.
This supersedes the earlier exclusion of PR #249 below after protected source integration.
Retired-key and full J-11 project aggregation remain separate.
No tag, installation or release was attempted. Final checks are appended in checks.json;
the earlier preparation record below remains historical.

Next: route the exact integrated source through the release PR and required hosted
checks; tag only its actual main merge SHA after exact-SHA acceptance. Recheck signing
configuration and tag availability before publication.

## Integrated source checks

[checks.json](checks.json) now records 27 focused root cases and three portable suites:
67 unittest cases plus 131 MCP wire assertions, no skips. Three strict plugin validators,
source inventory, document copies, static manifest URLs, signed Windows preflight and
whitespace checks pass. The private identifier gate passes for the current tree and new
history since `fadba69`; its precise file/blob counts and source hashes are in the receipt.
Brand lint remains zero errors and 2043 warnings. These are synthetic/source checks;
hosted release-SHA, native assets, provider and installed-runtime acceptance are not run.

## Final source follow-up

Accepted guard diagnostic source `da3299321dc987b5c61c2ab15d27da41c7648f34` is
included: failed/busy cache commands record structured reasons and exit codes without
copying child stdout/stderr into cleanup receipts or the journal. The focused real-child
regression passes all four cases; version consistency also passes four cases.

Handling-secrets guidance now matches retained rotation reservations, identity checks
and uncertain exposure. It explicitly leaves reconciliation/full 5200-case convergence
acceptance open and does not imply standalone commands enforce every project policy.
Plugin 0.22.1 is unchanged. Four manifest cases, strict plugin validation, inventory
and diff checks pass. These documentation and diagnostic corrections do not establish
live provider, installed-runtime or native asset acceptance.

## Issued-key activity integration

Accepted source `3c3510ea` restores the separate per-issued-key spend capability after
independent review. Four combined synthetic suites pass: 41 unittest cases and
486 credential assertions, no skips. They include activity, rotation, credential
compatibility and the corrected provider-boundary fixtures from `1d216101`.
Fourteen focused version/plugin/document-copy cases, static manifest URLs, inventory
and whitespace checks also pass. The actual CLI help was separately checked after
removing stale rotation advice in `a045276`.

The original PR #249 may be marked superseded only after protected source integration.
The new report does not include retired keys or close full project-spend accounting.

## Further review corrections and remaining rotation failures

The model binding (`77866893`, receipts `ee2a56c`) and consumer-safe revoke
(`23b4249`, receipts `e22f46f`) are now integrated. Portable suite registration
preserves issued-key activity, rotation accounting and both new suites. App 0.25.0
and plugin 0.22.1 remain unchanged. Documentation append conflicts preserve both
spend and revoke guidance; source inventory was regenerated after integration.

The latest [full-rotation baseline](../2026-10-13-consumer-safe-revoke/raw/baseline-128.json)
completes 128 cases at source `23b4249`: **28 PASS, 100 FAIL**. It supersedes the
older model-bound 62 PASS / 66 FAIL baseline for diagnosis, without erasing it.
This source-bound baseline was not rerun on this integration tree. Full 5,200-case
acceptance and reconciliation remain open. Seed 7000104's invalid 0.03/four-slot
split must be rejected unchanged; its separate successful rotation leaving 0.034
capacity above a 0.03 cap remains a real strict-ceiling defect. Neither outcome
settles the other, and the strict invariant has not been weakened.

Cleanup diagnostic follow-ups `c5d39b8` and `bd64afb` are included together. The
first was independently rejected for retaining extra fields in otherwise-valid
summaries and ignoring nonzero child exits. The correction reconstructs bounded
known fields and rejects invalid/failed child receipts; the independent correction
review passed. See [the retained correction record](../2026-10-13-cleanup-command-diagnostics/README.md).
Final bounded integration checks pass: 61 key-management cases and 486 credential
assertions without skips; 128 disk-guard cases with two platform skips; and 14
version, manifest and document-copy cases. Inventory and whitespace checks pass.
`checks.json` binds these results to source hashes. No tag, provider mutation,
installed acceptance or automatic-cleanup enablement is claimed.

## Log-rotation correction

Independently accepted source `931c1d6` is integrated. The corrected log policy
refuses linked directories, source/generation symlinks, nonregular files and
hardlinks before rotation or permission repair. Copy/truncate verifies its source
descriptor and exclusively creates its destination, syncing the copy before
truncation. This maintenance path runs independently of automatic cleanup.
The [source packet](../2026-10-13-log-rotation-safety/README.md) preserves the
original synthetic failures and the remaining concurrent-append, external-directory
race and refusal-observability limits. No live logs were changed by this validation.

The lifecycle suite passes 60 cases (including 12 log-policy cases), and maintenance
passes 142 cases with one skip. Four version cases and inventory/whitespace checks
also pass. The receipts in `checks.json` cover this integrated source. The broader J3 model/provenance work belongs to a later wave and is excluded
from this candidate. Full J3 acceptance remains red as recorded above. Final combined
independent review and protected source checks precede readiness, merge or tagging.

## Initial preparation snapshot (superseded integration status)

This is a local release-preparation candidate, not a published or installed release.
Application versions are 0.25.0; the companion plugin version is 0.22.1. The source
base is `fadba69ff3d7438aadd97c6b98c3b9e2bf83702f`.

Before this candidate can be released, integrate the independently reviewed rotation
accounting (`f07ff79beefafb45741511654864786e5f7c9498`), checkout candidate inventory
(`aafcfd5d78f38d5fb41813cb04473bb78cec0985`) and scoped Heroku preservation
(`da10f4c0c3cd177dbdd2c03c3e9b1222f1929057`). Their changelog text is prepared here;
it is not proof those implementations are already in this preparation tree.
Cache work is excluded until accepted. Regenerate the source inventory after integration.

## Bounded changes

Ten application version files, six plugin version files, CHANGELOG.md and the generated
engine source inventory change. The generated compatibility copy comes from
`tools/sync_engine_docs.py`. Cargo.lock changes only the `project-observatory-desktop`
package; its executable is named `project-observatory-app`. No dependency is bumped.
Published changelog sections are retained; the former Unreleased content belongs to
0.25.0, with additional recovery fixes and an explicit unresolved-rotation boundary.

The release environment's `AZURE_SIGNING_ENABLED` readback is `true` (2026-10-13).
The signed Windows preflight passes. Historical unsigned-version disclosures remain;
the actual new release's signing receipt must establish its outcome. Recheck the
setting before publication: turning signing off requires the exact unsigned disclosure
that `tools/desktop_release.py preflight` enforces.

## Validation and next task

[checks.json](checks.json) records 27 focused test cases: version 4, plugin manifests 4,
desktop contract 11, doc copies 6 and release workflow 2. Inventory, static manifest
validation (25 URLs; network resolution not run), signed desktop preflight and diff
checks pass. Brand lint reports zero errors and 2043 warnings, not a clean pack.

The tag query found no `v0.25.0*` remote tag at preparation time; it reserves nothing.
No tag, push, hosted workflow dispatch, install or cleanup enablement was performed.
No mandatory manual upgrade step was established, so no RELEASE.json marker was added;
reassess after final source integration. Existing rotation and live-provider limitations
are not relabelled complete by this release note.

Exact next task: integrate the accepted source wave, regenerate inventory, verify all
version homes and source acceptance on the final candidate, then route the release PR.
Only its actual main merge commit may be tagged. The guard requires main ancestry and
passing check.yml on that exact SHA; this product uses approval by checks. Asset and
installed-runtime verification remain separate required release steps.
