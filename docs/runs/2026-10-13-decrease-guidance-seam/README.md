# Emitted decrease guidance reaches the guarded CLI

Status: independently reviewed source candidate at c862c596, NOT_ACCEPTED as a
complete system. Runtime dependency b4fbc94 passed scoped review and was delivered
with handoff 89e8cd7. This candidate adds the separate guidance/parser seam. Two
full measurement attempts timed out without publishing case counts. The separate
broad suite completed with 542 PASS / 8 FAIL. The latest completed full matrix
remains 153a160: 2147 PASS / 3053 FAIL / 0 ERROR. See the
[frozen measurement and next task](measurement.md); none of these results is waived.

## Baseline source seam before this change

At 5abd295 `issue_for_policy` appends generic `limit NAME --set USD` to `settle`
for near-target held-ceiling rows. `settle` is only printed in the over-limit
row. Thus no-aggregate-breach held-ceiling rows and newly observed above-share
PATCH results may have no executable correction text. The actual guarded door
exists as `limit NAME --set USD --decrease-only`; it is NOT_WIRED into these plans.

`adv_j1_property.limit_remedies` reads only the over-limit tail, accepts its exact
old generic command and validates target/identity/plan/ledger/decrease. Its apply
function calls general `cmd_limit`, not the new flag. The existing source precheck
must never be relabelled as production decrease authority.

## Proposed small change

1. For a held-ceiling row with the policy's existing positive target, emit the
   exact existing guarded command, preserving the source's name, amount and
   action label. Make it visible on the actual CLI even without an over-limit
   aggregate. Describe this as a request for one observed decrease, not a promise
   of convergence or automatic capacity recovery. Do not derive a new amount,
   change reset, enable a disabled key or mutate during planning.
2. Where the old over-limit settle text emitted the same generic limit command,
   replace it with the guarded form. Deduplicate a single command represented in
   both row guidance and aggregate guidance. Existing revocation semantics remain
   unchanged. Do not infer permission from arbitrary prose, action labels alone,
   internal provider fields or an adapter's unspoken target.
3. Adapt the model to parse only actual emitted guarded text; preserve the exact
   target, one matching plan row, exact current identity, plan and ledger digests,
   supported positive decrease and all original failure assertions. Apply through
   the actual CLI parser/dispatcher with the emitted arguments. Missing flag does
   not acquire guarded meaning. No new random draws, events, recovery attempts,
   provider hooks, secret fixtures or automatically invented operations.
4. An unresolved effect remains unresolved. Do not silently refresh, clear repair
   evidence, consumers or pending identities just to let a retry proceed. Any
   future observation-action adapter requires an actual emitted observation
   command and separate review; absence of that feature may leave cases red.

## Tests before edits

- Real source emits a flagged command for held-ceiling with and without aggregate
  overflow, and for a measured above-share lowering response.
- Real parser dispatch reaches `decrease_limit`; stale smaller current capacity,
  identity/reset drift and malformed capacity refuse before PATCH.
- One exact decrease passes; equal no-op uses no write; unknown effect preserves
  intent and supplies no fabricated success or automatic retry.
- Unflagged/no-emission/foreign name/duplicate row/mutated plan or ledger/raise/
  unsupported amount is rejected by the adapter. Proposal discovery writes
  nothing and consumes no RNG draws. All original assertions and event ASTs stay
  byte-identical, aside from the reviewed remedy translation functions.
- Replay prior actionable seeds 7000017/7000032/7000039, old repaired
  7000008/7000018/7000029, strict 7000828, consumer 7000072, pending 7002628,
  external-L 7003708, and invalid-policy 7000104. Record actual outcomes rather
  than predicting that every case becomes green.

## Acceptance and integration

Independent exact source/parser/model review precedes a new complete measurement.
Keep the 5200 seed list, every original invariant and the five-attempt bound.
Only a source-faithful, accepted remedy adapter warrants full remeasurement.
The separate main changes 4c3c794 and 1b91f28 still require composition review;
4c retains a smaller epsilon and is not a substitute for strict upward rounding.
No live provider, vault, consumer or installed-runtime operation is authorized
by this test-only/source-development packet.

## Independent design clarification

The reviewer accepted the direction with two required controls. Emitted USD must
round-trip to the exact existing planned target; `:g` can lose significant digits
(e.g. 123456.78) and is not acceptable command serialization. The adapter must
invoke the real parser with an argument list, never a shell, and emulate only the
real entry exception tuple (OSError, ValueError, RuntimeError). Unexpected failures
must escape and remain model errors, not become a generic unresolved/pass result.
No arbitrary Exception catch, hidden refresh, new operator event or extra attempt.
The runtime dependency is the corrected source candidate b4fbc94, whose independent
scoped source review passed, not the rejected first candidate. Implementation review
subsequently passed at c862c596 within the scope recorded in the measurement packet.

## Candidate evidence and exact next task

The [baseline](baseline.json) binds three actual-source counterexamples to
89e8cd7. [Tests-first red](red.json) records 28 cases with two failures and three
errors before runtime/model edits. [Focused receipt](focused.json) records 30
passing cases, actual CLI output/parser dispatch, exact large target, no-aggregate
and post-effect guidance, unknown-effect preservation, negative identity/context
controls, and exact entrypoint exception handling. Only `limit_remedies` and
`apply_limit_remedy` have changed model function ASTs; every other function,
including original event/RNG/five-attempt/assertion logic, is identical.

[Prior safety receipt](safety-focused.json): all 28 decrease-door tests and all
115 prior assertions pass. Inventory check and whitespace check exit zero;
brand lint exits zero with 2407 warnings, not a warning-free result.
Representative original seeds: 17/32/39/8/18/29/104 pass; 828/72/2628/3708 fail
(all prefixed 7000000). Exact seeds and traces are in `focused.json`.
These focused results precede the separate measurements linked above.

Fixture changes follow the new production representation: no-emission negatives
remove the row's command rather than only aggregate text; an unknown effect now
asserts no retry, while persistent echo uses a separate fresh fixture. No old
failure receipt or production acceptance criterion was changed.

Independent exact source review and immutable privacy/history gate passed for
c862c596, published as draft PR #276. Full measurements did not complete; the
separate broad suite is red. Next: review and implement the bounded progress
receipt contract in the measurement packet before another expensive full run.
Current-main composition, live/provider and installed acceptance remain open.
The new command requests an explicit action; proposal discovery does not execute
it or make external writers atomic.
