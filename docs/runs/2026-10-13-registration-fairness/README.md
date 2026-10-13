# Registration caller and build-root fairness: bounded OBS-87 slice

The guard refreshed Git hygiene only after planning, repeatedly started with the
same roots and overwrote failed holder evidence with the command phase's `none`.
This change reserves an early registration-only refresh, consumes successful
dated roots, advances a persisted cursor before each expensive attempt, bounds
each root, renews child deadlines and preserves phase-specific holder evidence.

`test_disk_guard.py RegistrationFairness` first reproduced six defects (four
failures, two errors), then passed all seven cases including bounded directory
enumeration. Selected `RegistrationFairness Builds TargetRevalidation Run
RunBudget` passed 36 tests before the enumeration extension; the subsequent
`RegistrationFairness Builds TargetRevalidation` passed 29 tests. Source inventory,
engine-doc sync, whitespace and public-tree pattern checks passed (888 files,
zero findings; no private denylist/history scan).

Collector counterpart: commit `778c8f056a4e5e34144b67a53048085006d125f4`,
`collectors/scan_git_hygiene.py`, with its registration-only contract in
[the collector handoff](../2026-10-13-worktree-registration/README.md) integrated locally as `ef67b84`. `RegistrationIntegration` runs the actual
registration subprocess against a throwaway repository and linked worktree. It
proves the expired inherited deadline is renewed and old hygiene bytes and mtime
remain unchanged. Together with the seven fairness tests it passed eight cases.

The combined portable `--suite disk_guard --jobs 1 --timeout 180` passed 118 tests
with two platform skips in 18.02 seconds on Python 3.14.7; the exact synthetic
receipt is [verification.json](verification.json). Final inventory/doc-sync/diff
checks passed. Final public-tree pattern check passed 890 files, zero findings,
without a private denylist or history scan. Next: independently review and
integrate the combined cleanup candidate, including the separate archive repair.

No live cleanup or host survey was run. This is fairness across registered
roots; discovery outside registered repositories and resuming within oversized
roots remain outside this slice. Cooperative filesystem deadlines do not
preempt a blocked operating-system call. Membership never proves abandonment
or authorizes removal without the existing independent safeguards.

Skills: task-pipeline (test-first bounded change), evidence-docs (measured scope).


Independent IV11 correction: schema-valid registration documents could contain
null repository/worktree rows and crash the consumer without degradation.
`RegistrationMalformed` reproduced ten failed assertions and eight errors across
four tests before the fix; a missing-protection-flag case also failed before its
strict check. The consumer now excludes malformed rows/metadata, retains valid
peers and reports invalid counts. The dated fallback is covered as well. Selected
`RegistrationMalformed RegistrationIntegration RegistrationFairness Builds`
passed 17 tests, including an integrated guard run that reaches later phases.
No live survey or cleanup was run for this correction. Next: independently
verify the correction on the combined candidate commit.
