# 2026-10-01: not applicable is not degraded, and slow is not missing

Objective: make a live installation's `degraded` list name only real gaps. On one operator's
machine the service stayed `degraded` on eight reasons; each was classified with evidence as a
transient, an engine defect or a step for a person, and the engine defects were fixed here.

## What the eight reasons were

| Reason | Class | Evidence | Outcome |
|---|---|---|---|
| a CLI token call "did not answer within 30s" | transient, plus a defect: one attempt | the same call answered in 8-13 s three times at load average ~220; the collector re-run at once reported 0 degradations | retried with a longer limit (`slow_command.py`) |
| `git status --porcelain` "did not finish in 25s" in one checkout | transient, plus the same defect | 0.3 s when timed by hand; the filesystem collector re-run reported none | same retry in `scan_filesystem.sh` |
| seven RDAP 404s | engine defect | all four TLDs are absent from the IANA RDAP bootstrap (publication 2026-09-30); every domain is delegated in DNS | `not_applicable` when absent from the bootstrap AND held in DNS; a 404 under a TLD that runs RDAP now says "may be unregistered" |
| "no default branch could be resolved" | engine defect | the remote has one branch, `develop`, and `git ls-remote --symref origin HEAD` names it; the checkout was cloned without `origin/HEAD` | a remote with exactly one branch resolves to it; with several the reason names `git remote set-head origin --auto` |
| a companion tool's database "does not exist" (sessions and an OpenRouter consumer) | engine defect | the tool's whole home directory is absent: not installed, not broken | home absent is `not_applicable`; home present without its database or key stays degraded |
| three OpenRouter consumers "not in this account's listing" | engine defect | the listing stops at 1,200 newest keys by design; a full walk found two of the keys near position 7,400, and 8,000 listed keys held no provisioning key at all | missing keys are searched once past the bound and remembered by hash (`GET /keys/{hash}`); the provisioning key is confirmed with `GET /key`; "gone" only on a 404 for the hash or a listing walked to its end |
| three Google refusals "API has not been used in project" | engine defect | each service account reads the surface it is set up for (two read 72 properties, one reads Search Console) and is refused on the other | `not_applicable` when another credential reads that surface; the `analytics.api_disabled` info finding stays |
| a Bitbucket credential missing | engine defect | the receipt was eight days old and the integration is OFF in the workspace, so the collector never ran again | a receipt of an integration that is off is a leftover, named on `not_applicable` |

## What shipped

`not_applicable` beside `degraded` on a receipt, read by `degradations.every_not_applicable()`,
shown on the board as one `collector.not_applicable` info row per receipt, and never counted by
the service snapshot. Files: `degradations.py`, `slow_command.py`, `collectors/scan_domains.py`,
`scan_git_hygiene.py`, `scan_filesystem.py`, `scan_heroku.py`, `scan_openrouter.py`,
`scan_sessions.py`, `scan_google.py`, `google_registry.py`, `tools/google_findings.py`,
`tools/build_findings.py`, `survey.py`; design note in
[FABRIC-SERVICE.md](../../design/FABRIC-SERVICE.md).

## Checks actually run

- New suite `test_honest_absence.py` (BOUNDARY): run first against the unchanged engine, 18
  failures, among them the behaviours themselves (an off integration's receipt degraded, a
  one-branch remote unresolved, an absent companion degraded, covered Google refusals
  degraded); after the change 49 assertions, 0 failures.
- Root tests: 83, OK. `tools/update_inventory.py --check`: passed. `compileall` on Python 3.11:
  OK. `tools/check_public_release.py --history --history-ref HEAD`: 0 findings.
- `project-observatory full check --jobs 3`: see the pull request for the counts.
- A dry run of the fixed collectors against a live workspace, writing only to a scratch copy
  of its receipts: every collector reported 0 degradations, and the service snapshot computed
  from that copy held no collector row. A second OpenRouter run answered from the remembered
  hashes in 17 s instead of 95 s.

## What remains for a person

- Release: the next version is the first under the new licence, so cutting it is the
  maintainer's decision ([AGENTS.md → Releasing](../../../AGENTS.md#releasing)); every
  machine then runs `project-observatory full update`.
- Whether an integration that is off should be on, and whether a credential should also read
  a surface it is not set up for, are the operator's choices; the `not_applicable` rows and the
  `analytics.api_disabled` info rows name the step for each.

## Next task

Release the engine (version bump, tag, GitHub release with wheel and `SHA256SUMS`), then
`project-observatory full update` on each machine and confirm its service reports only the
reasons that remain.
