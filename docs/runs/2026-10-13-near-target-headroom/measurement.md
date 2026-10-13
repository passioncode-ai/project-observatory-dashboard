# Current composition remains failed

Source `5f6f2a98a8ad952deeb153857d22c442276f0f4b`, after independently reviewed
lowering progress and near-target admission. The [full original matrix](measurement/full-5200.json)
is **2102 PASS / 3098 FAIL / 0 ERROR**, all 5200 requested seeds judged. Exit 1;
full acceptance is **NOT_MET**. Scoped independent acceptance of individual
corrections does not change this result.

[Execution](measurement/full-5200-execution.json): 249.86 seconds, one worker,
900-second deadline, owned temporary fixtures removed, zero retained fixture
entries, source hashes and commit unchanged. The model remains SHA256
`059eeddcea7a08bbe4e92ffa496fc243d72915c1976bb39b3b923be6fb0b88a9`.
No original invariant, event, random draw or five-attempt recovery bound changed.

[Comparison against e22](measurement/full-5200-comparison.json): 1413 prior passes
now fail and 14 prior failures pass. The latter are the 13 targeted cases and
7003180. Earlier failed full matrices remain intact; this is a new snapshot.
[Input correlation](measurement/input-cross-tab.json), not causal proof, partitions
the 1413 regressions into 74 exact, 91 rounded, 54 downward and 1194 positive-echo
inputs. They cannot all be dismissed as unsupported provider precision.

[Summary](measurement/full-5200-summary.json) separates issue occurrences from
unique seed counts. There are no `(a)` or `(a-rot)` reports. Five `(L)` occurrences
belong to three seeds: 7001212, 7003708 and 7004446. Their first representation
failures follow external subcent changes to unknown minted objects; unchanged
reservations cannot guarantee bounds against an external writer. The failures
remain failures. This is not evidence that unknown objects were reconciled.

[Priority and representative traces](measurement/full-5200-traces.json) show two
additional actionable boundaries. In 7000008/7000018/7000029, the existing exact
manual decrease is still emitted, but the test adapter accepts only the former
`in-place` action and ignores `held-ceiling`. A faithful adapter correction needs
its own tests and review. In 7000017/7000032/7000039, the aggregate fits and there
is no planned increase; the truthful held share has no emitted correction step.
That user-path gap needs a separate decision, without inventing lower shares or
automatic retry loops. Consumer and pending-identity refusals also remain open.

The sequential [broad credential receipt](measurement/credential-doors-final.json)
reports **542 PASS / 8 FAIL**, exit 1, 86.81 seconds, approximately 13.43 MB of
observed temporary file bytes. HOME and TMP were isolated; source remained clean
and unchanged. [Log](measurement/credential-doors-final.txt): seven retained
seed regressions and the aggregate legacy convergence assertion fail; 293 of its
2000 property seeds fail `(c)`, while `(a)` and `(b)` pass. This also blocks full
acceptance and must not be replaced by the 115 focused passing assertions.

Next: independent recount and representative review, then a separate tests-first
adapter correction limited to interpreting the actual emitted held-share command.
Preserve this matrix. Do not silently waive original outcomes, change a policy
split, clear consumers or perform live provider actions to manufacture convergence.
