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

The broad credential-door suite and final source checks are recorded in
[checks.json](checks.json). The combined original 5200-case remeasurement is
**NOT_RUN at preparation** and must bind the frozen candidate before any new
acceptance claim. Independently accepted component checks are bounded evidence.
Next: freeze this source, replay all original seeds, independently bind the
composition, complete private-name/history and protected exact-source CI gates.
No live provider call, vault mutation, merge, tag or install is claimed.
Versions remain application 0.25.0 and plugin 0.22.1 preparation. The separately
reviewed log-retention observability and open-unlinked work are excluded.
