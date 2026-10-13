# Original 5200-case measurement: not accepted

The original range, seeds 7000000–7005199, ran serially on model source
`6ee4e6c` at checkout `f1dc6a7`. It attempted all 5200 cases; 5198 completed
invariant evaluation. Results: **4307 PASS, 891 FAIL, 2 ERROR**. Exit status was
1 and the runner's status was ERROR. [The full matrix](raw/full-5200.json)
preserves every case, original issue and source hash. No failure is waived.

[Execution metadata](raw/full-execution.json) records 264.07 seconds, one job,
unchanged before/after source and zero fixture entries left by harness cleanup.
Only the exclusively owned fixture root was removed. This was synthetic offline
measurement; no provider or vault access occurred.

## Triage

[The summary](raw/full-summary.json) retains every failed/error seed.
[Representative traces](raw/full-first-traces.json) cover each output category.
These counts are diagnostic groups, not a claim that all members share one cause:

| Outcome group | Cases |
|---|---:|
| Repeated explicit limit remedy | 448 |
| Blocked replacement capacity | 176 |
| Missing consumed identity | 118 |
| Extra policy holder | 128 |
| Pending absence attestation refused | 9 |
| Strict `(a)` ceiling violation | 10 |
| `(L)` representation violation | 2 |
| Unjudged injected save exception | 2 |

All ten `(a)` cases were replayed individually. Each performed a replacement POST
that moved a below-cap total above its strict cap after observing an externally
increased positive fraction. The subtraction in `_cents_up` discarded that
fraction. This is an engine admission defect, not just an external post-write
mutation. A separate production correction counts all observed fractions and
retains blocked convergence rather than relaxing the cap.

The two `(L)` cases, 7001212 and 7004446, lose reservation coverage immediately
after the model externally increases an unknown mint's capacity. No intervening
engine action caused that first violation. Their pending reservation remains;
its assumed upper bound no longer holds. Both stay FAIL, and reconciliation is
open. A bounded listing cannot authorize guessing the identity or new capacity.

Both ERROR cases, 7001556 and 7003454, inject a ledger-save OSError during
`cmd_revoke`. The real CLI entry point catches `(OSError, ValueError, RuntimeError)`
and exits 2. The direct model wrapper only caught simulated process death.
These cases were therefore unjudged, never passing. A separate test-only adapter
must reproduce the real exception boundary while retaining effects, ledger state,
original failure hooks and all post-action invariants.

## Next task

Review the strict production admission correction and the separately tested CLI
boundary adapter. Replay their original counterexamples, then remeasure the
unchanged 5200 range on the actual combined source. Full convergence, meaningful
successful policy rotation, complete consumer coverage and external writer
serialization remain open. This full measurement replaces NOT_RUN for this exact
source only; it does not establish acceptance of the system.
