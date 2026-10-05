# 2026-10-05 — release 0.16.0: agent memory with access, consent, scope, receipts and forgetting

`.github/workflows/release.yml` built, signed, notarized, attested and published this release.
What changed for a user is in `CHANGELOG.md` → 0.16.0. This record uses synthetic names only.

## What it carries

| Pull request | Merge | What |
|---|---|---|
| #147–#149, #151 | — | embedding-policy/1: consent per project before any remote embedding (PB-137 N-002, N-003) |
| #150, #154, #158 | `54e5bc2`, `e9d0531` | access-bindings/1 at every entry point, with the fixes from the first independent review (N-007, N-008) |
| #152 | — | vector namespaces, one per pinned model identity (N-005) |
| #155 | `8be9af0` | search filters scope and validity before its candidate window (N-009) |
| #156 | `616f01c` | retrieval receipts, scoped explain, redaction on the way out (N-012) |
| #157 | `44358e1` | checkpoint chunk provenance; replays resurrect nothing (N-011) |
| #153, #159 | `5c31318` | `full memory-http` on loopback, memory/0.1 under its own names (N-015, N-016, N-025) |
| #161 | `59bf82a` | `full forget`: withdrawal, erasure, a receipt per backend (N-013) |
| #162 | `b6c64a4` | facts that expire, lessons that cite evidence, no self-promotion (N-014) |
| #163 | `aa6788d` | the release: version 0.16.0 in every declaration, `observatory-log` 0.16.0, the changelog, and the pre-release fixes below |

## Before the tag

- **A second independent review** read the memory work as a whole. Each of its findings was fixed in #163, and each fix's test was watched failing on the code before it:
  - `full forget` left text in a handoff goal, body chunks, provenance, conflict links and old full-text segments.
  - The daily estate-history run rewrote facts the operator had confirmed. `ledger.renew` now extends only `valid_to`.
  - Validity times in forms that compare wrongly as text were accepted.
  - A flood without a bearer grew the access journal by one line per request.
  - `memory.*` refusals were not `isError`, and evidence strings were stored in a shape nothing reads.
- **The backlog was walked for anything critical.** Three rows were fixed in #163:
  - OBS-24, a `KeyError` on a registry row without `ownership`;
  - OBS-31, the anonymous GitHub rate limit in `full update`;
  - issue #95 (OBS-16), a Cloudflare token left live after a failed issue. It was the only security-relevant open issue.

  Issue #97 (OBS-18, duplicate inventory rows) was fixed with them. OBS-17 (#96, the Dependabot lock name) stays open: renaming the lock changes what the wheel ships, so it is its own change. Issue #146 (COM-10/12) waits on the Fabric COM-02/03 contracts.
- **The privacy gate's denylist was stale.** It was built on 2026-09-24, before the organization transfer, and with it `main` itself reported 46 rows of findings. It was rebuilt from the registry with the baseline `v0.15.0` (`tools/build_private_denylist.py … --baseline-ref v0.15.0`) and stored owner-only outside every repository. Against it, `main` had two findings and #163 had the same two:
  - `GITHUB_TOKEN`, the standard variable name `full update` now reads;
  - the public repository that row OBS-32 names, added by the operator in #143.

  Both now have entries with reasons in `tools/public-identifiers.json`, and the gate passes with 0 findings.
- **Local gate on the release head:**
  - `compileall`: 0
  - root `unittest`: 126 OK
  - `update_inventory.py --check`: passed
  - wheel + `check_package.py`: passed
  - `run_portable.py --jobs 1`: 223/223 PASS
- **Hosted checks.** The first CI run failed on one new test: the conflict fixture ran `git merge` with no identity. On a runner with none, git refuses before the conflict exists. This was reproduced in a Linux container (git 2.47.3) and fixed by giving the merge an identity and asserting the conflict. The three required checks then passed on the release head `ecb79f6`.

## Release

- The annotated tag `v0.16.0` (`bcf69a2`) points at `aa6788d`.
- Release run [37298501665](https://github.com/passioncode-ai/project-observatory-dashboard/actions/runs/37298501665): `wheel`, `macos` and `publish` all succeeded.
- **The `release` environment was approved twice by an agent** (Claude Code), once for `macos` and once for `publish`, through the maintainer's account. That was on the operator's explicit instruction of 2026-10-05 to build and release autonomously and to approve everything along the way. The approval comments say so. The organization's written rule is that a person approves a release run. This is a recorded exception, as at 0.15.0, and keeping or amending the rule stays the operator's decision.
- Published 2026-10-05T10:56:54Z, not a draft and not a prerelease, with four assets.

## Verification of the downloaded assets

| Check | Result |
|---|---|
| `shasum -a 256 -c SHA256SUMS` | both OK — wheel `07a79a9c9341f8bb09e936492a26ea858c00a25a9640e9ac4f5b9ba9ab9b1c04`, app zip `8709a777fa3b809ea9431a35fe8aa74d25e72ef5ff5eed7a01df63b93f48e221`; equal to the GitHub asset digests |
| `gpg --verify SHA256SUMS.asc SHA256SUMS` (organization key in a throwaway keyring, from `passioncode-ai/.github/release-signing`) | Good signature, key `63B3 0DC3 24BD 6974 87AA 3194 4FAF B8AE C803 B6A7` |
| `gh attestation verify <file> -R passioncode-ai/project-observatory-dashboard --signer-repo passioncode-ai/.github` | exit 0 for the wheel and the app zip |
| `tools/check_package.py` on the wheel, from a checkout of the tag | `passed: true`, 548 runtime files, `failures: []` |
| the app in the zip: `spctl -a -vv`, `stapler validate` | `source=Notarized Developer ID`; the stapled ticket validates; `CFBundleShortVersionString` 0.16.0 |

## Installation on the maintainer's machine

- `project-observatory full update --version 0.16.0 --apply` reported `status: updated`, `rolled_back: false` and `degraded: []`. An encrypted snapshot was taken first. The census found 21 session MCP servers on the previous release. Each answers `stale-server` until its session reconnects.
- `project-observatory --version` prints 0.16.0, `full doctor` exits 0, and `/health` answers version 0.16.0 with `silent_after_s` 480.
- Migration `0010-vector-namespaces` ran on the live store. The pre-namespace OpenAI index is the `legacy` namespace, and no namespace is active.
- Both installers were re-run from the installed engine (`install_launchd.py install`, `serverd.py --install`). The tick is installed with `RunAtLoad` false; the server runs with `KeepAlive`.
- The Mac app: 0.15.0 is kept at `$HOME/DATA/_archive/observatory-app-rollback-0.15.0-2026-10-05/`. 0.16.0 was installed from the verified zip with `macos/scripts/install-app.sh … --open` and `spctl` accepts it. The 0.14.0 rollback copy was removed.
- The companion plugin: PassionCode launcher [#38](https://github.com/passioncode-ai/passioncode/pull/38) (`f5bdda6`) pins Observatory Log `v0.16.0` and is released as 0.1.28 (tag `v0.1.28`, npm `@passioncode-ai/passioncode@0.1.28`). That closes the launcher's PC-11. `npx @passioncode-ai/passioncode@latest update` installed `observatory-log@passioncode` 0.16.0 here. The launcher's new `prune` step removed 23 older launcher releases.

## Website

`passioncode-ai.github.io` [#42](https://github.com/passioncode-ai/passioncode-ai.github.io/pull/42) (`1d5dedb`, SITE-011) was deployed as Worker version `06d6bf32-…`. The Observatory page reads "Latest release: 0.16.0" (curl). The home page served the new bytes on a cache-bypassing request, while one plain request was still answered from Cloudflare's edge cache (`cf-cache-status: HIT`, `max-age=0, must-revalidate`).

## Not done here

- **OBS-35, the operator's decision:** admit `multilingual-e5-small` for ranking only, or keep search lexical. Until then the vector arm stays off.
- **PB-137 nodes outside this repository or deferred:**
  - N-006 and N-010 are deferred;
  - N-018, N-020 and N-023 have been handed to their owners;
  - N-021 and N-022 are not started.

  The program's graph in org-index is the source.
- **Open engine rows:** OBS-15, 17, 19–22, 25–28 and 32.

## Next task

OBS-17: Dependabot pip bumps keep `requirements-full.lock` in step (issue #96). Today every such pull request fails at install.
