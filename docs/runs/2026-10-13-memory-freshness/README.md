# OBS-87: lightweight memory freshness

<sub>ssheleg skills — task-pipeline · evidence-docs · copywriting · working-with-fabric · skill-search</sub>

## Bounded packet

At the packet baseline, the disk guard refreshed `machine.json` after cleanup planning and the full
survey performs process attribution and disk sizing. A short budget can leave RAM/swap
old; a single report timestamp does not describe the age of every source.
Source: `observatory/engine/tools/disk_guard.py:run`,
`collectors/scan_machine.py:main`, and `machine_view.py:summary` in that engine.

Implement a separate `machine-memory.json` receipt before expensive guard work, with
bounded OS commands for RAM/swap and a process RSS snapshot using executable basenames,
never argv, working-directory walks or disk sizing. Preserve unknown numeric values as
null with reasons. Stamp memory and processes separately. Readers select each newest
attempt independently and disclose stale/unknown timestamps and failed sources. RSS
is a per-process sum and is not unique physical memory or reclaimable disk space.

Expose an explicit `full machine --memory-only` command. Keep full survey compatibility
and disk evidence separate; sampling failure does not freshen an old success. A failed
new attempt has unknown values and its own failure receipt. UI/CLI/MCP carry the same
freshness contract. No lifecycle or deletion commands are added.

## Executable acceptance scenarios

1. A memory-only command makes no disk, cwd, argv, simulator or launchd inventory call.
2. Missing commands, malformed output and deadline expiry produce null values and
   explicit error codes; a measured zero remains zero.
3. A fresh memory sample with an old disk/process receipt exposes each timestamp;
   unknown or future timestamps do not become fresh by loading them.
4. Guard refresh happens before expensive phases, with a child deadline and a small
   reserved budget. Auto-cleanup being disabled does not disable measurement.
5. CLI, MCP and Machine page preserve unknown swap and show source freshness.

## Scope and next step

Unregistered checkout discovery remains open: it needs explicit configured roots,
persisted bounded cursors, safe Git validation, identity binding and read-only candidate
receipts. Folder names or absence from registrations never establish abandonment.
This packet neither closes that gap nor grants any new deletion authority.

## Implemented and checked

`memory_snapshot.collect` gathers bounded OS counters and executable-only RSS;
`collectors/scan_memory.py` writes the independent receipt. `disk_guard.run` refreshes
it before planning, including when automatic cleanup is disabled. The CLI command,
Machine page, MCP response, assistant evidence and machine findings select memory and
processes independently through `memory_snapshot.read_sources`. Full disk and detailed
process attribution keep their original survey timestamps. A new failed attempt is
not hidden behind a previous successful measurement. Windows-native RAM collection
remains unsupported and explicit; RSS is never a reclamation promise.

Tests-first evidence: `test_reader_uses_light_receipt_without_freshening_disk` and
`test_findings_and_assistant_use_new_memory_without_redating_disk` both failed on the
previous readers, then passed. `test_collector_is_bounded_and_never_asks_for_args_or_disk`,
`test_failed_commands_are_null_and_errors_do_not_leak_bodies`, and
`test_invalid_process_identity_and_huge_numeric_are_not_consumed` cover privacy,
unknown values and malformed receipts. Guard tests cover early sampling, disabled
cleanup and failed refresh reporting. Collector subprocess coverage uses an expired
budget in an isolated synthetic workspace, so it cannot survey the host.

Validation command (synthetic offline, one worker):

```sh
python observatory/engine/tests/run_portable.py --suite memory_freshness --suite machine --suite disk_guard --suite assistant --suite i18n --jobs 1 --timeout 180 --report-dir REPORT_DIRECTORY
```

Next: independent exact-commit review; then explicit-root, read-only checkout
candidate inventory with persisted bounded traversal. Installed/scheduled acceptance
and native Windows/macOS/Linux cross-platform validation remain separate from these
synthetic checks. No live cleanup or process termination was run.


Result: PASS on Python 3.14.7, 281 unittest cases, including two explicitly skipped
platform-specific disk-guard cases. Breakdown: memory freshness 16, machine 59,
disk guard 123, assistant 39, i18n 44. Source inventory and working-tree pattern
privacy checks passed (exit 0); private denylist and history privacy were NOT RUN
in this bounded candidate check. Independent review is pending.

**Made with [ssheleg skills](https://github.com/ssheleg/sshlg-skills)**

- `task-pipeline`: bounded test-first implementation.
- `evidence-docs`: source and test-backed packet.
- `copywriting`: memory freshness wording.
- `working-with-fabric`: preserved service lifecycle boundary; outside the family.
- `skill-search`: route selection.

## Independent review correction

Review reproduced equal-second ordering, future attempt timestamps, a killed collector
leaving an older success visible, and a delayed older collector overwriting a newer
failure. `begin_attempt` now writes an unknown provisional receipt before launch;
`finish_attempt` compares the attempt ID under a bounded cross-process lock before
replacement. Collection holds no lock. A killed process leaves an explicit incomplete
attempt. Guard failure updates only its own attempt. Microsecond timestamps reduce
collisions; legacy equal-time light receipts win conservatively, and an invalid/future
attempt is never fresh even if its measurement timestamp appears current.

Regression evidence: `test_same_second_failed_light_attempt_wins_over_full_success`,
`test_future_attempt_cannot_be_fresh_even_with_current_measurement`,
`test_guard_timeout_publishes_failed_new_attempt_for_every_reader`, and
`test_older_finish_cannot_overwrite_newer_started_attempt` failed before correction.
`test_two_collectors_finishing_out_of_order_keep_newer_failure` exercises real file
replacement with concurrent collectors. Disk-full or lock-write failure still cannot
persist a new receipt and is reported by the caller; no persistence claim is made then.

Correction validation: PASS, memory freshness 21 and disk guard 123 cases (two
platform skips), jobs 1. Source inventory and pattern privacy passed again.
