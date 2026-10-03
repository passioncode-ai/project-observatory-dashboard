# 2026-10-03 — release 0.13.0: three audit-and-fix runs over 0.12.0

The operator asked for a correct build, no errors, Keychain-safe behaviour, a clean
onboarding for a new user, key management that works, an app in the PassionCode tones and
every UX scenario walked. The work was three independent audit-and-fix runs, one after
another, each fixing what the previous one missed, with documentation true to the code at
the end, then a release installed on the machine and named on the website. This record
uses synthetic names only.

## The three runs

| Run | Pull request | Found | Fixed | Report |
|---|---|---|---|---|
| 1 | #119 | 77 | 73 (one in part) | [run 1](../../reports/2026-10-03-observatory-audit-run-1/README.md) |
| 2 | #120 | 29 | 27 | [run 2](../../reports/2026-10-03-observatory-audit-run-2/README.md) |
| 3 | #121 | 67 | 66, and one in part | [run 3](../../reports/2026-10-03-observatory-audit-run-3/README.md) |

What each run left open, and why, is in its own report. What changed for a user is
`CHANGELOG.md` → 0.13.0.

## Release

- Release pull request #122 was merged by rebase as `c7a8912`. All three required checks
  passed on it: `test (ubuntu-latest, 3.11)`, `test (ubuntu-latest, 3.14)` and
  `test (macos-latest, 3.14)`.
- The annotated tag `v0.13.0` points at `c7a89125edcd5ee1a8141feb55d4aca2c828e834`.
- `observatory-log` is 0.14.0 in its three manifests and every `SKILL.md`, because its hook
  and its `handling-secrets` text changed.

Local gates on the release branch, before the pull request:

| Check | Result |
|---|---|
| `project-observatory full check` (Python 3.14) | 202 of 202 suites PASS, 0 FAIL |
| `python -m unittest discover -s tests` | 107 tests, OK |
| `swift test` (`macos/`) | 57 tests, 0 failures |
| `claude plugin validate --strict`: the repository, `observatory/engine/skill` and `observatory-log` | passed ×3 |
| `tools/check_public_release.py --history --history-ref HEAD` | passed, 0 findings |

## The wheel

The wheel was built with `python -m build --wheel` from a fresh clone of the tag (HEAD
`c7a8912`).

- `tools/check_package.py`: 492 runtime files, 499 archive files, `failures: []`.
- `project_observatory-0.13.0-py3-none-any.whl` SHA-256:
  `2dd6c494728c7cf8f373ceaeda0a76e986af22ea17a3df94ae0d398868074415`.
- `gh release create v0.13.0 --verify-tag` published the wheel and `SHA256SUMS`.
- A re-download of both assets was byte-equal to the built wheel (`cmp`), and
  `shasum -a 256 -c SHA256SUMS` returned OK. GitHub's asset digest is the same SHA-256.

## Installation on the maintainer's machine

- `full update --check` reported `update-available`. `full update --version 0.13.0 --apply`
  returned `status: updated`, `rolled_back: false`, `degraded: []`.
- `project-observatory --version` prints 0.13.0. `full doctor` exits 0. The always-on
  server's `/health` says 0.13.0.
- The app was built from the tag with `macos/scripts/build-app.sh`. The clone was made full
  first: `CFBundleVersion` counts commits, and a shallow clone would build bundle 1, which
  Launch Services ranks below the installed one.
  - Result: 0.13.0 (244), 0 warnings, `codesign --verify --deep --strict` OK.
  - `install-app.sh --open` left one registration of the bundle id.
  - The app opened on the live dashboard, in Russian, in the dark gold theme. A window
    screenshot taken by window id is kept in the maintainer's private record.
- The plugin reaches Claude Code through the PassionCode launcher, which pins each member to
  a tag. It had pinned `observatory-log` at `v0.10.0` (plugin 0.13.0). So the hook and skill
  changes of 0.11.0–0.13.0 had not reached an installed agent.
  - Launcher 0.1.22 pins `v0.13.0` (passioncode-ai/passioncode #29, tag `v0.1.22`; its
    release workflow published it to npm).
  - `npx @passioncode-ai/passioncode@0.1.22 update` installed `observatory-log` 0.14.0.

## Notarized app (added the same day)

The maintainer asked for the app signed and notarized with the maintainer's own Apple
developer account.

- **Build.** From a full clone of `v0.13.0` (`c7a8912`, bundle 244), `build-app.sh` ran with
  `OBSERVATORY_SIGN_IDENTITY` set to the account's Developer ID Application certificate.
  `codesign -dv` shows the Developer ID authority chain, `flags=0x10000(runtime)` and a secure
  timestamp. No Keychain dialog appeared.
- **Notarization.** `macos/scripts/notarize.sh` (this change) ran with the account's App Store
  Connect API key, decoded into a temporary 0600 file for the run.
  - Apple answered `Accepted`.
  - The ticket was stapled, and `stapler validate` passed.
  - `spctl --assess --type execute` printed `accepted`, `source=Notarized Developer ID`.
- **Release asset.**
  - `ProjectObservatory-0.13.0-macos.zip` (SHA-256 `d7eff63e…3f916a44`) is attached to the
    release, and its line is added to `SHA256SUMS`.
  - A re-download of every asset passed `shasum -a 256 -c SHA256SUMS`, and GitHub's digests are
    the same.
  - `full update --check --version 0.13.0` still answers `up-to-date` with `degraded: []`: the
    updater reads only the wheel's line.
- **Gatekeeper on a download.** The downloaded zip, given a browser's `com.apple.quarantine`
  flag and unzipped, passed `spctl` as `Notarized Developer ID`.
- **Installed copy.** The notarized build replaced the ad hoc one in `/Applications` and opened
  on the live dashboard.

## Website

- passioncode.ai names 0.13.0 (passioncode-ai/passioncode-ai.github.io #31, merged as `d59d521`):
  - the card;
  - the Observatory page: the two-step install with `requirements-full.lock`, and
    `observatory_overview` as the agent's first call;
  - the facts row, with the SHA-256 above;
  - SCN-006.
- The site was deployed from that commit with `npm run deploy`. Both `/` and `/observatory/`
  served bytes equal to `dist/`, and `www` answers 301.
- The `site/` folder in this repository was not deployed. Its former host redirects (301) to
  passioncode.ai/observatory/, so the PassionCode 1.1.0 token change there has no live surface.

## Not run

- Live provider calls and real credential rotation (the gate's own `NOT_RUN` scopes).
- Regenerating the README screenshots from the demo estate (run 3's O-5).

## Next task

Regenerate the README screenshots from the demo estate through the reviewed-image flow (O-5).
Then pick up the open low items O-1 to O-6 and the partly-fixed KEY-R3-8 from
[run 3](../../reports/2026-10-03-observatory-audit-run-3/README.md).
