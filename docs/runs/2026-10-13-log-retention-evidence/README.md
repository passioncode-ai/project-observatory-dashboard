# Observable log-retention outcomes

## State now

Bounded source implementation based on
`12b7343277004aa408283fb4aef06ccbae935954`; independent review and integration
remain pending. No release version, installed configuration, service or live log
was changed. This is separate from the prepared release candidate.

## Contract

`log_policy.sweep_report` reports the same rotation execution, with complete,
partial or unknown scope coverage; directory-entry count; completed rotations;
refusals/failures; and fixed reason counts. A lost whole-scope receipt keeps action counts null because effects may
already have occurred. It never emits paths, file names,
contents or external exception bodies. Missing/unreadable enumeration is unknown
with null observed count, while a successfully listed empty directory is zero.
Action counters report attempts made, not an estimate of missing data or bytes.
Legacy `rotate` still returns a boolean and `sweep` a list of rotated names;
legacy operation failures still raise. Retention limits and linked-file guards
are unchanged. The detailed sweep continues peer files after a reported failure.

Maintenance independently measures engine, updater and hook scopes, adds each
attempt time and persists the receipt with its state. `maintain status`/doctor
whitelist stored fields, reject contradictory or malformed receipt rows and keep
measurement time plus age/stale state. One hour is the explicit freshness window,
not a claim that every scheduled pass succeeded. Health carries that same typed
payload and displays its incomplete/stale warning. No additional scan is run by
a reader. [Onboarding](../../ONBOARDING.md) and the OSS portable scenarios describe
the same boundaries.

## Validation

Small real-file fixtures and isolated HOME/TMPDIR only:
18 lifecycle tests (six new evidence cases plus twelve preserved log-policy
cases), five maintenance persistence/reader/compatibility cases and three OS
privacy boundary tests passed. The new API's initial four-case baseline failed
because it did not exist; an early peer-scope test correctly observed the absent
updater directory as unknown, so the positive fixture now explicitly creates it.
No production behavior was weakened to satisfy that fixture.

Coverage includes absent/empty/unreadable directories, linked directory,
hardlinked log, invalid generation, copy and fsync faults, chmod failure,
normal rotation, external same-name generation handling, independent scopes,
stale/malformed/contradictory receipts, post-effect receipt loss and private-marker omission.
[Structured check receipt](checks.json).

## Limits and next task

Rotation failures may already have moved/deleted older generations; counts do not
claim rollback. Copy/truncate can still lose a concurrent append, and directory
writers remain outside this process's exclusion. This work adds no new deletion
policy, live cleanup or automatic enablement. It does not resolve the separate
rotation-accounting acceptance program.

Next: independent fault/reader review of the exact commit, then a separately
routed integration and its required privacy/hosted checks. Installed acceptance
is NOT_RUN.
