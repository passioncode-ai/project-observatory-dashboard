# Release 0.25.0 preparation

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
