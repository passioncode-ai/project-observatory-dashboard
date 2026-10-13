# Proposed J3 bounded decrease-only repair contract

Status: scoped design review passed; implementation and acceptance remain pending. No production implementation, provider operation or
new automatic remedy. Source baseline `153a1605fbbaf2caf1674a3f70cf2da3557934f3`;
production SHA256 `602d50bd7f594c74585c10eff3388bd68a16d90a0d5e26cb85c4c50e826e31ba`.
Prerequisite evidence: [actual CLI semantics](manual-semantics.json)
and [held-share guidance gap](guidance-boundary.md).

## Decision proposed

Add `limit NAME --set USD --decrease-only`, an explicit caller-requested repair.
Keep general `limit NAME --set USD` semantics separate. This flag never derives a
new amount, divides a budget differently or treats an observed fraction as noise.
It accepts an exact issued ledger NAME rather than guessing an identity from a
label. It preserves the existing reset period; combining it with `--reset` is
refused in this first unit. Missing, malformed, unlimited, zero or negative fresh
capacity refuses before PATCH. The requested amount must be supported, positive
and no greater than the fresh finite observed ceiling. Equal target is a read-only
no-op. A current smaller ceiling refuses instead of raising it to the old target.

Add `limit NAME --repair-status --refresh` as an explicit observation operation.
It may update local repair evidence from an exact-hash read, but never PATCHes,
DELETEs, mints, delivers or retries the earlier request. `--repair-status` without
refresh is ledger-only and reports unknown/stale observation honestly. Flags that
mix observation with `--set` or `--reset` refuse. Names/help remain proposed until
scenario and copy review in the implementation change.

## Preconditions and authority

The existing policy lock covers the whole local read/intent/effect/commit step.
Resolve an existing account and exact primary hash; refuse pending issuance,
unresolved rotation, secondary identities, conflicting aliases or unresolved
prior repair evidence. Fresh provider identity must equal the ledger primary;
reset must match the recorded period. Missing/foreign/malformed metadata is not
an absence proof or finite capacity. Local account-label equivalence does not
remove an exact-hash conflict on another row.

The explicit target is the caller's authority. A stored historical policy digest
is context only, never a current grant/headroom calculation. The step does not
increase this key when the provider obeys the request and observation contract;
it does not establish that the whole estate was below its cap beforehand.
No new headroom may be inferred from the requested amount or a saved ledger value.

The destination reference and existing policy digest, if present, bind the intent
to its current ledger context. They do not prove complete consumer discovery.
Lowering can interrupt a consumer's use; the explicit caller target authorizes
this limited change, not consumer cutover, deletion or issuance. Retain all
consumer observations and never clear holder evidence as part of this command.

## Durable state and one effect

Use one bounded `ceiling_repair` object on the existing issued record:

- schema version, unique attempt id, status and attempted/observed timestamps;
- exact primary identity and reset binding; requested target; fresh-before amount;
- digest of relevant ledger identity/destination/policy context;
- observation basis (`same-operation-response` or `subsequent-current-read`);
- actual amount when known, otherwise null plus a bounded reason category.

Never store credential values, new labels/tails, raw command arguments or provider
error bodies. The original ledger amount remains historical until actual evidence
is committed; intent never overwrites it with the requested target.

Persist intent before any provider effect and require the existing durable journal
pre-action record. If either write fails, do not PATCH. An intent left after a
pre-action failure remains unresolved until explicit observation; this is safe
incomplete work, not proof the provider effect happened. Call PATCH at most once.
After timeout, malformed/foreign response or final ledger/journal failure, retain
intent/unknown evidence and refuse another mutation. Never retry automatically.

A complete response must match primary hash and reset, contain a finite positive
actual amount and be no greater than the fresh-before amount. Commit actual,
never requested fiction. If actual exceeds target but does not increase exposure,
report `observed-unmet`, exit nonzero and preserve the exact actual value. If actual
fits target, report observed completion. A response above fresh-before is an
unconfirmed unsafe provider outcome, not successful decrease; retain uncertainty.
A subsequent full read is needed before any further request. Write the required
post-effect observation journal record before committing the final ledger state:
if that journal or final save fails, the durable ledger intent remains unresolved.
A journal observation and an unresolved ledger intent are distinct receipts, not
a falsely completed command.

## Explicit observation and alias safety

An unresolved repair blocks mutations through every existing writer of that exact
identity, including aliases, and supplies no saved-value headroom to policy
admission. Plan a small shared guard alongside `_require_resolved_rotation` rather
than implementing separate inconsistent checks. Read-only commands remain usable.

`--repair-status --refresh` obtains one current exact-hash/period/finite-capacity
observation under the local lock and rechecks the intent's identity/context digest.
It records `observed-satisfied` or `observed-unmet` according to the target and
updates the ledger actual amount. This resolves what is observable now, not the
historical delivery of an uncertain PATCH. A current amount above the old before
value may be recorded as current unmet evidence, with the historical unknown/unsafe
outcome retained; the refresh itself performs no increase. It does not automatically reapply the
request. A future separate invocation is a new explicit attempt with a new fresh
check. Missing identity, wrong reset, unread or changed context stays unresolved;
404 does not erase the issued row or any consumer evidence. Historical intent and
outcome references remain journaled; do not accumulate an unbounded ledger array.

## Boundaries that remain real

The provider has no transaction with this local lock. An external writer can
change capacity between GET and PATCH or after the response, and a provider can
return an inaccurate observation. Thus this is a bounded observed-decrease
contract, not global atomic policy enforcement or unconditional no-increase proof.
Unknown effect never authorizes spending the apparent released capacity. The
original strict model and its external `(L)` failures stay unchanged.

The existing model adapter's precheck is not promoted to real authority by this
proposal. Initial tests of this command are supplemental scenarios. Any eventual
original-model adapter must follow actual emitted guidance and the real command,
retain all invariants/RNG/events, and receive independent review separately.

## Implementation packet and tests-first order

1. Review this spec and refuse implementation if unknown-effect reconciliation,
   local alias blocking or historical-vs-current evidence remains ambiguous.
2. Add synthetic actual-CLI tests: exact decrease; equal no-op; fresh smaller
   amount; missing/foreign identity; unlimited/missing/negative/bool/nonfinite
   amount; wrong reset; policy-context drift; pending/rotation; same-hash alias.
3. Test every durable seam: intent failure, pre-action journal failure, timeout
   before/after effect, malformed response, valid higher-than-target nonincrease,
   response above before, final ledger failure, and journal failure. Assert at
   most one PATCH, no mint/delete/delivery and retained prior observations.
4. Test status refresh after each uncertain seam: no provider mutations, exact
   current actual preserved, satisfied/unmet distinct, bad source/context refuses,
   no consumer evidence erased and no historical-response confirmation fabricated.
5. Test policy admission and all existing writer aliases refuse while unresolved;
   explicit refresh permits only subsequently guarded work. General manual limit
   remains compatible and cannot silently bypass an unresolved repair record.
6. Update CLI parser/help, actual output schema if applicable, portable scenarios,
   brand strings and source inventory together. Add exact safe guidance only after
   the real command exists. Independent source/probe review precedes integration.

This is one bounded repair door, not the full J3 reconciliation engine. Source
ownership: one isolated developer branch; shared product files require leases.
No live provider/vault tests or deployment. Preserve the existing 5200/broad failed
receipts and measure supplemental acceptance separately.

## Review clarification: guard order and refresh state

Independent spec review required two precise amendments before implementation.

**Guard before local publication as well as provider effects.** Presence of a
malformed `ceiling_repair` object, unknown schema/status, missing identity/digest
or invalid timestamps is unresolved, not legacy absence. An exact-hash alias
cannot bypass the guard through another account label. Check before any mutation
of identity, destination, policy provenance or saved ceiling on that identity.
In particular, `issue_for_policy:1046` re-attests `policy_binding` before its
admission loop today: guard the complete affected identity set before that write.
Do not let policy re-attestation invalidate a pending repair's own context digest.

The writer audit must cover `_issue_key_locked`, internal `_forget`/`_undo_mint`,
`issue_for_policy`, `_set_limit_locked`, `_rotate_one_locked`, `claim_key`,
`forget_pending_key`, `revoke_key`, and the provider-only `toggle_key` path.
Callers through the key server use those same guards. Internal mint cleanup must
remain legal for its own new row while never becoming a way to erase an unrelated
repair. Claiming an alias of a protected identity is a context mutation and must
refuse. `save_ledger` call sites are an audit inventory, not proof alone that all
writers are guarded; test the actual paths with an unresolved same-hash alias.

**Every refresh is a new observation attempt, even after a prior known result.**
Under the local lock, first validate existing context and persist a refresh intent
with current attempted_at, status `observation-pending`, and actual=null/reason
`not-observed-this-attempt`. Keep previous measured amount/time explicitly as
historical, never fresh. Require the pre-action observation journal before GET.
Then perform the one current identity read. On failure, persist a bounded reason
if possible; the already durable observation-pending state remains unresolved if
that update also fails. No mutation is permitted from the prior resolved state.

On a valid read, require an observation-result journal before publishing final
`observed-satisfied` or `observed-unmet` state and actual/time to the ledger. If
journal or ledger publication fails, the pending state stays authoritative. This
same ordering applies when refreshing a previously observed result; a failed new
attempt cannot hide behind its old success. The command explicitly authorizes
local observation-state writes but never a provider mutation. A context mismatch
refuses before any refresh write or GET and keeps the old record for reconciliation.

The single repair object keeps bounded current/previous observation fields; the
journal preserves older attempt history. Current attempt and prior measurement
timestamps are separate. Unknown is not zero, and the returned evidence never
labels prior actual as measured by a failed refresh.
