# Cleanup admission avoids unusable process inventories

## State now

Preliminary admission cannot authorize path removal with the current incomplete
holder inventory. Avoid its process-table capture at all three cleanup callers
while preserving unknown refusal. Explicit positive-hint collection remains the
default internal API for other readers; final probes and cache checks are unchanged.

Five tests first produced three failures and one missing-API error; the unchanged
positive-hint case passed. All five pass after the change. Guard receipts explicitly
mark not-attempted and null measurement time. No live process inventory was used
by these synthetic tests. No host RAM saving or installed change is claimed.

Focused validation: `python observatory/engine/tests/test_disk_guard.py` ran153
cases,152 passed and1 native Windows case skipped on this host.
`python observatory/engine/tests/test_git_hardening.py` passed61 cases.
Root documentation-copy checks passed6 cases; source-inventory check and
`git diff --check` passed. These are local checks, not installed acceptance.

Next task: independent exact-source review and privacy, then draft delivery. Bound the remaining explicit capture
paths in a separate unit with combined output caps, a shared deadline and owned
process-group cleanup; this small admission change does not claim those bounds.
