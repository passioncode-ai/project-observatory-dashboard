# Saved reservations do not prove current admission capacity

Two additional synthetic counterexamples show why retaining uncertain effects
and proving a current upper bound are different obligations:

- An ambiguous POST leaves a hashless pending row and a $5 reservation. An
  external provider change makes its actual ceiling $8. A later $10 policy
  previously minted another $5 and left $13. The row was represented, but its
  saved bound was no longer current. [Original source-bound counterexample](pending-counterexample.json).
- A primary key at $5 has a successor whose actual ceiling is $8, while the
  ledger's successor reservation remains $5. If the extra's GET fails, a $20
  policy previously minted another $10, moving $13 to $23. The new rotation
  regression reproduces this for unavailable, foreign, malformed and hashless
  extra observations, with current-row and confirmed-404 positive controls.

`_counted` now needs a current provider row or a confirmed missing identity;
no missing-row path uses `limit_usd` as admission evidence. `_rotation_exposure`
also stops using predecessor/successor reservation values as fallback bounds.
A missing/malformed extra identity or phase is unknown. A matching current row
counts its supported ceiling, and an exact-hash GET returning 404 counts zero.

These are read/admission changes. Existing reservations, identities and pending
states remain in the ledger. A known-identity pending delivery can still supply
a current observed ceiling. Under a finite policy cap, unknown exposure blocks
new issuance and raises; it does not forbid separately justified decreases.
Explicit unlimited policy semantics are unchanged.

## Verification and remaining boundary

The new pending regression failed before the correction. The rotation tests
failed eight subcases before fallback removal. Final focused checks passed
51 credential assertions and 21 rotation tests; [source-bound receipt](unobserved-exposure-tests.json).
The missing-account observation also has a tests-first exception regression.
Existing tests that expected stale reservations to supply numeric current bounds
now require unknown while separately asserting reservation retention.

The source audit covered both primary `_counted` call sites and additional
`_rotation_exposure` accounting in `issue_for_policy`. Remaining writes to saved
ceilings record intent, effects or journal metadata; they are not used as current
admission fallbacks by these paths. This does not guard every standalone writer
or create an atomic snapshot across provider observations.

The two original `(L)` seeds still describe an external mutation immediately
invalidating an unknown identity's reservation. No local admission guard can
prevent that external effect or instantly establish the missing identity. Their
original FAIL results stay unchanged; only subsequent unsafe admission is
refused. Next: independent review, composition and the original 5200-case
measurement with all remaining failures retained. No provider operation ran
outside isolated synthetic fixtures.
