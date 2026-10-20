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

## Next

Merge, tag `v0.27.0`, verify the release (sums, attestation, GPG), re-pin Observatory Log in the PassionCode set,
update the installed engine, then run the first production enrollment when the organization server's single-use
token is in the vault slot its owner names.
