# Full checks run as a nightly batch

The complete `observatory-release-check` workflow runs at 23:00 Poland on
the default branch, or through `workflow_dispatch` for a reviewed accumulated
branch candidate. Push and pull-request updates do not start this full suite.
Keep focused local checks during development. GitHub may delay or drop a schedule;
a missing run is not a passing run, and a branch outside main is not in its batch.

For protected branch acceptance, a maintainer dispatches the accumulated candidate
once with `gh workflow run check.yml --ref <branch>`, then verifies the resulting
run's exact commit and every required matrix check. A changed commit needs its own
acceptance. Required check names and branch protection are unchanged: no check
means the pull request remains pending. Do not use admin bypass to bridge this wait.

The schedule uses the IANA `Poland` alias and GitHub's timezone support, so 23:00 follows local daylight
saving time without a second cron or an in-job clock filter. See the
[GitHub schedule contract](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
The workflow has one concurrency group per workflow/ref. A new same-ref run can
cancel a running check even on the same commit, including manual versus nightly
checks of main. This favors the newest requested batch over completing the earlier
run; it does not promise commit ordering. GitHub compares concurrency names without
case, so refs differing only by letter case also share a group. Otherwise different
branches and tags use different groups. A cancelled run is not acceptance. Matrix jobs have a60-minute
limit and the Windows job has a30-minute limit; unchanged per-step test behavior
remains visible in the run's outcomes.

Release signing retains its separate `release.yml` guard: main ancestry and a
successful full check on the exact release commit are required. The guard may
dispatch this workflow on its tag. This cadence change does not modify releases,
required checks, test commands, matrices or the existing informative Windows steps.

Next verification: independently review the source diff, run one explicit branch
acceptance batch, integrate through protection, then observe the first scheduled
run on main. Until that observed run, the nightly behavior is configured source,
not a completed operational check.

The alias is defined by the [IANA backward links](https://data.iana.org/time-zones/tzdb/backward).
Local zoneinfo comparison across2020–2035 found every hourly offset equal to its
linked canonical zone. GitHub scheduler execution of this alias remains to be observed.
