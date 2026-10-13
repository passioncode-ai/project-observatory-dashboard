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
[the collector handoff](../2026-10-13-worktree-registration/README.md) once integrated.
Next: combine that collector and run the single synthetic portable disk-guard
suite, then independently review the combined cleanup candidate.

No live cleanup or host survey was run. This is fairness across registered
roots; discovery outside registered repositories and resuming within oversized
roots remain outside this slice. Cooperative filesystem deadlines do not
preempt a blocked operating-system call. Membership never proves abandonment
or authorizes removal without the existing independent safeguards.

Skills: task-pipeline (test-first bounded change), evidence-docs (measured scope).
