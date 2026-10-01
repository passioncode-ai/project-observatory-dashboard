# 2026-10-01: every open pull request reviewed and landed

Objective: review, finish and land (or close with a reason) every open pull request in this
repository. Four were open: #83, #89, #93 and #62; #100
(agent-sync) was another run's and landed on its own.

## What shipped

Commits are on `main`; each pull request's merge records its exact base.

| PR | What was wrong | What changed | On `main` |
|---|---|---|---|
| #83 Dependabot google-auth 2.58.1 | every row failed at install: Dependabot raised the pin in `pyproject.toml` and does not read `requirements-full.lock`, so the constraint contradicted it | the lock names 2.58.1; a clean install with the lock froze to exactly the lock (`diff` exit 0) | [`7f6fd62`](https://github.com/passioncode-ai/project-observatory-dashboard/commit/7f6fd62) |
| #98 (new) | `main` red from 2026-10-01 00:00 UTC: `reset_date` counted from the wall clock, so a key measured as spent in September "lifted" on 2026-11-01 | `key_reset_date` counts from the document's `checked_at`; test watched failing | [`e863e52`](https://github.com/passioncode-ai/project-observatory-dashboard/commit/e863e52) |
| #89 email-send, email-routing, workers-edit | `email-routing` was named after the zone alone, so a second project in one zone would roll the first project's token; no test attempted what the presets forbid | the routing writer is named per zone and slot; refusal tests (wrong level, wrong path, refused slot, failed probe) | [`bf34dff`](https://github.com/passioncode-ai/project-observatory-dashboard/commit/bf34dff), [`bfeef3f`](https://github.com/passioncode-ai/project-observatory-dashboard/commit/bfeef3f) |
| #93 fabric-inbox-server, fabric-inbox-account, workers-observability-read | no refusal tests; the issue summary said "on account X only" while the token also holds DNS Write on every zone of the account; no documentation | rebased onto #89's `probe` shape; refusal tests; the summary names the zone half; `handling-secrets` documents all three; group lists checked equal to fabric-inbox's `TOKEN_PERMISSIONS` / `ACCOUNT_TOKEN_PERMISSIONS` | [`fbb9566`](https://github.com/passioncode-ai/project-observatory-dashboard/commit/fbb9566) … [`9d5f993`](https://github.com/passioncode-ai/project-observatory-dashboard/commit/9d5f993) |
| #62 suppression identity (PB-032) | still needed (`scan_leaks.py` unchanged on `main` since 0.3.11), but carried a `CHANGELOG.md` entry outside a release and a run record outside the runs shape | rebased; changelog entry dropped; record moved and corrected ([its record](../2026-09-25-suppression-boundaries/README.md)) | [`5b9992e`](https://github.com/passioncode-ai/project-observatory-dashboard/commit/5b9992e), [`b9d076c`](https://github.com/passioncode-ai/project-observatory-dashboard/commit/b9d076c) |

## Checks actually run

- Required CI (three rows) green on every pull request's final head before its merge, and on
  `main` at `b9d076c` (push run, success).
- Locally, per branch: `project-observatory full check --suite credential_doors` exit 0 (#89,
  #93), `--suite leak_coverage --suite leak_scan_incremental` exit 0 (#62),
  `--suite temporary_block` exit 1 before and 0 after (#98), root tests exit 0,
  `tools/update_inventory.py --check` exit 0, `tools/check_public_release.py --history` exit 0.
- A full local `project-observatory full check` reported three suites as TIMEOUT on a host
  at load average about 180; each passed when run alone (exit 0). CI is the verdict for those.

## What went wrong on the way

- A force-push of #93's branch, rebasing it, dropped a commit another session had pushed a
  few minutes earlier (`workers-observability-read`): the lease was taken from a fetch that
  already contained it. It was restored with its author and ported to the `probe` shape, and
  the pull request says so. Checking `git log <lease>..<remote>` before a force-push would
  have caught it; a lease value is not a review of what it overwrites.
- One macOS row of #93 failed once in `test_conformance_receipt` ("OwnerRefused"); the other
  macOS run on the same commit passed, and the re-run passed. Unexplained; watch for a repeat.

## Open, filed

- [#95](https://github.com/passioncode-ai/project-observatory-dashboard/issues/95): a freshly
  minted Cloudflare token whose probe or vault delivery fails stays live (r2-bucket already
  cleans up).
- [#96](https://github.com/passioncode-ai/project-observatory-dashboard/issues/96): Dependabot pip
  bumps leave `requirements-full.lock` behind.
- [#97](https://github.com/passioncode-ai/project-observatory-dashboard/issues/97):
  `update_inventory.py` writes duplicate entries while paths are unmerged, and `--check`
  accepts them.

## Next task

The next release describes #62 (from `docs/COMPATIBILITY.md` "Unreleased: suppression
identity" and OSS-13), the new Cloudflare presets and the reset-date fix in `CHANGELOG.md`.
After that, #95 is the next contributor task.
