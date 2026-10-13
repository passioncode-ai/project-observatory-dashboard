# Faithful adapter measurement: still not accepted

Frozen `153a1605fbbaf2caf1674a3f70cf2da3557934f3` completes all original 5200
seeds: **2147 PASS / 3053 FAIL / 0 ERROR**, exit1, full acceptance **NOT_MET**.
[Matrix](measurement/full-5200.json), [summary](measurement/full-5200-summary.json),
[execution](measurement/full-5200-execution.json). The single worker took 240.18
seconds, retained zero owned fixture entries, removed its temporary root and
preserved exact source hashes and HEAD throughout.

[Compared with the previous composition](measurement/full-5200-comparison.json),
45 failures now pass and zero passes regress. This quantifies interpreting the
actual emitted held-ceiling command, not a new provider or operator event. The
only changed model function is `limit_remedies`; original invariant bodies,
random draws, seeds and five recovery attempts are unchanged. Production remains
byte-identical to 5f6. The model hash is now
`374eee9807585e7599771c23866026746c924f9f8c586abb4025bc5a068c1a6b`.

No `(a)` or `(a-rot)` is reported. Five `(L)` issue occurrences remain across
three distinct seeds 7001212, 7003708 and 7004446. Consumer, unknown-identity,
unsupported-precision and missing-guidance outcomes remain unresolved. Group
labels in the summary describe output, not universal causal classifications.

The sequential [broad credential receipt](measurement/credential-doors.json)
is **542 PASS / 8 FAIL**, exit1, 82.69 seconds. Its [full log](measurement/credential-doors.txt)
retains the same convergence failures; the temporary fixture peak observed was
13,433,934 file bytes, with isolated HOME/TMP and unchanged clean source. Nothing
in the helper correction waives those assertions.

Next: implement only the independently reviewed
[decrease-only contract](../2026-10-13-decrease-only-repair/README.md), tests first,
with supplemental scenarios for the real CLI. Its future acceptance cannot be
retroactively substituted for this failed original model snapshot. Do not merge,
release or advertise full J3 completion from the scoped green helper checks.
