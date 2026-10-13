# Tick stage receipts

Status: scoped synthetic checks pass; independent review pending. No installed-runtime or latency claim.

## Objective and scope

A captured-output tail stage can consume the rest of the tick without printing its identity. `Tick.bounded` waits before returning output, and the supervisor only records the whole-run outcome. Add bounded, run-correlated stage observations to the existing `tick.json` and `tick-run.json` contracts. Preserve the last completed report while a run is in progress, distinguish a watchdog refusal to start from a timeout, and leave an interrupted stage unknown when no terminal receipt exists.

No collector scheduling, deadlines, provider activity or installed service changes. Recorded stage duration measures the watchdog call, including startup and shutdown; it is not pure collector CPU time. Missing stage rows do not prove the stage was not started.

## Acceptance

Synthetic tests must cover a silent tail, nonzero exits, not-started versus stopped, lost terminal writes, stale run records, process-start failures, prior completed report preservation and metadata-only supervisor copying. Runtime/model source from the parent remains measured-red; this bounded observability task does not settle it.

## Resume

Run `tick_progress` focused tests and the affected lifecycle/tick compatibility checks; regenerate documentation copies and inventory, then obtain independent source review. No installation or release follows from these checks.

## Implemented contract

`tick_progress.Progress` preserves the previous completed `tick.json` fields and
atomically adds current progress before invoking either streaming or captured
steps. A UUID passed by `tick_lease.supervised` correlates only this run.
`record_run` copies a bounded, validated metadata projection into the existing
supervisor receipt and 50-run history; arbitrary extra fields are discarded.
A stage receipt write failure logs a fixed diagnostic and does not change the
command exit. This instrumentation does not redact the pre-existing streaming
child log; the new metadata never contains output, argv or exception text.

The vocabulary is fixed and tested against current tick calls. Each snapshot
reads at most 256 KiB and retains at most 128 stage rows. `coverage: observed`
means these rows were observed; it is not proof that every planned stage ran.
`partial` includes lost terminal writes, unknown stage names or failed writes.
`unknown` is missing, stale or malformed evidence. A stage name of `unknown`
does not identify the interrupted command. Scheduling and budgets are unchanged.
The supervisor itself can still be killed or unable to write; absent evidence
remains unknown, not healthy or finished.

## Verification and remaining work

[Checks and exact source hashes](checks.json) record the passing compatibility
and contract suites. The final stage suite has 16 cases, including a real synthetic
supervisor timeout. Existing tick 38, lifecycle 60, health 9, privacy 17 tests,
failed-step 22 and pipeline 37 assertions pass. The initial portable run exposed
an omitted sandbox module dependency and is recorded rather than erased.
Documentation copies and source inventory are checked. Windows-native execution
and installed latency were not measured here. Obtain independent scoped review,
then publish a draft branch under the repository policy; do not treat the parent
measured-red provider acceptance as solved by this change.

## Review correction

The first source candidate `d370dab` was **CHANGES_REQUIRED**: a FIFO receipt
could block outside the watchdog, and an overflowing numeric duration could
suppress the whole-run receipt. A self-review also reproduced a broken diagnostic
sink replacing the original exit. Tests reproduce all three before correction.
Receipt reads now require a regular file through the shared OS privacy boundary,
with nonblocking/no-follow flags and a byte limit. Malformed numeric data becomes
unknown. Optional stage projection is isolated from the authoritative run receipt,
and fixed diagnostics are best effort. Synthetic FIFO and symlink cases preserve
the foreign input; Windows-native race guarantees are not established by POSIX
fixtures. The final 16-case stage suite passes; lifecycle 60 and privacy 17 cases
were rerun after the source correction. No installed service was touched.
