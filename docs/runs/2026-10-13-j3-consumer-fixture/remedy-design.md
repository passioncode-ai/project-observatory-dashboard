# Proposed source-backed remedies for the remaining adversarial cases

Status: accepted for test-only implementation after independent review of `d17b477`. This extends fixture source
`532b70911ec39fcb4590a6632283dc63dfcaecf5` and the
[24-case classification](residuals.md). No production mutation or live provider
operation is authorized. The original range is7000000–7005199 inclusive; all5200
requested cases and invariants remain required. The128-case receipt stays FAIL.

## Contract and authority

The adversary's original `(c')` asks whether applying remedies named by the door,
after failures stop, serves every policy slot and reaches a write-free next run.
It does not assert spontaneous recovery without caller action. Consequently a
fixture can faithfully execute an existing documented caller remedy, while its
receipt must distinguish that intervention from an automatic engine action.
The strict capacity `(a)`, represented identity `(L)`, accepted cent plan `(b)`,
convergence `(c')`, consumer protection `(R)` and invalid-policy unchanged-state
checks remain intact. No assertion, seed, denominator or five-attempt limit is removed.

Source authority is the generator's module contract and current door at the source
commit above: `openrouter.py:1056–1063` emits exact limit guidance; `:1229` implements
the command effect; `:1579–1600` implements journaled pending attestation; `:1724–1729`
names its prerequisite. A test operator admission is not a provider observation.
Local account names or project-folder resemblance never prove exact identity.

## Unit1: explicit ceiling decreases

Execute only the structured interpretation of an exact `limit NAME --set AMOUNT`
remedy actually present in the current over-limit plan. Use a narrow allowlist parser,
never a shell/eval or a command copied wholesale from prose. Resolve NAME against
that same plan/ledger; require its exact primary identity, unchanged ledger record,
finite positive planned amount and a current provider row showing that amount is
not a raise. Do not compute a smaller amount to manufacture headroom. Invoke the
real `cmd_limit` against the existing synthetic provider, so original write counters
and capacity observers see every effect. Preserve reset semantics. Record proposed,
operator-admitted, applied/refused and observed-after separately.

Candidate cases are7000008,7000018,7000029. Their plan explicitly names decreases
which the old convergence loop ignored. This is a testable prediction, not a pass.
Positive control: exact-source remedy lowers the exact identity and the next policy
run serves all slots with a write-free repeat. Negative controls: raised target,
foreign/missing identity, changed ledger, missing/malformed remedy, failed or unknown
PATCH, and persistent positive metadata echo. Refusal never updates the model's
capacity to the desired value. Unknown outcome remains represented and failing.

## Unit2: explicit synthetic operator attestation

Ordinary revoke stays the first route. A hashless pending row past the bounded listing
may use the already documented `--forget-pending --reason` branch only after a separate
synthetic operator event establishes absence in that fixture's complete finite world.
Bind the event to the exact ledger revision, row name/account and pending-no-hash shape.
No live or potentially live matching identity, retained consumer observation, failed
inventory or unknown outcome permits that event. Do not infer absence from a bounded
listing, a user-supplied reason string, or a changed display name. The existing
harness's simulated manual provider deletion is observable prerequisite evidence,
not evidence that production can recover an unknown hash automatically.

Then call real `cmd_revoke(..., forget_pending=True, reason=...)` and capture the
required attestation journal event. The receipt calls it operator-attested, never
provider-confirmed. Production intentionally accepts this assertion and does not
verify its truth; the model adapter owns its synthetic proof precondition. Do not
claim the adapter's guard as a new production security feature.

Candidate cases7000083 and7000093 remain failures until this behavior is tested.
Negative controls: live matching key, unknown/incomplete inventory, known hash,
changed row, consumer still bound, missing reason, and required-journal failure.
All must retain the row/uncertainty and make no unaccounted replacement. A positive
case records the attestation, clears only that row, then serves the missing slot.

## What cannot be closed by these adapters

Ten positive-echo over-limit cases and three echo-blocked replacement cases remain
red. `_asked` is the fake observer's private state, not a production authority source.
Do not use it to bypass reported exposure, reset the echo after fault injection,
round away a possible ceiling or silently underfund a slot. Even repeated documented
PATCHes may leave the disagreement; retain those findings.

Six cases require an external consumer transition:7000025,7000028,7000045,7000072,
7000087,7000089. Stopping injected faults does not stop a consumer. Policy removal
does not authorize deleting the identity it still reads. Keep those cases failing.
A later, separately reviewed operator-handoff model may demonstrate progress only
with exact old/new identities, admitted destination ownership, durable delivery
outcome, fresh complete consumer readback and unchanged source binding. Unknown
or failed delivery retains the old binding. Changing a label, deleting a dictionary
entry or writing an empty scan is never the handoff. Positive handoff would be an
additional scenario, not retroactive success for an original case without that event.

## Delivery coverage limit

`make_door.deliver` currently returns a synthetic result or raises before any modeled
consumer-state effect. Explicit `consumer-label` events populate the destination
survey; delivery alone does not populate every vault/remote holder. Therefore the
fixture does not cover live vault/remote discovery or an after-effect delivery timeout.
That future model must separately cover before-effect, confirmed-after-effect and
unknown-after-effect delivery. No consumer disappears just because the call failed.

## Execution and acceptance packet

1. Review this design and its source bindings before editing the adapter.
2. Add tests first for both positive commands and the listed negative proof boundaries.
3. Implement only tests/helpers; no production code, new automated remedy or provider
   semantics assumption. Keep the existing source-stamped baseline unchanged.
4. Run focused tests with one worker, then the same128 seeds on a committed source.
   Compare exact seed identities; candidate improvement is at most the five named
   cases, while the other19 must remain failures under unchanged assumptions.
   A different result requires investigation, not a rewritten expectation.
5. Independent review replays named counterexamples and challenges the operator and
   provider proof separation. Preserve all prior failing receipts.

Full5200 remains NOT_RUN here. Universal refusal of policy-marked rotation does not
prove useful successful rotation or reconciliation. Meaningful positive rotation,
strict ceiling preservation across uncertain effects, all-writer policy admission,
full holder coverage and installed/live acceptance remain separate open requirements.

## Implementation qualification

Pre-action digest checks are test preconditions, not a new production compare-and-swap
transaction. The real commands take their existing non-reentrant policy locks; the
adapter acquires no nested lock. External changes after its precheck remain outside
this simulated phase. No production race-safety claim follows from these negatives.
The producer fixture at `532b709` passed six independent probes and representative
residual replays. The remedial adapter is a separate change requiring its own review.

## Renamed identity correction

The first implementation `f3ffea10` wrongly allowed an absence attestation when a
live model-owned identity had been renamed away from the pending name. A regression
failed before correction, and independent review reproduced it. The adapter now
requires every synthetic model-owned key to have an explicit primary, predecessor
or successor ledger reference; unknown/malformed ownership refuses. The ownership
fact is model data, never inferred from a display-name prefix, and is included in
the inventory digest. The finite operator-proof scope remains separate from real
provider discoverability. The original failing source receipt is retained.
