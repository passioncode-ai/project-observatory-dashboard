# Bind adversarial remedies to the current consumer producer

## Bounded task packet

Scope: tests and evidence only. Production source and original invariant assertions
are unchanged. Use the inherited model and one-worker synthetic checks; no live
provider, operational vault or release action. Parent authorization covers this
fixture correction and requires independent review before integration.

Source baseline `1536add` completed128 seeds with70 convergence failures. The harness
unlinks `openrouter.json` before convergence and invokes revoke without refreshing it
(`adv_j1_property.py`, convergence block); its consumer-label cases also omit schema,
timestamp and degradation fields. Current `_revoke_scan` requires those fields and
`cmd_revoke` explicitly directs callers to refresh. `scan_openrouter.main` emits the
current schema and reports an unlisted consumer as degraded.

Plan: execute the actual producer over an isolated finite synthetic provider snapshot
and synthetic destination labels. Keep consumer observations across all refreshes and
convergence; never erase them to obtain an empty-success receipt. Refresh immediately
before documented revoke remedies. Preserve all original predicates, random event
choices, seed coverage and five-attempt convergence limit. Existing stale/missing
negative fixtures remain tests of refusal, not current-success fixtures.

Checks: producer-shaped known-empty positive; listed and unlisted consumed negatives;
stale/missing/degraded refusal; retained observations after refresh; no RNG draws,
provider writes, provider-call hooks or ledger mutation by the adapter. Replay128
seeds with exact source bindings and retain all residual failures. Full5200 remains
NOT_RUN until the adapter passes independent review. A finite complete test snapshot
is not a claim about real pagination, vault or remote consumer coverage.

Two new producer tests failed before implementation because the adapter was absent.
The adapter now passes known-empty, known-holder, unlisted, stale and missing source
checks. A separate regression retains the observed consumer through convergence.

Next task: replay the bounded baseline and request independent fixture review. No production reconciliation remedy is authorized by this packet.

## Bounded source receipt

Source `532b70911ec39fcb4590a6632283dc63dfcaecf5` passes the11-case
[focused suite](raw/tests.json). The [128-case replay](raw/baseline-128.json)
completed128/128 with104PASS,24FAIL,0ERROR, exit1. This is a failing adversarial
run. The [comparison](raw/comparison.json) preserves every previous failure and
identifies46 resolved seeds and zero newly failing seeds against the prior70-failure
receipt. All remaining findings are the original convergence assertion `(c')`.
Last-trace categories are diagnostics, not independently established root causes.
Independent fixture review is pending. Full5200, real collector pagination, live
providers, installed acceptance and full remote/vault consumer coverage remain open.

[Proposed next remedy design](remedy-design.md) separates explicit operator action,
provider observation and unresolved consumer/metadata conditions. Implementation
waits for independent design review.

## Explicit remedy implementation

Corrected source `6ee4e6c26a1f403b57584417ea380c193438eb38` passes19
[focused cases](raw/remedy-tests.json). The first six new helper tests failed before
implementation; a further renamed-identity refusal test failed on `f3ffea10` before
its correction. The independent reviewer also reproduced that attestation seam.

The [same128 seeds](raw/documented-remedies-128.json) now complete109PASS,19FAIL,
0ERROR, exit1. [Exact comparison](raw/remedy-comparison.json) identifies precisely
7000008,7000018,7000029,7000083,7000093 as improved; all other19 failures remain.
No original invariant or five-attempt bound changed. Explicit operator actions are
recorded in traces and stay separate from spontaneous recovery. Corrective source
review is pending; full5200 is still NOT_RUN in this receipt.
