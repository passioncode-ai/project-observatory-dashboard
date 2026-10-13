# Frozen guidance measurement: incomplete full runs, failing broad suite

Source: `c862c596c3ce99216f01f89d1047773bfceb9f03`. Status:
**NOT_ACCEPTED**. This is a source and synthetic-test record, not a statement
about live provider behavior or installed operation.

## Results and their limits

| Measurement | Result | Completion | Evidence |
|---|---|---|---|
| Independent source/parser review | Scoped PASS, ten control groups | Complete within stated scope | [review](measurement/independent-source-review.json) |
| Original 5200, first attempt | Timeout after 900.553 seconds | NOT_COMPLETE; case counts unknown | [execution](measurement/j3-guidance-full-5200.json) |
| Original 5200, retry | Timeout after 1800.048 seconds | NOT_COMPLETE; case counts unknown | [execution](measurement/j3-guidance-full-5200-retry.json) |
| Broad credential suite | 542 PASS / 8 FAIL, exit 1, 702.87 seconds | Complete, terminal `8 failed` | [receipt](measurement/credential-doors.json) |

Both full attempts requested the original contiguous seeds 7000000 through
7005199, jobs 1. Before/after source hashes match and owned synthetic temporary
directories were removed. Neither produced its final case matrix. Completed,
PASS, FAIL and ERROR counts are **null**, not zero. There is no valid per-seed
comparison with the prior matrix. The latest completed full result remains
[153a160: 2147 PASS / 3053 FAIL / 0 ERROR](../2026-10-13-held-ceiling-remedy/measurement.md).
Historical complete failures and both incomplete attempts remain distinct.

The first wrapper sampled its owned temporary tree frequently. That observer
added measurable work; the retry and broad run disabled it. Their peak temporary
footprints are unknown. A longer timeout did not produce a completed full run.
This evidence does not establish deadlock or one exclusive cause of slowness.
The model replaces its operation journal with an in-memory append and uses a
short synthetic lock wait; real journal persistence cannot explain this run.

The broad result retains seven named property-(c) failures and an aggregate
property-(c) failure over 2000 cases: the suite reports 293 failing seeds. Only
examples are retained, so that aggregate is not an independently enumerable seed
list. Those failures concern
writes continuing after a clean rerun. The receipt lists the exact named seeds.
The count matches the previous broad assertion totals; that does not prove all
underlying observations or behaviors are identical. No acceptance criterion was
changed to make these results green.

## Why timeout loses useful evidence

At this source, `observatory/engine/tests/adversarial_runner.py` keeps normalized
case results in memory and publishes output only after the loop and source-after
verification. A killed run therefore loses its completed prefix. The next task
repairs the receipt boundary, not the model's assertions or unresolved behavior.

Proposed bounded contract, requiring independent design and implementation review:

1. Add an optional protected progress journal with an immutable source/seed header,
   a started-seed marker, normalized completed rows and a terminal source-after
   record. Append linearly; never rewrite the accumulated matrix per case.
2. Preserve original seed order, calls, exception classification, RNG, events,
   assertions and five attempts. Do not resume, add remedies or skip cases.
3. An unterminated journal is always INCOMPLETE. Count only durable completed
   records; a torn tail and an uncheckpointed case remain unknown. Source-after
   currentness stays unknown until it is checked. A passing prefix is not PASS.
4. Preserve prior complete records on writer errors; fail closed for unsafe
   destination types. Writer failure cannot make a run green. Retain the existing
   final report format and source verification for completed runs.
5. First test abrupt owned-child exit, mixed PASS/FAIL/ERROR rows, malformed/torn
   records, source drift, writer failure and ordinary completion using tiny fake
   cases. Compare model AST and invocation order. Keep unbounded captured output
   as a separately disclosed limit rather than silently changing it here.

Do not start a third blind full run. After the receipt change is reviewed, record
fresh resource observations and a chosen execution budget before measuring the
same original cases. This packet authorizes no service stop, provider action,
capacity assumption, ledger-digest relaxation or invariant waiver.

## Composition remains separate

This frozen candidate predates the observed main source
`a2716aa54e1a2ffeab3e5b07bc89fc3b48978561`. Main includes separately merged activity
reporting and earlier accounting/revocation changes. The candidate's conservative
rounding, bounded activity parsing and fixed error text must be compared with
those changes under review; a former replacement branch does not establish
equivalence. No current-main composition check was run in this measurement.

Exact next task: independently review the progress-journal contract, implement it
tests first without changing case semantics, review the resulting source, then
decide whether a fresh resource-bounded complete measurement is justified. Keep
this draft delivery and every prior red or incomplete receipt available.
