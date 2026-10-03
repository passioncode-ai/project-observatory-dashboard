# 2026-10-03 — release 0.14.0: the lifecycle contract, agent memory, releases from CI

The first release built, signed, notarized, attested and published by
`.github/workflows/release.yml`. What changed for a user is `CHANGELOG.md` → 0.14.0. This
record uses synthetic names only.

## What it carries

| Pull request | Merge | What |
|---|---|---|
| #125 | `c67be51` | the product lifecycle contract: no MCP probe in the tick, no privacy-guarded place in background sizing, bounded steps and tick, an idle server, one log policy, `stale-server`, minimal plist `PATH`, the lifecycle watch, build pruning ([record](../2026-10-03-lifecycle-contract/README.md)) |
| #131 | `1cf1f7b` | retention owns the pre-upgrade database copies |
| #127, #130 | `d6f2b33`, `b9e5473` | agent memory for workflows and its hardening ([record](../2026-10-03-agent-memory-workflows/README.md), [hardening](../2026-10-03-agent-memory-hardening/README.md)) |
| — | `d719c76`, `b02e3df`, `f558758` | `notarize.sh` and the CI release with its approval rule |
| #132 | `567b52d` | the release: version 0.14.0 in the five files `tests/test_version_consistency.py` reads, the changelog section |

`observatory-log` is unchanged at 0.14.0: no plugin file changed since `v0.13.0`, so the
launcher's pin needs no move.

## Release

- Before #132 merged: `run_portable.py` (what `full check` runs) 205/205 suites PASS, root tests
  124 OK, the source inventory current, the privacy gate passed; the three required checks passed
  on the release head `83f4e66`.
- The annotated tag `v0.14.0` (object `536f577`) points at `567b52d94c7cb07737a1cf1100349c115b734c6c`.
- Release run [37149027804](https://github.com/passioncode-ai/project-observatory-dashboard/actions/runs/37149027804):
  `wheel`, `macos` and `publish` succeeded. The `release` environment was approved by the
  operator; the agent that pushed the tag did not approve.
- Published 2026-10-03T20:27:19Z, not a draft and not a prerelease, with four assets:
  `project_observatory-0.14.0-py3-none-any.whl`, `ProjectObservatory-0.14.0-macos.zip`,
  `SHA256SUMS`, `SHA256SUMS.asc`.

## Verification of the downloaded assets

| Check | Result |
|---|---|
| `shasum -a 256 -c SHA256SUMS` | both files OK — wheel `c29af67b5df365f3a621c086a4ebca61e39af1c1645d27b8f7d4b77347e96562`, app zip `ae3b9b07f2a613ddd1d22f751acc9e4d0e6b9b9295befcff0215c7f98685a711` |
| `gpg --verify SHA256SUMS.asc SHA256SUMS` (organization key imported into a throwaway keyring) | Good signature, key `63B3 0DC3 24BD 6974 87AA 3194 4FAF B8AE C803 B6A7` |
| `gh attestation verify <file> -R passioncode-ai/project-observatory-dashboard --signer-repo passioncode-ai/.github` | exit 0 for the wheel and the app zip |
| `tools/check_package.py` on the wheel, run from a checkout of the tag | `passed: true`, 503 runtime files, `failures: []` |
| the app in the zip: `codesign -dv`, `spctl -a -vv`, `stapler validate` | Developer ID authority chain, hardened runtime; `source=Notarized Developer ID`; the stapled ticket validates |

**Found:** `AGENTS.md` → Releasing told a maintainer to verify with
`gh attestation verify <file> -R <repository>` alone, which fails with "verifying with issuer
sigstore.dev": the attestation is signed by the shared workflow in `passioncode-ai/.github`.
This change adds `--signer-repo passioncode-ai/.github` there, as the organization's
release-signing README already says. `SHA256SUMS` itself is GPG-signed, not attested.

## Installation on the maintainer's machine

- `project-observatory full update --version 0.14.0 --apply` (the first try was refused with
  "GitHub API rate limit reached for anonymous requests"; it ran after the limit reset):
  `status: updated`, `rolled_back: false`, `degraded: []`, both jobs stopped and restarted.
  The census of session MCP servers is absent from this report because the update ran in the
  0.13.0 updater, which has no census; 0.14.0's updater reports it from the next update on.
- `project-observatory --version` prints 0.14.0; `full doctor` exits 0; `/health` answers
  version 0.14.0 with `silent_after_s` 480.
- Both installers re-run from the installed engine, so the plists carry 0.14.0's shape:

  | Job | `launchctl print` | Environment |
  |---|---|---|
  | tick | `program = /bin/bash`, `exit timeout = 30` (was 5) | minimal `PATH` of 8 directories (was 20, including two plugin `bin` folders of deleted versions); `OBSERVATORY_TICK_CEILING_SECONDS=1500`; `OBSERVATORY_PYTHON` the virtual environment's interpreter |
  | server | running, `program` the virtual environment's interpreter, `exit timeout = 40` | the same minimal `PATH` |

  No plist names a Homebrew `Cellar` path.

## Not done here

- The Mac app on this machine is still the build installed before; installing the notarized
  0.14.0 zip is a person's step (`install-app.sh` quits the running app).
- passioncode.ai still names 0.13.0; the website update is its own change.

## Next task

Install `ProjectObservatory-0.14.0-macos.zip` on the maintainer's machine and name 0.14.0 on
passioncode.ai; then the deferred lifecycle item: a signed launcher as the jobs'
`ProgramArguments[0]` ([AGENTS.md → Lifecycle](../../../AGENTS.md#lifecycle)).
