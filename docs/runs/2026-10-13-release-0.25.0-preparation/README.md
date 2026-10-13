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
The separate per-key activity/spend PR #249 is excluded and remains open: its activity
functions and CLI are not present here.
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
