# Preserve evidence when a verification suite cannot complete

## State now

Implemented, pending independent review and merge. The portable runner used to discard
all suite results when one worker raised, for example while creating a sandbox on a full
disk. It also replaced the completed result with an unscoped error when report export failed.
The result now retains each selected suite, marks an exceptional suite ERROR with unknown
completion and exception type only, fails overall, and preserves stdout if export fails.

Checks: `tests/test_runner_error_receipts.py` reproduced both failures on the prior code
(KeyError for missing suite_count/suites), then passed both regressions. The synthetic
cli_compatibility portable suite passed 19 tests with jobs=1. Live providers and deployment
were not exercised. Source inventory regenerated; independent acceptance remains separate.

Next task: independent review of the diff and failure receipts, then required repository
checks and PR integration. No installed engine was changed.

## Independent review correction

The first review planted a permission failure after summary writing: stdout failed
but the saved summary still claimed PASS. A third regression reproduced it. The
summary is now written and restricted under a temporary name and published by rename
only after those operations succeed. All three regressions pass. Partial exports do
not publish a final success summary. This review changed the implementation; it was
not counted as acceptance of the earlier commit.
