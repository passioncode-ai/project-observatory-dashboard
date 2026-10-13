# Strict capacity admission and original-model replay

This source candidate follows the holder/prune checkpoint `12b7343`. It counts
every positive observed provider ceiling fraction and treats a held key whose
provider state cannot be read as unknown capacity. Hashless pending identities and
unobserved rotation extras retain their reservations but supply no current headroom. Neither a rounding allowance
nor an old saved ceiling may authorize a new provider POST. A fresh successful
read can establish bounded headroom; external writers after that observation
remain outside this point-in-time guarantee. Before a later policy increase, a
PATCH response must confirm the same key, reset period and ceiling no higher
than the planned bound. This is same-operation response evidence, not an
independent GET or proof against later external changes.

The model now builds current consumer evidence and gives explicit limit remedies
a fresh observation between bounded attempts. Its revoke adapter catches the same
three exception classes as the actual CLI; effects and invariants are retained.
The invalid-budget seed 7000104 remains a rejection/unchanged-state check. No seed
or invariant was suppressed to obtain acceptance.

## Evidence and provenance

- [Strict fractional admission](../2026-10-13-strict-ceiling-admission/README.md)
  and [unknown exposure refusal](../2026-10-13-strict-ceiling-admission/unread.md)
  record tests-first counterexamples and focused checks.
  [Unobserved reservations](../2026-10-13-strict-ceiling-admission/unobserved-reservations.md)
  and [PATCH confirmation](../2026-10-13-strict-ceiling-admission/lowering.md)
  close the adjacent independently reproduced admission paths.
- [Model fixture and remedies](../2026-10-13-j3-consumer-fixture/README.md) and
  [CLI boundary](../2026-10-13-j3-consumer-fixture/cli-boundary.md) describe the
  synthetic corrections, including failed intermediate evidence.
- [Original full measurement](../2026-10-13-j3-consumer-fixture/full-measurement.md)
  reported **4307 PASS, 891 FAIL, 2 ERROR**, exit 1. Ten strict-capacity failures
  motivated the production correction; two adapter errors motivated the CLI
  boundary correction. This old measurement remains evidence, not final-source
  acceptance. Convergence and representation failures remain open.
- [Composition mapping](composition.json) records original-to-delivery commits
  and final runtime/test source hashes. Original local commits need not resolve
  in a fresh public checkout; use the delivery lineage and hashes instead.

## Current gate and next task

Exact `e22b986e837125ce715a319bd7c47b5527081349` is **RED, NOT ACCEPTED**.
[Checks](checks.json) record the broad credential-door suite: 518 PASS assertions,
five FAIL, exit 1. Four named legacy seeds and the 2000-case property(c) aggregate
failed; the aggregate contains 348 failing seeds. The four named cases select a
provider echo that stores a positive fraction as actual capacity. The strict
refusal remains correct, but repeated clean reruns still attempt PATCH and fail
convergence. This is not a missing response field and cannot justify rounding
capacity away. The intermediate 511-assertion pass is retained separately.

[The final original 5200-case run](../2026-10-13-j3-consumer-fixture/composed-measurement.md)
completed all cases: **3501 PASS, 1699 FAIL, 0 ERROR**, exit 1. No strict (a) or
(a-rot) violation appears in this finite run; one representation (L) failure and
convergence failures remain. Relative to the original receipt, 820 cases newly
fail and 14 newly pass. Safer refusal does not establish convergence.

Both jobs used isolated temporary state and left source unchanged. The model ran
330.37 seconds; the broad suite ran 124.98 seconds with 13,318,102 observed peak
temporary bytes sampled every half second. Documentation-only successors retain
the entire `observatory/` tree `58eb053d1240a722822ad5dde55d044f337a8e39`.

Next: independently reconcile these red receipts, distinguish implementable
recovery from unverified provider assumptions, and prove each bounded correction
without suppressing seeds or loosening original invariants. The protected release
PR remains draft; CI readiness, merge, tag and installed acceptance are not claimed.
No live provider call or vault mutation occurred. Versions remain application
0.25.0 and plugin 0.22.1 preparation. Separate log-retention observability and
open-unlinked work are excluded from this measured source.
