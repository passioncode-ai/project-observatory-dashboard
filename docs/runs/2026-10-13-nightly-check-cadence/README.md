# OBS-91: nightly full-check cadence

## State now

Base `1b91f28` starts the complete workflow on both push and pull request. The
correction configures23:00 Poland plus manual dispatch, preserves every
job, matrix and test step, and adds60/30-minute job limits and workflow/ref
concurrency. Existing artifact retention is already seven days. The plugin
validation shell assignment is separated from export so command failure is not
masked; the command itself is unchanged.

Actionlint1.7.12 passes. The structural comparison in `validation.json` preserves
all original steps and job settings except the declared timeout and shell-error
correction. Release workflow and branch protection are unchanged. Existing Windows
informative outcomes remain informative; this change does not accept them as tests.

Same-ref duplicate cancellation and case-insensitive group names are explicit
in the cadence document. The IANA alias correction requires independent review
of this exact candidate; hosted acceptance remains pending. No full
suite was dispatched during source editing. No nightly run can use this source
until it reaches the default branch. No release, install or resource saving is
claimed from the configuration change.

See [cadence](../../CI-CADENCE.md) for the exact branch acceptance command and
[GitHub schedule semantics](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
for default-branch, timezone and delayed-run behavior.

Next task: independent review, final committed privacy scan, publish the branch,
then run one explicit accumulated acceptance batch and integrate through required
checks. Observe the first scheduled run separately; missing/cancelled is not PASS.
