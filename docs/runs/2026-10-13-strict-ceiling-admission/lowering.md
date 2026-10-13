# Confirm released capacity before another policy increase

The policy planner previously assumed a requested decrease had freed its planned
capacity. With one key at $8 and a $10 policy split into $2 and $8, the provider
could acknowledge the PATCH with an observed $2.0004 limit. The subsequent exact
$8 POST then left $10.0004. [The synthetic counterexample](lowering-counterexample.json)
changes only the PATCH observation; it does not inflate the following POST.

Policy calls now pass their planned ceiling to `_set_limit_locked`. Before PATCH,
the provider key must match the recorded identity. Before recording success or
continuing to later increases, the mutation response must contain that same hash,
the requested reset period and a supported positive ceiling whose rounded-up cents
fit the planned amount. Missing, foreign, nonfinite, negative, zero, higher or
wrong-reset observations stop the partial run. The provider effect remains; the
old ledger amount is retained, and no mutation is retried. A successful response
records the actual confirmed amount, including a lower-than-requested observation.

This is confirmation from the same-operation provider receipt. It does not add
an independent GET, prove provider truthfulness, prevent an external change after
the response, or serialize other account writers. The generic standalone limit
command is unchanged; this correction governs policy admission that depends on
released capacity.

## Evidence and next task

The new nine-mode regression was added first: eight assertions failed and the
exact-response positive passed. After the correction, 13 focused policy functions
passed 80 assertions; [source-bound receipt](lowering-tests.json). The shared
policy fake now returns the PATCH fields it accepted, matching its existing
successful-effect contract, instead of omitting its effect from the response.
No random seed, invariant or full-model event choice changed.

Next: independent review of this exact effect-confirmation boundary, compose all
accepted changes, then run the original 5200-case measurement. Uncertain provider
behavior and original external-mutation representation failures remain visible;
this correction is not complete reconciliation or live-provider acceptance.
