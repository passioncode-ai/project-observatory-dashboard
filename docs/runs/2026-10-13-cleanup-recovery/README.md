# Cleanup preservation and survey coverage recovery

## State now

The source wave preserves unique local work during cleanup, retains partial test
receipts, and separates cheap worktree registration from expensive hygiene scans.
Independent adversarial review accepted the bounded source at
`799a429891e8d11bd1bb06c596bda9adcbe0a25c` after five corrective review rounds.
This is source acceptance, not a release, installation or measured space saving.

Execution is tracked by OBS-84 through OBS-87 in [the board](../../backlog.md).
All 79 existing board IDs remain; OBS-80 through OBS-83 retain their previously
planned meanings. OBS-87 is partly implemented and remains open.

## Behavior and evidence

| Contract | Implementation and reproducible evidence |
|---|---|
| Keep unique ignored files, nested repositories and local state | Shared `cleanup_safety.py`; `test_disk_guard.GitTierSafety` and `SnapshotSafety` |
| Refuse changed paths and uncertain holders | Every path plan carries device/inode identity; apply repeats eligibility and bounded holder checks; `test_disk_guard.TargetRevalidation` and `FreshTargetProbe` |
| Verify manual archives before removal | Working and staged patches, streamed untracked manifests, archive readback, final content/HEAD/index checks; `test_git_hardening.CleanupNeverLosesWork` |
| Preserve hidden index changes and Git references | Hidden index flags refuse removal; detached retention refs are verified; branch deletion binds proof to the tip, uses expected-tip comparison and never dereferences a symbolic branch; the same hardening suite reproduces each failure |
| Retain verification evidence on exceptions | Per-suite ERROR rows retain peer results; report publication failures retain stdout and cannot publish a final PASS file; `tests/test_runner_error_receipts.py` |
| Preserve unknown measurements | Missing swap remains null, file-scan errors remain incomplete, and consumers expose those limits; `test_disk_guard` telemetry cases |
| Discover registered worktrees before expensive cleanup | Independent bounded registration, dated stale carry-forward, explicit source errors, a cursor persisted before attempts, per-root planning time and per-phase holders; registration/fairness tests in `test_machine.py` and `test_disk_guard.py` |
| Reject malformed evidence | Invalid nested registration rows produce degradation; valid peers survive. Missing full-survey sources do not become successful empty surveys |

[Verification summary](verification.json) records the tested source, interpreter,
suite counts and limits. The five selected portable suites passed: 257 unittest
cases (two platform skips) and 128 legacy MCP assertions. The root suite passed
172 cases without expected failures. Changed Python files also parse with the
Python 3.11 grammar; this does not substitute for a Python 3.11 runtime run.

The independent reader reproduced the destructive counterexamples, checked the
corrected candidates and compared the reviewed source blobs with the integrated
head. It found no remaining blocking defect in this bounded wave. Whole-engine,
native, live-provider and installed acceptance are separate checks.

## Hosted-check correction

Full hosted compatibility checks at `328b5d1` found two additional failures outside
those selected suites: direct OS file opening in the archive reader, and discovery
of the measured-empty translation ID passed through a variable. `e80c73d` routes
the reader through `osprivacy.open` (including Windows binary mode) and registers
the existing dynamic message ID. Independent review accepted this bounded correction;
osprivacy (17), i18n (44), and Git hardening (56) passed independently. The root
suite still passes 172 cases. Native Windows was not run locally. Required hosted
checks on the corrected candidate remain the integration gate.

The next hosted 238-suite run exposed a path-hygiene classification error: the
branch/worktree recovery archives intentionally survive process exit, unlike test
temporary directories. `196f0be` adds two documented local exceptions; the runtime
AST is unchanged. API-listing/path checks (62 assertions) and Git hardening (56
cases) pass, and independent review accepted the archive-lifetime rationale.

## Remaining work and next task

Next: integrate the reviewed candidate under the repository's required checks,
then run release and installed-runtime acceptance before enabling any new behavior.
No operator's files were used as destructive test fixtures.

OBS-87 still needs discovery beyond registered anchors, resumption within a very
large root, complete cache ownership/regenerability evidence and lightweight memory
freshness. Allocated bytes do not prove reclaimable bytes. No process is stopped
and no swap file is deleted to implement these measurements.

Filesystem and holder observations remain point-in-time checks. An arbitrary
external writer or worktree attachment can race the final check. OS filesystem
calls are cooperative and may exceed soft deadlines. Branch configuration is
retained when a branch ref is deleted by compare-and-swap; metadata cleanup must
not weaken reference preservation.

Current-tree and newly introduced history are checked with the maintainer's
identifier gate. An inherited full-history finding is not converted into a passing
full-history result, and this change does not rewrite history.

## Component records

- [Board and reserved IDs](../2026-10-13-board-recovery/README.md)
- [Verification receipts](../2026-10-13-runner-receipts/README.md)
- [Local-state preservation](../2026-10-13-cleanup-local-state/README.md)
- [Guard revalidation](../2026-10-13-guard-revalidation/README.md)
- [Manual archives and reference corrections](../2026-10-13-manual-archive-revalidation/README.md)
- [Telemetry and identity](../2026-10-13-cleanup-coverage-truth/README.md)
- [Cheap registration](../2026-10-13-worktree-registration/README.md)
- [Caller integration and fairness](../2026-10-13-registration-fairness/README.md)
