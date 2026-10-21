# Run 2026-10-20: release 0.27.0

Objective (operator request, 2026-10-20): finish the release iteration's open engine work in one pass and ship it.

Base: `98219312` (origin/main, #302 merged) on branch `release/0.27.0`. This record is committed with the version
bump; resolve the final object id with `git log -1 --format=%H` on that branch.

## What the release carries

Everything merged since `v0.26.0`: OBS-155 (#306, the DigitalOcean door's team side and a per-team collector),
OBS-161 (#308, organization enrollment, `full org`, pinned to contract CT-65 / DEC-0094 at `de9eae4d`), the
Cloudflare presets `cache-purge` (#292) and `email-sending-domain` (#302), and `google-auth` 2.61.0 with its lock
(#316, which supersedes Dependabot #283). Each pull request passed an adversarial and a confirming review round.

## Decisions

- **0.27.0, not a patch**: new capabilities (semantic versioning, minor).
- **The plugin moves with the engine** to 0.27.0: its `handling-secrets` skill names the two new Cloudflare presets.
- **The 0.26.0 section of CHANGELOG is restored to the tag's text**: merges after the tag had placed the entries of
  #292, #302 and #316 inside it; they move to 0.27.0, and `diff` against `git show v0.26.0:CHANGELOG.md` is empty.
- **Rows closed**: OBS-155 and OBS-161 (this release), and OBS-156, OBS-157, OBS-159, OBS-160, OBS-162, which shipped
  in 0.26.0 and whose owner is this run.
- **Closed without merge** (content already on main through 0.25.0): #258, #259, #261, #263, #209; #283 replaced by #316.
- **No `RELEASE.json` marker**: nothing needs a person before installation. Enrollment against a production
  organization server is an operator-run step after installation (`full org enroll`), not an upgrade step.

## Published

Recorded 2026-10-21 from the release itself (`gh release view v0.27.0 -R passioncode-ai/project-observatory-dashboard`):
`v0.27.0` on `e6e8673c` (#320), published 2026-10-20T16:47:49Z, not a draft or prerelease, 19 assets (the wheel,
the macOS zip, Linux AppImage and deb and Windows installers for x64 and arm64 with their signatures and receipts,
`latest.json`, `SHA256SUMS`, `SHA256SUMS.asc`).

- Release workflow run 39454033288: attempt 1 failed in `guard`, because `check.yml` run 39454032758 on the tagged
  commit had failed (`test (ubuntu-latest, 3.14)`, the complete-engine step: `tests/test_machine_backup.py` errored in
  a restore case; the detached `git gc --auto` a restore's fetch could leave in a test's temporary folder is what
  #321 removed after the release). Both runs were re-run; attempt 2 of
  each succeeded, and attempt 2 of the release run published.
- Re-downloaded `project_observatory-0.27.0-py3-none-any.whl` and `ProjectObservatory-0.27.0-macos.zip`:
  `shasum -a 256 --ignore-missing -c SHA256SUMS` reads OK for both; `gh attestation verify <file> -R
  passioncode-ai/project-observatory-dashboard --signer-repo passioncode-ai/.github` exits 0 for both; `gpg --verify
  SHA256SUMS.asc SHA256SUMS` reads a good signature by the organization's release key.

## Next

Re-pin Observatory Log 0.27.0 in the PassionCode set and update the installed engines (`full update --apply`; this
record does not observe either), then run the first production enrollment when the organization server's single-use
token is in the vault slot its owner names.
