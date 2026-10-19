# OBS-80: preserve a survey during a scoped Heroku refresh

## State now

Source `da10f4c` passed independent counterexample review: 25 scoped tests and 61
existing assertions pass. Provider-only updates preserve local unknown state
through repeated refreshes. No live provider was called. Combined integration,
hosted checks, release and installed acceptance remain separate.

## Scope and source

[OBS-80](../../backlog.md) owns `scan_heroku --only`. PR [#209](https://github.com/passioncode-ai/project-observatory-dashboard/pull/209)
is a documentation-only report at `9ea04f453a2bc74ded3126759a0e64d0b8192e3c`; its original OBS-76
label conflicts with the organization-edition row. This implementation uses the reserved
OBS-80 identity and starts from `5c14e718295cf7349aaa7502e9dc94d521f7e203`.
The existing PR branch and shared backlog are unchanged.

The original collector filtered the app list and wrote it over the entire survey.
The synthetic two-app regression demonstrates that this removed the unrequested app.
No live Heroku call or credential lookup was used to reproduce it.

## Contract

`collectors/scan_heroku.py OUTPUT --only APP [APP ...]` now patches positively refreshed
apps into the previous raw survey. Unrequested rows retain their content and order,
including optional fields and their degradation entries. Successful target rows retain
unknown optional fields. New target apps append; missing targets are not deletions.

`scoped_refresh` records `attempted_at`, `requested`, `refreshed`, `failed` and `status`
(`complete_for_scope` or `partial`). The global `scanned_at`, account/team metadata and
orphan-remote inventory retain their old values; the attempt does not claim estate-wide
freshness. Each successfully measured target gets `provider_scanned_at` for remote fields only.
Its prior `local_folders` stay unchanged, `scoped_refresh.local_checkouts` says
`not_refreshed`, and new apps name their unknown local inventory in `degraded`.
A scoped refresh never scans local checkouts; only a full scan owns that inventory. A first scoped
survey has a null global timestamp and an explicit incomplete-survey degradation.

Authentication/list failures, unavailable app fields, missing target apps and scan
exceptions preserve previous measurements, name the gap in `degraded`, and return nonzero.
A changed app identity requires a complete survey. Unreadable or malformed previous JSON,
unsupported schema versions, duplicate names, an empty `--only` and a held writer lock
are refused. Failed atomic output writes retain the previous destination.

Both full and scoped collector invocations hold the same nonblocking output-sibling
`.lock` file from before provider access until publication. The file remains reusable;
its existence is not evidence that a process holds it. This is advisory locking:
external writers must adopt it, and arbitrary file replacement is not fenced.
The full-survey replacement and full-survey failure semantics are otherwise unchanged.
This change does not repair a survey already truncated by an earlier collector run;
a complete scan is needed to recover absent historical rows.

## Requirements and checks

| Requirement | Synthetic check |
|---|---|
| Preserve other projects and global timestamps | `ScopedSurvey.test_subset_preserves_unrequested_rows_and_global_metadata` |
| Preserve missing or unreadable targets with honest failure | auth/list/missing-target/unreadable-target/exception tests |
| Preserve extensions; clear only refreshed target errors | unknown-field and target-degradation tests |
| Refuse unsafe baselines and account identity replacement | malformed-prior/invalid-inventory/identity-change tests |
| Serialize full and scoped writers before credentials | shared-writer-lock and full-scan-lock tests |
| Preserve previous bytes on publication failure | atomic-write failure test |
| Expose release/GitHub read failures to scoped merging | `test_scan_app_reports_release_and_github_unreadability` |

The regression suite first failed against the original implementation (exit 1;
11 tests, 14 failed assertions/subtests and 3 errors). After correction,
`tests/run_portable.py --suite heroku_scoped --suite heroku --jobs 1` passed:
25 scoped unittest cases, 61 existing Heroku assertions, zero skips.
[checks.json](checks.json) retains the machine-readable synthetic receipt.
The suite is registered in `BOUNDARY`; future portable runs include it.

## Handoff

Independent review must check the exact candidate and the preservation/error cases,
then route its PR as OBS-80 and reconcile the obsolete #209 documentation branch.
No board row, PR, deployment, live scan or provider mutation is declared completed here.
Live provider acceptance and Windows lock behavior were not tested in this run.

## Independent review correction

The reviewer reproduced a producer seam: `{}` instead of a provider collection
was accepted as empty, before scoped merging could see an error. The real-reader
regression failed on the first candidate (exit 1). Container and row shapes are
now validated; a real empty list remains a valid empty measurement. The reviewer
also found missing local inventory clearing old folders. Scoped refresh now
retains that independent measurement and marks it not refreshed, without calling
the local scanner. The corrected suite has 25 cases; the original 18-case receipt
is superseded by the current checks.json.

Malformed billable quantities are also unreadable: negative formation quantities and
negative, nonfinite or boolean addon cents preserve old measurements. The numeric
regression failed before correction; real measured zero quantity and zero price still pass.

A repeated provider-only refresh cannot clear unknown local coverage: a new app keeps
`local_folders_status: not_measured` and its degradation until a full survey replaces
the row. A two-refresh regression was observed failing before this correction.
