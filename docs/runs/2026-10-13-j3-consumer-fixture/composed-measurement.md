# Composed 5200-case measurement: not accepted

The unchanged original seeds 7000000–7005199 ran on exact combined source
`e22b986e837125ce715a319bd7c47b5527081349`. All 5200 cases completed invariant
evaluation: **3501 PASS, 1699 FAIL, 0 ERROR**, exit 1. The
[complete matrix](raw/composed-full-5200.json) retains every outcome and the
SHA256 of every source dependency. The [execution receipt](raw/composed-full-5200-execution.json)
records 330.37 seconds, one job, identical source before/after, zero remaining
fixture entries and removal of only the exclusively owned temporary root.
No live provider, credential store or installed runtime was exercised.

The [original measurement](full-measurement.md) is preserved unchanged.
[Per-seed comparison](raw/composed-full-5200-comparison.json) records 820 former
PASS cases now failing and 14 former non-PASS cases now passing. Safer refusals
are not convergence and do not turn those new failures into accepted outcomes.
The full requirement remains unmet.

## Results and scope

[The summary](raw/composed-full-5200-summary.json) lists every failed seed. These
are output categories, not proof that every member shares one root cause:

| Output category | Cases |
|---|---:|
| Blocked capacity | 408 |
| Other convergence failure | 293 |
| Repeated explicit limit remedy | 750 |
| Missing consumed identity | 119 |
| Extra policy holder | 124 |
| Pending absence attestation refused | 4 |
| Representation `(L)` | 1 |

No `(a)` or `(a-rot)` violation appears in this finite run. All ten original
strict `(a)` seeds were also replayed: each now refuses replacement capacity and
still fails convergence. Both formerly unjudged injected-save cases, 7001556 and
7003454, now pass their original assertions through the real CLI error boundary.
These facts establish the measured scope, not a general proof against external
writers or all provider behavior.

[Priority and representative traces](raw/composed-full-5200-traces.json) replay
all original/current strict failures and errors, plus the first seed in each
output category, without altering the provider, RNG or assertions. Remaining
`(L)` seed 7004446 still loses reservation coverage when an external model event
increases an unknown minted object's ceiling. That first violation precedes a
subsequent engine operation; later explicit model operator cleanup does not
remove the earlier failure. Seed 7001212 now reaches a different refusal path,
remaining a convergence failure rather than the original `(L)` event.

Representative 7000002 repeatedly receives a ceiling higher than requested after
PATCH. Rejecting that response protects headroom but does not complete recovery.
Representative 7000005 repeatedly applies the documented limit remedy and sees
an above-request provider value. Representative 7000000 retains blocked capacity
after an above-request echo. Complete response fields therefore do not make these
precise echoes safe to round away. Consumer/unknown-identity refusals likewise
remain explicit; none authorizes inventing absence or deleting a held identity.

## Exact next task

Independently reconcile these receipts, then separate implementable recovery
changes from assumptions that an external provider would need to satisfy.
Propose and reproduce each bounded correction before changing source. Preserve
all seeds, original assertions and the five-attempt bound. Investigate the
separate legacy property(c) convergence failures under the same strict precision
contract; do not increase cap tolerance or waive the original acceptance rule.
Meaningful successful policy rotation, complete consumer coverage, external-writer
serialization and full convergence remain open.
