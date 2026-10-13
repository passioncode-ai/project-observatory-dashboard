# Portable suites need execution evidence

Status: local scoped checks pass; independent source review pending.

## Objective, scope and dependencies

A unittest file defining cases but omitting `unittest.main()` exited zero and was
accepted with zero tests. `nothing_ran` only noticed explicit skip markers. Fix
that reporting seam in `observatory/engine/tests/run_portable.py`, preserve the
legacy script protocols and existing setup/log-failure receipts, and leave
interpreter exits separate from test evidence. Parent source `795b01c` remains
measured-red; this fix does not accept its provider implementation.

## Contract

Bootstrap groups `LEGACY` and `BOUNDARY` do not identify the output protocol. New
suites default to `unittest`, requiring a final complete summary and at least one
executed case. Existing `ASSERTION_LINE_SUITES` explicitly use PASS/FAIL/SKIP
lines, including direct assertions and the ENV JavaScript helper. A silent assert
has no compatibility exemption. `SUITE_MINIMUMS` can raise the positive minimum.
[Protocol inventory](protocols.json) accounts for all 257 registered source suites;
it is static classification, not a claim to have rerun all 257.

Receipts add `test_protocol`, `minimum_executed`, `executed_checks` and
`skipped_checks`. Exit zero with no or insufficient execution evidence is ERROR,
with a fixed `reason_code`, and is included in `not_run`. An explicit all-skipped
suite remains SKIP. Reported failures remain FAIL even when the script swallows
its exit. Nonzero exits and timeouts keep their actual code and status. A PASS
line cannot substitute for missing unittest cases, and an earlier complete
summary cannot lend its cases to a final zero-case or incomplete summary. A
failed complete summary stays FAIL even after a later successful or all-skipped
run; final-block counts never erase earlier failure evidence. Malformed
or impossible skip counters are unknown evidence, never zero skips.

This is a protocol for trusted repository tests. A script can forge a complete
well-formed summary or PASS lines. These counts do not authenticate execution,
prove every intended assertion ran, or establish exhaustive coverage. Higher
minimums are explicit source metadata, not inferred from yesterday's count.

## Evidence and resume

[Checks](checks.json) bind the runner source: 24 root runner/fault cases pass,
including a forgotten main, misleading PASS, all-skipped unittest, stderr
failures, swallowed exit, malformed counters and the protocol's forgery limit.
Six real compatible suites pass: unittest health plus five line reporters,
including the JavaScript ENV controls. Initial false-positive evidence is
retained; no earlier run has been relabelled. Documentation copies and inventory
are regenerated and checked.

Next: independent review at the committed source, privacy current/history gate,
then a draft branch handoff. Do not merge or release the measured-red parent as
a consequence of this scoped fix.
