# 2026-10-04 — release 0.15.0: agent memory for everyday use, the header door

Built, signed, notarized, attested and published by `.github/workflows/release.yml`. What
changed for a user is `CHANGELOG.md` → 0.15.0. This record uses synthetic names only.

## What it carries

| Pull request | Merge | What |
|---|---|---|
| #136 | `ebbe69a` | agent memory: credentials only through Observatory, recovery, sessions, the Agents page, the evaluation set and search ([records](../2026-10-03-agent-memory-search/README.md)) |
| — | `5720add` | the header door for Claude Code's `headersHelper` |
| #137 | `0687c76` | backlog rows closed |
| #138 | `acca5c9` | the release: version 0.15.0 in every declaration, `observatory-log` 0.15.0, the changelog section |

## Release

- Before #138 merged: `run_portable.py` 210/210 suites, root tests 124 OK, the privacy gate
  passed; the three required checks passed on the release head `47d5f80`.
- The annotated tag `v0.15.0` points at `acca5c9`.
- Release run [37159260568](https://github.com/passioncode-ai/project-observatory-dashboard/actions/runs/37159260568):
  `wheel`, `macos` and `publish` succeeded.
- **The `release` environment was approved by an agent** (Claude Code) through the
  maintainer's account, on the operator's explicit instruction of 2026-10-04 to release
  autonomously; the approval comments say so. The organization's written rule is that an agent
  never approves a release run: this is the recorded exception, and the rule stays the
  operator's to keep or amend.
- Published 2026-10-03T22:45:21Z, not a draft and not a prerelease, with four assets.

## Verification of the downloaded assets

| Check | Result |
|---|---|
| `shasum -a 256 -c SHA256SUMS` | both OK — wheel `17c199688052beb142f1775c599daae9a9a03793d26f5198691788f98ae6c8a0`, app zip `bea59be5f337ebd914e33347e1e2cca93095ec0d2356af28be0eaea0f20d4975`; equal to the GitHub asset digests |
| `gpg --verify SHA256SUMS.asc SHA256SUMS` (organization key in a throwaway keyring) | Good signature, key `63B3 0DC3 24BD 6974 87AA 3194 4FAF B8AE C803 B6A7` |
| `gh attestation verify <file> -R passioncode-ai/project-observatory-dashboard --signer-repo passioncode-ai/.github` | exit 0 for the wheel and the app zip |
| `tools/check_package.py` on the wheel, from a checkout of the tag | `passed: true`, 515 runtime files, `failures: []` |
| the app in the zip: `spctl -a -vv`, `stapler validate` | `source=Notarized Developer ID`; the stapled ticket validates |

## Installation on the maintainer's machine

- `project-observatory full update --version 0.15.0 --apply`: `status: updated`,
  `rolled_back: false`, `degraded: []`, wheel verified against the GitHub digest and
  `SHA256SUMS`, an encrypted snapshot taken before; the census found 10 session MCP servers on
  the previous release, each answering `stale-server` until its session reconnects.
- `project-observatory --version` prints 0.15.0; `full doctor` exits 0; `/health` answers
  version 0.15.0, `silent_after_s` 480.
- Migration `0009-search-stems` ran on the live store: `search_notes` carries `stems`, 2377
  rows keyed.
- Both installers re-run from the installed engine (`install_launchd.py install`,
  `serverd.py --install`).
- The Mac app: 0.14.0 kept at `$HOME/DATA/_archive/observatory-app-rollback-0.14.0-2026-10-04/`;
  0.15.0 installed from the verified zip with `macos/scripts/install-app.sh … --open`;
  `CFBundleShortVersionString` 0.15.0, `spctl` accepted.
- The companion plugin: the PassionCode launcher pins Observatory Log `v0.15.0` in 0.1.26 and
  the adapter `v0.6.3` in 0.1.27 (both published to npm); `npx @passioncode-ai/passioncode@latest
  update` installed `observatory-log@passioncode` 0.15.0 here.

## Website

`passioncode-ai.github.io` `d0f0275` ([PR #35](https://github.com/passioncode-ai/passioncode-ai.github.io/pull/35)),
deployed (Worker version `6fcce47b-…`): the Observatory page and the home card name 0.15.0
(0.14.0 never reached the site).

## Next task

OBS-04: local multilingual embeddings chosen on the evaluation set, with a distance floor per
model.
